from __future__ import annotations

import logging
from dataclasses import dataclass

from nano_github.database import Database
from nano_github.github_client import (
    GitHubAPIError,
    GitHubAppNotConfigured,
    get_installation,
    list_installation_repositories,
)

LOGGER = logging.getLogger(__name__)

SETUP_SUCCESS_MESSAGE = (
    "Nano GitHub is now connected. You can return to Discord and refresh /github dashboard."
)
SETUP_INVALID_TOKEN_MESSAGE = (
    "Invalid or expired setup token. Please return to Discord and press Connect GitHub again."
)
SETUP_VERIFY_FAILED_MESSAGE = (
    "Nano GitHub could not verify this GitHub installation. "
    "Please return to Discord and press Connect GitHub again."
)
SETUP_SAVE_FAILED_MESSAGE = (
    "Nano GitHub could not save this GitHub installation. "
    "Please return to Discord and press Connect GitHub again."
)


@dataclass(frozen=True)
class GitHubSetupResult:
    success: bool
    message: str
    installation_id: int | None = None
    account_login: str | None = None
    error_reason: str | None = None


def complete_github_installation_setup(
    db: Database,
    state: str | None,
    installation_id: int | None,
    setup_action: str | None = None,
) -> tuple[bool, str]:
    result = complete_github_installation_setup_details(
        db,
        state,
        installation_id,
        setup_action,
    )
    return result.success, result.message


def complete_github_installation_setup_details(
    db: Database,
    state: str | None,
    installation_id: int | None,
    setup_action: str | None = None,
) -> GitHubSetupResult:
    if not state or installation_id is None:
        return GitHubSetupResult(
            False,
            SETUP_INVALID_TOKEN_MESSAGE,
            installation_id=installation_id,
            error_reason="The setup token was invalid or expired.",
        )

    setup_token = db.get_valid_github_setup_token(state)
    if setup_token is None:
        return GitHubSetupResult(
            False,
            SETUP_INVALID_TOKEN_MESSAGE,
            installation_id=installation_id,
            error_reason="The setup token was invalid or expired.",
        )

    try:
        installation = get_installation(installation_id)
        repositories = list_installation_repositories(installation_id)
    except (GitHubAppNotConfigured, GitHubAPIError):
        LOGGER.exception(
            "Failed to fetch GitHub installation %s during setup action %s",
            installation_id,
            setup_action,
        )
        return GitHubSetupResult(
            False,
            SETUP_VERIFY_FAILED_MESSAGE,
            installation_id=installation_id,
            error_reason="Nano GitHub could not verify this GitHub installation.",
        )

    if not db.mark_github_setup_token_used(state):
        return GitHubSetupResult(
            False,
            SETUP_INVALID_TOKEN_MESSAGE,
            installation_id=installation_id,
            error_reason="The setup token was invalid or expired.",
        )

    try:
        db.add_guild_installation(
            setup_token.guild_id,
            installation.id,
            installation.account_login,
            installation.account_type,
        )
        for repository in repositories:
            db.upsert_installed_repository(
                installation.id,
                repository.owner,
                repository.repo,
                repository.repository_full_name,
            )
    except Exception:
        LOGGER.exception(
            "Failed to save GitHub installation %s for guild %s",
            installation.id,
            setup_token.guild_id,
        )
        return GitHubSetupResult(
            False,
            SETUP_SAVE_FAILED_MESSAGE,
            installation_id=installation.id,
            account_login=installation.account_login,
            error_reason="Nano GitHub could not save this GitHub installation.",
        )

    LOGGER.info(
        "Connected GitHub installation %s to guild %s via setup action %s",
        installation.id,
        setup_token.guild_id,
        setup_action,
    )
    return GitHubSetupResult(
        True,
        SETUP_SUCCESS_MESSAGE,
        installation_id=installation.id,
        account_login=installation.account_login,
    )
