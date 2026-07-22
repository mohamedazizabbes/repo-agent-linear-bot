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
    """Query the RAG backend and return the full answer as a string.

    Returns a clean error message instead of partial output when
    generation fails, times out, or the stream is interrupted.
    """
    payload: dict = {
        "question": question,
        "target_repo": repo_name,
    }
    if session_id:
        payload["session_id"] = session_id

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{RAG_BACKEND_URL}/query",
                json=payload,
            )
            resp.raise_for_status()

            # Response is SSE text/event-stream — parse out the answer
            full_answer: list[str] = []
            completed = False
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        completed = True
                        break
                    full_answer.append(chunk)

        answer = "".join(full_answer).strip()

        # Guard: only post meaningful, complete answers.
        # Reject single-word fragments from interrupted streams.
        if not answer or not completed:
            log.warning(
                "RAG returned incomplete answer (completed=%s, len=%d)",
                completed, len(answer),
            )
            return "Sorry — the answer generation was interrupted. Please try again."

        if len(answer.split()) < 3:
            log.warning("RAG answer too short (%d words): %r", len(answer.split()), answer)
            return "Sorry — the answer was too short to be useful. Please try again."

        return answer

    except httpx.HTTPStatusError as e:
        log.error("RAG backend HTTP error: %s", e)
        return f"Sorry — the RAG backend returned an error ({e.response.status_code}). Please try again later."
    except httpx.ConnectError:
        log.error("Cannot connect to RAG backend at %s", RAG_BACKEND_URL)
        return "Sorry — the RAG backend is unreachable. Please try again later."
    except httpx.TimeoutException:
        log.error("RAG backend timed out")
        return "Sorry — the answer took too long to generate. Please try a shorter question."
    except Exception as e:
        log.error("Unexpected error querying RAG backend: %s", e)
        return "Sorry — something went wrong while generating the answer. Please try again."
