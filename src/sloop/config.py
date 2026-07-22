"""Load and validate .sloop/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    goal: str = ""
    seed_ideas: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    max_concurrent: int = 2
    test_command: str | None = None  # full suite, run during wrap-up
    tick_seconds: float = 2.0
    verify_timeout_seconds: int = 600
    max_attempts: int = 3  # total tries per task before blocked (spec: retry <=2)
    max_tasks_per_proposal: int = 5

    # budget
    max_cost_usd: float | None = None
    max_wall_clock_min: float = 240.0
    wrap_up_at: float = 0.8

    # agents
    model: str | None = None  # None -> Claude Code default
    idea_model: str | None = None
    build_model: str | None = None
    review_model: str | None = None
    permission_mode: str = "bypassPermissions"  # build agent, inside its own worktree
    max_turns: int = 80
    job_timeout_seconds: int = 1800  # hard cap per model call

    def model_for(self, agent: str) -> str | None:
        return {
            "idea": self.idea_model,
            "build": self.build_model,
            "review": self.review_model,
        }.get(agent) or self.model


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    flat: dict = {}
    for section in ("budget", "agents"):
        flat.update(data.pop(section, {}))
    flat.update(data)

    cfg = Config()
    for key, value in flat.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    if not cfg.goal:
        raise ValueError(f"config {path}: 'goal' is required")
    if not (0.1 <= cfg.wrap_up_at <= 1.0):
        raise ValueError("wrap_up_at must be between 0.1 and 1.0")
    return cfg
