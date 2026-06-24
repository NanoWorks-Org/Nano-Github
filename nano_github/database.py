from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

REVIEW_MODE_ANYONE = "anyone"
REVIEW_MODE_GITHUB_REVIEWERS = "github_reviewers"
REVIEW_MODE_DISCORD_ROLE = "discord_role"
REVIEW_MODES = {
    REVIEW_MODE_ANYONE,
    REVIEW_MODE_GITHUB_REVIEWERS,
    REVIEW_MODE_DISCORD_ROLE,
}


@dataclass(frozen=True)
class LinkedRepository:
    guild_id: int
    owner: str
    repo: str
    webhook_secret: str
    installation_id: int | None = None
    repository_full_name: str | None = None


@dataclass(frozen=True)
class InstalledRepository:
    installation_id: int
    owner: str
    repo: str
    repository_full_name: str


@dataclass(frozen=True)
class GuildInstallation:
    guild_id: int
    installation_id: int
    account_login: str | None
    account_type: str | None
    created_at: str


class RepositoryNotLinkedToGuild(ValueError):
    def __init__(self, guild_id: int, repo_full_name: str) -> None:
        self.guild_id = guild_id
        self.repo_full_name = repo_full_name
        super().__init__(
            f"{repo_full_name.strip().lower()} is not linked to Discord guild {guild_id}."
        )


class InstallationNotBoundToGuild(ValueError):
    def __init__(self, guild_id: int, installation_id: int | None) -> None:
        self.guild_id = guild_id
        self.installation_id = installation_id
        super().__init__(
            f"GitHub App installation {installation_id} is not bound to Discord guild {guild_id}."
        )


@dataclass(frozen=True)
class PrMessage:
    guild_id: int
    owner: str
    repo: str
    pr_number: int
    channel_id: int
    message_id: int
    state: str
    requested_reviewers: tuple[str, ...]
    requested_teams: tuple[str, ...]


@dataclass(frozen=True)
class PrReviewSettings:
    guild_id: int
    review_mode: str
    discord_role_id: int | None


@dataclass(frozen=True)
class IssueSettings:
    guild_id: int
    default_owner: str | None
    default_repo: str | None
    suggestion_label: str
    bug_label: str
    allowed_labels: tuple[str, ...]
    default_labels: tuple[str, ...]
    submission_log_channel_id: int | None
    enabled: bool


