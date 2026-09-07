from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable, Protocol

from .errors import PatchSetUnavailableError
from .labels import STATUS_LABELS, commitfest_tag_labels, status_label, subsystem_labels
from .models import Attachment, CommitFestEntry, GitCommit, MailMessage, PollBatch, stable_fingerprint
from .state import ConversationMirror, StateStore, ThreadMirror


class Source(Protocol):
    name: str

    def poll(self, cursor: str | None) -> PollBatch: ...


class Sink(Protocol):
    def branch_for(self, thread_id: str) -> str: ...
    def publish_patchset(self, thread_id: str, attachments: tuple[Attachment, ...]) -> str: ...
    def ensure_pr(self, title: str, body: str, branch: str) -> int: ...
    def ensure_issue(self, title: str, body: str, marker: str) -> int: ...
    def ensure_discussion(self, title: str, body: str, marker: str) -> int: ...
    def comment(self, pr_number: int, body: str, marker: str) -> None: ...
    def discussion_comment(self, number: int, body: str, marker: str) -> None: ...
    def labels(self, pr_number: int, labels: Iterable[str]) -> None: ...
    def milestone(self, pr_number: int, title: str) -> None: ...
    def lock(self, kind: str, number: int) -> None: ...
    def link_discussion_pr(
        self, discussion_number: int, pr_number: int, marker: str
    ) -> None: ...


@dataclass
class SyncReport:
    mail_seen: int = 0
    mail_changed: int = 0
    mail_skipped: int = 0
    bugs_seen: int = 0
    bugs_changed: int = 0
    commitfest_seen: int = 0
    commitfest_changed: int = 0
    git_seen: int = 0
    git_changed: int = 0

    @property
    def changed(self) -> int:
        return (
            self.mail_changed
            + self.bugs_changed
            + self.commitfest_changed
            + self.git_changed
        )


