from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Awaitable

import httpx

log = logging.getLogger(__name__)

RAG_BACKEND_URL = os.getenv("RAG_BACKEND_URL", "http://localhost:8000")

# Poll interval / max retries for waiting on indexing
POLL_INTERVAL = 5
MAX_POLLS = 120  # 10 minutes max


async def ensure_indexed(
    repo_url: str,
    repo_name: str,
    send: Callable[[str], Awaitable[None]],
) -> bool:
    """Ensure the repo is indexed in the RAG backend.

    1. Trigger /ingest with repo_url (returns immediately, background task).
    2. Poll /ingest/status/{repo_name} until "ready" or timeout.
    3. Sends progress updates via the `send` callback.

    Returns True when ready, False on failure/timeout.
    """
    # Check current status first
    status = await _get_status(repo_name)
    if status == "ready":
        log.info("Repo %s already indexed", repo_name)
        return True

    # Trigger ingestion
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RAG_BACKEND_URL}/ingest",
                json={"repo_url": repo_url},
            )
            resp.raise_for_status()
            data = resp.json()
            log.info("Ingest triggered for %s: %s", repo_name, data)
    except Exception as e:
        log.error("Failed to trigger ingest for %s: %s", repo_name, e)
        await send(f"Failed to start indexing: {e}")
        return False

    # Poll until ready
    await send("Indexing in progress... this may take a few minutes.")
    for i in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL)
        status = await _get_status(repo_name)
        if status == "ready":
            await send("Indexing complete.")
            return True
        if status.startswith("error"):
            await send(f"Indexing failed: {status}")
            return False
        if i % 12 == 0 and i > 0:
            await send(f"Still indexing... ({i * POLL_INTERVAL}s elapsed)")

    await send("Indexing timed out. Please try again later.")
    return False


async def _get_status(repo_name: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{RAG_BACKEND_URL}/ingest/status/{repo_name}"
            )
            resp.raise_for_status()
            return resp.json().get("status", "unknown")
    except Exception as e:
        log.warning("Status check failed for %s: %s", repo_name, e)
        return "unknown"
