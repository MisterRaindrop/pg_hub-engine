from __future__ import annotations

import tempfile
from pathlib import Path
import subprocess
import unittest
from dataclasses import replace

from pg_hub.config import Config
from pg_hub.errors import PatchSetUnavailableError
from pg_hub.github import GitHubAuth, PatchPublisher, order_patch_attachments
from pg_hub.models import Attachment, MailMessage
from pg_hub.sink import ConsoleSink
from pg_hub.sources import (
    FixtureSources,
    FixtureSource,
    PostgresGitSource,
    _ArchiveIndexParser,
    _CommitFestIndexParser,
    _MessagePageParser,
)
from pg_hub.state import StateStore, normalize_title
from pg_hub.sync import SyncEngine


ROOT = Path(__file__).resolve().parent.parent


class SyncTests(unittest.TestCase):
    def test_fixture_sync_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            sink = ConsoleSink(verbose=False)
            sources = FixtureSources.load(ROOT / "fixtures" / "demo.json")
            self.assertEqual(sources.mail.name, "fixture-mail")
            attachment = sources.mail.items[0].attachments[0]
            self.assertTrue(attachment.url.startswith("https://github.com/"))
            self.assertTrue(attachment.effective_download_url.startswith("file://"))
            engine = SyncEngine(store, sink)
            first = engine.sync_all(sources.mail, sources.commitfest, sources.git)
            action_count = len(sink.actions)
            second = engine.sync_all(sources.mail, sources.commitfest, sources.git)
            self.assertEqual(first.mail_changed, 3)
            self.assertEqual(first.commitfest_changed, 1)
            self.assertEqual(first.git_changed, 1)
            self.assertGreater(action_count, 0)
            self.assertEqual(second.changed, 0)
            self.assertEqual(len(sink.actions), action_count)
            mirror = store.thread("demo-v1@postgresql.org")
            self.assertIsNotNone(mirror)
            self.assertIn("status:needs-review", store.labels(mirror.thread_id))
            self.assertIn("upstream:committed", store.labels(mirror.thread_id))
            store.close()

    def test_archive_index_extracts_message_ids_once(self) -> None:
        parser = _ArchiveIndexParser()
        parser.feed(
            '<a href="/message-id/one%40example.test">one</a>'
            '<a href="/message-id/one%40example.test">duplicate</a>'
            '<a href="/message-id/two%2Btag%40example.test">two</a>'
        )
        self.assertEqual(parser.message_ids, ["one@example.test", "two+tag@example.test"])

    def test_message_page_extracts_thread_body_and_patch(self) -> None:
        parser = _MessagePageParser("https://www.postgresql.org")
        parser.feed(
            """
            <h1 class="subject">Re: [PATCH v2] Better buffers</h1>
            <table class="message-header">
              <tr><th>From:</th><td>Alice &lt;a@example.test&gt;</td></tr>
              <tr><th>Date:</th><td>2026-09-07 01:02:03</td></tr>
            </table>
            <select id="thread_select"><option value="root%40example.test">root</option></select>
            <div class="message-content"><p>Hello<br>world.</p></div>
            <a href="/message-id/attachment/42/v2.patch">v2.patch</a>
            """
        )
        self.assertEqual(parser.subject, "Re: [PATCH v2] Better buffers")
        self.assertEqual(parser.headers["From"], "Alice <a@example.test>")
        self.assertEqual(parser.thread_ids, ["root@example.test"])
        self.assertIn("Hello", parser.body)
        self.assertEqual(parser.attachments[0].name, "v2.patch")

    def test_commitfest_index_extracts_patch_snapshot(self) -> None:
        parser = _CommitFestIndexParser("https://commitfest.postgresql.org", "61")
        parser.feed(
            """
            <h1>Commitfest PG20-2 (2026-09-01 – 2026-09-30)</h1>
            <table><tr>
              <td><a href="/patch/812/">Better buffers</a></td>
              <td>812</td><td><span>Needs review</span></td><td>Performance</td>
            </tr></table>
            """
        )
        self.assertEqual(parser.commitfest, "PG20-2")
        self.assertEqual(parser.rows[0][0][:4], ["Better buffers", "812", "Needs review", "Performance"])

    def test_title_normalization_removes_mail_noise(self) -> None:
        self.assertEqual(
            normalize_title("Re: [PATCH v7] Improve Buffer Prefetch"),
            "improve buffer prefetch",
        )

    def test_upstream_github_target_is_blocked(self) -> None:
        config = Config.from_env()
        unsafe = Config(**{**config.__dict__, "github_repository": "postgres/postgres", "github_token": "x"})
        with self.assertRaisesRegex(ValueError, "refusing"):
            unsafe.validate_github_target()

    def test_patch_attachment_becomes_a_real_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "postgres"
            subprocess.run(["git", "init", "-q", "-b", "master", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "test@example.test"], check=True
            )
            (repository / "README").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-q", "-m", "base"], check=True
            )
            config = replace(Config.from_env(), cache_path=Path(temp) / "cache")
            publisher = PatchPublisher(config, GitHubAuth(config))
            publisher._apply_patchset(
                repository, [ROOT / "fixtures" / "v1-0001-async-buffer-prefetch.patch"]
            )
            self.assertEqual(
                (repository / "pg_hub_demo.txt").read_text(encoding="utf-8"),
                "asynchronous buffer prefetch\ndemo patch v1\n",
            )
            count = subprocess.run(
                ["git", "-C", str(repository), "rev-list", "--count", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(count.stdout.strip(), "2")

    def test_patch_series_is_ordered_and_selects_master_variant(self) -> None:
        attachments = tuple(
            Attachment(name=name, url=f"https://example.test/{name}")
            for name in (
                "0002-follow-up.patch",
                "v3-PG17-0001-backpatch.patch",
                "v3-PG18-0001-backpatch.patch",
                "v3-0001-main.patch",
            )
        )
        self.assertEqual(
            [item.name for item in order_patch_attachments(attachments, "master")],
            ["v3-0001-main.patch", "0002-follow-up.patch"],
        )
        self.assertEqual(
            [item.name for item in order_patch_attachments(attachments, "REL_18_STABLE")],
            ["v3-PG18-0001-backpatch.patch"],
        )

    def test_unavailable_patch_is_skipped_without_replaying(self) -> None:
        class UnavailableSink(ConsoleSink):
            def publish_patchset(self, thread_id, attachments):
                del thread_id, attachments
                raise PatchSetUnavailableError("empty attachment")

        message = MailMessage(
            message_id="empty@example.test",
            thread_id="empty@example.test",
            subject="[PATCH] Empty archive attachment",
            author="Alice",
            sent_at="2026-09-07T00:00:00+00:00",
            body="The archive advertised a patch but returned zero bytes.",
            archive_url="https://example.test/message/empty",
            attachments=(
                Attachment(name="v1-0001-empty.patch", url="https://example.test/empty"),
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            source = FixtureSource("mail-test", (message,))
            sink = UnavailableSink(verbose=False)
            engine = SyncEngine(store, sink)
            first = engine.sync_mail(source)
            second = engine.sync_mail(source)
            self.assertEqual(first.mail_skipped, 1)
            self.assertEqual(second.mail_skipped, 0)
            self.assertEqual(second.changed, 0)
            store.close()

    def test_non_patch_hackers_thread_becomes_locked_discussion(self) -> None:
        root = MailMessage(
            message_id="design@example.test",
            thread_id="design@example.test",
            subject="Proposal: improve planner diagnostics",
            author="Alice",
            sent_at="2026-09-07T00:00:00+00:00",
            body="I would like to discuss planner diagnostics.",
            archive_url="https://example.test/message/design",
        )
        reply = replace(
            root,
            message_id="reply@example.test",
            subject="Re: Proposal: improve planner diagnostics",
            author="Bob",
            body="Here is some feedback.",
        )
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            sink = ConsoleSink(verbose=False)
            engine = SyncEngine(store, sink)
            report = engine.sync_mail(FixtureSource("discussion-test", (root, reply)))
            self.assertEqual(report.mail_changed, 2)
            mirror = store.conversation(root.thread_id, "discussion")
            self.assertIsNotNone(mirror)
            actions = [name for name, _ in sink.actions]
            self.assertIn("open discussion", actions)
            self.assertIn("discussion reply", actions)
            self.assertIn("lock conversation", actions)
            store.close()

    def test_pgsql_bugs_thread_becomes_locked_issue(self) -> None:
        bug = MailMessage(
            message_id="bug@example.test",
            thread_id="bug@example.test",
            subject="BUG #19000: server crash",
            author="Reporter",
            sent_at="2026-09-07T00:00:00+00:00",
            body="The server crashed while running SQL.",
            archive_url="https://example.test/message/bug",
            mailing_list="pgsql-bugs",
        )
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            sink = ConsoleSink(verbose=False)
            report = SyncEngine(store, sink).sync_bugs(
                FixtureSource("bugs-test", (bug,))
            )
            self.assertEqual(report.bugs_changed, 1)
            mirror = store.conversation(bug.thread_id, "issue")
            self.assertIsNotNone(mirror)
            actions = [name for name, _ in sink.actions]
            self.assertIn("open issue", actions)
            self.assertIn("sync labels", actions)
            self.assertIn("lock conversation", actions)
            store.close()

    def test_discussion_is_linked_when_thread_later_gets_patch(self) -> None:
        root = MailMessage(
            message_id="proposal@example.test",
            thread_id="proposal@example.test",
            subject="Proposal: a new executor feature",
            author="Alice",
            sent_at="2026-09-07T00:00:00+00:00",
            body="Design discussion first.",
            archive_url="https://example.test/message/proposal",
        )
        patch = replace(
            root,
            message_id="patch@example.test",
            subject="Re: [PATCH v1] Proposal: a new executor feature",
            attachments=(
                Attachment(
                    name="v1-0001-feature.patch",
                    url="https://example.test/v1-0001-feature.patch",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            sink = ConsoleSink(verbose=False)
            report = SyncEngine(store, sink).sync_mail(
                FixtureSource("transition-test", (root, patch))
            )
            self.assertEqual(report.mail_changed, 2)
            self.assertIsNotNone(store.conversation(root.thread_id, "discussion"))
            self.assertIsNotNone(store.thread(root.thread_id))
            self.assertIn("link discussion", [name for name, _ in sink.actions])
            store.close()

    def test_git_source_uses_commit_hash_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "origin"
            subprocess.run(["git", "init", "-q", "-b", "master", str(origin)], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(origin), "config", "user.email", "test@example.test"], check=True
            )
            (origin / "history.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "add", "history.txt"], check=True)
            subprocess.run(["git", "-C", str(origin), "commit", "-q", "-m", "first"], check=True)
            config = replace(
                Config.from_env(),
                cache_path=root / "cache",
                postgres_git_url=str(origin),
                postgres_git_branch="master",
            )
            source = PostgresGitSource(config)
            first = source.poll(None)
            self.assertEqual(len(first.items), 1)
            self.assertEqual(first.items[0].subject, "first")

            (origin / "history.txt").write_text("one\ntwo\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(origin), "add", "history.txt"], check=True)
            subprocess.run(["git", "-C", str(origin), "commit", "-q", "-m", "second"], check=True)
            second = source.poll(first.cursor)
            self.assertEqual([item.subject for item in second.items], ["second"])
            self.assertNotEqual(first.cursor, second.cursor)


if __name__ == "__main__":
    unittest.main()
