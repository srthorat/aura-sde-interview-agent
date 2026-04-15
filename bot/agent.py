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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from loguru import logger


# ---------------------------------------------------------------------------
# Per-session state — keyed by session_id so concurrent candidates are isolated
# ---------------------------------------------------------------------------

@dataclass
class _SessionState:
    asked:  list[str]             = field(default_factory=list)
    grades: dict[str, dict]       = field(default_factory=dict)
    notes:  list[dict]            = field(default_factory=list)
    current_round: int | None     = None
    current_category: str         = ""

_sessions: dict[str, _SessionState] = {}  # session_id → state
_session_baselines: dict[str, dict[str, Any]] = {}  # session_id → restored-state baseline


def create_session_state(session_id: str) -> None:
    """Initialise isolated state for a new interview session."""
    _sessions[session_id] = _SessionState()
    _session_baselines.pop(session_id, None)
    logger.info(f"[agent] Created session state for {session_id}")


def destroy_session_state(session_id: str) -> None:
    """Release state when the session ends to avoid memory leaks."""
    _sessions.pop(session_id, None)
    _session_baselines.pop(session_id, None)
    _tool_loop_tracker.pop(session_id, None)
    logger.info(f"[agent] Destroyed session state for {session_id}")


def _get_state(session_id: str) -> _SessionState:
    """Return the state for session_id, creating lazily if missing."""
    if session_id not in _sessions:
        logger.warning(f"[agent] No state found for {session_id} — creating lazily")
        _sessions[session_id] = _SessionState()
    return _sessions[session_id]


def mark_session_baseline(session_id: str) -> None:
    """Snapshot restored state so current-session tools can exclude prior history."""
    state = _get_state(session_id)
    _session_baselines[session_id] = {
        "asked_count": len(state.asked),
        "notes_count": len(state.notes),
        "grades": {
            category: {
                "grade": str(data.get("grade", "")).strip(),
                "notes": str(data.get("notes", "")).strip(),
            }
            for category, data in state.grades.items()
            if isinstance(category, str) and isinstance(data, dict)
        },
    }


def get_session_delta(session_id: str) -> dict[str, Any]:
    """Return the current-session delta relative to the restored baseline."""
    state = _get_state(session_id)
    baseline = _session_baselines.get(session_id, {})

    asked_count = baseline.get("asked_count", 0)
    notes_count = baseline.get("notes_count", 0)
    prior_grades = baseline.get("grades", {})

    current_questions = state.asked[asked_count:] if isinstance(asked_count, int) and 0 <= asked_count <= len(state.asked) else list(state.asked)
    current_notes = state.notes[notes_count:] if isinstance(notes_count, int) and 0 <= notes_count <= len(state.notes) else list(state.notes)
    current_grades = {
        category: data
        for category, data in state.grades.items()
        if prior_grades.get(category) != data
    }

    return {
        "questions": list(current_questions),
        "notes": list(current_notes),
        "grades": current_grades,
        "prior_grades": dict(prior_grades),
    }


def export_session_state(session_id: str) -> dict[str, Any]:
    """Return a JSON-safe snapshot of the interview state for persistence."""
    state = _get_state(session_id)
    return {
        "asked": [question for question in state.asked if isinstance(question, str) and question.strip()],
        "grades": {
            str(category): {
                "grade": str(data.get("grade", "")).strip(),
                "notes": str(data.get("notes", "")).strip(),
            }
            for category, data in state.grades.items()
            if isinstance(category, str) and isinstance(data, dict)
        },
        "notes": [
            {
                "question": str(note.get("question", "")).strip(),
                "strength": str(note.get("strength", "")).strip(),
                "weakness": str(note.get("weakness", "")).strip(),
            }
            for note in state.notes
            if isinstance(note, dict)
        ],
        "current_round": state.current_round,
        "current_category": state.current_category,
    }


