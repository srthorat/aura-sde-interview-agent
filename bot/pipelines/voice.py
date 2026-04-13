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
import uuid
from dataclasses import dataclass, field

import aiohttp
import websockets.exceptions

from google import genai
from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import VertexAiSessionService, InMemorySessionService, Session
from bot.sessions import FileSessionService
from google.genai import types as genai_types
from google.oauth2 import service_account
from loguru import logger
from livekit import rtc

from bot.audio.silero_vad import SileroVADAnalyzer, VADState, VADParams
from bot.agent import (
    build_adk_agent,
    create_session_state,
    destroy_session_state,
    export_session_state,
    get_session_delta,
    import_session_state,
    mark_session_baseline,
    _get_state as _agent_get_state,
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
_AFFIRMATIVE_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|please do|go ahead|that's right|correct|i am|i'm done|done)\b",
    re.IGNORECASE,
)
_RECAP_REQUEST_RE = re.compile(
    r"\b(?:recap|summary|summari[sz]e|what (?:have|did) we (?:cover|done)|remind me|where are we)\b",
    re.IGNORECASE,
)
_FEEDBACK_REQUEST_RE = re.compile(
    r"\b(?:feedback|score|scorecard|grade|graded|assessment|how did i do|rubric|report)\b",
    re.IGNORECASE,
)
_WRAP_REQUEST_RE = re.compile(
    r"\b(?:wrap up|end (?:this )?(?:round|session|interview)|let'?s stop|let'?s end|done for today|finish this round)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\b[\w']+\b")


def _is_exit_confirmation_prompt(text: str) -> bool:
    last_bot = (text or "").strip().lower()
    if not last_bot:
        return False
    return any(
        phrase in last_bot
        for phrase in (
            "are you done for today",
            "would you like to stop",
            "do you want to end the session",
            "shall we wrap up",
            "should we end here",
        )
    )


def _is_affirmative_exit_reply(user_text: str) -> bool:
    last_user = (user_text or "").strip().lower()
    if not last_user:
        return False
    return bool(_AFFIRMATIVE_RE.fullmatch(last_user) or _AFFIRMATIVE_RE.search(last_user))


def _is_recap_request(user_text: str) -> bool:
    return bool(_RECAP_REQUEST_RE.search((user_text or "").strip().lower()))


def _is_feedback_request(user_text: str) -> bool:
    return bool(_FEEDBACK_REQUEST_RE.search((user_text or "").strip().lower()))


def _is_wrap_request(user_text: str) -> bool:
    last_user = (user_text or "").strip().lower()
    return bool(_WRAP_REQUEST_RE.search(last_user) or _EXIT_PHRASE_RE.search(last_user))


