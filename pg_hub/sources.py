from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request, urlopen

from .config import Config
from .models import Attachment, CommitFestEntry, GitCommit, MailMessage, PollBatch


USER_AGENT = "pg_hub/0.1 (+read-only PostgreSQL development mirror)"


def http_get(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _clean_text(parts: Iterable[str]) -> str:
    return " ".join("".join(parts).replace("\xa0", " ").split())


class _ArchiveIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.message_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        match = re.fullmatch(r"/message-id/([^/]+)", href)
        if match:
            message_id = unquote(match.group(1))
            if message_id not in self.message_ids:
                self.message_ids.append(message_id)


class _MessagePageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.subject_parts: list[str] = []
        self.body_parts: list[str] = []
        self.headers: dict[str, str] = {}
        self.thread_ids: list[str] = []
        self.attachments: list[Attachment] = []
        self._in_subject = False
        self._in_body = False
        self._in_header = False
        self._in_thread_select = False
        self._current_header_key: list[str] | None = None
        self._current_header_value: list[str] | None = None
        self._attachment_href: str | None = None
        self._attachment_name_parts: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        classes = self._classes(attrs)
        if tag == "h1" and "subject" in classes:
            self._in_subject = True
        elif tag == "table" and "message-header" in classes:
            self._in_header = True
        elif tag == "div" and "message-content" in classes:
            self._in_body = True
        elif self._in_header and tag == "th":
            self._current_header_key = []
        elif self._in_header and tag == "td":
            self._current_header_value = []
        elif tag == "select" and data.get("id") == "thread_select":
            self._in_thread_select = True
        elif self._in_thread_select and tag == "option" and data.get("value"):
            self.thread_ids.append(unquote(str(data["value"])))
        elif tag == "a":
            href = data.get("href") or ""
            if "/message-id/attachment/" in href:
                self._attachment_href = urljoin(self.base_url, href)
                self._attachment_name_parts = []
        if self._in_body and tag in {"br", "p", "div", "pre"}:
            self.body_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_subject:
            self._in_subject = False
        elif tag == "table" and self._in_header:
            self._in_header = False
        elif tag == "div" and self._in_body:
            self._in_body = False
        elif self._in_header and tag == "th" and self._current_header_key is not None:
            self._current_header_key = self._current_header_key
        elif self._in_header and tag == "td" and self._current_header_value is not None:
            key = _clean_text(self._current_header_key or []).rstrip(":")
            value = _clean_text(self._current_header_value)
            if key:
                self.headers[key] = value
            self._current_header_key = None
            self._current_header_value = None
        elif tag == "select" and self._in_thread_select:
            self._in_thread_select = False
        elif tag == "a" and self._attachment_href:
            name = _clean_text(self._attachment_name_parts)
            if name:
                self.attachments.append(Attachment(name=name, url=self._attachment_href))
            self._attachment_href = None
            self._attachment_name_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_subject:
            self.subject_parts.append(data)
        if self._in_body:
            self.body_parts.append(data)
        if self._current_header_value is not None:
            self._current_header_value.append(data)
        elif self._current_header_key is not None:
            self._current_header_key.append(data)
        if self._attachment_href:
            self._attachment_name_parts.append(data)

    @property
    def subject(self) -> str:
        return _clean_text(self.subject_parts)

    @property
    def body(self) -> str:
        lines = (line.rstrip() for line in "".join(self.body_parts).splitlines())
        return "\n".join(line for line in lines if line.strip()).strip()


class _CommitFestIndexParser(HTMLParser):
    def __init__(self, base_url: str, commitfest_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.commitfest_id = commitfest_id
        self.commitfest_parts: list[str] = []
        self.rows: list[tuple[list[str], str]] = []
        self._in_h1 = False
        self._in_tr = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._cells: list[str] = []
        self._patch_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "h1":
            self._in_h1 = True
        elif tag == "tr":
            self._in_tr = True
            self._cells = []
            self._patch_href = ""
        elif self._in_tr and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []
        elif self._in_tr and tag == "a":
            href = data.get("href") or ""
            if re.fullmatch(r"/patch/\d+/", href):
                self._patch_href = urljoin(self.base_url, href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self._in_h1 = False
        elif self._in_tr and tag in {"td", "th"} and self._in_cell:
            self._cells.append(_clean_text(self._cell_parts))
            self._in_cell = False
        elif tag == "tr" and self._in_tr:
            if self._patch_href and len(self._cells) >= 3:
                self.rows.append((self._cells, self._patch_href))
            self._in_tr = False

    def handle_data(self, data: str) -> None:
        if self._in_h1:
            self.commitfest_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)

    @property
    def commitfest(self) -> str:
        title = _clean_text(self.commitfest_parts)
        match = re.search(r"Commitfest\s+([^ (]+)", title, re.I)
        return match.group(1) if match else f"CF-{self.commitfest_id}"


class PostgresArchiveSource:
    name = "mail"

    def __init__(self, config: Config):
        self.config = config

    def poll(self, cursor: str | None) -> PollBatch:
        if cursor:
            start = self._parse_cursor(cursor) - timedelta(minutes=1)
        else:
            start = datetime.now(timezone.utc) - timedelta(
                minutes=self.config.mail_lookback_minutes
            )
        stamp = start.strftime("%Y%m%d%H%M")
        index_url = (
            f"{self.config.archive_base_url}/list/"
            f"{quote(self.config.mailing_list)}/since/{stamp}/"
        )
        parser = _ArchiveIndexParser()
        parser.feed(http_get(index_url).decode("utf-8", errors="replace"))
        ids = parser.message_ids[-self.config.mail_max_messages :]
        messages = tuple(self._fetch_message(message_id) for message_id in ids)
        newest = max((self._parse_date(item.sent_at) for item in messages), default=start)
        return PollBatch(messages, newest.isoformat())

    def _fetch_message(self, message_id: str) -> MailMessage:
        url = f"{self.config.archive_base_url}/message-id/{quote(message_id, safe='')}"
        parser = _MessagePageParser(self.config.archive_base_url)
        parser.feed(http_get(url).decode("utf-8", errors="replace"))
        root_id = parser.thread_ids[0] if parser.thread_ids else message_id
        return MailMessage(
            message_id=message_id.strip("<>"),
            thread_id=root_id.strip("<>"),
            subject=parser.subject or parser.headers.get("Subject", "(no subject)"),
            author=parser.headers.get("From", "unknown"),
            sent_at=self._parse_date(parser.headers.get("Date", "")).isoformat(),
            body=parser.body,
            archive_url=url,
            attachments=tuple(parser.attachments),
        )

    @staticmethod
    def _parse_cursor(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _parse_date(value: str) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class CommitFestSource:
    name = "commitfest"

    def __init__(self, config: Config):
        self.config = config

    def poll(self, cursor: str | None) -> PollBatch:
        del cursor  # The page is a snapshot; per-patch fingerprints provide dedup.
        url = f"{self.config.commitfest_base_url}/{quote(self.config.commitfest_id)}/"
        parser = _CommitFestIndexParser(
            self.config.commitfest_base_url, self.config.commitfest_id
        )
        parser.feed(http_get(url).decode("utf-8", errors="replace"))
        entries: list[CommitFestEntry] = []
        for cells, patch_url in parser.rows:
            patch_id = cells[1].strip()
            if not patch_id.isdigit():
                continue
            tags = tuple(part.strip() for part in cells[3].split(",") if part.strip()) if len(cells) > 3 else ()
            entries.append(
                CommitFestEntry(
                    patch_id=patch_id,
                    title=cells[0],
                    status=cells[2],
                    commitfest=parser.commitfest,
                    url=patch_url,
                    tags=tags,
                )
            )
        snapshot = hashlib.sha256(
            json.dumps([item.fingerprint for item in entries]).encode("utf-8")
        ).hexdigest()
        return PollBatch(tuple(entries), snapshot)


class PostgresGitSource:
    name = "git"

    def __init__(self, config: Config):
        self.config = config
        self.repository = config.cache_path / "postgres-source"

    def poll(self, cursor: str | None) -> PollBatch:
        self.repository.parent.mkdir(parents=True, exist_ok=True)
        if not self.repository.exists():
            self._run(
                "git", "clone", "--depth=200", "--no-checkout", "--branch",
                self.config.postgres_git_branch,
                self.config.postgres_git_url, str(self.repository),
            )
        else:
            self._run(
                "git", "-C", str(self.repository), "fetch", "--quiet",
                "--depth=200", "origin", self.config.postgres_git_branch,
            )
        head = self._run(
            "git", "-C", str(self.repository), "rev-parse",
            f"origin/{self.config.postgres_git_branch}",
        ).strip()
        revisions = [head]
        if cursor and self._commit_exists(cursor):
            output = self._run(
                "git", "-C", str(self.repository), "rev-list", "--reverse",
                "--max-count=100", f"{cursor}..{head}",
            )
            revisions = [line for line in output.splitlines() if line]
        commits = tuple(self._read_commit(revision) for revision in revisions)
        return PollBatch(commits, head)

    def _commit_exists(self, revision: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.repository), "cat-file", "-e", f"{revision}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _read_commit(self, revision: str) -> GitCommit:
        output = self._run(
            "git", "-C", str(self.repository), "show", "-s",
            "--format=%H%x1f%s%x1f%an%x1f%aI", revision,
        ).strip()
        commit_hash, subject, author, committed_at = output.split("\x1f", 3)
        return GitCommit(
            commit_hash=commit_hash,
            subject=subject,
            author=author,
            committed_at=committed_at,
            url=(
                "https://git.postgresql.org/gitweb/"
                f"?p=postgresql.git;a=commit;h={commit_hash}"
            ),
        )

    @staticmethod
    def _run(*args: str) -> str:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"git command failed: {detail}")
        return result.stdout


@dataclass
class FixtureSources:
    mail: "FixtureSource"
    commitfest: "FixtureSource"
    git: "FixtureSource"

    @classmethod
    def load(cls, path: Path) -> "FixtureSources":
        document = json.loads(path.read_text(encoding="utf-8"))
        mail = tuple(_mail_from_json(item, path.parent) for item in document["mail"])
        commitfest = tuple(CommitFestEntry(**_tuple_fields(item, "tags")) for item in document["commitfest"])
        git = tuple(GitCommit(**item) for item in document["git"])
        return cls(
            mail=FixtureSource("mail", mail),
            commitfest=FixtureSource("commitfest", commitfest),
            git=FixtureSource("git", git),
        )


class FixtureSource:
    def __init__(self, name: str, items: tuple[Any, ...]):
        self.name = name
        self.items = items

    def poll(self, cursor: str | None) -> PollBatch:
        del cursor
        value = hashlib.sha256(
            json.dumps([getattr(item, "fingerprint", str(item)) for item in self.items]).encode()
        ).hexdigest()
        return PollBatch(self.items, value)


def _tuple_fields(value: dict[str, Any], *names: str) -> dict[str, Any]:
    result = dict(value)
    for name in names:
        if name in result:
            result[name] = tuple(result[name])
    return result


def _mail_from_json(value: dict[str, Any], fixture_dir: Path) -> MailMessage:
    data = dict(value)
    attachments = []
    for item in data.pop("attachments", []):
        attachment = dict(item)
        download_url = str(attachment.get("download_url", ""))
        if download_url.startswith("fixture://"):
            relative = download_url.removeprefix("fixture://")
            attachment["download_url"] = (fixture_dir / relative).resolve().as_uri()
        attachments.append(Attachment(**attachment))
    return MailMessage(attachments=tuple(attachments), **data)
