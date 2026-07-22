"""Core data types. Plain dataclasses, serialized as JSON on disk."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# Task lifecycle: backlog -> ready -> running -> done | blocked
STATUSES = ("backlog", "ready", "running", "done", "blocked")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    """A unit of work. `detail` is standalone; `verify` is a shell command."""

    id: str
    title: str
    detail: str
    verify: str
    deps: list[str] = field(default_factory=list)
    status: str = "backlog"
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    prompt_hash: str | None = None  # pinned on first dispatch
    branch: str | None = None
    created_ts: str = field(default_factory=now_iso)
    updated_ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Job:
    """One model call. Append-only; never mutated after being written."""

    id: str
    task_id: str | None
    agent: str  # idea | build | review
    prompt_file: str
    prompt_hash: str
    input: str
    output: str
    cost_usd: float
    duration_ms: int
    num_turns: int
    verdict: str  # ok | verify_pass | verify_fail | review_approve | review_reject | error | limit
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


def new_job_id() -> str:
    return "J" + uuid.uuid4().hex[:10]
