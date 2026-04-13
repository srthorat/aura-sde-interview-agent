from __future__ import annotations

import importlib

import pytest

import bot.agent as agent


@pytest.fixture(autouse=True)
def reset_agent_state(monkeypatch):
    agent._sessions.clear()
    agent._session_baselines.clear()
    agent._tool_loop_tracker.clear()
    agent._session_id_context = ""
    monkeypatch.setattr(agent.random, "choice", lambda items: items[0])
    yield
    agent._sessions.clear()
    agent._session_baselines.clear()
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


def test_select_session_questions_uses_topic_for_flat_rounds():
    selected = agent.select_session_questions(
        round_hint="system_design",
        difficulty="hard",
        count=3,
        topic="cache",
    )

    assert selected
    assert all("cache" in question.lower() for question in selected)


def test_record_note_and_rubric_report_paths():
    agent.create_session_state("grade-session")
    agent._session_id_context = "grade-session"

    note = agent.record_answer_note("Q", "Strong structure", "Missed edge cases")
    assert note["status"] == "noted"
    assert agent._get_state("grade-session").notes[0]["strength"] == "Strong structure"
    assert agent._get_state("grade-session").asked == ["Q"]

    # Duplicate notes for the same question should not duplicate asked-tracking.
    agent.record_answer_note("Q", "Repeated note", "Repeated weakness")
    assert agent._get_state("grade-session").asked == ["Q"]

    invalid = agent.submit_rubric_grade("communication", "bad-grade", "Nope")
    assert "error" in invalid

    valid = agent.submit_rubric_grade("Communication", "yes", "Clear explanation")
    assert valid == {"status": "graded", "category": "communication", "grade": "yes"}

    report = agent.get_rubric_report()
    assert report["count"] == 1
    assert report["overall_count"] == 1
    assert "communication: YES" in report["report"]
    assert report["grades"]["communication"]["notes"] == "Clear explanation"


def test_evaluate_candidate_answer_normalizes_quoted_grade_keys():
    agent.create_session_state("quoted-grade-session")
    agent._session_id_context = "quoted-grade-session"

    result = agent.evaluate_candidate_answer(
        question="Tell me about a conflict.",
        strength="Brief but direct",
        weakness="Did not provide a full STAR answer",
        category_grades=[
            {"'category'": "communication", "'grade'": "no", "'notes'": "Candidate declined to answer in detail."}
        ],
    )

    assert result["graded_categories"] == ["communication"]
    report = agent.get_rubric_report()
    assert report["grades"]["communication"]["grade"] == "no"


def test_evaluate_candidate_answer_infers_grade_when_grade_key_is_malformed():
    agent.create_session_state("inferred-grade-session")
    agent._session_id_context = "inferred-grade-session"

    result = agent.evaluate_candidate_answer(
        question="How do two stacks support undo/redo?",
        strength="Candidate identified undo semantics clearly.",
        weakness="Did not explain the redo stack handoff.",
        category_grades=[
            {
                "'category'": "problem_solving",
                "adás": "yes",
                "notes": "Candidate identified key operation roles.",
            }
        ],
    )

    assert result["graded_categories"] == ["problem_solving"]
    report = agent.get_rubric_report()
    assert report["grades"]["problem_solving"]["grade"] == "yes"


