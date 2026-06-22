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


@dataclass(frozen=True)
class LinkedRepository:
    guild_id: int
    owner: str
    repo: str
    webhook_secret: str


@dataclass(frozen=True)
class PrMessage:
    guild_id: int
    owner: str
    repo: str
    pr_number: int
    channel_id: int
    message_id: int
    state: str


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
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, owner, repo),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_linked_repositories_repo
                    ON linked_repositories(owner, repo);

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
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, owner, repo, pr_number),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_webhook_secret_column()

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

    def link_repository(self, guild_id: int, owner: str, repo: str) -> LinkedRepository:
        owner, repo = _normalize_repo(owner, repo)
        webhook_secret = _generate_webhook_secret()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO linked_repositories (guild_id, owner, repo, webhook_secret)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, owner, repo, webhook_secret),
            )
            row = self._connection.execute(
                """
                SELECT guild_id, owner, repo, webhook_secret
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
                return LinkedRepository(guild_id, owner, repo, webhook_secret)

        if row is None:
            raise RuntimeError("Failed to link repository")

        return LinkedRepository(
            guild_id=int(row["guild_id"]),
            owner=row["owner"],
            repo=row["repo"],
            webhook_secret=row["webhook_secret"],
        )

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
                SELECT guild_id, owner, repo, webhook_secret
                FROM linked_repositories
                WHERE guild_id = ? AND owner = ? AND repo = ?
                """,
                (guild_id, owner, repo),
            ).fetchone()

        return _linked_repository_from_row(row) if row else None

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

        return LinkedRepository(guild_id, owner, repo, webhook_secret)

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

    def set_pr_review_channel(self, guild_id: int, channel_id: int) -> None:
        with self._lock, self._connection:
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
                SELECT guild_id, owner, repo, webhook_secret
                FROM linked_repositories
                WHERE owner = ? AND repo = ?
                """,
                (owner, repo),
            ).fetchall()
        return [_linked_repository_from_row(row) for row in rows]

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

        return {
            "repositories": [(row["owner"], row["repo"]) for row in repos],
            "log_channels": {row["event_type"]: int(row["channel_id"]) for row in log_channels},
            "pr_review_channel": int(pr_channel["channel_id"]) if pr_channel else None,
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
                SELECT guild_id, owner, repo, pr_number, channel_id, message_id, state
                FROM pr_messages
                WHERE guild_id = ? AND owner = ? AND repo = ? AND pr_number = ?
                """,
                (guild_id, owner, repo, pr_number),
            ).fetchone()

        if not row:
            return None

        return PrMessage(
            guild_id=int(row["guild_id"]),
            owner=row["owner"],
            repo=row["repo"],
            pr_number=int(row["pr_number"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]),
            state=row["state"],
        )

    def upsert_pr_message(
        self,
        guild_id: int,
        owner: str,
        repo: str,
        pr_number: int,
        channel_id: int,
        message_id: int,
        state: str,
    ) -> None:
        owner, repo = _normalize_repo(owner, repo)
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
                    state
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, owner, repo, pr_number) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, owner, repo, pr_number, channel_id, message_id, state),
            )

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


def _normalize_repo(owner: str, repo: str) -> tuple[str, str]:
    return owner.strip().lower(), repo.strip().lower()


def _generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def _linked_repository_from_row(row: sqlite3.Row) -> LinkedRepository:
    return LinkedRepository(
        guild_id=int(row["guild_id"]),
        owner=row["owner"],
        repo=row["repo"],
        webhook_secret=row["webhook_secret"],
    )
