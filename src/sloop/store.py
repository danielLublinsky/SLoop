"""Disk is the state. Everything lives as plain JSON/JSONL under .sloop/.

Writes are atomic (tmp file + rename). Jobs and events are append-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import Job, Task, now_iso

log = logging.getLogger("sloop")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)  # <repo>/.sloop
        self.tasks_dir = self.root / "tasks"
        self.jobs_dir = self.root / "jobs"
        self.prompts_dir = self.root / "prompts"
        self.snapshots_dir = self.prompts_dir / ".snapshots"
        self.events_path = self.root / "events.jsonl"
        self.kill_path = self.root / "KILL"
        self.session_path = self.root / "SESSION.md"

    def ensure_dirs(self) -> None:
        for d in (self.tasks_dir, self.jobs_dir, self.prompts_dir, self.snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- atomic write helper ------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # -- tasks ---------------------------------------------------------------

    def load_tasks(self) -> dict[str, Task]:
        tasks: dict[str, Task] = {}
        for f in sorted(self.tasks_dir.glob("T*.json")):
            try:
                tasks[f.stem] = Task.from_dict(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:  # corrupt task file: skip, don't crash the loop
                log.warning("skipping unreadable task file %s: %s", f, e)
        return tasks

    def save_task(self, task: Task) -> None:
        task.updated_ts = now_iso()
        self._atomic_write(self.tasks_dir / f"{task.id}.json", json.dumps(task.to_dict(), indent=2))

    def next_task_id(self, existing: dict[str, Task]) -> str:
        nums = [int(m.group(1)) for t in existing for m in [re.match(r"T(\d+)$", t)] if m]
        for f in self.tasks_dir.glob("T*.json"):
            m = re.match(r"T(\d+)$", f.stem)
            if m:
                nums.append(int(m.group(1)))
        return f"T{(max(nums) + 1 if nums else 1):03d}"

    # -- jobs (append-only) ----------------------------------------------------

    def append_job(self, job: Job) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.jobs_dir / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(job.to_dict()) + "\n")

    # -- events (append-only) ----------------------------------------------------

    def append_event(self, event: str, **data) -> None:
        rec = {"ts": now_iso(), "event": event, **data}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        extra = " ".join(f"{k}={v}" for k, v in data.items() if k != "detail")
        log.info("%s %s", event, extra)

    # -- prompt snapshots ---------------------------------------------------------

    def snapshot_prompt(self, prompt_hash: str, text: str) -> None:
        path = self.snapshots_dir / f"{prompt_hash}.md"
        if not path.exists():
            self._atomic_write(path, text)

    def load_prompt_snapshot(self, prompt_hash: str) -> str | None:
        path = self.snapshots_dir / f"{prompt_hash}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    # -- guards ----------------------------------------------------------------

    def kill_requested(self) -> bool:
        return self.kill_path.exists()

    def clear_kill(self) -> None:
        self.kill_path.unlink(missing_ok=True)
