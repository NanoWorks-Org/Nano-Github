from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from nano_github.database import (
    Database,
    InstallationNotBoundToGuild,
    RepositoryNotLinkedToGuild,
)
from nano_github.repository_access import (
    INSTALLATION_NOT_BOUND_MESSAGE,
    NO_REPOSITORY_CONFIGURED_MESSAGE,
    REPOSITORY_NOT_LINKED_MESSAGE,
    resolve_issue_repository,
)


GUILD_A = 1001
GUILD_B = 2002
INSTALLATION_A = 111
INSTALLATION_B = 222
REPO_A = "ownerA/repoA"
REPO_B = "ownerB/repoB"


class RepositoryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "nano-github.sqlite3")
        self.db.init()
        self.db.upsert_guild(GUILD_A, "Guild A")
        self.db.upsert_guild(GUILD_B, "Guild B")
        self.db.add_guild_installation(GUILD_A, INSTALLATION_A, "ownerA", "User")
        self.db.add_guild_installation(GUILD_B, INSTALLATION_B, "ownerB", "User")
        self.db.upsert_installed_repository(INSTALLATION_A, "ownerA", "repoA", REPO_A)
        self.db.upsert_installed_repository(INSTALLATION_B, "ownerB", "repoB", REPO_B)
        self.guild_a_repo = self.db.link_repository(
            GUILD_A,
            "ownerA",
            "repoA",
            installation_id=INSTALLATION_A,
            repository_full_name=REPO_A,
        )
        self.guild_b_repo = self.db.link_repository(
            GUILD_B,
            "ownerB",
            "repoB",
            installation_id=INSTALLATION_B,
            repository_full_name=REPO_B,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_guild_a_dashboard_does_not_show_repo_b(self) -> None:
        repos = self.db.list_linked_repositories_for_guild(GUILD_A)
        self.assertEqual([repo.repository_full_name for repo in repos], [REPO_A])

    def test_guild_b_dashboard_does_not_show_repo_a(self) -> None:
        repos = self.db.list_linked_repositories_for_guild(GUILD_B)
        self.assertEqual([repo.repository_full_name for repo in repos], [REPO_B])

    def test_guild_a_installed_repo_list_only_includes_installation_a(self) -> None:
        repos = self.db.list_installed_repositories_for_guild(GUILD_A)
        self.assertEqual([repo.repository_full_name for repo in repos], [REPO_A.lower()])

    def test_guild_b_installed_repo_list_only_includes_installation_b(self) -> None:
        repos = self.db.list_installed_repositories_for_guild(GUILD_B)
        self.assertEqual([repo.repository_full_name for repo in repos], [REPO_B.lower()])

    def test_guild_a_cannot_set_repo_b_as_default(self) -> None:
        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.set_issue_default_repository(GUILD_A, "ownerB", "repoB")

    def test_guild_b_cannot_set_repo_a_as_default(self) -> None:
        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.set_issue_default_repository(GUILD_B, "ownerA", "repoA")

    def test_guild_a_cannot_create_issues_in_repo_b(self) -> None:
        linked_repo, error = resolve_issue_repository(
            self.db,
            GUILD_A,
            None,
            "ownerB",
            "repoB",
        )

        self.assertIsNone(linked_repo)
        self.assertEqual(error, REPOSITORY_NOT_LINKED_MESSAGE)

    def test_guild_b_cannot_create_issues_in_repo_a(self) -> None:
        linked_repo, error = resolve_issue_repository(
            self.db,
            GUILD_B,
            None,
            "ownerA",
            "repoA",
        )

        self.assertIsNone(linked_repo)
        self.assertEqual(error, REPOSITORY_NOT_LINKED_MESSAGE)

    def test_guild_a_cannot_load_labels_from_repo_b(self) -> None:
        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.assert_repo_linked_to_guild(GUILD_A, REPO_B)

    def test_guild_b_cannot_load_labels_from_repo_a(self) -> None:
        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.assert_repo_linked_to_guild(GUILD_B, REPO_A)

    def test_guild_a_pr_buttons_cannot_act_on_repo_b(self) -> None:
        self.db.upsert_pr_message(
            GUILD_A,
            "ownerB",
            "repoB",
            pr_number=7,
            channel_id=123,
            message_id=456,
            state="open",
        )
        pr_message = self.db.get_pr_message_by_discord_message(GUILD_A, 456)
        self.assertIsNotNone(pr_message)

        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.assert_repo_linked_to_guild(
                GUILD_A,
                f"{pr_message.owner}/{pr_message.repo}",  # type: ignore[union-attr]
            )

    def test_guild_b_pr_buttons_cannot_act_on_repo_a(self) -> None:
        self.db.upsert_pr_message(
            GUILD_B,
            "ownerA",
            "repoA",
            pr_number=8,
            channel_id=123,
            message_id=789,
            state="open",
        )
        pr_message = self.db.get_pr_message_by_discord_message(GUILD_B, 789)
        self.assertIsNotNone(pr_message)

        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.assert_repo_linked_to_guild(
                GUILD_B,
                f"{pr_message.owner}/{pr_message.repo}",  # type: ignore[union-attr]
            )

    def test_webhook_for_installation_a_only_routes_to_guild_a(self) -> None:
        repos = self.db.find_linked_repositories_for_installation(
            INSTALLATION_A,
            "ownerA",
            "repoA",
        )
        self.assertEqual([repo.guild_id for repo in repos], [GUILD_A])

    def test_webhook_for_installation_b_only_routes_to_guild_b(self) -> None:
        repos = self.db.find_linked_repositories_for_installation(
            INSTALLATION_B,
            "ownerB",
            "repoB",
        )
        self.assertEqual([repo.guild_id for repo in repos], [GUILD_B])

    def test_global_repo_not_linked_to_guild_is_rejected(self) -> None:
        self.db.upsert_installed_repository(INSTALLATION_B, "ownerC", "repoC", "ownerC/repoC")

        with self.assertRaises(RepositoryNotLinkedToGuild):
            self.db.assert_repo_linked_to_guild(GUILD_A, "ownerC/repoC")

    def test_guild_with_no_bound_installation_has_no_installed_repos(self) -> None:
        guild_c = 3003
        self.db.upsert_guild(guild_c, "Guild C")

        self.assertEqual(self.db.get_guild_installations(guild_c), [])
        self.assertEqual(self.db.list_installed_repositories_for_guild(guild_c), [])

    def test_guild_with_no_linked_repo_issue_create_message(self) -> None:
        guild_c = 3003
        self.db.upsert_guild(guild_c, "Guild C")
        self.db.add_guild_installation(guild_c, 333)

        linked_repo, error = resolve_issue_repository(self.db, guild_c, None, None, None)

        self.assertIsNone(linked_repo)
        self.assertEqual(error, NO_REPOSITORY_CONFIGURED_MESSAGE)

    def test_revoke_setup_tokens_only_affects_current_guild(self) -> None:
        token_a = self.db.create_github_setup_token(GUILD_A, 11)
        token_b = self.db.create_github_setup_token(GUILD_B, 22)

        self.db.revoke_pending_setup_tokens(GUILD_A)

        self.assertIsNone(self.db.get_valid_github_setup_token(token_a))
        self.assertIsNotNone(self.db.get_valid_github_setup_token(token_b))

    def test_disconnect_github_only_removes_github_state_for_current_guild(self) -> None:
        token_a = self.db.create_github_setup_token(GUILD_A, 11)
        token_b = self.db.create_github_setup_token(GUILD_B, 22)
        self.db.set_issue_default_repository(GUILD_A, "ownerA", "repoA")
        self.db.set_issue_creation_enabled(GUILD_A, False)
        self.db.set_log_channel(GUILD_A, "issues", 444)
        self.db.set_pr_review_channel(GUILD_A, 555)
        self.db.add_issue_blocked_role(GUILD_A, 666)

        self.db.disconnect_github_for_guild(GUILD_A)

        self.assertEqual(self.db.get_guild_installations(GUILD_A), [])
        self.assertEqual(self.db.list_linked_repositories_for_guild(GUILD_A), [])
        settings = self.db.get_issue_settings(GUILD_A)
        self.assertIsNotNone(settings)
        self.assertIsNone(settings.default_owner)  # type: ignore[union-attr]
        self.assertIsNone(settings.default_repo)  # type: ignore[union-attr]
        self.assertFalse(settings.enabled)  # type: ignore[union-attr]
        self.assertEqual(self.db.get_log_channel(GUILD_A, "issues"), 444)
        self.assertEqual(self.db.get_pr_review_channel(GUILD_A), 555)
        self.assertEqual(self.db.get_issue_blocked_roles(GUILD_A), (666,))
        self.assertIsNone(self.db.get_valid_github_setup_token(token_a))

        self.assertEqual(
            [item.installation_id for item in self.db.get_guild_installations(GUILD_B)],
            [INSTALLATION_B],
        )
        self.assertEqual(
            [
                repo.repository_full_name
                for repo in self.db.list_linked_repositories_for_guild(GUILD_B)
            ],
            [REPO_B],
        )
        self.assertIsNotNone(self.db.get_valid_github_setup_token(token_b))

        linked_repo, error = resolve_issue_repository(self.db, GUILD_A, settings, None, None)
        self.assertIsNone(linked_repo)
        self.assertEqual(error, NO_REPOSITORY_CONFIGURED_MESSAGE)

    def test_reset_guild_config_removes_all_current_guild_config(self) -> None:
        token_a = self.db.create_github_setup_token(GUILD_A, 11)
        token_b = self.db.create_github_setup_token(GUILD_B, 22)
        self.db.set_issue_default_repository(GUILD_A, "ownerA", "repoA")
        self.db.set_log_channel(GUILD_A, "issues", 444)
        self.db.set_pr_review_channel(GUILD_A, 555)
        self.db.add_issue_blocked_role(GUILD_A, 666)
        self.db.upsert_pr_message(
            GUILD_A,
            "ownerA",
            "repoA",
            pr_number=7,
            channel_id=123,
            message_id=456,
            state="open",
        )

        self.db.reset_guild_config(GUILD_A)

        self.assertEqual(self.db.get_guild_installations(GUILD_A), [])
        self.assertEqual(self.db.list_linked_repositories_for_guild(GUILD_A), [])
        self.assertIsNone(self.db.get_issue_settings(GUILD_A))
        self.assertIsNone(self.db.get_log_channel(GUILD_A, "issues"))
        self.assertIsNone(self.db.get_pr_review_channel(GUILD_A))
        self.assertEqual(self.db.get_issue_blocked_roles(GUILD_A), ())
        self.assertIsNone(self.db.get_pr_message(GUILD_A, "ownerA", "repoA", 7))
        self.assertIsNone(self.db.get_valid_github_setup_token(token_a))

        self.assertEqual(
            [item.installation_id for item in self.db.get_guild_installations(GUILD_B)],
            [INSTALLATION_B],
        )
        self.assertEqual(
            [
                repo.repository_full_name
                for repo in self.db.list_linked_repositories_for_guild(GUILD_B)
            ],
            [REPO_B],
        )
        self.assertIsNotNone(self.db.get_valid_github_setup_token(token_b))

        linked_repo, error = resolve_issue_repository(self.db, GUILD_A, None, None, None)
        self.assertIsNone(linked_repo)
        self.assertEqual(error, NO_REPOSITORY_CONFIGURED_MESSAGE)

    def test_single_linked_repo_fallback_uses_guild_repo(self) -> None:
        linked_repo, error = resolve_issue_repository(self.db, GUILD_A, None, None, None)

        self.assertIsNone(error)
        self.assertEqual(linked_repo, self.guild_a_repo)

    def test_default_repo_works_only_when_linked_to_guild(self) -> None:
        self.db.set_issue_default_repository(GUILD_A, "ownerA", "repoA")
        settings = self.db.get_issue_settings(GUILD_A)

        linked_repo, error = resolve_issue_repository(self.db, GUILD_A, settings, None, None)

        self.assertIsNone(error)
        self.assertEqual(linked_repo, self.guild_a_repo)

    def test_linked_repo_uses_installation_id_from_guild_record(self) -> None:
        linked_repo = self.db.assert_repo_linked_to_guild(GUILD_A, REPO_A)

        self.assertEqual(linked_repo.installation_id, INSTALLATION_A)

    def test_cannot_link_repository_from_unbound_installation(self) -> None:
        with self.assertRaises(InstallationNotBoundToGuild):
            self.db.link_repository(
                GUILD_A,
                "ownerB",
                "repoB",
                installation_id=INSTALLATION_B,
                repository_full_name=REPO_B,
            )

    def test_bound_installation_without_link_rejects_issue_resolution(self) -> None:
        self.db.add_guild_installation(GUILD_A, INSTALLATION_B)

        linked_repo, error = resolve_issue_repository(
            self.db,
            GUILD_A,
            None,
            "ownerB",
            "repoB",
        )

        self.assertIsNone(linked_repo)
        self.assertEqual(error, REPOSITORY_NOT_LINKED_MESSAGE)

    def test_linked_repo_without_bound_installation_is_rejected(self) -> None:
        legacy_repo = self.db.link_repository(GUILD_A, "ownerLegacy", "repoLegacy")

        with self.assertRaises(InstallationNotBoundToGuild):
            self.db.assert_repo_linked_to_guild(
                GUILD_A,
                f"{legacy_repo.owner}/{legacy_repo.repo}",
            )

    def test_resolver_reports_unbound_installation_for_stale_link(self) -> None:
        self.db.remove_guild_installation(GUILD_A, INSTALLATION_A)

        linked_repo, error = resolve_issue_repository(self.db, GUILD_A, None, "ownerA", "repoA")

        self.assertIsNone(linked_repo)
        self.assertEqual(error, INSTALLATION_NOT_BOUND_MESSAGE)

    def test_existing_linked_repositories_with_installation_ids_are_migrated(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE guilds (
                    guild_id INTEGER PRIMARY KEY,
                    guild_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE linked_repositories (
                    guild_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    webhook_secret TEXT NOT NULL,
                    installation_id INTEGER,
                    repository_full_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, owner, repo)
                );
                INSERT INTO guilds (guild_id, guild_name) VALUES (1001, 'Guild A');
                INSERT INTO linked_repositories (
                    guild_id,
                    owner,
                    repo,
                    webhook_secret,
                    installation_id,
                    repository_full_name
                )
                VALUES (1001, 'ownera', 'repoa', 'secret', 111, 'ownerA/repoA');
                """
            )
        finally:
            connection.close()

        migrated = Database(legacy_path)
        try:
            migrated.init()
            self.assertTrue(migrated.is_installation_bound_to_guild(GUILD_A, INSTALLATION_A))
            linked_repo = migrated.assert_repo_linked_to_guild(GUILD_A, REPO_A)
            self.assertEqual(linked_repo.installation_id, INSTALLATION_A)
        finally:
            migrated.close()

    def test_webhook_lookup_ignores_unbound_legacy_installation(self) -> None:
        self.db.remove_guild_installation(GUILD_A, INSTALLATION_A)

        repos = self.db.find_linked_repositories_for_installation(
            INSTALLATION_A,
            "ownerA",
            "repoA",
        )

        self.assertEqual(repos, [])


if __name__ == "__main__":
    unittest.main()