def import_session_state(session_id: str, snapshot: dict[str, Any]) -> None:
    """Restore a persisted session snapshot into the current in-process state."""
    state = _get_state(session_id)

    asked = snapshot.get("asked", []) if isinstance(snapshot, dict) else []
    state.asked = [
        question.strip()
        for question in asked
        if isinstance(question, str) and question.strip()
    ]

    restored_grades: dict[str, dict[str, str]] = {}
    grades = snapshot.get("grades", {}) if isinstance(snapshot, dict) else {}
    if isinstance(grades, dict):
        for category, data in grades.items():
            if not isinstance(category, str) or not isinstance(data, dict):
                continue
            grade = str(data.get("grade", "")).lower().strip()
            notes = str(data.get("notes", "")).strip()
            if grade in _VALID_GRADES and notes:
                restored_grades[category.strip().lower().replace(" ", "_")] = {
                    "grade": grade,
                    "notes": notes,
                }
    state.grades = restored_grades

    restored_notes: list[dict[str, str]] = []
    notes = snapshot.get("notes", []) if isinstance(snapshot, dict) else []
    if isinstance(notes, list):
        for note in notes:
            if not isinstance(note, dict):
                continue
            question = str(note.get("question", "")).strip()
            strength = str(note.get("strength", "")).strip()
            weakness = str(note.get("weakness", "")).strip()
            if question and (strength or weakness):
                restored_notes.append({
                    "question": question,
                    "strength": strength,
                    "weakness": weakness,
                })
    state.notes = restored_notes
    current_round = snapshot.get("current_round") if isinstance(snapshot, dict) else None
    state.current_round = current_round if isinstance(current_round, int) and 1 <= current_round <= 4 else None
    current_category = snapshot.get("current_category", "") if isinstance(snapshot, dict) else ""
    state.current_category = current_category.strip() if isinstance(current_category, str) else ""
    logger.info(
        f"[agent] Restored persisted state for {session_id} "
        f"(asked={len(state.asked)}, grades={len(state.grades)}, notes={len(state.notes)})"
    )
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
        "Tell me about a time you disagreed with a technical decision that had already been made. What did you do?",
        "Describe a situation where you had to work with a very difficult colleague. How did you manage it?",
        "Tell me about a time you went above and beyond what was expected of you on a project.",
        "Give an example of a project you are most proud of. What made it special?",
        "Tell me about a time you had to give difficult feedback to a peer. How did you deliver it?",
        "Describe a time when you had to make a decision with very limited information. What was your process?",
        "Tell me about a time you saw a process that wasn't working and took the initiative to fix it.",
        "Give an example of when you used data to change someone's mind.",
        "Tell me about a time you had to disagree with a customer or stakeholder. How did you handle it?",
        "Describe a time when priorities changed mid-sprint or mid-project. How did you adapt?",
        "Tell me about the most impactful bug you ever found and fixed in production.",
        "Give an example of a time you mentored someone. What was your approach?",
        "Tell me about a time you had to deliver bad news to your team or manager. How did you do it?",
        "Describe the biggest technical risk you ever took. Did it pay off?",
        "Tell me about a time you had to roll back a release. What happened and what did you learn?",
        "Give an example of when you had to sacrifice a long-term goal for a short-term one. Was it worth it?",
        "Tell me about a time you worked on a cross-functional team with non-engineers. How did you communicate?",
        "Describe a time when you had to estimate the effort for an ambiguous project. How did you approach it?",
        "Tell me about a project that changed significantly in scope. How did you manage the change?",
        "Give an example of a time you pushed back on a product decision. What was the outcome?",
        "Tell me about a time you automated something that was previously done manually. What was the impact?",
        "Describe how you have handled on-call duties or production incidents in the past.",
        "Tell me about a time you had to collaborate with a remote or distributed team. What challenges did you face?",
        "Give an example of when you left code or a system better than you found it.",
        "Tell me about a technical decision you made that you later regretted. What did you learn?",
        "Describe how you stay current with new technologies. Give a recent example.",
        "Tell me about a time you had to advocate for engineering best practices to a non-technical audience.",
        "Give an example of when your attention to detail caught a problem others had overlooked.",
    ],
    "system_design": [
        "Design a URL shortener like bit.ly that handles 100 million URLs and 1 billion reads per day.",
        "How would you design Google Search's autocomplete suggestion system?",
        "Design a distributed rate limiter that works across multiple data centres.",
        "Walk me through the architecture of a real-time collaborative document editor like Google Docs.",
        "How would you design a notification system that sends push, email, and SMS at massive scale?",
        "Design YouTube's video upload and transcoding pipeline.",
        "How would you build a global leaderboard for an online game with millions of concurrent players?",
        "Design Twitter's tweet fanout and timeline generation system.",
        "How would you design a distributed key-value store like DynamoDB or Cassandra?",
        "Design a ride-sharing system like Uber — focus on the real-time driver-matching component.",
        "How would you design a distributed message queue like Kafka?",
        "Design a search engine's web crawler and indexing pipeline at Google scale.",
        "How would you design a distributed cache layer like Redis Cluster?",
        "Design an Instagram-scale photo storage and delivery system.",
        "How would you design a Dropbox or Google Drive file storage and sync service?",
        "Design a payment processing system that handles millions of transactions per day safely.",
        "How would you design an ad serving system that selects the most relevant ad in under 50ms?",
        "Design a live streaming platform like Twitch — how do you handle encoding, CDN, and real-time chat?",
        "How would you design a hotel reservation or flight booking system with strong consistency?",
        "Design a distributed job scheduler that reliably executes millions of background tasks.",
        "How would you design a fraud detection system for a large e-commerce platform?",
        "Design a recommendation engine for a music streaming service like Spotify.",
        "How would you design a real-time analytics dashboard that ingests billions of events per day?",
        "Design WhatsApp's messaging system — focus on delivery guarantees and end-to-end encryption architecture.",
        "How would you design a distributed transaction system with ACID guarantees across microservices?",
        "Design the Google Maps route calculation and ETA prediction system.",
        "How would you design a content delivery network to serve static assets globally with sub-100ms latency?",
        "Design a stock trading platform that matches buy and sell orders with very low latency.",
        "How would you build a multi-tenant SaaS platform with strong data isolation between customers?",
        "Design a code repository and CI/CD pipeline system like GitHub.",
        "How would you design a location-based nearby-search API at Yelp or Google Maps scale?",
        "Design a log aggregation and alerting system that monitors thousands of microservices.",
        "How would you build a globally consistent distributed lock service?",
        "Design a social graph service that powers the friendship and follower system at Facebook scale.",
        "How would you design an A/B testing platform that handles concurrent experiments on millions of users?",
    ],
    "debrief": [
        "Looking back across all your rounds, which answer are you least satisfied with and why?",
        "If you had 30 more minutes on your system design, what would you add or change?",
        "What do you think your single biggest technical blind spot is right now?",
        "How would a peer describe your code quality under pressure?",
        "Which algorithm or data structure do you feel least confident about?",
        "What is one question from today that you wish you had answered differently?",
        "How do you typically approach a problem you have never seen before?",
        "If you were hiring, what qualities would you look for that you think you demonstrated today?",
        "What technical trade-off did you make today that you would most want to revisit?",
        "Which part of today's interview felt most natural and which felt hardest?",
        "How would you rate your communication of technical ideas today on a scale of 1–10 and why?",
        "What assumptions did you make in your system design that you would want to validate first?",
        "If you had to redo your coding question with no time pressure, what would your cleaner solution look like?",
        "What one piece of feedback would you give yourself as your own interviewer today?",
        "How did you manage nerves or stress during the interview?",
        "What aspect of Google engineering culture do you think your answers reflected well?",
        "Which of your answers today showed the most depth of thinking?",
        "How do you typically balance writing clean code against meeting a deadline?",
        "What would you change in your system design to support 100x more scale?",
        "How confident are you in the time and space complexity analysis you gave today?",
        "Which data structure did you rely on most today — was it always the best choice?",
        "What is one thing you learned or were reminded of during this interview?",
        "If a teammate reviewed your solution from today, what would they praise?",
        "What follow-up question are you glad I didn't ask about your system design?",
        "How would you improve the question I asked you to make it a better interview question?",
        "What is one assumption you made that, if wrong, would completely change your solution?",
        "How did your behavioural examples today reflect your values as an engineer?",
        "What question about your own experience were you hoping I would ask but didn't?",
        "How do you think your performance today compares to your typical performance under pressure?",
        "What is the most important lesson from a project failure you didn't get to share today?",
        "If you could add one round to better showcase your skills, what would it cover?",
        "How did you decide what to include vs. omit in your system design discussion?",
        "What edge cases from your coding solution are you now worried you missed?",
        "If this were a real hiring decision, what additional information would you want me to have about you?",
        "How has your thinking on any of today's topics evolved during our conversation?",
    ],
    "debugging": [
        "You are given a service that intermittently returns stale data after deploys. How would you debug it end to end?",
        "A production API's p99 latency doubled after a release, but average latency is unchanged. Walk me through your debugging process.",
        "You see a memory leak in a long-running Python service. How would you narrow down the root cause?",
        "A distributed job processor is occasionally running the same job twice. How would you investigate and fix it?",
        "A binary search implementation passes most tests but fails on some edge cases. What specific bugs would you look for first?",
        "Review this situation: a cache is improving latency but causing occasional stale reads for critical user actions. How would you debug and mitigate it?",
        "A teammate says their code works locally but fails in CI. How would you structure the debugging conversation and investigation?",
        "You are handed a flaky test suite with non-deterministic failures. How would you isolate the cause?",
        "An on-call alert shows rising error rates, but only for one region. What signals would you inspect first and why?",
        "A queue-backed worker system is falling behind even though CPU usage is low. How would you debug the bottleneck?",
        "You are reviewing code that introduced a race condition in a concurrent component. How would you explain the bug and propose a fix?",
        "A customer reports that search results are missing recently uploaded documents. How would you debug the indexing pipeline?",
        "A mobile client shows duplicate messages after reconnecting to the backend. How would you investigate whether the bug is client-side, server-side, or protocol-related?",
        "You are given logs, metrics, and a stack trace for a crash loop in one microservice. How would you prioritize your debugging steps?",
        "A feature flag rollout caused only a subset of users to see incorrect behavior. How would you reason about reproducing and fixing it?",
    ],
}

# Build a flat "coding" pool from all topic questions (used when category='coding' but no topic).
# Populated lazily after _QUESTIONS_BY_TOPIC is defined — see bottom of dict.

