"""sloop CLI — v1: `sloop init` and `sloop activate`. No TUI, no fancy."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path

from .config import load_config
from .store import Store

TEMPLATES = Path(__file__).parent / "templates"


def _repo_arg(path_str: str) -> Path:
    repo = Path(path_str).expanduser().resolve()
    if not repo.is_dir():
        sys.exit(f"error: {repo} is not a directory")
    return repo


def cmd_init(args: argparse.Namespace) -> None:
    repo = _repo_arg(args.repo)
    root = repo / ".sloop"
    store = Store(root)
    store.ensure_dirs()

    from .worktree import ensure_excludes
    asyncio.run(ensure_excludes(repo))

    wrote = []
    for name in ("idea.md", "build.md", "review.md"):
        dest = store.prompts_dir / name
        if not dest.exists():
            shutil.copyfile(TEMPLATES / name, dest)
            wrote.append(str(dest.relative_to(repo)))
    config_dest = root / "config.toml"
    if not config_dest.exists():
        shutil.copyfile(TEMPLATES / "config.toml", config_dest)
        wrote.append(str(config_dest.relative_to(repo)))

    print(f"initialized {root}")
    for w in wrote:
        print(f"  + {w}")
    print("\nnext steps:")
    print(f"  1. edit {config_dest.relative_to(repo)} — set your goal")
    print(f"  2. sloop activate {repo}")


def cmd_activate(args: argparse.Namespace) -> None:
    repo = _repo_arg(args.repo)
    root = repo / ".sloop"
    config_path = root / "config.toml"
    if not config_path.exists():
        sys.exit(f"error: {config_path} not found — run `sloop init {repo}` first")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(config_path)
    except ValueError as e:
        sys.exit(f"error: {e}")

    store = Store(root)
    if args.fake:
        from .agents.fake import DemoFakeExecutor
        executor = DemoFakeExecutor()
        logging.getLogger("sloop").warning("running with --fake executor (no model calls)")
    else:
        from .agents.base import AgentSDKExecutor
        executor = AgentSDKExecutor()

    from .orchestrator import Orchestrator
    from .worktree import GitError
    orch = Orchestrator(repo, config, store, executor)
    try:
        asyncio.run(orch.run())
    except GitError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass
    print(f"\nsession report: {store.session_path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sloop", description="Autonomous multi-agent build loop.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="scaffold .sloop/ (config + default prompts) in a repo")
    p_init.add_argument("repo", nargs="?", default=".", help="target repo path (default: cwd)")
    p_init.set_defaults(func=cmd_init)

    p_act = sub.add_parser("activate", help="run the loop until budget/limit forces a clean stop")
    p_act.add_argument("repo", nargs="?", default=".", help="target repo path (default: cwd)")
    p_act.add_argument("--fake", action="store_true",
                       help="smoke-test the machinery with a fake executor (no model calls)")
    p_act.set_defaults(func=cmd_activate)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