class SyncEngine:
    def __init__(self, state: StateStore, sink: Sink):
        self.state = state
        self.sink = sink

    def sync_all(
        self, mail: Source, commitfest: Source, git: Source, bugs: Source | None = None
    ) -> SyncReport:
        report = SyncReport()
        self.sync_mail(mail, report)
        if bugs is not None:
            self.sync_bugs(bugs, report)
        self.sync_commitfest(commitfest, report)
        self.sync_git(git, report)
        return report

    def sync_mail(self, source: Source, report: SyncReport | None = None) -> SyncReport:
        report = report or SyncReport()
        batch = source.poll(self.state.cursor(source.name))
        report.mail_seen += len(batch.items)
        for item in batch.items:
            if not isinstance(item, MailMessage):
                raise TypeError("mail source returned a non-mail item")
            if self.state.event_is_current("mail", item.message_id, item.fingerprint):
                continue
            try:
                changed = self._sync_hackers_message(item)
            except PatchSetUnavailableError as error:
                print(
                    "skip mail patch   "
                    f"message_id={item.message_id} reason={error}"
                )
                self.state.record_event("mail", item.message_id, item.fingerprint)
                report.mail_skipped += 1
                continue
            self.state.record_event("mail", item.message_id, item.fingerprint)
            report.mail_changed += int(changed)
        self.state.set_cursor(source.name, batch.cursor)
        return report

    def _sync_hackers_message(self, message: MailMessage) -> bool:
        mirror = self.state.thread(message.thread_id)
        if mirror is None and not message.patch_attachments:
            return self._sync_discussion_message(message)
        return self._sync_patch_message(message, mirror)

    def _sync_patch_message(
        self, message: MailMessage, mirror: ThreadMirror | None
    ) -> bool:
        created = False
        if mirror is None:
            if not message.patch_attachments:
                # A PR needs a real branch. A later message in the same thread
                # carrying the patch can create it.
                return False
            branch = self.sink.publish_patchset(message.thread_id, message.patch_attachments)
            title = clean_subject(message.subject)
            pr_number = self.sink.ensure_pr(title, render_pr_body(message), branch)
            mirror = ThreadMirror(
                thread_id=message.thread_id,
                pr_number=pr_number,
                branch=branch,
                title=title,
                latest_patch_fingerprint=patchset_fingerprint(message.patch_attachments),
            )
            self.state.save_thread(mirror)
            created = True
            discussion = self.state.conversation(message.thread_id, "discussion")
            if discussion is not None:
                self.sink.link_discussion_pr(
                    discussion.number,
                    pr_number,
                    marker_for("discussion-pr", message.thread_id),
                )
        elif message.patch_attachments:
            patch_fingerprint = patchset_fingerprint(message.patch_attachments)
            if mirror.latest_patch_fingerprint != patch_fingerprint:
                branch = self.sink.publish_patchset(message.thread_id, message.patch_attachments)
                mirror = ThreadMirror(
                    thread_id=mirror.thread_id,
                    pr_number=mirror.pr_number,
                    branch=branch,
                    title=mirror.title,
                    latest_patch_fingerprint=patch_fingerprint,
                )
                self.state.save_thread(mirror)
        is_root_message = message.message_id == message.thread_id
        if not created and is_root_message:
            self.sink.ensure_pr(
                mirror.title, render_pr_body(message), mirror.branch
            )
        if not created and not is_root_message:
            marker = marker_for("message", message.message_id)
            self.sink.comment(mirror.pr_number, render_mail_comment(message, marker), marker)
        labels = self.state.labels(message.thread_id)
        labels.update({f"source:{message.mailing_list}", "type:patch"})
        labels.update(subsystem_labels(message.subject, message.body))
        self._sync_labels(mirror, labels)
        self.sink.lock("pr", mirror.pr_number)
        return True

    def _sync_discussion_message(self, message: MailMessage) -> bool:
        mirror = self.state.conversation(message.thread_id, "discussion")
        created = False
        thread_marker = marker_for("discussion-thread", message.thread_id)
        if mirror is None:
            title = clean_subject(message.subject)
            number = self.sink.ensure_discussion(
                title, render_discussion_body(message), thread_marker
            )
            mirror = ConversationMirror(message.thread_id, "discussion", number, title)
            self.state.save_conversation(mirror)
            created = True
        elif message.message_id == message.thread_id:
            self.sink.ensure_discussion(
                mirror.title, render_discussion_body(message), thread_marker
            )
        if not created and message.message_id != message.thread_id:
            marker = marker_for("message", message.message_id)
            self.sink.discussion_comment(
                mirror.number, render_mail_comment(message, marker), marker
            )
        self.sink.lock("discussion", mirror.number)
        return True

    def sync_bugs(
        self, source: Source, report: SyncReport | None = None
    ) -> SyncReport:
        report = report or SyncReport()
        batch = source.poll(self.state.cursor(source.name))
        report.bugs_seen += len(batch.items)
        for item in batch.items:
            if not isinstance(item, MailMessage):
                raise TypeError("bugs source returned a non-mail item")
            if self.state.event_is_current("bugs", item.message_id, item.fingerprint):
                continue
            changed = self._sync_issue_message(item)
            self.state.record_event("bugs", item.message_id, item.fingerprint)
            report.bugs_changed += int(changed)
        self.state.set_cursor(source.name, batch.cursor)
        return report

    def _sync_issue_message(self, message: MailMessage) -> bool:
        mirror = self.state.conversation(message.thread_id, "issue")
        created = False
        thread_marker = marker_for("issue-thread", message.thread_id)
        if mirror is None:
            title = clean_subject(message.subject)
            number = self.sink.ensure_issue(
                title, render_issue_body(message), thread_marker
            )
            mirror = ConversationMirror(message.thread_id, "issue", number, title)
            self.state.save_conversation(mirror)
            created = True
        elif message.message_id == message.thread_id:
            self.sink.ensure_issue(mirror.title, render_issue_body(message), thread_marker)
        if not created and message.message_id != message.thread_id:
            marker = marker_for("message", message.message_id)
            self.sink.comment(
                mirror.number, render_mail_comment(message, marker), marker
            )
        labels = {f"source:{message.mailing_list}", "type:bug"}
        labels.update(subsystem_labels(message.subject, message.body))
        self.sink.labels(mirror.number, labels)
        self.sink.lock("issue", mirror.number)
        return True

    def sync_commitfest(
        self, source: Source, report: SyncReport | None = None
    ) -> SyncReport:
        report = report or SyncReport()
        batch = source.poll(self.state.cursor(source.name))
        report.commitfest_seen += len(batch.items)
        for item in batch.items:
            if not isinstance(item, CommitFestEntry):
                raise TypeError("CommitFest source returned an invalid item")
            if self.state.event_is_current("commitfest", item.patch_id, item.fingerprint):
                continue
            mirror = self.state.commitfest_thread(item.patch_id)
            if mirror is None and item.thread_id:
                mirror = self.state.thread(item.thread_id)
            if mirror is None:
                mirror = self.state.find_thread_by_title(item.title)
            if mirror is None:
                # Do not consume it: a mail thread arriving later should still match.
                continue
            self.state.link_commitfest(item.patch_id, mirror.thread_id)
            labels = self.state.labels(mirror.thread_id)
            labels.difference_update(STATUS_LABELS)
            labels = {label for label in labels if not label.startswith("cf:")}
            labels.add(status_label(item.status))
            labels.add(f"cf:{item.commitfest.lower()}")
            labels.update(commitfest_tag_labels(item.tags))
            labels.update(subsystem_labels(item.title, " ".join(item.tags)))
            self._sync_labels(mirror, labels)
            self.sink.milestone(mirror.pr_number, item.commitfest)
            self.state.record_event("commitfest", item.patch_id, item.fingerprint)
            report.commitfest_changed += 1
        self.state.set_cursor(source.name, batch.cursor)
        return report

    def sync_git(self, source: Source, report: SyncReport | None = None) -> SyncReport:
        report = report or SyncReport()
        batch = source.poll(self.state.cursor(source.name))
        report.git_seen += len(batch.items)
        for item in batch.items:
            if not isinstance(item, GitCommit):
                raise TypeError("git source returned an invalid item")
            if self.state.event_is_current("git", item.commit_hash, item.fingerprint):
                continue
            mirror = self.state.thread(item.thread_id) if item.thread_id else None
            if mirror is None:
                mirror = self.state.find_thread_by_title(item.subject)
            if mirror is not None:
                marker = marker_for("commit", item.commit_hash)
                self.sink.comment(
                    mirror.pr_number, render_commit_comment(item, marker), marker
                )
                labels = self.state.labels(mirror.thread_id)
                labels.add("upstream:committed")
                self._sync_labels(mirror, labels)
                report.git_changed += 1
            self.state.record_event("git", item.commit_hash, item.fingerprint)
        self.state.set_cursor(source.name, batch.cursor)
        return report

    def _sync_labels(self, mirror: ThreadMirror, labels: set[str]) -> None:
        self.sink.labels(mirror.pr_number, labels)
        self.state.set_labels(mirror.thread_id, labels)


