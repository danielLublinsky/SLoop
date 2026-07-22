"""The orchestrator is code, not a model.

tick:
  1. guards        kill-switch / budget >= wrap_up_at / subscription limit
  2. harvest       collect finished build pipelines
  3. reconcile     backlog -> ready where deps are done
  4. dispatch      up to max_concurrent ready tasks -> build pipeline
  5. refill        queue empty -> idea agent proposes next tasks
  6. sleep, repeat

Each build pipeline: worktree -> build agent -> commit -> verify
-> (serialized) rebase -> re-verify -> review -> squash-merge -> done.
Failure: append error to task, retry with the error fed back, then blocked.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import time
from pathlib import Path

from . import worktree as wt
from .agents.base import AgentResult, Executor
from .agents.parsing import parse_proposed_tasks, parse_review_verdict
from .agents.render import render_build_prompt, render_idea_prompt, render_review_prompt
from .config import Config
from .models import Job, Task, new_job_id, now_iso
from .prompts import load_prompt
from .store import Store
from .usage import UsageGauge

log = logging.getLogger("sloop")

DRAIN_TIMEOUT_S = 15 * 60
MAX_EMPTY_REFILLS = 2
HEARTBEAT_S = 30


class Orchestrator:
    def __init__(self, repo: Path, config: Config, store: Store, executor: Executor):
        self.repo = repo
        self.cfg = config
        self.store = store
        self.executor = executor
        self.gauge = UsageGauge(config.max_cost_usd, config.max_wall_clock_min)
        self.tasks: dict[str, Task] = {}
        self.running: dict[str, asyncio.Task] = {}
        self.merge_lock = asyncio.Lock()
        self.base_branch = "main"
        self.stop_reason: str | None = None
        self.hard_stop = False
        self.session_done: list[str] = []
        self._empty_refills = 0
        self._sigints = 0

    # ------------------------------------------------------------------ setup

    async def setup(self) -> None:
        self.store.ensure_dirs()
        self.store.clear_kill()
        await wt.ensure_excludes(self.repo)  # before the clean-tree check: .sloop/ must not count as dirty
        self.base_branch = await wt.ensure_repo_ready(self.repo)
        await wt.prune_all_worktrees(self.repo)
        self.tasks = self.store.load_tasks()
        for task in self.tasks.values():  # crash recovery: a crash costs one tick
            if task.status == "running":
                task.status = "ready"
                self.store.save_task(task)
        self._repair_verifies()
        self.store.append_event("activated", repo=str(self.repo), base=self.base_branch,
                                tasks=len(self.tasks), goal=self.cfg.goal[:120])

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal)
            except NotImplementedError:
                pass

    def _repair_verifies(self) -> None:
        """Heal tasks stored with a hallucinated absolute-cd verify prefix.
        A task blocked only because its verify could never run gets its
        attempts back and re-enters the queue via reconcile."""
        from .agents.parsing import CD_ABS_PREFIX_RE
        for task in self.tasks.values():
            cleaned = CD_ABS_PREFIX_RE.sub("", task.verify)
            if cleaned and cleaned != task.verify:
                task.verify = cleaned
                if task.status == "blocked":
                    task.status = "backlog"  # reconcile re-promotes when deps allow
                    task.attempts = 0
                    task.errors.append("verify repaired (stripped absolute cd prefix); attempts reset")
                self.store.save_task(task)
                self.store.append_event("verify_repaired", task=task.id, verify=cleaned)

    def _on_signal(self) -> None:
        self._sigints += 1
        if self._sigints == 1:
            self.stop_reason = self.stop_reason or "interrupted (Ctrl-C) — wrapping up"
            self.store.append_event("stop_requested", why="signal")
        else:
            self.hard_stop = True
            for t in self.running.values():
                t.cancel()
            self.store.append_event("hard_stop", why="second signal")

    # ------------------------------------------------------------------- loop

    async def run(self) -> None:
        await self.setup()
        try:
            while True:
                if self._check_guards():
                    break
                self._harvest()
                self._reconcile()
                self._dispatch()
                if await self._maybe_refill():
                    break
                await asyncio.sleep(self.cfg.tick_seconds)
        finally:
            await self.wrap_up(self.stop_reason or "stopped")

    def _check_guards(self) -> bool:
        if self.stop_reason:
            return True
        if self.store.kill_requested():
            self.stop_reason = "kill-switch file present"
            return True
        if self.gauge.limit_hit:
            self.stop_reason = "subscription usage limit reached"
            return True
        if self.gauge.fraction() >= self.cfg.wrap_up_at:
            self.stop_reason = f"budget at {self.gauge.fraction() * 100:.0f}% (>= {self.cfg.wrap_up_at * 100:.0f}%)"
            return True
        return False

    def _harvest(self) -> None:
        for task_id in [tid for tid, fut in self.running.items() if fut.done()]:
            fut = self.running.pop(task_id)
            exc = fut.exception() if not fut.cancelled() else None
            if fut.cancelled() or exc:
                task = self.tasks.get(task_id)
                if task and task.status == "running":
                    self._fail_task(task, f"pipeline crashed: {exc or 'cancelled'}")

    def _reconcile(self) -> None:
        done_ids = {t.id for t in self.tasks.values() if t.status == "done"}
        for task in self.tasks.values():
            if task.status == "backlog" and all(d in done_ids for d in task.deps):
                blocked_deps = [d for d in task.deps if self.tasks.get(d) and self.tasks[d].status == "blocked"]
                if blocked_deps:
                    self._set_status(task, "blocked",
                                     error=f"dependency blocked: {', '.join(blocked_deps)}")
                else:
                    self._set_status(task, "ready")
            elif task.status == "backlog":
                # a dep is blocked forever -> route around it
                if any(self.tasks.get(d) and self.tasks[d].status == "blocked" for d in task.deps):
                    self._set_status(task, "blocked", error="dependency blocked")

    def _dispatch(self) -> None:
        if self.stop_reason:
            return
        ready = sorted((t for t in self.tasks.values() if t.status == "ready"), key=lambda t: t.id)
        for task in ready:
            if len(self.running) >= self.cfg.max_concurrent:
                break
            self._set_status(task, "running")
            task.attempts += 1
            self.store.save_task(task)
            self.running[task.id] = asyncio.create_task(self._pipeline(task), name=f"pipeline-{task.id}")
            self.store.append_event("dispatched", task=task.id, attempt=task.attempts, title=task.title)

    async def _maybe_refill(self) -> bool:
        """Returns True when the loop should end (idea agent is out of ideas)."""
        if self.stop_reason or self.running:
            return False
        if any(t.status in ("ready", "backlog") for t in self.tasks.values()):
            return False
        created = await self._refill()
        if created:
            self._empty_refills = 0
            return False
        self._empty_refills += 1
        if self._empty_refills >= MAX_EMPTY_REFILLS:
            self.stop_reason = "idea agent has no further proposals — goal likely complete"
            return True
        return False

    # ------------------------------------------------------------------ agents

    async def _run_agent(self, agent: str, prompt: str, prompt_file: str,
                         prompt_hash: str, cwd: Path, task_id: str | None) -> AgentResult:
        job = asyncio.create_task(self.executor.run(
            prompt,
            agent=agent,
            cwd=cwd,
            model=self.cfg.model_for(agent),
            permission_mode=self.cfg.permission_mode,
            max_turns=self.cfg.max_turns,
        ))
        started = time.monotonic()
        result: AgentResult | None = None
        try:
            while result is None:
                done, _ = await asyncio.wait({job}, timeout=HEARTBEAT_S)
                elapsed = int(time.monotonic() - started)
                if done:
                    exc = job.exception()
                    result = (AgentResult(output=f"executor crashed: {exc}", is_error=True)
                              if exc else job.result())
                elif elapsed >= self.cfg.job_timeout_seconds:
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                    result = AgentResult(
                        output=f"{agent} agent timed out after {elapsed}s (job_timeout_seconds)",
                        is_error=True, duration_ms=elapsed * 1000)
                else:
                    log.info("agent working… agent=%s task=%s elapsed=%ds", agent, task_id or "-", elapsed)
        except asyncio.CancelledError:
            job.cancel()
            raise
        self.gauge.add_job(result.cost_usd)
        if result.limit_hit:
            self.gauge.limit_hit = True
        verdict = "limit" if result.limit_hit else ("error" if result.is_error else "ok")
        self.store.append_job(Job(
            id=new_job_id(), task_id=task_id, agent=agent,
            prompt_file=prompt_file, prompt_hash=prompt_hash,
            input=prompt, output=result.output,
            cost_usd=result.cost_usd, duration_ms=result.duration_ms,
            num_turns=result.num_turns, verdict=verdict,
        ))
        self.store.append_event("job_finished", agent=agent, task=task_id or "-",
                                cost=f"${result.cost_usd:.3f}", turns=result.num_turns,
                                verdict=verdict, usage=self.gauge.summary())
        return result

    async def _refill(self) -> int:
        template, phash = load_prompt(self.store.prompts_dir / "idea.md")
        prompt = render_idea_prompt(
            template, goal=self.cfg.goal, seed_ideas=self.cfg.seed_ideas,
            constraints=self.cfg.constraints, tasks=self.tasks,
            max_tasks=self.cfg.max_tasks_per_proposal,
        )
        self.store.append_event("refill_started")
        result = await self._run_agent("idea", prompt, "prompts/idea.md", phash, self.repo, None)
        if result.is_error:
            self.store.append_event("refill_failed", error=result.output[:300])
            return 0
        proposals, warnings = parse_proposed_tasks(result.output, self.cfg.max_tasks_per_proposal)
        for w in warnings:
            self.store.append_event("proposal_warning", warning=w)

        new_ids: list[str] = []
        for item in proposals:
            task_id = self.store.next_task_id(self.tasks)
            deps: list[str] = []
            for d in item["deps"]:
                if isinstance(d, int) and 0 <= d < len(new_ids):
                    deps.append(new_ids[d])
                elif isinstance(d, str) and d in self.tasks:
                    deps.append(d)
            task = Task(id=task_id, title=item["title"], detail=item["detail"],
                        verify=item["verify"], deps=deps)
            self.tasks[task_id] = task
            self.store.save_task(task)
            new_ids.append(task_id)
            self.store.append_event("task_created", task=task_id, title=task.title,
                                    verify=task.verify, deps=",".join(deps) or "-")
        return len(new_ids)

    # --------------------------------------------------------------- pipeline

    async def _pipeline(self, task: Task) -> None:
        branch = wt.branch_name(task.id)
        task.branch = branch
        worktree_path: Path | None = None
        try:
            worktree_path = await wt.create_worktree(self.repo, task.id, self.base_branch)

            # prompt pinned on first dispatch so retries stay comparable
            if task.prompt_hash:
                template = self.store.load_prompt_snapshot(task.prompt_hash)
                if template is None:
                    template, task.prompt_hash = load_prompt(self.store.prompts_dir / "build.md")
                    self.store.snapshot_prompt(task.prompt_hash, template)
            else:
                template, task.prompt_hash = load_prompt(self.store.prompts_dir / "build.md")
                self.store.snapshot_prompt(task.prompt_hash, template)
            self.store.save_task(task)

            prompt = render_build_prompt(template, task=task, constraints=self.cfg.constraints)
            result = await self._run_agent("build", prompt, "prompts/build.md",
                                           task.prompt_hash, worktree_path, task.id)
            if self.gauge.limit_hit:
                # don't burn the attempt on a limit cutoff; task goes back to ready
                task.attempts = max(task.attempts - 1, 0)
                self._set_status(task, "ready")
                return
            if result.is_error:
                self._fail_task(task, f"build agent error: {result.output[:1500]}")
                return

            if not await wt.commit_all(worktree_path, f"wip: {task.id} {task.title}"):
                self._fail_task(task, "build agent produced no changes in the worktree")
                return

            from .verify import run_verify
            ok, out = await run_verify(task.verify, worktree_path, self.cfg.verify_timeout_seconds)
            if not ok:
                self._fail_task(task, f"verify failed: {task.verify}\n{out[-1500:]}")
                return
            self.store.append_event("verify_passed", task=task.id)

            async with self.merge_lock:
                ok, out = await wt.rebase_onto(worktree_path, self.base_branch)
                if not ok:
                    self._fail_task(task, f"rebase onto {self.base_branch} failed:\n{out}")
                    return
                ok, out = await run_verify(task.verify, worktree_path, self.cfg.verify_timeout_seconds)
                if not ok:
                    self._fail_task(task, f"verify failed after rebase onto {self.base_branch}:\n{out[-1500:]}")
                    return

                diff = await wt.diff_vs_base(self.repo, self.base_branch, branch)
                rtemplate, rhash = load_prompt(self.store.prompts_dir / "review.md")
                rprompt = render_review_prompt(rtemplate, task=task, diff=diff,
                                               constraints=self.cfg.constraints)
                rresult = await self._run_agent("review", rprompt, "prompts/review.md",
                                                rhash, self.repo, task.id)
                if self.gauge.limit_hit:
                    task.attempts = max(task.attempts - 1, 0)
                    self._set_status(task, "ready")
                    return
                verdict, reasons = parse_review_verdict(rresult.output)
                if verdict != "approve":
                    self._fail_task(task, "review rejected:\n- " + "\n- ".join(reasons or ["(no reasons given)"]))
                    return

                message = (f"{task.id}: {task.title}\n\n{task.detail[:800]}\n\n"
                           f"verified-by: {task.verify}\n\n"
                           f"Co-Authored-By: Claude (SLoop) <noreply@anthropic.com>")
                await wt.squash_merge(self.repo, branch, message)

            self._set_status(task, "done")
            self.session_done.append(task.id)
            self.store.append_event("task_done", task=task.id, title=task.title)
        except asyncio.CancelledError:
            self._set_status(task, "ready")
            raise
        except Exception as e:
            self._fail_task(task, f"pipeline error: {e}")
        finally:
            if worktree_path is not None:
                try:
                    await wt.remove_worktree(self.repo, task.id)
                except Exception:
                    pass

    # ---------------------------------------------------------------- helpers

    def _set_status(self, task: Task, status: str, error: str | None = None) -> None:
        task.status = status
        if error:
            task.errors.append(error)
        self.store.save_task(task)

    def _fail_task(self, task: Task, error: str) -> None:
        blocked = task.attempts >= self.cfg.max_attempts
        self._set_status(task, "blocked" if blocked else "ready", error=error)
        self.store.append_event("task_failed", task=task.id, attempt=task.attempts,
                                blocked=blocked, error=error[:300])

    # ---------------------------------------------------------------- wrap-up

    async def wrap_up(self, reason: str) -> None:
        self.store.append_event("wrap_up_started", reason=reason, usage=self.gauge.summary())

        if self.running and not self.hard_stop:
            pending = list(self.running.values())
            self.store.append_event("draining", in_flight=len(pending))
            done, still_pending = await asyncio.wait(pending, timeout=DRAIN_TIMEOUT_S)
            for fut in still_pending:
                fut.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
        elif self.running:
            for fut in self.running.values():
                fut.cancel()
            await asyncio.gather(*self.running.values(), return_exceptions=True)
        self.running.clear()
        self._harvest()

        suite_line = "not configured"
        if self.cfg.test_command and not self.hard_stop:
            from .verify import run_verify
            ok, out = await run_verify(self.cfg.test_command, self.repo,
                                       self.cfg.verify_timeout_seconds)
            suite_line = f"PASS — `{self.cfg.test_command}`" if ok else \
                         f"FAIL — `{self.cfg.test_command}`\n\n```\n{out[-2000:]}\n```"
            self.store.append_event("full_suite", ok=ok)

        self._write_session_md(reason, suite_line)
        self._notify(reason)
        self.store.append_event("halted", reason=reason, usage=self.gauge.summary())

    def _write_session_md(self, reason: str, suite_line: str) -> None:
        tasks = self.tasks
        shipped = [tasks[i] for i in self.session_done if i in tasks]
        blocked = [t for t in tasks.values() if t.status == "blocked"]
        remaining = [t for t in tasks.values() if t.status in ("ready", "backlog", "running")]

        lines = [
            "# SLoop session report",
            f"- ended: {now_iso()}",
            f"- reason: {reason}",
            f"- usage: {self.gauge.summary()}",
            "",
            "## Shipped this session",
        ]
        lines += [f"- **{t.id}** {t.title}" for t in shipped] or ["- (nothing landed)"]
        lines += ["", "## Blocked"]
        if blocked:
            for t in blocked:
                last = (t.errors[-1].splitlines()[0] if t.errors else "?")[:160]
                lines.append(f"- **{t.id}** {t.title} — {last}")
        else:
            lines.append("- (none)")
        lines += ["", "## Next (remaining queue)"]
        lines += [f"- **{t.id}** [{t.status}] {t.title}" for t in remaining] or ["- (queue empty)"]
        lines += ["", "## Full test suite", suite_line, ""]
        Store._atomic_write(self.store.session_path, "\n".join(lines))

    def _notify(self, reason: str) -> None:
        body = f"{reason}\n{self.gauge.summary()}\nshipped: {len(self.session_done)} task(s)"
        try:
            subprocess.run(["notify-send", "SLoop halted", body],
                           timeout=5, capture_output=True)
        except Exception:
            pass
