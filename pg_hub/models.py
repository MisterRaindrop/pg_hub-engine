from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def stable_fingerprint(value: Any) -> str:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str
    content_type: str = "application/octet-stream"
    size: str = ""
    download_url: str | None = None

    @property
    def is_patch(self) -> bool:
        lower = self.name.lower()
        return lower.endswith((".patch", ".diff"))

    @property
    def effective_download_url(self) -> str:
        return self.download_url or self.url


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    thread_id: str
    subject: str
    author: str
    sent_at: str
    body: str
    archive_url: str
    in_reply_to: str | None = None
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)

    @property
    def patch_attachments(self) -> tuple[Attachment, ...]:
        return tuple(item for item in self.attachments if item.is_patch)

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(self)


@dataclass(frozen=True)
class CommitFestEntry:
    patch_id: str
    title: str
    status: str
    commitfest: str
    url: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    thread_id: str | None = None

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(self)


@dataclass(frozen=True)
class GitCommit:
    commit_hash: str
    subject: str
    author: str
    committed_at: str
    url: str
    thread_id: str | None = None

    @property
    def fingerprint(self) -> str:
        return self.commit_hash


@dataclass(frozen=True)
class PollBatch:
    items: tuple[Any, ...]
    cursor: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
