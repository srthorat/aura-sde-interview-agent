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

# Topic-tagged questions for when the candidate requests a specific data structure or algorithm
_QUESTIONS_BY_TOPIC: dict[str, list[str]] = {
    "stack": [
        "How would you implement a queue using only two stacks? Walk me through enqueue and dequeue.",
        "Use a stack to evaluate a mathematical expression given in postfix notation.",
        "Describe how you would use a stack to check for balanced parentheses in a string.",
        "How would you implement a min-stack that supports push, pop, and getMin all in O(1)?",
    ],
    "queue": [
        "How would you implement a queue using only two stacks?",
        "Design a circular queue using a fixed-size array. How do you handle wrap-around?",
        "Explain how a deque (double-ended queue) works and when you would use one over a regular queue.",
        "How would you use a queue to implement BFS on a graph? Walk through your approach.",
    ],
    "linked list": [
        "How would you detect a cycle in a singly linked list? What is the time and space complexity?",
        "Walk me through reversing a singly linked list in-place.",
        "How would you merge two sorted linked lists without extra space?",
        "Find the middle element of a linked list in one pass. How does your approach handle even-length lists?",
    ],
    "tree": [
        "How would you check if a binary tree is balanced? Walk through your approach and complexity.",
        "Describe the difference between in-order, pre-order, and post-order traversal. When would you use each?",
        "How would you find the lowest common ancestor of two nodes in a binary search tree?",
        "Given a binary tree, return the level-order traversal as a list of lists.",
    ],
    "graph": [
        "Describe an approach to detect a cycle in a directed graph using DFS.",
        "How would you find the shortest path between two nodes in an unweighted graph?",
        "Explain how topological sort works. What kind of graph does it require?",
        "Walk me through how you would check if a graph is bipartite.",
    ],
    "array": [
        "Given an unsorted array, find the two numbers that add up to a target sum. What is the optimal complexity?",
        "How would you find the maximum subarray sum? Describe Kadane's algorithm.",
        "Rotate an array to the right by k steps in O(1) extra space.",
        "Given a sorted array, search for a target value in O(log n). How does binary search handle edge cases?",
    ],
    "hash map": [
        "How would you implement an LRU cache using a hash map and a doubly linked list?",
        "Find the first non-repeating character in a string using a hash map.",
        "Group anagrams together from a list of strings. What is the time complexity?",
        "Given an array, find all pairs that sum to zero. How does a hash set help here?",
    ],
    "string": [
        "Walk me through finding the longest substring without repeating characters.",
        "How would you check if two strings are anagrams of each other?",
        "Implement string compression: 'aabcccdddd' → 'a2b1c3d4'.",
        "Given a string, find the longest palindromic substring. What is your approach?",
    ],
    "recursion": [
        "Implement the Fibonacci sequence both recursively and iteratively. Compare the complexities.",
        "How would you solve the Tower of Hanoi problem recursively? What is the recurrence relation?",
        "Walk me through generating all subsets of a set using recursion.",
        "How would you flatten a deeply nested list using recursion?",
    ],
    "dynamic programming": [
        "Explain how you would solve the 0/1 knapsack problem using dynamic programming.",
        "How would you find the longest common subsequence of two strings?",
        "Walk me through the coin change problem — find the minimum number of coins for a target amount.",
        "How does memoisation differ from tabulation? Give a concrete example.",
    ],
    "sorting": [
        "Walk me through quicksort. What is the average and worst-case complexity, and when does worst-case occur?",
        "Explain merge sort. How would you use it to sort a linked list?",
        "What is the difference between a stable and an unstable sort? Give an example of each.",
        "How would you sort an array containing only 0s, 1s, and 2s in a single pass?",
    ],
    "binary search": [
        "Implement binary search on a sorted array. How do you handle duplicates?",
        "How would you find the first and last position of a target in a sorted array?",
        "A sorted array has been rotated at an unknown pivot. How do you search it in O(log n)?",
        "Use binary search to find the square root of a number without using the sqrt function.",
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


def get_interview_question(round_number: int = 1, category: str = "", topic: str = "") -> dict[str, str]:
    """Return a targeted interview question for the given round, category, and topic.

    Args:
        round_number: Interview round 1–4 (default 1).
        category: One of 'behavioural', 'coding', 'system_design', 'debrief'.
                  If empty, picks the default category for the round.
        topic: Specific data structure or algorithm requested by the candidate.
               Examples: 'stack', 'queue', 'linked list', 'tree', 'graph',
               'array', 'hash map', 'string', 'recursion', 'dynamic programming',
               'sorting', 'binary search'. Leave empty to pick from the full pool.
    """
    # If a specific topic was requested, look it up directly
    if topic:
        topic_key = topic.lower().strip()
        # Fuzzy match: check if any known topic key is contained in the request
        matched_pool = None
        for key, pool in _QUESTIONS_BY_TOPIC.items():
            if key in topic_key or topic_key in key:
                matched_pool = pool
                break
        if matched_pool:
            available = [q for q in matched_pool if q not in _asked_this_session]
            if not available:
                available = matched_pool
            question = random.choice(available)
            _asked_this_session.append(question)
            return {"question": question, "category": "coding", "round": str(round_number), "topic": topic_key}

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
            "Fetch a targeted interview question for a specific round, category, and topic. "
            "ALWAYS call this when presenting the next question. "
            "If the candidate mentions a specific data structure or algorithm (e.g. 'stack', 'queue', "
            "'linked list', 'tree', 'graph', 'array', 'hash map', 'sorting', 'binary search', "
            "'dynamic programming', 'recursion', 'string'), pass it as the topic parameter."
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
                "topic": {
                    "type": "STRING",
                    "description": (
                        "Specific data structure or algorithm the candidate requested. "
                        "One of: 'stack', 'queue', 'linked list', 'tree', 'graph', 'array', "
                        "'hash map', 'string', 'recursion', 'dynamic programming', 'sorting', 'binary search'. "
                        "Leave empty if the candidate did not specify a topic."
                    ),
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