def _tool_timing_guard(tool_name: str, last_user: str, session_delta: dict[str, object], has_prior_history: bool) -> dict[str, str] | None:
    """Return a blocking response when a read-only tool is called at the wrong time."""
    current_questions = len(session_delta.get("questions", []))
    current_notes = len(session_delta.get("notes", []))
    current_grades = len(session_delta.get("grades", {}))

    if tool_name == "get_session_summary":
        startup_recap_ok = current_questions == 0 and has_prior_history
        explicit_recap_ok = _is_recap_request(last_user)
        if startup_recap_ok or explicit_recap_ok:
            return None
        return {
            "status": "blocked",
            "reason": "Use get_session_summary only for startup recap or when the candidate explicitly asks for a recap.",
        }

    if tool_name in {"get_round_scorecard", "get_rubric_report"}:
        explicit_feedback_ok = _is_feedback_request(last_user) or _is_wrap_request(last_user)
        round_wrap_ok = current_notes >= 2 or current_grades >= 2 or current_questions >= 3
        if explicit_feedback_ok or round_wrap_ok:
            return None
        return {
            "status": "blocked",
            "reason": f"Use {tool_name} only at round wrap-up or when the candidate explicitly asks for feedback.",
        }

    return None


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
    """Pick session service based on environment variables.

    Priority:
      1. VERTEX_AI_REASONING_ENGINE_ID set → VertexAiSessionService (GCP-managed, survives deployments)
      2. SESSION_PERSIST_DIR set           → FileSessionService (disk-backed, survives restarts)
      3. neither                           → InMemorySessionService (RAM only, resets on restart)
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
    persist_dir = os.environ.get("SESSION_PERSIST_DIR", "").strip()

    if project and engine_id:
        try:
            svc = VertexAiSessionService(project=project, location=location)
            logger.info(f"[adk] Using VertexAiSessionService (engine: {engine_id})")
            return svc
        except Exception as exc:
            logger.warning(f"[adk] VertexAiSessionService unavailable, falling back: {exc}")

    if persist_dir:
        try:
            svc = FileSessionService(persist_dir=persist_dir)
            logger.info(f"[adk] Using FileSessionService — sessions persist to {persist_dir}")
            return svc
        except Exception as exc:
            logger.warning(f"[adk] FileSessionService unavailable, using InMemory: {exc}")

    logger.info("[adk] Using InMemorySessionService (no persistence across restarts)")
    return InMemorySessionService()


_session_service = None
_session_service_failures = 0
_SESSION_SERVICE_FAILURE_RESET_THRESHOLD = 3


def _get_session_service():
    """Return the ADK session service singleton. Re-creates on repeated failures."""
    global _session_service, _session_service_failures
    if _session_service is None:
        _session_service = _build_session_service()
        _session_service_failures = 0
    return _session_service


def _record_session_service_success() -> None:
    """Clear the failure counter after a successful session-service operation."""
    global _session_service_failures
    if _session_service_failures:
        logger.info("[adk] Session service recovered — clearing failure counter")
    _session_service_failures = 0


def _record_session_service_failure(operation: str, exc: Exception) -> None:
    """Track repeated session-service failures and force a rebuild when needed."""
    global _session_service_failures
    _session_service_failures += 1
    logger.warning(
        f"[adk] Session service failure during {operation} "
        f"({_session_service_failures}/{_SESSION_SERVICE_FAILURE_RESET_THRESHOLD}): {exc}"
    )
    if _session_service_failures >= _SESSION_SERVICE_FAILURE_RESET_THRESHOLD:
        logger.warning(
            f"[adk] Session service hit failure threshold during {operation} — forcing rebuild"
        )
        _reset_session_service()
        _session_service_failures = 0


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


class _NullEvents:
    """No-op _Events stub used before the LiveKit room is connected.

    Allows Gemini WS tasks to start (and kickoff to be sent) before
    room.connect() completes so Gemini's first-audio generation overlaps
    the LiveKit handshake.  Replaced by a real _Events instance once the
    room is connected.
    """

    async def send(self, _event: dict) -> None:  # noqa: D102
        pass  # silently discard pre-connect UI events


# ---------------------------------------------------------------------------
# ADK session helpers
# ---------------------------------------------------------------------------

# Approximate token cap for injected history (~500 tokens ≈ 2 000 chars at ~4 chars/token).
# Keeps system instruction within Gemini's context but avoids burning prompt budget.
_HISTORY_CHAR_LIMIT = 1_500   # trimmed from 2000 — reduces per-turn token count
_HISTORY_RECENT_TURN_LIMIT = 3   # trimmed from 4 — fewer turns in context
_HISTORY_QUESTION_LIMIT = 2      # trimmed from 3 — only last 2 asked questions
_SESSION_STATE_MARKER = "AURA_SESSION_STATE_V1:"

# In-process cache: avoids create_session() on every reconnection.
# Pre-warmed Session OBJECTS (user_id → fresh Session, ready to use instantly).
# Storing the object avoids a get_session() API call (saves ~1.9s) on connect.
# Each session is created fresh so it has no prior conversation history —
# that prevents Gemini from processing old turns and interrupting the greeting.
_vertex_session_obj_cache: dict[str, "Session"] = {}

# In-memory state snapshots (user_id → snapshot dict).
# Updated by _persist_session_state at session end; read by
# _vertex_get_or_create_session to restore state without a Vertex API call.
# Lost on container restart (acceptable — users start fresh round-count).
_vertex_state_snapshot_cache: dict[str, dict] = {}

# In-flight pre-warm tasks (user_id → Task).
_vertex_prewarm_tasks: dict[str, "asyncio.Task[None]"] = {}


async def _vertex_prewarm_for_user(
    session_service,
    app_name: str,
    user_id: str,
) -> None:
    """Create a fresh Vertex AI session and cache the Session object.

    Also loads the prior session's state snapshot from Vertex in parallel so
    that interview history (questions, grades, notes) survives container
    restarts.  Always creates a NEW session so that run_live() never receives
    old conversation history, which was causing Gemini to interrupt the
    opening greeting.
    """
    if _is_anon_user(user_id):
        _vertex_prewarm_tasks.pop(user_id, None)
        return

    async def _load_prior_state() -> None:
        """Fetch latest prior session from Vertex and restore state snapshot."""
        if user_id in _vertex_state_snapshot_cache:
            return  # already in memory (same container lifetime)
        try:
            result = await session_service.list_sessions(app_name=app_name, user_id=user_id)
            prior_sessions = getattr(result, "sessions", []) or []
            if not prior_sessions:
                return
                
            # Iterate through prior sessions until we find one with a valid snapshot.
            # This handles the case where the most recent session was dropped/empty.
            for session_meta in prior_sessions:
                prior = await session_service.get_session(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_meta.id,
                )
                snapshot = _extract_session_state_snapshot(getattr(prior, "events", []) or [])
                if snapshot:
                    _vertex_state_snapshot_cache[user_id] = snapshot
                    logger.info(f"[adk] Pre-warm: restored state snapshot for {user_id} from Vertex session {session_meta.id}")
                    return
        except Exception as exc:
            logger.debug(f"[adk] Pre-warm: could not load prior state for {user_id}: {exc}")

    _vs_t = time.monotonic()
    try:
        # Run in parallel: create fresh session + load prior state.
        # Both are network-bound and independent.
        session, _ = await asyncio.gather(
            session_service.create_session(app_name=app_name, user_id=user_id),
            _load_prior_state(),
        )
        _vertex_session_obj_cache[user_id] = session
        logger.info(
            f"[adk] Pre-warm: cached Session object {session.id} for {user_id} "
            f"({int((time.monotonic() - _vs_t) * 1000)}ms)"
        )
    except Exception as exc:
        logger.warning(f"[adk] Pre-warm failed for {user_id}: {exc}")
    finally:
        _vertex_prewarm_tasks.pop(user_id, None)


def _resolve_app_name(agent_name: str) -> str:
    engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if engine_id and project:
        return f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
    return agent_name


def _extract_text_from_event(event) -> tuple[str, str] | None:
    try:
        content = getattr(event, "content", None)
        if not content:
            return None
        role = getattr(content, "role", "unknown")
        parts = getattr(content, "parts", [])
        text = " ".join(
            getattr(part, "text", "") for part in parts if getattr(part, "text", None)
        ).strip()
        if not text:
            return None
        return role, text
    except Exception:
        return None


def _extract_session_state_snapshot(history: list) -> dict | None:
    for event in reversed(history):
        item = _extract_text_from_event(event)
        if not item:
            continue
        _role, text = item
        if not text.startswith(_SESSION_STATE_MARKER):
            continue
        payload = text[len(_SESSION_STATE_MARKER):].strip()
        try:
            snapshot = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("[adk] Ignoring invalid persisted session snapshot")
            return None
        return snapshot if isinstance(snapshot, dict) else None
    return None


def _history_to_context(history: list) -> str:
    """Convert ADK session events into a compact context snippet.

    Synthetic persisted-state events are ignored. For longer histories, keep a
    compact list of recently asked questions plus the latest exchange instead of
    reinjecting the full transcript.
    """
    if not history:
        return ""
    turns: list[tuple[str, str]] = []
    for event in history:
        item = _extract_text_from_event(event)
        if not item:
            continue
        role, text = item
        if text.startswith(_SESSION_STATE_MARKER):
            continue
        turns.append((role, text))

    if not turns:
        return ""

    if len(turns) <= _HISTORY_RECENT_TURN_LIMIT:
        lines = [f"{role.upper()}: {text}" for role, text in turns]
        context = "Recent conversation history:\n" + "\n".join(lines)
        return context if len(context) <= _HISTORY_CHAR_LIMIT else ""

    recent_lines = [
        f"{role.upper()}: {text}"
        for role, text in turns[-_HISTORY_RECENT_TURN_LIMIT:]
    ]
    asked_questions: list[str] = []
    for role, text in turns:
        if role.lower() != "model" or "?" not in text:
            continue
        question = text.strip()
        if question in asked_questions:
            continue
        asked_questions.append(question)

    sections: list[str] = []
    if asked_questions:
        question_lines = asked_questions[-_HISTORY_QUESTION_LIMIT:]
        sections.append(
            "Questions already covered:\n" + "\n".join(f"- {question}" for question in question_lines)
        )
    sections.append("Latest exchange:\n" + "\n".join(recent_lines))
    context = "Recent conversation context:\n" + "\n\n".join(sections)
    return context if len(context) <= _HISTORY_CHAR_LIMIT else ""


def _is_anon_user(user_id: str) -> bool:
    """Anonymous users don't need cross-session Vertex AI persistence."""
    return not user_id or user_id in ("anonymous", "anon") or user_id.startswith("anon_")


