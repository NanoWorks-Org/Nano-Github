from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord

NANO_BLUE = 0x2F80ED
NANO_DARK_BLUE = 0x163B73
NANO_GREEN = 0x2EA043
NANO_RED = 0xDA3633
NANO_GOLD = 0xD29922

ISSUE_COLORS = {
    "opened": NANO_GREEN,
    "reopened": NANO_GOLD,
    "edited": NANO_BLUE,
    "closed": NANO_RED,
}


@dataclass(frozen=True)
class EmbedMessage:
    embed: discord.Embed
    view: discord.ui.View | None = None


def push_embed(payload: dict[str, Any]) -> discord.Embed:
    messages = push_messages(payload)
    if messages:
        return messages[0].embed

    repo = payload.get("repository") or {}
    branch = _branch_name(payload.get("ref"))
    return discord.Embed(
        title=f"No commits pushed to {branch}",
        description=f"**{repo.get('full_name', 'Unknown repository')}**",
        color=NANO_DARK_BLUE,
    )


def push_messages(payload: dict[str, Any]) -> list[EmbedMessage]:
    commits = [commit for commit in payload.get("commits") or [] if isinstance(commit, dict)]

    if not commits:
        if payload.get("created") or payload.get("deleted"):
            return [_branch_change_message(payload)]
        return []

    messages: list[EmbedMessage] = []
    if len(commits) > 5:
        messages.append(_push_summary_message(payload, len(commits)))

    messages.extend(_commit_message(payload, commit) for commit in commits[:5])
    return messages


def _push_summary_message(payload: dict[str, Any], commit_count: int) -> EmbedMessage:
    repo = payload.get("repository") or {}
    compare_url = _string(payload.get("compare"))
    branch = _branch_name(payload.get("ref"))
    pusher = (payload.get("pusher") or {}).get("name") or (payload.get("sender") or {}).get("login")

    embed = discord.Embed(
        title=f"{commit_count} commits pushed",
        url=compare_url,
        color=NANO_DARK_BLUE,
    )
    embed.description = "Showing the first 5 commits from this push."
    embed.add_field(name="Repository", value=_repo_name(repo), inline=True)
    embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    embed.add_field(name="Pusher", value=_string(pusher, "Unknown"), inline=True)
    if compare_url:
        embed.add_field(name="Compare", value=f"[View changes]({compare_url})", inline=False)

    return EmbedMessage(embed=embed, view=_link_view("View Changes", compare_url))


def _branch_change_message(payload: dict[str, Any]) -> EmbedMessage:
    repo = payload.get("repository") or {}
    compare_url = _string(payload.get("compare") or repo.get("html_url"))
    branch = _branch_name(payload.get("ref"))
    action = "created" if payload.get("created") else "deleted"
    color = NANO_GREEN if action == "created" else NANO_RED

    embed = discord.Embed(
        title=f"Branch {action}: {branch}",
        url=compare_url,
        color=color,
    )
    embed.add_field(name="Repository", value=_repo_name(repo), inline=True)
    embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    embed.add_field(
        name="Pusher",
        value=_string((payload.get("pusher") or {}).get("name"), "Unknown"),
        inline=True,
    )

    return EmbedMessage(embed=embed, view=_link_view("View Changes", compare_url))


def _commit_message(payload: dict[str, Any], commit: dict[str, Any]) -> EmbedMessage:
    repo = payload.get("repository") or {}
    branch = _branch_name(payload.get("ref"))
    title, body = _commit_title_and_body(commit.get("message"))
    commit_url = _string(commit.get("html_url") or commit.get("url"))
    short_sha = _short_sha(commit)
    author = commit.get("author") or {}

    embed = discord.Embed(title=_truncate(title, 256), url=commit_url, color=NANO_DARK_BLUE)
    embed.description = _truncate(body or "No commit description provided.", 700)
    embed.add_field(name="Repository", value=_repo_name(repo), inline=True)
    embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    embed.add_field(name="Author", value=_commit_author_name(author), inline=True)
    embed.add_field(name="Short SHA", value=f"`{short_sha}`", inline=True)

    changed_files = _changed_file_count(commit)
    additions, deletions = _line_counts(commit)
    changes = []
    if changed_files is not None:
        changes.append(f"{changed_files} changed file{'s' if changed_files != 1 else ''}")
    if additions is not None:
        changes.append(f"+{additions}")
    if deletions is not None:
        changes.append(f"-{deletions}")
    if changes:
        embed.add_field(name="Changes", value=" | ".join(changes), inline=True)

    thumbnail_url = _commit_author_avatar(payload, author)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    return EmbedMessage(embed=embed, view=_link_view("View Commit", commit_url))


