from __future__ import annotations

import importlib

import pytest

import bot.agent as agent


@pytest.fixture(autouse=True)
def reset_agent_state(monkeypatch):
    agent._sessions.clear()
    agent._tool_loop_tracker.clear()
    agent._session_id_context = ""
    monkeypatch.setattr(agent.random, "choice", lambda items: items[0])
    yield
    agent._sessions.clear()
    agent._tool_loop_tracker.clear()
    agent._session_id_context = ""


def test_session_state_lifecycle_and_summary():
    agent.create_session_state("s1")
    agent._session_id_context = "s1"
    state = agent._get_state("s1")
    assert state.asked == []
    assert agent.get_session_summary() == {
        "summary": "No questions have been asked yet in this session.",
    }

    state.asked.extend(["Question one", "Question two"])
    summary = agent.get_session_summary()
    assert summary["questions_asked"] == "2"
    assert "2 questions covered" in summary["summary"]

    agent.destroy_session_state("s1")
    assert "s1" not in agent._sessions


def test_get_state_creates_missing_session_lazily():
    state = agent._get_state("missing")
    assert state.asked == []
    assert "missing" in agent._sessions


def test_get_current_time_shape():
    result = agent.get_current_time()
    assert result.keys() == {"time", "date"}
    assert "UTC" in result["time"]


def test_get_interview_question_uses_topic_and_difficulty():
    agent.create_session_state("topic-session")
    agent._session_id_context = "topic-session"

    result = agent.get_interview_question(
        round_number=2,
        topic="stack",
        difficulty="easy",
    )

    assert result["category"] == "coding"
    assert result["round"] == "2"
    assert result["topic"] == "stack"
    assert result["difficulty"] == "easy"
    assert "instruction" in result
    assert result["question"] in agent._QUESTIONS_BY_TOPIC["stack"]["easy"]


def test_get_interview_question_combines_difficulties_and_falls_back_when_topic_unknown():
    agent.create_session_state("fallback-session")
    agent._session_id_context = "fallback-session"

    combined = agent.get_interview_question(round_number=2, topic="queue", difficulty="")
    assert combined["topic"] == "queue"
    assert combined["difficulty"] == "any"

    fallback = agent.get_interview_question(round_number=99, topic="unknown-topic", category="")
    assert fallback["category"] == "behavioural"
    assert fallback["round"] == "99"


def test_get_interview_question_reuses_pool_when_all_questions_are_exhausted():
    agent.create_session_state("reuse-session")
    agent._session_id_context = "reuse-session"
    state = agent._get_state("reuse-session")
    state.asked.extend(agent._QUESTIONS["behavioural"])

    result = agent.get_interview_question(round_number=1, category="behavioural")
    assert result["category"] == "behavioural"
    assert result["question"] == agent._QUESTIONS["behavioural"][0]


def test_get_interview_question_reuses_topic_pool_when_exhausted():
    agent.create_session_state("topic-exhausted")
    agent._session_id_context = "topic-exhausted"
    state = agent._get_state("topic-exhausted")
    state.asked.extend(agent._QUESTIONS_BY_TOPIC["stack"]["easy"])

    result = agent.get_interview_question(round_number=2, topic="stack", difficulty="easy")
    assert result["topic"] == "stack"
    assert result["question"] == agent._QUESTIONS_BY_TOPIC["stack"]["easy"][0]


def test_record_note_and_rubric_report_paths():
    agent.create_session_state("grade-session")
    agent._session_id_context = "grade-session"

    note = agent.record_answer_note("Q", "Strong structure", "Missed edge cases")
    assert note["status"] == "noted"
    assert agent._get_state("grade-session").notes[0]["strength"] == "Strong structure"

    invalid = agent.submit_rubric_grade("communication", "bad-grade", "Nope")
    assert "error" in invalid

    valid = agent.submit_rubric_grade("Communication", "yes", "Clear explanation")
    assert valid == {"status": "graded", "category": "communication", "grade": "yes"}

    report = agent.get_rubric_report()
    assert report["count"] == 1
    assert "communication: YES" in report["report"]
    assert report["grades"]["communication"]["notes"] == "Clear explanation"


def test_destroy_session_state_removes_loop_tracker():
    agent.create_session_state("cleanup-session")
    agent._tool_loop_tracker["cleanup-session"] = {"name": "tool", "count": 1, "last_result": {}}
    agent.destroy_session_state("cleanup-session")

    assert "cleanup-session" not in agent._sessions
    assert "cleanup-session" not in agent._tool_loop_tracker


def test_get_rubric_report_empty_and_end_conversation():
    agent.create_session_state("empty-grade")
    agent._session_id_context = "empty-grade"

    assert agent.get_rubric_report() == {
        "report": "No rubric grades have been recorded yet.",
        "count": 0,
    }
    assert agent.end_conversation() == {"__end_session__": True, "status": "ending"}


def test_build_agent_and_runner(monkeypatch):
    captured_agent = {}
    captured_runner = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured_agent.update(kwargs)

    class FakeRunner:
        def __init__(self, **kwargs):
            captured_runner.update(kwargs)

    monkeypatch.setattr(agent, "Agent", FakeAgent)
    monkeypatch.setattr(agent, "Runner", FakeRunner)
    monkeypatch.setattr(agent, "InMemorySessionService", lambda: "memory-service")

    built_agent = agent.build_adk_agent(
        "system",
        model="gemini-test",
        before_tool_callback="before",
        after_tool_callback="after",
    )
    built_runner = agent.build_adk_runner("fake-agent")

    assert built_agent is not None
    assert captured_agent["name"] == "aura"
    assert captured_agent["model"] == "gemini-test"
    assert captured_agent["before_tool_callback"] == "before"
    assert captured_agent["after_tool_callback"] == "after"
    assert len(captured_agent["tools"]) == 7

    assert built_runner is not None
    assert captured_runner == {
        "agent": "fake-agent",
        "app_name": "aura",
        "session_service": "memory-service",
    }


@pytest.mark.asyncio
async def test_dispatch_tool_call_success_unknown_error_and_circuit_breaker(monkeypatch):
    agent.create_session_state("dispatch-session")

    success = await agent.dispatch_tool_call(
        "get_interview_question",
        {"round_number": 2, "topic": "stack", "difficulty": "easy"},
        session_id="dispatch-session",
    )
    assert success["topic"] == "stack"

    unknown = await agent.dispatch_tool_call("missing_tool", {}, session_id="dispatch-session")
    assert unknown == {"error": "Unknown tool: missing_tool"}

    monkeypatch.setitem(agent.TOOL_REGISTRY, "boom", lambda **kwargs: (_ for _ in ()).throw(ValueError("boom")))
    errored = await agent.dispatch_tool_call("boom", {}, session_id="dispatch-session")
    assert errored == {"error": "boom"}

    first = await agent.dispatch_tool_call("get_session_summary", {}, session_id="loop-session")
    second = await agent.dispatch_tool_call("get_session_summary", {}, session_id="loop-session")
    third = await agent.dispatch_tool_call("get_session_summary", {}, session_id="loop-session")
    fourth = await agent.dispatch_tool_call("get_session_summary", {}, session_id="loop-session")

    assert first["summary"].startswith("No questions")
    assert second["summary"].startswith("No questions")
    assert third["summary"].startswith("No questions")
    assert fourth["summary"].startswith("No questions")
    assert "instruction" in fourth
