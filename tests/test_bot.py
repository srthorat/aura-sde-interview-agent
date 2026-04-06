from __future__ import annotations

import asyncio
import importlib
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_bot_module():
    import bot.bot as bot_module

    return importlib.reload(bot_module)


@pytest.fixture(autouse=True)
def clean_room_tasks():
    bot_module = _import_bot_module()
    bot_module._room_tasks.clear()
    yield
    bot_module._room_tasks.clear()


def test_helpers_and_prompt_loading(monkeypatch, tmp_path):
    bot_module = _import_bot_module()

    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    assert bot_module._require_env("LIVEKIT_URL") == "wss://example.livekit.cloud"
    assert bot_module._livekit_url() == "wss://example.livekit.cloud"

    monkeypatch.delenv("MISSING_ENV", raising=False)
    with pytest.raises(RuntimeError):
        bot_module._require_env("MISSING_ENV")

    monkeypatch.delenv("LIVEKIT_ROOM_PREFIX", raising=False)
    assert bot_module._room_prefix() == "aura-s4"
    monkeypatch.setenv("LIVEKIT_ROOM_PREFIX", " custom-prefix ")
    assert bot_module._room_prefix() == "custom-prefix"
    monkeypatch.setenv("LIVEKIT_ROOM_PREFIX", "   ")
    assert bot_module._room_prefix() == "aura-s4"

    monkeypatch.setenv("BOT_SYSTEM_PROMPT", "override prompt")
    assert bot_module._system_instruction() == "override prompt"

    monkeypatch.delenv("BOT_SYSTEM_PROMPT", raising=False)
    prompts_dir = Path(bot_module.__file__).parent / "prompts"
    system_prompt_path = prompts_dir / "system_prompt.md"
    actual_prompt = system_prompt_path.read_text().strip()
    assert bot_module._system_instruction() == actual_prompt

    real_exists = Path.exists

    def fake_exists(path_obj):
        if path_obj == system_prompt_path:
            return False
        return real_exists(path_obj)

    monkeypatch.setattr(Path, "exists", fake_exists)
    with pytest.raises(FileNotFoundError):
        bot_module._system_instruction()


def test_generate_room_name_and_main(monkeypatch):
    bot_module = _import_bot_module()

    monkeypatch.setattr(bot_module, "uuid4", lambda: SimpleNamespace(hex="abc123456789"))
    monkeypatch.setenv("LIVEKIT_ROOM_PREFIX", "aura-demo")
    assert bot_module._generate_room_name() == "aura-demo-abc1234567"

    captured = {}
    monkeypatch.setattr(bot_module.uvicorn, "run", lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs}))
    monkeypatch.setenv("PORT", "9999")
    bot_module.main()
    assert captured["args"] == ("bot.bot:app",)
    assert captured["kwargs"]["port"] == 9999


