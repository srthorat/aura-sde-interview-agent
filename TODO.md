# Aura SDE Interview Agent — Remaining Work

This file reflects the current repo state after test/coverage cleanup.

## 🔴 Deployment / Production

### 1. Vertex session service recovery — `bot/pipelines/voice.py`
**Status**: still open.

The ADK session service is still a long-lived singleton. If Vertex AI session operations start failing repeatedly, new sessions may continue to reuse a bad client until the process restarts.

### 2. Cloud Build trigger ownership — `infra/main.tf`
**Status**: still requires environment-specific setup.

The trigger now points at the repo root, but `github.owner` still needs to be set before `terraform apply`.

## 🟡 Product / Data Quality

### 3. Persist answer notes beyond process memory — `bot/agent.py`
**Status**: partially complete.

Per-session isolation exists, but `record_answer_note()` still stores answer evidence only in in-memory session state for the live process. The final call summary captures notes, but the raw structured note stream is not persisted independently.

### 4. Compress long history before reinjection — `bot/pipelines/voice.py`
**Status**: still open.

`_history_to_context()` caps history by characters, but it still injects raw prior turns instead of a compact learned summary.

### 5. Candidate progress dashboard — `frontend/public/demo.html`
**Status**: not implemented.

The UI shows live transcript, metrics, and post-call summary, but no persistent per-candidate progress view yet.

### 6. Custom question-set configuration — `bot/agent.py`
**Status**: not implemented.

Question banks are still embedded in source instead of being externally configurable.

## 🔵 Optional Enhancements

### 7. Preview-only thinking mode and proactive audio
**Status**: partially present.

Preview-model hooks exist in `voice.py`, but these paths still need explicit product validation and rollout decisions.
