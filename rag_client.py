from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

RAG_BACKEND_URL = os.getenv("RAG_BACKEND_URL", "http://localhost:8000")


async def ask_rag(
    question: str,
    repo_name: str,
    session_id: str | None = None,
) -> str:
    """Query the RAG backend and return the full answer as a string."""
    payload: dict = {
        "question": question,
        "target_repo": repo_name,
    }
    if session_id:
        payload["session_id"] = session_id

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{RAG_BACKEND_URL}/query",
            json=payload,
        )
        resp.raise_for_status()

    # Response is SSE text/event-stream — parse out the answer
    full_answer: list[str] = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            full_answer.append(chunk)

    return "".join(full_answer).strip() or "No answer found."