def test_mint_token_and_launch_room_bot(monkeypatch):
    bot_module = _import_bot_module()

    class FakeToken:
        def __init__(self, key, secret):
            self.values = {"key": key, "secret": secret}

        def with_identity(self, value):
            self.values["identity"] = value
            return self

        def with_name(self, value):
            self.values["name"] = value
            return self

        def with_metadata(self, value):
            self.values["metadata"] = value
            return self

        def with_ttl(self, value):
            self.values["ttl"] = value
            return self

        def with_grants(self, value):
            self.values["grants"] = value
            return self

        def to_jwt(self):
            return "jwt-token"

    monkeypatch.setenv("LIVEKIT_API_KEY", "lk-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "lk-secret")
    monkeypatch.setenv("LIVEKIT_TOKEN_TTL_MINUTES", "45")
    monkeypatch.setattr(bot_module.api, "AccessToken", FakeToken)
    monkeypatch.setattr(bot_module.api, "VideoGrants", lambda **kwargs: kwargs)

    jwt_token = bot_module._mint_token(
        room_name="room-1",
        identity="user-1",
        name="User One",
        metadata={"role": "user"},
        hidden=True,
    )
    assert jwt_token == "jwt-token"

    created_tasks = []

    class FakeTask:
        def __init__(self):
            self.callback = None

        def done(self):
            return False

        def add_done_callback(self, callback):
            self.callback = callback

        def result(self):
            return None

        def cancel(self):
            return None

    monkeypatch.setattr(bot_module, "_mint_token", lambda **kwargs: "bot-jwt")
    monkeypatch.setattr(bot_module, "_livekit_url", lambda: "wss://lk")
    monkeypatch.setattr(bot_module, "_system_instruction", lambda: "prompt")
    monkeypatch.setattr(
        bot_module,
        "build_room_config",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    async def fake_run_room_bot(config):
        return None

    monkeypatch.setattr(bot_module, "run_room_bot", fake_run_room_bot)

    def fake_create_task(coro, name=None):
        created_tasks.append((coro, name))
        coro.close()
        return FakeTask()

    monkeypatch.setattr(bot_module.asyncio, "create_task", fake_create_task)
    bot_module._launch_room_bot(room_name="room-a", user_id="candidate-1")
    assert "room-a" in bot_module._room_tasks
    assert created_tasks[0][1] == "room-bot:room-a"

    launched_task = bot_module._room_tasks["room-a"]

    class CancelledDoneTask:
        def result(self):
            raise asyncio.CancelledError()

    launched_task.callback(CancelledDoneTask())
    assert "room-a" not in bot_module._room_tasks

    bot_module._launch_room_bot(room_name="room-b", user_id="candidate-2")
    failing_task = bot_module._room_tasks["room-b"]

    class ErrorDoneTask:
        def result(self):
            raise RuntimeError("task failed")

    failing_task.callback(ErrorDoneTask())
    assert "room-b" not in bot_module._room_tasks

    existing_count = len(created_tasks)
    bot_module._room_tasks["room-a"] = FakeTask()
    bot_module._launch_room_bot(room_name="room-a", user_id="candidate-1")
    assert len(created_tasks) == existing_count


@pytest.mark.asyncio
async def test_lifespan_health_summary_and_candidate_check(monkeypatch):
    bot_module = _import_bot_module()

    class PendingTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def done(self):
            return False

    class DoneTask:
        def cancel(self):
            return None

        def done(self):
            return True

    pending = PendingTask()
    done = DoneTask()
    bot_module._room_tasks["active"] = pending
    bot_module._room_tasks["done"] = done

    gathered = {}

    async def fake_gather(*tasks, **kwargs):
        gathered["tasks"] = tasks
        gathered["kwargs"] = kwargs
        return []

    monkeypatch.setattr(bot_module.asyncio, "gather", fake_gather)

    async with bot_module.lifespan(bot_module.app):
        health = await bot_module.health()
        assert health["active_rooms"] == 1
        assert health["bot"] == "Aura"

    assert pending.cancelled is True
    assert gathered["kwargs"]["return_exceptions"] is True
    assert bot_module._room_tasks == {}

    import bot.pipelines.voice as voice_module

    voice_module._room_summaries.clear()
    assert await bot_module.get_room_summary("missing-room") == {"status": "pending"}
    voice_module._room_summaries["room-1"] = {"score": 5}
    assert await bot_module.get_room_summary("room-1") == {"status": "ready", "data": {"score": 5}}
    assert "room-1" not in voice_module._room_summaries

    assert await bot_module.check_candidate("") == {"exists": False, "rounds": 0, "user_id": ""}
    assert await bot_module.check_candidate("anonymous") == {"exists": False, "rounds": 0, "user_id": "anonymous"}

    class FakeService:
        async def list_sessions(self, app_name, user_id):
            return SimpleNamespace(sessions=[SimpleNamespace(id="1"), SimpleNamespace(id="2")])

    monkeypatch.setattr(bot_module, "_get_session_service", lambda: FakeService())
    monkeypatch.setenv("VERTEX_AI_REASONING_ENGINE_ID", "engine-1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "asia-south1")
    result = await bot_module.check_candidate("  Alice  ")
    assert result == {"exists": True, "rounds": 2, "user_id": "alice"}

    monkeypatch.delenv("VERTEX_AI_REASONING_ENGINE_ID", raising=False)
    fallback = await bot_module.check_candidate("Bob")
    assert fallback == {"exists": True, "rounds": 2, "user_id": "bob"}

    class BrokenService:
        async def list_sessions(self, app_name, user_id):
            raise RuntimeError("lookup failed")

    monkeypatch.setattr(bot_module, "_get_session_service", lambda: BrokenService())
    broken = await bot_module.check_candidate("Charlie")
    assert broken == {"exists": False, "rounds": 0, "user_id": "charlie"}


@pytest.mark.asyncio
async def test_lifespan_with_no_tasks(monkeypatch):
    bot_module = _import_bot_module()
    bot_module._room_tasks.clear()

    called = {"gathered": False}

    async def fake_gather(*tasks, **kwargs):
        called["gathered"] = True
        return []

    monkeypatch.setattr(bot_module.asyncio, "gather", fake_gather)

    async with bot_module.lifespan(bot_module.app):
        pass

    assert called["gathered"] is False


@pytest.mark.asyncio
async def test_create_livekit_session_route(monkeypatch):
    bot_module = _import_bot_module()

    monkeypatch.setattr(bot_module, "_generate_room_name", lambda: "generated-room")
    monkeypatch.setattr(bot_module, "uuid4", lambda: SimpleNamespace(hex="cafebabedeadbeef"))
    monkeypatch.setattr(bot_module, "_mint_token", lambda **kwargs: "user-jwt")
    launched = {}
    monkeypatch.setattr(bot_module, "_launch_room_bot", lambda **kwargs: launched.update(kwargs))
    monkeypatch.setattr(bot_module, "_livekit_url", lambda: "wss://livekit.test")

    req = bot_module.SessionBootstrapRequest(display_name="  Jane  ", user_id="  candidate-7  ")
    response = await bot_module.create_livekit_session(req)
    assert response.room_name == "generated-room"
    assert response.participant_identity == "web-cafebabe"
    assert response.participant_name == "Jane"
    assert response.access_token == "user-jwt"
    assert launched == {"room_name": "generated-room", "user_id": "candidate-7"}

    req2 = bot_module.SessionBootstrapRequest(room_name="manual-room", display_name="   ", user_id="   ")
    response2 = await bot_module.create_livekit_session(req2)
    assert response2.room_name == "manual-room"
    assert response2.participant_name == "Guest"


def test_static_mount_and_main_block(monkeypatch):
    dist_dir = Path("/home/ubuntu/velox/aura-sde-interview-agent/frontend/dist")
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    import sys
    sys.modules.pop("bot.bot", None)
    bot_module = _import_bot_module()

    assert any(getattr(route, "name", None) == "frontend" for route in bot_module.app.routes)

    import uvicorn

    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: called.update({"args": args, "kwargs": kwargs}))
    monkeypatch.setenv("PORT", "8123")
    runpy.run_module("bot.bot", run_name="__main__")
    assert called["args"] == ("bot.bot:app",)
    assert called["kwargs"]["port"] == 8123


def test_import_without_frontend_dist_skips_mount(monkeypatch):
    import sys

    real_exists = Path.exists

    def fake_exists(path_obj):
        if str(path_obj).endswith("frontend/dist"):
            return False
        return real_exists(path_obj)

    monkeypatch.setattr(Path, "exists", fake_exists)
    sys.modules.pop("bot.bot", None)
    bot_module = _import_bot_module()

    assert not any(getattr(route, "name", None) == "frontend" for route in bot_module.app.routes)