# Topic questions organised by difficulty tier — easy / medium / hard (≈ 20 per topic).
_QUESTIONS_BY_TOPIC: dict[str, dict[str, list[str]]] = {
    "stack": {
        "easy": [
            "What is a stack? Explain LIFO with two real-world examples.",
            "How would you implement a stack using a Python list? What are the time complexities of push, pop, and peek?",
            "Check if a string of parentheses is balanced using a stack. Walk through your algorithm.",
            "Use a stack to reverse a string in-place. Walk through your approach.",
            "How would you use two stacks to simulate undo and redo functionality?",
            "Describe how a call stack works in a recursive program. What happens when recursion is too deep?",
            "Implement a stack that tracks the minimum element in O(1) time. What extra storage do you need?",
            "What is the difference between a stack and a queue? Give one use case where each is the right choice.",
            "How is a stack implemented under the hood in most languages? What memory area does it use?",
            "Why can a stack be used to convert a recursive algorithm into an iterative one?",
            "What happens when you call pop() on an empty stack? How would you guard against it?",
            "Explain the concept of stack overflow. What causes it in real programs?",
        ],
        "medium": [
            "Implement a queue using two stacks. Analyse the amortised time complexity of each operation.",
            "Evaluate a postfix (reverse Polish notation) expression using a stack. Walk through the algorithm.",
            "Decode a string like '3[a2[bc]]' → 'abcbcabcbcabcbc' using a stack.",
            "Implement a browser history with back and forward navigation using two stacks.",
            "Given a histogram of bar heights, find the largest rectangle that fits entirely within using a stack.",
            "Return the next greater element for each element in an array using a monotonic stack.",
            "Given a sequence of push and pop operations, determine if the pop sequence is valid for the push sequence.",
            "Design a min-stack that also supports retrieving the second minimum in O(1) time. What extra state do you keep?",
        ],
        "hard": [
            "Solve the trapping rainwater problem using a monotonic stack. Explain approach and complexity.",
            "Generate all valid combinations of n pairs of parentheses using a stack-based DFS.",
            "Implement a basic calculator that evaluates a string expression with +, -, (, ) using a stack.",
            "Find the maximum area rectangle in a binary matrix by reducing it to the histogram problem.",
            "Design an in-memory file system supporting mkdir, addContentToFile, readContentFromFile using a stack for path traversal.",
            "Solve the largest rectangle in a skyline problem using two monotonic stacks for left and right boundaries.",
            "Given an array, compute the sum of subarray minimums using a monotonic stack and explain how duplicate values change the boundary logic.",
            "Implement an arithmetic expression evaluator with +, -, *, /, parentheses, and unary minus using operator and value stacks.",
        ],
    },
    "queue": {
        "easy": [
            "What is a queue? Describe FIFO and give two real-world use cases.",
            "How would you implement a queue using a Python list? What are the performance trade-offs?",
            "How does a circular queue work and why is it more efficient than a simple array-based queue?",
            "How would you use a queue to implement BFS on a graph? Walk through the algorithm on a small example.",
            "What is a deque (double-ended queue)? Give a problem that specifically benefits from one.",
            "Implement a queue with enqueue, dequeue, and a max() that returns the current maximum in O(1).",
            "Explain how a task scheduler could use a queue. How would you handle task priority?",
            "What is the time complexity of enqueue and dequeue on a Python list versus a collections.deque?",
            "When would you use a priority queue instead of a regular queue? Name a real algorithm that needs one.",
            "What is the difference between a queue and a stack at a conceptual level?",
            "How would you detect whether a queue is empty? What error should you raise on dequeue from an empty queue?",
            "Explain the producer-consumer pattern. Why is a queue the natural data structure there?",
        ],
        "medium": [
            "Implement a queue using two stacks. How does the amortised analysis work for dequeue?",
            "Design a circular buffer with a fixed capacity. How do you distinguish full from empty?",
            "Given a stream of integers, find the maximum in each sliding window of size k using a deque.",
            "Design a FIFO cache with a given capacity using a queue plus a hash map.",
            "Given a binary tree, return its right-side view using a queue-based level-order traversal.",
            "Design a hit counter that counts hits in the last 300 seconds as hits arrive in real time.",
            "Find the shortest path in an unweighted grid from start to end using BFS with a queue.",
            "Given tasks with cooldown intervals, schedule them in minimum time using a queue for pending work and explain the trade-offs.",
        ],
        "hard": [
            "Find shortest paths in a weighted graph from a source using Dijkstra's — walk through the priority-queue logic.",
            "Design a multi-level feedback queue (MLFQ) scheduler as used in OS process scheduling.",
            "Merge k sorted linked lists into one using a min-heap priority queue.",
            "Given a stream of n integers, maintain the top-k most frequent elements at any point using a min-heap.",
            "Design a rate limiter with per-user per-second limits using a token bucket algorithm with a queue.",
            "Solve the sliding window maximum problem in O(n) using a monotonic deque and prove correctness.",
            "Design a delayed-job queue that supports enqueue, cancel, and polling the next ready job efficiently at scale.",
            "Given building heights, compute the first taller building to the right for each index while supporting online inserts of new buildings.",
        ],
    },
    "linked list": {
        "easy": [
            "How would you reverse a singly linked list in-place? Walk through the three-pointer manipulation.",
            "Find the middle of a linked list in one pass using slow and fast pointers.",
            "How would you delete the Nth node from the end of a list in a single pass?",
            "Merge two sorted linked lists into one sorted list without extra space.",
            "Check if a linked list is a palindrome. How do you handle the in-place reversal?",
            "Remove all nodes with a given value from a linked list.",
            "Determine if a linked list has a cycle using Floyd's tortoise-and-hare algorithm.",
            "What is the difference between a singly linked list and a doubly linked list? When do you prefer each?",
            "How do you insert a node at the head of a singly linked list? What is the time complexity?",
            "How do you append a node to the tail of a singly linked list? How would a tail pointer help?",
            "What does it mean for a linked list node to have a null next pointer?",
            "Compare an array and a linked list: when is each faster for insert, delete, and random access?",
        ],
        "medium": [
            "Find the starting node of a cycle in a linked list. How does Floyd's algorithm identify the entry point?",
            "Add two numbers represented as linked lists where digits are stored in reverse order.",
            "Find the intersection node of two linked lists in O(n) time and O(1) space.",
            "Reorder a linked list: L0→L1→…→Ln becomes L0→Ln→L1→Ln−1→… in-place.",
            "Group all odd-indexed nodes together followed by all even-indexed nodes in a linked list.",
            "Clone a linked list where each node has a random pointer that can point to any node or null.",
            "Flatten a multilevel doubly linked list where each node may have a child doubly linked list.",
            "Partition a linked list around a value x so nodes less than x come first while preserving original relative order.",
        ],
        "hard": [
            "Reverse nodes in k-groups. How do you handle fewer than k remaining nodes at the tail?",
            "Remove all nodes with duplicate values from a sorted linked list, leaving only distinct numbers.",
            "Convert a sorted linked list to a height-balanced BST in O(n log n).",
            "Merge k sorted linked lists — compare the heap approach vs. divide-and-conquer on complexity.",
            "Rotate a linked list to the right by k places. How do you find the new tail in one pass?",
            "Design a linked list that supports O(1) insert-at-head, O(1) delete-any-node, and O(1) move-to-front.",
            "Sort a linked list in O(n log n) time and O(1) extra space. Why is merge sort the natural fit?",
            "Deep-copy a linked list with random pointers in O(1) extra space by interleaving cloned nodes with originals.",
        ],
    },
    "tree": {
        "easy": [
            "What are the three DFS traversal orders of a binary tree? Describe their output on a small example.",
            "How would you find the height of a binary tree using recursion? What is the base case?",
            "Check if a binary tree is a valid BST. What property must every node satisfy?",
            "Count nodes in a complete binary tree. Can you do better than O(n)?",
            "Find the maximum value in a BST without recursion.",
            "How would you check if two binary trees are structurally identical?",
            "Return the level-order traversal of a binary tree as a list of lists.",
            "What is a leaf node? How do you identify one in code?",
            "Explain the difference between a binary tree and a binary search tree.",
            "What is a full binary tree vs. a complete binary tree? Give a visual example of each.",
            "How does a BST make searching O(log n) on average? When does it degrade to O(n)?",
            "Why is recursion the natural approach for most tree problems?",
        ],
        "medium": [
            "How would you check if a binary tree is balanced? Walk through the O(n) single-pass algorithm.",
            "Find the lowest common ancestor — first in a BST, then in a general binary tree.",
            "Given a binary tree, return its zigzag level-order traversal.",
            "Serialize and deserialize a binary tree to and from a string. How do you encode null nodes?",
            "Construct a binary tree from its preorder and inorder traversal arrays.",
            "Find all root-to-leaf paths that sum to a given target value.",
            "Convert a sorted array to a height-balanced BST.",
            "Return the boundary traversal of a binary tree: left boundary, leaves, then right boundary.",
        ],
        "hard": [
            "Find the maximum path sum in a binary tree where the path can start and end at any node.",
            "Flatten a binary tree into a linked list in-place following preorder traversal.",
            "Recover a BST where exactly two nodes have been swapped by mistake. Can you do it in O(1) space?",
            "Find the diameter of a binary tree — the longest path between any two nodes.",
            "Implement a BST iterator that uses O(h) memory where h is the tree height.",
            "Design a segment tree that supports range sum queries and point updates in O(log n).",
            "Given a binary tree, place the minimum number of cameras so every node is monitored. Derive the tree-DP states.",
            "Serialize and deserialize an N-ary tree while preserving child order and minimizing output size.",
        ],
    },
    "graph": {
        "easy": [
            "Describe BFS and DFS on a graph. What data structures does each algorithm use?",
            "How would you find all connected components in an undirected graph?",
            "Given an adjacency list, determine if a path exists between two nodes using BFS.",
            "Compare adjacency matrix vs. adjacency list representations. When is each preferred?",
            "Find the number of islands in a 2D grid of 1s and 0s using DFS.",
            "How would you determine if an undirected graph is bipartite?",
            "Clone an undirected graph. How do you handle cycles during traversal?",
            "What is the difference between a directed and an undirected graph? Give a real-world example of each.",
            "What does it mean for a graph to be weighted? Where does edge weight matter?",
            "What is a DAG (directed acyclic graph)? Give a concrete real-world example.",
            "What is the difference between a path and a cycle in a graph?",
            "How do you represent a graph in memory and how does the choice affect time and space complexity?",
        ],
        "medium": [
            "Detect a cycle in a directed graph using DFS. How do you track the current recursion stack?",
            "Given courses and prerequisites, determine if you can finish all courses — reduce to cycle detection in a DAG.",
            "Explain topological sort using both Kahn's BFS and DFS. When does a valid order not exist?",
            "Find the shortest path between two nodes in an unweighted graph using BFS.",
            "Given airline tickets, reconstruct the complete itinerary in lexicographic order using Eulerian path.",
            "Find the minimum number of steps to transform one word to another changing one letter at a time (word ladder).",
            "Given a 2D grid, find the shortest path from top-left to bottom-right avoiding obstacles.",
        ],
        "hard": [
            "Find all strongly connected components using Kosaraju's or Tarjan's algorithm.",
            "Implement Dijkstra's algorithm for single-source shortest paths. Why does it fail with negative weights?",
            "Find all critical connections (bridges) in a network using Tarjan's bridge-finding algorithm.",
            "Given equality/inequality constraints over variables, determine if all can be satisfied simultaneously using Union-Find.",
            "Find the minimum spanning tree of a weighted undirected graph — compare Kruskal's and Prim's.",
            "Solve the network delay time problem: find the time for a signal to reach all nodes from a source.",
            "Given a directed graph with weighted edges and a stop limit k, find the cheapest path from source to destination with at most k stops.",
            "Reconstruct an alien dictionary order from a sorted word list, then detect when the ordering constraints are inconsistent.",
        ],
    },
    "array": {
        "easy": [
            "Find two numbers in an unsorted array that add up to a target sum. What is the optimal time and space complexity?",
            "Remove duplicates from a sorted array in-place without extra space.",
            "Move all zeros to the end of an array while maintaining the relative order of non-zero elements.",
            "Find the maximum and minimum values in an unsorted array in a single pass.",
            "Rotate an array left by k positions.",
            "Find the second largest element in an array without sorting it.",
            "Given an array of booleans, move all Trues to the front in a single pass.",
            "What is the difference between a static array and a dynamic array? How does Python's list grow?",
            "What is an index out-of-bounds error and how do you guard against it?",
            "What is a two-pointer technique? Describe the basic pattern on a sorted array.",
            "What is a prefix sum array and when is it useful?",
            "How does cache locality make arrays faster than linked lists in practice?",
        ],
        "medium": [
            "Find the maximum subarray sum using Kadane's algorithm. What is the time and space complexity?",
            "Rotate an array to the right by k steps using O(1) extra space — explain the three-reversal trick.",
            "Given a sorted array rotated at an unknown pivot, search for a target in O(log n).",
            "Find all unique triplets in an array that sum to zero. How do you avoid duplicates efficiently?",
            "Find the length of the longest consecutive sequence in an unsorted array in O(n).",
            "Find the maximum profit from a single buy-then-sell transaction on a stock price array.",
            "Return a new array where each element is the product of all other elements without using division.",
        ],
        "hard": [
            "Find the median of two sorted arrays of different sizes in O(log(min(m,n))). Walk through the binary search.",
            "Solve the trapping rainwater problem. Compare the stack, two-pointer, and prefix-array approaches.",
            "Find the longest increasing subsequence in O(n log n) using patience sorting with binary search.",
            "Find the minimum window substring containing all characters of a target string in O(n).",
            "Given a 2D matrix sorted row-wise and column-wise, search for a target in O(m+n).",
            "Solve jump game II: find the minimum number of jumps to reach the last index with a greedy approach.",
            "Given an unsorted array, find the first missing positive integer in O(n) time and O(1) extra space.",
            "Count the number of range sums that lie within [lower, upper] using prefix sums and a modified merge-sort approach.",
        ],
    },
    "hash map": {
        "easy": [
            "Find the first non-repeating character in a string using a hash map.",
            "Count the frequency of each element in an array using a hash map.",
            "Find the intersection of two arrays — elements that appear in both.",
            "Check if two strings are isomorphic — every character in s maps uniquely to a character in t.",
            "Using a hash set, find all duplicate values in an array in O(n).",
            "Given a list of pairs, find all values that appear more than once.",
            "Group elements with the same value from an array using a hash map.",
            "What is a hash function? What properties make a good one?",
            "What is a hash collision? Describe two strategies for resolving one.",
            "What is the average time complexity of get, put, and delete in a hash map?",
            "What is the difference between a hash map and a hash set?",
            "When would you use a sorted map (like a BST map) instead of a hash map?",
        ],
        "medium": [
            "Implement an LRU cache using a hash map and a doubly linked list. Walk through get and put.",
            "Group anagrams together from a list of strings. What is your hashing strategy?",
            "Design a data structure that supports insert, delete, and getRandom in O(1) average time.",
            "Find the longest consecutive sequence in an unsorted array in O(n) using a hash set.",
            "Implement a time-based key-value store: set(key, value, timestamp) and get(key, timestamp).",
            "Find the longest subarray with sum equal to k using prefix sums and a hash map.",
            "Find all pairs in an array that sum to zero — hash-set approach vs. sorting.",
        ],
        "hard": [
            "Design a HashMap from scratch without built-in hash tables. How do you handle collisions and resizing?",
            "Implement consistent hashing for a distributed cache. How do you add/remove nodes with minimal key remapping?",
            "Find the smallest window in s containing all characters of t using a sliding window and hash map.",
            "Find the number of subarrays whose XOR equals k using a prefix XOR hash map.",
            "Design a log aggregation system that counts events per minute and answers range sum queries efficiently.",
            "Find the minimum number of distinct values in a sliding window of size k at every position.",
            "Design a distributed unique-ID generator that avoids collisions across regions while keeping IDs roughly time ordered.",
            "Given a stream of events, maintain the longest consecutive interval of timestamps seen so far using hash-based merging.",
        ],
    },
    "string": {
        "easy": [
            "How would you reverse a string in-place?",
            "Check if two strings are anagrams of each other. What is your approach and complexity?",
            "Check if a string is a palindrome, ignoring spaces and punctuation.",
            "Find the first non-repeating character in a string.",
            "Implement string compression: 'aabcccdddd' → 'a2b1c3d4'.",
            "Check if one string is a rotation of another — what is the one-line trick?",
            "Count the number of words in a sentence.",
            "What is the difference between a character and a byte in a UTF-8 encoded string?",
            "How are strings typically stored in memory in Python vs. C? What are the implications for mutability?",
            "What is ASCII? Name five common ASCII values that every programmer should know.",
            "What is the time complexity of string concatenation in a loop? How do you fix it?",
            "Explain what a substring is vs. a subsequence. Give an example of each.",
        ],
        "medium": [
            "Find the longest substring without repeating characters using a sliding window.",
            "Find the longest palindromic substring. Compare expand-around-center vs. DP approaches.",
            "Convert a Roman numeral string to an integer.",
            "Reverse the words in a sentence while preserving spaces.",
            "Given a pattern string and a target string, check if the target follows the pattern.",
            "Implement strStr() — find the first occurrence of a needle in a haystack. Explain the KMP approach.",
            "Find all permutations of a string. How do you avoid duplicates when characters repeat?",
        ],
        "hard": [
            "Find the minimum window substring containing all characters of a target string in O(n).",
            "Find the longest palindromic subsequence using dynamic programming.",
            "Implement regex matching supporting '.' and '*'. How do you handle the star operator in the DP recurrence?",
            "Format a list of words so every line has exactly a given width with full justification.",
            "Implement a trie with insert, search, and startsWith. When is a trie better than a hash map?",
            "Find all words on a 2D character board using DFS with backtracking and a trie for pruning.",
            "Implement wildcard pattern matching supporting '?' and '*' for an entire string. Compare greedy and DP approaches.",
            "Given a compressed string with nested repetition like '2[a3[b]]', return the decoded string and analyze the worst-case output size.",
        ],
    },
    "recursion": {
        "easy": [
            "Implement the Fibonacci sequence recursively. What is the time complexity and why?",
            "Write a recursive function to calculate the factorial of n.",
            "Use recursion to check if a string is a palindrome.",
            "Implement binary search recursively. What is the base case?",
            "Compute x^n using fast exponentiation (exponentiation by squaring).",
            "Recursively count occurrences of a value in a nested list.",
            "Sum all integers from 1 to n using recursion. How does the call stack look?",
            "What are the two required parts of any recursive function?",
            "What is a stack overflow in the context of recursion? How many frames does Python allow by default?",
            "What is tail recursion? Does Python optimise it?",
            "When should you prefer iteration over recursion?",
            "What is indirect recursion? Give a simple example with two functions calling each other.",
        ],
        "medium": [
            "Generate all subsets of a set using recursion. How do you avoid duplicates?",
            "Generate all permutations of a string using backtracking. How do you skip repeated characters?",
            "Solve the Tower of Hanoi recursively. What is the recurrence relation and closed-form number of moves?",
            "Flatten a deeply nested list structure using recursion.",
            "Generate all valid combinations of n pairs of parentheses using recursion.",
            "Find all combinations that sum to a target value using backtracking.",
            "Implement a recursive descent parser for arithmetic expressions with +, -, *, /.",
            "Given a phone keypad mapping, generate all possible letter combinations for a digit string using recursion and backtracking.",
        ],
        "hard": [
            "Solve the N-Queens problem using backtracking. What pruning strategies improve performance?",
            "Find all valid Sudoku solutions for a given 9×9 board using recursive backtracking.",
            "Use memoised recursion to check if a target string can be segmented into dictionary words.",
            "Solve the word search problem: find a word's path on a 2D character board using DFS with backtracking.",
            "Describe how mutual recursion can parse and evaluate a full arithmetic expression with operator precedence.",
            "Count the ways to tile a 2×n board with 2×1 dominoes using memoised recursion. Derive the closed form.",
            "Partition a string into all possible palindrome decompositions using recursion and pruning. What memoization helps?",
            "Generate all structurally unique BSTs containing values 1..n using recursive divide-and-conquer.",
        ],
    },
    "dynamic programming": {
        "easy": [
            "Implement Fibonacci using bottom-up DP. What is the space complexity vs. the recursive version?",
            "What is the difference between memoisation and tabulation? Give a concrete example of each.",
            "Solve the climbing stairs problem: how many distinct ways to climb n stairs taking 1 or 2 steps at a time?",
            "Find the minimum cost to climb stairs where each step has a cost using DP.",
            "Find the maximum sum of non-adjacent elements in an array.",
            "Given coin denominations and a target, find the minimum number of coins needed.",
            "How would you use DP to count the number of ways to make change for a target amount?",
            "What is optimal substructure? Why is it a prerequisite for dynamic programming?",
            "What is overlapping subproblems? How does DP exploit this property?",
            "How do you decide whether a problem can be solved with DP?",
            "What is the time and space complexity of the recursive Fibonacci vs. DP Fibonacci?",
            "What is a state in a DP problem? Give an example from the coin-change problem.",
        ],
        "medium": [
            "Solve the 0/1 knapsack problem using DP. Describe the state, transition, and base case.",
            "Find the longest common subsequence of two strings. What is the recurrence relation?",
            "Find the longest increasing subsequence in O(n²). How would you improve to O(n log n)?",
            "Compute the edit distance between two strings using DP. What do the three choices in the recurrence represent?",
            "Count the number of unique paths in a grid from top-left to bottom-right with obstacles.",
            "Find the minimum path sum in a grid moving only right or down.",
            "Solve the matrix chain multiplication problem. What does the optimal substructure look like?",
        ],
        "hard": [
            "Solve the burst balloons problem using interval DP. What is the key insight in the recurrence?",
            "Count the ways to parenthesise a boolean expression so that it evaluates to True.",
            "Implement regex matching with '.' and '*' using DP. Carefully handle the star operator.",
            "Find the largest sum rectangle in a 2D matrix using Kadane's on column prefix sums.",
            "Find the shortest superstring covering a set of strings using bitmask DP. Explain the state representation.",
            "Solve the egg drop problem: find the minimum trials to determine the critical floor with k eggs and n floors.",
            "Given a string, partition it into the minimum number of palindromic substrings. Derive the DP recurrence and reconstruction.",
            "Count the number of ways to assign + or - signs to reach a target sum. Show the DP reduction from subset-sum.",
        ],
    },
    "sorting": {
        "easy": [
            "Walk through bubble sort. What is its time complexity and when would you ever use it?",
            "How does selection sort work? Compare it to insertion sort for nearly sorted data.",
            "Why does insertion sort perform well on small or nearly sorted arrays?",
            "What does it mean for a sort to be stable? Give an example where stability matters.",
            "Sort an array of 0s, 1s, and 2s in a single pass (Dutch National Flag problem).",
            "How does counting sort work? What are its constraints on input data?",
            "Sort a list of strings in lexicographic order using a comparator.",
            "What is the comparison model of sorting? What is the theoretical lower bound for comparisons?",
            "What does in-place sorting mean? Give one example of an in-place sort and one that is not.",
            "What is the best-case time complexity of bubble sort and when does it occur?",
            "How does Python's built-in sort work at a high level? What algorithm does it use?",
            "What is the difference between sort() and sorted() in Python?",
        ],
        "medium": [
            "Walk through quicksort. What causes O(n²) worst case and how can you mitigate it?",
            "Explain merge sort. How would you use it to sort a linked list? What is the space complexity on arrays vs. lists?",
            "How does heap sort work? Describe heapify and why you process from n/2 down to 0.",
            "What is radix sort and when is it faster than comparison-based sorts?",
            "Sort an array of intervals by start time, then merge all overlapping intervals.",
            "Find the kth largest element in an unsorted array — compare quickselect vs. using a heap.",
            "Merge two sorted arrays into one in O(m+n) time and O(1) extra space.",
            "Given intervals with start and end times, determine the minimum number of meeting rooms needed after sorting the boundaries.",
        ],
        "hard": [
            "Explain Timsort as used in Python. How does it combine merge sort and insertion sort adaptively?",
            "Sort a nearly sorted array where each element is at most k positions from its correct position using a min-heap.",
            "External sort: how would you sort a 100 GB file on a machine with only 4 GB RAM?",
            "Sort a linked list in O(n log n) time and O(1) extra space. What are the challenges vs. sorting an array?",
            "Explain introsort — how does it combine quicksort, heap sort, and insertion sort, and why do libraries use it?",
            "Given an array of n integers, find the minimum number of swaps to sort it. How do you model it as a graph problem?",
            "Given an array where each element is in [1, n], count reverse pairs efficiently using a modified merge sort and explain the proof.",
            "Sort log lines with mixed numeric and lexicographic payloads under a custom comparator while preserving stability requirements.",
        ],
    },
    "binary search": {
        "easy": [
            "Implement binary search on a sorted array. What are the exact loop conditions?",
            "Find the index of the first occurrence of a target in a sorted array with duplicates.",
            "Find the index of the last occurrence of a target in a sorted array.",
            "Find the floor (largest element ≤ target) and ceiling (smallest element ≥ target) in a sorted array.",
            "Use binary search to find the integer square root of a number without using sqrt.",
            "Count how many times a target appears in a sorted array using two binary searches.",
            "Explain the 'search on answer space' pattern. Give a simple example of a problem it solves.",
            "What is the prerequisite for binary search to work correctly?",
            "What is the time complexity of binary search and how do you derive it?",
            "What is an off-by-one error in binary search? How do you avoid it?",
            "Explain the difference between the left-biased and right-biased mid calculation. When does it matter?",
            "Can binary search be used on a linked list? Why or why not?",
        ],
        "medium": [
            "A sorted array has been rotated at an unknown pivot. How do you search for a target in O(log n)?",
            "Find the minimum element in a rotated sorted array. How do you determine which half is sorted?",
            "Find the peak element in an unsorted array in O(log n). How do neighbour comparisons guide the search?",
            "Find the median of two sorted arrays in O(log(min(m,n))) without merging them.",
            "Search a 2D matrix where rows are sorted and the first element of each row exceeds the last of the previous row.",
            "Find the first and last positions of a target in a sorted array using a single binary search helper.",
            "A mountain array increases then decreases. Find the peak, then binary-search the correct half for a target.",
        ],
        "hard": [
            "Find the kth smallest element in an m×n multiplication table using binary search on the answer space.",
            "Split an array into m subarrays to minimise the largest subarray sum using binary search on the answer.",
            "Find the smallest divisor such that the sum of ceil(element/divisor) over all elements does not exceed a threshold.",
            "Describe the median-of-medians algorithm that finds the kth element in O(n) worst-case. Compare to quickselect.",
            "Count pairs (i, j) with i < j such that arr[j] - arr[i] ≤ k in a sorted array using binary search.",
            "Given words sorted by an alien alphabet, reconstruct the character order using binary search and topological sort.",
            "Find the kth smallest pair distance in an array by binary-searching the answer and counting valid pairs efficiently.",
            "Minimise the maximum gas-station gap after adding k new stations using binary search on the answer space.",
        ],
    },
}