async def _vertex_get_or_create_session(
    session_service,
    app_name: str,
    user_id: str,
    local_session_id: str,
) -> Session:
    """Return a fresh Vertex AI session for this run.

    Always uses a newly-created session (from pre-warm cache or created here)
    so Gemini never sees prior conversation turns. Interview state restoration
    prefers the in-memory _vertex_state_snapshot_cache for speed, and falls back
    to persisted session-event snapshots when cache is empty (for example after
    process/container restart).
    """
    session: Session | None = None

    # Fast path: use the pre-warmed Session object directly (0 API calls).
    session = _vertex_session_obj_cache.pop(user_id, None)
    if session is not None:
        logger.info(f"[adk] Using pre-warmed session {session.id} for {user_id} (0ms, object cache)")

    if session is None:
        # If /api/candidate/check fired a pre-warm that’s still in flight,
        # await it instead of creating a duplicate session.
        prewarm_task = _vertex_prewarm_tasks.get(user_id)
        if prewarm_task and not prewarm_task.done():
            logger.info(f"[adk] Awaiting in-flight pre-warm for {user_id}")
            try:
                await prewarm_task
            except Exception:
                pass  # failure already logged inside the task
            session = _vertex_session_obj_cache.pop(user_id, None)
            if session:
                logger.info(f"[adk] Got pre-warmed session {session.id} for {user_id} after await")

    if session is None:
        # No pre-warm available — create fresh session now.
        _vs_t = time.monotonic()
        session = await session_service.create_session(app_name=app_name, user_id=user_id)
        logger.info(f"[adk] Created Vertex AI session {session.id} for {user_id} ({int((time.monotonic()-_vs_t)*1000)}ms)")

    _record_session_service_success()

    # Restore interview state from in-memory snapshot (fast path).
    if not _is_anon_user(user_id):
        snapshot = _vertex_state_snapshot_cache.get(user_id)
        if snapshot:
            import_session_state(local_session_id, snapshot)
            logger.info(f"[adk] Restored interview state (in-memory snapshot) for {user_id}")
        else:
            # Slow-path fallback: recover from persisted ADK session events so
            # named-candidate history survives service restarts.
            # Scan up to 10 most-recent sessions, one snapshot per unique round,
            # then merge across rounds so full multi-round history is restored.
            try:
                _restore_t0 = time.monotonic()
                sessions = await session_service.list_sessions(app_name=app_name, user_id=user_id)
                existing = getattr(sessions, "sessions", []) or []
                seen_rounds: set = set()
                merged: dict = {}
                for session_meta in existing[:10]:
                    session_id = getattr(session_meta, "id", "")
                    if not session_id or session_id == session.id:
                        continue
                    prior = await session_service.get_session(
                        app_name=app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    prior_snapshot = _extract_session_state_snapshot(getattr(prior, "events", []) or [])
                    if not prior_snapshot:
                        continue
                    round_key = prior_snapshot.get("current_round")
                    if round_key in seen_rounds:
                        continue
                    seen_rounds.add(round_key)
                    if not merged:
                        merged = dict(prior_snapshot)
                    else:
                        # Grades: most-recent (already in merged) wins for overlapping keys
                        prior_grades = prior_snapshot.get("grades", {})
                        merged_grades = {**prior_grades, **merged.get("grades", {})}
                        merged["grades"] = merged_grades
                        # Asked questions: deduplicated union
                        prior_asked = prior_snapshot.get("asked", [])
                        existing_asked = merged.get("asked", [])
                        merged["asked"] = prior_asked + [q for q in existing_asked if q not in prior_asked]
                        # Notes: concatenate
                        merged["notes"] = prior_snapshot.get("notes", []) + merged.get("notes", [])
                if merged:
                    import_session_state(local_session_id, merged)
                    _vertex_state_snapshot_cache[user_id] = merged
                    logger.info(
                        f"[adk] Restored interview state from {len(seen_rounds)} unique round(s) for {user_id} "
                        f"(fallback restore took {int((time.monotonic() - _restore_t0) * 1000)}ms)"
                    )
                else:
                    logger.info(
                        f"[adk] Vertex fallback restore: no prior snapshot found for {user_id} "
                        f"(scanned {min(len(existing), 10)} sessions, "
                        f"{int((time.monotonic() - _restore_t0) * 1000)}ms)"
                    )
            except Exception as exc:
                logger.warning(f"[adk] Vertex fallback restore failed for {user_id}: {exc}")

    return session


async def _restore_prior_session_state(
    session_service,
    app_name: str,
    user_id: str,
    live_session_id: str,
) -> bool:
    """Fallback restore for InMemory/File session services (non-Vertex path).

    For Vertex AI, restoration is handled inline by _vertex_get_or_create_session.
    """
    if _is_anon_user(user_id):
        return False
    try:
        sessions = await session_service.list_sessions(app_name=app_name, user_id=user_id)
        existing = getattr(sessions, "sessions", []) or []
        for session_meta in existing:
            session_id = getattr(session_meta, "id", "")
            if not session_id or session_id == live_session_id:
                continue
            session = await session_service.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
            snapshot = _extract_session_state_snapshot(getattr(session, "events", []) or [])
            if not snapshot:
                continue
            import_session_state(live_session_id, snapshot)
            _record_session_service_success()
            logger.info(
                f"[adk] Restored persisted interview state from session {session_id} for user {user_id}"
            )
            return True
        _record_session_service_success()
    except Exception as exc:
        _record_session_service_failure(f"restore session state for {user_id}", exc)
        logger.warning(f"[adk] Failed to restore persisted interview state for {user_id}: {exc}")
    return False


async def _persist_session_state(
    session_service,
    app_name: str,
    user_id: str,
    session_id: str,
    snapshot: dict,
) -> None:
    if _is_anon_user(user_id):
        return
    # Cache the snapshot in memory so the next connect can restore state
    # instantly (no get_session() API call needed).
    _vertex_state_snapshot_cache[user_id] = snapshot
    try:
        session = await session_service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        payload = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
        event = Event(
            author="system",
            invocation_id=str(uuid.uuid4()),
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=f"{_SESSION_STATE_MARKER}{payload}")],
            ),
        )
        await session_service.append_event(session=session, event=event)
        _record_session_service_success()
        logger.info(f"[adk] Persisted interview state snapshot for {user_id}")
    except Exception as exc:
        _record_session_service_failure(f"persist session state for {user_id}", exc)
        logger.warning(f"[adk] Failed to persist interview state snapshot for {user_id}: {exc}")


