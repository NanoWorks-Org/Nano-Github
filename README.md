# Nano GitHub

Nano GitHub is a Discord bot for GitHub notifications and pull request review workflows.

It deliberately separates read-only logging from pull request review:

- **Logging channels** receive normal GitHub event embeds for commits, issues, issue comments, and releases.
- **Pull request review channels** receive interactive PR review cards with buttons that submit GitHub Pull Request Reviews through the Nano GitHub GitHub App.
- **Discord issue creation** lets server members create GitHub suggestions and bug reports through the Nano GitHub GitHub App without needing GitHub accounts.

## Features

- Python, `discord.py`, FastAPI, and SQLite
- Slash-command setup per Discord server
- Per-server linked repository configuration
- Per-server GitHub issue creation settings
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
GITHUB_APP_ID=replace-with-your-github-app-id
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/github-app-private-key.pem
API_HOST=0.0.0.0
API_PORT=8080
DATABASE_PATH=data/nano_github.sqlite3
```

`GITHUB_APP_PRIVATE_KEY` is also supported. If you store it in an environment variable, replace newlines with `\n`.

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
/github set_review_mode anyone
/github status
```

Use:

```text
/github unlink_repo
```

to remove linked repositories for the server.

## Discord Issue Creation

Issue creation is configured per Discord server and always checks that the selected GitHub repository is linked to that same server.

Server owners or members with Manage Server can configure it:

```text
/issue configure default_repo_owner default_repo_name suggestion bug #issue-submissions
/issue status
/issue disable
```

Normal server members can create issues unless issue creation has been disabled:

```text
/issue create type:suggestion title:"Add dark mode" description:"Please add a dark theme."
/issue create type:bug title:"Sync failed" description:"The webhook did not post." owner:nanoworks repo:nano-github
```

Repository selection works like this:

- If `owner` and `repo` are provided, that repository must already be linked to the Discord server.
- If no repository is provided and a default repository is configured, Nano GitHub uses the default.
- If no default is configured and the server has exactly one linked repository, Nano GitHub uses it automatically.
- If multiple repositories are linked and no default or explicit repository is available, Nano GitHub asks the user to specify `owner` and `repo`.

Suggestions receive the configured suggestion label, defaulting to `suggestion`. Bugs receive the configured bug label, defaulting to `bug`. If GitHub rejects the labels because they do not exist, the issue is still created and the Discord reply mentions that labels could not be applied.

Created GitHub issues include the submitted description plus a short Discord source footer containing only the Discord username.

Discord users do not need GitHub accounts. Issue creation uses the Nano GitHub GitHub App installation token for the selected repository. The GitHub App must be installed on that repository and have `Issues: Read and write` permission.

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

The button behavior is:

- **View Pull Request** opens GitHub and does not require GitHub API permissions.
- **Approve** submits a Pull Request Review with event `APPROVE`.
- **Request Changes** submits a Pull Request Review with event `REQUEST_CHANGES`.
- **Comment** opens a Discord modal, collects review text, and submits a Pull Request Review with event `COMMENT`.

Review actions use a repository installation token for the linked repository. The GitHub App must have `Pull requests: Read and write` permission. If the permission is missing, Nano GitHub replies with a friendly Discord error such as:

```text
Nano GitHub does not currently have Pull Request Review permissions for this repository.
```

PR review actions are configured per Discord server:

- `Anyone` allows any server member who can see the PR card to use the review buttons.
- `GitHub Reviewers Only` allows users whose Discord username or server display name matches a currently requested GitHub reviewer login cached from pull request webhooks.
- `Discord Role Restricted` allows only members with the configured Discord role.

Use `/github app_status owner repo` to verify repository installation, installation token generation, issue creation permission, and pull request review permission.

Required GitHub App repository permissions:

- Metadata: read-only, required by GitHub Apps.
- Issues: read and write, required for `/issue create`.
- Pull requests: read and write, required for Approve, Request Changes, and Comment.

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