_CATEGORIES_BY_ROUND = {
    1: ["behavioural"],
    2: ["coding"],
    3: ["system_design"],
    4: ["behavioural", "coding", "system_design", "debrief"],
}

_asked_this_session: list[str] = []  # legacy — only used when no session_id context
_session_id_context: str = ""         # set by dispatch_tool_call per-invocation


# ---------------------------------------------------------------------------
# ADK Tools
# ---------------------------------------------------------------------------

# Populate the flat "coding" pool from all topic questions.
_QUESTIONS["coding"] = [
    q
    for pools in _QUESTIONS_BY_TOPIC.values()
    for qs in pools.values()
    for q in qs
]


def _topic_terms(topic: str) -> list[str]:
    normalized = topic.strip().lower().replace("-", " ")
    if not normalized:
        return []
    terms = {normalized}
    for part in normalized.split():
        if len(part) >= 3:
            terms.add(part)
    return [term for term in terms if term]


def _filter_questions_by_topic(pool: list[str], topic: str) -> list[str]:
    terms = _topic_terms(topic)
    if not terms:
        return list(pool)
    filtered = [
        question
        for question in pool
        if any(term in question.lower() for term in terms)
    ]
    return filtered if filtered else list(pool)


def select_session_questions(
    round_hint: str = "",
    difficulty: str = "medium",
    count: int = 9,
    topic: str = "",
) -> list[str]:
    """Pre-select `count` questions for the session based on round and difficulty.

    Called at session-creation time (in bot.py _system_instruction) so the
    question bank is baked into the system prompt before the live session starts.
    No live tool call is needed during the interview — Gemini reads from the
    injected bank directly.

    For coding rounds, one question is picked per topic at the requested
    difficulty tier, giving broad topic coverage in a small list.
    For other rounds (behavioural, system_design, debrief) the flat pool is
    sampled randomly.
    """
    norm = round_hint.strip().lower().replace(" ", "_").replace("-", "_")
    diff = (difficulty or "medium").strip().lower()
    if diff not in ("easy", "medium", "hard"):
        diff = "medium"

    pool: list[str] = []
    topic_key = topic.strip().lower()

    if norm in ("coding", "coding_1", "coding_2"):
        if topic_key:
            matched_pools: dict[str, list[str]] | None = None
            for key, topic_pools in _QUESTIONS_BY_TOPIC.items():
                if key in topic_key or topic_key in key:
                    matched_pools = topic_pools
                    break
            if matched_pools:
                bucket = matched_pools.get(diff, [])
                if bucket:
                    return random.sample(bucket, min(count, len(bucket)))
        # One question per topic at the requested difficulty, then random-sample count.
        for topic_pools in _QUESTIONS_BY_TOPIC.values():
            bucket = topic_pools.get(diff, [])
            if bucket:
                pool.append(random.choice(bucket))
        random.shuffle(pool)

    elif norm in ("system_design",):
        pool = _filter_questions_by_topic(list(_QUESTIONS.get("system_design", [])), topic_key)

    elif norm in ("behavioural", "googliness"):
        pool = _filter_questions_by_topic(list(_QUESTIONS.get("behavioural", [])), topic_key)

    elif norm in ("debugging", "code_review"):
        pool = _filter_questions_by_topic(list(_QUESTIONS.get("debugging", [])), topic_key)

    elif norm in ("targeted_debrief", "debrief"):
        pool = _filter_questions_by_topic(list(_QUESTIONS.get("debrief", [])), topic_key)

    else:
        # No round selected: mix evenly across all categories.
        for cat in ("behavioural", "system_design", "debrief"):
            cat_pool = _filter_questions_by_topic(list(_QUESTIONS.get(cat, [])), topic_key)
            if cat_pool:
                pool.extend(random.sample(cat_pool, min(3, len(cat_pool))))
        coding_pool = _filter_questions_by_topic(list(_QUESTIONS.get("coding", [])), topic_key)
        if coding_pool:
            pool.extend(random.sample(coding_pool, min(3, len(coding_pool))))

    if not pool:
        return []
    return random.sample(pool, min(count, len(pool)))


