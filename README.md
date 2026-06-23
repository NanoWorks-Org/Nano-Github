# Nano GitHub

Nano GitHub is a Discord bot for GitHub notifications and pull request review workflows.

It deliberately separates read-only logging from pull request review:

- **Logging channels** receive normal GitHub event embeds for commits, issues, issue comments, and releases.
- **Pull request review channels** receive interactive PR review cards with buttons that submit GitHub Pull Request Reviews through the Nano GitHub GitHub App.
- **Discord issue creation** lets server members create labeled GitHub issues through the Nano GitHub GitHub App without needing GitHub accounts.

## Features

- Python, `discord.py`, FastAPI, and SQLite
- Slash-command setup per Discord server
- Per-server linked repository configuration
- Per-server GitHub issue creation settings
- GitHub App webhook endpoint at `POST /webhooks/github`
- HMAC SHA-256 verification for GitHub App and legacy webhook secrets
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
- A GitHub App with a webhook secret
- Docker, if running with Compose

## Environment

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Required variables:

```env
DISCORD_TOKEN=replace-with-your-discord-bot-token
GITHUB_APP_ID=replace-with-your-github-app-id
GITHUB_APP_WEBHOOK_SECRET=replace-with-your-github-app-webhook-secret
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/github-app-private-key.pem
API_HOST=0.0.0.0
API_PORT=8080
DATABASE_PATH=data/nano_github.sqlite3
```

`GITHUB_APP_PRIVATE_KEY` is also supported. If you store it in an environment variable, replace newlines with `\n`.

`GITHUB_WEBHOOK_SECRET` is still supported as a legacy fallback for old manually-created repository webhooks. New GitHub App webhook deliveries use `GITHUB_APP_WEBHOOK_SECRET`.

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

Nano GitHub keeps the visible slash-command surface small:

```text
/github dashboard
/issue create title description
```

Use `/github dashboard` for setup and administration. The dashboard is ephemeral and admin-only, and it is the preferred place to link repositories, choose defaults, review webhook details, check GitHub App readiness, and configure issue/PR behavior.

Normal setup is:

1. Invite the Discord bot.
2. Install the Nano GitHub GitHub App on the GitHub account, organization, or repository.
3. Open `/github dashboard`.
4. Select the installed repository and choose the Discord channels/defaults you want.

You do not need to create repository webhooks manually for normal GitHub App installs.

## Admin Dashboard

Use `/github dashboard` to open an ephemeral Nano GitHub admin control panel. The dashboard requires Administrator or Manage Server permission. If a normal user opens it, Nano GitHub replies ephemerally with:

```text
You need Administrator or Manage Server permission to use the Nano GitHub dashboard.
```

Dashboard sections:

- **Overview** shows linked repositories, log channels, PR review channel and mode, issue creation status, label settings, GitHub App readiness, webhook setup, and bot/API status.
- **Repositories** lists linked repositories, shows GitHub App installed repositories known to Nano GitHub, and lets admins link from an installed-repository dropdown. Manual owner/repo entry remains available as an advanced fallback.
- **Default Repository** shows the current default repository, linked repositories, and whether automatic single-repo defaulting is active. Admins can choose the default from a linked-repo dropdown.
- **Log Channels** shows configured event log channels and setup guidance.
- **PR Reviews** shows the PR review channel and mode, and includes an interactive selector for basic PR review mode changes.
- **Issue Creation** shows enabled/disabled state, default repository, submission log, allowed labels, default labels, and buttons to enable/disable issue creation or edit labels.
- **GitHub App** shows installed repositories known to Nano GitHub, whether the selected linked repository has an installation ID, and whether issue creation and PR review actions are available.
- **Webhook Info** explains that GitHub App webhooks are automatic. Manual payload URL and per-repository secrets are shown only as legacy/advanced information. The GitHub App webhook secret is never shown in Discord.

Standalone setup/status/config commands are no longer registered in the normal slash-command surface. The dashboard replaces those flows, with channel setup shown as dashboard guidance where a full picker is not implemented yet.

## Discord Issue Creation

Issue creation is configured per Discord server and always uses a repository linked to that same server.

Normal server members can create issues unless issue creation has been disabled:

```text
/issue create title:"Add dark mode" description:"Please add a dark theme."
/issue create title:"Sync failed" description:"The webhook did not post." labels:"bug"
```

Repository selection works like this:

- Dashboard interactions can explicitly choose a repository where needed.
- If a default repository is configured in the dashboard, Nano GitHub uses it.
- If no default is configured and the server has exactly one linked repository, Nano GitHub uses that repository automatically.
- If multiple repositories are linked and no default is configured, `/issue create` shows a Discord linked-repository dropdown.

Normal users do not type GitHub owner/repo names. Owner/repo entry is reserved for admin dashboard setup flows and advanced maintenance code paths.

Issue type is label-based. `/issue create` no longer asks for a separate suggestion/bug type. Labels determine whether an issue is a bug, suggestion, enhancement, feature request, or anything else your GitHub repository uses.

Labels are pulled live from the selected GitHub repository using the GitHub App installation token. Users can use slash-command autocomplete for the optional `labels` field, or run `/issue create` without labels and choose from a Discord label select menu before creating the issue. If the repository has more labels than Discord can display in a select menu, Nano GitHub shows the first set and autocomplete can be used to search.

