"""DemoFakeExecutor — full end-to-end loop without any model calls.

Used by `sloop activate --fake` to smoke-test the machinery:
idea proposes one trivial task, build actually creates the file,
review approves, second idea round proposes nothing -> wrap-up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import AgentResult

FAKE_MARKER = "FAKE_CREATE:"


class DemoFakeExecutor:
    def __init__(self) -> None:
        self.idea_rounds = 0

    async def run(self, prompt: str, *, agent: str, cwd: Path, model: str | None,
                  permission_mode: str, max_turns: int) -> AgentResult:
        if agent == "idea":
            self.idea_rounds += 1
            if self.idea_rounds > 1:
                return AgentResult(output="```json\n[]\n```", cost_usd=0.001, num_turns=1)
            tasks = [{
                "title": "Create hello.txt",
                "detail": ("Create a file named hello.txt in the repository root "
                           "containing the line 'hello from sloop'. "
                           f"{FAKE_MARKER} hello.txt"),
                "verify": "test -f hello.txt && grep -q 'hello from sloop' hello.txt",
                "deps": [],
            }]
            return AgentResult(output="```json\n" + json.dumps(tasks) + "\n```",
                               cost_usd=0.001, num_turns=1)

        if agent == "build":
            m = re.search(rf"{FAKE_MARKER}\s*(\S+)", prompt)
            filename = m.group(1) if m else "hello.txt"
            (cwd / filename).write_text("hello from sloop\n", encoding="utf-8")
            return AgentResult(output=f"created {filename}", cost_usd=0.002, num_turns=3)

        if agent == "review":
            return AgentResult(output='```json\n{"verdict": "approve", "reasons": []}\n```',
                               cost_usd=0.001, num_turns=1)

        return AgentResult(output="unknown agent", is_error=True)
