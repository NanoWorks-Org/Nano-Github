# Nano GitHub

A Discord bot that connects GitHub repositories directly to Discord.

Nano GitHub provides:

- Commit logging
- Issue logging
- Pull request logging
- Release logging
- Comment logging
- Interactive pull request reviews from Discord
- GitHub App integration
- Multi-server support
- Webhook security verification
- Dashboard integration

---

# Features

## GitHub Activity Logging

Automatically send GitHub events into Discord channels.

Supported events:

- Pushes / Commits
- Issues
- Issue Comments
- Pull Requests
- Pull Request Reviews
- Pull Request Review Comments
- Releases

---

## Pull Request Review System

When a pull request is opened:

- A review embed is posted in Discord
- Reviewers can:
  - View PR
  - Approve
  - Request Changes

Responses are sent back to GitHub using the installed GitHub App.

---

## GitHub App Integration

Nano GitHub uses a GitHub App instead of personal access tokens.

Benefits:

- More secure
- Repository-scoped permissions
- Easier setup
- Supports multiple repositories

---

## Dashboard

The dashboard provides:

- Linked repositories
- Installed repositories
- Default repository
- Log channels
- PR review settings
- GitHub App readiness checks

---

# Requirements

## Discord

- Discord Server
- Manage Server permission
- Ability to invite bots

## GitHub

- GitHub organisation or repository
- Permission to install GitHub Apps

## Hosting

- Docker & Docker Compose recommended

or

- Python 3.11+

---

# Installation

## Option 1 – Docker (Recommended)

### Clone Repository

```bash
git clone https://github.com/nanoworks-org/nano-github.git
cd nano-github
```

---

### Create Environment File

Create:

```env
DISCORD_TOKEN=
CLIENT_ID=
GUILD_ID=

GITHUB_APP_ID=
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
GITHUB_WEBHOOK_SECRET=

PRIVATE_KEY_PATH=/app/private-key.pem

DATABASE_PATH=/app/data/nano_github.db

WEB_HOST=0.0.0.0
WEB_PORT=8080
PUBLIC_URL=https://your-domain.com
```

---

### Add GitHub App Private Key

Place:

```text
private-key.pem
```

inside the project directory.

---

### Start

```bash
docker compose up -d --build
```

Check logs:

```bash
docker logs -f nano-github
```

---

# GitHub App Setup

## Create GitHub App

Create a GitHub App with:

### Permissions

Repository Permissions:

| Permission | Access |
|------------|---------|
| Contents | Read |
| Pull Requests | Read & Write |
| Issues | Read & Write |
| Metadata | Read |
| Commit Statuses | Read |
| Actions | Read |

---

### Webhook Events

Enable:

- Push
- Pull Request
- Pull Request Review
- Pull Request Review Comment
- Issues
- Issue Comment
- Release

---

### Webhook URL

```text
https://your-domain.com/github/webhook
```

---

### Callback URL

```text
https://your-domain.com/github/install/callback
```

---

# Discord Setup

Invite the bot using:

```text
applications.commands
bot
```

Recommended permissions:

- View Channels
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Use Slash Commands

Administrator is NOT required.

---

# First-Time Setup

## 1. Install GitHub App

Run:

```text
/github connect
```

Install the GitHub App when prompted.

---

## 2. Link Repository

```text
/github repo add owner/repository
```

Example:

```text
/github repo add nanoworks-org/nano-github
```

---

## 3. Set Default Repository

```text
/github repo default owner/repository
```

Example:

```text
/github repo default nanoworks-org/nano-github
```

---

## 4. Configure Log Channels

Example:

```text
/github logs commits #github-logs
/github logs issues #github-logs
/github logs releases #github-logs
```

---

# Commands

## General

### Dashboard

```text
/github dashboard
```

Shows:

- Linked repositories
- Log channels
- PR settings
- Installation status

---

### Status

```text
/github status
```

Shows GitHub connectivity and health.

---

# Repository Commands

## Add Repository

```text
/github repo add owner/repository
```

Example:

```text
/github repo add nanoworks-org/nano-github
```

---

## Remove Repository

```text
/github repo remove owner/repository
```

---

## List Repositories

```text
/github repo list
```

---

## Set Default Repository

```text
/github repo default owner/repository
```

---

# Logging Commands

## Commit Logs

```text
/github logs commits #channel
```

---

## Issue Logs

```text
/github logs issues #channel
```

---

## Comment Logs

```text
/github logs comments #channel
```

---

## Release Logs

```text
/github logs releases #channel
```

---

## Pull Request Logs

```text
/github logs pullrequests #channel
```

---

# Pull Request Review Commands

## Configure Review Channel

```text
/github pr channel #reviews
```

---

## Restrict Reviews to Discord Role

```text
/github pr role @Developers
```

Only members with the role can approve or request changes.

---

## Disable Role Restriction

```text
/github pr role disable
```

---

# Issue Creation

## Enable

```text
/github issues enable
```

Allows users to create GitHub issues from Discord.

---

## Disable

```text
/github issues disable
```

---

## Set Submission Log

```text
/github issues log #github-submissions
```

---

# Example Workflow

## Developer Opens Pull Request

GitHub:

```text
feature/login-system
→ main
```

---

## Discord Receives Embed

```text
PR #42
Add login system

Author:
DuckQuack001

Repository:
nanoworks-org/nano-github
```

Buttons:

- View PR
- Approve
- Request Changes

---

## Reviewer Approves

Discord reviewer presses:

```text
Approve
```

Nano GitHub:

- Creates GitHub review
- Updates PR
- Logs action

---

# Security

Nano GitHub includes:

- GitHub webhook signature verification
- GitHub App authentication
- Repository ownership validation
- Role-restricted reviews
- SQLite persistence
- Audit logging

---

# Troubleshooting

## GitHub App Not Connected

Check:

```text
/github status
```

Verify:

- App installed
- App permissions granted
- Webhook URL reachable

---

## No Events Arriving

Check:

1. GitHub App installed on repository
2. Webhook deliveries successful
3. Bot online
4. Log channels configured

---

## Review Buttons Not Working

Check:

- GitHub App permissions
- PR review role restrictions
- Repository linked correctly

---

# Resetting Nano GitHub

If credentials are compromised:

1. Revoke GitHub App installation.
2. Generate a new private key.
3. Regenerate webhook secret.
4. Update `.env`.
5. Restart Nano GitHub.
6. Reinstall the GitHub App.

---

# Nano Works

Nano GitHub is part of the Nano Works ecosystem.

Additional services currently in development:

- Nano Community
- Nano Server
- Nano Dashboard

---
© Nano Works