def get_current_time(**kwargs) -> dict[str, str]:
    """Return the current UTC time — useful for answer-timing feedback."""
    now = datetime.now(timezone.utc)
    return {
        "time": now.strftime("%H:%M UTC"),
        "date": now.strftime("%A, %B %d, %Y"),
    }


def get_interview_question(round_number: int = 1, category: str = "", topic: str = "", difficulty: str = "", **kwargs) -> dict[str, str]:
    """Return a targeted interview question for the given round, category, topic, and difficulty.

    Args:
        round_number: Interview round 1–4 (default 1).
        category: One of 'behavioural', 'coding', 'system_design', 'debrief'.
                  If empty, picks the default category for the round.
        topic: Specific data structure or algorithm requested by the candidate.
               Examples: 'stack', 'queue', 'linked list', 'tree', 'graph',
               'array', 'hash map', 'string', 'recursion', 'dynamic programming',
               'sorting', 'binary search'. Leave empty to pick from the full pool.
        difficulty: Desired difficulty level: 'easy', 'medium', or 'hard'.
                    Leave empty to pick from all levels combined.
    """
    state = _get_state(_session_id_context)

    # If a specific topic was requested, look it up in the difficulty-tiered bank
    if topic:
        topic_key = topic.lower().strip()
        matched_pools: dict[str, list[str]] | None = None
        for key, pools in _QUESTIONS_BY_TOPIC.items():
            if key in topic_key or topic_key in key:
                matched_pools = pools
                break
        if matched_pools:
            diff = difficulty.lower().strip() if difficulty else ""
            if diff in matched_pools:
                pool = matched_pools[diff]
            else:
                # Combine all difficulty levels when none specified
                pool = [q for qs in matched_pools.values() for q in qs]
            available = [q for q in pool if q not in state.asked]
            if not available:
                available = pool
            question = random.choice(available)
            state.asked.append(question)
            state.current_round = round_number if round_number in _ROUND_LABELS else state.current_round
            state.current_category = "coding"
            return {
                "question": question,
                "category": "coding",
                "round": str(round_number),
                "topic": topic_key,
                "difficulty": diff or "any",
                "instruction": "Present this question to the candidate now. Do NOT call this tool again until they answer.",
            }

    if not category:
        cats = _CATEGORIES_BY_ROUND.get(round_number, ["behavioural"])
        category = random.choice(cats)

    pool = _QUESTIONS.get(category, _QUESTIONS["behavioural"])
    # Avoid repeating questions already asked this session
    available = [q for q in pool if q not in state.asked]
    if not available:
        available = pool  # fallback: allow repeats if pool exhausted

    question = random.choice(available)
    state.asked.append(question)
    state.current_round = round_number if round_number in _ROUND_LABELS else state.current_round
    state.current_category = category

    return {
        "question": question,
        "category": category,
        "round": str(round_number),
        "instruction": "Present this question to the candidate now. Do NOT call this tool again until they answer.",
    }


