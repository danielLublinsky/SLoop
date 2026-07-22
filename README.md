# SLoop

SLoop - slop away a program.

Give it a goal and a direction, activate, and it runs multiple agents
autonomously until the usage limit forces a clean stop. Spec: [SYSTEM.md](SYSTEM.md) ·
Design decisions: [DESIGN.md](DESIGN.md)

## Install

```sh
uv venv && uv pip install -e .
```

## Use

```sh
sloop init  /path/to/target-repo     # scaffold .sloop/ (config + prompts)
$EDITOR /path/to/target-repo/.sloop/config.toml   # set your goal
sloop activate /path/to/target-repo  # run until budget/limit stops it
```

- Stop gracefully: `touch <repo>/.sloop/KILL` or Ctrl-C (twice = hard stop).
- Resume: run `sloop activate` again — all state lives on disk in `.sloop/`.
- Smoke-test the machinery without model calls: `sloop activate <repo> --fake`.

## What it does

```
tick: guards → reconcile deps → dispatch builds (git worktree each)
      → verify (shell exit 0) → review (read-only diff check)
      → squash-merge (one task, one commit) → refill via idea agent
wrap-up (budget ≥80% / limit / kill): drain, full test suite, SESSION.md, notify.
```

Every model call is recorded append-only in `.sloop/jobs/*.jsonl` — exact
input, exact output, cost, verdict. Prompts live in `.sloop/prompts/*.md`,
hot-reloaded per dispatch and hash-pinned per task.
