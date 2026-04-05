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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from livekit import api
from loguru import logger
from pydantic import BaseModel, Field

from bot.pipelines.voice import AuraRoomConfig, build_room_config, run_room_bot

load_dotenv()

_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
_room_tasks: dict[str, asyncio.Task[None]] = {}


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


def _system_instruction() -> str:
    env_override = os.getenv("BOT_SYSTEM_PROMPT", "").strip()
    if env_override:
        return env_override
    prompts_dir = Path(__file__).parent / "prompts"
    system_prompt_file = prompts_dir / "system_prompt.md"
    if not system_prompt_file.exists():
        raise FileNotFoundError(
            f"Required prompt file not found: {system_prompt_file}. "
            "Add bot/prompts/system_prompt.md or set BOT_SYSTEM_PROMPT env var."
        )
    return system_prompt_file.read_text().strip()


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


def _launch_room_bot(*, room_name: str, user_id: str = "anonymous") -> None:
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
        system_instruction=_system_instruction(),
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

    access_token = _mint_token(
        room_name=room_name,
        identity=participant_identity,
        name=participant_name,
        metadata={"role": "user", "room": room_name, "user_id": user_id},
    )

    _launch_room_bot(room_name=room_name, user_id=user_id)

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