# ---------------------------------------------------------------------------
# Rubric grading
# ---------------------------------------------------------------------------

# Canonical rubric categories mapped from the grading_rubric.md file.
_RUBRIC_CATEGORIES = {
    "problem_solving", "code_fluency", "autonomy", "cs_fundamentals",
    "system_design", "resoluteness", "communication", "curiosity",
    "awareness", "collaboration", "do_hard_things", "level_up", "time_is_precious",
}

_VALID_GRADES = {"strong_no", "no", "mixed", "yes", "strong_yes"}
_GRADE_TO_SCORE = {
    "strong_no": 1.0,
    "no": 1.8,
    "mixed": 2.6,
    "yes": 3.3,
    "strong_yes": 4.0,
}
_ROUND_LABELS = {
    1: "Behavioural",
    2: "Coding",
    3: "System Design",
    4: "Targeted Debrief",
}
_ROUND_SCORECARD_CATEGORIES = {
    1: [
        "communication",
        "collaboration",
        "awareness",
        "do_hard_things",
        "level_up",
        "time_is_precious",
        "resoluteness",
    ],
    2: [
        "problem_solving",
        "code_fluency",
        "autonomy",
        "cs_fundamentals",
        "communication",
        "resoluteness",
    ],
    3: [
        "system_design",
        "problem_solving",
        "cs_fundamentals",
        "communication",
        "collaboration",
        "awareness",
    ],
    4: [
        "awareness",
        "communication",
        "curiosity",
        "level_up",
        "collaboration",
        "autonomy",
    ],
}
_CATEGORY_TO_SCORECARD_ROUND = {
    "behavioural": 1,
    "coding": 2,
    "system_design": 3,
    "debrief": 4,
}


def _humanize_category(category: str) -> str:
    return category.replace("_", " ")


