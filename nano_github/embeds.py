from __future__ import annotations

from typing import Any

import discord

NANO_BLUE = 0x2F80ED
NANO_DARK_BLUE = 0x163B73
NANO_GREEN = 0x2EA043
NANO_RED = 0xDA3633
NANO_GOLD = 0xD29922


def push_embed(payload: dict[str, Any]) -> discord.Embed:
    repo = payload.get("repository") or {}
    commits = payload.get("commits") or []
    compare_url = payload.get("compare") or repo.get("html_url")
    branch = _branch_name(payload.get("ref"))

    title = f"{len(commits)} commit{'s' if len(commits) != 1 else ''} pushed"
    embed = discord.Embed(title=title, url=compare_url, color=NANO_DARK_BLUE)
    embed.description = f"**{repo.get('full_name', 'Unknown repository')}** `{branch}`"

    for commit in commits[:5]:
        message = (commit.get("message") or "No commit message").splitlines()[0]
        commit_id = (commit.get("id") or "")[:7]
        author = (commit.get("author") or {}).get("name") or "Unknown author"
        commit_url = commit.get("url")
        embed.add_field(
            name=f"`{commit_id}` {author}",
            value=f"[{_truncate(message, 180)}]({commit_url})" if commit_url else _truncate(message, 180),
            inline=False,
        )

    if len(commits) > 5:
        embed.set_footer(text=f"{len(commits) - 5} more commits on GitHub")

    return embed


def issue_embed(payload: dict[str, Any]) -> discord.Embed:
    issue = payload.get("issue") or {}
    repo = payload.get("repository") or {}
    action = payload.get("action", "updated").replace("_", " ")
    number = issue.get("number", "?")
    color = NANO_GREEN if action == "opened" else NANO_DARK_BLUE

    embed = discord.Embed(
        title=f"Issue #{number} {action}: {issue.get('title', 'Untitled issue')}",
        url=issue.get("html_url"),
        color=color,
    )
    embed.add_field(name="Repository", value=repo.get("full_name", "Unknown"), inline=True)
    embed.add_field(name="Author", value=(issue.get("user") or {}).get("login", "Unknown"), inline=True)
    embed.add_field(name="State", value=issue.get("state", "unknown"), inline=True)

    body = issue.get("body")
    if body:
        embed.description = _truncate(body, 500)

    return embed


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
    embed.add_field(name="Author", value=(comment.get("user") or {}).get("login", "Unknown"), inline=True)
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
        title=f"{prerelease} {action}: {release.get('name') or release.get('tag_name') or 'Untitled'}",
        url=release.get("html_url"),
        color=NANO_GOLD,
    )
    embed.add_field(name="Repository", value=repo.get("full_name", "Unknown"), inline=True)
    embed.add_field(name="Tag", value=release.get("tag_name", "unknown"), inline=True)
    embed.add_field(name="Author", value=(release.get("author") or {}).get("login", "Unknown"), inline=True)

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

    title = f"Pull Request #{pr.get('number', '?')} {action}: {pr.get('title', 'Untitled PR')}"
    embed = discord.Embed(title=title, url=pr.get("html_url"), color=color)
    embed.add_field(name="Repository", value=repo.get("full_name", "Unknown"), inline=True)
    embed.add_field(name="Author", value=(pr.get("user") or {}).get("login", "Unknown"), inline=True)
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


def _pr_color(action: str, state: str, draft: bool) -> int:
    if state == "merged":
        return NANO_BLUE
    if state == "closed":
        return NANO_RED
    if draft:
        return NANO_GOLD
    if action in {"opened", "reopened", "ready for review"}:
        return NANO_GREEN
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
    return ref.removeprefix("refs/heads/")


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}..."
