"""Render the three agent prompts from their template files + live state."""

from __future__ import annotations

from ..models import Task
from ..prompts import render


def _bullets(items: list[str], empty: str = "(none)") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def render_idea_prompt(template: str, *, goal: str, seed_ideas: list[str],
                       constraints: list[str], tasks: dict[str, Task],
                       max_tasks: int) -> str:
    by_status: dict[str, list[str]] = {}
    for t in tasks.values():
        by_status.setdefault(t.status, []).append(f"{t.id} {t.title}")
    state_lines = []
    for status in ("done", "running", "ready", "backlog", "blocked"):
        for line in by_status.get(status, [])[:50]:
            state_lines.append(f"[{status}] {line}")
    return render(template, {
        "goal": goal,
        "seed_ideas": _bullets(seed_ideas),
        "constraints": _bullets(constraints),
        "task_state": "\n".join(state_lines) or "(no tasks yet — this is the first proposal)",
        "max_tasks": str(max_tasks),
    })


def render_build_prompt(template: str, *, task: Task, constraints: list[str]) -> str:
    feedback = ""
    if task.errors:
        recent = task.errors[-2:]
        feedback = ("Previous attempt(s) on this task FAILED. Fix the cause this time:\n"
                    + "\n---\n".join(recent))
    return render(template, {
        "title": task.title,
        "detail": task.detail,
        "verify": task.verify,
        "constraints": _bullets(constraints),
        "feedback": feedback,
    })


def render_review_prompt(template: str, *, task: Task, diff: str,
                         constraints: list[str]) -> str:
    return render(template, {
        "title": task.title,
        "detail": task.detail,
        "verify": task.verify,
        "diff": diff or "(empty diff)",
        "constraints": _bullets(constraints),
    })
