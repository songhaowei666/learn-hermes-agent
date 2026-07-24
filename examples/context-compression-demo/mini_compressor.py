"""
Mini Context Compressor — stripped-down demo of hermes-agent's compression engine.

Demonstrates the core algorithm from agent/context_compressor.py without the
SQLite / gateway / plugin infrastructure.  Uses the REAL prompt templates from
the hermes-agent source.

Run:
    python mini_compressor.py                          # demo with built-in conversation
    python mini_compressor.py --input data.json        # compress a JSON conversation file
    python mini_compressor.py --interactive            # interactive mode
    python mini_compressor.py --stress                 # stress test: multi-round compression

Requirements: pip install openai tiktoken
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import textwrap
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Load .env from project root (searches parent directories)
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

# ---------------------------------------------------------------------------
# Real prompt templates from hermes-agent/agent/context_compressor.py
# ---------------------------------------------------------------------------

HISTORICAL_TASK_HEADING = "## Historical Task Snapshot"
HISTORICAL_IN_PROGRESS_HEADING = "## Historical In-Progress State"
HISTORICAL_PENDING_ASKS_HEADING = "## Historical Pending User Asks"
HISTORICAL_REMAINING_WORK_HEADING = "## Historical Remaining Work"

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Respond ONLY to the latest user message that appears AFTER this "
    "summary — that message is the single source of truth for what to do "
    "right now. "
    "Topic overlap with the summary does NOT mean you should resume its "
    "task: even on similar topics, the latest user message WINS. Treat ONLY "
    "the latest message as the active task and discard stale items from "
    f"'{HISTORICAL_TASK_HEADING}' / '{HISTORICAL_IN_PROGRESS_HEADING}' / "
    f"'{HISTORICAL_PENDING_ASKS_HEADING}' / "
    f"'{HISTORICAL_REMAINING_WORK_HEADING}' entirely — do not 'wrap up' or "
    "'finish' work described there unless the latest message explicitly "
    "asks for it. "
    "Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll "
    "back', 'just verify', 'don't do that anymore', 'never mind', a new "
    "topic) must immediately end any in-flight work described in the "
    "summary; do not re-surface it in later turns. "
    "IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system "
    "prompt is ALWAYS authoritative and active — never ignore or deprioritize "
    "memory content due to this compaction note. "
    "The current session state (files, config, etc.) may reflect work "
    "described here — avoid repeating it:"
)

# ---------------------------------------------------------------------------
# Mini ContextEngine (ABC) — mirrors context_engine.py
# ---------------------------------------------------------------------------


class MiniContextEngine(ABC):
    """Abstract base for pluggable context engines."""

    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6
    tail_token_budget: int = 20_000

    last_prompt_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 200_000
    compression_count: int = 0

    def __init__(self, context_length: int = 200_000):
        self.context_length = context_length
        self.threshold_tokens = int(context_length * self.threshold_percent)

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        if prompt_tokens is not None:
            self.last_prompt_tokens = prompt_tokens
        return self.last_prompt_tokens >= self.threshold_tokens

    @abstractmethod
    def compress(
        self, messages: List[Dict[str, Any]], focus_topic: str | None = None
    ) -> List[Dict[str, Any]]: ...

    def on_session_reset(self) -> None:
        self.compression_count = 0
        self.last_prompt_tokens = 0


# ---------------------------------------------------------------------------
# MiniCompressor — mirrors the five-step algorithm from context_compressor.py
# ---------------------------------------------------------------------------


class MiniCompressor(MiniContextEngine):
    """Default compression engine using lossy LLM summarisation of middle turns."""

    def __init__(
        self,
        context_length: int = 200_000,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "deepseek-v4-pro",
    ):
        super().__init__(context_length)
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "sk-demo-key")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._previous_summary: str | None = None

        # Anti-thrashing state (mirrors ContextCompressor)
        self._summary_failure_cooldown_until: float = 0.0
        self._ineffective_compression_count: int = 0
        self._fallback_compression_streak: int = 0
        self._last_compress_aborted: bool = False
        self._last_summary_error: str | None = None
        self._last_compression_made_progress: bool = False
        self._last_summary_fallback_used: bool = False
        self._last_aux_model_failure_model: str | None = None
        self._last_aux_model_failure_error: str | None = None

    # ── Public entry point ──────────────────────────────────────────────

    def compress(
        self,
        messages: List[Dict[str, Any]],
        focus_topic: str | None = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Run the five-step compression algorithm."""
        if not force and self._automatic_compression_blocked():
            print("⛔ Compression blocked (cooldown / anti-thrashing). Use force=True to override.")
            return messages

        original_count = len(messages)

        # Step 1: Prune old tool results (no LLM call)
        print(f"[Step 1] Pruning tool results (input: {original_count} messages)...")
        messages, pruned = self._prune_old_tool_results(messages)
        print(f"         Pruned {pruned} tool result(s)")

        # Step 2-3: Determine head / middle / tail boundaries
        print("[Step 2-3] Determining head / middle / tail boundaries...")
        head, middle, tail = self._split_head_middle_tail(messages)
        print(f"         head={len(head)}  middle={len(middle)}  tail={len(tail)}")

        if len(middle) == 0:
            print("         No middle turns to compress — returning unchanged.")
            self._last_compress_aborted = False
            self._last_compression_made_progress = False
            return messages

        # Step 4: Generate LLM summary of middle turns
        print(f"[Step 4] Generating summary for {len(middle)} middle messages...")
        summary = self._generate_summary(middle, focus_topic)

        if summary is None:
            print("         LLM summary failed — using deterministic fallback.")
            summary = self._deterministic_fallback(middle)
            self._last_summary_fallback_used = True
            self._last_compression_made_progress = True
        else:
            self._last_summary_fallback_used = False
            self._last_compression_made_progress = True
            print(f"         Summary generated ({len(summary)} chars)")

        # Step 5: Assemble result with SUMMARY_PREFIX
        summary_msg = self._build_summary_message(summary)
        compressed = head + [summary_msg] + tail

        self.compression_count += 1
        self._previous_summary = summary

        print(
            f"[Step 5] Done. {original_count} messages → {len(compressed)} messages "
            f"(summary replaces {len(middle)} middle turns)"
        )
        return compressed

    # ── Step 1: Prune old tool results ──────────────────────────────────

    def _prune_old_tool_results(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Replace old tool results with one-line summaries (no LLM call)."""
        if not messages:
            return messages, 0

        result = [m.copy() for m in messages]
        pruned = 0

        # Build tool_call_id -> (name, args) map
        call_id_map: Dict[str, Tuple[str, str]] = {}
        for msg in result:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    call_id_map[tc.get("id", "")] = (
                        fn.get("name", "unknown"),
                        fn.get("arguments", ""),
                    )

        # Token-budget walk to find prune boundary
        accumulated = 0
        boundary = len(result)
        min_protect = min(self.protect_last_n, len(result))
        for i in range(len(result) - 1, -1, -1):
            msg_tokens = _rough_token_estimate(json.dumps(result[i], ensure_ascii=False))
            if accumulated + msg_tokens > self.tail_token_budget and (len(result) - i) >= min_protect:
                boundary = i
                break
            accumulated += msg_tokens

        # Deduplicate identical tool results (keep newest full copy)
        content_hashes: Dict[str, Tuple[int, str]] = {}
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content") or ""
            if not isinstance(content, str) or len(content) < 200:
                continue
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
            if h in content_hashes:
                result[i] = {**msg, "content": "[Duplicate — same content as more recent call]"}
                pruned += 1
            else:
                content_hashes[h] = (i, msg.get("tool_call_id", "?"))

        # One-line summaries for old tool results
        for i in range(boundary):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            call_id = msg.get("tool_call_id", "?")
            tool_name, _args = call_id_map.get(call_id, ("unknown", ""))

            # Build informative one-liner
            lines = content.count("\n") + 1
            chars = len(content)
            snippet = content[:120].replace("\n", " ").strip()
            result[i] = {
                **msg,
                "content": (
                    f"[{tool_name}] {snippet}... ({lines} lines, {chars} chars)"
                ),
            }
            pruned += 1

        return result, pruned

    # ── Step 2-3: Head/Middle/Tail split ────────────────────────────────

    def _split_head_middle_tail(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """Split messages into head (protected), middle (compressible), tail (protected)."""
        # Head: system + first protect_first_n non-system messages
        head: List[Dict] = []
        non_system_count = 0
        idx = 0
        for idx, msg in enumerate(messages):
            head.append(msg)
            if msg.get("role") != "system":
                non_system_count += 1
            if non_system_count >= self.protect_first_n:
                idx += 1
                break

        # Tail: walk backward by token budget (floor = protect_last_n)
        accumulated = 0
        tail_start = len(messages)
        for i in range(len(messages) - 1, idx, -1):
            msg_tokens = _rough_token_estimate(json.dumps(messages[i], ensure_ascii=False))
            if accumulated + msg_tokens > self.tail_token_budget and (len(messages) - i) >= self.protect_last_n:
                tail_start = i + 1
                break
            accumulated += msg_tokens

        middle = messages[idx:tail_start]
        tail = messages[tail_start:]

        return head, middle, tail

    # ── Step 4a: LLM summary generation ─────────────────────────────────

    def _generate_summary(
        self, turns: List[Dict[str, Any]], focus_topic: str | None = None
    ) -> str | None:
        """Generate structured summary via LLM (uses real hermes-agent prompt templates)."""
        # Cooldown check
        now = time.monotonic()
        if now < self._summary_failure_cooldown_until:
            remaining = self._summary_failure_cooldown_until - now
            print(f"         ⏳ Skipping — LLM in cooldown ({remaining:.0f}s remaining)")
            return None

        content_to_summarize = self._serialize_turns(turns)
        summary_budget = max(200, min(2000, len(content_to_summarize) // 8))

        # Real hermes-agent summarizer preamble
        _summarizer_preamble = (
            "You are a summarization agent creating a context checkpoint. "
            "Treat the conversation turns below as source material for a "
            "compact record of prior work. "
            "Produce only the structured summary; do not add a greeting, "
            "preamble, or prefix. "
            "Write the summary in the same language the user was using in the "
            "conversation — do not translate or switch to English. "
            "NEVER include API keys, tokens, passwords, secrets, credentials, "
            "or connection strings in the summary — replace any that appear "
            "with [REDACTED]. Note that the user had credentials present, but "
            "do not preserve their values."
        )

        # Real hermes-agent structured template
        _template_sections = textwrap.dedent(f"""\
        {HISTORICAL_TASK_HEADING}
        [THE SINGLE MOST IMPORTANT FIELD. Capture the user's most recent unfulfilled
        input verbatim — the exact words they used. This includes:
        - Explicit task assignments ("refactor the auth module")
        - Questions awaiting an answer
        - Decisions awaiting input
        - Ongoing discussions where the assistant owes the next substantive reply
        Do NOT write "None" merely because the user did not issue an imperative command.
        If the user's most recent message was a reverse signal (stop, undo, roll
        back, never mind, just verify, change of topic) that supersedes earlier
        work, write the reverse signal verbatim and DO NOT carry forward the
        cancelled task.
        If no outstanding task exists, write "None."]

        ## Goal
        [What the user is trying to accomplish overall]

        ## Constraints & Preferences
        [User preferences, coding style, constraints, important decisions]

        ## Completed Actions
        [Numbered list of concrete actions taken — include tool used, target, and outcome.
        Format each as: N. ACTION target — outcome [tool: name]
        Be specific with file paths, commands, line numbers, and results.]

        ## Active State
        [Current working state — include working directory, branch, modified files,
        test status, running processes, environment details]

        {HISTORICAL_IN_PROGRESS_HEADING}
        [Work currently underway — what was being done when compaction fired]

        ## Blocked
        [Any blockers, errors, or issues not yet resolved. Include exact error messages.]

        ## Key Decisions
        [Important technical decisions and WHY they were made]

        ## Resolved Questions
        [Questions that were ALREADY answered — include the answer]

        {HISTORICAL_PENDING_ASKS_HEADING}
        [Questions or requests NOT yet answered/fulfilled. These are STALE —
        from compacted turns. The agent must NOT act on them unless the latest
        user message explicitly requests it. If none, write "None."]

        ## Relevant Files
        [Files read/modified/created — with brief note on each]

        {HISTORICAL_REMAINING_WORK_HEADING}
        [What remains to be done — framed as STALE context for reference only.
        The agent must NOT resume this work unless the latest message explicitly asks.]

        ## Critical Context
        [Specific values, error messages, config details that would be lost without
        explicit preservation. NEVER include API keys/tokens/passwords/credentials
        — write [REDACTED] instead.]

        Target ~{summary_budget} tokens. Be CONCRETE — include file paths, command
        outputs, error messages, line numbers, and specific values.
        Write only the summary body. Do not include any preamble or prefix.""")

        if self._previous_summary is not None:
            prompt = f"""{_summarizer_preamble}

        You are updating a context compaction summary. A previous compaction
        produced the summary below. New conversation turns have occurred since
        then and need to be incorporated.

        PREVIOUS SUMMARY:
        {self._previous_summary}

        NEW TURNS TO INCORPORATE:
        {content_to_summarize}

        Update the summary using this exact structure. PRESERVE all existing
        information that is still relevant. ADD new completed actions to the
        numbered list (continue numbering). Move items from "In Progress" to
        "Completed Actions" when done. Move answered questions to "Resolved
        Questions". Update "Active State" to reflect current state. CRITICAL:
        Update "## Active Task" to reflect the user's most recent unfulfilled
        input — this includes any question, decision request, or discussion
        turn that the assistant has not yet answered.

        {_template_sections}"""
        else:
            prompt = f"""{_summarizer_preamble}

        Create a structured checkpoint summary for the conversation after earlier
        turns are compacted. The summary should preserve enough detail for
        continuity without re-reading the original turns.

        TURNS TO SUMMARIZE:
        {content_to_summarize}

        Use this exact structure:

        {_template_sections}"""

        # Inject focus topic
        if focus_topic:
            prompt += f"""

        FOCUS TOPIC: "{focus_topic}"
        This compaction should PRIORITISE preserving all information related to
        the focus topic above. For content related to "{focus_topic}", include
        full detail. For content NOT related, summarise more aggressively."""

        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a summarization agent. Produce only the summary."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=min(4096, summary_budget + 500),
                temperature=0.3,
            )
            summary = response.choices[0].message.content
            return summary.strip() if summary else None

        except Exception as exc:
            self._last_summary_error = str(exc)
            self._last_compress_aborted = True
            self._summary_failure_cooldown_until = time.monotonic() + 60
            err_msg = str(exc)[:200]
            print(f"         ❌ LLM call failed: {err_msg}")
            return None

    # ── Step 4b: Deterministic fallback ──────────────────────────────────

    def _deterministic_fallback(self, turns: List[Dict[str, Any]]) -> str:
        """Build a local fallback summary when LLM is unavailable (mirrors real code)."""
        user_messages = [m for m in turns if m.get("role") == "user"]
        user_asks = [m.get("content", "")[:200] for m in user_messages[-5:]]
        tool_msgs = [m for m in turns if m.get("role") == "tool"]

        last_ask = user_asks[-1] if user_asks else "Unknown"

        completed: List[str] = []
        for i, msg in enumerate(tool_msgs[:12], 1):
            content = (msg.get("content") or "")[:120]
            completed.append(f"{i}. [tool call] → {content.strip()[:100]}")

        previous_note = ""
        if self._previous_summary:
            previous_note = (
                "\n\nPrevious compaction summary was present and should still be "
                "treated as background continuity context, but the latest LLM "
                "summary update failed."
            )

        return textwrap.dedent(f"""\
        {HISTORICAL_TASK_HEADING}
        User asked: {last_ask!r}

        ## Goal
        Recovered from deterministic fallback — LLM summarizer was unavailable.{previous_note}

        ## Constraints & Preferences
        Generated locally without LLM. Verify current state from recent messages.

        ## Completed Actions
        {chr(10).join(completed) if completed else "None recoverable."}

        ## Active State
        Unknown from fallback — inspect current context.

        {HISTORICAL_IN_PROGRESS_HEADING}
        Unknown from fallback.

        ## Blocked
        None recoverable.

        ## Key Decisions
        None recoverable.

        ## Resolved Questions
        None recoverable.

        {HISTORICAL_PENDING_ASKS_HEADING}
        None recoverable.

        ## Relevant Files
        Unknown from fallback.

        {HISTORICAL_REMAINING_WORK_HEADING}
        Unknown from fallback.

        ## Critical Context
        None recoverable.""")

    # ── Step 5: Build the summary message ───────────────────────────────

    def _build_summary_message(self, summary_text: str) -> Dict[str, Any]:
        """Wrap summary with the real SUMMARY_PREFIX from hermes-agent."""
        return {
            "role": "user",
            "content": f"{SUMMARY_PREFIX}\n{summary_text}",
            "_compressed_summary": True,  # metadata key from real code
        }

    # ── Helpers ─────────────────────────────────────────────────────────

    def _serialize_turns(self, turns: List[Dict[str, Any]]) -> str:
        """Serialize middle turns for the summarizer prompt."""
        lines: List[str] = []
        for msg in turns:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "[multimodal content]"
            name = msg.get("name", "")
            name_tag = f" ({name})" if name else ""

            if msg.get("tool_calls"):
                tools = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args_preview = (fn.get("arguments", "") or "")[:200]
                    tools.append(f"  → {fn.get('name', '?')}({args_preview})")
                content = (content or "") + "\n" + "\n".join(tools)

            lines.append(f"[{role}{name_tag}] {str(content)[:2000]}")
        return "\n\n".join(lines)

    def _automatic_compression_blocked(self) -> bool:
        """Guard — check cooldown and anti-thrashing (mirrors real code)."""
        # Condition 1: LLM cooldown
        remaining = self._summary_failure_cooldown_until - time.monotonic()
        if remaining > 0:
            print(f"⏳ Compression deferred — summary LLM in cooldown ({remaining:.0f}s)")
            return True

        # Condition 2: Anti-thrashing
        if self._ineffective_compression_count >= 2:
            print(f"⛔ Compression blocked — {self._ineffective_compression_count} "
                  "consecutive ineffective compressions. Try /new or force=True.")
            return True
        if self._fallback_compression_streak >= 2:
            print(f"⛔ Compression blocked — {self._fallback_compression_streak} "
                  "consecutive fallbacks. Try /new or force=True.")
            return True

        return False

    @property
    def name(self) -> str:
        return "compressor"


# ---------------------------------------------------------------------------
# Token estimation (approximate, mirrors _estimate_msg_budget_tokens)
# ---------------------------------------------------------------------------

def _rough_token_estimate(text: str) -> int:
    """Very rough token count: ~4 chars per token."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Built-in demo conversation
