from __future__ import annotations

import hashlib
from typing import Iterable

from .models import Attachment


class ConsoleSink:
    """A local GitHub simulation used by the zero-credential demo."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.actions: list[tuple[str, dict[str, object]]] = []
        self._next_pr = 1

    @staticmethod
    def branch_for(thread_id: str) -> str:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
        return f"pg-hub/patch-{digest}"

    def _record(self, action: str, **details: object) -> None:
        self.actions.append((action, details))
        if self.verbose:
            rendered = " ".join(f"{key}={value}" for key, value in details.items())
            print(f"{action:<18} {rendered}".rstrip())

    def publish_patchset(self, thread_id: str, attachments: tuple[Attachment, ...]) -> str:
        branch = self.branch_for(thread_id)
        self._record(
            "publish branch", branch=branch,
            patches=",".join(item.name for item in attachments),
        )
        return branch

    def ensure_pr(self, title: str, body: str, branch: str) -> int:
        del body
        number = self._next_pr
        self._next_pr += 1
        self._record("open PR", number=number, branch=branch, title=title)
        return number

    def comment(self, pr_number: int, body: str, marker: str) -> None:
        del body
        self._record("add comment", pr=pr_number, id=marker)

    def labels(self, pr_number: int, labels: Iterable[str]) -> None:
        self._record("sync labels", pr=pr_number, labels=",".join(sorted(set(labels))))

    def milestone(self, pr_number: int, title: str) -> None:
        self._record("set milestone", pr=pr_number, milestone=title)
