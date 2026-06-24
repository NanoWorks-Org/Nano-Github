# Nano GitHub

Nano GitHub allows your community, development team, or staff team to track repository activity, review pull requests, and manage GitHub integrations without leaving Discord.

---

## Features

### GitHub Activity Logging

Automatically receive Discord notifications for:

* Commits
* Pull Requests
* Pull Request Reviews
* Issues
* Issue Comments
* Releases

---

### Pull Request Reviews

Review pull requests directly from Discord.

Team members can:

* View pull requests
* Approve pull requests
* Request changes

Actions are synced back to GitHub automatically.

---

### Issue Creation

Allow Discord users to submit GitHub issues without opening GitHub.

Perfect for:

* Suggestions
* Bug reports
* Community feedback

---

### Multiple Repositories

Link multiple repositories to a single Discord server.

Choose a default repository for commands and issue submissions.

---

## Getting Started

### Step 1 — Invite Nano GitHub

Invite Nano GitHub to your Discord server.

The bot requires:

* View Channels
* Send Messages
* Embed Links
* Read Message History
* Use Slash Commands

Administrator permission is not required.

---

### Step 2 — Connect GitHub

Run:

/github connect

Nano GitHub will provide a secure GitHub installation link.

Install the Nano GitHub App onto:

* Your personal account
* Your organisation
* Selected repositories

---

### Step 3 — Link a Repository

Run:

/github repo add owner/repository

Example:

/github repo add nanoworks-org/nano-github

---

### Step 4 — Set a Default Repository

Run:

/github repo default owner/repository

Example:

/github repo default nanoworks-org/nano-github

This repository will be used for issue creation and other repository-specific features.

---

## Setting Up Logs

You can choose separate channels for each event type.

### Commit Logs

/github logs commits #github-logs

---

### Issue Logs

/github logs issues #github-logs

---

### Comment Logs

/github logs comments #github-logs

---

### Release Logs

/github logs releases #github-logs

---

### Pull Request Logs

/github logs pullrequests #github-logs

---

## Pull Request Reviews

### Set Review Channel

/github pr channel #pull-request-reviews

Whenever a pull request is opened, Nano GitHub will post an interactive review card.

---

### Restrict Reviews

/github pr role @Developers

Only members with this role will be able to:

* Approve pull requests
* Request changes

---

### Remove Restrictions

/github pr role disable

Anyone with access to the review channel may review pull requests.

---

## Issue Creation

### Enable Issue Creation

/github issues enable

---

### Disable Issue Creation

/github issues disable

---

### Configure Submission Logs

/github issues log #github-submissions

All issue submissions will be logged for staff review.

---

## Commands

### General

/github dashboard

View:

* Connected repositories
* Installed repositories
* Log channels
* Review settings
* Issue settings

---

/github status

Check GitHub connection status.

---

### Repository Management

/github repo add owner/repository

/github repo remove owner/repository

/github repo list

/github repo default owner/repository

---

### Logging

/github logs commits #channel

/github logs issues #channel

/github logs comments #channel

/github logs releases #channel

/github logs pullrequests #channel

---

### Pull Requests

/github pr channel #channel

/github pr role @role

/github pr role disable

---

### Issues

/github issues enable

/github issues disable

/github issues log #channel

---

## Frequently Asked Questions

### Does Nano GitHub need Administrator permission?

No.

Only the permissions required to send messages, embeds, and slash commands are needed.

---

### Can I connect multiple repositories?

Yes.

You can link multiple repositories and choose a default repository.

---

### Can I limit who reviews pull requests?

Yes.

Use role restrictions to allow only approved reviewers.

---

### Why are no GitHub events appearing?

Check that:

* The GitHub App is installed
* The repository is linked
* Log channels are configured
* The bot can send messages in the selected channels

---

## Support

If you encounter issues, use:

/github status

to check your current configuration and GitHub App status.

---

Nano GitHub • Built by Nano Works