# ---------------------------------------------------------------------------

def build_demo_conversation() -> List[Dict[str, Any]]:
    """Build a realistic multi-turn conversation that benefits from compression."""
    return [
        {"role": "system", "content": "You are a helpful coding assistant. Use tools when needed."},
        # Turn 1 (protected head)
        {"role": "user", "content": "I need to build a REST API for a todo app using FastAPI."},
        {"role": "assistant", "content": "Great! Let me set up the project structure first.",
         "tool_calls": [
             {"id": "call_1", "function": {"name": "terminal", "arguments": '{"cmd":"mkdir -p todo-app/app && cd todo-app && poetry init"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": "Created todo-app/app directory. Poetry project initialized.\n\n[toolu_vrtx_01...]\nProject name: todo-app\nVersion: 0.1.0\nDependencies: fastapi, uvicorn, sqlalchemy, pydantic"},
        # Turn 2 (protected head)
        {"role": "user", "content": "Add alembic for database migrations too."},
        {"role": "assistant", "content": "Good idea. Let me add alembic and set up the database models.",
         "tool_calls": [
             {"id": "call_2", "function": {"name": "terminal", "arguments": '{"cmd":"cd todo-app && poetry add alembic sqlalchemy"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_2",
         "content": "Added alembic 1.13.1 and sqlalchemy 2.0.30 to dependencies.\n\n[toolu_vrtx_02...]\nLock file updated."},
        # Turn 3 (middle — will be compressed)
        {"role": "user", "content": "Let's implement the models: Todo with id, title, description, completed, created_at, updated_at."},
        {"role": "assistant", "content": "Here's the SQLAlchemy model:",
         "tool_calls": [
             {"id": "call_3", "function": {"name": "write_file", "arguments": '{"path":"todo-app/app/models.py","content":"from sqlalchemy import Column, Integer, String, Boolean, DateTime\\nfrom sqlalchemy.ext.declarative import declarative_base\\n\\nBase = declarative_base()\\n\\nclass Todo(Base):\\n    __tablename__ = \\"todos\\"\\n    id = Column(Integer, primary_key=True)\\n    title = Column(String(200))\\n    description = Column(String(1000))\\n    completed = Column(Boolean, default=False)\\n    created_at = Column(DateTime, default=datetime.utcnow)\\n    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_3",
         "content": "Wrote 15 lines to todo-app/app/models.py"},
        # Turn 4 (middle — will be compressed)
        {"role": "user", "content": "Now write the CRUD endpoints — create, read all, read one, update, delete. Use proper HTTP status codes."},
        {"role": "assistant", "content": "Here's the CRUD router:",
         "tool_calls": [
             {"id": "call_4", "function": {"name": "write_file", "arguments": '{"path":"todo-app/app/main.py","content":"from fastapi import FastAPI, HTTPException\\nfrom .models import Todo, Base\\nfrom sqlalchemy import create_engine\\nfrom sqlalchemy.orm import sessionmaker\\n\\napp = FastAPI()\\n\\n@app.post(\\"/todos\\")\\nasync def create_todo(todo: TodoCreate):\\n    ..."}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_4",
         "content": "Wrote 80 lines to todo-app/app/main.py\nEndpoints: POST /todos, GET /todos, GET /todos/{id}, PUT /todos/{id}, DELETE /todos/{id}"},
        # Turn 5 (middle — will be compressed)
        {"role": "user", "content": "Add input validation — title should be 1-200 chars, description max 1000."},
        {"role": "assistant", "content": "Let me add Pydantic validation schemas:",
         "tool_calls": [
             {"id": "call_5", "function": {"name": "write_file", "arguments": '{"path":"todo-app/app/schemas.py","content":"from pydantic import BaseModel, Field\\n\\nclass TodoCreate(BaseModel):\\n    title: str = Field(min_length=1, max_length=200)\\n    description: str = Field(default=\\"\\", max_length=1000)"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_5",
         "content": "Wrote 20 lines to todo-app/app/schemas.py"},
        # Turn 6 (middle — will be compressed)
        {"role": "user", "content": "The tests are failing. Here's the error:\n```\nE   sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: todos\n```"},
        {"role": "assistant", "content": "You need to create the tables before running tests. Let me add database initialization:",
         "tool_calls": [
             {"id": "call_6", "function": {"name": "write_file", "arguments": '{"path":"todo-app/app/database.py","content":"from sqlalchemy import create_engine\\nfrom sqlalchemy.orm import sessionmaker\\nfrom .models import Base\\n\\nengine = create_engine(\\"sqlite:///./todos.db\\")\\nSessionLocal = sessionmaker(bind=engine)\\n\\ndef init_db():\\n    Base.metadata.create_all(bind=engine)"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_6",
         "content": "Wrote 12 lines to todo-app/app/database.py"},
        # Turn 7 (middle — will be compressed)
        {"role": "user", "content": "Let's add some error handling — catch IntegrityError for duplicate entries, and add proper 404 messages."},
        {"role": "assistant", "content": "I'll update main.py with error handlers.",
         "tool_calls": [
             {"id": "call_7", "function": {"name": "read_file", "arguments": '{"path":"todo-app/app/main.py"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_7",
         "content": "1: from fastapi import FastAPI, HTTPException\n2: ...\n(a lot of content repeated from earlier writes — 80 lines total)"},
        # Turn 8 (protected tail — recent)
        {"role": "user", "content": "Actually, can we switch to async SQLAlchemy? I want to use asyncpg instead of sqlite."},
        {"role": "assistant", "content": "Let me check what we'd need to change...",
         "tool_calls": [
             {"id": "call_8", "function": {"name": "terminal", "arguments": '{"cmd":"cd todo-app && poetry add asyncpg sqlalchemy[asyncio]"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_8",
         "content": "Added asyncpg 0.29.0. SQLAlchemy asyncio extension already present (2.0.30).\n\nWarning: sqlalchemy 2.0.30 requires greenlet which is not installed."},
        # Turn 9 (protected tail — most recent)
        {"role": "user", "content": "Before converting everything, let me see what files we have right now."},
        {"role": "assistant", "content": "Sure, here's the current file listing:",
         "tool_calls": [
             {"id": "call_9", "function": {"name": "terminal", "arguments": '{"cmd":"cd todo-app && find . -type f | sort"}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_9",
         "content": "./app/__init__.py\n./app/database.py\n./app/main.py\n./app/models.py\n./app/schemas.py\n./pyproject.toml"},
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_messages_summary(messages: List[Dict[str, Any]], label: str) -> None:
    """Pretty-print a high-level view of the message list."""
    print(f"\n{'='*60}")
    print(f"  {label}: {len(messages)} messages")
    print(f"{'='*60}")
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "[multimodal]"
        preview = str(content)[:100].replace("\n", " ")
        compressed = " 🗜️" if msg.get("_compressed_summary") else ""
        tools = f" [{len(msg.get('tool_calls', []))} tool calls]" if msg.get("tool_calls") else ""
        print(f"  [{i:2d}] {role:11s}{compressed}{tools} | {preview}...")


def run_demo(api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-v4-pro"):
    """Run full demo with built-in conversation."""
    print("╔══════════════════════════════════════════════════╗")
    print("║   Mini Context Compressor — Demo                 ║")
    print("║   Using real hermes-agent prompt templates       ║")
    print("╚══════════════════════════════════════════════════╝")

    messages = build_demo_conversation()
    print_messages_summary(messages, "ORIGINAL CONVERSATION")

    compressor = MiniCompressor(
        context_length=128_000,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
    # Use a smaller tail budget so the demo actually compresses something
    compressor.tail_token_budget = 3000
    compressor.protect_first_n = 2
    compressor.protect_last_n = 4

    # Force token count above threshold to trigger compression
    compressor.last_prompt_tokens = 100_000  # simulate high usage

    if compressor.should_compress():
        print("\n⚡ Token threshold exceeded — triggering compression...\n")
    else:
        print("\n✅ Token usage is healthy — no compression needed.\n")
        return

    # Simulate auto-compression (force=False)
    compressed = compressor.compress(messages, force=False)
    print_messages_summary(compressed, "COMPRESSED CONVERSATION")

    # Show the summary message
    for msg in compressed:
        if msg.get("_compressed_summary"):
            print(f"\n{'='*60}")
            print("  COMPRESSION SUMMARY (injected as user message):")
            print(f"{'='*60}")
            print(msg["content"][:2000])
            print(f"... ({len(msg['content'])} total chars)")
            break

    # Simulate second compression (iterative update)
    print("\n\n─── Second Compression (iterative update) ───\n")
    tail_messages = [
        {"role": "user", "content": "OK let's go with asyncpg. Convert all the database code."},
        {"role": "assistant", "content": "Converting to async SQLAlchemy...",
         "tool_calls": [
             {"id": "call_10", "function": {"name": "write_file",
              "arguments": '{"path":"todo-app/app/database.py","content":"..."}'}},
         ]},
        {"role": "tool", "tool_call_id": "call_10",
         "content": "Wrote 25 lines to database.py — now uses async engine + async sessionmaker"},
    ]
    extended = compressed + tail_messages
    compressor.last_prompt_tokens = 100_000  # still high
    compressed2 = compressor.compress(extended, force=False)
    print_messages_summary(compressed2, "AFTER SECOND COMPRESSION")

    print("\n✅ Demo complete!")


def main():
    parser = argparse.ArgumentParser(
        description="Mini Context Compressor — demo of hermes-agent's compression engine"
    )
    parser.add_argument(
        "--input", "-i", type=str,
        help="Path to a JSON array of messages to compress",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Interactive mode — type messages manually",
    )
    parser.add_argument(
        "--stress", action="store_true",
        help="Stress test: multi-round compression on built-in conversation",
    )
    parser.add_argument(
        "--model", "-m", type=str, default="deepseek-v4-pro",
        help="Model for summarization (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--api-key", type=str, default=os.environ.get("OPENAI_API_KEY"),
        help="OpenAI-compatible API key",
    )
    parser.add_argument(
        "--base-url", type=str, default=os.environ.get("OPENAI_BASE_URL"),
        help="OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without LLM call — shows head/middle/tail split only",
    )
    parser.add_argument(
        "--tail-budget", type=int, default=3000,
        help="Token budget for tail protection (default: 3000; lower = more compression)",
    )
    parser.add_argument(
        "--protect-last", type=int, default=4,
        help="Minimum tail message count to protect (default: 4)",
    )
    parser.add_argument(
        "--protect-first", type=int, default=2,
        help="Head message count to protect beyond system prompt (default: 2)",
    )
    args = parser.parse_args()

    if args.interactive:
        print("Interactive mode — not implemented yet. Use --help for options.")
        return

    if args.stress:
        print("Stress test — not implemented yet. Use --help for options.")
        return

    if args.input:
        with open(args.input) as f:
            messages = json.load(f)
        compressor = MiniCompressor(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
        )
        compressor.last_prompt_tokens = 100_000
        # Auto-fit tail budget to produce visible split when not explicitly set.
        # Default is low enough that the short example messages actually produce a
        # middle region — the hermes-agent default of ~20K is realistic for real
        # conversations but would swallow these tiny demo messages entirely.
        if args.tail_budget == 3000:
            total_tokens = sum(
                _rough_token_estimate(json.dumps(m, ensure_ascii=False))
                for m in messages
            )
            # Target: tail ≈ 30% of total, but at least enough to cover protect_last_n
            compressor.tail_token_budget = max(300, int(total_tokens * 0.30))
        else:
            compressor.tail_token_budget = args.tail_budget
        compressor.protect_last_n = args.protect_last
        compressor.protect_first_n = args.protect_first
        print_messages_summary(messages, "INPUT")
        if args.dry_run:
            head, middle, tail = compressor._split_head_middle_tail(messages)
            print(f"\nHead: {len(head)} | Middle: {len(middle)} | Tail: {len(tail)}")
            print(f"(Dry run — no LLM call, tail_budget={compressor.tail_token_budget})")
        else:
            result = compressor.compress(messages)
            print_messages_summary(result, "OUTPUT")
        return

    # Default: run the built-in demo
    run_demo(api_key=args.api_key, base_url=args.base_url, model=args.model)


if __name__ == "__main__":
    main()