If labels are selected, Nano GitHub applies those labels. If no labels are selected, Nano GitHub applies the server’s configured default labels if present, otherwise it creates the issue with no labels. Admins can configure allowed labels and default labels per server in the dashboard. When allowed labels are configured, the autocomplete and dropdown only show allowed labels. If a requested GitHub label disappears before creation, the issue is still created, valid labels are applied, and the Discord reply lists failed labels.

When `/issue create` creates a GitHub issue, Nano GitHub records the issue URL and number. If the GitHub `issues.opened` webhook arrives for that same issue, Nano GitHub suppresses the duplicate public issue-log embed while keeping the user command success response and the optional issue submission log.

Created GitHub issues include the submitted description plus a short Discord source footer containing only the Discord username.

Discord users do not need GitHub accounts. Issue creation uses the Nano GitHub GitHub App installation token for the selected repository. The GitHub App must be installed on that repository and have `Issues: Read and write` permission.

## GitHub App Webhook Setup

Configure the webhook on the Nano GitHub GitHub App itself:

1. In the GitHub App settings, enable webhooks.
2. Set **Webhook URL** to your public endpoint, for example:

   ```text
   https://your-domain.example/webhooks/github
   ```

3. Set **Webhook secret** to the same value as `GITHUB_APP_WEBHOOK_SECRET`.
4. Subscribe the App to these repository events:
   - Pushes
   - Issues
   - Issue comments
   - Pull requests
   - Releases
5. Save the GitHub App, then install it on the account, organization, or repository.

When GitHub sends App webhooks, Nano GitHub verifies `X-Hub-Signature-256` with `GITHUB_APP_WEBHOOK_SECRET`, records the App `installation.id`, and learns repositories from GitHub App installation payloads and repository webhook payloads. If a repository is installed but not linked to a Discord server yet, Nano GitHub accepts the delivery with `202` and logs that no guild is linked.

For local development, expose `localhost:8080` through a tunnel such as ngrok or Cloudflare Tunnel, then use the public tunnel URL as the Payload URL.

### Legacy Manual Repository Webhooks

Manual repository webhooks are optional and should only be used for older setups or advanced maintenance. In a GitHub repository, create a webhook only if you cannot use the GitHub App webhook:

1. Open **Settings** -> **Webhooks** -> **Add webhook**.
2. Set **Payload URL** to `https://your-domain.example/webhooks/github`.
3. Set **Content type** to `application/json`.
4. Set **Secret** to that linked repository's legacy secret from `/github dashboard` -> **Webhook Info**.
5. Select Pushes, Issues, Issue comments, Pull requests, and Releases.

Legacy per-repository secrets are scoped to the Discord guild/repository link. Nano GitHub does not expose `GITHUB_APP_WEBHOOK_SECRET` in Discord.

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
- **Approve** submits a Pull Request Review with event `APPROVE` and the body `Approved from Discord by <Discord username>.`
- **Request Changes** opens a Discord modal, requires a short reason, and submits a Pull Request Review with event `REQUEST_CHANGES`.
- **Comment** opens a Discord modal, collects review text, and submits a Pull Request Review with event `COMMENT` prefixed with `Comment from Discord by <Discord username>:`.

After Approve, Request Changes, or Comment succeeds on GitHub, Nano GitHub edits the original PR review card and adds or updates a `Latest Review Activity` field with the action, Discord username, short reason or comment when provided, and timestamp. The View Pull Request button remains on updated cards, and the review buttons use persistent custom IDs so they continue working after bot restarts.

PR review card colors:

- Waiting/open/pending review: orange
- Approved from Discord: green
- Changes requested from Discord: red
- Comment-only activity: neutral Nano blue

Review actions use a repository installation token for the linked repository. The GitHub App must have `Pull requests: Read and write` permission. If the permission is missing, Nano GitHub replies with a friendly Discord error such as:

```text
Nano GitHub does not currently have Pull Request Review permissions for this repository.
```

PR review actions are configured per Discord server:

- `Anyone` allows any server member who can see the PR card to use the review buttons.
- `GitHub Reviewers Only` allows users whose Discord username or server display name matches a currently requested GitHub reviewer login cached from pull request webhooks.
- `Discord Role Restricted` allows only members with the configured Discord role.

Use `/github dashboard` -> GitHub App to verify repository installation, installation token generation, issue creation permission, and pull request review permission. Dashboard actions are admin-only, with the dashboard accepting Administrator or Manage Server. Normal users can run `/issue create` when issue creation is enabled.

Required GitHub App repository permissions:

- Metadata: read-only, required by GitHub Apps.
- Issues: read and write, required for `/issue create`.
- Pull requests: read and write, required for Approve, Request Changes, and Comment.

## Webhook Behavior

- Webhook signatures are verified with `X-Hub-Signature-256`.
- GitHub App webhook signatures are verified with `GITHUB_APP_WEBHOOK_SECRET` when the payload includes an `installation`.
- Legacy manual repository webhooks continue to use per-repository secrets, with `GITHUB_WEBHOOK_SECRET` kept as a fallback for older deployments.
- Malformed JSON returns `400` and is logged without crashing the bot.
- App events for installed repositories not linked to a Discord server are accepted with `202` and logged as unlinked.
- Legacy events for repositories not linked to a Discord server are rejected unless they can be verified for a linked repository.
- If a channel is not configured for an event, the event is skipped and a warning is logged.
- PR review cards are updated when Nano GitHub has already stored a Discord message for that PR.
- Issue webhooks for issues created by `/issue create` are deduplicated against the recorded issue URL/number to avoid duplicate public issue embeds.

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

