from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import discord
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from nano_github import embeds
from nano_github.config import Settings
from nano_github.database import Database, LinkedRepository
from nano_github.discord_bot import NanoGitHubBot, PullRequestReviewView
from nano_github.github_setup import (
    SETUP_INVALID_TOKEN_MESSAGE,
    complete_github_installation_setup,
)

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
    "review_requested",
    "review_request_removed",
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


@router.get("/github/install/callback", response_class=HTMLResponse)
async def github_install_callback(
    request: Request,
    state: str | None = None,
    installation_id: int | None = None,
    setup_action: str | None = None,
) -> HTMLResponse:
    db: Database = request.app.state.db
    success, message = complete_github_installation_setup(
        db,
        state,
        installation_id,
        setup_action,
    )
    if not success and message != SETUP_INVALID_TOKEN_MESSAGE:
        return _html_response(
            message,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    if not success:
        return _install_error_response()
    return _html_response(message)


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

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Malformed GitHub webhook payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON",
        ) from exc

    if not isinstance(payload, dict):
        LOGGER.warning("Malformed GitHub webhook payload: JSON root is not an object")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload")

    event = x_github_event or ""
    action = payload.get("action")
    repo_info = _repository_info(payload)
    owner = repo_info["owner"]
    repo = repo_info["repo"]
    repository_full_name = repo_info["full_name"]
    installation_id = _installation_id(payload)

    if not owner or not repo:
        if installation_id is not None:
            if not _valid_signature(body, x_hub_signature_256, settings.github_app_webhook_secret):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
            stored_count = _store_installation_repositories(db, installation_id, payload)
            LOGGER.info(
                "Accepted %s installation webhook for installation %s with %s repositories stored",
                event,
                installation_id,
                stored_count,
            )
            return {"accepted": True, "event": event, "repositories": stored_count}
        LOGGER.warning("Ignoring %s webhook without repository information", event)
        return {"ignored": True, "reason": "missing repository"}

    linked_repositories = _linked_repositories_for_payload(db, owner, repo, installation_id)
    app_signature_valid = installation_id is not None and _valid_signature(
        body,
        x_hub_signature_256,
        settings.github_app_webhook_secret,
    )
    verified_repositories = _verified_repositories(
        body=body,
        signature=x_hub_signature_256,
        linked_repositories=linked_repositories,
        app_signature_valid=app_signature_valid,
        legacy_fallback_secret=settings.github_webhook_secret,
        installation_id=installation_id,
    )
    if not verified_repositories and not (app_signature_valid and not linked_repositories):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    if installation_id is not None:
        db.upsert_installed_repository(
            installation_id,
            owner,
            repo,
            repository_full_name,
        )

    if not linked_repositories:
        LOGGER.info(
            "Accepted %s for installed repository %s/%s installation %s with no linked guild",
            event,
            owner,
            repo,
            installation_id,
        )
        return {"accepted": True, "event": event, "guilds": 0, "reason": "repository not linked"}

    guild_ids = [linked_repo.guild_id for linked_repo in verified_repositories]
    db.record_webhook_event(x_github_delivery, event, action, owner, repo, payload)

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

    if log_event_type == "issues" and action == "opened":
        guild_ids = [
            guild_id
            for guild_id in guild_ids
            if not _is_command_created_issue(db, guild_id, owner, repo, payload)
        ]
        if not guild_ids:
            LOGGER.info("Suppressing duplicate issue log for Discord-created %s/%s issue", owner, repo)
            return {"accepted": True, "event": event, "guilds": 0, "deduplicated": True}

    messages = _log_messages(log_event_type, payload)
    if not messages:
        LOGGER.info(
            "Ignoring %s event for %s/%s because it has no dispatchable messages",
            event,
            owner,
            repo,
        )
        return {"ignored": True, "reason": "no dispatchable messages"}

    await _dispatch_log_event(bot, db, guild_ids, log_event_type, messages)
    return {"accepted": True, "event": event, "guilds": len(guild_ids)}