def _resolve_scorecard_round(state: _SessionState, round_number: int = 0, category: str = "") -> tuple[int, str]:
    category_key = category.lower().strip().replace(" ", "_") if category else ""
    if category_key == "behavioral":
        category_key = "behavioural"
    derived_round = _CATEGORY_TO_SCORECARD_ROUND.get(category_key)

    if isinstance(round_number, int) and round_number in _ROUND_LABELS:
        resolved_round = round_number
    elif derived_round:
        resolved_round = derived_round
    elif state.current_round in _ROUND_LABELS:
        resolved_round = state.current_round or 1
    else:
        resolved_round = 1

    resolved_category = category_key or state.current_category or _CATEGORIES_BY_ROUND.get(resolved_round, ["behavioural"])[0]
    return resolved_round, resolved_category


def _build_round_scorecard(
    state: _SessionState,
    round_number: int = 0,
    category: str = "",
    grades: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    resolved_round, resolved_category = _resolve_scorecard_round(state, round_number, category)
    target_categories = _ROUND_SCORECARD_CATEGORIES.get(resolved_round, [])
    grade_source = grades if grades is not None else state.grades
    observed = [
        (name, grade_source[name])
        for name in target_categories
        if name in grade_source and grade_source[name].get("grade") in _GRADE_TO_SCORE
    ]
    if not observed and grade_source:
        observed = [
            (name, data)
            for name, data in grade_source.items()
            if data.get("grade") in _GRADE_TO_SCORE
        ]

    if not observed:
        return {
            "status": "insufficient_evidence",
            "round_number": resolved_round,
            "round_label": _ROUND_LABELS[resolved_round],
            "category": resolved_category,
            "summary": "There is not enough graded evidence yet to score this round.",
        }

    numeric_score = sum(_GRADE_TO_SCORE[data["grade"]] for _, data in observed) / len(observed)
    spoken_score = max(1, min(4, int(round(numeric_score))))
    strongest = max(observed, key=lambda item: _GRADE_TO_SCORE[item[1]["grade"]])
    weakest = min(observed, key=lambda item: _GRADE_TO_SCORE[item[1]["grade"]])

    if numeric_score >= 3.6:
        overall = "standout"
    elif numeric_score >= 3.0:
        overall = "strong"
    elif numeric_score >= 2.3:
        overall = "developing"
    else:
        overall = "needs work"

    return {
        "status": "ready",
        "round_number": resolved_round,
        "round_label": _ROUND_LABELS[resolved_round],
        "category": resolved_category,
        "score_out_of_4": round(numeric_score, 1),
        "spoken_score": spoken_score,
        "overall_assessment": overall,
        "graded_categories": [name for name, _data in observed],
        "top_strength": {
            "category": strongest[0],
            "grade": strongest[1]["grade"],
            "notes": strongest[1]["notes"],
        },
        "focus_area": {
            "category": weakest[0],
            "grade": weakest[1]["grade"],
            "notes": weakest[1]["notes"],
        },
        "summary": (
            f"Round {resolved_round} ({_ROUND_LABELS[resolved_round]}): about {spoken_score} out of 4 overall. "
            f"Your strongest signal was {_humanize_category(strongest[0])}. "
            f"The main area to tighten is {_humanize_category(weakest[0])}."
        ),
        "instruction": (
            f"Tell the candidate their Round {resolved_round} score explicitly as {spoken_score} out of 4, "
            "then explain the strongest signal and the main area to improve in plain English."
        ),
    }


def submit_rubric_grade(category: str, grade: str, notes: str, **kwargs) -> dict[str, str]:
    """Record a rubric grade for a specific evaluation category.

    Args:
        category: One of the rubric categories (e.g. 'problem_solving', 'communication').
        grade: One of 'strong_no', 'no', 'mixed', 'yes', 'strong_yes'.
        notes: Observable facts — specific things you heard/saw — that justify this grade.
    """
    state = _get_state(_session_id_context)
    category = category.lower().strip().replace(" ", "_")
    grade = grade.lower().strip()
    if grade not in _VALID_GRADES:
        return {"error": f"Invalid grade '{grade}'. Must be one of: {', '.join(sorted(_VALID_GRADES))}"}
    state.grades[category] = {"grade": grade, "notes": notes}
    logger.info(f"[rubric] {category}: {grade} — {notes}")
    return {"status": "graded", "category": category, "grade": grade}


def get_rubric_report(scope: str = "current", **kwargs) -> dict[str, Any]:
    """Return rubric grades for the current session by default, plus overall grades."""
    state = _get_state(_session_id_context)
    delta = get_session_delta(_session_id_context)
    scope_key = (scope or "current").strip().lower()
    selected = delta["grades"] if scope_key != "overall" else state.grades
    if not selected:
        if scope_key != "overall" and state.grades:
            return {
                "report": "No new rubric grades have been recorded in this session yet.",
                "count": 0,
                "overall_count": len(state.grades),
                "all_grades": dict(state.grades),
            }
        return {"report": "No rubric grades have been recorded yet.", "count": 0}
    lines = [
        f"{cat}: {data['grade'].upper()} — {data['notes']}"
        for cat, data in selected.items()
    ]
    return {
        "report": "\n".join(lines),
        "grades": dict(selected),
        "all_grades": dict(state.grades),
        "count": len(selected),
        "overall_count": len(state.grades),
        "scope": "overall" if scope_key == "overall" else "current",
    }


def get_round_scorecard(round_number: int = 0, category: str = "", scope: str = "current", **kwargs) -> dict[str, Any]:
    """Return a spoken-friendly 1-4 round score using current-session grades by default."""
    state = _get_state(_session_id_context)
    delta = get_session_delta(_session_id_context)
    scope_key = (scope or "current").strip().lower()
    grade_source = state.grades if scope_key == "overall" else delta["grades"]
    scorecard = _build_round_scorecard(
        state,
        round_number=round_number,
        category=category,
        grades=grade_source,
    )
    if scorecard.get("status") == "ready":
        scorecard["scope"] = "overall" if scope_key == "overall" else "current"
    return scorecard


def end_conversation(**kwargs) -> dict[str, Any]:
    """Signal that the conversation should end gracefully.

    Call this ONLY when the candidate has spoken a clear, unambiguous goodbye
    (e.g. "bye", "goodbye", "I'm done for today", "I have to go") AND you have
    already said a warm farewell in response.

    NEVER call for:
    - "ok", "okay", "sure", "alright", "fine", "got it", "yeah", "yes", "no",
      "hmm", "uh-huh" — these are acknowledgements, NOT exits.
    - Garbled, unclear, or poorly-transcribed speech.
    - Short utterances (fewer than 3 words) that aren't explicit goodbyes.
    - Silence or pauses.
    - Any utterance containing pause words: "wait", "hold on", "hang on",
      "stop", "pause", "one moment", "give me a sec", "let me think".
    - Any utterance you are not 100% certain is a deliberate goodbye.

    When in doubt, do NOT call this. Keep the session alive.
    """
    logger.info("[interview] end_conversation requested by LLM")
    return {"__end_session__": True, "status": "ending"}


def record_answer_note(question: str, strength: str, weakness: str, **kwargs) -> dict[str, str]:
    """Save a structured note about a candidate's answer.

    Args:
        question: The question that was answered.
        strength: What the candidate did well.
        weakness: What needs improvement or was missing.
    """
    state = _get_state(_session_id_context)
    # In live mode we often ask questions directly from prompt context (no
    # get_interview_question tool call), so track asked questions here too.
    question_text = str(question).strip()
    if question_text and question_text not in state.asked:
        state.asked.append(question_text)

    note = {"question": question, "strength": strength, "weakness": weakness}
    state.notes.append(note)
    logger.info(f"[interview] Note — Q: {question[:60]}... | + {strength} | - {weakness}")
    return {
        "status": "noted",
        "question_snippet": question[:80],
        "strength": strength,
        "weakness": weakness,
    }


def _normalize_category_grade_entry(entry: dict[str, Any]) -> dict[str, str]:
    """Normalize slightly malformed LLM tool payloads for category grades.

    Gemini sometimes sends keys with control characters (e.g. ``\\x08grade'``)
    or extra quotes (``'category'``).  Strip everything that is not a plain
    ASCII letter so we can match ``category``, ``grade``, ``notes`` reliably.
    """
    import re

    _KEY_RE = re.compile(r"[^a-z]")          # keep only lowercase letters
    _TARGET_KEYS = ("category", "grade", "notes")

    normalized: dict[str, str] = {}
    for raw_key, raw_value in entry.items():
        if raw_value is None:
            continue
        key = _KEY_RE.sub("", str(raw_key).lower())
        # Exact match first, then substring match for garbled prefixes
        if key in _TARGET_KEYS:
            normalized[key] = str(raw_value).strip()
        else:
            for target in _TARGET_KEYS:
                if target in key and target not in normalized:
                    normalized[target] = str(raw_value).strip()
                    break

    # Some model payloads misspell the grade key (for example with a locale
    # variant), but still include a valid grade token in values.
    if "grade" not in normalized:
        for raw_value in entry.values():
            candidate = str(raw_value).strip().lower()
            if candidate in _VALID_GRADES:
                normalized["grade"] = candidate
                break
    return normalized



def evaluate_candidate_answer(question: str, strength: str, weakness: str, category_grades: list[dict[str, str]] | None = None, **kwargs) -> dict[str, Any]:
    """Record an answer note and submit multiple rubric grades in a single call to save latency.
    
    Args:
        question: The question that was answered.
        strength: What the candidate did well.
        weakness: What needs improvement or was missing.
        category_grades: Optional list of dicts. Each dict MUST have:
                         - 'category' (e.g. 'problem_solving')
                         - 'grade' ('strong_no', 'no', 'mixed', 'yes', 'strong_yes')
                         - 'notes' (observable facts)
    """
    note_result = record_answer_note(question, strength, weakness)
    grade_results = []
    if category_grades:
        for g in category_grades:
            normalized = _normalize_category_grade_entry(g)
            if "category" in normalized and "grade" in normalized and "notes" in normalized:
                res = submit_rubric_grade(
                    normalized["category"],
                    normalized["grade"],
                    normalized["notes"],
                )
                grade_results.append(res)
    return {
        "status": "success",
        "note_recorded": note_result,
        "grades_submitted": grade_results,
        "graded_categories": [result.get("category", "") for result in grade_results if isinstance(result, dict)],
    }


def get_session_summary(scope: str = "overall", **kwargs) -> dict[str, str]:
    """Return an overall recap by default, with current-session counts included when available."""
    state = _get_state(_session_id_context)
    delta = get_session_delta(_session_id_context)
    scope_key = (scope or "overall").strip().lower()
    questions = delta["questions"] if scope_key == "current" else state.asked
    count = len(questions)
    if count == 0:
        message = (
            "No new questions have been asked yet in this session."
            if scope_key == "current"
            else "No questions have been asked yet in this session."
        )
        result = {"summary": message}
    else:
        summary = f"{count} question{'s' if count != 1 else ''} covered: " + "; ".join(
            q[:50] + "…" for q in questions
        )
        result = {"summary": summary, "questions_asked": str(count)}

    current_count = len(delta["questions"])
    if scope_key != "current" and current_count:
        result["current_session_questions_asked"] = str(current_count)
        result["current_session_summary"] = (
            f"{current_count} question{'s' if current_count != 1 else ''} covered in this live session: "
            + "; ".join(q[:50] + "…" for q in delta["questions"])
        )
    return result


# ---------------------------------------------------------------------------
# Tool registry + live declarations
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    "get_current_time": get_current_time,
    "get_interview_question": get_interview_question,
    "record_answer_note": record_answer_note,
    "evaluate_candidate_answer": evaluate_candidate_answer,
    "get_session_summary": get_session_summary,
    "submit_rubric_grade": submit_rubric_grade,
    "get_rubric_report": get_rubric_report,
    "get_round_scorecard": get_round_scorecard,
    "end_conversation": end_conversation,
}

