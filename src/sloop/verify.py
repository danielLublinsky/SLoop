"""A task is done when a shell command exits 0. Never when an agent says so."""

from __future__ import annotations

import asyncio
from pathlib import Path

TAIL_CHARS = 8000


async def run_verify(command: str, cwd: Path, timeout: int) -> tuple[bool, str]:
    """Run `command` in `cwd`. Returns (exit_ok, combined output tail)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"verify timed out after {timeout}s: {command}"
        text = out.decode("utf-8", errors="replace")
        if len(text) > TAIL_CHARS:
            text = "…" + text[-TAIL_CHARS:]
        return proc.returncode == 0, text
    except Exception as e:
        return False, f"verify failed to run: {e}"
