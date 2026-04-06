"""Gemini Live ↔ LiveKit audio bridge for Aura — Google SDE Interview Coach.

Architecture (ADK Runner.run_live):

  Browser mic → LiveKit room (WebRTC)
    → rtc.AudioStream → PCM16 @ 16 kHz
    → LiveRequestQueue.send_realtime(blob) → ADK Runner → Gemini Live
    → ADK Event stream → inline_data audio → rtc.AudioSource → LiveKit → Browser

Session persistence (100% Google Cloud):
  - ADK Runner manages sessions via the supplied session_service.
  - History is sent to the Gemini Live model automatically.
  - After the live session ends, the session already has all turns persisted.

Agent definition (google.adk):
  - bot.agent.build_adk_agent() returns an LlmAgent with instruction + tools.
  - Tools are auto-declared to Gemini Live by the ADK Runner.
  - Tool dispatch is handled automatically — no manual dispatch_tool_call.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp

from google import genai
from google.adk.agents import Agent
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import VertexAiSessionService, InMemorySessionService, Session
from google.genai import types as genai_types
from google.oauth2 import service_account
from loguru import logger
from livekit import rtc

from bot.audio.silero_vad import SileroVADAnalyzer, VADState, VADParams
from bot.agent import (
    build_adk_agent,
    create_session_state,
    destroy_session_state,
)


# ---------------------------------------------------------------------------
# Audio constants
# ---------------------------------------------------------------------------

GEMINI_INPUT_SAMPLE_RATE = 16_000   # Gemini Live expects 16 kHz mono PCM16
GEMINI_INPUT_CHANNELS = 1
GEMINI_OUTPUT_SAMPLE_RATE = 24_000  # Gemini Live outputs 24 kHz mono PCM16
GEMINI_OUTPUT_CHANNELS = 1
SEND_CHUNK_BYTES = GEMINI_INPUT_SAMPLE_RATE * 2 // 10  # 100 ms chunks

_PAUSE_PHRASE_RE = re.compile(
    r"\b(?:wait|hold on|hang on|stop|pause|one moment|give me a sec|give me a second|let me think|let me restart)\b",
    re.IGNORECASE,
)
_EXIT_PHRASE_RE = re.compile(
    r"\b(?:bye|goodbye|good bye|see you|talk later|done for today|enough for today|"
    r"have to go|need to leave|come back later|continue next time|"
    r"wrap up|end the (?:session|interview)|i'm leaving|gotta go)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\b[\w']+\b")


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


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
    session_id: str = field(default_factory=lambda: os.urandom(8).hex())
    gemini_model: str = field(
        default_factory=lambda: os.getenv(
            "GEMINI_LIVE_MODEL", "gemini-live-2.5-flash-native-audio"
        )
    )
    voice_id: str = field(
        default_factory=lambda: os.getenv("GEMINI_VOICE", "Aoede")
    )
    idle_timeout_secs: float = field(
        default_factory=lambda: float(os.getenv("USER_IDLE_TIMEOUT_SECS", "300"))
    )
    max_duration_secs: float = field(
        default_factory=lambda: float(os.getenv("MAX_CALL_DURATION_SECS", "1200"))
    )
    allow_interruptions: bool = field(
        default_factory=lambda: _env_flag("ALLOW_INTERRUPTIONS", "true")
    )
    interruption_min_words: int = field(
        default_factory=lambda: max(1, int(os.getenv("INTERRUPTION_MIN_WORDS", "3")))
    )


def build_room_config(
    *,
    livekit_url: str,
    room_name: str,
    token: str,
    system_instruction: str,
    user_id: str = "anonymous",
    allow_interruptions: bool | None = None,
    interruption_min_words: int | None = None,
) -> AuraRoomConfig:
    return AuraRoomConfig(
        livekit_url=livekit_url,
        room_name=room_name,
        token=token,
        system_instruction=system_instruction,
        user_id=user_id,
        allow_interruptions=(
            allow_interruptions if allow_interruptions is not None else _env_flag("ALLOW_INTERRUPTIONS", "true")
        ),
        interruption_min_words=(
            interruption_min_words
            if interruption_min_words is not None
            else max(1, int(os.getenv("INTERRUPTION_MIN_WORDS", "3")))
        ),
    )


# ---------------------------------------------------------------------------
# Google client helpers
# ---------------------------------------------------------------------------

def _bridge_env_for_adk() -> None:
    """Map custom env vars to standard ones expected by google-genai Client.

    The ADK's Gemini class creates a Client() that reads from standard env vars:
      GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION,
      GOOGLE_APPLICATION_CREDENTIALS (or ADC).
    Our deploy scripts use GOOGLE_CLOUD_PROJECT_ID and GOOGLE_VERTEX_CREDENTIALS_PATH.
    """
    model_type = os.getenv("GEMINI_MODEL", "ga").lower()
    if model_type == "preview":
        # Preview uses GOOGLE_API_KEY — no Vertex AI needed
        return
    project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
    if project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
    creds_path = os.environ.get("GOOGLE_VERTEX_CREDENTIALS_PATH", "")
    if creds_path:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)
    # For GOOGLE_VERTEX_CREDENTIALS (JSON string), write to temp file
    creds_json = os.environ.get("GOOGLE_VERTEX_CREDENTIALS", "")
    if creds_json and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json", prefix="gcp_creds_")
        with os.fdopen(fd, "w") as f:
            f.write(creds_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        logger.info(f"[adk] Wrote GOOGLE_VERTEX_CREDENTIALS to temp file for ADK")

    logger.info(
        f"[adk] Env bridge: GOOGLE_GENAI_USE_VERTEXAI={os.environ.get('GOOGLE_GENAI_USE_VERTEXAI')}, "
        f"GOOGLE_CLOUD_PROJECT={os.environ.get('GOOGLE_CLOUD_PROJECT', '<unset>')}, "
        f"GOOGLE_APPLICATION_CREDENTIALS={'set' if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') else '<unset>'}"
    )


_bridge_env_done = False


def _ensure_env_for_adk() -> None:
    """Call _bridge_env_for_adk() once, lazily (after load_dotenv has run)."""
    global _bridge_env_done
    if not _bridge_env_done:
        _bridge_env_for_adk()
        _bridge_env_done = True


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
    
def _gemini_text_model() -> str:
    """Return the text model used for grading and narrative summaries."""
    return os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


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
_session_service_failures = 0


def _get_session_service():
    """Return the ADK session service singleton. Re-creates on repeated failures."""
    global _session_service, _session_service_failures
    if _session_service is None:
        _session_service = _build_session_service()
        _session_service_failures = 0
    return _session_service


def _reset_session_service() -> None:
    """Force re-initialisation on next call (called after persistent failures)."""
    global _session_service
    _session_service = None
    logger.warning("[adk] Session service reset — will reinitialise on next call")


# ---------------------------------------------------------------------------
# Data event sender
# ---------------------------------------------------------------------------

# In-memory summary store, keyed by room_name.
# Used as HTTP fallback when the data channel is closed before call-summary
# can be delivered (e.g. user clicks Disconnect).
_room_summaries: dict[str, dict] = {}

class _Events:
    def __init__(self, room: rtc.Room):
        self._room = room

    async def send(self, event: dict) -> None:
        try:
            await self._room.local_participant.publish_data(
                json.dumps(event).encode(), reliable=True, topic="aura-events"
            )
        except Exception as exc:
            logger.warning(f"[events] send failed ({event.get('type', '?')}): {exc}")


# ---------------------------------------------------------------------------
# ADK session helpers
# ---------------------------------------------------------------------------

# Approximate token cap for injected history (~500 tokens ≈ 2 000 chars at ~4 chars/token).
# Keeps system instruction within Gemini's context but avoids burning prompt budget.
_HISTORY_CHAR_LIMIT = 2_000


def _history_to_context(history: list) -> str:
    """Convert ADK session events into a capped conversation context snippet.

    Iterates newest→oldest to fill the char budget, then reverses so the final
    string is chronological.  Caps at ~500 tokens to control prompt cost.
    """
    if not history:
        return ""
    lines: list[str] = []
    used = 0
    for event in reversed(history):
        try:
            content = getattr(event, "content", None)
            if not content:
                continue
            role = getattr(content, "role", "unknown")
            parts = getattr(content, "parts", [])
            text = " ".join(
                getattr(p, "text", "") for p in parts if getattr(p, "text", None)
            ).strip()
            if not text:
                continue
            line = f"{role.upper()}: {text}"
            if used + len(line) > _HISTORY_CHAR_LIMIT:
                break
            lines.append(line)
            used += len(line) + 1  # +1 for newline
        except Exception:
            continue
    if not lines:
        return ""
    lines.reverse()  # restore chronological order
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
        self._audio_source: rtc.AudioSource | None = None
        self._ended_reason = "customer_ended_call"  # overwritten on specific exit paths
        self._client_disconnected = asyncio.Event()  # set when LiveKit peer leaves
        self._early_summary_task: asyncio.Task | None = None  # kicked off when end_conversation confirmed
        self._early_grade_task: asyncio.Task | None = None
        # ── Pipecat-style interruption state (4 flags) ───────────────────
        # Mirrors Pipecat's implicit pipeline state: Silero VAD fires
        # UserStartedSpeakingFrame → CancelFrame propagates upstream.
        # See solution2 (Pipecat) for the reference implementation.
        self._bot_speaking = False       # Bot audio actively playing to LiveKit
        self._user_speaking = False      # User speech detected by Silero VAD
        self._interrupted = False        # Gemini confirmed interrupted — discard until turn_complete
        self._pause_requested = False    # User said "stop"/"wait"/"hang on"
        self._last_activity_time = time.monotonic()  # shared idle clock (both Silero + Gemini tx)
        self._last_user_utterance = ""   # last user transcription (for exit-phrase guard)
        self._user_stopped_speaking_at: float | None = None  # for STS latency measurement

    # ── Pipecat-equivalent frame handlers ─────────────────────────────
    # These mirror Pipecat's UserStartedSpeakingFrame, BotStartedSpeakingFrame,
    # CancelFrame, etc. — implemented directly instead of via frame pipeline.

    async def _on_user_started_speaking(self) -> None:
        """Silero VAD speech start — equivalent to Pipecat UserStartedSpeakingFrame.

        When allow_interruptions=True and bot is speaking, this triggers
        immediate barge-in: clears the LiveKit playout queue for instant
        silence while Gemini server-side VAD decides the "real" turn.
        Does NOT set _interrupted — only Gemini's interrupted event does that.
        """
        if self._user_speaking:
            return
        self._user_speaking = True
        logger.debug("[aura] User started speaking (Silero VAD)")
        await self._events.send({"type": "user-started-speaking"})
        # Immediate barge-in — clear playout queue for instant silence.
        # Gemini will confirm via `interrupted` event if it's a real interruption.
        if self._config.allow_interruptions and self._bot_speaking:
            if self._audio_source is not None:
                self._audio_source.clear_queue()
            self._bot_speaking = False
            logger.info("[aura] Bot audio cut (user barge-in via Silero)")
            await self._events.send({"type": "bot-stopped-speaking"})
            await self._events.send({"type": "interruption"})

    async def _on_user_stopped_speaking(self) -> None:
        """Silero VAD silence — equivalent to Pipecat UserStoppedSpeakingFrame."""
        if not self._user_speaking:
            return
        self._user_speaking = False
        self._user_stopped_speaking_at = time.monotonic()
        logger.debug("[aura] User stopped speaking (Silero VAD)")
        await self._events.send({"type": "user-stopped-speaking"})
        # Auto-clear pause if user resumes interacting (Silero detected speech
        # then silence — means user is actively present, not paused).
        if self._pause_requested:
            self._pause_requested = False
            logger.info("[aura] Auto-cleared pause (user speech detected after pause)")

    async def _on_bot_started_speaking(self) -> None:
        """First audio byte from Gemini — equivalent to Pipecat BotStartedSpeakingFrame."""
        if self._bot_speaking or self._interrupted:
            return
        self._bot_speaking = True
        logger.debug("[aura] Bot started speaking")
        await self._events.send({"type": "bot-started-speaking"})
        # Measure STS (Speech-to-Speech) latency: user stopped → bot started
        if self._user_stopped_speaking_at is not None:
            sts_ms = round((time.monotonic() - self._user_stopped_speaking_at) * 1000)
            self._user_stopped_speaking_at = None
            logger.info(f"[aura] STS latency: {sts_ms}ms")
            await self._events.send({
                "type": "latency",
                "data": {"total_ms": sts_ms},
            })

    async def _on_bot_stopped_speaking(self) -> None:
        """Bot turn ended or interrupted — equivalent to Pipecat BotStoppedSpeakingFrame."""
        if not self._bot_speaking:
            return
        self._bot_speaking = False
        logger.debug("[aura] Bot stopped speaking")
        await self._events.send({"type": "bot-stopped-speaking"})

    def _is_output_blocked(self) -> bool:
        """True when bot audio should be discarded (not sent to LiveKit).

        Equivalent to Pipecat's transport.output() gating after CancelFrame.
        """
        if self._interrupted or self._pause_requested:
            return True
        # Block bot audio while user is actively speaking (Pipecat: CancelFrame
        # prevents new LLM frames while user holds the floor)
        if self._config.allow_interruptions and self._user_speaking:
            return True
        return False

    async def _interrupt_bot(self, reason: str) -> None:
        """Gemini confirmed interruption — gate all audio until turn_complete.

        Called ONLY from Gemini's `interrupted` event (not from Silero VAD).
        Silero does the instant queue-clear; this sets the persistent gate.
        """
        if self._audio_source is not None:
            self._audio_source.clear_queue()
        if not self._bot_speaking:
            # Bot already stopped (or never started) — don't set the
            # _interrupted flag, otherwise it leaks into the NEXT turn and
            # discards that turn's text.
            logger.debug(f"[aura] Ignoring interrupt ({reason}) — bot not speaking")
            return
        self._interrupted = True
        self._bot_speaking = False
        logger.info(f"[aura] Bot interrupted ({reason})")
        await self._events.send({"type": "bot-stopped-speaking"})
        await self._events.send({"type": "interruption"})

    async def run(self) -> None:
        _ensure_env_for_adk()
        cfg = self._config
        session_service = _get_session_service()

        # ── End-session signal: set by after_tool_callback when end_conversation fires
        self._end_session_event = asyncio.Event()

        # ── Tool callbacks ────────────────────────────────────────────────
        # before_tool_callback: sets per-session context so tools read the right state
        import bot.agent as _agent_mod

        def _before_tool(tool, args, tool_context, **kwargs):
            _agent_mod._session_id_context = cfg.session_id
            return None  # let tool execute normally

        def _after_tool(tool, args, tool_context, tool_response=None, **kwargs):
            if isinstance(tool_response, dict) and tool_response.get("__end_session__"):
                if self._pause_requested:
                    logger.info("[aura] Blocking end_conversation — user is in pause mode")
                    return {"status": "blocked", "reason": "User is in pause mode. The session is NOT ending. Continue the interview when the user is ready."}
                # Code-level safety net: verify last user utterance contains
                # a real exit phrase. Prevents LLM hallucinating exits from
                # "ok", garbled speech, or single-word acknowledgements.
                last = self._last_user_utterance.strip().lower()
                if last and not _EXIT_PHRASE_RE.search(last):
                    logger.warning(
                        f"[aura] Blocking end_conversation — last utterance has no exit phrase: {last!r}"
                    )
                    return {"status": "blocked", "reason": "The candidate did NOT ask to leave. The session is NOT ending. Continue the interview normally."}
                logger.info(f"[aura] end_conversation allowed — exit phrase found in: {last!r}")
                self._end_session_event.set()
                # Kick off summary + grading NOW while bot is still saying goodbye.
                # By the time the event loop exits and session teardown runs,
                # these may already be finished.
                try:
                    from bot.agent import _get_state
                    _state = _get_state(cfg.session_id)
                    _turns = list(new_turns)  # snapshot
                    self._early_grade_task = asyncio.create_task(
                        self._auto_grade_session(_turns, _state)
                    )
                    self._early_summary_task = asyncio.create_task(
                        self._generate_call_summary(_turns, _state)
                    )
                    logger.info("[aura] Early summary+grade tasks started (parallel with goodbye)")
                except Exception as exc:
                    logger.warning(f"[aura] Failed to start early summary: {exc}")
            return None  # don't alter result

        adk_agent = build_adk_agent(
            system_instruction=cfg.system_instruction,
            model=cfg.gemini_model,
            before_tool_callback=_before_tool,
            after_tool_callback=_after_tool,
        )

        # ── Runner + LiveRequestQueue ─────────────────────────────────────
        # Use the full Reasoning Engine resource name when available
        engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
        project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        if engine_id and project:
            app_name = f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
        else:
            app_name = adk_agent.name

        runner = Runner(
            agent=adk_agent,
            app_name=app_name,
            session_service=session_service,
            auto_create_session=True,
        )

        live_queue = LiveRequestQueue()

        run_config = RunConfig(
            streaming_mode=StreamingMode.BIDI,
            session_resumption=genai_types.SessionResumptionConfig(transparent=True),
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=cfg.voice_id
                    )
                ),
                language_code="en-US",
            ),
            realtime_input_config=genai_types.RealtimeInputConfig(
                automatic_activity_detection=genai_types.AutomaticActivityDetection(
                    silence_duration_ms=1200,
                    prefix_padding_ms=300,
                )
            ),
            input_audio_transcription=genai_types.AudioTranscriptionConfig(
                language_codes=["en-US"],
            ),
            output_audio_transcription=genai_types.AudioTranscriptionConfig(),
            enable_affective_dialog=True,
            proactivity=(
                genai_types.ProactivityConfig(proactive_audio=True)
                if cfg.gemini_model == "preview"
                else None
            ),
        )

        # Connect to LiveKit
        logger.info(f"[aura] Connecting to room {cfg.room_name} (user={cfg.user_id})")
        await self._room.connect(cfg.livekit_url, cfg.token)
        self._events = _Events(self._room)
        create_session_state(cfg.session_id)

        # Detect client disconnect early so we don't wait for Gemini timeout
        @self._room.on("participant_disconnected")
        def _on_participant_left(participant: rtc.RemoteParticipant):
            logger.info(f"[aura] Participant {participant.identity} left — signalling client disconnect")
            self._client_disconnected.set()

        # Publish output audio track to LiveKit
        audio_source = rtc.AudioSource(
            GEMINI_OUTPUT_SAMPLE_RATE,
            GEMINI_OUTPUT_CHANNELS,
            queue_size_ms=60,
        )
        self._audio_source = audio_source
        out_track = rtc.LocalAudioTrack.create_audio_track("aura-audio", audio_source)
        await self._room.local_participant.publish_track(
            out_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        start_time = time.monotonic()
        new_turns: list[dict] = []

        # Send timeout config to frontend so it can display countdown timers
        await self._events.send({
            "type": "session-config",
            "data": {
                "idle_timeout_secs": cfg.idle_timeout_secs,
                "max_duration_secs": cfg.max_duration_secs,
            },
        })

        # ── Start ADK event stream + audio send loop ─────────────────────
        recv_task = asyncio.create_task(
            self._process_events(runner, live_queue, run_config, cfg, audio_source, new_turns),
            name="aura-recv",
        )
        send_task = asyncio.create_task(
            self._send_audio_loop(live_queue), name="aura-send"
        )
        timeout_task = asyncio.create_task(
            asyncio.sleep(cfg.max_duration_secs), name="aura-timeout"
        )
        disconnect_task = asyncio.create_task(
            self._client_disconnected.wait(), name="aura-client-disconnect"
        )

        # Yield so recv_task starts before we push the greeting
        await asyncio.sleep(0)

        # Trigger the opening greeting via LiveRequestQueue
        _FIXED_GREETING = (
            "Hello! I'm Aura, your Google SDE interview coach. "
            "Great to have you here. "
            "Which round would you like to practice today — "
            "Behavioural, Coding, System Design, or a Targeted Debrief?"
        )
        self._bot_speaking = True
        live_queue.send_content(
            genai_types.Content(
                role="user",
                parts=[genai_types.Part(
                    text=(
                        f"Begin the session. Your very first message to the candidate "
                        f"must be exactly this, word for word:\n\"{_FIXED_GREETING}\""
                    )
                )],
            )
        )

        done, pending = await asyncio.wait(
            [send_task, recv_task, timeout_task, disconnect_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Log which task ended the session and surface any exception
        task_names = {send_task: "send", recv_task: "recv", timeout_task: "timeout", disconnect_task: "client-disconnect"}
        for t in done:
            name = task_names.get(t, t.get_name())
            exc = t.exception() if not t.cancelled() else None
            if exc:
                logger.error(f"[aura] '{name}' task raised: {type(exc).__name__}: {exc}", exc_info=exc)
            elif t.cancelled():
                logger.debug(f"[aura] '{name}' task cancelled")
            else:
                logger.info(f"[aura] '{name}' task finished (triggered session end)")

        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        # Close the live queue to signal ADK to tear down the connection
        live_queue.close()

        if timeout_task in done:
            self._ended_reason = "exceeded_max_duration"
            logger.warning(f"[aura] Max duration ({cfg.max_duration_secs}s) reached")
        elif disconnect_task in done:
            self._ended_reason = "customer_ended_call"
            logger.info("[aura] Client disconnected from LiveKit room")
        elif send_task in done and self._ended_reason == "customer_ended_call":
            pass  # reason already set by _send_audio_loop
        # recv_task exits on end_conversation (assistant_ended_call) or stream end

        elapsed = time.monotonic() - start_time
        logger.info(f"[aura] Session ended ({cfg.user_id}, {elapsed:.1f}s)")

        # ADK Runner persists turns to the session automatically.
        # We still collect new_turns for the call-summary and webhook.

        # ── Auto-grade & summary ─────────────────────────────────────────
        from bot.agent import _get_state
        state = _get_state(cfg.session_id)

        # Tell the frontend we're generating feedback so it waits.
        try:
            await self._events.send({"type": "generating-summary"})
        except Exception:
            pass

        # Always run auto-grading to fill any missing rubric grades.
        # This ensures feedback exists even on timeout/idle/disconnect.
        # Run auto-grade and summary in PARALLEL to minimise wait time.
        # If early tasks were kicked off during end_conversation, reuse them.
        if self._early_summary_task and not self._early_summary_task.done():
            summary_task = self._early_summary_task
            logger.info("[aura] Reusing early summary task (still running)")
        elif self._early_summary_task and self._early_summary_task.done():
            summary_task = self._early_summary_task
            logger.info("[aura] Reusing early summary task (already finished)")
        else:
            summary_task = asyncio.create_task(
                self._generate_call_summary(new_turns, state)
            )
        if self._early_grade_task and not self._early_grade_task.done():
            grade_task = self._early_grade_task
            logger.info("[aura] Reusing early grade task (still running)")
        elif self._early_grade_task and self._early_grade_task.done():
            grade_task = self._early_grade_task
            logger.info("[aura] Reusing early grade task (already finished)")
        else:
            grade_task = asyncio.create_task(
                self._auto_grade_session(new_turns, state)
            )
        # Wait for both — grades mutate state in-place, summary returns a string.
        await asyncio.gather(grade_task, summary_task, return_exceptions=True)
        narrative_summary = summary_task.result() if not summary_task.cancelled() else None

        summary_payload = {
            "duration_secs": round(elapsed, 1),
            "ended_reason": self._ended_reason,
            "questions_asked": state.asked,
            "rubric_grades": state.grades,
            "answer_notes": state.notes,
            "narrative_summary": narrative_summary,
        }

        # Store for HTTP fallback (frontend polls if data-channel delivery fails)
        _room_summaries[cfg.room_name] = summary_payload

        try:
            await self._events.send({
                "type": "call-summary",
                "data": summary_payload,
            })
        except Exception as exc:
            logger.warning(f"[aura] Failed to send call-summary event: {exc}")

        webhook_url = os.getenv("WEBHOOK_URL", "").strip()
        if webhook_url:
            await self._post_end_of_call_webhook(
                webhook_url, cfg, new_turns, elapsed, narrative_summary
            )

        # Give the data channel time to deliver the call-summary event
        # before tearing down the room connection.
        await asyncio.sleep(3)

        destroy_session_state(cfg.session_id)
        await self._room.disconnect()

    async def _post_end_of_call_webhook(
        self,
        url: str,
        cfg: AuraRoomConfig,
        transcript: list[dict],
        duration_secs: float,
        narrative_summary: str | None = None,
    ) -> None:
        """POST end-of-call report to WEBHOOK_URL (best-effort, never raises)."""
        from bot.agent import _get_state
        try:
            state = _get_state(cfg.session_id)
            payload = {
                "event": "call_ended",
                "user_id": cfg.user_id,
                "session_id": cfg.session_id,
                "duration_secs": round(duration_secs, 1),
                "ended_reason": self._ended_reason,
                "transcript": transcript,
                "questions_asked": state.asked,
                "rubric_grades": state.grades,
                "answer_notes": state.notes,
            }
            if narrative_summary:
                payload["summary"] = narrative_summary

            # Auth: header (X-API-Key) or body envelope, mirroring askjohngeorge
            api_key = os.getenv("WEBHOOK_API_KEY", "").strip()
            auth_type = os.getenv("WEBHOOK_AUTH_TYPE", "header").strip().lower()
            headers = {"Content-Type": "application/json"}
            if api_key:
                if auth_type == "body":
                    payload = {"auth": {"type": "api_key", "key": api_key}, "data": payload}
                else:
                    headers["X-API-Key"] = api_key

            async with aiohttp.ClientSession() as http:
                async with http.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    logger.info(f"[webhook] POST {url} → {resp.status}")
        except Exception as exc:
            logger.warning(f"[webhook] Failed to post end-of-call report: {exc}")

    async def _auto_grade_session(
        self,
        transcript: list[dict],
        state,
    ) -> None:
        """Server-side auto-grading: analyze transcript and fill missing rubric grades.

        Runs at session end regardless of how the session ended (timeout, idle,
        disconnect, user exit). Only grades categories not already graded by the
        LLM during conversation. Also fills answer_notes if missing.
        """
        if not transcript:
            logger.info("[auto-grade] No transcript — skipping")
            return

        # Only grade if at least one question was asked
        if not state.asked:
            logger.info("[auto-grade] No questions asked — skipping")
            return

        lines = [f"{t['role'].upper()}: {t['content']}" for t in transcript]
        tx_text = "\n".join(lines)
        if len(tx_text) > 14_000:
            tx_text = tx_text[-14_000:]

        # Tell the LLM which categories are already graded so it skips them
        already_graded = list(state.grades.keys())
        already_clause = ""
        if already_graded:
            already_clause = (
                f"\nAlready graded (DO NOT re-grade these): {', '.join(already_graded)}\n"
            )

        prompt = (
            "You are an expert Google SDE interviewer grading a mock interview.\n"
            "Below is the transcript. Evaluate the candidate on ALL observable rubric "
            "categories and produce a JSON object.\n\n"
            f"Transcript:\n{tx_text}\n"
            f"{already_clause}\n"
            "Rubric categories (only grade those with clear evidence in the transcript):\n"
            "- problem_solving, code_fluency, autonomy, cs_fundamentals, system_design, "
            "resoluteness, communication, curiosity, awareness, collaboration, "
            "do_hard_things, level_up, time_is_precious\n\n"
            "Grade scale: strong_no, no, mixed, yes, strong_yes\n\n"
            "Also produce answer_notes for each question the candidate attempted.\n\n"
            "Return ONLY valid JSON (no markdown fences) with this exact structure:\n"
            '{\n'
            '  "grades": {\n'
            '    "category_name": {"grade": "yes", "notes": "Observable facts..."}\n'
            '  },\n'
            '  "answer_notes": [\n'
            '    {"question": "...", "strength": "...", "weakness": "..."}\n'
            '  ]\n'
            '}\n'
        )

        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=_gemini_text_model(),
                    contents=prompt,
                ),
                timeout=20.0,
            )
            raw = (resp.text or "").strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

            import json
            data = json.loads(raw)

            # Merge grades (only add missing ones)
            new_grades = data.get("grades", {})
            added = 0
            valid_grades = {"strong_no", "no", "mixed", "yes", "strong_yes"}
            for cat, info in new_grades.items():
                cat_key = cat.lower().strip().replace(" ", "_")
                if cat_key in state.grades:
                    continue  # already graded by LLM during conversation
                grade_val = info.get("grade", "").lower().strip()
                notes_val = info.get("notes", "")
                if grade_val in valid_grades and notes_val:
                    state.grades[cat_key] = {"grade": grade_val, "notes": notes_val}
                    added += 1

            # Merge answer notes (only add if we have fewer than the questions asked)
            new_notes = data.get("answer_notes", [])
            if new_notes and len(state.notes) < len(state.asked):
                for note in new_notes:
                    q = note.get("question", "")
                    s = note.get("strength", "")
                    w = note.get("weakness", "")
                    if q and (s or w):
                        # Avoid duplicates
                        existing_qs = {n.get("question", "")[:50] for n in state.notes}
                        if q[:50] not in existing_qs:
                            state.notes.append({"question": q, "strength": s, "weakness": w})

            logger.info(
                f"[auto-grade] Added {added} grades, "
                f"total grades={len(state.grades)}, notes={len(state.notes)}"
            )
        except asyncio.TimeoutError:
            logger.warning("[auto-grade] LLM grading timed out after 20s")
        except json.JSONDecodeError as exc:
            logger.warning(f"[auto-grade] Failed to parse JSON: {exc}")
        except Exception as exc:
            logger.warning(f"[auto-grade] Failed: {exc}")

    async def _generate_call_summary(
        self,
        transcript: list[dict],
        state,
    ) -> str | None:
        """Call Gemini to produce a freeform narrative interview summary (best-effort)."""
        if not transcript:
            return None
        try:
            # Build plain-text transcript (last ~12k chars to stay within context)
            lines = [
                f"{t['role'].upper()}: {t['content']}"
                for t in transcript
            ]
            tx_text = "\n".join(lines)
            if len(tx_text) > 12_000:
                tx_text = tx_text[-12_000:]

            # Include rubric summary if available
            rubric_text = ""
            if state.grades:
                rubric_lines = [
                    f"  {cat}: {d['grade'].upper()} — {d['notes']}"
                    for cat, d in state.grades.items()
                ]
                rubric_text = "\nRubric grades:\n" + "\n".join(rubric_lines)

            prompt = (
                "You are an expert technical interviewer. Below is the transcript of a "
                "Google SDE mock interview conducted by AI interviewer Aura.\n\n"
                f"Transcript:\n{tx_text}\n"
                f"{rubric_text}\n\n"
                "Write a concise (150–250 word) narrative summary of this interview session. "
                "Cover: overall performance, strongest moments, areas for improvement, "
                "and one concrete recommendation. Address the candidate directly (use 'you')."
            )

            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=_gemini_text_model(),
                    contents=prompt,
                ),
                timeout=15.0,
            )
            summary = (resp.text or "").strip()
            if summary:
                logger.info(f"[summary] Generated {len(summary)}-char narrative summary")
            return summary or None
        except asyncio.TimeoutError:
            logger.warning("[summary] LLM summary timed out after 15s")
            return None
        except Exception as exc:
            logger.warning(f"[summary] Failed to generate summary: {exc}")
            return None

    async def _send_audio_loop(self, live_queue: LiveRequestQueue) -> None:
        track = await self._wait_for_audio_track()
        if track is None:
            logger.warning("[aura] No audio track found — send loop exiting")
            return

        # AudioStream with sample_rate=16000, num_channels=1:
        # LiveKit resamples + down-mixes in C++ (WebRTC signal processing).
        audio_stream = rtc.AudioStream(
            track,
            sample_rate=GEMINI_INPUT_SAMPLE_RATE,  # 16000
            num_channels=1,
            frame_size_ms=10,
            capacity=4,
        )

        # Silero VAD — used ONLY for instant frontend UI events (user-started/stopped-speaking).
        # Same role as Pipecat's vad_analyzer on the transport: fires speaking indicators fast,
        # client-side, without waiting for a Gemini round-trip.
        # Turn-end detection is still 100% Gemini server-side VAD — we never send
        # ActivityStart / ActivityEnd.
        vad = SileroVADAnalyzer(
            params=VADParams(
                confidence=0.75,  # matches solution2 / askjohngeorge
                start_secs=0.2,
                stop_secs=0.2,
                min_volume=0.6,   # matches Pipecat default (volume is EBU R128 now)
            )
        )
        user_speaking = False  # track state to avoid duplicate events
        self._last_activity_time = time.monotonic()  # reset idle clock on session start

        logger.info("[aura] Audio send loop started (Gemini server-side VAD + Silero barge-in UI)")

        frame_count = 0
        async for event in audio_stream:
            if not isinstance(event, rtc.AudioFrameEvent):
                continue

            # LiveKit delivers 16 kHz mono PCM16 — ready for Gemini.
            pcm = bytes(event.frame.data)
            if not pcm:
                continue

            frame_count += 1
            now = time.monotonic()

            # ── Silero VAD: instant barge-in (Pipecat-style) ────────────
            # Mirrors Pipecat's SileroVADAnalyzer on the transport layer.
            # ALWAYS fires user-started/stopped-speaking (no backchannel filter).
            # When allow_interruptions=True and bot is speaking, user speech
            # triggers _interrupt_bot() — same as Pipecat's CancelFrame.
            states = vad.process(pcm)
            for state in states:
                if state == VADState.SPEAKING and not user_speaking:
                    user_speaking = True
                    self._last_activity_time = now  # reset idle clock on speech start
                    logger.info(f"[aura] Silero → SPEAKING (frame={frame_count}, bot_speaking={self._bot_speaking})")
                    await self._on_user_started_speaking()
                elif state == VADState.QUIET and user_speaking:
                    user_speaking = False
                    logger.info(f"[aura] Silero → QUIET (frame={frame_count})")
                    await self._on_user_stopped_speaking()

            # Stream continuously — Gemini's built-in VAD decides turn boundaries.
            live_queue.send_realtime(
                genai_types.Blob(
                    data=pcm,
                    mime_type=f"audio/pcm;rate={GEMINI_INPUT_SAMPLE_RATE}",
                )
            )

            # Idle timeout: fires if no speech detected for idle_timeout_secs.
            # Updated by both Silero VAD and Gemini input_transcription events.
            if (now - self._last_activity_time) > self._config.idle_timeout_secs:
                self._ended_reason = "silence_timed_out"
                logger.warning(
                    f"[aura] User idle for >{self._config.idle_timeout_secs}s — ending session"
                )
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

    async def _process_events(
        self,
        runner: Runner,
        live_queue: LiveRequestQueue,
        run_config: RunConfig,
        cfg: AuraRoomConfig,
        audio_source: rtc.AudioSource,
        new_turns: list[dict],
    ) -> None:
        """Consume ADK Runner.run_live() events and forward to LiveKit."""
        bot_model_buf = ""   # model_turn text (non-native-audio fallback)
        bot_tx_buf = ""      # output_transcription (native audio ground truth)
        user_buf = ""        # input_transcription accumulator

        try:
            logger.info(f"[aura] Starting Runner.run_live (model={cfg.gemini_model})")
            await self._events.send({"type": "bot-ready"})

            async for event in runner.run_live(
                user_id=cfg.user_id,
                session_id=cfg.session_id,
                live_request_queue=live_queue,
                run_config=run_config,
            ):
                # ── Audio output → LiveKit ─────────────────────────────
                # ADK Events carry audio as content.parts[0].inline_data
                if (
                    event.content
                    and event.content.parts
                    and not self._is_output_blocked()
                ):
                    for part in event.content.parts:
                        blob = getattr(part, "inline_data", None)
                        if blob and blob.data and blob.mime_type and blob.mime_type.startswith("audio/"):
                            n = len(blob.data) // 2
                            if n > 0:
                                await self._on_bot_started_speaking()
                                await audio_source.capture_frame(
                                    rtc.AudioFrame(
                                        data=blob.data,
                                        sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
                                        num_channels=GEMINI_OUTPUT_CHANNELS,
                                        samples_per_channel=n,
                                    )
                                )

                        # Model turn text (non-native-audio fallback).
                        # In native audio mode output_transcription is the
                        # ground truth — only accumulate here for fallback at
                        # turn_complete; do NOT send events (avoids doubling).
                        txt = getattr(part, "text", None)
                        if txt and not self._is_output_blocked():
                            bot_model_buf += txt

                # ── User input transcription ───────────────────────────
                # event.partial=True → incremental; False/None → final
                if event.input_transcription:
                    chunk = (event.input_transcription.text or "").strip()
                    is_partial = bool(event.partial)
                    if chunk:
                        self._last_activity_time = time.monotonic()  # Gemini heard user — reset idle
                        logger.debug(f"[aura] user transcript (partial={is_partial}): {chunk!r}")

                        if is_partial:
                            # Incremental word chunk — accumulate into growing buffer
                            user_buf = (user_buf + " " + chunk).strip() if user_buf else chunk.strip()
                        else:
                            # Final — this is the full utterance (use as-is, replaces buffer)
                            user_buf = chunk
                            self._last_user_utterance = chunk

                        if _PAUSE_PHRASE_RE.search(chunk.lower()):
                            self._pause_requested = True
                            user_buf = ""
                            if self._audio_source is not None:
                                self._audio_source.clear_queue()
                            if self._bot_speaking:
                                self._bot_speaking = False
                                await self._events.send({"type": "bot-stopped-speaking"})
                            logger.info(f"[aura] Pause phrase: {chunk!r}")
                        else:
                            if self._pause_requested:
                                self._pause_requested = False
                                user_buf = ""
                                logger.info("[aura] User resumed after pause")
                                user_buf = chunk  # keep the resuming text

                        if user_buf.strip():
                            await self._events.send({
                                "type": "user-transcription",
                                "data": {"text": user_buf.strip(), "final": not is_partial},
                            })

                # ── Bot output transcription ───────────────────────────
                # event.partial=True → incremental chunk; False/None → final
                if (
                    event.output_transcription
                    and event.output_transcription.text
                    and not self._is_output_blocked()
                ):
                    new_tx = event.output_transcription.text
                    is_partial = bool(event.partial)

                    if is_partial:
                        # Incremental word chunk — accumulate into growing buffer
                        bot_tx_buf = (bot_tx_buf + new_tx) if bot_tx_buf else new_tx.strip()
                    else:
                        # Final complete transcript — use as-is
                        bot_tx_buf = new_tx

                    logger.debug(f"[aura] bot transcript (partial={is_partial}): {new_tx!r}")
                    await self._on_bot_started_speaking()
                    if bot_tx_buf.strip():
                        await self._events.send({
                            "type": "bot-transcription",
                            "data": {"text": bot_tx_buf.strip(), "final": not is_partial},
                        })

                # ── Gemini interrupted ─────────────────────────────────
                # MUST be processed BEFORE turn_complete (same event can carry both)
                if event.interrupted:
                    await self._interrupt_bot("gemini_interrupted")
                    bot_model_buf = ""
                    bot_tx_buf = ""
                    logger.info("[aura] Gemini server interrupted bot")

                # ── Turn complete ──────────────────────────────────────
                if event.turn_complete:
                    was_interrupted = self._interrupted
                    self._interrupted = False

                    final_bot = "" if (self._pause_requested or was_interrupted) else (
                        bot_tx_buf.strip() or bot_model_buf.strip()
                    )

                    if user_buf.strip():
                        logger.info(f"[aura] user turn: {user_buf.strip()[:80]}")
                        new_turns.append({"role": "user", "content": user_buf.strip()})
                        await self._events.send({
                            "type": "user-transcription",
                            "data": {"text": user_buf.strip(), "final": True},
                        })

                    if final_bot:
                        logger.info(f"[aura] bot turn: {final_bot[:80]}")
                        new_turns.append({"role": "model", "content": final_bot})
                        await self._events.send({
                            "type": "bot-transcription",
                            "data": {"text": final_bot, "final": True},
                        })

                    await self._on_bot_stopped_speaking()
                    bot_model_buf = ""
                    bot_tx_buf = ""
                    user_buf = ""
                    logger.debug(
                        f"[aura] State after turn_complete: "
                        f"bot_speaking={self._bot_speaking}, "
                        f"user_speaking={self._user_speaking}, "
                        f"interrupted={self._interrupted}, "
                        f"pause={self._pause_requested}"
                    )

                # ── Tool calls (logged — ADK dispatches automatically) ─
                if event.get_function_calls():
                    for fc in event.get_function_calls():
                        logger.info(f"[aura] Tool call: {fc.name}({dict(fc.args or {})})")

                # ── Tool responses (logged — ADK sends back automatically)
                if event.get_function_responses():
                    for fr in event.get_function_responses():
                        logger.info(f"[aura] Tool response: {fr.name} → {fr.response}")

                # ── Usage metrics ──────────────────────────────────────
                if event.usage_metadata:
                    um = event.usage_metadata
                    prompt_tok = getattr(um, "prompt_token_count", 0) or 0
                    completion_tok = getattr(um, "candidates_token_count", 0) or 0
                    total_tok = getattr(um, "total_token_count", 0) or 0
                    logger.debug(f"[aura] usage_metadata: prompt={prompt_tok}, completion={completion_tok}, total={total_tok}")
                    await self._events.send({
                        "type": "metrics",
                        "data": {"tokens": [{
                            "prompt_tokens": prompt_tok,
                            "completion_tokens": completion_tok,
                            "total_tokens": total_tok,
                        }]},
                    })

                # ── End session check ─────────────────────────────────
                if self._end_session_event.is_set():
                    self._ended_reason = "assistant_ended_call"
                    logger.info("[aura] end_conversation — closing event loop")
                    return

            logger.info("[aura] Runner.run_live stream ended normally")

        except Exception as exc:
            logger.error(f"[aura] event processing error: {type(exc).__name__}: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_room_bot(config: AuraRoomConfig) -> None:
    session = AuraVoiceSession(config)
    try:
        await session.run()
    except Exception as exc:
        logger.exception(f"[aura] Room bot crashed ({config.room_name}): {exc}")