def issue_embed(payload: dict[str, Any]) -> discord.Embed:
    return issue_message(payload).embed


def issue_message(payload: dict[str, Any]) -> EmbedMessage:
    issue = payload.get("issue") or {}
    repo = payload.get("repository") or {}
    raw_action = _string(payload.get("action"), "updated")
    action = raw_action.replace("_", " ")
    number = issue.get("number", "?")
    issue_url = _string(issue.get("html_url"))
    title = (
        f"Issue #{number} {action}: "
        f"{_truncate(_string(issue.get('title'), 'Untitled issue'), 180)}"
    )

    embed = discord.Embed(
        title=_truncate(title, 256),
        url=issue_url,
        color=ISSUE_COLORS.get(raw_action, NANO_DARK_BLUE),
    )
    embed.description = _truncate(_string(issue.get("body"), "No description provided."), 500)
    embed.add_field(name="Repository", value=_repo_name(repo), inline=True)
    embed.add_field(
        name="Author",
        value=_string((issue.get("user") or {}).get("login"), "Unknown"),
        inline=True,
    )
    embed.add_field(name="State", value=_string(issue.get("state"), "unknown"), inline=True)
    embed.add_field(name="Labels", value=_labels(issue.get("labels")), inline=False)

    avatar_url = _string((issue.get("user") or {}).get("avatar_url"))
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    return EmbedMessage(embed=embed, view=_link_view("View Issue", issue_url))


def issue_comment_embed(payload: dict[str, Any]) -> discord.Embed:
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    repo = payload.get("repository") or {}
    action = payload.get("action", "updated").replace("_", " ")

    embed = discord.Embed(
        title=f"Comment {action} on issue #{issue.get('number', '?')}",
        url=comment.get("html_url") or issue.get("html_url"),
        color=NANO_BLUE,
    )
    embed.add_field(name="Repository", value=repo.get("full_name", "Unknown"), inline=True)
    embed.add_field(
        name="Author",
        value=(comment.get("user") or {}).get("login", "Unknown"),
        inline=True,
    )
    embed.add_field(name="Issue", value=issue.get("title", "Untitled issue"), inline=False)

    body = comment.get("body")
    if body:
        embed.description = _truncate(body, 700)

    return embed


def release_embed(payload: dict[str, Any]) -> discord.Embed:
    release = payload.get("release") or {}
    repo = payload.get("repository") or {}
    action = payload.get("action", "published").replace("_", " ")
    prerelease = "Pre-release" if release.get("prerelease") else "Release"

    embed = discord.Embed(
        title=f"{prerelease} {action}: "
        f"{release.get('name') or release.get('tag_name') or 'Untitled'}",
        url=release.get("html_url"),
        color=NANO_GOLD,
    )
    embed.add_field(name="Repository", value=repo.get("full_name", "Unknown"), inline=True)
    embed.add_field(name="Tag", value=release.get("tag_name", "unknown"), inline=True)
    embed.add_field(
        name="Author",
        value=(release.get("author") or {}).get("login", "Unknown"),
        inline=True,
    )

    body = release.get("body")
    if body:
        embed.description = _truncate(body, 700)

    return embed


