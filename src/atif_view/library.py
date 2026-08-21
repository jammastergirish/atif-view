"""What you decide about a transcript, as opposed to what it is.

Titles, folders, tags and stars live here; paths, sizes and formats stay in
atif-make's index. Keeping them apart means re-indexing can never destroy an
annotation, and an annotation survives its file being re-scanned from somewhere
new — records are keyed by content, so a file that moves keeps everything.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIBRARY_PATH = Path.home() / ".atif" / "library.json"
VERSION = 1

# Two viewers can be open at once; serialise our own writes and let the atomic
# replace settle the rest.
_lock = threading.Lock()

_DEFAULTS: dict[str, Any] = {
    "title": "",
    "folder": "",
    "tags": [],
    "starred": False,
    "note": "",
    # Individual steps starred inside a transcript. Held as the viewer's step
    # keys — "12" at the top level, "<trajectory>-3" inside a subagent, whose
    # ids restart at 1 — so a star cannot land on the wrong step.
    "starred_steps": [],
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def load(path: Path | None = None) -> dict[str, dict]:
    """Every annotation, by key. A missing or damaged file reads as empty.

    Losing annotations is bad; refusing to start because one byte is wrong is
    worse, so a corrupt file is treated as absent rather than raised.
    """
    path = path or LIBRARY_PATH
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def save(entries: dict[str, dict], path: Path | None = None) -> None:
    """Write the whole library atomically.

    Writing in place would leave a truncated file if the process died mid-write,
    losing every annotation; a temp file plus os.replace either fully lands or
    leaves the previous version untouched.
    """
    path = path or LIBRARY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": VERSION, "entries": entries}, indent=2)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=".library-", suffix=".tmp", delete=False
    )
    try:
        with handle as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def get(key: str, path: Path | None = None) -> dict:
    """One record, with defaults filled in, whether or not it is annotated."""
    path = path or LIBRARY_PATH
    return {**_DEFAULTS, **load(path).get(key, {})}


def _clean_tags(value: Any) -> list[str]:
    """Tags are short, unique, lower-case labels — order preserved."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tag in value:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().lower()[:40]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out[:20]


def _clean_steps(value: Any) -> list[str]:
    """Step keys, unique and order-preserved. Kept as strings because a
    subagent's key is not a number."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        key = str(item).strip()[:120]
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out[:2000]


def _clean_folder(value: Any) -> str:
    """A '/'-nested path, with empty and stray segments dropped."""
    if not isinstance(value, str):
        return ""
    parts = [p.strip() for p in value.split("/")]
    return "/".join(p for p in parts if p)[:200]


def update(key: str, path: Path | None = None, **fields: Any) -> dict:
    """Merge fields into one record and persist. Returns the merged record."""
    if not key:
        raise ValueError("a key is required")
    path = path or LIBRARY_PATH
    with _lock:
        entries = load(path)
        record = {**_DEFAULTS, **entries.get(key, {})}
        for name, value in fields.items():
            if name not in _DEFAULTS:
                continue
            if name == "tags":
                record["tags"] = _clean_tags(value)
            elif name == "starred_steps":
                record["starred_steps"] = _clean_steps(value)
            elif name == "folder":
                record["folder"] = _clean_folder(value)
            elif name == "starred":
                record["starred"] = bool(value)
            else:
                record[name] = str(value).strip()[:500] if value is not None else ""
        record.setdefault("added", _now())
        # An entry annotated back to nothing is not worth keeping.
        if any(record[f] for f in _DEFAULTS):
            entries[key] = record
        else:
            entries.pop(key, None)
        save(entries, path)
    return record


def remove(key: str, path: Path | None = None) -> bool:
    """Forget one record. True when there was something to forget."""
    path = path or LIBRARY_PATH
    with _lock:
        entries = load(path)
        existed = entries.pop(key, None) is not None
        if existed:
            save(entries, path)
    return existed


def folders(path: Path | None = None) -> list[str]:
    """Every folder, including implied parents, sorted for a tree walk.

    'Redwood/SOC2' implies 'Redwood' even when nothing is filed directly there,
    or the tree would have a hole in it.
    """
    path = path or LIBRARY_PATH
    found: set[str] = set()
    for record in load(path).values():
        folder = record.get("folder") or ""
        parts = [p for p in folder.split("/") if p]
        for depth in range(1, len(parts) + 1):
            found.add("/".join(parts[:depth]))
    return sorted(found)


def tags(path: Path | None = None) -> list[tuple[str, int]]:
    """Every tag with a count, most used first."""
    path = path or LIBRARY_PATH
    counts: dict[str, int] = {}
    for record in load(path).values():
        for tag in record.get("tags") or []:
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def decorate(rows: list[dict], path: Path | None = None) -> list[dict]:
    """Fold annotations into index rows so a client gets one object per session."""
    path = path or LIBRARY_PATH
    entries = load(path)
    for row in rows:
        row.update({**_DEFAULTS, **entries.get(row.get("key", ""), {})})
    return rows
