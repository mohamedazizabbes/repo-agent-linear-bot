from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

LINEAR_WEBHOOK_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
LINEAR_CLIENT_ID = os.environ.get("LINEAR_CLIENT_ID", "")
LINEAR_CLIENT_SECRET = os.environ.get("LINEAR_CLIENT_SECRET", "")
LINEAR_REDIRECT_URI = os.environ.get("LINEAR_REDIRECT_URI", "")

# App-actor token — set after completing OAuth install once
APP_ACCESS_TOKEN = os.environ.get("LINEAR_APP_ACCESS_TOKEN", "")

# Fallback personal key (only used if no app token)
FALLBACK_API_KEY = os.environ.get("LINEAR_API_KEY", "")


def _load_token() -> str:
    return APP_ACCESS_TOKEN or FALLBACK_API_KEY


if not LINEAR_CLIENT_ID:
    log.warning("LINEAR_CLIENT_ID not set")
if not LINEAR_WEBHOOK_SECRET:
    log.warning("LINEAR_WEBHOOK_SECRET not set — signature verification skipped")
if not _load_token():
    log.warning("No access token available — comments will fail")

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


@app.get("/callback")
async def oauth_callback(code: str):
    if not code:
        return {"error": "missing code"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.linear.app/oauth/token",
            json={
                "client_id": LINEAR_CLIENT_ID,
                "client_secret": LINEAR_CLIENT_SECRET,
                "code": code,
                "redirect_uri": LINEAR_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    token = data.get("access_token", "")
    log.info("OAuth token received — copy this into LINEAR_APP_ACCESS_TOKEN: %s", token[:16] if token else "EMPTY")
    return {"ok": True, "access_token": token, "instruction": "Copy this access_token into LINEAR_APP_ACCESS_TOKEN env var on Render, then redeploy."}


@app.get("/linear/webhook")
async def webhook_get():
    return {"ok": True, "message": "webhook endpoint is live"}


@app.post("/linear/webhook")
async def webhook(req: Request):
    body = await req.body()
    sig = req.headers.get("Linear-Signature", "")
    log.info("Webhook received — sig=%s body_len=%d", sig[:16] if sig else "MISSING", len(body))

    expected = hmac.new(
        LINEAR_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if LINEAR_WEBHOOK_SECRET and not hmac.compare_digest(sig, expected):
        log.warning("Signature mismatch")
        return {"ok": False}

    payload = await req.json()
    event_type = payload.get("type")
    log.info("Payload type=%s action=%s", event_type, payload.get("action"))

    # --- Agent session event (proper agent app) ---
    if event_type == "AgentSessionEvent":
        session = payload.get("agentSession", {})
        comment = session.get("comment", {})
        text = comment.get("body", "")
        issue_id = session.get("issueId")
        log.info("AgentSessionEvent — body=%s issueId=%s", text[:200], issue_id)

        return await _handle_comment(text, issue_id, payload)

    # --- Fallback: plain Comment event (webhook-only mode) ---
    if payload.get("action") == "create" and event_type == "Comment":
        comment = payload.get("data", {})
        text = comment.get("body", "")
        issue_id = comment.get("issueId")
        log.info("Comment event — body=%s issueId=%s", text[:200], issue_id)

        return await _handle_comment(text, issue_id, payload)

    return {"ok": True}


async def _handle_comment(text: str, issue_id: str | None, payload: dict):
    if "@repoagent" not in text.lower():
        return {"ok": True}

    m = re.match(r".*@repoagent\s+/(\S+)\s+(.*)", text, re.I | re.S)
    if not m:
        log.info("No alias match for comment: %s", text[:200])
        return {"ok": True}

    alias, question = m.group(1), m.group(2)
    log.info("Parsed alias=%s question=%s", alias, question[:100])

    repo = lookup_repo(alias)
    if not repo:
        log.warning("Unknown repo alias: %s", alias)
        if issue_id:
            await post_comment(issue_id, f"Unknown repo alias: `{alias}`")
        return {"ok": True}

    if not issue_id:
        log.error("No issueId found in payload: %s", payload)
        return {"ok": False}

    async def send(msg: str):
        await post_comment(issue_id, msg)

    ready = await ensure_indexed(repo["url"], repo["repo_name"], send)
    if ready:
        answer = await ask_rag(question, repo["repo_name"], session_id=issue_id)
        await post_comment(issue_id, answer)

    return {"ok": True}


async def post_comment(issue_id: str, body: str):
    token = _load_token()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": token},
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
