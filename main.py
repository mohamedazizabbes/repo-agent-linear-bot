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
LINEAR_CLIENT_ID = os.environ.get("LINEAR_CLIENT_ID", "")
LINEAR_CLIENT_SECRET = os.environ.get("LINEAR_CLIENT_SECRET", "")
LINEAR_REDIRECT_URI = os.environ.get("LINEAR_REDIRECT_URI", "https://repo-agent-linear-bot.onrender.com/callback")

# Cached tokens (refreshed on startup and on 401)
_access_token: str = ""
_refresh_token: str = ""
_token_loaded = False


async def _refresh_tokens() -> str:
    """Exchange refresh_token for a fresh access_token (and new refresh_token)."""
    global _access_token, _refresh_token

    refresh = os.environ.get("LINEAR_REFRESH_TOKEN", "")
    if not refresh:
        log.error("LINEAR_REFRESH_TOKEN not set — cannot refresh. Do OAuth once to get it.")
        return ""

    if not LINEAR_CLIENT_ID or not LINEAR_CLIENT_SECRET:
        log.error("LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET required for token refresh.")
        return ""

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.linear.app/oauth/token",
            data={
                "client_id": LINEAR_CLIENT_ID,
                "client_secret": LINEAR_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if resp.status_code != 200:
            log.error("Token refresh failed: %s", data)
            return ""

        _access_token = data.get("access_token", "")
        _refresh_token = data.get("refresh_token", refresh)  # Linear rotates refresh tokens
        log.info(
            "Token refreshed — access_token=%s… refresh_token=%s…",
            _access_token[:8] if _access_token else "NONE",
            _refresh_token[:8] if _refresh_token else "NONE",
        )

        if _refresh_token != refresh:
            log.warning(
                "Refresh token rotated. Update LINEAR_REFRESH_TOKEN on Render to: %s",
                _refresh_token,
            )

        return _access_token


def _load_token() -> str:
    """Load token. Returns raw value, caller adds Bearer prefix."""
    # OAuth access token (preferred — auto-refreshable)
    env_token = os.environ.get("LINEAR_APP_ACCESS_TOKEN", "")
    if env_token:
        return env_token

    # Personal API key (fallback — never expires but can't refresh)
    api_key = os.environ.get("LINEAR_API_KEY", "")
    if api_key:
        return api_key

    # Cached token from refresh
    return _access_token


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

    # Refresh token on every startup
    if os.environ.get("LINEAR_REFRESH_TOKEN"):
        tok = await _refresh_tokens()
        if tok:
            log.info("Token refreshed on startup")
        else:
            log.error("Startup token refresh failed — will try again on 401")
    else:
        log.info("No LINEAR_REFRESH_TOKEN — using LINEAR_API_KEY or static token")

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
            data={
                "client_id": LINEAR_CLIENT_ID,
                "client_secret": LINEAR_CLIENT_SECRET,
                "code": code,
                "redirect_uri": LINEAR_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        data = resp.json()
        log.info("OAuth token exchange — status=%d", resp.status_code)
        if resp.status_code != 200:
            return {"ok": False, "error": data}

    token = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    log.info(
        "Token received — access_token=%s… refresh_token=%s…",
        token[:16] if token else "EMPTY",
        refresh[:16] if refresh else "EMPTY",
    )
    return {
        "ok": True,
        "access_token": token,
        "refresh_token": refresh,
        "instruction": (
            f"1. Copy access_token into LINEAR_APP_ACCESS_TOKEN on Render\n"
            f"2. Copy refresh_token into LINEAR_REFRESH_TOKEN on Render\n"
            f"3. Redeploy"
        ),
    }


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
    action = payload.get("action")
    log.info("Payload type=%s action=%s", event_type, action)

    if event_type == "AgentSessionEvent" and action == "created":
        session = payload.get("agentSession", {})
        comment = session.get("comment", {})
        text = comment.get("body", "")
        issue_id = session.get("issueId")
        log.info("AgentSessionEvent — body=%s issueId=%s", text[:200], issue_id)
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
    raw_token = _load_token()
    if not raw_token:
        log.error("No Linear token available — cannot post comment")
        return

    auth_header = f"Bearer {raw_token}" if not raw_token.startswith("Bearer ") else raw_token

    async def _do_post(auth: str) -> httpx.Response | None:
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.post(
                "https://api.linear.app/graphql",
                headers={"Authorization": auth},
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

    # First attempt
    resp = await _do_post(auth_header)
    if resp.status_code == 401 and os.environ.get("LINEAR_REFRESH_TOKEN"):
        log.info("Token expired — attempting refresh...")
        new_token = await _refresh_tokens()
        if new_token:
            auth_header = f"Bearer {new_token}"
            resp = await _do_post(auth_header)

    if resp.status_code == 401:
        log.error(
            "Linear 401 — token expired and refresh failed. "
            "Re-run OAuth at /callback then update LINEAR_REFRESH_TOKEN on Render."
        )
    elif resp.status_code != 200:
        log.error("Linear comment failed: %s %s", resp.status_code, resp.text[:200])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
