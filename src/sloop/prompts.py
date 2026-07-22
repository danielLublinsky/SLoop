"""Prompt files: load, hash, render. Plain files, hot-reloaded each dispatch."""

from __future__ import annotations

import hashlib
from pathlib import Path


def load_prompt(path: Path) -> tuple[str, str]:
    """Return (text, sha256-prefixed hash)."""
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return text, f"sha256:{digest}"


def render(template: str, mapping: dict[str, str]) -> str:
    """Minimal {{key}} substitution — no templating engine needed."""
    out = template
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", value if value is not None else "")
    return out
