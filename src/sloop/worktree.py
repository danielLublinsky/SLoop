"""Git plumbing: worktree-per-task isolation and the serialized merge pipeline.

Layout (inside the target repo, kept out of git via .git/info/exclude):
    <repo>/.sloop-worktrees/<task-id>   worktree on branch sloop/<task-id>
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

WORKTREES_DIR = ".sloop-worktrees"
BRANCH_PREFIX = "sloop/"
DIFF_MAX_CHARS = 40_000


class GitError(RuntimeError):
    pass


async def git(cwd: Path, *args: str, check: bool = True) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="replace").strip()
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {text[:2000]}")
    return proc.returncode, text


async def ensure_repo_ready(repo: Path) -> str:
    """Repo must exist, have a HEAD commit, and a clean tree. Returns base branch."""
    if not (repo / ".git").exists():
        raise GitError(f"{repo} is not a git repository (run `git init` and commit first)")
    code, _ = await git(repo, "rev-parse", "--verify", "HEAD", check=False)
    if code != 0:
        _, status = await git(repo, "status", "--porcelain")
        if status.strip():
            raise GitError(
                f"{repo} has no commits yet and contains untracked files — run:\n"
                f"  git add -A && git commit -m 'initial'"
            )
        # truly empty repo: bootstrap it so worktrees have a base to branch from
        await git(repo, "commit", "--allow-empty", "-m", "sloop: initial commit")
    _, status = await git(repo, "status", "--porcelain")
    if status.strip():
        raise GitError(f"{repo} has uncommitted changes — commit or stash them first:\n{status[:1000]}")
    _, branch = await git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return branch


async def ensure_excludes(repo: Path) -> None:
    """Keep .sloop/ and worktrees out of git status without touching .gitignore."""
    if not (repo / ".git").is_dir():
        return
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = [ln for ln in ("/.sloop/", f"/{WORKTREES_DIR}/") if ln not in existing]
    if lines:
        with exclude.open("a", encoding="utf-8") as f:
            f.write("\n# sloop\n" + "\n".join(lines) + "\n")


def worktree_path(repo: Path, task_id: str) -> Path:
    return repo / WORKTREES_DIR / task_id


def branch_name(task_id: str) -> str:
    return BRANCH_PREFIX + task_id


async def create_worktree(repo: Path, task_id: str, base_branch: str) -> Path:
    path = worktree_path(repo, task_id)
    branch = branch_name(task_id)
    # clean any leftovers from a previous crashed run
    await remove_worktree(repo, task_id)
    await git(repo, "worktree", "add", "-b", branch, str(path), base_branch)
    return path


async def remove_worktree(repo: Path, task_id: str) -> None:
    path = worktree_path(repo, task_id)
    branch = branch_name(task_id)
    await git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    await git(repo, "worktree", "prune", check=False)
    await git(repo, "branch", "-D", branch, check=False)


async def prune_all_worktrees(repo: Path) -> None:
    """Startup crash recovery: drop every sloop worktree and branch."""
    root = repo / WORKTREES_DIR
    if root.exists():
        for entry in root.iterdir():
            if entry.is_dir():
                await remove_worktree(repo, entry.name)
    await git(repo, "worktree", "prune", check=False)
    code, out = await git(repo, "branch", "--list", f"{BRANCH_PREFIX}*", check=False)
    if code == 0:
        for line in out.splitlines():
            branch = line.strip().lstrip("* ").strip()
            if branch.startswith(BRANCH_PREFIX):
                await git(repo, "branch", "-D", branch, check=False)


async def commit_all(worktree: Path, message: str) -> bool:
    """Commit everything the build agent left in the worktree.
    Returns False when there is nothing to commit."""
    await git(worktree, "add", "-A")
    code, _ = await git(worktree, "diff", "--cached", "--quiet", check=False)
    if code == 0:
        return False  # nothing staged
    await git(worktree, "commit", "-m", message)
    return True


async def rebase_onto(worktree: Path, base_branch: str) -> tuple[bool, str]:
    code, out = await git(worktree, "rebase", base_branch, check=False)
    if code != 0:
        await git(worktree, "rebase", "--abort", check=False)
        return False, out[:2000]
    return True, out


async def diff_vs_base(repo: Path, base_branch: str, branch: str) -> str:
    _, out = await git(repo, "diff", f"{base_branch}...{branch}", check=False)
    if len(out) > DIFF_MAX_CHARS:
        out = out[:DIFF_MAX_CHARS] + "\n… (diff truncated)"
    return out


async def squash_merge(repo: Path, branch: str, message: str) -> None:
    """One task, one commit on the base branch. Caller holds the merge lock."""
    try:
        await git(repo, "merge", "--squash", branch)
        await git(repo, "commit", "-m", message)
    except GitError:
        await git(repo, "reset", "--merge", check=False)
        raise
