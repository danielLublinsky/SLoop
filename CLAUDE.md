# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SLoop is an autonomous multi-agent build loop: point it at a git repo with a goal, `sloop activate`, and a deterministic Python orchestrator drives Claude agents (via the Claude Agent SDK) until a budget/usage limit forces a clean stop. Read [SYSTEM.md](SYSTEM.md) (the spec) and [DESIGN.md](DESIGN.md) (v1 decisions + module map) before changing behavior — the spec's principles are load-bearing invariants, not aspirations:

- The orchestrator is code, not a model. All control flow lives in `orchestrator.py`.
- Disk is the state (`.sloop/` in the *target* repo). Never keep authoritative state in memory only.
- A task is done only when its `verify` shell command exits 0 — never because an agent said so.
- `jobs/*.jsonl` and `events.jsonl` are append-only. Never rewrite them.
- No verify, no task: proposals without a verify command are dropped at parse time.
- One task = one squash commit on the base branch.

## Commands

```sh
# dev setup (local venv)
uv venv && uv pip install -e .

# the user's global install is `uv tool install --editable .`
# → code edits here take effect on the NEXT `sloop activate`, no reinstall

# end-to-end regression check — there is no pytest suite (deliberate v1 choice);
# this fake run IS the test. It must end with task_done + halted:
t=$(mktemp -d) && git -C "$t" init -q -b main \
  && git -C "$t" config user.email t@t && git -C "$t" config user.name t \
  && .venv/bin/sloop init "$t" > /dev/null \
  && sed -i 's/^goal = .*/goal = "demo"/' "$t/.sloop/config.toml" \
  && timeout 60 .venv/bin/sloop activate "$t" --fake

# real run (spends the user's Claude subscription usage — don't do this casually)
sloop init <repo> && $EDITOR <repo>/.sloop/config.toml && sloop activate <repo>
```

Python ≥ 3.10 required (`tomllib`, dataclasses, `asyncio`). Only runtime dependency is `claude-agent-sdk`.

## Architecture (the parts that span multiple files)

**Control flow** — `cli.py` → `Orchestrator.run()` (single asyncio process). Each tick: guards (KILL file / budget / subscription-limit) → harvest finished pipelines → reconcile deps (`backlog`→`ready`) → dispatch up to `max_concurrent` build pipelines → refill via idea agent when the queue is empty. Wrap-up (drain in-flight, run `test_command`, write `SESSION.md`, notify, halt) is triggered by any guard, by Ctrl-C, or by the idea agent returning `[]` twice.

**Per-task pipeline** (`Orchestrator._pipeline`): create `git worktree` on branch `sloop/<id>` → build agent runs with `cwd=worktree` → orchestrator commits whatever it left (`commit_all`; "no changes" = failure) → run `task.verify` in the worktree → then, **serialized under `merge_lock`**: rebase onto base branch → re-run verify → review agent judges the diff → squash-merge. Any failure appends to `task.errors` (fed back into the next build prompt), `attempts >= max_attempts` ⇒ `blocked`. The loop routes around blocked tasks; blocked deps propagate.

**Executor seam** — the orchestrator never imports the Claude Agent SDK. `agents/base.py` defines the `Executor` protocol; `AgentSDKExecutor` (spawns the SDK's bundled Claude Code CLI per job — fresh context each time, reads `~/.claude` auth) and `agents/fake.py` `DemoFakeExecutor` (`--fake`) are the two implementations. Keep new executor features behind this protocol so `--fake` keeps covering the machinery. SDK fields are read defensively (`getattr`) — the SDK is the only untested-in-CI boundary.

**Agent contracts** — prompts rendered in `agents/render.py` from templates, outputs parsed in `agents/parsing.py`. Idea agent returns a fenced JSON task list (deps as 0-based batch indices or existing `T###` ids); review agent returns `{"verdict": "approve"|"reject", ...}` where *unparseable output = reject*. Verify commands get a hallucinated `cd /abs/path &&` prefix stripped at parse time AND repaired on stored tasks at startup (`_repair_verifies`, which also un-blocks tasks broken only by that).

**Prompt pinning** — on a task's first dispatch, `prompts/build.md` is hashed and snapshotted to `.sloop/prompts/.snapshots/<hash>.md`; retries load the snapshot. Editing a prompt file affects only newly dispatched tasks.

**Tool policy** — build agent gets write tools + Bash with `permission_mode` from config (default `bypassPermissions`, sandboxed only by the worktree + review + kill-switch); idea/review agents are restricted to Read/Grep/Glob with Write/Edit/Bash disallowed. Review being read-only is a design guarantee — don't loosen it.

## Gotchas

- `src/sloop/templates/*` are **copied** into a target repo by `sloop init`, which never overwrites existing files. Fixing a template does nothing for already-initialized repos — either code the fix (like the verify sanitizer) or tell the user to re-copy the prompt.
- `.sloop/` and `.sloop-worktrees/` are kept out of the target repo's git status via `.git/info/exclude` (written by both `init` and `activate`); `ensure_excludes` must run *before* the clean-tree check in `setup()`.
- Budget gauge (`usage.py`) resets per activation; the subscription-limit backstop (`limit_hit`) is detected by regex on executor errors and must not consume the task's attempt (see the `limit_hit` branches in `_pipeline`).
- Agent model defaults to the user's Claude Code default model when config `model` keys are unset — a slow/expensive default is a common user complaint; per-agent overrides live under `[agents]` in config.toml.
- Every model call goes through `Orchestrator._run_agent`, which owns heartbeat logging, the `job_timeout_seconds` hard cap, gauge accounting, and the append-only Job record. Don't call the executor from anywhere else.
