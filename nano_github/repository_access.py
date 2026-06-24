from __future__ import annotations

from nano_github.database import (
    Database,
    InstalledRepository,
    InstallationNotBoundToGuild,
    IssueSettings,
    LinkedRepository,
    RepositoryNotLinkedToGuild,
)

NO_REPOSITORY_CONFIGURED_MESSAGE = "No repository configured for this server."
REPOSITORY_NOT_LINKED_MESSAGE = "This repository is not linked to this Discord server."
INSTALLATION_NOT_BOUND_MESSAGE = (
    "This GitHub App installation is not connected to this Discord server."
)


def resolve_issue_repository(
    db: Database,
    guild_id: int,
    settings: IssueSettings | None,
    owner: str | None,
    repo: str | None,
) -> tuple[LinkedRepository | None, str | None]:
    if bool(owner) != bool(repo):
        return None, "Please specify both owner and repo."

    if owner and repo:
        repo_full_name = f"{owner}/{repo}"
        try:
            return db.assert_repo_linked_to_guild(guild_id, repo_full_name), None
        except RepositoryNotLinkedToGuild:
            return None, REPOSITORY_NOT_LINKED_MESSAGE
        except InstallationNotBoundToGuild:
            return None, INSTALLATION_NOT_BOUND_MESSAGE

    if settings and settings.default_owner and settings.default_repo:
        try:
            linked_repo = db.assert_repo_linked_to_guild(
                guild_id,
                f"{settings.default_owner}/{settings.default_repo}",
            )
        except (RepositoryNotLinkedToGuild, InstallationNotBoundToGuild):
            linked_repo = None
        if linked_repo is not None:
            return linked_repo, None

    linked_repos = [
        linked_repo
        for linked_repo in db.list_linked_repositories_for_guild(guild_id)
        if linked_repo.installation_id is not None
        and db.is_installation_bound_to_guild(guild_id, linked_repo.installation_id)
    ]
    if len(linked_repos) == 1:
        return linked_repos[0], None
    if len(linked_repos) > 1:
        return None, "Multiple repositories linked. Please specify owner and repo."
    return None, NO_REPOSITORY_CONFIGURED_MESSAGE


def link_installed_repository_to_guild(
    db: Database,
    guild_id: int,
    installed_repo: InstalledRepository,
) -> tuple[LinkedRepository, bool]:
    existing_linked_repos = [
        repo
        for repo in db.list_linked_repositories_for_guild(guild_id)
        if repo.installation_id is not None
        and db.is_installation_bound_to_guild(guild_id, repo.installation_id)
    ]
    linked_repo = db.link_repository(
        guild_id,
        installed_repo.owner,
        installed_repo.repo,
        installation_id=installed_repo.installation_id,
        repository_full_name=installed_repo.repository_full_name,
    )
    default_set = not existing_linked_repos
    if default_set:
        db.set_issue_default_repository(guild_id, linked_repo.owner, linked_repo.repo)
    return linked_repo, default_set
