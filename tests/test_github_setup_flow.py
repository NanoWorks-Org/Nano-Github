from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from nano_github.database import Database, _format_datetime, _parse_datetime, _utc_now
from nano_github.github_client import GitHubInstallation, GitHubRepository
from nano_github.github_setup import (
    SETUP_INVALID_TOKEN_MESSAGE,
    complete_github_installation_setup,
)
from nano_github.repository_access import link_installed_repository_to_guild


GUILD_ID = 123456789
USER_ID = 987654321
INSTALLATION_ID = 142201252
REPO_FULL_NAME = "duckquack001/testing-repo"
HAS_DISCORD = find_spec("discord") is not None


class GitHubSetupFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "nano-github.sqlite3")
        self.db.init()
        self.db.upsert_guild(GUILD_ID, "Setup Guild")

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_create_setup_token_stores_guild_and_user(self) -> None:
        token = self.db.create_github_setup_token(GUILD_ID, USER_ID)
        setup_token = self.db.get_valid_github_setup_token(token)

        self.assertIsNotNone(setup_token)
        self.assertEqual(setup_token.guild_id, GUILD_ID)  # type: ignore[union-attr]
        self.assertEqual(setup_token.user_id, USER_ID)  # type: ignore[union-attr]
        self.assertTrue(token.startswith("NW-"))

    def test_setup_token_expires_after_ten_minutes(self) -> None:
        token = self.db.create_github_setup_token(GUILD_ID, USER_ID)
        setup_token = self.db.get_valid_github_setup_token(token)

        self.assertIsNotNone(setup_token)
        created_at = _parse_datetime(setup_token.created_at)  # type: ignore[union-attr]
        expires_at = _parse_datetime(setup_token.expires_at)  # type: ignore[union-attr]
        self.assertEqual(expires_at - created_at, timedelta(minutes=10))

    def test_expired_token_cannot_be_used(self) -> None:
        token = self.db.create_github_setup_token(GUILD_ID, USER_ID)
        with self.db._lock, self.db._connection:
            self.db._connection.execute(
                """
                UPDATE github_setup_tokens
                SET expires_at = ?
                WHERE token = ?
                """,
                (_format_datetime(_utc_now() - timedelta(seconds=1)), token),
            )

        self.assertIsNone(self.db.get_valid_github_setup_token(token))

    def test_used_token_cannot_be_reused(self) -> None:
        token = self.db.create_github_setup_token(GUILD_ID, USER_ID)

        self.assertTrue(self.db.mark_github_setup_token_used(token))
        self.assertIsNone(self.db.get_valid_github_setup_token(token))
        self.assertFalse(self.db.mark_github_setup_token_used(token))

    def test_valid_callback_stores_guild_installation_and_repositories(self) -> None:
        token = self.db.create_github_setup_token(GUILD_ID, USER_ID)

        with _mock_github_installation():
            success, message = complete_github_installation_setup(
                self.db,
                token,
                INSTALLATION_ID,
                "install",
            )

        self.assertTrue(success)
        self.assertIn("Nano GitHub is now connected", message)
        installations = self.db.get_guild_installations(GUILD_ID)
        self.assertEqual([item.installation_id for item in installations], [INSTALLATION_ID])
        self.assertEqual(installations[0].account_login, "duckquack001")
        repos = self.db.list_installed_repositories_for_guild(GUILD_ID)
        self.assertEqual([repo.repository_full_name for repo in repos], [REPO_FULL_NAME])
        self.assertIsNone(self.db.get_valid_github_setup_token(token))

    def test_invalid_callback_does_not_store_guild_installation(self) -> None:
        success, message = complete_github_installation_setup(
            self.db,
            "NW-INVALID",
            INSTALLATION_ID,
        )

        self.assertFalse(success)
        self.assertEqual(message, SETUP_INVALID_TOKEN_MESSAGE)
        self.assertEqual(self.db.get_guild_installations(GUILD_ID), [])

    def test_callback_with_missing_state_fails(self) -> None:
        success, message = complete_github_installation_setup(
            self.db,
            None,
            INSTALLATION_ID,
        )

        self.assertFalse(success)
        self.assertEqual(message, SETUP_INVALID_TOKEN_MESSAGE)

    def test_callback_with_missing_installation_id_fails(self) -> None:
        token = self.db.create_github_setup_token(GUILD_ID, USER_ID)

        success, message = complete_github_installation_setup(
            self.db,
            token,
            None,
        )

        self.assertFalse(success)
        self.assertEqual(message, SETUP_INVALID_TOKEN_MESSAGE)
        self.assertIsNotNone(self.db.get_valid_github_setup_token(token))

    def test_webhook_event_does_not_create_guild_installations(self) -> None:
        self.db.upsert_installed_repository(
            INSTALLATION_ID,
            "duckquack001",
            "testing-repo",
            REPO_FULL_NAME,
        )

        self.assertEqual(self.db.get_guild_installations(GUILD_ID), [])
        self.assertEqual(self.db.list_installed_repositories_for_guild(GUILD_ID), [])

    def test_dashboard_shows_connect_github_when_no_installation_is_bound(self) -> None:
        if not HAS_DISCORD:
            self.skipTest("discord.py is not installed in this test runtime")
        from nano_github.discord_bot import GitHubDashboardView, _dashboard_embed

        view = GitHubDashboardView(GUILD_ID, "github_app", has_bound_installation=False)
        labels = [getattr(item, "label", None) for item in view.children]
        embed = _dashboard_embed(
            section="github_app",
            config={"log_channels": {}, "pr_review_channel": None},
            linked_repos=[],
            installed_repos=[],
            guild_installations=[],
            issue_settings=None,
            issue_blocked_role_ids=(),
            selected_log_type="commits",
            guild=None,
            pr_review_mode="anyone",
            pr_review_role_id=None,
            selected_repo=None,
            default_repo=None,
            app_statuses={},
            bot_ready=True,
            has_bound_installation=False,
        )

        self.assertIn("Connect GitHub", labels)
        self.assertIn("Status: Not connected", embed.fields[0].value)

    def test_dashboard_shows_connected_installation_after_binding(self) -> None:
        if not HAS_DISCORD:
            self.skipTest("discord.py is not installed in this test runtime")
        from nano_github.discord_bot import GitHubDashboardView, _dashboard_embed

        installation = self.db.add_guild_installation(
            GUILD_ID,
            INSTALLATION_ID,
            "duckquack001",
            "User",
        )
        view = GitHubDashboardView(GUILD_ID, "github_app", has_bound_installation=True)
        labels = [getattr(item, "label", None) for item in view.children]
        embed = _dashboard_embed(
            section="github_app",
            config={"log_channels": {}, "pr_review_channel": None},
            linked_repos=[],
            installed_repos=[],
            guild_installations=[installation],
            issue_settings=None,
            issue_blocked_role_ids=(),
            selected_log_type="commits",
            guild=None,
            pr_review_mode="anyone",
            pr_review_role_id=None,
            selected_repo=None,
            default_repo=None,
            app_statuses={},
            bot_ready=True,
            has_bound_installation=True,
        )

        self.assertIn("Connect another GitHub installation", labels)
        self.assertIn(f"Installation ID: `{INSTALLATION_ID}`", embed.fields[0].value)
        self.assertIn("Account: `duckquack001`", embed.fields[0].value)

    def test_repository_dropdown_only_uses_bound_installations(self) -> None:
        self.db.add_guild_installation(GUILD_ID, INSTALLATION_ID)
        self.db.upsert_installed_repository(INSTALLATION_ID, "duckquack001", "testing-repo")
        self.db.upsert_installed_repository(999, "other", "repo")

        repos = self.db.list_installed_repositories_for_guild(GUILD_ID)

        self.assertEqual([repo.repository_full_name for repo in repos], [REPO_FULL_NAME])

    def test_linked_repository_uses_bound_installation_id(self) -> None:
        self.db.add_guild_installation(GUILD_ID, INSTALLATION_ID)
        installed_repo = self.db.upsert_installed_repository(
            INSTALLATION_ID,
            "duckquack001",
            "testing-repo",
            REPO_FULL_NAME,
        )

        linked_repo, _ = link_installed_repository_to_guild(self.db, GUILD_ID, installed_repo)

        self.assertEqual(linked_repo.installation_id, INSTALLATION_ID)

    def test_first_linked_repo_becomes_default(self) -> None:
        self.db.add_guild_installation(GUILD_ID, INSTALLATION_ID)
        installed_repo = self.db.upsert_installed_repository(
            INSTALLATION_ID,
            "duckquack001",
            "testing-repo",
            REPO_FULL_NAME,
        )

        linked_repo, default_set = link_installed_repository_to_guild(
            self.db,
            GUILD_ID,
            installed_repo,
        )
        settings = self.db.get_issue_settings(GUILD_ID)

        self.assertTrue(default_set)
        self.assertIsNotNone(settings)
        self.assertEqual(settings.default_owner, linked_repo.owner)  # type: ignore[union-attr]
        self.assertEqual(settings.default_repo, linked_repo.repo)  # type: ignore[union-attr]

    def test_issue_create_resolves_only_after_repo_is_linked(self) -> None:
        from nano_github.repository_access import NO_REPOSITORY_CONFIGURED_MESSAGE
        from nano_github.repository_access import resolve_issue_repository

        self.db.add_guild_installation(GUILD_ID, INSTALLATION_ID)
        installed_repo = self.db.upsert_installed_repository(
            INSTALLATION_ID,
            "duckquack001",
            "testing-repo",
            REPO_FULL_NAME,
        )

        linked_repo, error = resolve_issue_repository(self.db, GUILD_ID, None, None, None)

        self.assertIsNone(linked_repo)
        self.assertEqual(error, NO_REPOSITORY_CONFIGURED_MESSAGE)

        linked_repo, _ = link_installed_repository_to_guild(self.db, GUILD_ID, installed_repo)
        resolved_repo, error = resolve_issue_repository(self.db, GUILD_ID, None, None, None)

        self.assertIsNone(error)
        self.assertEqual(resolved_repo, linked_repo)


def _mock_github_installation():
    return patch.multiple(
        "nano_github.github_setup",
        get_installation=lambda installation_id: GitHubInstallation(
            id=installation_id,
            account_login="duckquack001",
            account_type="User",
        ),
        list_installation_repositories=lambda installation_id: (
            GitHubRepository(
                owner="duckquack001",
                repo="testing-repo",
                repository_full_name=REPO_FULL_NAME,
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
