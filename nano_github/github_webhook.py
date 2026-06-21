from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import discord
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, status

from nano_github import embeds
from nano_github.config import Settings
from nano_github.database import Database
from nano_github.discord_bot import NanoGitHubBot, PullRequestReviewView

LOGGER = logging.getLogger(__name__)

router = APIRouter()

LOG_EVENT_MAP = {
    "push": "commits",
    "issues": "issues",
    "issue_comment": "comments",
    "release": "releases",
}

PR_ACTIONS = {
    "opened",
    "reopened",
    "ready_for_review",
    "synchronize",
    "closed",
}


def create_app(settings: Settings, db: Database, bot: NanoGitHubBot) -> FastAPI:
    app = FastAPI(title="Nano GitHub Webhooks")
    app.state.settings = settings
    app.state.db = db
    app.state.bot = bot
    app.include_router(router)
    return app


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    bot: NanoGitHubBot = request.app.state.bot

    if not _valid_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Malformed GitHub webhook payload: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed JSON") from exc

    if not isinstance(payload, dict):
        LOGGER.warning("Malformed GitHub webhook payload: JSON root is not an object")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload")

    event = x_github_event or ""
    action = payload.get("action")
    repo_info = _repository_info(payload)
    owner = repo_info["owner"]
    repo = repo_info["repo"]
    db.record_webhook_event(x_github_delivery, event, action, owner, repo, payload)

    if not owner or not repo:
        LOGGER.warning("Ignoring %s webhook without repository information", event)
        return {"ignored": True, "reason": "missing repository"}

    guild_ids = db.find_guilds_for_repository(owner, repo)
    if not guild_ids:
        LOGGER.info("Ignoring %s for unlinked repository %s/%s", event, owner, repo)
        return {"ignored": True, "reason": "repository not linked"}

    if event == "pull_request":
        if action not in PR_ACTIONS:
            LOGGER.info("Ignoring pull_request action %s for %s/%s", action, owner, repo)
            return {"ignored": True, "reason": "unsupported pull_request action"}
        await _dispatch_pull_request(bot, db, guild_ids, owner, repo, payload)
        return {"accepted": True, "event": event, "guilds": len(guild_ids)}

    log_event_type = LOG_EVENT_MAP.get(event)
    if not log_event_type:
        LOGGER.info("Ignoring unsupported GitHub event %s", event)
        return {"ignored": True, "reason": "unsupported event"}

    await _dispatch_log_event(bot, db, guild_ids, log_event_type, payload)
    return {"accepted": True, "event": event, "guilds": len(guild_ids)}


def _valid_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _repository_info(payload: dict[str, Any]) -> dict[str, str | None]:
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")
    owner = (repository.get("owner") or {}).get("login")
    repo = repository.get("name")

    if isinstance(full_name, str) and "/" in full_name:
        full_owner, full_repo = full_name.split("/", 1)
        owner = owner or full_owner
        repo = repo or full_repo

    return {
        "owner": owner.lower() if isinstance(owner, str) else None,
        "repo": repo.lower() if isinstance(repo, str) else None,
    }


async def _dispatch_log_event(
    bot: NanoGitHubBot,
    db: Database,
    guild_ids: list[int],
    event_type: str,
    payload: dict[str, Any],
) -> None:
    embed = _log_embed(event_type, payload)
    for guild_id in guild_ids:
        channel_id = db.get_log_channel(guild_id, event_type)
        if channel_id is None:
            LOGGER.warning(
                "Skipping %s event for guild %s because no log channel is configured",
                event_type,
                guild_id,
            )
            continue

        channel = await _resolve_text_channel(bot, channel_id)
        if channel is None:
            LOGGER.warning("Configured log channel %s is unavailable for guild %s", channel_id, guild_id)
            continue

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            LOGGER.warning("Missing permission to send %s event to channel %s", event_type, channel_id)
        except discord.HTTPException:
            LOGGER.exception("Failed to send %s event to channel %s", event_type, channel_id)


async def _dispatch_pull_request(
    bot: NanoGitHubBot,
    db: Database,
    guild_ids: list[int],
    owner: str,
    repo: str,
    payload: dict[str, Any],
) -> None:
    pr = payload.get("pull_request") or {}
    pr_number = pr.get("number")
    if not isinstance(pr_number, int):
        LOGGER.warning("Skipping malformed pull_request payload without numeric PR number")
        return

    state = "merged" if pr.get("merged") else str(pr.get("state", "unknown"))
    github_url = pr.get("html_url")
    embed = embeds.pull_request_embed(payload)

    for guild_id in guild_ids:
        channel_id = db.get_pr_review_channel(guild_id)
        if channel_id is None:
            LOGGER.warning(
                "Skipping pull request event for guild %s because no PR review channel is configured",
                guild_id,
            )
            continue

        channel = await _resolve_text_channel(bot, channel_id)
        if channel is None:
            LOGGER.warning(
                "Configured PR review channel %s is unavailable for guild %s",
                channel_id,
                guild_id,
            )
            continue

        view = PullRequestReviewView(github_url if isinstance(github_url, str) else None)
        existing = db.get_pr_message(guild_id, owner, repo, pr_number)
        if existing and existing.channel_id == channel_id:
            try:
                message = await channel.fetch_message(existing.message_id)
                await message.edit(embed=embed, view=view)
                db.upsert_pr_message(guild_id, owner, repo, pr_number, channel_id, message.id, state)
                continue
            except discord.NotFound:
                LOGGER.warning(
                    "Stored PR message %s was not found; sending a new PR card",
                    existing.message_id,
                )
            except discord.Forbidden:
                LOGGER.warning("Missing permission to update PR message in channel %s", channel_id)
                continue
            except discord.HTTPException:
                LOGGER.exception("Failed to update PR message in channel %s", channel_id)
                continue

        try:
            message = await channel.send(embed=embed, view=view)
            db.upsert_pr_message(guild_id, owner, repo, pr_number, channel_id, message.id, state)
        except discord.Forbidden:
            LOGGER.warning("Missing permission to send PR card to channel %s", channel_id)
        except discord.HTTPException:
            LOGGER.exception("Failed to send PR card to channel %s", channel_id)


def _log_embed(event_type: str, payload: dict[str, Any]) -> discord.Embed:
    if event_type == "commits":
        return embeds.push_embed(payload)
    if event_type == "issues":
        return embeds.issue_embed(payload)
    if event_type == "comments":
        return embeds.issue_comment_embed(payload)
    if event_type == "releases":
        return embeds.release_embed(payload)
    raise ValueError(f"Unsupported log event type: {event_type}")


async def _resolve_text_channel(
    bot: NanoGitHubBot,
    channel_id: int,
) -> discord.TextChannel | discord.Thread | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel

    LOGGER.warning("Configured channel %s is not a text channel or thread", channel_id)
    return None

