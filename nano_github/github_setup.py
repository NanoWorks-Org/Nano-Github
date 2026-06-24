from __future__ import annotations

import logging

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


def complete_github_installation_setup(
    db: Database,
    state: str | None,
    installation_id: int | None,
    setup_action: str | None = None,
) -> tuple[bool, str]:
    if not state or installation_id is None:
        return False, SETUP_INVALID_TOKEN_MESSAGE

    setup_token = db.get_valid_github_setup_token(state)
    if setup_token is None:
        return False, SETUP_INVALID_TOKEN_MESSAGE

    try:
        installation = get_installation(installation_id)
        repositories = list_installation_repositories(installation_id)
    except (GitHubAppNotConfigured, GitHubAPIError):
        LOGGER.exception(
            "Failed to fetch GitHub installation %s during setup action %s",
            installation_id,
            setup_action,
        )
        return False, SETUP_VERIFY_FAILED_MESSAGE

    if not db.mark_github_setup_token_used(state):
        return False, SETUP_INVALID_TOKEN_MESSAGE

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

    LOGGER.info(
        "Connected GitHub installation %s to guild %s via setup action %s",
        installation.id,
        setup_token.guild_id,
        setup_action,
    )
    return True, SETUP_SUCCESS_MESSAGE
