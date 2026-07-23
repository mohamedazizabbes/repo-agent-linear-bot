from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

REPOS_FILE = Path(__file__).resolve().parent / "repos.json"

_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache

    # Try env var first (JSON string)
    raw = os.getenv("REPOS_JSON")
    if raw:
        _cache = json.loads(raw)
        return _cache

    # Fall back to repos.json file
    if REPOS_FILE.exists():
        _cache = json.loads(REPOS_FILE.read_text())
        return _cache

    _cache = {}
    return _cache


def lookup_repo(alias: str) -> dict | None:
    """Look up a repo by alias (case-insensitive, underscores/hyphens interchangeable).

    Returns {"url": "...", "repo_name": "..."} or None.
    """
    repos = _load()
    key = alias.lower().strip().replace("_", "-")
    for name, info in repos.items():
        if name.lower().replace("_", "-") == key:
            return {"url": info["url"], "repo_name": info.get("qdrant_name", name)}
    return None


def list_repos() -> dict[str, dict]:
    """Return all configured repos."""
    return _load()


def load_all_aliases() -> list[str]:
    """Return all configured repo aliases."""
    return list(_load().keys())