def test_session_baseline_splits_current_session_from_prior_history():
    agent.create_session_state("baseline-session")
    agent._session_id_context = "baseline-session"

    state = agent._get_state("baseline-session")
    state.asked.append("Prior question")
    state.notes.append({"question": "Prior question", "strength": "Good", "weakness": "Thin details"})
    state.grades["communication"] = {"grade": "yes", "notes": "Prior clear explanation"}

    agent.mark_session_baseline("baseline-session")

    asked_question = agent.get_interview_question(round_number=2, category="coding")["question"]
    agent.record_answer_note(asked_question, "Structured answer", "Missed one edge case")
    agent.submit_rubric_grade("problem_solving", "mixed", "Needed prompting on edge cases")

    delta = agent.get_session_delta("baseline-session")
    assert delta["questions"] == [agent._QUESTIONS["coding"][0]]
    assert delta["notes"] == [{"question": asked_question, "strength": "Structured answer", "weakness": "Missed one edge case"}]
    assert set(delta["grades"]) == {"problem_solving"}

    current_report = agent.get_rubric_report()
    assert current_report["scope"] == "current"
    assert current_report["count"] == 1
    assert current_report["overall_count"] == 2
    assert set(current_report["all_grades"]) == {"communication", "problem_solving"}

    overall_summary = agent.get_session_summary()
    assert overall_summary["questions_asked"] == "2"
    assert overall_summary["current_session_questions_asked"] == "1"

    current_summary = agent.get_session_summary(scope="current")
    assert current_summary["questions_asked"] == "1"


def test_round_scorecard_prefers_current_session_grades_when_baseline_exists():
    agent.create_session_state("scorecard-baseline")
    agent._session_id_context = "scorecard-baseline"

    state = agent._get_state("scorecard-baseline")
    state.current_round = 2
    state.current_category = "coding"
    state.grades["communication"] = {"grade": "strong_yes", "notes": "Prior session grade"}
    agent.mark_session_baseline("scorecard-baseline")

    agent.submit_rubric_grade("problem_solving", "yes", "Found the right approach quickly")
    scorecard = agent.get_round_scorecard()

    assert scorecard["scope"] == "current"
    assert scorecard["status"] == "ready"
    assert scorecard["graded_categories"] == ["problem_solving"]


def test_round_scorecard_uses_current_round_and_spoken_scale():
    agent.create_session_state("scorecard-session")
    agent._session_id_context = "scorecard-session"

    agent.get_interview_question(round_number=2, category="coding")
    agent.submit_rubric_grade("problem_solving", "yes", "Found the right approach quickly")
    agent.submit_rubric_grade("code_fluency", "mixed", "Needed prompting on edge cases")
    agent.submit_rubric_grade("communication", "strong_yes", "Explained trade-offs crisply")

    scorecard = agent.get_round_scorecard()

    assert scorecard["status"] == "ready"
    assert scorecard["round_number"] == 2
    assert scorecard["round_label"] == "Coding"
    assert scorecard["spoken_score"] in {1, 2, 3, 4}
    assert scorecard["top_strength"]["category"] == "communication"
    assert "out of 4" in scorecard["summary"]


def test_round_scorecard_reports_missing_evidence():
    agent.create_session_state("empty-scorecard")
    agent._session_id_context = "empty-scorecard"

    scorecard = agent.get_round_scorecard(round_number=3)

    assert scorecard == {
        "status": "insufficient_evidence",
        "round_number": 3,
        "round_label": "System Design",
        "category": "system_design",
        "summary": "There is not enough graded evidence yet to score this round.",
    }


def test_export_and_import_session_state_round_trip():
    agent.create_session_state("source-session")
    agent._session_id_context = "source-session"
    agent.get_interview_question(round_number=1, category="behavioural")
    agent.record_answer_note("Question 1", "Clear structure", "Needed sharper examples")
    agent.submit_rubric_grade("Communication", "yes", "Explained trade-offs clearly")

    snapshot = agent.export_session_state("source-session")

    agent.create_session_state("restored-session")
    agent.import_session_state(
        "restored-session",
        {
            **snapshot,
            "grades": {
                **snapshot["grades"],
                "ignored": {"grade": "invalid", "notes": "should not restore"},
            },
            "notes": snapshot["notes"] + ["not-a-note"],
        },
    )

    restored = agent._get_state("restored-session")
    assert restored.asked == snapshot["asked"]
    assert restored.grades == snapshot["grades"]
    assert restored.notes == snapshot["notes"]
    assert restored.current_round == snapshot["current_round"]
    assert restored.current_category == snapshot["current_category"]


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
    assert len(captured_agent["tools"]) == 6

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
