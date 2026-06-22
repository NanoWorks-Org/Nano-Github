from __future__ import annotations

import asyncio
import logging
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
    submit_pull_request_review,
)

LOGGER = logging.getLogger(__name__)

LOG_EVENT_TYPES = ("commits", "issues", "comments", "releases")
LogEventType = Literal["commits", "issues", "comments", "releases"]
WEBHOOK_PAYLOAD_URL = "https://api.nanoworks.co.uk/webhooks/github"
WEBHOOK_EVENTS = "Pushes, Issues, Issue comments, Pull requests, Releases"


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
        await _submit_pr_review(
            interaction,
            self.pr_message,
            "COMMENT",
            str(self.review_body.value),
        )


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
        await _handle_pr_review_button(interaction, "APPROVE")

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
        body = f"Changes requested from Discord by {_display_name(interaction.user)}."
        await _handle_pr_review_button(interaction, "REQUEST_CHANGES", body)

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


@github_group.command(name="setup", description="Initialize Nano GitHub for this server.")
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
        "Nano GitHub is ready for this server. Link a repository with `/github link_repo`.",
        ephemeral=True,
    )


@github_group.command(name="link_repo", description="Link a GitHub repository to this server.")
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
            "Use `/github webhook_info` to view the webhook secret."
        ),
        ephemeral=True,
    )


@github_group.command(name="webhook_info", description="Show webhook setup details for a linked repository.")
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
            f"Webhook info for `{linked_repo.owner}/{linked_repo.repo}`\n"
            f"Payload URL: `{WEBHOOK_PAYLOAD_URL}`\n"
            "Content Type: `application/json`\n"
            f"Secret: `{linked_repo.webhook_secret}`\n"
            f"Events to select: {WEBHOOK_EVENTS}"
        ),
        ephemeral=True,
    )


@github_group.command(name="rotate_secret", description="Rotate the webhook secret for a linked repository.")
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
            f"Rotated webhook secret for `{linked_repo.owner}/{linked_repo.repo}`.\n"
            f"New secret: `{linked_repo.webhook_secret}`\n"
            "Update the GitHub webhook secret before new deliveries will be accepted."
        ),
        ephemeral=True,
    )


@github_group.command(name="set_log_channel", description="Set a read-only GitHub event log channel.")
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