def clean_subject(subject: str) -> str:
    value = re.sub(r"^(?:\s*(?:re|fwd?)\s*:\s*)+", "", subject, flags=re.I)
    return " ".join(value.split())


def marker_for(kind: str, source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    return f"<!-- pg_hub:{kind}={digest} -->"


def patchset_fingerprint(attachments: tuple[Attachment, ...]) -> str:
    return stable_fingerprint(
        [{"name": item.name, "url": item.url} for item in attachments]
    )


def render_pr_body(message: MailMessage) -> str:
    attachments = "\n".join(
        f"- [{item.name}]({item.url})" for item in message.patch_attachments
    )
    marker = marker_for("thread", message.thread_id)
    return (
        "> **Read-only mirror.** Reply and review on pgsql-hackers; "
        "activity here is not sent upstream.\n\n"
        f"- Original author: {message.author}\n"
        f"- Mailing list: `{message.mailing_list}`\n"
        f"- Message-ID: `{message.message_id}`\n"
        f"- [Original email]({message.archive_url})\n\n"
        f"Patch files:\n{attachments}\n\n"
        f"---\n\n{truncate(message.body)}\n\n{marker}"
    )


def render_mail_comment(message: MailMessage, marker: str) -> str:
    attachment_block = ""
    if message.patch_attachments:
        links = ", ".join(f"[{item.name}]({item.url})" for item in message.patch_attachments)
        attachment_block = f"\n\nNew patch set: {links}"
    return (
        f"**{message.author}** via {message.mailing_list} · "
        f"[original email]({message.archive_url})\n\n"
        f"{truncate(message.body)}{attachment_block}\n\n{marker}"
    )


def render_issue_body(message: MailMessage) -> str:
    marker = marker_for("issue-thread", message.thread_id)
    return (
        "> **Read-only mirror.** Report and reply on pgsql-bugs; activity here "
        "is not sent upstream.\n\n"
        f"- Original author: {message.author}\n"
        f"- Mailing list: `{message.mailing_list}`\n"
        f"- Message-ID: `{message.message_id}`\n"
        f"- [Original email]({message.archive_url})\n\n"
        f"---\n\n{truncate(message.body)}\n\n{marker}"
    )


def render_discussion_body(message: MailMessage) -> str:
    marker = marker_for("discussion-thread", message.thread_id)
    return (
        "> **Read-only mirror.** Join the conversation on pgsql-hackers; "
        "activity here is not sent upstream.\n\n"
        f"- Original author: {message.author}\n"
        f"- Mailing list: `{message.mailing_list}`\n"
        f"- Message-ID: `{message.message_id}`\n"
        f"- [Original email]({message.archive_url})\n\n"
        f"---\n\n{truncate(message.body)}\n\n{marker}"
    )


def render_commit_comment(commit: GitCommit, marker: str) -> str:
    return (
        f"A matching commit was observed in PostgreSQL git:\n\n"
        f"[`{commit.commit_hash[:12]}`]({commit.url}) — {commit.subject} "
        f"by {commit.author}\n\n"
        f"The mirror labels this PR as committed but does not merge or close it.\n\n"
        f"{marker}"
    )


def truncate(body: str, limit: int = 60_000) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + "\n\n_[message truncated by pg_hub]_"
