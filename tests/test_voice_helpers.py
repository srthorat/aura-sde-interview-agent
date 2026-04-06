from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest


def _import_module():
    import bot.pipelines.voice as voice_module

    return importlib.reload(voice_module)


def _make_event(role: str | None, *parts: str):
    return SimpleNamespace(
        content=SimpleNamespace(
            role=role,
            parts=[SimpleNamespace(text=text) for text in parts],
        )
    )


def test_voice_env_helpers_and_room_config(monkeypatch):
    voice_module = _import_module()

    monkeypatch.setenv("BOOL_ON", "yes")
    monkeypatch.setenv("BOOL_OFF", "false")
    monkeypatch.setenv("INTERRUPTION_MIN_WORDS", "0")

    assert voice_module._env_flag("BOOL_ON") is True
    assert voice_module._env_flag("BOOL_OFF") is False
    assert voice_module._env_flag("MISSING", "off") is False

    cfg = voice_module.build_room_config(
        livekit_url="wss://lk",
        room_name="room-1",
        token="token",
        system_instruction="prompt",
    )
    assert cfg.livekit_url == "wss://lk"
    assert cfg.user_id == "anonymous"
    assert cfg.allow_interruptions is True
    assert cfg.interruption_min_words == 1

    cfg2 = voice_module.build_room_config(
        livekit_url="wss://lk",
        room_name="room-1",
        token="token",
        system_instruction="prompt",
        user_id="alice",
        allow_interruptions=False,
        interruption_min_words=7,
    )
    assert cfg2.user_id == "alice"
    assert cfg2.allow_interruptions is False
    assert cfg2.interruption_min_words == 7


def test_bridge_env_for_adk_and_ensure(monkeypatch, tmp_path):
    voice_module = _import_module()

    for key in [
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_VERTEX_CREDENTIALS",
        "GOOGLE_VERTEX_CREDENTIALS_PATH",
        "GOOGLE_CLOUD_PROJECT_ID",
        "GEMINI_MODEL",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("GEMINI_MODEL", "preview")
    voice_module._bridge_env_for_adk()
    assert "GOOGLE_GENAI_USE_VERTEXAI" not in voice_module.os.environ

    monkeypatch.setenv("GEMINI_MODEL", "ga")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    voice_module._bridge_env_for_adk()
    assert voice_module.os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert "GOOGLE_CLOUD_PROJECT" not in voice_module.os.environ

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "project-1")
    monkeypatch.setenv("GOOGLE_VERTEX_CREDENTIALS_PATH", "/tmp/key.json")
    voice_module._bridge_env_for_adk()
    assert voice_module.os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert voice_module.os.environ["GOOGLE_CLOUD_PROJECT"] == "project-1"
    assert voice_module.os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/tmp/key.json"

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_VERTEX_CREDENTIALS_PATH", raising=False)
    creds_path = tmp_path / "creds.json"

    def fake_mkstemp(*_args, **_kwargs):
        return voice_module.os.open(creds_path, voice_module.os.O_CREAT | voice_module.os.O_RDWR), str(creds_path)

    monkeypatch.setenv("GOOGLE_VERTEX_CREDENTIALS", json.dumps({"client_email": "a@b", "private_key": "x"}))
    monkeypatch.setattr(__import__("tempfile"), "mkstemp", fake_mkstemp)
    voice_module._bridge_env_for_adk()
    assert voice_module.os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(creds_path)
    assert creds_path.read_text() != ""

    calls = []
    voice_module._bridge_env_done = False
    monkeypatch.setattr(voice_module, "_bridge_env_for_adk", lambda: calls.append("called"))
    voice_module._ensure_env_for_adk()
    voice_module._ensure_env_for_adk()
    assert calls == ["called"]


def test_vertex_credentials_and_genai_client(monkeypatch):
    voice_module = _import_module()

    monkeypatch.setattr(
        voice_module.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: ("info", info, scopes),
    )
    monkeypatch.setattr(
        voice_module.service_account.Credentials,
        "from_service_account_file",
        lambda path, scopes: ("file", path, scopes),
    )

    monkeypatch.setenv("GOOGLE_VERTEX_CREDENTIALS", json.dumps({"project_id": "p"}))
    assert voice_module._vertex_credentials()[0] == "info"
    monkeypatch.delenv("GOOGLE_VERTEX_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_VERTEX_CREDENTIALS_PATH", "/tmp/file.json")
    assert voice_module._vertex_credentials()[0] == "file"
    monkeypatch.delenv("GOOGLE_VERTEX_CREDENTIALS_PATH", raising=False)
    assert voice_module._vertex_credentials() is None

    created = []
    monkeypatch.setattr(voice_module.genai, "Client", lambda **kwargs: created.append(kwargs) or kwargs)
    monkeypatch.setattr(voice_module, "_vertex_credentials", lambda: "creds")

    monkeypatch.setenv("GEMINI_MODEL", "preview")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError):
        voice_module._build_genai_client()

    monkeypatch.setenv("GOOGLE_API_KEY", "api-key")
    preview_client = voice_module._build_genai_client()
    assert preview_client == {"api_key": "api-key"}

    monkeypatch.setenv("GEMINI_MODEL", "ga")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    with pytest.raises(ValueError):
        voice_module._build_genai_client()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "project-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west1")
    ga_client = voice_module._build_genai_client()
    assert ga_client == {
        "vertexai": True,
        "project": "project-1",
        "location": "europe-west1",
        "credentials": "creds",
    }
    assert len(created) == 2

