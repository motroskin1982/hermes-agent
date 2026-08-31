"""Per-session runtime circuit breakers and continuation checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from agent.display import _detect_tool_failure
from hermes_constants import get_hermes_home
from utils import atomic_json_write

# This is a *lifetime* input-token counter, not the provider context-window
# limit.  Context is compacted separately by the conversation compressor, so
# a small lifetime cap causes healthy long-running gateway conversations to be
# terminated even when their active prompt is safely within the model window.
#
# Keep an escape hatch for operators who need a stricter budget, but use a
# production-safe default.  The previous 90k default routinely stopped active
# Telegram threads after only a few dozen turns and leaked an internal
# checkpoint status to users.
def _input_token_checkpoint_threshold() -> int:
    raw = os.environ.get("HERMES_INPUT_TOKEN_CHECKPOINT_THRESHOLD", "1000000")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1_000_000
    # Below 90k recreates the known premature-stop failure; an upper bound
    # retains a finite guardrail if an environment is accidentally malformed.
    return min(max(value, 90_000), 10_000_000)


INPUT_TOKEN_CHECKPOINT_THRESHOLD = _input_token_checkpoint_threshold()
IDENTICAL_FAILURE_LIMIT = 3
TOOL_FAILURE_LIMIT = 6
CHECKPOINT_SCHEMA = "hermes.runtime-continuation.v1"


def _safe_session_component(session_id: str) -> str:
    raw = str(session_id or "session")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")[:80] or "session"
    if cleaned == raw:
        return cleaned
    return f"{cleaned}-{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def _failure_fingerprint(tool_name: str, result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, sort_keys=True, default=str)
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        text = " ".join(str(text).split())
    return hashlib.sha256(f"{tool_name}\0{text}".encode("utf-8", errors="replace")).hexdigest()


class SessionCircuitBreaker:
    """Failure counters owned by exactly one AIAgent session."""

    def __init__(self) -> None:
        self.consecutive_fingerprint: str | None = None
        self.consecutive_count = 0
        self.tool_failures: Counter[str] = Counter()
        self.tripped: dict[str, Any] | None = None

    def observe(self, tool_name: str, result: Any) -> dict[str, Any] | None:
        failed, _ = _detect_tool_failure(tool_name, result if isinstance(result, str) else str(result))
        # The display classifier intentionally knows only selected tool shapes.
        # Circuit breaking must also cover plugin/MCP tools' conventional JSON.
        if not failed:
            try:
                payload = json.loads(result) if isinstance(result, str) else result
                if isinstance(payload, dict):
                    failed = bool(
                        payload.get("error")
                        or payload.get("success") is False
                        or payload.get("status") in {"error", "failed", "failure"}
                    )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if not failed:
            self.consecutive_fingerprint = None
            self.consecutive_count = 0
            # The per-tool limit is a consecutive-runaway guard, not a
            # lifetime error budget. A later successful call proves the tool
            # recovered; retaining old failures made ordinary RED/GREEN tests
            # and diagnostics pause after six unrelated failures.
            self.tool_failures[tool_name] = 0
            return None

        fingerprint = _failure_fingerprint(tool_name, result)
        if fingerprint == self.consecutive_fingerprint:
            self.consecutive_count += 1
        else:
            self.consecutive_fingerprint = fingerprint
            self.consecutive_count = 1
        self.tool_failures[tool_name] += 1

        if self.consecutive_count >= IDENTICAL_FAILURE_LIMIT:
            self.tripped = {
                "kind": "identical_consecutive_failures",
                "tool": tool_name,
                "count": self.consecutive_count,
                "failure_fingerprint": fingerprint,
            }
        elif self.tool_failures[tool_name] >= TOOL_FAILURE_LIMIT:
            self.tripped = {
                "kind": "tool_failure_limit",
                "tool": tool_name,
                "count": self.tool_failures[tool_name],
                "failure_fingerprint": fingerprint,
            }
        return self.tripped


def observe_tool_messages(breaker: SessionCircuitBreaker, messages: list[dict], start: int) -> dict[str, Any] | None:
    for message in messages[start:]:
        if isinstance(message, dict) and message.get("role") == "tool":
            tripped = breaker.observe(str(message.get("name") or "unknown"), message.get("content", ""))
            if tripped:
                return tripped
    return None


def persist_runtime_checkpoint(agent: Any, messages: list[dict], reason: str, details: dict[str, Any]) -> dict[str, Any]:
    """Persist non-secret continuation metadata; transcript content stays in SessionDB."""
    session_id = str(getattr(agent, "session_id", "") or "session")
    last = messages[-1] if messages and isinstance(messages[-1], dict) else {}
    checkpoint = {
        "schema": CHECKPOINT_SCHEMA,
        "created_at": time.time(),
        "session_id": session_id,
        "source": str(getattr(agent, "platform", "") or "cli"),
        "reason": reason,
        "continuation_required": True,
        "safe_continuation": {
            "message_count": len(messages),
            "last_role": last.get("role"),
            "last_tool_call_id": last.get("tool_call_id"),
            "input_tokens": int(getattr(agent, "session_input_tokens", 0) or 0),
            "api_calls": int(getattr(agent, "session_api_calls", 0) or 0),
        },
        "failure_summary": details if reason == "runtime_circuit_breaker" else None,
    }
    path = get_hermes_home() / "runtime-checkpoints" / f"{_safe_session_component(session_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, checkpoint)
    checkpoint["checkpoint_path"] = str(path)
    return checkpoint
