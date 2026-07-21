from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

LINEAR_API_KEY = os.environ["LINEAR_API_KEY"]
LINEAR_SIGNING_SECRET = os.environ["LINEAR_WEBHOOK_SECRET"]

from repos import lookup_repo
from rag_client import ask_rag
from indexing import ensure_indexed


@asynccontextmanager
async def lifespan(app: FastAPI):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    log.info("Linear bot starting up...")
    yield


app = FastAPI(title="Repo Agent Linear Bot", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/linear/webhook")
async def webhook(req: Request):
    body = await req.body()
    sig = req.headers.get("Linear-Signature", "")
    expected = hmac.new(
        LINEAR_SIGNING_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return {"ok": False}

    payload = await req.json()

    if payload.get("action") != "create" or payload.get("type") != "Comment":
        return {"ok": True}

    comment = payload.get("data", {})
    text = comment.get("body", "")
    if "@repoagent" not in text.lower():
        return {"ok": True}

    m = re.match(r".*@repoagent\s+/(\S+)\s+(.*)", text, re.I | re.S)
    if not m:
        return {"ok": True}

    alias, question = m.group(1), m.group(2)

    repo = lookup_repo(alias)
    if not repo:
        issue_id = comment.get("issueId")
        if issue_id:
            await post_comment(issue_id, f"Unknown repo alias: `{alias}`")
        return {"ok": True}

    issue_id = comment.get("issueId")
    if not issue_id:
        return {"ok": False}

    async def send(msg: str):
        await post_comment(issue_id, msg)

    ready = await ensure_indexed(repo["url"], repo["repo_name"], send)
    if ready:
        answer = await ask_rag(question, repo["repo_name"], session_id=issue_id)
        await post_comment(issue_id, answer)

    return {"ok": True}


async def post_comment(issue_id: str, body: str):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": LINEAR_API_KEY},
            json={
                "query": """
                    mutation($issueId: String!, $body: String!) {
                        commentCreate(
                            input: { issueId: $issueId, body: $body }
                        ) { success }
                    }
                """,
                "variables": {"issueId": issue_id, "body": body},
            },
        )
        if resp.status_code != 200:
            log.error(
                "Linear comment failed: %s %s", resp.status_code, resp.text[:200]
            )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