async def _load_adk_session(
    session_service, agent: Agent, user_id: str
) -> tuple[Session, str]:
    app_name = _resolve_app_name(agent.name)
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
        _record_session_service_success()
        return session, _history_to_context(history)
    except Exception as exc:
        _record_session_service_failure(f"load session for {user_id}", exc)
        logger.warning(f"[adk] Session load failed ({user_id}), starting fresh: {exc}")

        fresh_service = _get_session_service()
        session = await fresh_service.create_session(app_name=app_name, user_id=user_id)
        _record_session_service_success()
        return session, ""


async def _append_turn(
    session_service, agent: Agent, session: Session, role: str, text: str
) -> None:
    try:
        content = genai_types.Content(role=role, parts=[genai_types.Part(text=text)])
        event = Event(author=role, invocation_id=str(uuid.uuid4()), content=content)
        await session_service.append_event(session=session, event=event)
        _record_session_service_success()
    except Exception as exc:
        _record_session_service_failure(f"append {role} turn", exc)
        logger.warning(f"[adk] Session append failed: {exc}")


# ---------------------------------------------------------------------------
# Main voice session
# ---------------------------------------------------------------------------

class AuraVoiceSession:
    def __init__(self, config: AuraRoomConfig):
        self._config = config
        self._room = rtc.Room()
        self._client = _build_genai_client()
        self._events: _Events | _NullEvents = _NullEvents()
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
        self._last_bot_utterance = ""    # last final bot utterance (for exit confirmation)
        self._user_stopped_speaking_at: float | None = None  # for STS latency measurement
        self._session_graded_keys: set[str] = set()  # categories graded in THIS session

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
        _t0 = time.monotonic()
        _ms = lambda: int((time.monotonic() - _t0) * 1000)
        self._startup_t0 = _t0
        logger.info(f"[timing] T+0ms run() started (user={cfg.user_id})")
        session_service = _get_session_service()
        logger.info(f"[timing] T+{_ms()}ms session_service ready ({type(session_service).__name__})")

        # ── End-session signal: set by after_tool_callback when end_conversation fires
        self._end_session_event = asyncio.Event()

        # ── Tool callbacks ────────────────────────────────────────────────
        # before_tool_callback: sets per-session context so tools read the right state
        import bot.agent as _agent_mod

        def _before_tool(tool, args, tool_context, **kwargs):
            _agent_mod._session_id_context = cfg.session_id
            tool_name = getattr(tool, "name", "")
            if tool_name in {"get_session_summary", "get_round_scorecard", "get_rubric_report"}:
                session_delta = get_session_delta(cfg.session_id)
                has_prior_history = bool(_prior_grades or session_delta.get("prior_grades"))
                blocked = _tool_timing_guard(
                    tool_name,
                    self._last_user_utterance,
                    session_delta,
                    has_prior_history,
                )
                if blocked is not None:
                    logger.info(f"[aura] Blocking {tool_name} — {blocked['reason']}")
                    return blocked
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
                affirmed_exit = _is_exit_confirmation_prompt(self._last_bot_utterance) and _is_affirmative_exit_reply(last)
                if last and not _EXIT_PHRASE_RE.search(last) and not affirmed_exit:
                    logger.warning(
                        f"[aura] Blocking end_conversation — last utterance has no exit phrase: {last!r}"
                    )
                    return {"status": "blocked", "reason": "The candidate did NOT ask to leave. The session is NOT ending. Continue the interview normally."}
                logger.info(f"[aura] end_conversation allowed — exit confirmed by user utterance: {last!r}")
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
                        self._generate_call_summary(_turns, _state, prior_grades=_prior_grades)
                    )
                    logger.info("[aura] Early summary+grade tasks started (parallel with goodbye)")
                except Exception as exc:
                    logger.warning(f"[aura] Failed to start early summary: {exc}")
            # Emit rubric-update events for live grade display in the frontend.
            # Support both direct submit_rubric_grade and batched evaluate_candidate_answer.
            tool_name = getattr(tool, "name", None)
            if isinstance(tool_response, dict):
                updates: list[dict[str, str]] = []

                if tool_name == "submit_rubric_grade" and tool_response.get("status") == "graded":
                    updates.append(
                        {
                            "category": str(tool_response.get("category", "")),
                            "grade": str(tool_response.get("grade", "")),
                            "notes": str(args.get("notes", "")),
                        }
                    )

                if tool_name == "evaluate_candidate_answer" and tool_response.get("status") == "success":
                    for item in tool_response.get("grades_submitted", []) or []:
                        if not isinstance(item, dict):
                            continue
                        updates.append(
                            {
                                "category": str(item.get("category", "")),
                                "grade": str(item.get("grade", "")),
                                "notes": "",
                            }
                        )

                for update in updates:
                    cat = update.get("category", "")
                    if cat:
                        self._session_graded_keys.add(cat)  # track for session_grades
                    try:
                        asyncio.create_task(
                            self._events.send(
                                {
                                    "type": "rubric-update",
                                    "data": {
                                        "category": cat,
                                        "grade": update.get("grade", ""),
                                        "notes": update.get("notes", ""),
                                    },
                                }
                            )
                        )
                    except Exception as _re:
                        logger.debug(f"[rubric] rubric-update send failed: {_re}")
            return None  # don't alter result

        adk_agent = build_adk_agent(
            system_instruction=cfg.system_instruction,
            model=cfg.gemini_model,
            before_tool_callback=_before_tool,
            after_tool_callback=_after_tool,
        )

        # ── Runner + LiveRequestQueue ─────────────────────────────────────
        app_name = _resolve_app_name(adk_agent.name)

        _using_vertex = bool(os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip())

        # Anonymous users don't need persistence — override to InMemory BEFORE
        # building the Runner so we never construct a Runner with the wrong service.
        if _using_vertex and _is_anon_user(cfg.user_id):
            logger.info(f"[timing] T+{_ms()}ms anon user — using InMemory (bypassing Vertex AI)")
            session_service = InMemorySessionService()
            _using_vertex = False

        runner = Runner(
            agent=adk_agent,
            app_name=app_name,
            session_service=session_service,
            auto_create_session=True,
        )
        runner.context_cache_config = ContextCacheConfig(
            ttl_seconds=3600,
            min_tokens=0,
        )
        logger.info(f"[timing] T+{_ms()}ms agent + runner built ({type(session_service).__name__}, model={cfg.gemini_model})")

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
                    silence_duration_ms=600,   # 600ms — balanced: fast turns without premature cuts
                    prefix_padding_ms=200,     # 200ms — standard prefix padding
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

        # ── Parallel startup: session creation + room connection + state restore ──
        # All three involve network round-trips. create_session_state is local/sync
        # so it runs first to ensure import_session_state has a target to write into.
        logger.info(f"[aura] Connecting to room {cfg.room_name} (user={cfg.user_id})")
        create_session_state(cfg.session_id)

        # ── Session init + (optionally) early Gemini WS ─────────────────────────
        # FAST PATH: If the Vertex session object is already in the pre-warm cache
        # we can start Gemini tasks BEFORE room.connect, overlapping the 2.3 s
        # LiveKit handshake with Gemini's ~4.8 s first-audio generation time.
        # Expected saving: TTFA 7.3 s → ~4.8 s  (-2.5 s).
        #
        # Safety guarantee: Gemini TTFA (~4.8 s from kickoff) > room.connect +
        # publish_track (~2.5 s total), so the audio track is always live before
        # the first audio frame arrives.  _NullEvents() silently drops any UI
        # events fired before room.connect completes; real _Events replaces it
        # immediately after room.connect.
        _early_kickoff = False  # True when kickoff was already sent before room.connect

        if _using_vertex and cfg.user_id in _vertex_session_obj_cache:
            # ── OPTIMISED PATH: pre-warm cache hit ──────────────────────────────
            _adk_session = _vertex_session_obj_cache.pop(cfg.user_id)
            _record_session_service_success()
            if not _is_anon_user(cfg.user_id):
                _snap = _vertex_state_snapshot_cache.get(cfg.user_id)
                if _snap:
                    import_session_state(cfg.session_id, _snap)
                    logger.info(f"[adk] Restored interview state (in-memory snapshot) for {cfg.user_id}")
            logger.info(f"[adk] Using pre-warmed session {_adk_session.id} for {cfg.user_id} (0ms, object cache)")

            _prior_grades: dict = dict(_agent_get_state(cfg.session_id).grades)
            mark_session_baseline(cfg.session_id)
            logger.info(f"[aura] Prior grades loaded: {len(_prior_grades)} categories for {cfg.user_id}")

            # Create audio objects (pure local — no network needed).
            audio_source = rtc.AudioSource(
                GEMINI_OUTPUT_SAMPLE_RATE,
                GEMINI_OUTPUT_CHANNELS,
                queue_size_ms=300,
            )
            self._audio_source = audio_source
            out_track = rtc.LocalAudioTrack.create_audio_track("aura-audio", audio_source)
            start_time = time.monotonic()
            new_turns: list[dict] = []

            # Spawn Gemini WS tasks NOW — before room.connect.
            # self._events is still _NullEvents() so early sends are silently dropped.
            logger.info(f"[timing] T+{_ms()}ms → EARLY task spawn (Gemini WS starts before room.connect)")
            recv_task = asyncio.create_task(
                self._process_events(runner, live_queue, run_config, cfg, audio_source, new_turns, _adk_session),
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

            # Yield so recv_task enters runner.run_live() and opens the Gemini WS.
            await asyncio.sleep(0)

            # Send kickoff IMMEDIATELY — Gemini begins generating while room connects.
            self._bot_speaking = True
            logger.info(f"[timing] T+{_ms()}ms → kickoff sent EARLY (Gemini generating during room.connect)")
            live_queue.send_content(
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text="Begin the session now.")],
                )
            )
            _early_kickoff = True

            # Connect room — Gemini is already processing in the background.
            logger.info(f"[timing] T+{_ms()}ms → room.connect start (Gemini Live active in background)")
            await self._room.connect(cfg.livekit_url, cfg.token)
            logger.info(f"[timing] T+{_ms()}ms ✓ room.connect done")

            # Promote from _NullEvents to real _Events now that the room is connected.
            self._events = _Events(self._room)
            await self._events.send({"type": "bot-ready"})

            @self._room.on("participant_disconnected")
            def _on_participant_left(participant: rtc.RemoteParticipant):
                logger.info(f"[aura] Participant {participant.identity} left — signalling client disconnect")
                self._client_disconnected.set()

            _adk_session_id: str = _adk_session.id

        elif _using_vertex:
            # ── STANDARD VERTEX PATH: session not yet cached (cold / in-flight pre-warm) ──
            logger.info(f"[timing] T+{_ms()}ms → room.connect + Vertex session start (parallel)")
            try:
                _adk_session, _ = await asyncio.gather(
                    _vertex_get_or_create_session(
                        session_service, app_name, cfg.user_id, cfg.session_id
                    ),
                    self._room.connect(cfg.livekit_url, cfg.token),
                )
                logger.info(f"[timing] T+{_ms()}ms ✓ room.connect + Vertex session ready (id={_adk_session.id})")
            except Exception as _exc:
                logger.warning(f"[adk] Vertex AI session setup failed, falling back to in-memory: {_exc}")
                _fallback_svc = InMemorySessionService()
                # IMPORTANT: rebuild runner with the fallback service — the runner
                # holds a reference to its session service, so passing a session
                # created by a different service to run_live() would cause a lookup
                # failure. session_service local var is also updated so _persist
                # uses the correct service at teardown.
                session_service = _fallback_svc
                runner = Runner(
                    agent=adk_agent,
                    app_name=app_name,
                    session_service=_fallback_svc,
                    auto_create_session=True,
                )
                runner.context_cache_config = ContextCacheConfig(
                    ttl_seconds=3600,
                    min_tokens=0,
                )
                _adk_session, _ = await asyncio.gather(
                    _fallback_svc.create_session(app_name=app_name, user_id=cfg.user_id),
                    self._room.connect(cfg.livekit_url, cfg.token),
                )
                logger.info(f"[timing] T+{_ms()}ms ✓ room.connect + InMemory fallback session ready")

            _prior_grades = dict(_agent_get_state(cfg.session_id).grades)
            mark_session_baseline(cfg.session_id)
            logger.info(f"[aura] Prior grades loaded: {len(_prior_grades)} categories for {cfg.user_id}")

            self._events = _Events(self._room)

            @self._room.on("participant_disconnected")
            def _on_participant_left(participant: rtc.RemoteParticipant):
                logger.info(f"[aura] Participant {participant.identity} left — signalling client disconnect")
                self._client_disconnected.set()

            _adk_session_id = _adk_session.id if _adk_session else cfg.session_id

        else:
            # ── NON-VERTEX PATH (FileSession / InMemory) ──
            logger.info(f"[timing] T+{_ms()}ms → room.connect + state restore start (parallel)")
            _adk_session = None
            await asyncio.gather(
                self._room.connect(cfg.livekit_url, cfg.token),
                _restore_prior_session_state(session_service, app_name, cfg.user_id, cfg.session_id),
            )
            logger.info(f"[timing] T+{_ms()}ms ✓ room.connect + state restore done")

            _prior_grades = dict(_agent_get_state(cfg.session_id).grades)
            mark_session_baseline(cfg.session_id)
            logger.info(f"[aura] Prior grades loaded: {len(_prior_grades)} categories for {cfg.user_id}")

            self._events = _Events(self._room)

            @self._room.on("participant_disconnected")
            def _on_participant_left(participant: rtc.RemoteParticipant):
                logger.info(f"[aura] Participant {participant.identity} left — signalling client disconnect")
                self._client_disconnected.set()

            _adk_session_id = cfg.session_id

        # ── Audio source + task spawn (skipped for early-kickoff path) ───────────
        if not _early_kickoff:
            # Create audio source + track.
            # queue_size_ms=300: generous buffer so frames are never dropped while
            # the Gemini WebSocket setup handshake is in flight before kickoff.
            audio_source = rtc.AudioSource(
                GEMINI_OUTPUT_SAMPLE_RATE,
                GEMINI_OUTPUT_CHANNELS,
                queue_size_ms=300,
            )
            self._audio_source = audio_source
            out_track = rtc.LocalAudioTrack.create_audio_track("aura-audio", audio_source)
            start_time = time.monotonic()
            new_turns: list[dict] = []

            # ── Spawn ADK tasks BEFORE publish_track so the Gemini Live WebSocket
            # (BidiGenerateContentSetup handshake, ~400-800 ms) runs in parallel
            # with publish_track (~200-400 ms). Kickoff content is NOT sent until
            # after publish_track completes so no audio frames are produced before
            # the track is live — no frames can be lost.
            logger.info(f"[timing] T+{_ms()}ms → spawning recv+send tasks (Gemini WS pre-warming)")
            recv_task = asyncio.create_task(
                self._process_events(runner, live_queue, run_config, cfg, audio_source, new_turns, _adk_session),
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

            # Yield once so recv_task begins Gemini WS handshake immediately.
            await asyncio.sleep(0)

        # Publish local audio track — always required regardless of path.
        logger.info(f"[timing] T+{_ms()}ms → publish_track start")
        await self._room.local_participant.publish_track(
            out_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        logger.info(f"[timing] T+{_ms()}ms ✓ publish_track done — client can subscribe to bot audio")

        await self._events.send({
            "type": "session-config",
            "data": {
                "idle_timeout_secs": cfg.idle_timeout_secs,
                "max_duration_secs": cfg.max_duration_secs,
            },
        })

        # ADK live sessions need an initial content event to begin generation.
        # In the early-kickoff path this was already sent before room.connect.
        if not _early_kickoff:
            self._bot_speaking = True
            logger.info(f"[timing] T+{_ms()}ms → kickoff prompt sent to Gemini via live_queue")
            live_queue.send_content(
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text="Begin the session now.")],
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
                self._generate_call_summary(new_turns, state, prior_grades=_prior_grades)
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

        # session_grades: grades that are new or changed vs what existed at session-start.
        # Uses set-diff against _prior_grades rather than _session_graded_keys so that
        # grades from live tool calls AND auto-grade are both captured even if the
        # _after_tool callback missed a category (e.g. on abrupt WS disconnect).
        session_delta = get_session_delta(cfg.session_id)
        _session_grade_snapshot = session_delta["grades"]
        # Fall back to all current grades if the diff is empty but grades exist
        # (handles first-session users and reconnects that restored full state).
        _session_grades_final = _session_grade_snapshot if _session_grade_snapshot else state.grades

        summary_payload = {
            "duration_secs": round(elapsed, 1),
            "ended_reason": self._ended_reason,
            "history_enabled": not _is_anon_user(cfg.user_id),
            "questions_asked": session_delta["questions"],
            "rubric_grades": state.grades,
            "session_grades": _session_grades_final,
            "prior_grades": _prior_grades,
            "answer_notes": session_delta["notes"],
            "narrative_summary": narrative_summary,
        }

        # Store for HTTP fallback (frontend polls if data-channel delivery fails)
        _room_summaries[cfg.room_name] = summary_payload

        await _persist_session_state(
            session_service,
            app_name,
            cfg.user_id,
            _adk_session_id,
            export_session_state(cfg.session_id),
        )

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
                    self._session_graded_keys.add(cat_key)  # track for session_grades
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
        prior_grades: dict | None = None,
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

            # Include current session rubric summary if available
            rubric_text = ""
            if state.grades:
                rubric_lines = [
                    f"  {cat}: {d['grade'].upper()} — {d['notes']}"
                    for cat, d in state.grades.items()
                ]
                rubric_text = "\nThis session's rubric grades:\n" + "\n".join(rubric_lines)

            # Include prior session grades context so the narrative can reference progress
            prior_text = ""
            if prior_grades:
                prior_lines = [
                    f"  {cat}: {d['grade'].upper()} — {d['notes']}"
                    for cat, d in prior_grades.items()
                ]
                prior_text = "\nPrior session grades (for context on progress):\n" + "\n".join(prior_lines)

            has_prior = bool(prior_grades)
            continuation_note = (
                " This candidate has prior interview history — where relevant, "
                "comment on improvement or regression compared to prior sessions."
                if has_prior else ""
            )

            prompt = (
                "You are an expert technical interviewer. Below is the transcript of a "
                "Google SDE mock interview conducted by AI interviewer Aura.\n\n"
                f"Transcript:\n{tx_text}\n"
                f"{rubric_text}\n"
                f"{prior_text}\n\n"
                "Write a concise (150–250 word) narrative summary of this interview session. "
                "Cover: overall performance, strongest moments, areas for improvement, "
                f"and one concrete recommendation. Address the candidate directly (use 'you').{continuation_note}"
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
        adk_session=None,
    ) -> None:
        """Consume ADK Runner.run_live() events and forward to LiveKit."""
        bot_model_buf = ""   # model_turn text (non-native-audio fallback)
        bot_tx_buf = ""      # output_transcription (native audio ground truth)
        user_buf = ""        # input_transcription accumulator
        bot_final_emitted = False

        _proc_t0 = getattr(self, "_startup_t0", time.monotonic())
        _proc_ms = lambda: int((time.monotonic() - _proc_t0) * 1000)

        try:
            logger.info(f"[timing] T+{_proc_ms()}ms _process_events entered, starting Runner.run_live (model={cfg.gemini_model})")
            await self._events.send({"type": "bot-ready"})

            # VertexAiSessionService assigns its own IDs — use the pre-created
            # session object directly. For InMemory/File, user_id+session_id is fine.
            if adk_session is not None:
                _run_live_kwargs: dict = {"session": adk_session}
            else:
                _run_live_kwargs = {"user_id": cfg.user_id, "session_id": cfg.session_id}

            # Reconnect loop: transparently retry on WebSocket keepalive drops
            # (Google Vertex AI sends keepalive pings; if the pong round-trip
            # exceeds their timeout they close with 1011. We catch that here
            # and resume via a fresh run_live call with the same session.)
            _MAX_RECONNECTS = 3
            _reconnects = 0
            _is_reconnect = False  # True on 2nd+ iteration — suppress greeting

            _first_audio = True
            while True:
                try:
                    async for event in runner.run_live(
                        live_request_queue=live_queue,
                        run_config=run_config,
                        **_run_live_kwargs,
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
                                        if _first_audio:
                                            _first_audio = False
                                            logger.info(f"[timing] T+{_proc_ms()}ms ✓ FIRST AUDIO FRAME from Gemini (TTFA={_proc_ms()}ms)")
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
                                if not is_partial:
                                    bot_final_emitted = True

                        # ── Gemini interrupted ─────────────────────────────────
                        # MUST be processed BEFORE turn_complete (same event can carry both)
                        if event.interrupted:
                            await self._interrupt_bot("gemini_interrupted")
                            bot_model_buf = ""
                            bot_tx_buf = ""
                            bot_final_emitted = False
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
                                self._last_bot_utterance = final_bot
                                new_turns.append({"role": "model", "content": final_bot})
                                if not bot_final_emitted:
                                    await self._events.send({
                                        "type": "bot-transcription",
                                        "data": {"text": final_bot, "final": True},
                                    })
                                    bot_final_emitted = True

                            await self._on_bot_stopped_speaking()
                            bot_model_buf = ""
                            bot_tx_buf = ""
                            user_buf = ""
                            bot_final_emitted = False
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
                    break  # normal exit — leave the while loop
                except websockets.exceptions.ConnectionClosedError as exc:
                    _reconnects += 1
                    if _reconnects > _MAX_RECONNECTS:
                        _record_session_service_failure("Runner.run_live stream", exc)
                        logger.error(f"[aura] Too many WS reconnects ({_reconnects}), giving up: {exc}")
                        raise
                    logger.warning(
                        f"[aura] Vertex AI WS keepalive drop #{_reconnects}/{_MAX_RECONNECTS} "
                        f"— reconnecting in {_reconnects}s (transparent session resumption)"
                    )
                    await asyncio.sleep(float(_reconnects))

        except Exception as exc:
            _record_session_service_failure("Runner.run_live stream", exc)
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
