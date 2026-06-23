from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from nano_github import embeds
from nano_github.database import (
    REVIEW_MODE_ANYONE,
    REVIEW_MODE_DISCORD_ROLE,
    REVIEW_MODE_GITHUB_REVIEWERS,
    Database,
    InstalledRepository,
    IssueSettings,
    LinkedRepository,
    PrMessage,
)
from nano_github.github_client import (
    GitHubAPIError,
    GitHubAppNotConfigured,
    GitHubAppNotInstalled,
    GitHubAppMissingPermission,
    check_repository_permissions,
    create_issue as github_create_issue,
    list_repository_labels,
    submit_pull_request_review,
)

LOGGER = logging.getLogger(__name__)

LOG_EVENT_TYPES = ("commits", "issues", "comments", "releases")
LogEventType = Literal["commits", "issues", "comments", "releases"]
LOG_DASHBOARD_TYPES = (*LOG_EVENT_TYPES, "pr_reviews")
WEBHOOK_PAYLOAD_URL = "https://api.nanoworks.co.uk/webhooks/github"
WEBHOOK_EVENTS = "Pushes, Issues, Issue comments, Pull requests, Releases"
LABEL_FETCH_WARNING = "GitHub labels could not be loaded. You can still create the issue without labels."
DASHBOARD_ACCESS_MESSAGE = (
    "You need Administrator or Manage Server permission to use the Nano GitHub dashboard."
)
ISSUE_CREATION_BLOCKED_MESSAGE = "You are not allowed to create GitHub issues from Discord."
LABEL_CACHE_TTL_SECONDS = 300
_LABEL_CACHE: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
DASHBOARD_SECTIONS = {
    "overview": "Overview",
    "repositories": "Repositories",
    "default_repo": "Default Repository",
    "logs": "Log Channels",
    "pr_reviews": "PR Reviews",
    "issues": "Issue Creation",
    "github_app": "GitHub App",
    "webhook": "Webhook Info",
}


class PullRequestCommentModal(discord.ui.Modal, title="Comment on Pull Request"):
    review_body = discord.ui.TextInput(
        label="Review comment",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=3000,
    )

    def __init__(self, pr_message: PrMessage) -> None:
        super().__init__()
        self.pr_message = pr_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        comment = str(self.review_body.value).strip()
        discord_name = _display_name(interaction.user)
        await _submit_pr_review(
            interaction,
            self.pr_message,
            "COMMENT",
            f"Comment from Discord by {discord_name}: {comment}",
            activity_detail=comment,
        )


class PullRequestChangesModal(discord.ui.Modal, title="Request Pull Request Changes"):
    reason = discord.ui.TextInput(
        label="Short reason",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1000,
    )

    def __init__(self, pr_message: PrMessage) -> None:
        super().__init__()
        self.pr_message = pr_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip()
        discord_name = _display_name(interaction.user)
        await _submit_pr_review(
            interaction,
            self.pr_message,
            "REQUEST_CHANGES",
            f"Requested changes from Discord by {discord_name}: {reason}",
            activity_detail=reason,
        )


class IssueLabelsModal(discord.ui.Modal, title="Configure Issue Labels"):
    allowed_labels = discord.ui.TextInput(
        label="Allowed labels",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="suggestion, bug, enhancement, feature",
    )
    default_labels = discord.ui.TextInput(
        label="Default labels",
        style=discord.TextStyle.short,
        required=False,
        max_length=500,
        placeholder="suggestion",
    )

    def __init__(self, guild_id: int, settings: IssueSettings | None) -> None:
        super().__init__()
        self.guild_id = guild_id
        if settings:
            self.allowed_labels.default = ", ".join(settings.allowed_labels)
            self.default_labels.default = ", ".join(settings.default_labels)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This dashboard control belongs to another server.",
                ephemeral=True,
            )
            return
        db: Database = interaction.client.db  # type: ignore[attr-defined]
        settings = db.get_issue_settings(self.guild_id)
        default_repo = _configured_default_repo(settings)
        if default_repo is None:
            linked_repos = db.list_linked_repositories_for_guild(self.guild_id)
            if len(linked_repos) == 1:
                default_repo = (linked_repos[0].owner, linked_repos[0].repo)
        if default_repo is None:
            await interaction.response.send_message(
                "Choose a default issue repository in the dashboard before configuring labels.",
                ephemeral=True,
            )
            return
        default_owner, default_repo_name = default_repo

        db.set_issue_settings(
            self.guild_id,
            default_owner,
            default_repo_name,
            suggestion_label=settings.suggestion_label if settings else "suggestion",
            bug_label=settings.bug_label if settings else "bug",
            allowed_labels=_parse_comma_labels(str(self.allowed_labels.value)),
            default_labels=_parse_comma_labels(str(self.default_labels.value)),
            submission_log_channel_id=settings.submission_log_channel_id if settings else None,
        )
        await interaction.response.send_message(
            "Issue label settings updated. Use `/github dashboard` to view the refreshed panel.",
            ephemeral=True,
        )


