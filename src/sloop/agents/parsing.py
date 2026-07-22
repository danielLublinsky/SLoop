"""Robust extraction of JSON contracts from agent output."""

from __future__ import annotations

import json
import re

FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

# verify runs with the repo root as cwd; a hallucinated `cd /abs/path &&` prefix
# (e.g. `cd /root/repo && …`) would fail on every attempt through no fault of
# the build agent — strip it.
CD_ABS_PREFIX_RE = re.compile(r"^\s*cd\s+/\S*\s*&&\s*")


def extract_json(text: str):
    """Find the first parseable JSON value in agent output, or None."""
    candidates: list[str] = [m.strip() for m in FENCE_RE.findall(text)]
    # fall back to the largest bracketed span
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_proposed_tasks(text: str, limit: int) -> tuple[list[dict], list[str]]:
    """Parse the idea agent's output. Returns (valid_tasks, warnings)."""
    data = extract_json(text)
    warnings: list[str] = []
    if data is None:
        return [], ["idea agent output contained no parseable JSON"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [], ["idea agent output was not a JSON list"]

    valid: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            warnings.append(f"proposal #{i} is not an object — dropped")
            continue
        title = str(item.get("title") or "").strip()
        detail = str(item.get("detail") or "").strip()
        verify = str(item.get("verify") or "").strip()
        if not (title and detail):
            warnings.append(f"proposal #{i} missing title/detail — dropped")
            continue
        if not verify:
            warnings.append(f"proposal '{title}' has no verify command — dropped (no verify, no task)")
            continue
        cleaned = CD_ABS_PREFIX_RE.sub("", verify)
        if cleaned != verify:
            warnings.append(f"proposal '{title}': stripped absolute `cd` prefix from verify "
                            f"(runs from repo root): {verify[:80]!r}")
            verify = cleaned
        if not verify:
            warnings.append(f"proposal '{title}' verify was only a cd — dropped")
            continue
        deps = item.get("deps") or []
        if not isinstance(deps, list):
            deps = []
        valid.append({"title": title, "detail": detail, "verify": verify, "deps": deps})
        if len(valid) >= limit:
            break
    return valid, warnings


def parse_review_verdict(text: str) -> tuple[str, list[str]]:
    """Parse the review agent's output. Returns (verdict, reasons).
    Unparseable output rejects — the review agent must be explicit to approve."""
    data = extract_json(text)
    if isinstance(data, dict):
        verdict = str(data.get("verdict") or "").strip().lower()
        reasons = [str(r) for r in data.get("reasons") or [] if str(r).strip()]
        if verdict in ("approve", "reject"):
            return verdict, reasons
    return "reject", ["review output was not a valid verdict JSON"]
