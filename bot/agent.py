"""ADK Agent definition for Aura — Google SDE Interview Coach.

Aura is a real-time voice interview coach that conducts Google-style
technical interviews (coding, system design, behavioural) with full
cross-session memory.  Each candidate is identified by their user_id;
VertexAiSessionService persists round history so Aura knows exactly
which round the candidate is on and what was covered before.

Round progression (tracked automatically via session history):
  Round 1 — Behavioural / Leadership (Googleyness + Leadership principles)
  Round 2 — Coding / Algorithms (LC-style, spoken pseudocode)
  Round 3 — System Design (design a distributed system)
  Round 4 — Full debrief + targeted weak-spot practice

Tools available during live audio sessions:
  get_current_time        — answer-timing signals to the candidate
  get_interview_question  — pulls a targeted question by round + category
  record_answer_note      — saves a structured strength/weakness note
  get_session_summary     — returns a spoken performance summary
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from loguru import logger


# ---------------------------------------------------------------------------
# Question bank
# ---------------------------------------------------------------------------

_QUESTIONS: dict[str, list[str]] = {
    "behavioural": [
        "Tell me about a time you had a significant disagreement with your manager. How did you handle it?",
        "Describe a project where you had to deliver under an extremely tight deadline. What trade-offs did you make?",
        "Give me an example of a time you influenced a team decision without having direct authority.",
        "Tell me about the most technically complex problem you have solved. Walk me through your thinking.",
        "Describe a time you failed. What did you learn and what would you do differently?",
        "Tell me about a time you had to learn a completely new technology quickly to deliver a project.",
        "Give an example of when you had to balance quality versus speed. What did you decide and why?",
    ],
    "coding": [
        "Given an array of integers, find the two numbers that add up to a target sum. What is the time and space complexity of your approach?",
        "How would you design a function to check if a binary tree is balanced? Think aloud as you reason through edge cases.",
        "Explain how you would implement an LRU cache. What data structures would you use and why?",
        "Walk me through how you would find the longest substring without repeating characters.",
        "How would you merge two sorted linked lists? Can you do it without extra space?",
        "Describe an approach to detect a cycle in a directed graph.",
        "How would you implement a queue using only two stacks?",
    ],
    "system_design": [
        "Design a URL shortener like bit.ly that handles 100 million URLs and 1 billion reads per day.",
        "How would you design Google Search's autocomplete suggestion system?",
        "Design a distributed rate limiter that works across multiple data centres.",
        "Walk me through the architecture of a real-time collaborative document editor like Google Docs.",
        "How would you design a notification system that sends push, email, and SMS at massive scale?",
        "Design YouTube's video upload and transcoding pipeline.",
        "How would you build a global leaderboard for an online game with millions of concurrent players?",
    ],
    "debrief": [
        "Looking back across all your rounds, which answer are you least satisfied with and why?",
        "If you had 30 more minutes on your system design, what would you add or change?",
        "What do you think your single biggest technical blind spot is right now?",
        "How would a peer describe your code quality under pressure?",
    ],
}

_CATEGORIES_BY_ROUND = {
    1: ["behavioural"],
    2: ["coding"],
    3: ["system_design"],
    4: ["behavioural", "coding", "system_design", "debrief"],
}

_asked_this_session: list[str] = []


# ---------------------------------------------------------------------------
# ADK Tools
# ---------------------------------------------------------------------------

def get_current_time() -> dict[str, str]:
    """Return the current UTC time — useful for answer-timing feedback."""
    now = datetime.now(timezone.utc)
    return {
        "time": now.strftime("%H:%M UTC"),
        "date": now.strftime("%A, %B %d, %Y"),
    }


def get_interview_question(round_number: int = 1, category: str = "") -> dict[str, str]:
    """Return a targeted interview question for the given round and category.

    Args:
        round_number: Interview round 1–4 (default 1).
        category: One of 'behavioural', 'coding', 'system_design', 'debrief'.
                  If empty, picks the default category for the round.
    """
    if not category:
        cats = _CATEGORIES_BY_ROUND.get(round_number, ["behavioural"])
        category = random.choice(cats)

    pool = _QUESTIONS.get(category, _QUESTIONS["behavioural"])
    # Avoid repeating questions already asked this session
    available = [q for q in pool if q not in _asked_this_session]
    if not available:
        available = pool  # fallback: allow repeats if pool exhausted

    question = random.choice(available)
    _asked_this_session.append(question)

    return {
        "question": question,
        "category": category,
        "round": str(round_number),
    }


def record_answer_note(question: str, strength: str, weakness: str) -> dict[str, str]:
    """Save a structured note about a candidate's answer.

    Args:
        question: The question that was answered.
        strength: What the candidate did well.
        weakness: What needs improvement or was missing.
    """
    logger.info(f"[interview] Note recorded — Q: {question[:60]}... | + {strength} | - {weakness}")
    return {
        "status": "noted",
        "question_snippet": question[:80],
        "strength": strength,
        "weakness": weakness,
    }


def get_session_summary() -> dict[str, str]:
    """Return a summary of questions asked so far this session."""
    count = len(_asked_this_session)
    if count == 0:
        return {"summary": "No questions have been asked yet in this session."}
    summary = f"{count} question{'s' if count != 1 else ''} covered: " + "; ".join(
        q[:50] + "…" for q in _asked_this_session
    )
    return {"summary": summary, "questions_asked": str(count)}


# ---------------------------------------------------------------------------
# Tool registry + live declarations
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    "get_current_time": get_current_time,
    "get_interview_question": get_interview_question,
    "record_answer_note": record_answer_note,
    "get_session_summary": get_session_summary,
}

LIVE_TOOL_DECLARATIONS = [
    {
        "name": "get_current_time",
        "description": "Returns the current UTC time and date. Use to tell the candidate how long they have been speaking.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "get_interview_question",
        "description": (
            "Fetch a targeted interview question for a specific round and category. "
            "Call this when you are ready to present the next question."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "round_number": {
                    "type": "INTEGER",
                    "description": "Interview round number 1–4.",
                },
                "category": {
                    "type": "STRING",
                    "description": "Question category: 'behavioural', 'coding', 'system_design', or 'debrief'. Leave empty to use round default.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "record_answer_note",
        "description": (
            "Save a structured note about the quality of a candidate's answer. "
            "Call this after the candidate finishes answering a question."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "The question that was just answered.",
                },
                "strength": {
                    "type": "STRING",
                    "description": "What the candidate did well in their answer.",
                },
                "weakness": {
                    "type": "STRING",
                    "description": "What was missing, unclear, or needs improvement.",
                },
            },
            "required": ["question", "strength", "weakness"],
        },
    },
    {
        "name": "get_session_summary",
        "description": "Get a summary of all questions asked and notes taken in this session so far.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def build_adk_agent(system_instruction: str) -> Agent:
    """Create and return the ADK Interview Coach agent with all tools."""
    return Agent(
        name="aura",
        model="gemini-2.5-flash",
        instruction=system_instruction,
        tools=[
            get_current_time,
            get_interview_question,
            record_answer_note,
            get_session_summary,
        ],
    )


def build_adk_runner(agent: Agent) -> Runner:
    """Wrap the agent in an ADK Runner with an in-memory session service."""
    return Runner(
        agent=agent,
        app_name="aura",
        session_service=InMemorySessionService(),
    )


# ---------------------------------------------------------------------------
# Tool dispatcher (called by voice pipeline for live audio tool calls)
# ---------------------------------------------------------------------------

async def dispatch_tool_call(name: str, args: dict) -> dict:
    """Dispatch a tool call arriving from a Gemini Live audio session."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        logger.warning(f"[tools] Unknown tool: {name}")
        return {"error": f"Unknown tool: {name}"}
    try:
        result = fn(**args)
        logger.info(f"[tools] {name}({args}) → {result}")
        return result
    except Exception as exc:
        logger.exception(f"[tools] {name} raised: {exc}")
        return {"error": str(exc)}
