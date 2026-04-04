"""Gemini Live ↔ LiveKit audio bridge for Aura — Google SDE Interview Coach.

Architecture (ADK + google-genai):

  Browser mic → LiveKit room (WebRTC)
    → rtc.AudioStream → PCM16 @ 16 kHz
    → google-genai aio.live session (Gemini Live native audio)
    → PCM16 @ 24 kHz response audio
    → rtc.AudioSource → LiveKit room → Browser speaker

Session persistence (100% Google Cloud):
  - google.adk.sessions.VertexAiSessionService stores full conversation
    history per user_id on Vertex AI Agent Engine.
  - Before a live session the prior turn history is injected into the
    Gemini Live system context so Aura remembers past interactions.
  - After the live session ends, new turns are appended to the ADK session.

Agent definition (google.adk):
  - bot.agent.build_adk_agent() returns an LlmAgent with instruction + tools.
  - Tools defined in bot.agent are declared to the Gemini Live session so they
    fire identically in real-time audio as they would in text turns.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import numpy as np
from google import genai
from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.sessions import VertexAiSessionService, InMemorySessionService, Session
from google.genai import types as genai_types
from google.oauth2 import service_account
from loguru import logger
from livekit import rtc

from bot.agent import LIVE_TOOL_DECLARATIONS, build_adk_agent, dispatch_tool_call

# ---------------------------------------------------------------------------
# Audio constants
# ---------------------------------------------------------------------------

GEMINI_INPUT_SAMPLE_RATE = 16_000   # Gemini Live expects 16 kHz mono PCM16
GEMINI_INPUT_CHANNELS = 1
GEMINI_OUTPUT_SAMPLE_RATE = 24_000  # Gemini Live outputs 24 kHz mono PCM16
GEMINI_OUTPUT_CHANNELS = 1
SEND_CHUNK_BYTES = GEMINI_INPUT_SAMPLE_RATE * 2 // 10  # 100 ms chunks


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AuraRoomConfig:
    livekit_url: str
    room_name: str
    token: str
    system_instruction: str
    user_id: str = "anonymous"
    gemini_model: str = field(
        default_factory=lambda: os.getenv(
            "GEMINI_LIVE_MODEL", "gemini-live-2.5-flash-native-audio"
        )
    )
    voice_id: str = field(
        default_factory=lambda: os.getenv("GEMINI_VOICE", "Aoede")
    )
    idle_timeout_secs: float = field(
        default_factory=lambda: float(os.getenv("USER_IDLE_TIMEOUT_SECS", "120"))
    )
    max_duration_secs: float = field(
        default_factory=lambda: float(os.getenv("MAX_CALL_DURATION_SECS", "840"))
    )


def build_room_config(
    *,
    livekit_url: str,
    room_name: str,
    token: str,
    system_instruction: str,
    user_id: str = "anonymous",
) -> AuraRoomConfig:
    return AuraRoomConfig(
        livekit_url=livekit_url,
        room_name=room_name,
        token=token,
        system_instruction=system_instruction,
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Google client helpers
# ---------------------------------------------------------------------------

def _vertex_credentials():
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    raw = os.environ.get("GOOGLE_VERTEX_CREDENTIALS")
    path = os.environ.get("GOOGLE_VERTEX_CREDENTIALS_PATH")
    if raw:
        return service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=scopes
        )
    if path:
        return service_account.Credentials.from_service_account_file(path, scopes=scopes)
    return None  # Application Default Credentials (Cloud Run)


def _build_genai_client() -> genai.Client:
    model_type = os.getenv("GEMINI_MODEL", "ga").lower()
    if model_type == "preview":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY required for preview model")
        return genai.Client(api_key=api_key)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise ValueError("GOOGLE_CLOUD_PROJECT_ID is required")
    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        credentials=_vertex_credentials(),
    )


def _build_session_service():
    """VertexAiSessionService when VERTEX_AI_REASONING_ENGINE_ID is set; InMemorySessionService otherwise."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
    if project and engine_id:
        try:
            svc = VertexAiSessionService(project=project, location=location)
            logger.info(f"[adk] Using VertexAiSessionService (engine: {engine_id})")
            return svc
        except Exception as exc:
            logger.warning(f"[adk] VertexAiSessionService unavailable, using InMemory: {exc}")
    else:
        if project and not engine_id:
            logger.info("[adk] VERTEX_AI_REASONING_ENGINE_ID not set — using InMemorySessionService")
    return InMemorySessionService()