def pull_request_embed(payload: dict[str, Any]) -> discord.Embed:
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    action = _action_label(payload.get("action", "updated"))
    merged = bool(pr.get("merged"))
    state = "merged" if merged else pr.get("state", "unknown")
    color = _pr_color(action, state, bool(pr.get("draft")))

    title = (
        f"Pull Request #{pr.get('number', '?')} {action}: "
        f"{pr.get('title', 'Untitled PR')}"
    )
    embed = discord.Embed(title=title, url=pr.get("html_url"), color=color)
    embed.add_field(name="Repository", value=repo.get("full_name", "Unknown"), inline=True)
    embed.add_field(
        name="Author",
        value=(pr.get("user") or {}).get("login", "Unknown"),
        inline=True,
    )
    embed.add_field(name="State", value=_pr_state_label(pr, action), inline=True)

    head = pr.get("head") or {}
    base = pr.get("base") or {}
    source = _branch_ref(head)
    target = _branch_ref(base)
    embed.add_field(name="Branch", value=f"`{source}` -> `{target}`", inline=False)

    changed_files = pr.get("changed_files")
    additions = pr.get("additions")
    deletions = pr.get("deletions")
    stats = []
    if changed_files is not None:
        stats.append(f"{changed_files} changed file{'s' if changed_files != 1 else ''}")
    if additions is not None:
        stats.append(f"+{additions}")
    if deletions is not None:
        stats.append(f"-{deletions}")
    if stats:
        embed.add_field(name="Changes", value=" | ".join(stats), inline=False)

    body = pr.get("body")
    if body:
        embed.description = _truncate(body, 700)

    embed.set_footer(text="Nano GitHub PR review channel")
    return embed


def issue_submission_success_embed(
    *,
    issue_title: str,
    issue_number: int,
    labels: tuple[str, ...],
    failed_labels: tuple[str, ...],
    repository: str,
    submitted_by: str,
    issue_url: str,
    labels_applied: bool,
    label_error: str | None = None,
) -> discord.Embed:
    color = NANO_GREEN if labels_applied else NANO_GOLD
    embed = discord.Embed(
        title=_truncate(issue_title, 256),
        url=issue_url,
        color=color,
    )
    embed.add_field(name="Issue", value=f"#{issue_number}", inline=True)
    embed.add_field(name="Labels", value=_label_names(labels), inline=True)
    embed.add_field(name="Repository", value=f"`{repository}`", inline=True)
    embed.add_field(name="Submitted by", value=submitted_by, inline=True)
    embed.add_field(name="GitHub issue", value=f"[Open issue]({issue_url})", inline=False)
    if not labels_applied:
        embed.add_field(
            name="Label warnings",
            value=(
                f"Issue created, but some labels could not be applied. {label_error}"
                if label_error
                else "Issue created, but some labels could not be applied."
            ),
            inline=False,
        )
    if failed_labels:
        embed.add_field(name="Rejected labels", value=_label_names(failed_labels), inline=False)
    embed.set_footer(text="Nano GitHub issue submission")
    return embed


def issue_submission_log_embed(
    *,
    issue_title: str,
    issue_number: int,
    labels: tuple[str, ...],
    repository: str,
    submitted_by: str,
    channel_id: int,
    issue_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=_truncate(f"Discord issue submitted: {issue_title}", 256),
        url=issue_url,
        color=NANO_DARK_BLUE,
    )
    embed.add_field(name="Issue", value=f"#{issue_number}", inline=True)
    embed.add_field(name="Labels", value=_label_names(labels), inline=True)
    embed.add_field(name="Repository", value=f"`{repository}`", inline=True)
    embed.add_field(name="Submitted by", value=submitted_by, inline=True)
    embed.add_field(name="Channel", value=f"<#{channel_id}>", inline=True)
    embed.add_field(name="GitHub issue", value=f"[Open issue]({issue_url})", inline=False)
    return embed


def issue_creation_blocked_attempt_embed(
    *,
    user: str,
    guild: str,
    blocked_roles: tuple[str, ...],
) -> discord.Embed:
    timestamp = discord.utils.utcnow()
    embed = discord.Embed(
        title="Blocked Discord issue creation attempt",
        color=NANO_RED,
    )
    embed.add_field(name="User", value=user, inline=False)
    embed.add_field(name="Guild", value=guild, inline=False)
    embed.add_field(
        name="Blocked roles",
        value=", ".join(blocked_roles) if blocked_roles else "Unknown",
        inline=False,
    )
    embed.add_field(name="Attempted action", value="Issue creation", inline=True)
    embed.add_field(name="Timestamp", value=f"<t:{int(timestamp.timestamp())}:f>", inline=True)
    embed.timestamp = timestamp
    return embed