@dataclass(frozen=True)
class IssueSubmission:
    id: int
    guild_id: int
    channel_id: int
    user_id: int
    owner: str
    repo: str
    issue_number: int
    issue_url: str
    issue_type: str
    title: str
    created_at: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def init(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS guilds (
                    guild_id INTEGER PRIMARY KEY,
                    guild_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS linked_repositories (
                    guild_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    webhook_secret TEXT NOT NULL,
                    installation_id INTEGER,
                    repository_full_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, owner, repo),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_linked_repositories_repo
                    ON linked_repositories(owner, repo);

                CREATE INDEX IF NOT EXISTS idx_linked_repositories_installation
                    ON linked_repositories(installation_id, owner, repo);

                CREATE TABLE IF NOT EXISTS installed_repositories (
                    installation_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    repository_full_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (installation_id, owner, repo)
                );

                CREATE INDEX IF NOT EXISTS idx_installed_repositories_repo
                    ON installed_repositories(owner, repo);

                CREATE TABLE IF NOT EXISTS guild_installations (
                    guild_id INTEGER NOT NULL,
                    installation_id INTEGER NOT NULL,
                    account_login TEXT,
                    account_type TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, installation_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_guild_installations_installation
                    ON guild_installations(installation_id, guild_id);

                CREATE TABLE IF NOT EXISTS log_channels (
                    guild_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, event_type),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pr_review_channels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT,
                    github_event TEXT NOT NULL,
                    action TEXT,
                    repository_owner TEXT,
                    repository_name TEXT,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_webhook_events_delivery
                    ON webhook_events(delivery_id);

                CREATE TABLE IF NOT EXISTS pr_messages (
                    guild_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    requested_reviewers TEXT NOT NULL DEFAULT '[]',
                    requested_teams TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, owner, repo, pr_number),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pr_review_settings (
                    guild_id INTEGER PRIMARY KEY,
                    review_mode TEXT NOT NULL DEFAULT 'anyone'
                        CHECK (review_mode IN ('anyone', 'github_reviewers', 'discord_role')),
                    discord_role_id INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS issue_settings (
                    guild_id INTEGER PRIMARY KEY,
                    default_owner TEXT,
                    default_repo TEXT,
                    suggestion_label TEXT NOT NULL DEFAULT 'suggestion',
                    bug_label TEXT NOT NULL DEFAULT 'bug',
                    allowed_labels TEXT NOT NULL DEFAULT '[]',
                    default_labels TEXT NOT NULL DEFAULT '[]',
                    submission_log_channel_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS issue_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_url TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_issue_submissions_guild
                    ON issue_submissions(guild_id, created_at);

                CREATE TABLE IF NOT EXISTS issue_role_rules (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    rule_type TEXT NOT NULL CHECK (rule_type IN ('allow', 'deny')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, role_id, rule_type),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS issue_blocked_roles (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, role_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_webhook_secret_column()
            self._ensure_installation_columns()
            self._ensure_guild_installations_table()
            self._ensure_pr_message_review_columns()
            self._ensure_issue_settings_label_columns()
            self._ensure_issue_blocked_roles_table()

    def upsert_guild(self, guild_id: int, guild_name: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO guilds (guild_id, guild_name)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, guild_name),
            )

    def link_repository(
        self,
        guild_id: int,
        owner: str,
        repo: str,
        installation_id: int | None = None,
        repository_full_name: str | None = None,
    ) -> LinkedRepository:
        owner, repo = _normalize_repo(owner, repo)
        if installation_id is not None:
            self.assert_installation_bound_to_guild(guild_id, installation_id)
        if repository_full_name is None:
            repository_full_name = f"{owner}/{repo}"
        webhook_secret = _generate_webhook_secret()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO linked_repositories (
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild_id, owner, repo, webhook_secret, installation_id, repository_full_name),
            )
            if installation_id is not None:
                self._connection.execute(
                    """
                    UPDATE linked_repositories
                    SET
                        installation_id = ?,
                        repository_full_name = ?
                    WHERE guild_id = ? AND owner = ? AND repo = ?
                    """,
                    (installation_id, repository_full_name, guild_id, owner, repo),
                )
            row = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                FROM linked_repositories
                WHERE guild_id = ? AND owner = ? AND repo = ?
                """,
                (guild_id, owner, repo),
            ).fetchone()

            if row and not row["webhook_secret"]:
                webhook_secret = _generate_webhook_secret()
                self._connection.execute(
                    """
                    UPDATE linked_repositories
                    SET webhook_secret = ?
                    WHERE guild_id = ? AND owner = ? AND repo = ?
                    """,
                    (webhook_secret, guild_id, owner, repo),
                )
                return LinkedRepository(
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name,
                )

        if row is None:
            raise RuntimeError("Failed to link repository")

        return _linked_repository_from_row(row)

    def get_linked_repository(
        self,
        guild_id: int,
        owner: str,
        repo: str,
    ) -> LinkedRepository | None:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                FROM linked_repositories
                WHERE guild_id = ? AND owner = ? AND repo = ?
                """,
                (guild_id, owner, repo),
            ).fetchone()

        return _linked_repository_from_row(row) if row else None

    def get_guild_linked_repository(
        self,
        guild_id: int,
        repo_full_name: str,
    ) -> LinkedRepository | None:
        owner, repo = _normalize_repo_full_name(repo_full_name)
        if owner is None or repo is None:
            return None
        return self.get_linked_repository(guild_id, owner, repo)

    def assert_repo_linked_to_guild(
        self,
        guild_id: int,
        repo_full_name: str,
    ) -> LinkedRepository:
        linked_repo = self.get_guild_linked_repository(guild_id, repo_full_name)
        if linked_repo is None:
            raise RepositoryNotLinkedToGuild(guild_id, repo_full_name)
        if linked_repo.installation_id is None:
            raise InstallationNotBoundToGuild(guild_id, None)
        self.assert_installation_bound_to_guild(guild_id, linked_repo.installation_id)
        return linked_repo

    def list_linked_repositories_for_guild(self, guild_id: int) -> list[LinkedRepository]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                FROM linked_repositories
                WHERE guild_id = ?
                ORDER BY owner, repo
                """,
                (guild_id,),
            ).fetchall()
        return [_linked_repository_from_row(row) for row in rows]

    def add_guild_installation(
        self,
        guild_id: int,
        installation_id: int,
        account_login: str | None = None,
        account_type: str | None = None,
    ) -> GuildInstallation:
        account_login = (
            account_login.strip().lower()
            if account_login and account_login.strip()
            else None
        )
        account_type = (
            account_type.strip().lower()
            if account_type and account_type.strip()
            else None
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)",
                (guild_id,),
            )
            self._connection.execute(
                """
                INSERT INTO guild_installations (
                    guild_id,
                    installation_id,
                    account_login,
                    account_type
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, installation_id) DO UPDATE SET
                    account_login = excluded.account_login,
                    account_type = excluded.account_type
                """,
                (guild_id, installation_id, account_login, account_type),
            )
            row = self._connection.execute(
                """
                SELECT guild_id, installation_id, account_login, account_type, created_at
                FROM guild_installations
                WHERE guild_id = ? AND installation_id = ?
                """,
                (guild_id, installation_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to bind GitHub installation to guild")
        return _guild_installation_from_row(row)

    def remove_guild_installation(self, guild_id: int, installation_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                DELETE FROM guild_installations
                WHERE guild_id = ? AND installation_id = ?
                """,
                (guild_id, installation_id),
            )

    def get_guild_installations(self, guild_id: int) -> list[GuildInstallation]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT guild_id, installation_id, account_login, account_type, created_at
                FROM guild_installations
                WHERE guild_id = ?
                ORDER BY installation_id
                """,
                (guild_id,),
            ).fetchall()
        return [_guild_installation_from_row(row) for row in rows]

    def get_guilds_for_installation(self, installation_id: int) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT guild_id
                FROM guild_installations
                WHERE installation_id = ?
                ORDER BY guild_id
                """,
                (installation_id,),
            ).fetchall()
        return [int(row["guild_id"]) for row in rows]

    def is_installation_bound_to_guild(self, guild_id: int, installation_id: int) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM guild_installations
                WHERE guild_id = ? AND installation_id = ?
                """,
                (guild_id, installation_id),
            ).fetchone()
        return row is not None

    def assert_installation_bound_to_guild(
        self,
        guild_id: int,
        installation_id: int | None,
    ) -> None:
        if installation_id is None or not self.is_installation_bound_to_guild(
            guild_id,
            installation_id,
        ):
            raise InstallationNotBoundToGuild(guild_id, installation_id)

    def rotate_webhook_secret(
        self,
        guild_id: int,
        owner: str,
        repo: str,
    ) -> LinkedRepository | None:
        owner, repo = _normalize_repo(owner, repo)
        webhook_secret = _generate_webhook_secret()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE linked_repositories
                SET webhook_secret = ?
                WHERE guild_id = ? AND owner = ? AND repo = ?
                """,
                (webhook_secret, guild_id, owner, repo),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                FROM linked_repositories
                WHERE guild_id = ? AND owner = ? AND repo = ?
                """,
                (guild_id, owner, repo),
            ).fetchone()

        return _linked_repository_from_row(row) if row else None

    def unlink_repositories(self, guild_id: int) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM linked_repositories WHERE guild_id = ?",
                (guild_id,),
            )
            return cursor.rowcount

    def set_log_channel(self, guild_id: int, event_type: str, channel_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)",
                (guild_id,),
            )
            self._connection.execute(
                """
                INSERT INTO log_channels (guild_id, event_type, channel_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, event_type) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, event_type, channel_id),
            )

    def get_log_channel(self, guild_id: int, event_type: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT channel_id
                FROM log_channels
                WHERE guild_id = ? AND event_type = ?
                """,
                (guild_id, event_type),
            ).fetchone()
        return int(row["channel_id"]) if row else None

    def clear_log_channel(self, guild_id: int, event_type: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM log_channels WHERE guild_id = ? AND event_type = ?",
                (guild_id, event_type),
            )

    def set_pr_review_channel(self, guild_id: int, channel_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)",
                (guild_id,),
            )
            self._connection.execute(
                """
                INSERT INTO pr_review_channels (guild_id, channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, channel_id),
            )

    def get_pr_review_channel(self, guild_id: int) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT channel_id FROM pr_review_channels WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return int(row["channel_id"]) if row else None

    def clear_pr_review_channel(self, guild_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM pr_review_channels WHERE guild_id = ?",
                (guild_id,),
            )

    def find_guilds_for_repository(self, owner: str, repo: str) -> list[int]:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT guild_id
                FROM linked_repositories
                WHERE owner = ? AND repo = ?
                """,
                (owner, repo),
            ).fetchall()
        return [int(row["guild_id"]) for row in rows]

    def find_linked_repositories(self, owner: str, repo: str) -> list[LinkedRepository]:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                FROM linked_repositories
                WHERE owner = ? AND repo = ?
                """,
                (owner, repo),
            ).fetchall()
        return [_linked_repository_from_row(row) for row in rows]

    def find_linked_repositories_for_installation(
        self,
        installation_id: int,
        owner: str,
        repo: str,
    ) -> list[LinkedRepository]:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    linked_repositories.guild_id,
                    linked_repositories.owner,
                    linked_repositories.repo,
                    linked_repositories.webhook_secret,
                    linked_repositories.installation_id,
                    linked_repositories.repository_full_name
                FROM linked_repositories
                INNER JOIN guild_installations
                    ON guild_installations.guild_id = linked_repositories.guild_id
                    AND guild_installations.installation_id = linked_repositories.installation_id
                WHERE linked_repositories.owner = ? AND linked_repositories.repo = ?
                    AND linked_repositories.installation_id = ?
                ORDER BY linked_repositories.guild_id
                """,
                (owner, repo, installation_id),
            ).fetchall()
        return [_linked_repository_from_row(row) for row in rows]

    def upsert_installed_repository(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        repository_full_name: str | None = None,
    ) -> InstalledRepository:
        owner, repo = _normalize_repo(owner, repo)
        repository_full_name = (repository_full_name or f"{owner}/{repo}").strip().lower()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO installed_repositories (
                    installation_id,
                    owner,
                    repo,
                    repository_full_name
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(installation_id, owner, repo) DO UPDATE SET
                    repository_full_name = excluded.repository_full_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (installation_id, owner, repo, repository_full_name),
            )
            self._connection.execute(
                """
                UPDATE linked_repositories
                SET
                    installation_id = ?,
                    repository_full_name = ?
                WHERE owner = ? AND repo = ?
                    AND installation_id = ?
                    AND EXISTS (
                        SELECT 1
                        FROM guild_installations
                        WHERE guild_installations.guild_id = linked_repositories.guild_id
                            AND guild_installations.installation_id = ?
                    )
                """,
                (
                    installation_id,
                    repository_full_name,
                    owner,
                    repo,
                    installation_id,
                    installation_id,
                ),
            )
        return InstalledRepository(installation_id, owner, repo, repository_full_name)

    def list_installed_repositories(self) -> list[InstalledRepository]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT installation_id, owner, repo, repository_full_name
                FROM installed_repositories
                ORDER BY owner, repo
                """
            ).fetchall()
        return [_installed_repository_from_row(row) for row in rows]

    def list_installed_repositories_for_guild(self, guild_id: int) -> list[InstalledRepository]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    installed_repositories.installation_id,
                    installed_repositories.owner,
                    installed_repositories.repo,
                    installed_repositories.repository_full_name
                FROM installed_repositories
                INNER JOIN guild_installations
                    ON guild_installations.installation_id =
                        installed_repositories.installation_id
                WHERE guild_installations.guild_id = ?
                ORDER BY installed_repositories.owner, installed_repositories.repo
                """,
                (guild_id,),
            ).fetchall()
        return [_installed_repository_from_row(row) for row in rows]

    def get_status(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            repos = self._connection.execute(
                """
                SELECT owner, repo
                FROM linked_repositories
                WHERE guild_id = ?
                ORDER BY owner, repo
                """,
                (guild_id,),
            ).fetchall()
            log_channels = self._connection.execute(
                """
                SELECT event_type, channel_id
                FROM log_channels
                WHERE guild_id = ?
                ORDER BY event_type
                """,
                (guild_id,),
            ).fetchall()
            pr_channel = self._connection.execute(
                "SELECT channel_id FROM pr_review_channels WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            pr_settings = self._connection.execute(
                """
                SELECT review_mode, discord_role_id
                FROM pr_review_settings
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()

        return {
            "repositories": [(row["owner"], row["repo"]) for row in repos],
            "log_channels": {row["event_type"]: int(row["channel_id"]) for row in log_channels},
            "pr_review_channel": int(pr_channel["channel_id"]) if pr_channel else None,
            "pr_review_mode": pr_settings["review_mode"] if pr_settings else REVIEW_MODE_ANYONE,
            "pr_review_role_id": int(pr_settings["discord_role_id"])
            if pr_settings and pr_settings["discord_role_id"] is not None
            else None,
        }

    def record_webhook_event(
        self,
        delivery_id: str | None,
        github_event: str,
        action: str | None,
        owner: str | None,
        repo: str | None,
        payload: dict[str, Any],
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO webhook_events (
                    delivery_id,
                    github_event,
                    action,
                    repository_owner,
                    repository_name,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    github_event,
                    action,
                    owner.lower() if owner else None,
                    repo.lower() if repo else None,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                ),
            )

    def get_pr_message(
        self,
        guild_id: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> PrMessage | None:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    pr_number,
                    channel_id,
                    message_id,
                    state,
                    requested_reviewers,
                    requested_teams
                FROM pr_messages
                WHERE guild_id = ? AND owner = ? AND repo = ? AND pr_number = ?
                """,
                (guild_id, owner, repo, pr_number),
            ).fetchone()

        return _pr_message_from_row(row) if row else None

    def get_pr_message_by_discord_message(
        self,
        guild_id: int,
        message_id: int,
    ) -> PrMessage | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    guild_id,
                    owner,
                    repo,
                    pr_number,
                    channel_id,
                    message_id,
                    state,
                    requested_reviewers,
                    requested_teams
                FROM pr_messages
                WHERE guild_id = ? AND message_id = ?
                """,
                (guild_id, message_id),
            ).fetchone()

        return _pr_message_from_row(row) if row else None

    def upsert_pr_message(
        self,
        guild_id: int,
        owner: str,
        repo: str,
        pr_number: int,
        channel_id: int,
        message_id: int,
        state: str,
        requested_reviewers: list[str] | tuple[str, ...] | None = None,
        requested_teams: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        owner, repo = _normalize_repo(owner, repo)
        reviewers_json = json.dumps(sorted(_normalize_names(requested_reviewers or [])))
        teams_json = json.dumps(sorted(_normalize_names(requested_teams or [])))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO pr_messages (
                    guild_id,
                    owner,
                    repo,
                    pr_number,
                    channel_id,
                    message_id,
                    state,
                    requested_reviewers,
                    requested_teams
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, owner, repo, pr_number) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    state = excluded.state,
                    requested_reviewers = excluded.requested_reviewers,
                    requested_teams = excluded.requested_teams,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    owner,
                    repo,
                    pr_number,
                    channel_id,
                    message_id,
                    state,
                    reviewers_json,
                    teams_json,
                ),
            )

    def set_pr_review_settings(
        self,
        guild_id: int,
        review_mode: str,
        discord_role_id: int | None = None,
    ) -> PrReviewSettings:
        if review_mode not in REVIEW_MODES:
            raise ValueError(f"Unsupported PR review mode: {review_mode}")
        if review_mode != REVIEW_MODE_DISCORD_ROLE:
            discord_role_id = None

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO pr_review_settings (guild_id, review_mode, discord_role_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    review_mode = excluded.review_mode,
                    discord_role_id = excluded.discord_role_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, review_mode, discord_role_id),
            )

        return self.get_pr_review_settings(guild_id)

    def get_pr_review_settings(self, guild_id: int) -> PrReviewSettings:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT guild_id, review_mode, discord_role_id
                FROM pr_review_settings
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()

        if row:
            return _pr_review_settings_from_row(row)
        return PrReviewSettings(guild_id, REVIEW_MODE_ANYONE, None)

    def set_issue_settings(
        self,
        guild_id: int,
        default_owner: str,
        default_repo: str,
        suggestion_label: str = "suggestion",
        bug_label: str = "bug",
        allowed_labels: list[str] | tuple[str, ...] | None = None,
        default_labels: list[str] | tuple[str, ...] | None = None,
        submission_log_channel_id: int | None = None,
    ) -> IssueSettings:
        default_owner, default_repo = _normalize_repo(default_owner, default_repo)
        self.assert_repo_linked_to_guild(guild_id, f"{default_owner}/{default_repo}")
        suggestion_label = _normalize_label(suggestion_label, "suggestion")
        bug_label = _normalize_label(bug_label, "bug")
        allowed_labels_json = json.dumps(_normalize_label_list(allowed_labels or []))
        default_labels_json = json.dumps(_normalize_label_list(default_labels or []))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO issue_settings (
                    guild_id,
                    default_owner,
                    default_repo,
                    suggestion_label,
                    bug_label,
                    allowed_labels,
                    default_labels,
                    submission_log_channel_id,
                    enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    default_owner = excluded.default_owner,
                    default_repo = excluded.default_repo,
                    suggestion_label = excluded.suggestion_label,
                    bug_label = excluded.bug_label,
                    allowed_labels = excluded.allowed_labels,
                    default_labels = excluded.default_labels,
                    submission_log_channel_id = excluded.submission_log_channel_id,
                    enabled = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    default_owner,
                    default_repo,
                    suggestion_label,
                    bug_label,
                    allowed_labels_json,
                    default_labels_json,
                    submission_log_channel_id,
                ),
            )

        settings = self.get_issue_settings(guild_id)
        if settings is None:
            raise RuntimeError("Failed to save issue settings")
        return settings

    def get_issue_settings(self, guild_id: int) -> IssueSettings | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    guild_id,
                    default_owner,
                    default_repo,
                    suggestion_label,
                    bug_label,
                    allowed_labels,
                    default_labels,
                    submission_log_channel_id,
                    enabled
                FROM issue_settings
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        return _issue_settings_from_row(row) if row else None

    def disable_issue_creation(self, guild_id: int) -> IssueSettings:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO issue_settings (
                    guild_id,
                    suggestion_label,
                    bug_label,
                    enabled
                )
                VALUES (?, 'suggestion', 'bug', 0)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id,),
            )

        settings = self.get_issue_settings(guild_id)
        if settings is None:
            raise RuntimeError("Failed to disable issue creation")
        return settings

    def set_issue_creation_enabled(self, guild_id: int, enabled: bool) -> IssueSettings:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO issue_settings (
                    guild_id,
                    suggestion_label,
                    bug_label,
                    enabled
                )
                VALUES (?, 'suggestion', 'bug', ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, 1 if enabled else 0),
            )

        settings = self.get_issue_settings(guild_id)
        if settings is None:
            raise RuntimeError("Failed to update issue creation status")
        return settings

    def set_issue_default_repository(
        self,
        guild_id: int,
        default_owner: str,
        default_repo: str,
    ) -> IssueSettings:
        default_owner, default_repo = _normalize_repo(default_owner, default_repo)
        current = self.get_issue_settings(guild_id)
        return self.set_issue_settings(
            guild_id,
            default_owner,
            default_repo,
            suggestion_label=current.suggestion_label if current else "suggestion",
            bug_label=current.bug_label if current else "bug",
            allowed_labels=current.allowed_labels if current else (),
            default_labels=current.default_labels if current else (),
            submission_log_channel_id=current.submission_log_channel_id if current else None,
        )

    def set_issue_submission_log_channel(
        self,
        guild_id: int,
        channel_id: int | None,
    ) -> IssueSettings:
        current = self.get_issue_settings(guild_id)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)",
                (guild_id,),
            )
            self._connection.execute(
                """
                INSERT INTO issue_settings (
                    guild_id,
                    suggestion_label,
                    bug_label,
                    allowed_labels,
                    default_labels,
                    submission_log_channel_id,
                    enabled
                )
                VALUES (?, 'suggestion', 'bug', '[]', '[]', ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    submission_log_channel_id = excluded.submission_log_channel_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, channel_id),
            )

        settings = self.get_issue_settings(guild_id)
        if settings is None:
            raise RuntimeError("Failed to update issue submission log channel")
        if current is None:
            return settings
        return settings

    def get_issue_blocked_roles(self, guild_id: int) -> tuple[int, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT role_id
                FROM issue_blocked_roles
                WHERE guild_id = ?
                ORDER BY role_id
                """,
                (guild_id,),
            ).fetchall()
        return tuple(int(row["role_id"]) for row in rows)

    def add_issue_blocked_role(self, guild_id: int, role_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)",
                (guild_id,),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO issue_blocked_roles (guild_id, role_id)
                VALUES (?, ?)
                """,
                (guild_id, role_id),
            )

    def remove_issue_blocked_role(self, guild_id: int, role_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                DELETE FROM issue_blocked_roles
                WHERE guild_id = ? AND role_id = ?
                """,
                (guild_id, role_id),
            )

    def clear_issue_blocked_roles(self, guild_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM issue_blocked_roles WHERE guild_id = ?",
                (guild_id,),
            )

    def record_issue_submission(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        owner: str,
        repo: str,
        issue_number: int,
        issue_url: str,
        issue_type: str,
        title: str,
    ) -> IssueSubmission:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT
                    id,
                    guild_id,
                    channel_id,
                    user_id,
                    owner,
                    repo,
                    issue_number,
                    issue_url,
                    issue_type,
                    title,
                    created_at
                FROM issue_submissions
                WHERE guild_id = ? AND owner = ? AND repo = ? AND issue_number = ?
                """,
                (guild_id, owner, repo, issue_number),
            ).fetchone()
            if row is not None:
                return _issue_submission_from_row(row)

            cursor = self._connection.execute(
                """
                INSERT INTO issue_submissions (
                    guild_id,
                    channel_id,
                    user_id,
                    owner,
                    repo,
                    issue_number,
                    issue_url,
                    issue_type,
                    title
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    user_id,
                    owner,
                    repo,
                    issue_number,
                    issue_url,
                    issue_type,
                    title,
                ),
            )
            submission_id = cursor.lastrowid
            row = self._connection.execute(
                """
                SELECT
                    id,
                    guild_id,
                    channel_id,
                    user_id,
                    owner,
                    repo,
                    issue_number,
                    issue_url,
                    issue_type,
                    title,
                    created_at
                FROM issue_submissions
                WHERE id = ?
                """,
                (submission_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to record issue submission")
        return _issue_submission_from_row(row)

    def has_issue_submission(
        self,
        guild_id: int,
        owner: str,
        repo: str,
        issue_number: int | None = None,
        issue_url: str | None = None,
    ) -> bool:
        owner, repo = _normalize_repo(owner, repo)
        with self._lock:
            if issue_number is not None:
                row = self._connection.execute(
                    """
                    SELECT 1
                    FROM issue_submissions
                    WHERE guild_id = ? AND owner = ? AND repo = ? AND issue_number = ?
                    """,
                    (guild_id, owner, repo, issue_number),
                ).fetchone()
                if row:
                    return True
            if issue_url:
                row = self._connection.execute(
                    """
                    SELECT 1
                    FROM issue_submissions
                    WHERE guild_id = ? AND owner = ? AND repo = ? AND issue_url = ?
                    """,
                    (guild_id, owner, repo, issue_url),
                ).fetchone()
                return row is not None
        return False

    def _ensure_webhook_secret_column(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(linked_repositories)").fetchall()
        }
        if "webhook_secret" not in columns:
            self._connection.execute("ALTER TABLE linked_repositories ADD COLUMN webhook_secret TEXT")

        rows = self._connection.execute(
            """
            SELECT guild_id, owner, repo
            FROM linked_repositories
            WHERE webhook_secret IS NULL OR webhook_secret = ''
            """
        ).fetchall()
        for row in rows:
            self._connection.execute(
                """
                UPDATE linked_repositories
                SET webhook_secret = ?
                WHERE guild_id = ? AND owner = ? AND repo = ?
                """,
                (
                    _generate_webhook_secret(),
                    int(row["guild_id"]),
                    row["owner"],
                    row["repo"],
                ),
            )

    def _ensure_installation_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(linked_repositories)").fetchall()
        }
        if "installation_id" not in columns:
            self._connection.execute("ALTER TABLE linked_repositories ADD COLUMN installation_id INTEGER")
        if "repository_full_name" not in columns:
            self._connection.execute("ALTER TABLE linked_repositories ADD COLUMN repository_full_name TEXT")

        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS installed_repositories (
                installation_id INTEGER NOT NULL,
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                repository_full_name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (installation_id, owner, repo)
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_installed_repositories_repo
                ON installed_repositories(owner, repo)
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_linked_repositories_installation
                ON linked_repositories(installation_id, owner, repo)
            """
        )

    def _ensure_guild_installations_table(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_installations (
                guild_id INTEGER NOT NULL,
                installation_id INTEGER NOT NULL,
                account_login TEXT,
                account_type TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, installation_id),
                FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_guild_installations_installation
                ON guild_installations(installation_id, guild_id)
            """
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO guild_installations (guild_id, installation_id)
            SELECT DISTINCT guild_id, installation_id
            FROM linked_repositories
            WHERE installation_id IS NOT NULL
            """
        )

    def _ensure_pr_message_review_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(pr_messages)").fetchall()
        }
        if "requested_reviewers" not in columns:
            self._connection.execute(
                "ALTER TABLE pr_messages ADD COLUMN requested_reviewers TEXT NOT NULL DEFAULT '[]'"
            )
        if "requested_teams" not in columns:
            self._connection.execute(
                "ALTER TABLE pr_messages ADD COLUMN requested_teams TEXT NOT NULL DEFAULT '[]'"
            )

    def _ensure_issue_settings_label_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(issue_settings)").fetchall()
        }
        if "allowed_labels" not in columns:
            self._connection.execute(
                "ALTER TABLE issue_settings ADD COLUMN allowed_labels TEXT NOT NULL DEFAULT '[]'"
            )
        if "default_labels" not in columns:
            self._connection.execute(
                "ALTER TABLE issue_settings ADD COLUMN default_labels TEXT NOT NULL DEFAULT '[]'"
            )

    def _ensure_issue_blocked_roles_table(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_blocked_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id),
                FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
            )
            """
        )


def _normalize_repo(owner: str, repo: str) -> tuple[str, str]:
    return owner.strip().lower(), repo.strip().lower()


def _normalize_repo_full_name(repo_full_name: str) -> tuple[str | None, str | None]:
    if "/" not in repo_full_name:
        return None, None
    owner, repo = repo_full_name.split("/", 1)
    owner, repo = _normalize_repo(owner, repo)
    if not owner or not repo:
        return None, None
    return owner, repo


def _normalize_label(value: str | None, fallback: str) -> str:
    if not value or not value.strip():
        return fallback
    return value.strip()


def _normalize_label_list(values: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = value.strip()
        key = label.lower()
        if label and key not in seen:
            normalized.append(label)
            seen.add(key)
    return normalized


def _normalize_names(values: list[str] | tuple[str, ...]) -> set[str]:
    return {value.strip().lower() for value in values if value.strip()}


def _generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def _linked_repository_from_row(row: sqlite3.Row) -> LinkedRepository:
    installation_id = row["installation_id"]
    return LinkedRepository(
        guild_id=int(row["guild_id"]),
        owner=row["owner"],
        repo=row["repo"],
        webhook_secret=row["webhook_secret"],
        installation_id=int(installation_id) if installation_id is not None else None,
        repository_full_name=row["repository_full_name"],
    )


def _installed_repository_from_row(row: sqlite3.Row) -> InstalledRepository:
    return InstalledRepository(
        installation_id=int(row["installation_id"]),
        owner=row["owner"],
        repo=row["repo"],
        repository_full_name=row["repository_full_name"],
    )


def _guild_installation_from_row(row: sqlite3.Row) -> GuildInstallation:
    return GuildInstallation(
        guild_id=int(row["guild_id"]),
        installation_id=int(row["installation_id"]),
        account_login=row["account_login"],
        account_type=row["account_type"],
        created_at=row["created_at"],
    )


def _pr_message_from_row(row: sqlite3.Row) -> PrMessage:
    return PrMessage(
        guild_id=int(row["guild_id"]),
        owner=row["owner"],
        repo=row["repo"],
        pr_number=int(row["pr_number"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]),
        state=row["state"],
        requested_reviewers=_json_string_tuple(row["requested_reviewers"]),
        requested_teams=_json_string_tuple(row["requested_teams"]),
    )


def _pr_review_settings_from_row(row: sqlite3.Row) -> PrReviewSettings:
    role_id = row["discord_role_id"]
    return PrReviewSettings(
        guild_id=int(row["guild_id"]),
        review_mode=row["review_mode"],
        discord_role_id=int(role_id) if role_id is not None else None,
    )


def _issue_settings_from_row(row: sqlite3.Row) -> IssueSettings:
    log_channel_id = row["submission_log_channel_id"]
    return IssueSettings(
        guild_id=int(row["guild_id"]),
        default_owner=row["default_owner"],
        default_repo=row["default_repo"],
        suggestion_label=row["suggestion_label"],
        bug_label=row["bug_label"],
        allowed_labels=_json_string_tuple(row["allowed_labels"]),
        default_labels=_json_string_tuple(row["default_labels"]),
        submission_log_channel_id=int(log_channel_id) if log_channel_id is not None else None,
        enabled=bool(row["enabled"]),
    )


def _issue_submission_from_row(row: sqlite3.Row) -> IssueSubmission:
    return IssueSubmission(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        channel_id=int(row["channel_id"]),
        user_id=int(row["user_id"]),
        owner=row["owner"],
        repo=row["repo"],
        issue_number=int(row["issue_number"]),
        issue_url=row["issue_url"],
        issue_type=row["issue_type"],
        title=row["title"],
        created_at=row["created_at"],
    )


def _json_string_tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring malformed JSON list in PR message row")
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(item for item in data if isinstance(item, str))