_session_service = None


def _get_session_service():
    global _session_service
    if _session_service is None:
        _session_service = _build_session_service()
    return _session_service


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _resample_pcm16(data: bytes, in_rate: int, out_rate: int) -> bytes:
    if in_rate == out_rate:
        return data
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    n_out = int(len(samples) * out_rate / in_rate)
    if n_out == 0:
        return b""
    indices = np.linspace(0, len(samples) - 1, n_out)
    return np.interp(indices, np.arange(len(samples)), samples).astype(np.int16).tobytes()


def _stereo_to_mono_pcm16(data: bytes) -> bytes:
    samples = np.frombuffer(data, dtype=np.int16)
    if len(samples) % 2 != 0:
        samples = samples[:-1]
    mono = (
        (samples[0::2].astype(np.int32) + samples[1::2].astype(np.int32)) // 2
    ).astype(np.int16)
    return mono.tobytes()


def _to_gemini_input(frame: rtc.AudioFrame) -> bytes:
    data = bytes(frame.data)
    if frame.num_channels == 2:
        data = _stereo_to_mono_pcm16(data)
    if frame.sample_rate != GEMINI_INPUT_SAMPLE_RATE:
        data = _resample_pcm16(data, frame.sample_rate, GEMINI_INPUT_SAMPLE_RATE)
    return data


# ---------------------------------------------------------------------------
# Data event sender
# ---------------------------------------------------------------------------

class _Events:
    def __init__(self, room: rtc.Room):
        self._room = room

    async def send(self, event: dict) -> None:
        try:
            await self._room.local_participant.publish_data(
                json.dumps(event).encode(), reliable=True, topic="aura-events"
            )
        except Exception as exc:
            logger.debug(f"[events] send failed: {exc}")


# ---------------------------------------------------------------------------
# ADK session helpers
# ---------------------------------------------------------------------------

def _history_to_context(history: list) -> str:
    """Convert last 20 ADK session events into a conversation context snippet."""
    if not history:
        return ""
    lines = []
    for event in history[-20:]:
        try:
            content = getattr(event, "content", None)
            if not content:
                continue
            role = getattr(content, "role", "unknown")
            parts = getattr(content, "parts", [])
            text = " ".join(
                getattr(p, "text", "") for p in parts if getattr(p, "text", None)
            ).strip()
            if text:
                lines.append(f"{role.upper()}: {text}")
        except Exception:
            continue
    if not lines:
        return ""
    return "Recent conversation history:\n" + "\n".join(lines)


async def _load_adk_session(
    session_service, agent: Agent, user_id: str
) -> tuple[Session, str]:
    # Use the full Reasoning Engine resource name when available, otherwise agent name
    engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if engine_id and project:
        app_name = f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
    else:
        app_name = agent.name
    try:
        sessions = await session_service.list_sessions(app_name=app_name, user_id=user_id)
        existing = getattr(sessions, "sessions", []) or []
        if existing:
            session = await session_service.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=existing[0].id,
            )
            logger.info(f"[adk] Loaded session {session.id} for {user_id}")
        else:
            session = await session_service.create_session(app_name=app_name, user_id=user_id)
            logger.info(f"[adk] Created session {session.id} for {user_id}")
        history = getattr(session, "events", []) or []
        return session, _history_to_context(history)
    except Exception as exc:
        logger.warning(f"[adk] Session load failed ({user_id}), starting fresh: {exc}")
        session = await session_service.create_session(app_name=app_name, user_id=user_id)
        return session, ""


async def _append_turn(
    session_service, agent: Agent, session: Session, role: str, text: str
) -> None:
    try:
        content = genai_types.Content(role=role, parts=[genai_types.Part(text=text)])
        event = Event(author=role, content=content)
        await session_service.append_event(session=session, event=event)
    except Exception as exc:
        logger.warning(f"[adk] Session append failed: {exc}")


# ---------------------------------------------------------------------------
# Main voice session
# ---------------------------------------------------------------------------

