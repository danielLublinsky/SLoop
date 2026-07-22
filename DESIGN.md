# SLoop — v1 Design Decisions

Companion to [SYSTEM.md](SYSTEM.md) (the spec). This records what was decided
for v1 and why. 2026-07-23.

## Decisions

| Open question | Decision |
|---|---|
| Executor (§8) | **Claude Agent SDK** (Python). Claude Code as a library — built-in tools, subscription auth. Isolated behind an `Executor` protocol (`agents/base.py`) so it stays swappable; `--fake` proves the seam works. |
| "Usage" for wrap-up | **Both**: primary gauge = per-activation budget (`max_cost_usd` and/or `max_wall_clock_min`, wrap-up at `wrap_up_at`, default 0.8); hard backstop = subscription "usage limit reached" errors detected in executor output → immediate wrap-up without burning the task's attempt. |
| V1 scope | **Core loop + `sloop activate` CLI.** No TUI. Dashboard/Jobs view later read the same files. |
| Architecture | **Single asyncio process, plain-file state, worktree per task.** State in `.sloop/` inside the target repo (atomic tmp+rename writes; jobs/events append-only JSONL). Worktrees under `.sloop-worktrees/`, kept out of `git status` via `.git/info/exclude`. |
| Parallel builds (§8) | `git worktree` per task, branch `sloop/<id>`. Merges serialized behind one lock: **rebase onto base → re-verify → review → squash-merge** (one task, one commit). Rebase conflict = task failure with the error fed back. |
| Prompt pinning | On first dispatch the build prompt file is snapshotted to `.sloop/prompts/.snapshots/<hash>.md`; retries load the snapshot. Editing prompts affects only newly dispatched tasks. |
| Review agent read-only | Enforced by tool policy: `allowed_tools = Read/Grep/Glob`, `disallowed_tools = Write/Edit/Bash`. Same for the idea agent. Unparseable review output = reject (approval must be explicit). |
| No verify, no task | Enforced at parse time: idea-agent proposals without a `verify` command are dropped with a logged warning. |
| Stopping | Kill-file (`.sloop/KILL`), first Ctrl-C = graceful wrap-up, second = hard cancel. Idea agent returning `[]` twice = goal complete → wrap-up. |
| Crash recovery | On activate: `running` tasks → `ready`, all sloop worktrees/branches pruned, loop resumes from disk. A crash costs one tick. |

## Module map

```
src/sloop/
├─ cli.py            sloop init | sloop activate [--fake]
├─ config.py         .sloop/config.toml → Config
├─ models.py         Task, Job dataclasses
├─ store.py          tasks/*.json, jobs/*.jsonl, events.jsonl, snapshots, KILL
├─ orchestrator.py   tick loop, dispatch, refill, wrap-up, SESSION.md
├─ worktree.py       git plumbing: worktrees, rebase, squash-merge pipeline
├─ verify.py         run task.verify, exit 0 or fail
├─ usage.py          budget gauge + limit backstop
├─ prompts.py        load/hash/render prompt files
├─ agents/
│  ├─ base.py        Executor protocol + AgentSDKExecutor
│  ├─ fake.py        DemoFakeExecutor (--fake smoke runs)
│  ├─ render.py      idea/build/review prompt rendering
│  └─ parsing.py     JSON contract extraction (tasks, verdicts)
└─ templates/        default idea.md, build.md, review.md, config.toml
```

## Failure semantics

- `max_attempts = 3` total per task (spec: retry ≤2 with error fed back).
- Every failure appends to `task.errors`; the last two are injected into the
  next build prompt as feedback.
- A task whose dependency is `blocked` becomes `blocked` (route around).
- A subscription-limit cutoff mid-build resets the task to `ready` without
  consuming an attempt.

## Deferred to v2+

- Textual TUI (Activate screen, dashboard, jobs browser) on top of the same files.
- Re-run a single past job with an edited prompt (the Jobs-view action).
- Periodic boot-and-smoke-test task (§8).
- Stronger sandboxing than worktree + review (containers).
- Test suite (skipped in v1 by operator choice; `--fake` covers the happy and
  failure paths end-to-end).