def _install_error_response() -> HTMLResponse:
    return _html_response(
        SETUP_INVALID_TOKEN_MESSAGE,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _html_response(message: str, status_code: int = status.HTTP_200_OK) -> HTMLResponse:
    return HTMLResponse(
        (
            "<!doctype html><html><head><title>Nano GitHub</title></head>"
            f"<body><p>{message}</p></body></html>"
        ),
        status_code=status_code,
    )


def _verified_repositories(
    body: bytes,
    signature: str | None,
    linked_repositories: list[LinkedRepository],
    app_signature_valid: bool,
    legacy_fallback_secret: str | None,
    installation_id: int | None,
) -> list[LinkedRepository]:
    if installation_id is not None and app_signature_valid:
        LOGGER.info(
            "Accepted GitHub App webhook for %s/%s installation %s",
            linked_repositories[0].owner if linked_repositories else "unlinked",
            linked_repositories[0].repo if linked_repositories else "repository",
            installation_id,
        )
        return linked_repositories

    if installation_id is not None and not linked_repositories:
        return []

    verified = [
        linked_repo
        for linked_repo in linked_repositories
        if _valid_signature(body, signature, linked_repo.webhook_secret)
    ]
    if verified:
        return verified

    if legacy_fallback_secret and _valid_signature(body, signature, legacy_fallback_secret):
        LOGGER.warning(
            "Accepted GitHub webhook for %s/%s with fallback GITHUB_WEBHOOK_SECRET",
            linked_repositories[0].owner,
            linked_repositories[0].repo,
        )
        return linked_repositories

    return []


def _valid_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    if not signature or not secret:
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _linked_repositories_for_payload(
    db: Database,
    owner: str,
    repo: str,
    installation_id: int | None,
) -> list[LinkedRepository]:
    if installation_id is not None:
        return db.find_linked_repositories_for_installation(installation_id, owner, repo)
    return db.find_linked_repositories(owner, repo)


def _installation_id(payload: dict[str, Any]) -> int | None:
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        return None
    installation_id = installation.get("id")
    return installation_id if isinstance(installation_id, int) else None


def _store_installation_repositories(
    db: Database,
    installation_id: int,
    payload: dict[str, Any],
) -> int:
    stored = 0
    for repository in _installation_repositories(payload):
        full_name = repository.get("full_name")
        owner = (repository.get("owner") or {}).get("login")
        repo = repository.get("name")
        if isinstance(full_name, str) and "/" in full_name:
            full_owner, full_repo = full_name.split("/", 1)
            owner = owner or full_owner
            repo = repo or full_repo
        if not isinstance(owner, str) or not isinstance(repo, str):
            continue
        db.upsert_installed_repository(
            installation_id,
            owner,
            repo,
            full_name if isinstance(full_name, str) else None,
        )
        stored += 1
    return stored


def _installation_repositories(payload: dict[str, Any]) -> list[dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    for key in ("repositories", "repositories_added"):
        value = payload.get(key)
        if isinstance(value, list):
            repositories.extend(item for item in value if isinstance(item, dict))
    return repositories


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
        "full_name": full_name.lower() if isinstance(full_name, str) else None,
    }


async def _dispatch_log_event(
    bot: NanoGitHubBot,
    db: Database,
    guild_ids: list[int],
    event_type: str,
    messages: list[embeds.EmbedMessage],
) -> None:
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
            LOGGER.warning(
                "Configured log channel %s is unavailable for guild %s",
                channel_id,
                guild_id,
            )
            continue

        try:
            for message in messages:
                await channel.send(embed=message.embed, view=message.view)
        except discord.Forbidden:
            LOGGER.warning(
                "Missing permission to send %s event to channel %s",
                event_type,
                channel_id,
            )
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
    requested_reviewers = _requested_reviewer_logins(pr)
    requested_teams = _requested_team_slugs(pr)

    for guild_id in guild_ids:
        channel_id = db.get_pr_review_channel(guild_id)
        if channel_id is None:
            LOGGER.warning(
                "Skipping pull request event for guild %s because no PR review channel "
                "is configured",
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
                db.upsert_pr_message(
                    guild_id,
                    owner,
                    repo,
                    pr_number,
                    channel_id,
                    message.id,
                    state,
                    requested_reviewers,
                    requested_teams,
                )
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
            db.upsert_pr_message(
                guild_id,
                owner,
                repo,
                pr_number,
                channel_id,
                message.id,
                state,
                requested_reviewers,
                requested_teams,
            )
        except discord.Forbidden:
            LOGGER.warning("Missing permission to send PR card to channel %s", channel_id)
        except discord.HTTPException:
            LOGGER.exception("Failed to send PR card to channel %s", channel_id)


def _log_messages(event_type: str, payload: dict[str, Any]) -> list[embeds.EmbedMessage]:
    if event_type == "commits":
        return embeds.push_messages(payload)
    if event_type == "issues":
        return [embeds.issue_message(payload)]
    if event_type == "comments":
        return [embeds.EmbedMessage(embeds.issue_comment_embed(payload))]
    if event_type == "releases":
        return [embeds.EmbedMessage(embeds.release_embed(payload))]
    raise ValueError(f"Unsupported log event type: {event_type}")


def _is_command_created_issue(
    db: Database,
    guild_id: int,
    owner: str,
    repo: str,
    payload: dict[str, Any],
) -> bool:
    issue = payload.get("issue") or {}
    issue_number = issue.get("number")
    issue_url = issue.get("html_url")
    if db.has_issue_submission(
        guild_id,
        owner,
        repo,
        issue_number=issue_number if isinstance(issue_number, int) else None,
        issue_url=issue_url if isinstance(issue_url, str) else None,
    ):
        return True

    body = issue.get("body")
    return (
        isinstance(body, str)
        and "Submitted from Discord" in body
        and "Discord username:" in body
    )


def _requested_reviewer_logins(pr: dict[str, Any]) -> list[str]:
    reviewers = pr.get("requested_reviewers")
    if not isinstance(reviewers, list):
        return []
    return [
        login.strip().lower()
        for reviewer in reviewers
        if isinstance(reviewer, dict)
        for login in [reviewer.get("login")]
        if isinstance(login, str) and login.strip()
    ]


def _requested_team_slugs(pr: dict[str, Any]) -> list[str]:
    teams = pr.get("requested_teams")
    if not isinstance(teams, list):
        return []
    return [
        slug.strip().lower()
        for team in teams
        if isinstance(team, dict)
        for slug in [team.get("slug")]
        if isinstance(slug, str) and slug.strip()
    ]


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
