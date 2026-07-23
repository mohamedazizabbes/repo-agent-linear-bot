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

LINEAR_WEBHOOK_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")

if not LINEAR_WEBHOOK_SECRET:
    log.warning("LINEAR_WEBHOOK_SECRET not set — signature verification skipped")
if not LINEAR_API_KEY:
    log.warning("LINEAR_API_KEY not set — comments will fail")

from repos import lookup_repo, load_all_aliases
from rag_client import ask_rag
from indexing import ensure_indexed


def verify_signature(body: bytes, sig: str) -> bool:
    if not LINEAR_WEBHOOK_SECRET:
        return True
    expected = hmac.new(LINEAR_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


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


@app.get("/linear/webhook")
async def webhook_get():
    return {"ok": True, "message": "webhook endpoint is live"}


@app.post("/linear/webhook")
async def webhook(req: Request):
    body = await req.body()
    if not verify_signature(body, req.headers.get("Linear-Signature", "")):
        return {"ok": False}

    payload = await req.json()
    if payload.get("type") != "Comment" or payload.get("action") != "create":
        return {"ok": True}

    text = payload["data"]["body"].strip()
    issue_id = payload["data"]["issueId"]
    log.info("Comment received — text=%s issueId=%s", text[:200], issue_id)

    m = re.match(r"/(\S+)\s+(.*)", text)
    if not m:
        log.info("No alias match for comment: %s", text[:200])
        return {"ok": True}

    alias, question = m.group(1), m.group(2)
    log.info("Parsed alias=%s question=%s", alias, question[:100])

    repo = lookup_repo(alias)
    if not repo:
        available = ", ".join(load_all_aliases())
        await post_comment(issue_id, f"Unknown repo `{alias}`. Available: {available}")
        return {"ok": True}

    async def send(msg: str):
        await post_comment(issue_id, msg)

    ready = await ensure_indexed(repo["url"], repo["repo_name"], send)
    if ready:
        answer = await ask_rag(question, repo["repo_name"], session_id=issue_id)
        await post_comment(issue_id, answer)
    return {"ok": True}


async def post_comment(issue_id: str, body: str):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": LINEAR_API_KEY},
            json={
                "query": "mutation($issueId:String!,$body:String!){commentCreate(input:{issueId:$issueId,body:$body}){success}}",
                "variables": {"issueId": issue_id, "body": body},
            },
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
