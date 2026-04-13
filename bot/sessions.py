"""File-backed session service for Aura.

Wraps InMemorySessionService with transparent JSON persistence so that
interview history survives process restarts without requiring a Vertex AI
Reasoning Engine.

Usage (via env var):
    SESSION_PERSIST_DIR=/tmp/aura-sessions   # any writable directory

Storage layout:
    {persist_dir}/{app_name}/{user_id}/{session_id}.json

The service is a thin delegation wrapper: every in-memory call is forwarded
to InMemorySessionService, and the result is also written to / read from disk.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from google.adk.sessions import InMemorySessionService, Session
from google.adk.events import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    ListSessionsResponse,
    GetSessionConfig,
)
from loguru import logger


def _safe_name(value: str) -> str:
    """Normalize a string to a safe directory/file name component."""
    # Normalize unicode, keep alphanumerics, hyphens, underscores, dots
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[^\w\-.]", "_", value)
    return value[:128] or "default"


class FileSessionService(BaseSessionService):
    """InMemorySessionService + automatic JSON persistence to disk.

    - On start-up: loads all sessions from persist_dir into memory.
    - After create_session / append_event: writes updated session to disk.
    - After delete_session: removes the file from disk.
    """

    def __init__(self, persist_dir: str | Path) -> None:
        self._mem = InMemorySessionService()
        self._root = Path(persist_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        # Load existing sessions synchronously at construction time.
        # We directly inject into the internal sessions dict to avoid running
        # an event loop here (which would conflict when called inside async code).
        self._load_all_sync()
        logger.info(f"[sessions] FileSessionService ready — persist_dir={self._root}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_path(self, app_name: str, user_id: str, session_id: str) -> Path:
        return self._root / _safe_name(app_name) / _safe_name(user_id) / f"{session_id}.json"

    def _write(self, session: Session) -> None:
        path = self._session_path(session.app_name, session.user_id, session.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(session.model_dump_json(), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning(f"[sessions] Failed to persist session {session.id}: {exc}")
            tmp.unlink(missing_ok=True)

    def _delete_file(self, app_name: str, user_id: str, session_id: str) -> None:
        path = self._session_path(app_name, user_id, session_id)
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"[sessions] Failed to remove session file {path}: {exc}")

    def _load_all_sync(self) -> None:
        """Scan persist_dir and load every session JSON directly into the InMemory store.

        Bypasses the async API by injecting Session objects directly into
        InMemorySessionService._mem.sessions, which is a plain nested dict
        {app_name: {user_id: {session_id: Session}}}.
        """
        loaded = 0
        for json_path in self._root.rglob("*.json"):
            try:
                data = json_path.read_text(encoding="utf-8")
                session = Session.model_validate_json(data)
                # Directly inject into the internal sessions dict (no async needed)
                store = self._mem.sessions
                store.setdefault(session.app_name, {}).setdefault(session.user_id, {})[
                    session.id
                ] = session
                loaded += 1
            except Exception as exc:
                logger.warning(f"[sessions] Skipping corrupt session file {json_path}: {exc}")
        if loaded:
            logger.info(f"[sessions] Loaded {loaded} persisted session(s) from {self._root}")

    # ------------------------------------------------------------------
    # BaseSessionService interface
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict | None = None,
        session_id: str | None = None,
    ) -> Session:
        session = await self._mem.create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )
        self._write(session)
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        return await self._mem.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            config=config,
        )

    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: str | None = None,
    ) -> ListSessionsResponse:
        return await self._mem.list_sessions(app_name=app_name, user_id=user_id)

    async def append_event(self, session: Session, event: Event) -> Event:
        result = await self._mem.append_event(session, event)
        # Re-fetch to capture updated state
        updated = await self._mem.get_session(
            app_name=session.app_name,
            user_id=session.user_id,
            session_id=session.id,
        )
        if updated:
            self._write(updated)
        return result

    async def delete_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        await self._mem.delete_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        self._delete_file(app_name, user_id, session_id)
