"""Executor seam: the orchestrator never imports the Claude Agent SDK directly.

AgentSDKExecutor is the real thing (Claude Code as a library).
FakeExecutor lives in fake.py for smoke runs and tests.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("sloop")

LIMIT_RE = re.compile(r"usage limit|rate.?limit(ed)?\b.*reset|limit reached", re.IGNORECASE)

READONLY_TOOLS = ["Read", "Grep", "Glob"]
BUILD_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "TodoWrite"]


def _brief(tool_input) -> str:
    """One-line summary of a tool call's input for the live log."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "pattern", "file_path", "path", "url", "description", "prompt"):
        if tool_input.get(key):
            return str(tool_input[key])[:100]
    try:
        return json.dumps(tool_input)[:100]
    except (TypeError, ValueError):
        return ""


@dataclass
class AgentResult:
    output: str
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    is_error: bool = False
    limit_hit: bool = False


class Executor(Protocol):
    async def run(
        self,
        prompt: str,
        *,
        agent: str,
        cwd: Path,
        model: str | None,
        permission_mode: str,
        max_turns: int,
    ) -> AgentResult: ...


class AgentSDKExecutor:
    """Runs each job as a fresh Claude Agent SDK query() — one call, fresh context."""

    async def run(
        self,
        prompt: str,
        *,
        agent: str,
        cwd: Path,
        model: str | None,
        permission_mode: str,
        max_turns: int,
    ) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions, query  # lazy: keeps fake mode SDK-free

        kwargs: dict = {
            "cwd": str(cwd),
            "max_turns": max_turns,
        }
        if agent == "build":
            kwargs["allowed_tools"] = BUILD_TOOLS
            kwargs["permission_mode"] = permission_mode
        else:  # idea / review explore read-only
            kwargs["allowed_tools"] = READONLY_TOOLS
            kwargs["disallowed_tools"] = ["Write", "Edit", "Bash", "NotebookEdit"]
        if model:
            kwargs["model"] = model

        options = ClaudeAgentOptions(**kwargs)

        started = time.monotonic()
        text_parts: list[str] = []
        result_msg = None
        try:
            async for message in query(prompt=prompt, options=options):
                name = type(message).__name__
                if name == "AssistantMessage":
                    for block in getattr(message, "content", None) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
                        tool = getattr(block, "name", None)
                        if tool and type(block).__name__ == "ToolUseBlock":
                            log.info("  [%s] %s %s", agent, tool,
                                     _brief(getattr(block, "input", None)))
                elif name == "ResultMessage":
                    result_msg = message
        except Exception as e:
            output = f"executor error: {e}"
            return AgentResult(
                output=output,
                duration_ms=int((time.monotonic() - started) * 1000),
                is_error=True,
                limit_hit=bool(LIMIT_RE.search(str(e))),
            )

        final_text = getattr(result_msg, "result", None)
        output = final_text if isinstance(final_text, str) and final_text.strip() else "\n".join(text_parts)
        is_error = bool(getattr(result_msg, "is_error", False))
        return AgentResult(
            output=output,
            cost_usd=float(getattr(result_msg, "total_cost_usd", None) or 0.0),
            duration_ms=int(getattr(result_msg, "duration_ms", None)
                            or (time.monotonic() - started) * 1000),
            num_turns=int(getattr(result_msg, "num_turns", None) or 0),
            is_error=is_error,
            limit_hit=is_error and bool(LIMIT_RE.search(output)),
        )