class LinkRepositoryModal(discord.ui.Modal, title="Link Repository"):
    owner = discord.ui.TextInput(
        label="GitHub owner or organization",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=100,
    )
    repo = discord.ui.TextInput(
        label="GitHub repository",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
        linked_repo = db.link_repository(guild_id, str(self.owner.value), str(self.repo.value))
        await interaction.response.send_message(
            (
                f"Linked `{linked_repo.owner}/{linked_repo.repo}`. "
                "Use `/github dashboard` to choose channels and review GitHub App status."
            ),
            ephemeral=True,
        )


class DashboardNavigationSelect(discord.ui.Select):
    def __init__(self, current_section: str) -> None:
        options = [
            discord.SelectOption(
                label=label,
                value=section,
                default=section == current_section,
            )
            for section, label in DASHBOARD_SECTIONS.items()
        ]
        super().__init__(
            placeholder="Choose dashboard section",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await _edit_dashboard(interaction, self.values[0])


class DashboardReviewModeSelect(discord.ui.Select):
    def __init__(self, current_mode: str) -> None:
        options = [
            discord.SelectOption(
                label="Anyone",
                value=REVIEW_MODE_ANYONE,
                default=current_mode == REVIEW_MODE_ANYONE,
            ),
            discord.SelectOption(
                label="GitHub Reviewers Only",
                value=REVIEW_MODE_GITHUB_REVIEWERS,
                default=current_mode == REVIEW_MODE_GITHUB_REVIEWERS,
            ),
            discord.SelectOption(
                label="Discord Role Restricted",
                value=REVIEW_MODE_DISCORD_ROLE,
                default=current_mode == REVIEW_MODE_DISCORD_ROLE,
            ),
        ]
        super().__init__(
            placeholder="Set PR review mode",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        current = db.get_pr_review_settings(guild_id)
        review_mode = self.values[0]
        if review_mode == REVIEW_MODE_DISCORD_ROLE and current.discord_role_id is None:
            await interaction.response.send_message(
                "Choose the role from the dashboard role setup flow when it is available.",
                ephemeral=True,
            )
            return

        db.set_pr_review_settings(guild_id, review_mode, current.discord_role_id)
        await _edit_dashboard(interaction, "pr_reviews")


class DashboardLogTypeSelect(discord.ui.Select):
    def __init__(self, selected_log_type: str) -> None:
        options = [
            discord.SelectOption(
                label=_log_dashboard_type_label(log_type),
                value=log_type,
                default=log_type == selected_log_type,
            )
            for log_type in LOG_DASHBOARD_TYPES
        ]
        super().__init__(
            placeholder="Choose log channel to edit",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        await _edit_dashboard(
            interaction,
            "logs",
            selected_log_type=self.values[0],
        )


class DashboardLogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, selected_log_type: str) -> None:
        self.selected_log_type = selected_log_type
        super().__init__(
            placeholder="Choose the channel Nano GitHub should use for this log type.",
            min_values=1,
            max_values=1,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        channel = self.values[0] if self.values else None
        if not _is_sendable_log_channel(interaction, channel):
            await interaction.response.send_message(
                "Choose the channel Nano GitHub should use for this log type.",
                ephemeral=True,
            )
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        if self.selected_log_type == "pr_reviews":
            db.set_pr_review_channel(guild_id, channel.id)
        else:
            db.set_log_channel(guild_id, self.selected_log_type, channel.id)

        await _edit_dashboard(
            interaction,
            "logs",
            selected_log_type=self.selected_log_type,
        )
        await interaction.followup.send(
            f"{_log_dashboard_type_label(self.selected_log_type)} set to {channel.mention}.",
            ephemeral=True,
        )


class DashboardClearLogChannelButton(discord.ui.Button):
    def __init__(self, selected_log_type: str, disabled: bool) -> None:
        super().__init__(
            label="Clear Selected Log",
            style=discord.ButtonStyle.secondary,
            row=3,
            disabled=disabled,
        )
        self.selected_log_type = selected_log_type

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        if self.selected_log_type == "pr_reviews":
            db.clear_pr_review_channel(guild_id)
        else:
            db.clear_log_channel(guild_id, self.selected_log_type)

        await _edit_dashboard(
            interaction,
            "logs",
            selected_log_type=self.selected_log_type,
        )
        await interaction.followup.send(
            f"{_log_dashboard_type_label(self.selected_log_type)} cleared.",
            ephemeral=True,
        )


class DashboardRepositorySelect(discord.ui.Select):
    def __init__(
        self,
        linked_repos: list[LinkedRepository],
        selected_repo: tuple[str, str] | None,
    ) -> None:
        options = []
        for linked_repo in linked_repos[:25]:
            value = f"{linked_repo.owner}/{linked_repo.repo}"
            options.append(
                discord.SelectOption(
                    label=value,
                    value=value,
                    default=selected_repo == (linked_repo.owner, linked_repo.repo),
                )
            )
        super().__init__(
            placeholder="Choose linked repository",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await _edit_dashboard(interaction, "webhook", _parse_repo_value(self.values[0]))


class DashboardInstalledRepositorySelect(discord.ui.Select):
    def __init__(
        self,
        installed_repos: list[InstalledRepository],
        linked_repos: list[LinkedRepository],
    ) -> None:
        linked = {(repo.owner, repo.repo) for repo in linked_repos}
        self.installed_repos_by_value: dict[str, InstalledRepository] = {}
        options = []
        for index, installed_repo in enumerate(installed_repos[:25]):
            value = f"installed:{index}"
            self.installed_repos_by_value[value] = installed_repo
            options.append(
                discord.SelectOption(
                    label=installed_repo.repository_full_name[:100],
                    value=value,
                    description="Linked" if (installed_repo.owner, installed_repo.repo) in linked else "Available",
                )
            )
        super().__init__(
            placeholder="Link an installed GitHub App repository",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        installed_repo = self.installed_repos_by_value.get(self.values[0])
        if installed_repo is None:
            await interaction.response.send_message("Could not identify that repository.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
        linked_repo = db.link_repository(
            guild_id,
            installed_repo.owner,
            installed_repo.repo,
            installation_id=installed_repo.installation_id,
            repository_full_name=installed_repo.repository_full_name,
        )
        await interaction.response.send_message(
            (
                f"Linked `{linked_repo.owner}/{linked_repo.repo}` to this server. "
                "Use `/github dashboard` to choose channels and defaults."
            ),
            ephemeral=True,
        )


class DashboardDefaultRepositorySelect(discord.ui.Select):
    def __init__(
        self,
        linked_repos: list[LinkedRepository],
        selected_repo: tuple[str, str] | None,
    ) -> None:
        options = [
            discord.SelectOption(
                label=f"{linked_repo.owner}/{linked_repo.repo}",
                value=f"{linked_repo.owner}/{linked_repo.repo}",
                default=selected_repo == (linked_repo.owner, linked_repo.repo),
            )
            for linked_repo in linked_repos[:25]
        ]
        super().__init__(
            placeholder="Set default repository",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        selected_repo = _parse_repo_value(self.values[0])
        if guild_id is None or selected_repo is None:
            await interaction.response.send_message("Could not identify that repository.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        linked_repo = db.get_linked_repository(guild_id, *selected_repo)
        if linked_repo is None:
            await interaction.response.send_message(
                "That repository is not linked to this server.",
                ephemeral=True,
            )
            return
        db.set_issue_default_repository(guild_id, linked_repo.owner, linked_repo.repo)
        await _edit_dashboard(interaction, "default_repo", selected_repo)


class IssueRepositorySelect(discord.ui.Select):
    def __init__(self, parent: "IssueCreateView", linked_repos: list[LinkedRepository]) -> None:
        self.issue_view = parent
        options = [
            discord.SelectOption(
                label=f"{linked_repo.owner}/{linked_repo.repo}",
                value=f"{linked_repo.owner}/{linked_repo.repo}",
            )
            for linked_repo in linked_repos[:25]
        ]
        super().__init__(
            placeholder="Choose repository",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.issue_view.can_use(interaction):
            await interaction.response.send_message(
                "Only the user who started this issue can use this menu.",
                ephemeral=True,
            )
            return
        if await _reject_if_issue_creation_blocked(interaction):
            return
        selected_repo = _parse_repo_value(self.values[0])
        if selected_repo is None:
            await interaction.response.send_message("Could not identify that repository.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        linked_repo = db.get_linked_repository(self.issue_view.guild_id, *selected_repo)
        if linked_repo is None:
            await interaction.response.send_message(
                "That repository is not linked to this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        label_result = await _fetch_repository_label_result(linked_repo.owner, linked_repo.repo)
        labels = _available_issue_labels(
            self.issue_view.settings,
            label_result[0],
        )
        view = IssueCreateView(
            self.issue_view.guild_id,
            self.issue_view.channel_id,
            self.issue_view.user_id,
            self.issue_view.title,
            self.issue_view.description,
            self.issue_view.settings,
            self.issue_view.linked_repos,
            linked_repo,
            labels,
            label_warning=label_result[1],
        )
        await interaction.edit_original_response(
            embed=_issue_create_prompt_embed(
                linked_repo,
                view.selected_labels,
                labels,
                label_warning=view.label_warning,
            ),
            view=view,
        )


class IssueLabelSelect(discord.ui.Select):
    def __init__(self, parent: "IssueCreateView", labels: tuple[str, ...]) -> None:
        self.issue_view = parent
        defaults = {label.lower() for label in parent.selected_labels}
        options = [
            discord.SelectOption(
                label=label[:100],
                value=label,
                default=label.lower() in defaults,
            )
            for label in labels[:25]
        ]
        super().__init__(
            placeholder="Choose labels",
            min_values=0,
            max_values=min(25, len(options)),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.issue_view.can_use(interaction):
            await interaction.response.send_message(
                "Only the user who started this issue can use this menu.",
                ephemeral=True,
            )
            return
        if await _reject_if_issue_creation_blocked(interaction):
            return
        self.issue_view.selected_labels = list(self.values)
        await interaction.response.defer()


class IssueCreateButton(discord.ui.Button):
    def __init__(self, parent: "IssueCreateView") -> None:
        super().__init__(label="Create Issue", style=discord.ButtonStyle.success, row=1)
        self.issue_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.issue_view.can_use(interaction):
            await interaction.response.send_message(
                "Only the user who started this issue can use this button.",
                ephemeral=True,
            )
            return
        if await _reject_if_issue_creation_blocked(interaction):
            return
        if self.issue_view.linked_repo is None:
            await interaction.response.send_message("Choose a repository first.", ephemeral=True)
            return

        labels = (
            []
            if self.issue_view.label_warning
            else self.issue_view.selected_labels or list(self.issue_view.settings.default_labels)
        )
        await _create_issue_from_selection(
            interaction,
            self.issue_view.channel_id,
            self.issue_view.linked_repo,
            self.issue_view.title,
            self.issue_view.description,
            labels,
        )


class IssueCreateView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        title: str,
        description: str,
        settings: IssueSettings,
        linked_repos: list[LinkedRepository],
        linked_repo: LinkedRepository | None,
        labels: tuple[str, ...],
        label_warning: bool = False,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.title = title
        self.description = description
        self.settings = settings
        self.linked_repos = linked_repos
        self.linked_repo = linked_repo
        self.labels = labels
        self.label_warning = label_warning
        self.selected_labels = [] if label_warning else _labels_available_in_repo(settings.default_labels, labels)

        if linked_repo is None:
            self.add_item(IssueRepositorySelect(self, linked_repos))
        else:
            if labels:
                self.add_item(IssueLabelSelect(self, labels))
            self.add_item(IssueCreateButton(self))

    def can_use(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id and interaction.guild_id == self.guild_id


class DashboardIssueToggleButton(discord.ui.Button):
    def __init__(self, enabled: bool) -> None:
        super().__init__(
            label="Disable Issues" if enabled else "Enable Issues",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            row=1,
        )
        self.enabled = enabled

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        db.set_issue_creation_enabled(guild_id, not self.enabled)
        await _edit_dashboard(interaction, "issues")


class DashboardLinkRepositoryButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Advanced Manual Link",
            style=discord.ButtonStyle.primary,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        await interaction.response.send_modal(LinkRepositoryModal())


class DashboardConfigureLabelsButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Configure Labels",
            style=discord.ButtonStyle.primary,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        await interaction.response.send_modal(
            IssueLabelsModal(guild_id, db.get_issue_settings(guild_id))
        )


class DashboardIssueSubmissionLogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose the channel Nano GitHub should use for this log type.",
            min_values=1,
            max_values=1,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        channel = self.values[0] if self.values else None
        if not _is_sendable_log_channel(interaction, channel):
            await interaction.response.send_message(
                "Choose the channel Nano GitHub should use for this log type.",
                ephemeral=True,
            )
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        db.set_issue_submission_log_channel(guild_id, channel.id)
        await _edit_dashboard(interaction, "issues")
        await interaction.followup.send(
            f"Issue submission logs will be sent to {channel.mention}.",
            ephemeral=True,
        )


class DashboardClearIssueSubmissionLogButton(discord.ui.Button):
    def __init__(self, disabled: bool) -> None:
        super().__init__(
            label="Clear Submission Log",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        db.set_issue_submission_log_channel(guild_id, None)
        await _edit_dashboard(interaction, "issues")
        await interaction.followup.send(
            "Issue submission log channel cleared.",
            ephemeral=True,
        )


class DashboardAddIssueBlockedRoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Add blocked role",
            min_values=1,
            max_values=10,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        selected_roles = [role for role in self.values if isinstance(role, discord.Role)]
        for role in selected_roles:
            db.add_issue_blocked_role(guild_id, role.id)

        await _edit_dashboard(interaction, "issues")
        await interaction.followup.send(
            "Blocked role settings updated.",
            ephemeral=True,
        )


class DashboardRemoveIssueBlockedRoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Remove blocked role",
            min_values=1,
            max_values=10,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        selected_roles = [role for role in self.values if isinstance(role, discord.Role)]
        for role in selected_roles:
            db.remove_issue_blocked_role(guild_id, role.id)

        await _edit_dashboard(interaction, "issues")
        await interaction.followup.send(
            "Blocked role settings updated.",
            ephemeral=True,
        )


class DashboardClearIssueBlockedRolesButton(discord.ui.Button):
    def __init__(self, disabled: bool) -> None:
        super().__init__(
            label="Clear Blocked Roles",
            style=discord.ButtonStyle.danger,
            row=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        db.clear_issue_blocked_roles(guild_id)
        await _edit_dashboard(interaction, "issues")
        await interaction.followup.send(
            "Blocked role settings cleared.",
            ephemeral=True,
        )


class DashboardRevealSecretButton(discord.ui.Button):
    def __init__(self, selected_repo: tuple[str, str] | None) -> None:
        super().__init__(
            label="Reveal Secret",
            style=discord.ButtonStyle.danger,
            row=2,
            disabled=selected_repo is None,
        )
        self.selected_repo = selected_repo

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_dashboard_access(interaction):
            return
        guild_id = _require_guild(interaction)
        if guild_id is None:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
            return
        if self.selected_repo is None:
            await interaction.response.send_message(
                "Select a linked repository before revealing its webhook secret.",
                ephemeral=True,
            )
            return

        db: Database = interaction.client.db  # type: ignore[attr-defined]
        owner, repo = self.selected_repo
        linked_repo = db.get_linked_repository(guild_id, owner, repo)
        if linked_repo is None:
            await interaction.response.send_message(
                "That repository is not linked to this server.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"Warning: this is the webhook secret for `{linked_repo.owner}/{linked_repo.repo}`. "
                "Only paste it into GitHub webhook settings.\n\n"
                f"`{linked_repo.webhook_secret}`"
            ),
            ephemeral=True,
        )


class GitHubDashboardView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        section: str,
        *,
        linked_repos: list[LinkedRepository] | None = None,
        installed_repos: list[InstalledRepository] | None = None,
        selected_repo: tuple[str, str] | None = None,
        selected_log_type: str = "commits",
        pr_review_mode: str = REVIEW_MODE_ANYONE,
        issue_enabled: bool = False,
        issue_submission_log_channel_id: int | None = None,
        issue_blocked_role_ids: tuple[int, ...] = (),
        log_channels: dict[str, int] | None = None,
        pr_review_channel_id: int | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.section = section
        self.selected_repo = selected_repo
        self.selected_log_type = selected_log_type
        linked_repos = linked_repos or []
        installed_repos = installed_repos or []
        log_channels = log_channels or {}
        self.add_item(DashboardNavigationSelect(section))

        if section == "pr_reviews":
            self.add_item(DashboardReviewModeSelect(pr_review_mode))
        if section == "repositories":
            if installed_repos:
                self.add_item(DashboardInstalledRepositorySelect(installed_repos, linked_repos))
            self.add_item(DashboardLinkRepositoryButton())
        if section == "default_repo" and linked_repos:
            self.add_item(DashboardDefaultRepositorySelect(linked_repos, selected_repo))
        if section == "default_repo":
            self.add_item(DashboardLinkRepositoryButton())
        if section == "logs":
            configured_channel_id = (
                pr_review_channel_id
                if selected_log_type == "pr_reviews"
                else log_channels.get(selected_log_type)
            )
            self.add_item(DashboardLogTypeSelect(selected_log_type))
            self.add_item(DashboardLogChannelSelect(selected_log_type))
            self.add_item(
                DashboardClearLogChannelButton(
                    selected_log_type,
                    disabled=configured_channel_id is None,
                )
            )
        if section == "issues":
            self.add_item(DashboardIssueToggleButton(issue_enabled))
            self.add_item(DashboardConfigureLabelsButton())
            self.add_item(
                DashboardClearIssueSubmissionLogButton(
                    disabled=issue_submission_log_channel_id is None,
                )
            )
            self.add_item(DashboardIssueSubmissionLogChannelSelect())
            self.add_item(DashboardAddIssueBlockedRoleSelect())
            self.add_item(DashboardRemoveIssueBlockedRoleSelect())
            self.add_item(
                DashboardClearIssueBlockedRolesButton(disabled=not issue_blocked_role_ids)
            )
        if section == "webhook":
            if linked_repos:
                self.add_item(DashboardRepositorySelect(linked_repos, selected_repo))
            self.add_item(DashboardRevealSecretButton(selected_repo))


class PullRequestReviewView(discord.ui.View):
    """Interactive PR review card audit.

    Current state after this implementation:
    - View Pull Request is a real Discord link button and requires no GitHub API permissions.
    - Approve submits a GitHub Pull Request Review with event APPROVE.
    - Request Changes submits a GitHub Pull Request Review with event REQUEST_CHANGES.
    - Comment opens a Discord modal and submits a GitHub Pull Request Review with event COMMENT.

    GitHub requirements:
    - Repository lookup uses app JWT auth against GET /repos/{owner}/{repo}/installation.
    - Review actions create a repository installation token and require pull_requests:write.
    - Issue creation creates a repository installation token and requires issues:write.

    Discord requirements:
    - Configuration, repository linking, webhook, and channel commands are admin-only.
    - PR review actions are guild-scoped and configurable: anyone, requested GitHub reviewers
      by Discord display/username match, or a specific Discord role.
    """

    def __init__(self, github_url: str | None = None) -> None:
        super().__init__(timeout=None)
        if github_url:
            self.add_item(
                discord.ui.Button(
                    label="View Pull Request",
                    style=discord.ButtonStyle.link,
                    url=github_url,
                    row=0,
                )
            )

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="nano_github:pr_review:approve",
        row=1,
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        body = f"Approved from Discord by {_display_name(interaction.user)}."
        await _handle_pr_review_button(interaction, "APPROVE", body)

    @discord.ui.button(
        label="Request Changes",
        style=discord.ButtonStyle.danger,
        custom_id="nano_github:pr_review:request_changes",
        row=1,
    )
    async def request_changes(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        pr_message = await _resolve_pr_message_for_interaction(interaction)
        if pr_message is None:
            return
        if not await _can_use_pr_review_action(interaction, pr_message):
            return
        await interaction.response.send_modal(PullRequestChangesModal(pr_message))

    @discord.ui.button(
        label="Comment",
        style=discord.ButtonStyle.secondary,
        custom_id="nano_github:pr_review:comment",
        row=1,
    )
    async def comment(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        pr_message = await _resolve_pr_message_for_interaction(interaction)
        if pr_message is None:
            return
        if not await _can_use_pr_review_action(interaction, pr_message):
            return
        await interaction.response.send_modal(PullRequestCommentModal(pr_message))


class NanoGitHubBot(commands.Bot):
    def __init__(self, db: Database) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = db
        self.tree.add_command(github_group)
        self.tree.add_command(issue_group)

    async def setup_hook(self) -> None:
        self.add_view(PullRequestReviewView())

    async def on_ready(self) -> None:
        if not self.user:
            return
        LOGGER.info("Logged in as %s (%s)", self.user, self.user.id)
        try:
            synced = await self.tree.sync()
            LOGGER.info("Synced %s global slash commands", len(synced))
        except discord.HTTPException:
            LOGGER.exception("Failed to sync slash commands")


github_group = app_commands.Group(
    name="github",
    description="Configure Nano GitHub notifications and pull request review cards.",
)

issue_group = app_commands.Group(
    name="issue",
    description="Create and configure GitHub issues from Discord.",
)


def _require_guild(interaction: discord.Interaction) -> int | None:
    if interaction.guild_id is None:
        return None
    return int(interaction.guild_id)


async def _ensure_manage_guild(interaction: discord.Interaction) -> bool:
    permissions = interaction.permissions
    if not permissions.administrator:
        await interaction.response.send_message(
            "You need the Administrator permission to configure Nano GitHub.",
            ephemeral=True,
        )
        return False
    return True


async def _ensure_dashboard_access(interaction: discord.Interaction) -> bool:
    permissions = interaction.permissions
    if permissions.administrator or permissions.manage_guild:
        return True

    if interaction.response.is_done():
        await interaction.followup.send(DASHBOARD_ACCESS_MESSAGE, ephemeral=True)
    else:
        await interaction.response.send_message(DASHBOARD_ACCESS_MESSAGE, ephemeral=True)
    return False


async def setup(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    await interaction.response.send_message(
        "Nano GitHub is ready for this server. Link repositories from `/github dashboard`.",
        ephemeral=True,
    )


@app_commands.describe(owner="GitHub repository owner or organization", repo="GitHub repository name")
async def link_repo(interaction: discord.Interaction, owner: str, repo: str) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    linked_repo = db.link_repository(guild_id, owner, repo)
    await interaction.response.send_message(
        (
            f"Linked `{linked_repo.owner}/{linked_repo.repo}` to this server. "
            "Use `/github dashboard` to choose channels and review GitHub App status."
        ),
        ephemeral=True,
    )


@app_commands.describe(owner="GitHub repository owner or organization", repo="GitHub repository name")
async def webhook_info(interaction: discord.Interaction, owner: str, repo: str) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    linked_repo = db.get_linked_repository(guild_id, owner, repo)
    if linked_repo is None:
        await interaction.response.send_message(
            f"`{owner.strip().lower()}/{repo.strip().lower()}` is not linked to this server.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        (
            f"Legacy manual webhook info for `{linked_repo.owner}/{linked_repo.repo}`\n"
            f"Payload URL: `{WEBHOOK_PAYLOAD_URL}`\n"
            "Content Type: `application/json`\n"
            f"Secret: `{linked_repo.webhook_secret}`\n"
            f"Events to select: {WEBHOOK_EVENTS}"
        ),
        ephemeral=True,
    )


@app_commands.describe(owner="GitHub repository owner or organization", repo="GitHub repository name")
async def rotate_secret(interaction: discord.Interaction, owner: str, repo: str) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    linked_repo = db.rotate_webhook_secret(guild_id, owner, repo)
    if linked_repo is None:
        await interaction.response.send_message(
            f"`{owner.strip().lower()}/{repo.strip().lower()}` is not linked to this server.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        (
            f"Rotated legacy manual webhook secret for `{linked_repo.owner}/{linked_repo.repo}`.\n"
            f"New secret: `{linked_repo.webhook_secret}`\n"
            "Update the manual GitHub repository webhook secret before new legacy deliveries will be accepted."
        ),
        ephemeral=True,
    )


@app_commands.describe(event_type="Logging event type", channel="Discord channel for this event")
@app_commands.choices(
    event_type=[
        app_commands.Choice(name="commits", value="commits"),
        app_commands.Choice(name="issues", value="issues"),
        app_commands.Choice(name="comments", value="comments"),
        app_commands.Choice(name="releases", value="releases"),
    ]
)
async def set_log_channel(
    interaction: discord.Interaction,
    event_type: app_commands.Choice[str],
    channel: discord.TextChannel,
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    db.set_log_channel(guild_id, event_type.value, channel.id)
    await interaction.response.send_message(
        f"`{event_type.value}` events will be logged in {channel.mention}.",
        ephemeral=True,
    )


@app_commands.describe(channel="Discord channel for pull request review cards")
async def set_pr_review_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    db.set_pr_review_channel(guild_id, channel.id)
    await interaction.response.send_message(
        f"Pull request review cards will be posted in {channel.mention}.",
        ephemeral=True,
    )


@app_commands.describe(
    review_mode="Who can submit PR reviews from Discord",
    role="Required when using Discord Role Restricted mode",
)
@app_commands.choices(
    review_mode=[
        app_commands.Choice(name="Anyone", value=REVIEW_MODE_ANYONE),
        app_commands.Choice(name="GitHub Reviewers Only", value=REVIEW_MODE_GITHUB_REVIEWERS),
        app_commands.Choice(name="Discord Role Restricted", value=REVIEW_MODE_DISCORD_ROLE),
    ]
)
async def set_review_mode(
    interaction: discord.Interaction,
    review_mode: app_commands.Choice[str],
    role: discord.Role | None = None,
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return
    if review_mode.value == REVIEW_MODE_DISCORD_ROLE and role is None:
        await interaction.response.send_message(
            "Choose a Discord role for Discord Role Restricted mode.",
            ephemeral=True,
        )
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    settings = db.set_pr_review_settings(
        guild_id,
        review_mode.value,
        role.id if role else None,
    )
    mode_label = _review_mode_label(settings.review_mode, settings.discord_role_id)
    await interaction.response.send_message(
        f"PR review mode set to {mode_label}.",
        ephemeral=True,
    )


@app_commands.describe(owner="GitHub repository owner or organization", repo="GitHub repository name")
async def app_status(interaction: discord.Interaction, owner: str, repo: str) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    linked_repo = db.get_linked_repository(guild_id, owner, repo)
    if linked_repo is None:
        await interaction.response.send_message(
            f"`{owner.strip().lower()}/{repo.strip().lower()}` is not linked to this server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        check = await asyncio.to_thread(
            check_repository_permissions,
            linked_repo.owner,
            linked_repo.repo,
        )
    except GitHubAppNotConfigured:
        await interaction.followup.send(
            "GitHub App authentication is not configured.",
            ephemeral=True,
        )
        return
    except GitHubAPIError:
        LOGGER.exception(
            "Failed to check GitHub App status for %s/%s",
            linked_repo.owner,
            linked_repo.repo,
        )
        await interaction.followup.send(
            "GitHub API request failed while checking repository permissions.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title="GitHub App Status", color=embeds.NANO_BLUE)
    embed.add_field(name="Repository", value=f"`{check.owner}/{check.repo}`", inline=False)
    embed.add_field(name="Installed", value="Yes" if check.installed else "No", inline=True)
    embed.add_field(
        name="Issues",
        value=_permission_status(check.issues, check.can_create_issues),
        inline=True,
    )
    embed.add_field(
        name="Pull request reviews",
        value=_permission_status(check.pull_requests, check.can_review_pull_requests),
        inline=True,
    )
    if not check.installed:
        embed.description = "GitHub App is not installed for this repository."
    elif not check.can_review_pull_requests:
        embed.description = (
            "Nano GitHub does not currently have Pull Request Review permissions "
            "for this repository."
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def status(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    config = db.get_status(guild_id)
    repos = config["repositories"]
    log_channels = config["log_channels"]
    pr_channel = config["pr_review_channel"]
    pr_review_mode = config["pr_review_mode"]
    pr_review_role_id = config["pr_review_role_id"]

    embed = discord.Embed(title="Nano GitHub Status", color=0x2F80ED)
    embed.add_field(
        name="Linked repositories",
        value="\n".join(f"`{owner}/{repo}`" for owner, repo in repos) if repos else "None",
        inline=False,
    )
    embed.add_field(
        name="Log channels",
        value="\n".join(
            f"`{event}`: <#{channel_id}>" for event, channel_id in sorted(log_channels.items())
        )
        if log_channels
        else "None",
        inline=False,
    )
    embed.add_field(
        name="PR review channel",
        value=f"<#{pr_channel}>" if pr_channel else "Not configured",
        inline=False,
    )
    embed.add_field(
        name="PR review mode",
        value=_review_mode_label(pr_review_mode, pr_review_role_id),
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@github_group.command(name="dashboard", description="Open the Nano GitHub configuration dashboard.")
async def dashboard(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_dashboard_access(interaction):
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    embed, view = await _build_dashboard(interaction, "overview")
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def unlink_repo(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    removed = db.unlink_repositories(guild_id)
    await interaction.response.send_message(
        f"Unlinked {removed} repositor{'y' if removed == 1 else 'ies'} from this server.",
        ephemeral=True,
    )


async def issue_labels_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        return []

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    settings = _effective_issue_settings(db, guild_id)
    linked_repo, _ = _resolve_issue_repository(db, guild_id, settings, None, None)
    if linked_repo is None:
        return []

    if settings.allowed_labels:
        labels = settings.allowed_labels
    else:
        labels = await _fetch_repository_labels(linked_repo.owner, linked_repo.repo)
    query = current.rsplit(",", 1)[-1].strip().lower()
    prefix = current[: len(current) - len(current.rsplit(",", 1)[-1])]
    matches = [label for label in labels if not query or query in label.lower()]
    return [
        app_commands.Choice(name=label[:100], value=f"{prefix}{label}"[:100])
        for label in matches[:25]
    ]


@issue_group.command(name="create", description="Create a GitHub issue from Discord.")
@app_commands.describe(
    title="GitHub issue title",
    description="GitHub issue description",
    labels="Optional GitHub label; use autocomplete or choose labels after running the command",
)
@app_commands.autocomplete(labels=issue_labels_autocomplete)
async def create_issue(
    interaction: discord.Interaction,
    title: str,
    description: str,
    labels: str | None = None,
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if interaction.channel_id is None:
        await interaction.response.send_message(
            "Run this command in a server channel.",
            ephemeral=True,
        )
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    if await _reject_if_issue_creation_blocked(interaction):
        return

    settings = _effective_issue_settings(db, guild_id)
    if settings and not settings.enabled:
        await interaction.response.send_message(
            "Issue creation is disabled for this server.",
            ephemeral=True,
        )
        return

    linked_repo, error_message = _resolve_issue_repository(db, guild_id, settings, None, None)
    if error_message:
        linked_repos = db.list_linked_repositories_for_guild(guild_id)
        if linked_repos and "Multiple repositories linked" in error_message:
            view = IssueCreateView(
                guild_id,
                int(interaction.channel_id),
                interaction.user.id,
                title,
                description,
                settings,
                linked_repos,
                None,
                (),
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Choose Repository",
                    description="This server has multiple linked repositories and no default.",
                    color=embeds.NANO_BLUE,
                ),
                view=view,
                ephemeral=True,
            )
            return
        await interaction.response.send_message(error_message, ephemeral=True)
        return
    if linked_repo is None:
        await interaction.response.send_message(
            "No default repository configured.",
            ephemeral=True,
        )
        return

    requested_labels, blocked_labels = _labels_for_issue_create(settings, labels)
    if blocked_labels:
        await interaction.response.send_message(
            _invalid_label_message(blocked_labels[0], settings.allowed_labels),
            ephemeral=True,
        )
        return
    if labels:
        await _create_issue_from_selection(
            interaction,
            int(interaction.channel_id),
            linked_repo,
            title,
            description,
            requested_labels,
            blocked_labels,
        )
        return

    label_result = await _fetch_repository_label_result(linked_repo.owner, linked_repo.repo)
    repo_labels = _available_issue_labels(settings, label_result[0])
    view = IssueCreateView(
        guild_id,
        int(interaction.channel_id),
        interaction.user.id,
        title,
        description,
        settings,
        [linked_repo],
        linked_repo,
        repo_labels,
        label_warning=label_result[1],
    )
    await interaction.response.send_message(
        embed=_issue_create_prompt_embed(
            linked_repo,
            view.selected_labels,
            repo_labels,
            label_warning=view.label_warning,
        ),
        view=view,
        ephemeral=True,
    )


@app_commands.describe(
    default_repo_owner="Default linked GitHub repository owner or organization",
    default_repo_name="Default linked GitHub repository name",
    suggestion_label="Label used for suggestions",
    bug_label="Label used for bug reports",
    allowed_labels="Optional comma-separated labels users may choose",
    default_labels="Optional comma-separated labels used when /issue create labels is blank",
    submission_log_channel="Optional Discord channel for issue submission logs",
)
async def configure_issue_creation(
    interaction: discord.Interaction,
    default_repo_owner: str,
    default_repo_name: str,
    suggestion_label: str = "suggestion",
    bug_label: str = "bug",
    allowed_labels: str | None = None,
    default_labels: str | None = None,
    submission_log_channel: discord.TextChannel | None = None,
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    linked_repo = db.get_linked_repository(guild_id, default_repo_owner, default_repo_name)
    if linked_repo is None:
        await interaction.response.send_message(
            (
                f"`{default_repo_owner.strip().lower()}/"
                f"{default_repo_name.strip().lower()}` is not linked to this server."
            ),
            ephemeral=True,
        )
        return

    settings = db.set_issue_settings(
        guild_id,
        linked_repo.owner,
        linked_repo.repo,
        suggestion_label=suggestion_label,
        bug_label=bug_label,
        allowed_labels=_parse_comma_labels(allowed_labels),
        default_labels=_parse_comma_labels(default_labels),
        submission_log_channel_id=submission_log_channel.id if submission_log_channel else None,
    )

    embed = _issue_status_embed(settings)
    embed.title = "Issue Creation Configured"
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def issue_status(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    settings = db.get_issue_settings(guild_id)
    embed = _issue_status_embed(settings)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def disable_issue_creation(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    if not await _ensure_manage_guild(interaction):
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    db.upsert_guild(guild_id, interaction.guild.name if interaction.guild else None)
    db.disable_issue_creation(guild_id)
    await interaction.response.send_message(
        "Issue creation is disabled for this server.",
        ephemeral=True,
    )


async def _handle_pr_review_button(
    interaction: discord.Interaction,
    event: str,
    body: str | None = None,
) -> None:
    pr_message = await _resolve_pr_message_for_interaction(interaction)
    if pr_message is None:
        return
    if not await _can_use_pr_review_action(interaction, pr_message):
        return
    await _submit_pr_review(interaction, pr_message, event, body)


async def _resolve_pr_message_for_interaction(
    interaction: discord.Interaction,
) -> PrMessage | None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this action in a server.", ephemeral=True)
        return None

    message_id = getattr(interaction.message, "id", None)
    if not isinstance(message_id, int):
        await interaction.response.send_message(
            "This PR review card could not be identified.",
            ephemeral=True,
        )
        return None

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    pr_message = db.get_pr_message_by_discord_message(guild_id, message_id)
    if pr_message is None:
        await interaction.response.send_message(
            "This PR review card is no longer tracked. Wait for the next PR update.",
            ephemeral=True,
        )
        return None
    return pr_message


async def _can_use_pr_review_action(
    interaction: discord.Interaction,
    pr_message: PrMessage,
) -> bool:
    db: Database = interaction.client.db  # type: ignore[attr-defined]
    settings = db.get_pr_review_settings(pr_message.guild_id)

    if settings.review_mode == REVIEW_MODE_ANYONE:
        return True

    if settings.review_mode == REVIEW_MODE_DISCORD_ROLE:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        role_ids = {role.id for role in member.roles} if member else set()
        if settings.discord_role_id and settings.discord_role_id in role_ids:
            return True
        await interaction.response.send_message(
            "Only members with the configured PR review role can use this button.",
            ephemeral=True,
        )
        return False

    if settings.review_mode == REVIEW_MODE_GITHUB_REVIEWERS:
        discord_names = _discord_identity_names(interaction.user)
        reviewer_names = {reviewer.lower() for reviewer in pr_message.requested_reviewers}
        if discord_names & reviewer_names:
            return True

        if pr_message.requested_teams and not reviewer_names:
            await interaction.response.send_message(
                (
                    "This PR is assigned to a GitHub team. Use Discord Role Restricted "
                    "review mode for team-based reviews."
                ),
                ephemeral=True,
            )
            return False

        await interaction.response.send_message(
            (
                "Only requested GitHub reviewers can use this button. Nano GitHub matches "
                "the reviewer login against your Discord username or server display name."
            ),
            ephemeral=True,
        )
        return False

    await interaction.response.send_message(
        "PR review mode is not configured correctly for this server.",
        ephemeral=True,
    )
    return False


async def _submit_pr_review(
    interaction: discord.Interaction,
    pr_message: PrMessage,
    event: str,
    body: str | None = None,
    activity_detail: str | None = None,
) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        review = await asyncio.to_thread(
            submit_pull_request_review,
            pr_message.owner,
            pr_message.repo,
            pr_message.pr_number,
            event,
            body,
        )
    except GitHubAppNotInstalled:
        await interaction.followup.send(
            "GitHub App is not installed for this repository.",
            ephemeral=True,
        )
        return
    except GitHubAppNotConfigured:
        await interaction.followup.send(
            "GitHub App authentication is not configured.",
            ephemeral=True,
        )
        return
    except GitHubAppMissingPermission as exc:
        await interaction.followup.send(exc.user_message, ephemeral=True)
        return
    except GitHubAPIError as exc:
        LOGGER.warning(
            "GitHub PR review failed for %s/%s#%s with status %s: %s",
            pr_message.owner,
            pr_message.repo,
            pr_message.pr_number,
            exc.status_code,
            exc.message,
        )
        await interaction.followup.send(_friendly_pr_review_error(exc), ephemeral=True)
        return

    await _update_pr_review_activity(
        interaction,
        pr_message,
        event,
        activity_detail,
    )
    await interaction.followup.send(
        _pr_review_success_message(event, review.url),
        ephemeral=True,
    )


def _resolve_issue_repository(
    db: Database,
    guild_id: int,
    settings: IssueSettings | None,
    owner: str | None,
    repo: str | None,
) -> tuple[LinkedRepository | None, str | None]:
    if bool(owner) != bool(repo):
        return None, "Please specify both owner and repo."

    if owner and repo:
        linked_repo = db.get_linked_repository(guild_id, owner, repo)
        if linked_repo is None:
            normalized_owner = owner.strip().lower()
            normalized_repo = repo.strip().lower()
            return None, f"`{normalized_owner}/{normalized_repo}` is not linked to this server."
        return linked_repo, None

    if settings and settings.default_owner and settings.default_repo:
        linked_repo = db.get_linked_repository(
            guild_id,
            settings.default_owner,
            settings.default_repo,
        )
        if linked_repo is not None:
            return linked_repo, None

    linked_repos = db.list_linked_repositories_for_guild(guild_id)
    if len(linked_repos) == 1:
        return linked_repos[0], None
    if len(linked_repos) > 1:
        return None, "Multiple repositories linked. Please specify owner and repo."
    return None, "No default repository configured."


def _effective_issue_settings(db: Database, guild_id: int) -> IssueSettings:
    settings = db.get_issue_settings(guild_id)
    if settings is not None:
        return settings
    return IssueSettings(
        guild_id=guild_id,
        default_owner=None,
        default_repo=None,
        suggestion_label="suggestion",
        bug_label="bug",
        allowed_labels=(),
        default_labels=(),
        submission_log_channel_id=None,
        enabled=True,
    )


async def _fetch_repository_labels(owner: str, repo: str) -> tuple[str, ...]:
    labels, _ = await _fetch_repository_label_result(owner, repo)
    return labels


async def _fetch_repository_label_result(owner: str, repo: str) -> tuple[tuple[str, ...], bool]:
    cached_labels = _cached_repository_labels(owner, repo)
    if cached_labels is not None:
        return cached_labels, False

    try:
        labels = await asyncio.to_thread(list_repository_labels, owner, repo)
        _cache_repository_labels(owner, repo, labels)
        return labels, False
    except GitHubAppNotInstalled:
        LOGGER.warning("GitHub App is not installed while fetching labels for %s/%s", owner, repo)
        return (), True
    except GitHubAppNotConfigured:
        LOGGER.warning("GitHub App is not configured while fetching labels for %s/%s", owner, repo)
        return (), True
    except GitHubAPIError as exc:
        LOGGER.warning(
            "Failed to fetch GitHub labels for %s/%s: status=%s message=%s",
            owner,
            repo,
            exc.status_code,
            exc.message,
        )
        return (), True
    except Exception:
        LOGGER.exception("Unexpected error while fetching GitHub labels for %s/%s", owner, repo)
        return (), True


def _cached_repository_labels(owner: str, repo: str) -> tuple[str, ...] | None:
    key = (owner.strip().lower(), repo.strip().lower())
    cached = _LABEL_CACHE.get(key)
    if cached is None:
        return None
    cached_at, labels = cached
    if time.monotonic() - cached_at > LABEL_CACHE_TTL_SECONDS:
        _LABEL_CACHE.pop(key, None)
        return None
    return labels


def _cache_repository_labels(owner: str, repo: str, labels: tuple[str, ...]) -> None:
    key = (owner.strip().lower(), repo.strip().lower())
    _LABEL_CACHE[key] = (time.monotonic(), labels)


def _issue_create_prompt_embed(
    linked_repo: LinkedRepository,
    selected_labels: list[str],
    available_labels: tuple[str, ...],
    label_warning: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title="Create GitHub Issue",
        description="Choose labels from GitHub, then create the issue.",
        color=embeds.NANO_BLUE,
    )
    embed.add_field(
        name="Repository",
        value=f"`{linked_repo.owner}/{linked_repo.repo}`",
        inline=False,
    )
    embed.add_field(
        name="Selected labels",
        value=_label_list_display(selected_labels),
        inline=False,
    )
    if label_warning:
        embed.add_field(
            name="Labels",
            value=LABEL_FETCH_WARNING,
            inline=False,
        )
    elif len(available_labels) > 25:
        embed.add_field(
            name="Label list",
            value="Showing the first 25 GitHub labels. Use slash-command autocomplete to search more.",
            inline=False,
        )
    elif not available_labels:
        embed.add_field(
            name="Labels",
            value="No GitHub labels were available. The issue can still be created.",
            inline=False,
        )
    return embed


def _labels_available_in_repo(
    labels: list[str] | tuple[str, ...],
    available_labels: tuple[str, ...],
) -> list[str]:
    available = {label.lower() for label in available_labels}
    return [label for label in labels if label.lower() in available] if available else list(labels)


def _available_issue_labels(
    settings: IssueSettings,
    labels: tuple[str, ...],
) -> tuple[str, ...]:
    if not settings.allowed_labels:
        return labels
    allowed = {label.lower() for label in settings.allowed_labels}
    return tuple(label for label in labels if label.lower() in allowed)


async def _create_issue_from_selection(
    interaction: discord.Interaction,
    source_channel_id: int,
    linked_repo: LinkedRepository,
    title: str,
    description: str,
    labels: list[str],
    blocked_labels: list[str] | None = None,
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this action in a server.", ephemeral=True)
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    if await _reject_if_issue_creation_blocked(interaction):
        return

    settings = _effective_issue_settings(db, guild_id)
    if interaction.response.is_done():
        await interaction.followup.send("Creating GitHub issue...", ephemeral=True)
    else:
        await interaction.response.defer(thinking=True)

    body = _issue_body(interaction, description)
    try:
        created_issue = await asyncio.to_thread(
            github_create_issue,
            linked_repo.owner,
            linked_repo.repo,
            title,
            body,
            labels,
        )
    except GitHubAppNotInstalled:
        LOGGER.warning(
            "GitHub App is not installed for issue creation on %s/%s",
            linked_repo.owner,
            linked_repo.repo,
        )
        await interaction.followup.send(
            "GitHub App is not installed for this repository.",
            ephemeral=True,
        )
        return
    except GitHubAppNotConfigured:
        LOGGER.error("GitHub App authentication is not configured for issue creation")
        await interaction.followup.send(
            "GitHub App authentication is not configured.",
            ephemeral=True,
        )
        return
    except GitHubAppMissingPermission as exc:
        LOGGER.warning(
            "GitHub App is missing issue permissions on %s/%s: %s",
            linked_repo.owner,
            linked_repo.repo,
            exc.permission,
        )
        await interaction.followup.send(exc.user_message, ephemeral=True)
        return
    except GitHubAPIError as exc:
        LOGGER.warning(
            "GitHub issue creation failed for %s/%s with status %s: %s",
            linked_repo.owner,
            linked_repo.repo,
            exc.status_code,
            exc.message,
        )
        if labels and exc.status_code in {400, 422}:
            await interaction.followup.send(
                (
                    "GitHub rejected the selected label"
                    f"{'s' if len(labels) != 1 else ''}: {', '.join(labels)}. "
                    "The issue was not created."
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "GitHub API request failed. The issue was not created.",
            ephemeral=True,
        )
        return

    db.record_issue_submission(
        guild_id=guild_id,
        channel_id=source_channel_id,
        user_id=interaction.user.id,
        owner=linked_repo.owner,
        repo=linked_repo.repo,
        issue_number=created_issue.number,
        issue_url=created_issue.url,
        issue_type=_label_summary(labels),
        title=created_issue.title,
    )
    LOGGER.info(
        "Created GitHub issue #%s in %s/%s from Discord guild %s user %s",
        created_issue.number,
        linked_repo.owner,
        linked_repo.repo,
        guild_id,
        interaction.user.id,
    )

    blocked_labels = blocked_labels or []
    submitted_by = _display_name(interaction.user)
    repository = f"{linked_repo.owner}/{linked_repo.repo}"
    label_error = _combined_label_error(created_issue.label_error, blocked_labels)
    labels_applied = created_issue.labels_applied and not blocked_labels
    failed_labels = tuple(blocked_labels) + created_issue.failed_labels
    success_embed = embeds.issue_submission_success_embed(
        issue_title=created_issue.title,
        issue_number=created_issue.number,
        labels=created_issue.labels,
        failed_labels=failed_labels,
        repository=repository,
        submitted_by=submitted_by,
        issue_url=created_issue.url,
        labels_applied=labels_applied,
        label_error=label_error,
    )
    await interaction.followup.send(embed=success_embed)

    if settings.submission_log_channel_id:
        await _send_issue_submission_log(
            interaction.client,  # type: ignore[arg-type]
            settings.submission_log_channel_id,
            issue_title=created_issue.title,
            issue_number=created_issue.number,
            labels=created_issue.labels,
            repository=repository,
            submitted_by=submitted_by,
            submitted_by_avatar_url=_user_avatar_url(interaction.user),
            submitted_by_url=_discord_user_url(interaction.user),
            source_channel_id=source_channel_id,
            issue_url=created_issue.url,
        )


def _review_mode_label(review_mode: str, role_id: int | None = None) -> str:
    if review_mode == REVIEW_MODE_GITHUB_REVIEWERS:
        return "GitHub Reviewers Only"
    if review_mode == REVIEW_MODE_DISCORD_ROLE:
        return f"Discord Role Restricted (<@&{role_id}>)" if role_id else "Discord Role Restricted"
    return "Anyone"


def _permission_status(permission: str | None, allowed: bool) -> str:
    level = permission or "missing"
    return f"{level} ({'ready' if allowed else 'missing write access'})"


async def _update_pr_review_activity(
    interaction: discord.Interaction,
    pr_message: PrMessage,
    event: str,
    detail: str | None,
) -> None:
    channel = await _resolve_text_channel(interaction.client, pr_message.channel_id)  # type: ignore[arg-type]
    if channel is None:
        LOGGER.warning("Could not update PR review card; channel %s unavailable", pr_message.channel_id)
        return

    try:
        message = await channel.fetch_message(pr_message.message_id)
    except discord.NotFound:
        LOGGER.warning("Could not update PR review card; message %s unavailable", pr_message.message_id)
        return
    except discord.Forbidden:
        LOGGER.warning("Missing permission to update PR review card in channel %s", pr_message.channel_id)
        return
    except discord.HTTPException:
        LOGGER.exception("Failed to fetch PR review card %s", pr_message.message_id)
        return

    if message.embeds:
        embed = discord.Embed.from_dict(message.embeds[0].to_dict())
    else:
        embed = discord.Embed(
            title=f"Pull Request #{pr_message.pr_number}",
            url=_pr_url(pr_message),
            color=embeds.NANO_DARK_BLUE,
        )

    for index, field in enumerate(embed.fields):
        if field.name == "Latest Review Activity":
            embed.remove_field(index)
            break
    for index, field in enumerate(embed.fields):
        if field.name == "Review state":
            embed.remove_field(index)
            break

    timestamp = int(datetime.now(timezone.utc).timestamp())
    embed.color = discord.Color(_review_activity_color(event))
    value_lines = [
        f"Action: {_review_activity_label(event)}",
        f"Discord user: {_display_name(interaction.user)}",
        f"Time: <t:{timestamp}:f>",
    ]
    if detail:
        value_lines.insert(2, f"Note: {_short_text(detail)}")
    embed.add_field(name="Review state", value=_review_activity_label(event), inline=True)
    embed.add_field(name="Latest Review Activity", value="\n".join(value_lines), inline=False)

    try:
        await message.edit(embed=embed, view=PullRequestReviewView(_pr_url(pr_message)))
    except discord.Forbidden:
        LOGGER.warning("Missing permission to edit PR review card in channel %s", pr_message.channel_id)
    except discord.HTTPException:
        LOGGER.exception("Failed to update PR review card %s", pr_message.message_id)


def _pr_url(pr_message: PrMessage) -> str:
    return f"https://github.com/{pr_message.owner}/{pr_message.repo}/pull/{pr_message.pr_number}"


def _review_activity_label(event: str) -> str:
    labels = {
        "APPROVE": "Approved",
        "REQUEST_CHANGES": "Requested changes",
        "COMMENT": "Commented",
    }
    return labels.get(event, event.replace("_", " ").title())


def _review_activity_color(event: str) -> int:
    colors = {
        "APPROVE": embeds.NANO_GREEN,
        "REQUEST_CHANGES": embeds.NANO_RED,
        "COMMENT": embeds.NANO_BLUE,
    }
    return colors.get(event, embeds.NANO_DARK_BLUE)


def _short_text(value: str, limit: int = 180) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}..."


def _friendly_pr_review_error(exc: GitHubAPIError) -> str:
    if exc.status_code == 403 and (
        "resource not accessible" in exc.message.lower()
        or "permission" in exc.message.lower()
    ):
        return (
            "Nano GitHub does not currently have Pull Request Review permissions "
            "for this repository."
        )
    if exc.status_code == 404:
        return "Nano GitHub could not find that pull request for this repository."
    return "GitHub API request failed. The pull request review was not submitted."


def _pr_review_success_message(event: str, review_url: str | None) -> str:
    labels = {
        "APPROVE": "Pull request approved on GitHub.",
        "REQUEST_CHANGES": "Changes requested on GitHub.",
        "COMMENT": "Pull request review comment submitted on GitHub.",
    }
    message = labels.get(event, "Pull request review submitted on GitHub.")
    if review_url:
        return f"{message}\n{review_url}"
    return message


def _discord_identity_names(user: discord.abc.User) -> set[str]:
    names = {
        getattr(user, "name", ""),
        getattr(user, "global_name", "") or "",
        getattr(user, "display_name", "") or "",
        str(user).split("#", 1)[0],
    }
    return {name.strip().lower() for name in names if name and name.strip()}


def member_is_blocked_from_issue_creation(
    member: discord.Member,
    db: Database | None = None,
) -> bool:
    if db is None:
        return False
    return bool(_issue_creation_blocked_roles(member, db))


def _issue_creation_blocked_roles(
    member: discord.Member,
    db: Database,
) -> tuple[discord.Role, ...]:
    blocked_role_ids = set(db.get_issue_blocked_roles(member.guild.id))
    if not blocked_role_ids:
        return ()
    return tuple(role for role in member.roles if role.id in blocked_role_ids)


async def _reject_if_issue_creation_blocked(interaction: discord.Interaction) -> bool:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        if interaction.response.is_done():
            await interaction.followup.send("Run this action in a server.", ephemeral=True)
        else:
            await interaction.response.send_message("Run this action in a server.", ephemeral=True)
        return True

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None:
        return False

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    blocked_roles = _issue_creation_blocked_roles(member, db)
    if not blocked_roles:
        return False

    if interaction.response.is_done():
        await interaction.followup.send(ISSUE_CREATION_BLOCKED_MESSAGE, ephemeral=True)
    else:
        await interaction.response.send_message(ISSUE_CREATION_BLOCKED_MESSAGE, ephemeral=True)
    await _send_issue_creation_blocked_log(interaction, blocked_roles)
    return True


async def _send_issue_creation_blocked_log(
    interaction: discord.Interaction,
    blocked_roles: tuple[discord.Role, ...],
) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        return

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    settings = db.get_issue_settings(guild_id)
    log_channel_id = settings.submission_log_channel_id if settings else None
    if log_channel_id is None:
        return

    log_channel = await _resolve_text_channel(  # type: ignore[arg-type]
        interaction.client,
        log_channel_id,
    )
    if log_channel is None:
        LOGGER.warning(
            "Configured issue creation blocked-attempt log channel %s is unavailable",
            log_channel_id,
        )
        return

    guild_label = (
        f"{interaction.guild.name} ({guild_id})"
        if interaction.guild
        else f"Guild {guild_id}"
    )
    embed = embeds.issue_creation_blocked_attempt_embed(
        user=f"{interaction.user.mention} ({interaction.user.id})",
        guild=guild_label,
        blocked_roles=tuple(role.mention for role in blocked_roles),
    )
    try:
        await log_channel.send(embed=embed)
    except discord.Forbidden:
        LOGGER.warning(
            "Missing permission to send issue creation blocked-attempt log to channel %s",
            log_channel_id,
        )
    except discord.HTTPException:
        LOGGER.exception(
            "Failed to send issue creation blocked-attempt log to channel %s",
            log_channel_id,
        )


def _labels_for_issue_create(
    settings: IssueSettings | None,
    labels: str | None,
) -> tuple[list[str], list[str]]:
    LOGGER.debug("Raw Discord label input: %r", labels)
    parsed_labels = _parse_comma_labels(labels)
    LOGGER.debug("Parsed Discord label list: %s", parsed_labels)
    if parsed_labels:
        selected_labels = parsed_labels
    elif settings and settings.default_labels:
        selected_labels = list(settings.default_labels)
    else:
        selected_labels = []

    if not settings or not settings.allowed_labels:
        return selected_labels, []

    allowed = {label.lower(): label for label in settings.allowed_labels}
    accepted = [allowed[label.lower()] for label in selected_labels if label.lower() in allowed]
    blocked = [label for label in selected_labels if label.lower() not in allowed]
    return accepted, blocked


def _invalid_label_message(label: str, allowed_labels: tuple[str, ...]) -> str:
    allowed = ", ".join(allowed_labels) if allowed_labels else "None"
    return f"Invalid label: {label}. Allowed labels are: {allowed}"


def _parse_comma_labels(labels: str | None) -> list[str]:
    parsed: list[str] = []
    seen: set[str] = set()
    for raw_label in (labels or "").split(","):
        label = raw_label.strip()
        key = label.lower()
        if label and key not in seen:
            parsed.append(label)
            seen.add(key)
    return parsed


def _label_summary(labels: list[str] | tuple[str, ...]) -> str:
    return ", ".join(labels) if labels else "unlabeled"


def _label_list_display(labels: list[str] | tuple[str, ...]) -> str:
    return ", ".join(f"`{label}`" for label in labels) if labels else "None"


def _blocked_role_display(role_ids: tuple[int, ...]) -> str:
    if not role_ids:
        return "None"
    return _dashboard_value(", ".join(f"<@&{role_id}>" for role_id in role_ids), limit=900)


def _combined_label_error(github_error: str | None, blocked_labels: list[str]) -> str | None:
    errors = []
    if blocked_labels:
        errors.append("Not allowed in this server: " + ", ".join(blocked_labels))
    if github_error:
        errors.append(github_error)
    return " ".join(errors) if errors else None


def _issue_body(
    interaction: discord.Interaction,
    description: str,
) -> str:
    return "\n".join(
        [
            description.strip(),
            "",
            "---",
            "Submitted from Discord",
            "",
            f"Discord username: {interaction.user}",
        ]
    )


def _display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", None) or getattr(user, "name", "Unknown user")


def _user_avatar_url(user: discord.abc.User) -> str | None:
    avatar = getattr(user, "display_avatar", None) or getattr(user, "avatar", None)
    url = getattr(avatar, "url", None)
    return url if isinstance(url, str) and url else None


def _discord_user_url(user: discord.abc.User) -> str:
    return f"https://discord.com/users/{user.id}"


def _issue_status_embed(settings: IssueSettings | None) -> discord.Embed:
    embed = discord.Embed(title="Issue Creation Status", color=embeds.NANO_BLUE)
    if settings is None:
        embed.add_field(name="Enabled", value="Not configured", inline=True)
        embed.add_field(name="Default repository", value="Not configured", inline=True)
        embed.add_field(name="Suggestion label", value="suggestion", inline=True)
        embed.add_field(name="Bug label", value="bug", inline=True)
        embed.add_field(name="Allowed labels", value="Any linked repository label", inline=False)
        embed.add_field(name="Default labels", value="None", inline=False)
        embed.add_field(name="Log channel", value="Not configured", inline=True)
        return embed

    default_repo = (
        f"`{settings.default_owner}/{settings.default_repo}`"
        if settings.default_owner and settings.default_repo
        else "Not configured"
    )
    embed.add_field(name="Enabled", value="Yes" if settings.enabled else "No", inline=True)
    embed.add_field(name="Default repository", value=default_repo, inline=True)
    embed.add_field(name="Suggestion label", value=f"`{settings.suggestion_label}`", inline=True)
    embed.add_field(name="Bug label", value=f"`{settings.bug_label}`", inline=True)
    embed.add_field(
        name="Allowed labels",
        value=_label_list_display(settings.allowed_labels) if settings.allowed_labels else "Any",
        inline=False,
    )
    embed.add_field(
        name="Default labels",
        value=_label_list_display(settings.default_labels),
        inline=False,
    )
    embed.add_field(
        name="Log channel",
        value=f"<#{settings.submission_log_channel_id}>"
        if settings.submission_log_channel_id
        else "Not configured",
        inline=True,
    )
    return embed


async def _send_issue_submission_log(
    bot: commands.Bot,
    log_channel_id: int,
    *,
    issue_title: str,
    issue_number: int,
    labels: tuple[str, ...],
    repository: str,
    submitted_by: str,
    submitted_by_avatar_url: str | None,
    submitted_by_url: str | None,
    source_channel_id: int,
    issue_url: str,
) -> None:
    log_channel = await _resolve_text_channel(bot, log_channel_id)
    if log_channel is None:
        LOGGER.warning(
            "Configured issue submission log channel %s is unavailable",
            log_channel_id,
        )
        return

    embed = embeds.issue_submission_log_embed(
        issue_title=issue_title,
        issue_number=issue_number,
        labels=labels,
        repository=repository,
        submitted_by=submitted_by,
        submitted_by_avatar_url=submitted_by_avatar_url,
        submitted_by_url=submitted_by_url,
        channel_id=source_channel_id,
        issue_url=issue_url,
    )
    try:
        await log_channel.send(embed=embed)
    except discord.Forbidden:
        LOGGER.warning(
            "Missing permission to send issue submission log to channel %s",
            log_channel_id,
        )
    except discord.HTTPException:
        LOGGER.exception("Failed to send issue submission log to channel %s", log_channel_id)


async def _edit_dashboard(
    interaction: discord.Interaction,
    section: str,
    selected_repo: tuple[str, str] | None = None,
    *,
    selected_log_type: str = "commits",
) -> None:
    if not await _ensure_dashboard_access(interaction):
        return
    await interaction.response.defer()
    embed, view = await _build_dashboard(
        interaction,
        section,
        selected_repo,
        selected_log_type=selected_log_type,
    )
    await interaction.edit_original_response(embed=embed, view=view)


async def _build_dashboard(
    interaction: discord.Interaction,
    section: str,
    selected_repo: tuple[str, str] | None = None,
    *,
    selected_log_type: str = "commits",
) -> tuple[discord.Embed, GitHubDashboardView]:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        raise RuntimeError("Dashboard requires a guild interaction")

    db: Database = interaction.client.db  # type: ignore[attr-defined]
    config = db.get_status(guild_id)
    linked_repos = db.list_linked_repositories_for_guild(guild_id)
    installed_repos = db.list_installed_repositories()
    issue_settings = db.get_issue_settings(guild_id)
    issue_blocked_role_ids = db.get_issue_blocked_roles(guild_id)
    pr_review_mode = str(config["pr_review_mode"])
    pr_review_role_id = config["pr_review_role_id"]
    issue_enabled = bool(issue_settings.enabled) if issue_settings else False
    log_channels = config["log_channels"]
    if not isinstance(log_channels, dict):
        log_channels = {}
    selected_log_type = (
        selected_log_type
        if selected_log_type in LOG_DASHBOARD_TYPES
        else "commits"
    )

    default_repo = _configured_default_repo(issue_settings)
    if selected_repo is None and section == "default_repo":
        selected_repo = default_repo
    if selected_repo and not db.get_linked_repository(guild_id, *selected_repo):
        selected_repo = None
    if selected_repo is None and linked_repos and section != "default_repo":
        selected_repo = (linked_repos[0].owner, linked_repos[0].repo)

    app_statuses: dict[tuple[str, str], str] = {}
    if section in {"overview", "github_app"}:
        app_statuses = await _github_app_statuses(linked_repos)

    embed = _dashboard_embed(
        section=section,
        config=config,
        linked_repos=linked_repos,
        installed_repos=installed_repos,
        issue_settings=issue_settings,
        issue_blocked_role_ids=issue_blocked_role_ids,
        selected_log_type=selected_log_type,
        guild=interaction.guild,
        pr_review_mode=pr_review_mode,
        pr_review_role_id=pr_review_role_id,
        selected_repo=selected_repo,
        default_repo=default_repo,
        app_statuses=app_statuses,
        bot_ready=interaction.client.is_ready(),
    )
    view = GitHubDashboardView(
        guild_id,
        section,
        linked_repos=linked_repos,
        installed_repos=installed_repos,
        selected_repo=selected_repo,
        selected_log_type=selected_log_type,
        pr_review_mode=pr_review_mode,
        issue_enabled=issue_enabled,
        issue_submission_log_channel_id=(
            issue_settings.submission_log_channel_id if issue_settings else None
        ),
        issue_blocked_role_ids=issue_blocked_role_ids,
        log_channels={
            str(event): int(channel_id)
            for event, channel_id in log_channels.items()
            if isinstance(event, str) and isinstance(channel_id, int)
        },
        pr_review_channel_id=int(config["pr_review_channel"])
        if config["pr_review_channel"]
        else None,
    )
    return embed, view


def _dashboard_embed(
    *,
    section: str,
    config: dict[str, object],
    linked_repos: list[LinkedRepository],
    installed_repos: list[InstalledRepository],
    issue_settings: IssueSettings | None,
    issue_blocked_role_ids: tuple[int, ...],
    selected_log_type: str,
    guild: discord.Guild | None,
    pr_review_mode: str,
    pr_review_role_id: int | None,
    selected_repo: tuple[str, str] | None,
    default_repo: tuple[str, str] | None,
    app_statuses: dict[tuple[str, str], str],
    bot_ready: bool,
) -> discord.Embed:
    title = f"Nano GitHub Dashboard - {DASHBOARD_SECTIONS.get(section, 'Overview')}"
    embed = discord.Embed(title=title, color=embeds.NANO_BLUE)
    embed.set_footer(text="Admin control panel")

    log_channels = config["log_channels"]
    pr_channel = config["pr_review_channel"]
    if not isinstance(log_channels, dict):
        log_channels = {}

    if section == "overview":
        embed.description = "Live server configuration and GitHub integration summary."
        embed.add_field(name="Linked repositories", value=_repo_list(linked_repos), inline=False)
        embed.add_field(
            name="GitHub App installed repositories",
            value=_installed_repo_list(installed_repos, linked_repos),
            inline=False,
        )
        embed.add_field(
            name="Default repository",
            value=_default_repo_summary(linked_repos, default_repo),
            inline=False,
        )
        embed.add_field(
            name="Log channels",
            value=_log_channel_list(log_channels, pr_channel, guild),
            inline=False,
        )
        embed.add_field(
            name="PR reviews",
            value=(
                f"Channel: {_channel_display(pr_channel)}\n"
                f"Mode: {_review_mode_label(pr_review_mode, pr_review_role_id)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Issue creation",
            value=_issue_dashboard_summary(issue_settings, issue_blocked_role_ids, guild),
            inline=False,
        )
        embed.add_field(
            name="GitHub App readiness",
            value=_app_status_list(app_statuses, linked_repos),
            inline=False,
        )
        embed.add_field(name="Webhook", value=_webhook_dashboard_summary(linked_repos), inline=False)
        embed.add_field(name="Bot/API", value=_bot_api_summary(bot_ready), inline=False)
        return embed

    if section == "repositories":
        embed.description = "Select repositories already known from GitHub App installations."
        embed.add_field(name="Linked repositories", value=_repo_list(linked_repos), inline=False)
        embed.add_field(
            name="Installed repositories",
            value=_installed_repo_list(installed_repos, linked_repos),
            inline=False,
        )
        embed.add_field(
            name="Advanced",
            value=(
                "Use manual linking only for legacy webhook setups or before the first GitHub App "
                "webhook delivery has taught Nano GitHub about the installation."
            ),
            inline=False,
        )
        return embed

    if section == "default_repo":
        embed.description = "Default repository for issue creation and repository-aware actions."
        embed.add_field(
            name="Current default",
            value=_default_repo_summary(linked_repos, default_repo),
            inline=False,
        )
        embed.add_field(name="Linked repositories", value=_repo_list(linked_repos), inline=False)
        embed.add_field(
            name="Selection rule",
            value=(
                "Explicit UI selection wins, then configured default, then automatic single linked repo. "
                "If multiple repos are linked and no default is set, users get a repository dropdown."
            ),
            inline=False,
        )
        return embed

    if section == "logs":
        embed.description = "Read-only GitHub event logging channels."
        embed.add_field(
            name="Configured log channels",
            value=_log_channel_list(log_channels, pr_channel, guild),
            inline=False,
        )
        embed.add_field(
            name="Selected",
            value=_selected_log_channel_summary(
                selected_log_type,
                log_channels,
                pr_channel,
                guild,
            ),
            inline=False,
        )
        return embed

    if section == "pr_reviews":
        embed.description = "Interactive pull request review cards and button permissions."
        embed.add_field(name="Review channel", value=_channel_display(pr_channel), inline=True)
        embed.add_field(
            name="Review mode",
            value=_review_mode_label(pr_review_mode, pr_review_role_id),
            inline=True,
        )
        embed.add_field(
            name="Edit",
            value=(
                "Use the selector below to switch between Anyone and GitHub Reviewers Only. "
                "PR review channel and role selection are shown here and remain advanced dashboard follow-ups."
            ),
            inline=False,
        )
        return embed

    if section == "issues":
        embed.description = "Discord issue creation settings for this server."
        embed.add_field(
            name="Status",
            value=_issue_dashboard_summary(issue_settings, issue_blocked_role_ids, guild),
            inline=False,
        )
        embed.add_field(name="Labels", value=_issue_label_summary(issue_settings), inline=False)
        embed.add_field(
            name="Edit",
            value=(
                "Use the controls below to enable or disable issue creation, edit labels, "
                "and manage blocked roles. Use Default Repository to change where issues "
                "are created by default."
            ),
            inline=False,
        )
        return embed

    if section == "github_app":
        embed.description = "Live GitHub App installation and permission checks."
        embed.add_field(
            name="Known installed repositories",
            value=_installed_repo_list(installed_repos, linked_repos),
            inline=False,
        )
        embed.add_field(
            name="Selected repository",
            value=_selected_app_repo_status(selected_repo, linked_repos, app_statuses),
            inline=False,
        )
        embed.add_field(
            name="Repository readiness",
            value=_app_status_list(app_statuses, linked_repos),
            inline=False,
        )
        embed.add_field(
            name="Required permissions",
            value="Metadata: read | Issues: write | Pull requests: write",
            inline=False,
        )
        return embed

    if section == "webhook":
        embed.description = "GitHub App webhooks are automatic. Manual webhooks are legacy/advanced."
        embed.add_field(name="GitHub App webhook", value="Configured in the GitHub App, no server secret shown.", inline=False)
        embed.add_field(name="Legacy payload URL", value=f"`{WEBHOOK_PAYLOAD_URL}`", inline=False)
        embed.add_field(name="Legacy content type", value="`application/json`", inline=True)
        embed.add_field(name="Legacy events", value=WEBHOOK_EVENTS, inline=False)
        embed.add_field(
            name="Webhook secrets",
            value=_webhook_secret_list(linked_repos, selected_repo),
            inline=False,
        )
        embed.add_field(
            name="Reveal",
            value="Only reveal per-repository secrets for legacy manual GitHub webhooks.",
            inline=False,
        )
        return embed

    embed.description = "Choose a dashboard section from the menu."
    return embed


async def _github_app_statuses(
    linked_repos: list[LinkedRepository],
) -> dict[tuple[str, str], str]:
    if not linked_repos:
        return {}

    async def check(linked_repo: LinkedRepository) -> tuple[tuple[str, str], str]:
        key = (linked_repo.owner, linked_repo.repo)
        try:
            result = await asyncio.to_thread(
                check_repository_permissions,
                linked_repo.owner,
                linked_repo.repo,
            )
        except GitHubAppNotConfigured:
            return key, "GitHub App credentials are not configured"
        except GitHubAPIError as exc:
            return key, f"GitHub API check failed ({exc.status_code})"

        if not result.installed:
            return key, "App not installed"
        missing = []
        if not result.can_create_issues:
            missing.append("issues:write")
        if not result.can_review_pull_requests:
            missing.append("pull_requests:write")
        installation = f"installation {result.installation_id}" if result.installation_id else "no installation id"
        if missing:
            return key, f"{installation}; missing " + ", ".join(missing)
        return key, f"{installation}; issues ready; PR reviews ready"

    pairs = await asyncio.gather(*(check(linked_repo) for linked_repo in linked_repos))
    return dict(pairs)


def _repo_list(linked_repos: list[LinkedRepository]) -> str:
    if not linked_repos:
        return "None linked. Use the dashboard setup guidance to link a repository."
    lines = []
    for repo in linked_repos:
        installation = f" installation `{repo.installation_id}`" if repo.installation_id else " no installation id yet"
        lines.append(f"`{repo.owner}/{repo.repo}`:{installation}")
    return _dashboard_value("\n".join(lines))


def _installed_repo_list(
    installed_repos: list[InstalledRepository],
    linked_repos: list[LinkedRepository],
) -> str:
    if not installed_repos:
        return "None known yet. GitHub App webhook deliveries teach Nano GitHub about installations."
    linked = {(repo.owner, repo.repo) for repo in linked_repos}
    lines = []
    for repo in installed_repos:
        state = "linked" if (repo.owner, repo.repo) in linked else "available"
        lines.append(f"`{repo.repository_full_name}`: installation `{repo.installation_id}`, {state}")
    return _dashboard_value("\n".join(lines))


def _configured_default_repo(settings: IssueSettings | None) -> tuple[str, str] | None:
    if settings and settings.default_owner and settings.default_repo:
        return settings.default_owner, settings.default_repo
    return None


def _default_repo_summary(
    linked_repos: list[LinkedRepository],
    default_repo: tuple[str, str] | None,
) -> str:
    if default_repo:
        owner, repo = default_repo
        return f"Configured: `{owner}/{repo}`"
    if len(linked_repos) == 1:
        linked_repo = linked_repos[0]
        return f"Automatic single-repo default: `{linked_repo.owner}/{linked_repo.repo}`"
    if len(linked_repos) > 1:
        return "No configured default. Users will be asked to choose from linked repositories."
    return "No linked repositories."


def _log_dashboard_type_label(log_type: str) -> str:
    labels = {
        "commits": "Commit logs",
        "issues": "Issue logs",
        "comments": "Comment logs",
        "releases": "Release logs",
        "pr_reviews": "PR review logs",
    }
    return labels.get(log_type, log_type.replace("_", " ").title())


def _log_channel_list(
    log_channels: dict[object, object],
    pr_channel: object = None,
    guild: discord.Guild | None = None,
) -> str:
    lines = []
    for log_type in LOG_DASHBOARD_TYPES:
        lines.append(
            f"{_log_dashboard_type_label(log_type)}: "
            f"{_selected_log_channel_display(log_type, log_channels, pr_channel, guild)}"
        )
    return _dashboard_value("\n".join(lines))


def _selected_log_channel_display(
    log_type: str,
    log_channels: dict[object, object],
    pr_channel: object,
    guild: discord.Guild | None = None,
) -> str:
    channel_id = pr_channel if log_type == "pr_reviews" else log_channels.get(log_type)
    return _channel_display(channel_id, guild)


def _selected_log_channel_summary(
    log_type: str,
    log_channels: dict[object, object],
    pr_channel: object,
    guild: discord.Guild | None = None,
) -> str:
    return (
        f"{_log_dashboard_type_label(log_type)}: "
        f"{_selected_log_channel_display(log_type, log_channels, pr_channel, guild)}"
    )


def _channel_display(channel_id: object, guild: discord.Guild | None = None) -> str:
    if not channel_id:
        return "Not configured"
    try:
        resolved_channel_id = int(channel_id)
    except (TypeError, ValueError):
        return "Missing/deleted"
    if guild is not None and guild.get_channel(resolved_channel_id) is None:
        return f"Missing/deleted (<#{channel_id}>)"
    return f"<#{channel_id}>"


def _is_sendable_log_channel(
    interaction: discord.Interaction,
    channel: object,
) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    guild = interaction.guild
    bot_member = guild.me if guild else None
    if bot_member is None:
        return False
    permissions = channel.permissions_for(bot_member)
    return permissions.view_channel and permissions.send_messages and permissions.embed_links


def _issue_dashboard_summary(
    settings: IssueSettings | None,
    blocked_role_ids: tuple[int, ...] = (),
    guild: discord.Guild | None = None,
) -> str:
    if settings is None:
        return _dashboard_value(
            "\n".join(
                [
                    (
                        "Enabled with automatic defaults. Configure labels and default "
                        "repository in the dashboard."
                    ),
                    f"Blocked roles: {_blocked_role_display(blocked_role_ids)}",
                ]
            )
        )
    default_repo = (
        f"`{settings.default_owner}/{settings.default_repo}`"
        if settings.default_owner and settings.default_repo
        else "Not configured"
    )
    log_channel = _channel_display(settings.submission_log_channel_id, guild)
    return _dashboard_value(
        "\n".join(
            [
                f"Enabled: {'Yes' if settings.enabled else 'No'}",
                f"Default repository: {default_repo}",
                f"Submission log: {log_channel}",
                f"Blocked roles: {_blocked_role_display(blocked_role_ids)}",
            ]
        )
    )


def _issue_label_summary(settings: IssueSettings | None) -> str:
    if settings is None:
        return "Allowed labels: Any\nDefault labels: None\nQuick labels: suggestion, bug"
    return _dashboard_value(
        "\n".join(
            [
                "Allowed labels: "
                + (
                    _label_list_display(settings.allowed_labels)
                    if settings.allowed_labels
                    else "Any"
                ),
                f"Default labels: {_label_list_display(settings.default_labels)}",
                f"Suggestion quick label: `{settings.suggestion_label}`",
                f"Bug quick label: `{settings.bug_label}`",
            ]
        )
    )


def _app_status_list(
    app_statuses: dict[tuple[str, str], str],
    linked_repos: list[LinkedRepository],
) -> str:
    if not linked_repos:
        return "No linked repositories to check."
    lines = []
    for linked_repo in linked_repos:
        status = app_statuses.get((linked_repo.owner, linked_repo.repo), "Not checked")
        lines.append(f"`{linked_repo.owner}/{linked_repo.repo}`: {status}")
    return _dashboard_value("\n".join(lines))


def _selected_app_repo_status(
    selected_repo: tuple[str, str] | None,
    linked_repos: list[LinkedRepository],
    app_statuses: dict[tuple[str, str], str],
) -> str:
    if selected_repo is None:
        return "No linked repository selected."
    owner, repo = selected_repo
    linked_repo = next(
        (
            candidate
            for candidate in linked_repos
            if candidate.owner == owner and candidate.repo == repo
        ),
        None,
    )
    if linked_repo is None:
        return f"`{owner}/{repo}` is not linked to this server."
    installation = (
        f"`{linked_repo.installation_id}`"
        if linked_repo.installation_id is not None
        else "Not recorded yet"
    )
    readiness = app_statuses.get((owner, repo), "Not checked")
    return _dashboard_value(
        "\n".join(
            [
                f"Repository: `{owner}/{repo}`",
                f"Installation ID: {installation}",
                f"Actions: {readiness}",
            ]
        )
    )


def _webhook_dashboard_summary(linked_repos: list[LinkedRepository]) -> str:
    secret_count = sum(1 for repo in linked_repos if repo.webhook_secret)
    return _dashboard_value(
        "\n".join(
            [
                f"Payload URL: `{WEBHOOK_PAYLOAD_URL}`",
                "Content type: `application/json`",
                f"Secret status: {secret_count}/{len(linked_repos)} linked repos have a secret",
            ]
        )
    )


def _webhook_secret_list(
    linked_repos: list[LinkedRepository],
    selected_repo: tuple[str, str] | None,
) -> str:
    if not linked_repos:
        return "No linked repositories."
    lines = []
    for linked_repo in linked_repos:
        selected = " selected" if selected_repo == (linked_repo.owner, linked_repo.repo) else ""
        status = "exists" if linked_repo.webhook_secret else "missing"
        lines.append(f"`{linked_repo.owner}/{linked_repo.repo}`: secret {status}{selected}")
    return _dashboard_value("\n".join(lines))


def _bot_api_summary(bot_ready: bool) -> str:
    bot_state = "Connected" if bot_ready else "Starting"
    return f"Discord bot: {bot_state}\nAPI: webhook route configured at `/webhooks/github`"


def _dashboard_value(value: str, limit: int = 1024) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3].rstrip()}..."


def _parse_repo_value(value: str) -> tuple[str, str] | None:
    if "/" not in value:
        return None
    owner, repo = value.split("/", 1)
    owner = owner.strip().lower()
    repo = repo.strip().lower()
    if not owner or not repo:
        return None
    return owner, repo


async def _resolve_text_channel(
    bot: commands.Bot,
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
    return None
