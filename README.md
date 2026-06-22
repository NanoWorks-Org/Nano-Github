# Nano GitHub

Nano GitHub is a Discord bot for GitHub notifications and pull request review workflows.

It deliberately separates read-only logging from pull request review:

- **Logging channels** receive normal GitHub event embeds for commits, issues, issue comments, and releases.
- **Pull request review channels** receive interactive PR review cards with buttons. The MVP buttons do not fake reviews; they reply ephemerally that GitHub App review/comment permissions are not configured yet.

## Features

- Python, `discord.py`, FastAPI, and SQLite
- Slash-command setup per Discord server
- Per-server linked repository configuration
- GitHub webhook endpoint at `POST /webhooks/github`
- HMAC SHA-256 verification for GitHub webhook secrets
- Events supported now:
  - `push` -> commits log channel
  - `issues` -> issues log channel
  - `issue_comment` -> comments log channel
  - `release` -> releases log channel
  - `pull_request` -> PR review channel
- Docker and Docker Compose support
- Professional dark/blue Discord embeds

## Requirements

- Python 3.11+
- A Discord application with a bot token
- A GitHub webhook secret
- Docker, if running with Compose

## Environment

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Required variables:

```env
DISCORD_TOKEN=replace-with-your-discord-bot-token
GITHUB_WEBHOOK_SECRET=replace-with-a-long-random-secret
API_HOST=0.0.0.0
API_PORT=8080
DATABASE_PATH=data/nano_github.sqlite3
```

`DATABASE_URL` is also supported for SQLite, for example:

```env
DATABASE_URL=sqlite:///data/nano_github.sqlite3
```

## Running Locally

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run the bot and API:

```bash
python -m nano_github.main
```

The webhook endpoint will be available at:

```text
http://localhost:8080/webhooks/github
```

## Running With Docker

Create `.env`, then run:

```bash
docker compose up --build
```

SQLite data is stored in the `nano-github-data` Docker volume at `/data/nano_github.sqlite3`.

## Discord Bot Setup

Invite the bot to your Discord server with these permissions:

- Send Messages
- Embed Links
- Use Slash Commands
- Read Message History
- View Channels

Then configure Nano GitHub in Discord:

```text
/github setup
/github link_repo owner repo
/github set_log_channel commits #github-commits
/github set_log_channel issues #github-issues
/github set_log_channel comments #github-comments
/github set_log_channel releases #github-releases
/github set_pr_review_channel #pull-request-review
/github status
```

Use:

```text
/github unlink_repo
```

to remove linked repositories for the server.

## GitHub Webhook Setup

In the GitHub repository:

1. Open **Settings** -> **Webhooks** -> **Add webhook**.
2. Set **Payload URL** to your public endpoint, for example:

   ```text
   https://your-domain.example/webhooks/github
   ```

3. Set **Content type** to `application/json`.
4. Set **Secret** to the same value as `GITHUB_WEBHOOK_SECRET`.
5. Select these events:
   - Pushes
   - Issues
   - Issue comments
   - Pull requests
   - Releases
6. Save the webhook.

For local development, expose `localhost:8080` through a tunnel such as ngrok or Cloudflare Tunnel, then use the public tunnel URL as the Payload URL.

## Log Channels vs PR Review Channel

Commits, issues, issue comments, and releases are notification events. They are sent as read-only embeds to the log channel configured for their event type.

Issue embeds show the issue title, body preview, repository, author, state, labels, author avatar when GitHub provides one, and a View Issue link button.

Push events are formatted as commit notifications. A single-commit push sends one detailed commit embed. Pushes with 2-5 commits send one embed per commit. Pushes with more than 5 commits send a summary embed plus the first 5 commit embeds. Empty pushes are ignored unless GitHub marks them as branch creation or deletion events.

Pull requests are workflow events. They are not posted to normal log channels. They are sent to the configured PR review channel as interactive review cards that include:

- PR title and number
- Repository
- Author
- Source branch -> target branch
- Changed files, additions, and deletions when GitHub includes them
- Current state
- Direct GitHub link
- Buttons for View Pull Request, Approve, Request Changes, and Comment

For the MVP, Approve, Request Changes, and Comment respond ephemerally:

```text
GitHub App review permissions are not configured yet.
```

or:

```text
GitHub App comment permissions are not configured yet.
```

This is intentional. Nano GitHub does not pretend that a PR was approved or commented on. These buttons are structured so they can later be connected to GitHub's Pull Request Review API through a GitHub App with the right permissions.

## Webhook Behavior

- Webhook signatures are verified with `X-Hub-Signature-256`.
- Malformed JSON returns `400` and is logged without crashing the bot.
- Events for repositories not linked to a Discord server are ignored.
- If a channel is not configured for an event, the event is skipped and a warning is logged.
- PR review cards are updated when Nano GitHub has already stored a Discord message for that PR.

## Development Checks

Install development dependencies:

```bash
pip install ".[dev]"
```

Run Ruff:

```bash
ruff check .
```

Build Docker image:

```bash
docker compose build
```

Still ain't working.