@github_group.command(
    name="set_pr_review_channel",
    description="Set the interactive pull request review channel.",
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


@github_group.command(
    name="set_review_mode",
    description="Set who can use pull request review buttons.",
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


@github_group.command(
    name="app_status",
    description="Check GitHub App installation and repository permissions.",
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


@github_group.command(name="status", description="Show Nano GitHub configuration for this server.")
async def status(interaction: discord.Interaction) -> None:
    guild_id = _require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
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


@github_group.command(name="unlink_repo", description="Unlink all GitHub repositories from this server.")
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


@issue_group.command(name="create", description="Create a GitHub issue from Discord.")
@app_commands.rename(issue_type="type")
@app_commands.describe(
    issue_type="Create a suggestion or bug report",
    title="GitHub issue title",
    description="GitHub issue description",
    owner="GitHub repository owner or organization",
    repo="GitHub repository name",
)
@app_commands.choices(
    issue_type=[
        app_commands.Choice(name="suggestion", value="suggestion"),
        app_commands.Choice(name="bug", value="bug"),
    ]
)
async def create_issue(
    interaction: discord.Interaction,
    issue_type: app_commands.Choice[str],
    title: str,
    description: str,
    owner: str | None = None,
    repo: str | None = None,
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
    settings = db.get_issue_settings(guild_id)
    if settings and not settings.enabled:
        await interaction.response.send_message(
            "Issue creation is disabled for this server.",
            ephemeral=True,
        )
        return

    linked_repo, error_message = _resolve_issue_repository(db, guild_id, settings, owner, repo)
    if error_message:
        await interaction.response.send_message(error_message, ephemeral=True)
        return
    if linked_repo is None:
        await interaction.response.send_message(
            "No default repository configured.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    label = _label_for_issue_type(settings, issue_type.value)
    body = _issue_body(interaction, issue_type.value, description)
    try:
        created_issue = await asyncio.to_thread(
            github_create_issue,
            linked_repo.owner,
            linked_repo.repo,
            title,
            body,
            [label] if label else [],
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
        await interaction.followup.send(
            "GitHub API request failed. The issue was not created.",
            ephemeral=True,
        )
        return

    db.record_issue_submission(
        guild_id=guild_id,
        channel_id=int(interaction.channel_id),
        user_id=interaction.user.id,
        owner=linked_repo.owner,
        repo=linked_repo.repo,
        issue_number=created_issue.number,
        issue_url=created_issue.url,
        issue_type=issue_type.value,
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

    submitted_by = _display_name(interaction.user)
    repository = f"{linked_repo.owner}/{linked_repo.repo}"
    success_embed = embeds.issue_submission_success_embed(
        issue_title=created_issue.title,
        issue_number=created_issue.number,
        issue_type=issue_type.value,
        repository=repository,
        submitted_by=submitted_by,
        issue_url=created_issue.url,
        labels_applied=created_issue.labels_applied,
        label_error=created_issue.label_error,
    )
    await interaction.followup.send(embed=success_embed)

    if settings and settings.submission_log_channel_id:
        await _send_issue_submission_log(
            interaction.client,  # type: ignore[arg-type]
            settings.submission_log_channel_id,
            issue_title=created_issue.title,
            issue_number=created_issue.number,
            issue_type=issue_type.value,
            repository=repository,
            submitted_by=submitted_by,
            source_channel_id=int(interaction.channel_id),
            issue_url=created_issue.url,
        )


@issue_group.command(name="configure", description="Configure Discord issue creation.")
@app_commands.describe(
    default_repo_owner="Default linked GitHub repository owner or organization",
    default_repo_name="Default linked GitHub repository name",
    suggestion_label="Label used for suggestions",
    bug_label="Label used for bug reports",
    submission_log_channel="Optional Discord channel for issue submission logs",
)
async def configure_issue_creation(
    interaction: discord.Interaction,
    default_repo_owner: str,
    default_repo_name: str,
    suggestion_label: str = "suggestion",
    bug_label: str = "bug",
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
        submission_log_channel_id=submission_log_channel.id if submission_log_channel else None,
    )

    embed = _issue_status_embed(settings)
    embed.title = "Issue Creation Configured"
    await interaction.response.send_message(embed=embed, ephemeral=True)


@issue_group.command(name="status", description="Show issue creation configuration.")
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


@issue_group.command(name="disable", description="Disable Discord issue creation for this server.")
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


def _review_mode_label(review_mode: str, role_id: int | None = None) -> str:
    if review_mode == REVIEW_MODE_GITHUB_REVIEWERS:
        return "GitHub Reviewers Only"
    if review_mode == REVIEW_MODE_DISCORD_ROLE:
        return f"Discord Role Restricted (<@&{role_id}>)" if role_id else "Discord Role Restricted"
    return "Anyone"


def _permission_status(permission: str | None, allowed: bool) -> str:
    level = permission or "missing"
    return f"{level} ({'ready' if allowed else 'missing write access'})"


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


def _label_for_issue_type(settings: IssueSettings | None, issue_type: str) -> str:
    if issue_type == "bug":
        return settings.bug_label if settings else "bug"
    return settings.suggestion_label if settings else "suggestion"


def _issue_body(
    interaction: discord.Interaction,
    _issue_type: str,
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


def _issue_status_embed(settings: IssueSettings | None) -> discord.Embed:
    embed = discord.Embed(title="Issue Creation Status", color=embeds.NANO_BLUE)
    if settings is None:
        embed.add_field(name="Enabled", value="Not configured", inline=True)
        embed.add_field(name="Default repository", value="Not configured", inline=True)
        embed.add_field(name="Suggestion label", value="suggestion", inline=True)
        embed.add_field(name="Bug label", value="bug", inline=True)
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
    issue_type: str,
    repository: str,
    submitted_by: str,
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
        issue_type=issue_type,
        repository=repository,
        submitted_by=submitted_by,
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