def _pr_color(action: str, state: str, draft: bool) -> int:
    if state == "merged":
        return NANO_BLUE
    if state == "closed":
        return NANO_RED
    if draft:
        return NANO_GOLD
    if action in {"opened", "reopened", "ready for review"}:
        return NANO_GOLD
    return NANO_DARK_BLUE


def _action_label(action: str) -> str:
    if action == "synchronize":
        return "synchronized"
    return action.replace("_", " ")


def _pr_state_label(pr: dict[str, Any], action: str) -> str:
    if pr.get("merged"):
        return "merged"
    if pr.get("draft"):
        return f"{pr.get('state', 'unknown')} draft"
    return f"{pr.get('state', 'unknown')} ({action})"


def _branch_ref(ref: dict[str, Any]) -> str:
    repo_name = ((ref.get("repo") or {}).get("full_name")) or ref.get("label")
    branch = ref.get("ref") or "unknown"
    return f"{repo_name}:{branch}" if repo_name else branch


def _branch_name(ref: str | None) -> str:
    if not ref:
        return "unknown"
    return ref.removeprefix("refs/heads/").removeprefix("refs/tags/")


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}\u2026"


def _string(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        value = value.strip()
        if value:
            return value
    return fallback


def _repo_name(repo: dict[str, Any]) -> str:
    return _string(repo.get("full_name") or repo.get("name"), "Unknown repository")


def _link_view(label: str, url: str | None) -> discord.ui.View | None:
    if not url:
        return None

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
    return view


def _labels(labels: Any) -> str:
    if not isinstance(labels, list) or not labels:
        return "None"

    names = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            names.append(f"`{name.strip()}`")

    return _truncate(", ".join(names), 900) if names else "None"


def _label_names(labels: tuple[str, ...]) -> str:
    if not labels:
        return "None"
    return _truncate(", ".join(f"`{label}`" for label in labels), 900)


def _commit_title_and_body(message: Any) -> tuple[str, str]:
    message = _string(message, "No commit message")
    lines = message.splitlines()
    title = lines[0].strip() if lines and lines[0].strip() else "No commit message"
    body = "\n".join(line.rstrip() for line in lines[1:]).strip()
    return title, body


def _short_sha(commit: dict[str, Any]) -> str:
    commit_id = _string(commit.get("id") or commit.get("sha"))
    return commit_id[:7] if commit_id else "unknown"


def _commit_author_name(author: dict[str, Any]) -> str:
    return _string(author.get("name") or author.get("username") or author.get("login"), "Unknown")


def _commit_author_avatar(payload: dict[str, Any], author: dict[str, Any]) -> str | None:
    avatar_url = _string(author.get("avatar_url"))
    if avatar_url:
        return avatar_url

    sender = payload.get("sender") or {}
    author_login = _string(author.get("username") or author.get("login"))
    if author_login and author_login == sender.get("login"):
        return _string(sender.get("avatar_url")) or None

    return None


def _changed_file_count(commit: dict[str, Any]) -> int | None:
    filenames: set[str] = set()
    for key in ("added", "removed", "modified"):
        values = commit.get(key)
        if isinstance(values, list):
            filenames.update(str(value) for value in values if value)

    if filenames:
        return len(filenames)

    value = commit.get("changed_files")
    return value if isinstance(value, int) else None


def _line_counts(commit: dict[str, Any]) -> tuple[int | None, int | None]:
    stats = commit.get("stats") or {}
    additions = commit.get("additions")
    deletions = commit.get("deletions")
    if additions is None:
        additions = stats.get("additions")
    if deletions is None:
        deletions = stats.get("deletions")

    return (
        additions if isinstance(additions, int) else None,
        deletions if isinstance(deletions, int) else None,
    )