def test_gemini_text_model_default_and_override(monkeypatch):
    voice_module = _import_module()

    monkeypatch.delenv("GEMINI_TEXT_MODEL", raising=False)
    assert voice_module._gemini_text_model() == "gemini-2.5-flash"

    monkeypatch.setenv("GEMINI_TEXT_MODEL", "gemini-1.5-flash")
    assert voice_module._gemini_text_model() == "gemini-1.5-flash"

    monkeypatch.setenv("GEMINI_TEXT_MODEL", "   ")
    assert voice_module._gemini_text_model() == "gemini-2.5-flash"


def test_session_service_singleton_and_history(monkeypatch):
    voice_module = _import_module()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "project-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "asia-south1")
    monkeypatch.setenv("VERTEX_AI_REASONING_ENGINE_ID", "engine-1")

    monkeypatch.setattr(voice_module, "VertexAiSessionService", lambda **kwargs: ("vertex", kwargs))
    monkeypatch.setattr(voice_module, "InMemorySessionService", lambda: "memory")
    assert voice_module._build_session_service() == ("vertex", {"project": "project-1", "location": "asia-south1"})

    monkeypatch.setattr(voice_module, "VertexAiSessionService", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert voice_module._build_session_service() == "memory"

    monkeypatch.delenv("VERTEX_AI_REASONING_ENGINE_ID", raising=False)
    assert voice_module._build_session_service() == "memory"

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    assert voice_module._build_session_service() == "memory"

    voice_module._session_service = None
    voice_module._session_service_failures = 99
    monkeypatch.setattr(voice_module, "_build_session_service", lambda: "svc")
    assert voice_module._get_session_service() == "svc"
    assert voice_module._session_service_failures == 0
    assert voice_module._get_session_service() == "svc"
    voice_module._reset_session_service()
    assert voice_module._session_service is None

    history = [
        _make_event("user", "hello"),
        _make_event("model", "hi there"),
        SimpleNamespace(content=None),
        SimpleNamespace(content=SimpleNamespace(role="user", parts=[SimpleNamespace(not_text="x")])),
    ]
    context = voice_module._history_to_context(history)
    assert context == "Recent conversation history:\nUSER: hello\nMODEL: hi there"
    assert voice_module._history_to_context([]) == ""

    long_history = [_make_event("user", "x" * 2100)]
    assert voice_module._history_to_context(long_history) == ""

    broken_history = [SimpleNamespace(content=SimpleNamespace(role="user", parts=None))]
    assert voice_module._history_to_context(broken_history) == ""


@pytest.mark.asyncio
async def test_event_and_session_helpers(monkeypatch):
    voice_module = _import_module()

    publish_calls = []

    class Room:
        local_participant = SimpleNamespace(
            publish_data=lambda payload, reliable, topic: publish_calls.append((payload, reliable, topic))
        )

    async def fake_publish_data(payload, reliable, topic):
        publish_calls.append((payload, reliable, topic))

    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=fake_publish_data))
    sender = voice_module._Events(room)
    await sender.send({"type": "status", "ok": True})
    assert json.loads(publish_calls[0][0].decode()) == {"type": "status", "ok": True}

    async def broken_publish_data(*_args, **_kwargs):
        raise RuntimeError("fail")

    room.local_participant.publish_data = broken_publish_data
    await sender.send({"type": "broken"})

    agent = SimpleNamespace(name="agent-app")
    existing_session = SimpleNamespace(id="s-1", events=[_make_event("user", "hi")])

    class SessionService:
        async def list_sessions(self, **kwargs):
            self.last_list = kwargs
            return SimpleNamespace(sessions=[SimpleNamespace(id="s-1")])

        async def get_session(self, **kwargs):
            self.last_get = kwargs
            return existing_session

        async def create_session(self, **kwargs):
            self.last_create = kwargs
            return SimpleNamespace(id="created", events=[])

        async def append_event(self, **kwargs):
            self.last_append = kwargs

    service = SessionService()
    monkeypatch.setenv("VERTEX_AI_REASONING_ENGINE_ID", "engine-1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "project-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    session, context = await voice_module._load_adk_session(service, agent, "alice")
    assert session is existing_session
    assert "USER: hi" in context

    class FailingSessionService(SessionService):
        async def list_sessions(self, **kwargs):
            raise RuntimeError("boom")

    failing = FailingSessionService()
    session, context = await voice_module._load_adk_session(failing, agent, "bob")
    assert session.id == "created"
    assert context == ""

    class EmptySessionService(SessionService):
        async def list_sessions(self, **kwargs):
            self.last_list = kwargs
            return SimpleNamespace(sessions=[])

    monkeypatch.delenv("VERTEX_AI_REASONING_ENGINE_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    empty = EmptySessionService()
    created_session, created_context = await voice_module._load_adk_session(empty, agent, "carol")
    assert created_session.id == "created"
    assert created_context == ""
    assert empty.last_create == {"app_name": "agent-app", "user_id": "carol"}

    await voice_module._append_turn(service, agent, existing_session, "user", "hello")
    assert service.last_append["session"] is existing_session
    assert service.last_append["event"].author == "user"

    async def broken_append_event(**_kwargs):
        raise RuntimeError("fail")

    service.append_event = broken_append_event
    await voice_module._append_turn(service, agent, existing_session, "user", "hello")


@pytest.mark.asyncio
async def test_aura_voice_session_core_methods(monkeypatch):
    voice_module = _import_module()

    class FakeRoom:
        def __init__(self):
            self.local_participant = SimpleNamespace()

    monkeypatch.setattr(voice_module.rtc, "Room", FakeRoom)
    monkeypatch.setattr(voice_module, "_build_genai_client", lambda: "client")
    monotonic_values = iter([10.0, 20.0, 21.5])

    def fake_monotonic():
        return next(monotonic_values, 21.5)

    monkeypatch.setattr(voice_module.time, "monotonic", fake_monotonic)

    cfg = voice_module.AuraRoomConfig(
        livekit_url="wss://lk",
        room_name="room-1",
        token="token",
        system_instruction="prompt",
    )
    session = voice_module.AuraVoiceSession(cfg)
    sent = []

    async def send_event(event):
        sent.append(event)

    cleared = []
    session._events = SimpleNamespace(send=send_event)
    session._audio_source = SimpleNamespace(clear_queue=lambda: cleared.append(True))

    await session._on_user_started_speaking()
    assert session._user_speaking is True
    assert sent == [{"type": "user-started-speaking"}]

    session._bot_speaking = True
    session._config.allow_interruptions = False
    await session._on_user_started_speaking()
    assert session._bot_speaking is True
    session._config.allow_interruptions = True

    session._user_speaking = False
    await session._on_user_stopped_speaking()
    assert session._user_stopped_speaking_at is None

    session._user_speaking = True
    await session._on_user_stopped_speaking()
    assert session._user_speaking is False

    session._user_speaking = False
    session._bot_speaking = True
    session._audio_source = None
    await session._on_user_started_speaking()
    assert session._bot_speaking is False

    session._user_speaking = False
    session._bot_speaking = True
    session._audio_source = SimpleNamespace(clear_queue=lambda: cleared.append(True))
    await session._on_user_started_speaking()
    assert cleared == [True]
    assert sent[-2:] == [{"type": "bot-stopped-speaking"}, {"type": "interruption"}]

    session._pause_requested = True
    await session._on_user_stopped_speaking()
    assert session._user_speaking is False
    assert session._pause_requested is False

    session._user_stopped_speaking_at = 20.0
    session._interrupted = False
    session._bot_speaking = False
    await session._on_bot_started_speaking()
    assert session._bot_speaking is True
    assert sent[-2:] == [{"type": "bot-started-speaking"}, {"type": "latency", "data": {"total_ms": 1500}}]

    session._bot_speaking = False
    session._user_stopped_speaking_at = None
    await session._on_bot_started_speaking()
    assert sent[-1] == {"type": "bot-started-speaking"}

    await session._on_bot_started_speaking()
    assert sent[-1] == {"type": "bot-started-speaking"}

    await session._on_bot_stopped_speaking()
    assert session._bot_speaking is False
    assert sent[-1] == {"type": "bot-stopped-speaking"}

    await session._on_bot_stopped_speaking()
    assert sent[-1] == {"type": "bot-stopped-speaking"}

    assert session._is_output_blocked() is False
    session._interrupted = True
    assert session._is_output_blocked() is True
    session._interrupted = False
    session._pause_requested = True
    assert session._is_output_blocked() is True
    session._pause_requested = False
    session._user_speaking = True
    assert session._is_output_blocked() is True

    session._user_speaking = False
    session._bot_speaking = False
    session._interrupted = False
    await session._interrupt_bot("already-stopped")
    assert session._interrupted is False

    session._audio_source = None
    session._bot_speaking = True
    await session._interrupt_bot("barge-in")
    assert session._interrupted is True
    assert session._bot_speaking is False
    assert sent[-2:] == [{"type": "bot-stopped-speaking"}, {"type": "interruption"}]