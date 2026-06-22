from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from nano_github.database import Database

LOGGER = logging.getLogger(__name__)

LOG_EVENT_TYPES = ("commits", "issues", "comments", "releases")
LogEventType = Literal["commits", "issues", "comments", "releases"]
WEBHOOK_PAYLOAD_URL = "https://api.nanoworks.co.uk/webhooks/github"
WEBHOOK_EVENTS = "Pushes, Issues, Issue comments, Pull requests, Releases"


class PullRequestReviewView(discord.ui.View):
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
        await interaction.response.send_message(
            "GitHub App review permissions are not configured yet.",
            ephemeral=True,
        )

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
        await interaction.response.send_message(
            "GitHub App review permissions are not configured yet.",
            ephemeral=True,
        )

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
        await interaction.response.send_message(
            "GitHub App comment permissions are not configured yet.",
            ephemeral=True,
        )


class NanoGitHubBot(commands.Bot):
    def __init__(self, db: Database) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = db
        self.tree.add_command(github_group)

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


def _require_guild(interaction: discord.Interaction) -> int | None:
    if interaction.guild_id is None:
        return None
    return int(interaction.guild_id)


async def _ensure_manage_guild(interaction: discord.Interaction) -> bool:
    permissions = interaction.permissions
    if not permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to configure Nano GitHub.",
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
