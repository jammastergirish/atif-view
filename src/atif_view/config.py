"""Viewer settings, including an optional Anthropic API key.

A key kept in a file is weaker than one kept in the system keychain or a
password manager, so this module is deliberately narrow about it:

  * it lives at ~/.atif/config.json, 0600, inside a 0700 directory;
  * it is never returned to the page, never logged, and never put in a URL —
    the page only ever learns that a key is set and its last four characters;
  * it is only ever read to construct an API client.

The environment is still honoured: with no stored key, the SDK's own
ANTHROPIC_API_KEY handling applies as usual.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import store

CONFIG_PATH = store.ROOT / "config.json"
VERSION = 1

# Long enough to be a real key, short enough to reject a pasted paragraph.
MIN_KEY = 20
MAX_KEY = 300


def load(path: Path | None = None) -> dict[str, Any]:
    return store.read_json(path or CONFIG_PATH)


def api_key(path: Path | None = None) -> str | None:
    """The stored key, if there is one.

    A key typed into settings wins over the environment: it is the more
    explicit, more recent act, and `source()` shows which one is in use so the
    precedence is never a surprise.
    """
    value = load(path).get("api_key")
    return value if isinstance(value, str) and value else None


def source(path: Path | None = None) -> str | None:
    """Where a credential is coming from — for display, not for logic."""
    if api_key(path):
        return "settings"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "environment"
    return None


def hint(path: Path | None = None) -> str:
    """The last four characters of a stored key, enough to tell two apart."""
    key = api_key(path)
    return f"…{key[-4:]}" if key else ""


def set_api_key(value: str, path: Path | None = None) -> None:
    """Store a key. Raises ValueError on anything that is plainly not one."""
    value = (value or "").strip()
    if not value:
        raise ValueError("no key given")
    if len(value) < MIN_KEY or len(value) > MAX_KEY:
        raise ValueError("that does not look like an API key")
    if any(c.isspace() for c in value):
        raise ValueError("an API key contains no spaces — check what was pasted")

    path = path or CONFIG_PATH
    store.write_json(
        path, {**load(path), "version": VERSION, "api_key": value}, private=True
    )


def clear_api_key(path: Path | None = None) -> None:
    """Forget the stored key, falling back to the environment."""
    path = path or CONFIG_PATH
    data = load(path)
    data.pop("api_key", None)
    store.write_json(path, {**data, "version": VERSION}, private=True)
