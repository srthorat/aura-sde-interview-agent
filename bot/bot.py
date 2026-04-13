"""Aura — Google SDE Interview Coach — backend entrypoint.

Stack: Google ADK + Gemini Live + Vertex AI + LiveKit.

Bootstrap flow:
  POST /livekit/session
    → mint user + bot LiveKit tokens
    → spawn AuraVoiceSession task in background
    → return tokens to browser

Session persistence:
  VertexAiSessionService (ADK-native) stores conversation history per user_id
  on Vertex AI Agent Engine. InMemorySessionService is used as fallback for
  local dev without GCP credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from livekit import api
from loguru import logger
from pydantic import BaseModel, Field

from bot.pipelines.voice import (
    AuraRoomConfig,
    build_room_config,
    run_room_bot,
    _get_session_service,
    _bridge_env_for_adk,
    _is_anon_user,
    _vertex_prewarm_for_user,
    _vertex_prewarm_tasks,
    _vertex_session_obj_cache,
)
from bot.agent import select_session_questions

load_dotenv()

# Bridge GOOGLE_VERTEX_CREDENTIALS_PATH → GOOGLE_APPLICATION_CREDENTIALS early
# so that VertexAiSessionService works for all endpoints (not just voice sessions).
_bridge_env_for_adk()

_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
_room_tasks: dict[str, asyncio.Task[None]] = {}

_TRACK_PRESETS: dict[str, list[tuple[str, str]]] = {
    "compressed": [
        ("behavioural", "Behavioural"),
        ("coding", "Coding"),
        ("system_design", "System Design"),
        ("targeted_debrief", "Targeted Debrief"),
    ],
    "advanced": [
        ("googliness", "Googliness (Behavioural)"),
        ("coding_1", "Coding 1 (Algorithms & Data Structures)"),
        ("coding_2", "Coding 2 (Algorithms & Data Structures)"),
        ("system_design", "System Design"),
        ("debugging", "Debugging / Code Review (Practical Engineering)"),
        ("targeted_debrief", "Targeted Debrief"),
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _livekit_url() -> str:
    return _require_env("LIVEKIT_URL")


def _room_prefix() -> str:
    return os.getenv("LIVEKIT_ROOM_PREFIX", "aura-s4").strip() or "aura-s4"


def _normalize_track_preset(track_preset: str = "") -> str:
    value = (track_preset or "compressed").strip().lower().replace("-", "_").replace(" ", "_")
    if value in {"advanced", "google_style", "google", "onsite"}:
        return "advanced"
    return "compressed"


def _load_prompt_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"Required prompt file not found: {path}. "
            "Add the missing prompt file or set BOT_SYSTEM_PROMPT env var."
        )
    return path.read_text().strip()


def _normalize_candidate_name(display_name: str, user_id: str) -> str:
    candidate_name = (display_name or "").strip()
    if not candidate_name or candidate_name.lower() in {"guest", "candidate"}:
        candidate_name = user_id
    if candidate_name.lower().startswith("candidate "):
        candidate_name = candidate_name[10:].strip() or user_id
    candidate_name = candidate_name.replace("_", " ").replace("-", " ").strip()
    if not candidate_name:
        candidate_name = user_id or "Candidate"
    return " ".join(part.capitalize() for part in candidate_name.split())


def _startup_greeting(
    *,
    candidate_name: str,
    is_anon: bool,
    round_hint: str = "",
    difficulty_hint: str = "medium",
    topic_hint: str = "",
) -> str:
    intro = (
        "Hello! I'm Aura, your Google SDE interview coach."
        if is_anon
        else f"Hello {candidate_name}! I'm Aura, your Google SDE interview coach."
    )

    normalized_round = round_hint.strip().lower().replace(" ", "_").replace("-", "_")
    if not normalized_round:
        return (
            f"{intro} Great to have you here. Which round would you like to practice today — "
            "Behavioural, Coding, System Design, or a Targeted Debrief?"
        )

    round_labels = {
        "behavioural": "behavioural",
        "coding": "coding",
        "coding_1": "coding one, focused on algorithms and data structures",
        "coding_2": "coding two, focused on algorithms and data structures",
        "googliness": "googliness, or behavioural",
        "system_design": "system design",
        "debugging": "debugging and code review, focused on practical engineering",
        "targeted_debrief": "targeted debrief",
        "debrief": "targeted debrief",
    }
    round_label = round_labels.get(normalized_round, normalized_round.replace("_", " "))
    difficulty = (difficulty_hint or "medium").strip().lower()
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"
    article = "an" if difficulty[:1] in {"a", "e", "i", "o", "u"} else "a"
    topic = topic_hint.strip().lower()

    if topic:
        return (
            f"{intro} Great to have you here. We'll start with {article} {difficulty} {round_label} round "
            f"focused on {topic}. Let's begin."
        )
    return f"{intro} Great to have you here. We'll start with {article} {difficulty} {round_label} round. Let's begin."


def _system_instruction(
    user_id: str = "anonymous",
    display_name: str = "Guest",
    round_hint: str = "",
    difficulty_hint: str = "medium",
    topic_hint: str = "",
    track_preset: str = "compressed",
) -> str:
    env_override = os.getenv("BOT_SYSTEM_PROMPT", "").strip()
    if env_override:
        return env_override

    prompts_dir = Path(__file__).parent / "prompts"
    is_anon = _is_anon_user(user_id)
    normalized_track = _normalize_track_preset(track_preset)

    greeting_path = prompts_dir / (
        "prompt_greeting_anon.md" if is_anon else "prompt_greeting_named.md"
    )
    base_path = prompts_dir / (
        "system_prompt_anon.md" if is_anon else "system_prompt_named_fast.md"
    )

    candidate_name = _normalize_candidate_name(display_name, user_id)
    greeting = _load_prompt_text(greeting_path).format(
        candidate_name=candidate_name,
        startup_message=_startup_greeting(
            candidate_name=candidate_name,
            is_anon=is_anon,
            round_hint=round_hint,
            difficulty_hint=difficulty_hint,
            topic_hint=topic_hint,
        ),
    )
    base_prompt = _load_prompt_text(base_path)

    prompt_parts = [greeting, base_prompt]

    if normalized_track == "advanced":
        prompt_parts.append(
            "## Interview Track\n\n"
            "Use the advanced 6-round Google-style loop for this candidate: Googliness (Behavioural), Coding 1 (Algorithms & Data Structures), Coding 2 (Algorithms & Data Structures), System Design, Debugging / Code Review (Practical Engineering), then Targeted Debrief."
        )

    if round_hint:
        normalized_round = round_hint.strip().lower().replace(" ", "_").replace("-", "_")
        round_path = prompts_dir / f"prompt_round_{normalized_round}.md"
        if round_path.exists():
            prompt_parts.append(_load_prompt_text(round_path))

    # Pre-select questions and inject a focused bank into the system prompt.
    # For selected rounds, keep five active questions available so Aura can
    # immediately handle "pass", "next question", or "different question"
    # without another retrieval step.
    normalized_round = round_hint.strip().lower().replace(" ", "_").replace("-", "_")
    question_count = 5 if normalized_round else 4
    questions = select_session_questions(round_hint, difficulty_hint, count=question_count, topic=topic_hint)
    if questions:
        numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        prompt_parts.append(
            "## Question Bank — this session only\n\n"
            "These questions have been pre-selected for this session and difficulty. "
            "Ask at most 3 total per round.\n\n"
            "Rules for using the bank:\n"
            "- Ask questions from this bank in order.\n"
            "- If the candidate says pass, next question, skip, or similar, move to the next unused question from the bank.\n"
            "- If the candidate asks for an easier or harder question AND unused questions remain in the bank that better match that difficulty, pick the closest one.\n"
            "- If the candidate switches ROUND TYPE mid-session (e.g. asks for a coding question when the bank contains behavioural questions, or vice versa), DO NOT pick from the wrong-category bank. Instead, generate an appropriate question yourself from your own knowledge matching the requested round type and difficulty. Never mix round categories.\n"
            "- Do NOT call any tool to fetch questions.\n\n"
            + numbered
        )
    if topic_hint.strip():
        prompt_parts.append(
            f"Topic hint for this session: focus on {topic_hint.strip().lower()} when choosing from the question bank."
        )

    # Ultra-Low Latency Directives
    prompt_parts.append(
        "## Sub-Second Response Rules (CRITICAL)\n"
        "To keep the interview feeling natural, you MUST follow these timing rules:\n"
        "1. NO PREAMBLES: If you are asking a question, make the question the VERY FIRST WORDS out of your mouth. No \"Okay, moving on,\" or \"Here is your question:\". Just ask it.\n"
        "2. SPEAK BEFORE TOOLS: If you need to use `record_answer_note` or `submit_rubric_grade`, you MUST physically speak an acknowledgment to the candidate FIRST (e.g. \"Great point.\") so they hear audio instantly. The tool call must be the LAST thing you do in your turn, not the first.\n"
        "3. PAUSE TOLERANCE: If the user pauses mid-sentence (silence), give them a tiny moment. Acknowledge short pauses gracefully without cutting them off."
    )

    return "\n\n".join(part for part in prompt_parts if part)


def _mint_token(
    *,
    room_name: str,
    identity: str,
    name: str,
    metadata: dict[str, Any],
    hidden: bool = False,
) -> str:
    token = (
        api.AccessToken(_require_env("LIVEKIT_API_KEY"), _require_env("LIVEKIT_API_SECRET"))
        .with_identity(identity)
        .with_name(name)
        .with_metadata(json.dumps(metadata))
        .with_ttl(timedelta(minutes=int(os.getenv("LIVEKIT_TOKEN_TTL_MINUTES", "30"))))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                hidden=hidden,
            )
        )
    )
    return token.to_jwt()


def _generate_room_name() -> str:
    return f"{_room_prefix()}-{uuid4().hex[:10]}"


def _launch_room_bot(
    *,
    room_name: str,
    user_id: str = "anonymous",
    display_name: str = "Guest",
    round_hint: str = "",
    difficulty_hint: str = "medium",
    topic_hint: str = "",
    track_preset: str = "compressed",
) -> None:
    existing = _room_tasks.get(room_name)
    if existing and not existing.done():
        return

    bot_identity = f"bot-{room_name}"
    bot_token = _mint_token(
        room_name=room_name,
        identity=bot_identity,
        name="Aura",
        metadata={"role": "bot", "room": room_name},
        hidden=False,
    )

    config = build_room_config(
        livekit_url=_livekit_url(),
        room_name=room_name,
        token=bot_token,
        system_instruction=_system_instruction(user_id, display_name, round_hint, difficulty_hint, topic_hint, track_preset),
        user_id=user_id,
    )

    task = asyncio.create_task(run_room_bot(config), name=f"room-bot:{room_name}")
    _room_tasks[room_name] = task

    def _cleanup(done_task: asyncio.Task[None]) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.info(f"Bot task cancelled for room {room_name}")
        except Exception:
            logger.exception(f"Bot task failed for room {room_name}")
        finally:
            _room_tasks.pop(room_name, None)

    task.add_done_callback(_cleanup)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Aura — Google SDE Interview Coach backend starting")
    yield

    tasks = list(_room_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _room_tasks.clear()
    logger.info("Aura backend shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Aura Voice Agent - Solution 4", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class SessionBootstrapRequest(BaseModel):
    room_name: str | None = None
    display_name: str | None = Field(default=None, max_length=80)
    user_id: str | None = Field(default=None, max_length=64)
    track_preset: str | None = Field(default="compressed", max_length=20)
    round_hint: str | None = Field(default=None, max_length=40)
    difficulty_hint: str | None = Field(default=None, max_length=10)
    topic_hint: str | None = Field(default=None, max_length=40)


class SessionBootstrapResponse(BaseModel):
    livekit_url: str
    room_name: str
    participant_identity: str
    participant_name: str
    access_token: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/livekit/session", response_model=SessionBootstrapResponse)
@app.post("/api/livekit/session", response_model=SessionBootstrapResponse)
async def create_livekit_session(req: SessionBootstrapRequest):
    room_name = req.room_name or _generate_room_name()
    participant_identity = f"web-{uuid4().hex[:8]}"
    participant_name = (req.display_name or "Guest").strip() or "Guest"
    user_id = (req.user_id or "anonymous").strip() or "anonymous"
    is_anon = _is_anon_user(user_id)
    track_preset = _normalize_track_preset(req.track_preset or "compressed")
    round_hint = (req.round_hint or "").strip()
    difficulty_hint = (req.difficulty_hint or "").strip()
    topic_hint = (req.topic_hint or "").strip()

    if not is_anon:
        if not round_hint:
            raise HTTPException(status_code=400, detail="round_hint is required for named candidate sessions")
        if not difficulty_hint:
            raise HTTPException(status_code=400, detail="difficulty_hint is required for named candidate sessions")

    if not difficulty_hint:
        difficulty_hint = "medium"

    access_token = _mint_token(
        room_name=room_name,
        identity=participant_identity,
        name=participant_name,
        metadata={"role": "user", "room": room_name, "user_id": user_id},
    )

    _launch_room_bot(
        room_name=room_name,
        user_id=user_id,
        display_name=participant_name,
        track_preset=track_preset,
        round_hint=round_hint,
        difficulty_hint=difficulty_hint,
        topic_hint=topic_hint,
    )

    return SessionBootstrapResponse(
        livekit_url=_livekit_url(),
        room_name=room_name,
        participant_identity=participant_identity,
        participant_name=participant_name,
        access_token=access_token,
    )


@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    active_room_count = sum(1 for task in _room_tasks.values() if not task.done())
    return {
        "status": "ok",
        "bot": "Aura",
        "transport": "livekit",
        "model": os.getenv("GEMINI_LIVE_MODEL", "gemini-live-2.5-flash-native-audio"),
        "active_rooms": active_room_count,
    }


@app.get("/api/summary/{room_name}")
async def get_room_summary(room_name: str) -> dict[str, Any]:
    """HTTP fallback for call-summary when the data channel closed before delivery."""
    from bot.pipelines.voice import _room_summaries
    data = _room_summaries.pop(room_name, None)
    if data is None:
        return {"status": "pending"}
    return {"status": "ready", "data": data}


@app.get("/api/candidate/check")
async def check_candidate(user_id: str = "") -> dict[str, Any]:
    """Check whether a candidate ID has existing session history.

    Returns:
        exists:  True if prior sessions found
        rounds:  Count of prior sessions (proxy for rounds completed)
        user_id: Echoed back (sanitised)
    """
    uid = user_id.strip().lower()[:64]
    if not uid or uid == "anonymous":
        return {"exists": False, "rounds": 0, "user_id": uid}

    svc = _get_session_service()
    engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
    project   = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
    location  = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if engine_id and project:
        app_name = f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
    else:
        app_name = "aura"

    # Pre-warm: always create a fresh Vertex session in background so it's
    # ready by the time the user clicks Join.  Skip only if a task is already
    # running OR a fresh session object is already waiting in the cache.
    if (
        engine_id and project
        and not _is_anon_user(uid)
        and uid not in _vertex_prewarm_tasks
        and uid not in _vertex_session_obj_cache
    ):
        task = asyncio.create_task(_vertex_prewarm_for_user(svc, app_name, uid))
        _vertex_prewarm_tasks[uid] = task
        logger.info(f"[candidate/check] Vertex pre-warm task started for {uid!r}")

    try:
        result = await svc.list_sessions(app_name=app_name, user_id=uid)
        sessions = getattr(result, "sessions", []) or []
        return {"exists": len(sessions) > 0, "rounds": len(sessions), "user_id": uid}
    except Exception as exc:
        logger.debug(f"[candidate/check] lookup failed for {uid!r}: {exc}")
        return {"exists": False, "rounds": 0, "user_id": uid}


@app.get("/api/prewarm")
@app.get("/prewarm")
async def prewarm_candidate(user_id: str = "") -> dict[str, Any]:
    """Fire a Vertex AI session pre-warm for a named candidate and return immediately.

    Call this as early as possible (e.g. on input change) so the session object
    is already cached by the time the candidate clicks Join.  Returns instantly
    (<5 ms) — the pre-warm runs as a background task.
    """
    uid = user_id.strip().lower()[:64]
    if not uid or _is_anon_user(uid):
        return {"status": "skipped", "reason": "anonymous or empty user_id"}

    engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID", "").strip()
    project   = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "")
    location  = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

    if not (engine_id and project):
        return {"status": "skipped", "reason": "Vertex AI not configured"}

    if uid in _vertex_prewarm_tasks or uid in _vertex_session_obj_cache:
        return {"status": "already_running", "user_id": uid}

    app_name = f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
    svc = _get_session_service()
    task = asyncio.create_task(_vertex_prewarm_for_user(svc, app_name, uid))
    _vertex_prewarm_tasks[uid] = task
    logger.info(f"[prewarm] Pre-warm task started for {uid!r}")
    return {"status": "started", "user_id": uid}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    uvicorn.run(
        "bot.bot:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "7862")),
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