LIVE_TOOL_DECLARATIONS = [
    # ── Write-state tools (kept as live tools — needed for persistence) ──────
    # NOTE: get_interview_question, get_current_time, get_session_summary,
    # get_rubric_report, get_round_scorecard are intentionally NOT included here.
    # Each live tool call blocks Gemini from speaking (~4-7s round-trip). Read-only
    # tools cause Gemini to pause before generating its response, which users
    # experience as silence. Instead, the model draws questions from its own
    # knowledge + the session history injected in the system context.
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
        "name": "submit_rubric_grade",
        "description": (
            "Record a rubric grade for a specific evaluation category based on observable facts. "
            "Call this at the end of each round, or after observing clear evidence for a rubric dimension. "
            "Grades: 'strong_no', 'no', 'mixed', 'yes', 'strong_yes'. "
            "Categories: 'problem_solving', 'code_fluency', 'autonomy', 'cs_fundamentals', "
            "'system_design', 'resoluteness', 'communication', 'curiosity', 'awareness', "
            "'collaboration', 'do_hard_things', 'level_up', 'time_is_precious'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "Rubric category to grade (e.g. 'problem_solving', 'communication').",
                },
                "grade": {
                    "type": "STRING",
                    "description": "One of: 'strong_no', 'no', 'mixed', 'yes', 'strong_yes'.",
                },
                "notes": {
                    "type": "STRING",
                    "description": "Observable facts — specific things heard or seen — that justify this grade.",
                },
            },
            "required": ["category", "grade", "notes"],
        },
    },
    {
        "name": "end_conversation",
        "description": (
            "Gracefully end the conversation and hang up. "
            "Call this in TWO situations: "
            "(1) Full wrap-up: after you have completed a round, given a verbal scorecard, and said farewell. "
            "(2) Quick exit: whenever the candidate signals they are done for today — "
            "'I'm done for today', 'I'll come back later', 'I need to stop', 'Let's pause', "
            "'I have to go', 'bye', 'goodbye', 'see you', 'talk later' — "
            "after you have said a brief warm farewell (3 sentences max). "
            "NEVER call at the start of a conversation. "
            "NEVER call unless the candidate explicitly indicates they want to end or leave."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def build_adk_agent(
    system_instruction: str,
    model: str = "gemini-2.5-flash",
    before_tool_callback=None,
    after_tool_callback=None,
) -> Agent:
    """Create and return the ADK Interview Coach agent with all tools.

    Args:
        system_instruction: The system prompt.
        model: Gemini model name — use the live model for run_live().
        before_tool_callback: Optional callback invoked before each tool call.
        after_tool_callback: Optional callback invoked after each tool call.
    """
    # Only include tools that MUST be live (write-state or session teardown).
    # Read-only fetch tools (get_interview_question, get_current_time, etc.)
    # are intentionally excluded: each live tool call blocks Gemini from
    # speaking for ~4-8s per round-trip.  Gemini draws questions from its
    # own knowledge based on the system prompt.
    return Agent(
        name="aura",
        model=model,
        instruction=system_instruction,
        tools=[
            evaluate_candidate_answer,
            submit_rubric_grade,
            get_rubric_report,
            get_round_scorecard,
            get_session_summary,
            end_conversation,
        ],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
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

# Per-session circuit breaker: prevents Gemini from looping on the same tool.
# Maps session_id → {"name": str, "count": int, "last_result": dict}
_tool_loop_tracker: dict[str, dict] = {}

_MAX_CONSECUTIVE_CALLS = 3  # after this many, return cached + stop hint


async def dispatch_tool_call(name: str, args: dict, session_id: str = "") -> dict:
    """Dispatch a tool call arriving from a Gemini Live audio session.

    Args:
        name: Tool name.
        args: Tool arguments.
        session_id: The session_id of the caller — sets thread context so tools
                    read/write the correct per-session state.
    """
    global _session_id_context
    _session_id_context = session_id  # set before calling any tool

    # ── Circuit breaker: detect and break tool-call loops ──────────
    tracker = _tool_loop_tracker.get(session_id)
    if tracker and tracker["name"] == name:
        tracker["count"] += 1
    else:
        tracker = {"name": name, "count": 1, "last_result": {}}
    _tool_loop_tracker[session_id] = tracker

    if tracker["count"] > _MAX_CONSECUTIVE_CALLS:
        logger.warning(
            f"[tools] Circuit breaker: {name} called {tracker['count']}x "
            f"consecutively for session {session_id[:8]}. Returning cached result."
        )
        cached = dict(tracker["last_result"])
        cached["instruction"] = (
            "You have already called this tool multiple times. "
            "Use this result now and continue the conversation with the candidate. "
            "Do NOT call this tool again until the candidate has answered."
        )
        return cached

    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        logger.warning(f"[tools] Unknown tool: {name}")
        return {"error": f"Unknown tool: {name}"}
    try:
        result = fn(**args)
        logger.info(f"[tools] {name}({args}) → {result}")
        tracker["last_result"] = result
        return result
    except Exception as exc:
        logger.exception(f"[tools] {name} raised: {exc}")
        return {"error": str(exc)}
