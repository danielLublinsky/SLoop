# SLoop 'slop away a program' — System Spec

A TUI tool. You give it a goal and a direction, hit activate, and it runs
multiple agents autonomously until the usage limit forces a clean stop.

---

## 1. Principles

- **The orchestrator is code, not a model.** Deterministic scheduler; models are workers.
- **Disk is the state.** Never the conversation. A crash costs one tick.
- **A task is done when a shell command exits 0.** Never when an agent says so.
- **Every model call is recorded.** Append-only, for drift review and prompt tuning.

---

## 2. Components

| Component | Type | Responsibility |
|---|---|---|
| TUI | code | Activate screen, live dashboard, job browser |
| Orchestrator | code | Scheduling, dispatch, retries, budget, notifications |
| Idea agent | model | Proposes + scores the next feature → emits tasks |
| Build agent | model | Implements one task until `verify` passes |
| Review agent | model | Read-only diff check; catches test-gaming |
| Task queue | store | Work items |
| Job store | store | Every model call ever made |

---

## 3. Data

### Task — a unit of work
```
id, title, detail, deps[], verify, status, attempts, prompt_hash
status: backlog | ready | running | done | blocked
```
`detail` is standalone. The build agent sees a fresh context and nothing else.
`verify` is a shell command. No verify, no task.
`prompt_hash` is pinned on first dispatch so retries stay comparable.

### Job — one model call (append-only, never mutated)
```
id, task_id, agent, prompt_file, prompt_hash, input, output,
cost_usd, duration_ms, verdict, ts
```
One task → many jobs. That chain is the drift trail.

### Prompts
`prompts/{orchestrator,idea,build,review}.md` — plain files, hot-reloaded each
tick, hashed into every job record. Editing a prompt does not affect tasks
already dispatched; re-running with a new prompt is an explicit TUI action.

---

## 4. Loop

```
tick:
  1. reconcile      backlog → ready where deps are done
  2. check usage    ≥80% → wrap-up mode
  3. dispatch       up to MAX_CONCURRENT ready tasks → build agent
  4. verify         run task.verify; exit 0 or fail
  5. review         read-only diff check
  6. commit         one task, one commit
  7. refill         queue empty → idea agent proposes next feature
  8. sleep, repeat
```

Failure: append error to task, retry ≤2 with the error fed back, then `blocked`.
The loop routes around blocked tasks and continues.

---

## 5. Wrap-up mode (≥80% usage)

1. Freeze the idea agent — no new features.
2. Stop dispatching; let in-flight jobs finish.
3. Run the full test suite, commit whatever is green.
4. Write `SESSION.md`: what shipped, what's blocked, what's next.
5. OS notification. Halt.

Next activation resumes the same project from disk.

---

## 6. TUI

**Activate screen** — goal, seed ideas, constraints, repo path, concurrency, budget.

**Dashboard** — usage bar (green → amber at 80%), one pane per running agent,
task queue by status, scrolling event log. `q` stops gracefully.

**Jobs view** — browse any past job: exact input, exact output, cost, verdict.
Jump to its prompt file, edit, re-run that single task. This is the debugging
surface; the rest is decoration.

---

## 7. Guards

Kill-switch file · budget cap · wall-clock cap · max attempts per task ·
sandboxed workspace · review agent is read-only by design.

---

## 8. Open decisions

- Parallel builds share one worktree → collisions. Use `git worktree` per task.
- Unit tests green ≠ app runs. Add a periodic boot-and-smoke-test task.
- Which executor: Claude Agent SDK (native subagents) or OpenHands SDK.