class AuraVoiceSession:
    def __init__(self, config: AuraRoomConfig):
        self._config = config
        self._room = rtc.Room()
        self._client = _build_genai_client()
        self._events: _Events | None = None

    async def run(self) -> None:
        cfg = self._config
        session_service = _get_session_service()
        adk_agent = build_adk_agent(cfg.system_instruction)

        # Load ADK session — injects prior history into system instruction
        adk_session, history_context = await _load_adk_session(
            session_service, adk_agent, cfg.user_id
        )
        effective_instruction = cfg.system_instruction
        if history_context:
            effective_instruction = f"{cfg.system_instruction}\n\n{history_context}"

        # Connect to LiveKit
        logger.info(f"[aura] Connecting to room {cfg.room_name} (user={cfg.user_id})")
        await self._room.connect(cfg.livekit_url, cfg.token)
        self._events = _Events(self._room)

        # Build Gemini Live tool declarations from ADK agent
        live_tools = []
        if LIVE_TOOL_DECLARATIONS:
            declarations = [
                genai_types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=(
                        genai_types.Schema(
                            type=t["parameters"]["type"],
                            properties={
                                k: genai_types.Schema(**v)
                                for k, v in t["parameters"].get("properties", {}).items()
                            },
                            required=t["parameters"].get("required", []),
                        )
                        if t.get("parameters", {}).get("properties")
                        else None
                    ),
                )
                for t in LIVE_TOOL_DECLARATIONS
            ]
            live_tools = [genai_types.Tool(function_declarations=declarations)]

        live_config = genai_types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=effective_instruction,
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=cfg.voice_id
                    )
                )
            ),
            tools=live_tools if live_tools else None,
        )

        # Publish output audio track to LiveKit
        audio_source = rtc.AudioSource(GEMINI_OUTPUT_SAMPLE_RATE, GEMINI_OUTPUT_CHANNELS)
        out_track = rtc.LocalAudioTrack.create_audio_track("aura-audio", audio_source)
        await self._room.local_participant.publish_track(
            out_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        start_time = time.monotonic()
        new_turns: list[dict] = []

        async with self._client.aio.live.connect(
            model=cfg.gemini_model, config=live_config
        ) as live_session:
            logger.info(f"[aura] Gemini Live open (model={cfg.gemini_model})")
            await self._events.send({"type": "bot-ready"})

            await live_session.send(
                input="Start the conversation. Greet the user warmly and ask how you can help.",
                end_of_turn=True,
            )

            send_task = asyncio.create_task(
                self._send_audio_loop(live_session), name="aura-send"
            )
            recv_task = asyncio.create_task(
                self._recv_loop(live_session, audio_source, new_turns), name="aura-recv"
            )
            timeout_task = asyncio.create_task(
                asyncio.sleep(cfg.max_duration_secs), name="aura-timeout"
            )

            done, pending = await asyncio.wait(
                [send_task, recv_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if timeout_task in done:
                logger.warning(f"[aura] Max duration ({cfg.max_duration_secs}s) reached")

        elapsed = time.monotonic() - start_time
        logger.info(f"[aura] Session ended ({cfg.user_id}, {elapsed:.1f}s)")

        # Persist new turns to Vertex AI via ADK session service
        for turn in new_turns:
            await _append_turn(
                session_service, adk_agent, adk_session,
                turn["role"], turn["content"]
            )
        if new_turns:
            logger.info(f"[adk] Persisted {len(new_turns)} turns for {cfg.user_id}")

        await self._room.disconnect()

    async def _send_audio_loop(self, live_session) -> None:
        track = await self._wait_for_audio_track()
        if track is None:
            logger.warning("[aura] No audio track found — send loop exiting")
            return

        audio_stream = rtc.AudioStream(track)
        buffer = b""
        last_speech_time = time.monotonic()
        logger.info("[aura] Audio send loop started")

        async for event in audio_stream:
            if not isinstance(event, rtc.AudioFrameEvent):
                continue
            buffer += _to_gemini_input(event.frame)

            while len(buffer) >= SEND_CHUNK_BYTES:
                chunk, buffer = buffer[:SEND_CHUNK_BYTES], buffer[SEND_CHUNK_BYTES:]
                await live_session.send_realtime_input(
                    audio=genai_types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={GEMINI_INPUT_SAMPLE_RATE}",
                    )
                )
                rms = float(np.sqrt(np.mean(
                    np.frombuffer(chunk, dtype=np.int16).astype(np.float32) ** 2
                )))
                if rms > 200:
                    last_speech_time = time.monotonic()
                elif time.monotonic() - last_speech_time > self._config.idle_timeout_secs:
                    logger.warning("[aura] Idle timeout reached")
                    return

    async def _wait_for_audio_track(self):
        """Wait for a remote audio track to be subscribed. Returns the Track object."""
        timeout = float(os.getenv("CONNECTION_TIMEOUT_SECS", "60"))
        track_ready: asyncio.Event = asyncio.Event()
        found_track = None

        def _on_track_subscribed(track, pub, participant):
            nonlocal found_track
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                logger.info(f"[aura] Audio track subscribed from {participant.identity}")
                found_track = track
                track_ready.set()

        self._room.on("track_subscribed", _on_track_subscribed)

        # Check for already-subscribed audio tracks (timing race on fast connects)
        for p in self._room.remote_participants.values():
            for pub in p.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.track:
                    self._room.off("track_subscribed", _on_track_subscribed)
                    logger.info(f"[aura] Audio track already available from {p.identity}")
                    return pub.track

        try:
            await asyncio.wait_for(track_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[aura] Timed out waiting for participant audio track")
            found_track = None
        finally:
            self._room.off("track_subscribed", _on_track_subscribed)

        return found_track

    async def _recv_loop(
        self,
        live_session,
        audio_source: rtc.AudioSource,
        new_turns: list[dict],
    ) -> None:
        bot_buf = ""
        user_buf = ""

        async for message in live_session.receive():
            # Audio output → LiveKit
            if message.data:
                n = len(message.data) // 2
                if n > 0:
                    await audio_source.capture_frame(
                        rtc.AudioFrame(
                            data=message.data,
                            sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
                            num_channels=GEMINI_OUTPUT_CHANNELS,
                            samples_per_channel=n,
                        )
                    )

            if message.server_content:
                sc = message.server_content

                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        txt = getattr(part, "text", None)
                        if txt:
                            bot_buf += txt
                            await self._events.send(
                                {"type": "bot-llm-text", "data": {"text": bot_buf}}
                            )

                if getattr(sc, "input_transcription", None):
                    user_buf += sc.input_transcription.text or ""
                    if user_buf.strip():
                        await self._events.send({
                            "type": "user-transcription",
                            "data": {"text": user_buf.strip(), "final": False},
                        })

                if sc.turn_complete:
                    if bot_buf.strip():
                        new_turns.append({"role": "model", "content": bot_buf.strip()})
                        await self._events.send({
                            "type": "bot-transcription",
                            "data": {"text": bot_buf.strip(), "final": True},
                        })
                    if user_buf.strip():
                        new_turns.append({"role": "user", "content": user_buf.strip()})
                        await self._events.send({
                            "type": "user-transcription",
                            "data": {"text": user_buf.strip(), "final": True},
                        })
                    bot_buf = ""
                    user_buf = ""

                if getattr(sc, "interrupted", False):
                    bot_buf = ""
                    await self._events.send({"type": "interruption"})

            # ADK tool dispatch
            if message.tool_call:
                for fc in message.tool_call.function_calls:
                    result = await dispatch_tool_call(fc.name, dict(fc.args or {}))
                    await live_session.send_tool_response(
                        function_responses=[
                            genai_types.FunctionResponse(
                                id=fc.id,
                                name=fc.name,
                                response=result,
                            )
                        ]
                    )

            if message.usage_metadata:
                um = message.usage_metadata
                await self._events.send({
                    "type": "metrics",
                    "data": {"tokens": [{
                        "prompt_tokens": getattr(um, "prompt_token_count", 0),
                        "completion_tokens": getattr(um, "candidates_token_count", 0),
                        "total_tokens": getattr(um, "total_token_count", 0),
                    }]},
                })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_room_bot(config: AuraRoomConfig) -> None:
    session = AuraVoiceSession(config)
    try:
        await session.run()
    except Exception as exc:
        logger.exception(f"[aura] Room bot crashed ({config.room_name}): {exc}")
