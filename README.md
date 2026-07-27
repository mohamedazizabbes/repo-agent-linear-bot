# repo-agent-linear-bot

Linear bot for Repo Agent. Answer questions about your codebases directly in Linear issues.

## Architecture

```
Linear Webhook → repo-agent-linear-bot (Render) → code-explainer RAG backend → Qdrant + Groq LLM
```

The bot receives webhook events from Linear, looks up the repo alias, triggers indexing if needed, queries the RAG backend, and posts the answer as a comment on the Linear issue.

## Prerequisites

- **Linear account** with API access
- **Linear Personal API Key** — generate at https://linear.app → Settings → API → Personal API keys
- **Render account** — for deployment

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINEAR_API_KEY` | Yes | Linear personal API key (`lin_api_...`) |
| `LINEAR_WEBHOOK_SECRET` | Yes | Secret you define — Linear signs webhooks with this |
| `RAG_BACKEND_URL` | Yes | RAG backend URL (e.g. `https://code-explainer-c8g2.onrender.com`) |
| `GITHUB_TOKEN` | Optional | GitHub PAT with `repo` scope — needed for private repos |
| `REPOS_JSON` | Optional | Override repo aliases (JSON string); defaults to `repos.json` file |

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set env vars
export LINEAR_API_KEY=lin_api_xxx
export LINEAR_WEBHOOK_SECRET=your-secret
export RAG_BACKEND_URL=https://code-explainer-c8g2.onrender.com

# Run the bot
python main.py
```

Bot starts on `http://localhost:5000`.

## Exposing Locally (for Linear webhook)

Linear needs a public URL. Use ngrok:

```bash
ngrok http 5000
```

Then set the webhook URL in Linear to `https://<your-ngrok-url>/webhook`.

## Deploy to Render

1. Go to https://dashboard.render.com → **New +** → **Web Service**
2. Connect `mohamedazizabbes/repo-agent-linear-bot`
3. Settings:
   - **Name:** `repo-agent-linear-bot`
   - **Runtime:** Docker
   - **Port:** 5000
4. **Environment** → add all env vars from table above
5. **Create Web Service** → wait for deploy
6. Set Linear webhook URL to `https://<your-render-url>/webhook`

## Usage

In any Linear issue title or comment, include a repo prefix:

```
/culprit_ains What does the auth module do?
```

The bot remembers the repo for that issue — follow-up comments don't need the prefix:

```
How does the evaluation work?
```

To switch repos, use a new prefix:

```
/reservi-backend-api How are reservations made
```

## Available Repos

`agent-slack-bot`, `code-explainer`, `ask-agents`, `reservi-backend-api`, `CULPRIT_AINS`, `devpost_USAII`, `final_rag`, `rag_demo`, `4_repo_demo`, `WARCAST`, `CLI_codebase`, `RobotHealth`, `TeleWatch`, `Line-Follower-Robot`, `project_titanic`

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot doesn't respond to comments | Check Linear webhook is active and points to correct URL |
| 401 errors | Verify `LINEAR_API_KEY` starts with `lin_api_` |
| Duplicate answers on one comment | Normal — Linear sends both `AgentSessionEvent` and `Comment` webhooks; second is deduplicated |
| "Indexing failed" | Check `GITHUB_TOKEN` is set (needed for private repos) |
| Truncated answer | Bot guards against answers < 30 chars; check RAG backend health |
