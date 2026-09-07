from __future__ import annotations

import re
from typing import Iterable


LABEL_DEFINITIONS: dict[str, tuple[str, str]] = {
    "source:pgsql-hackers": ("0052cc", "Mirrored from pgsql-hackers"),
    "source:pgsql-bugs": ("b60205", "Mirrored from pgsql-bugs"),
    "type:patch": ("5319e7", "Mail thread contains a PostgreSQL patch"),
    "type:bug": ("d73a4a", "PostgreSQL bug report"),
    "upstream:committed": ("1f883d", "A matching PostgreSQL git commit was observed"),
    "status:needs-review": ("d4c5f9", "CommitFest: Needs review"),
    "status:waiting-on-author": ("fbca04", "CommitFest: Waiting on Author"),
    "status:ready-for-committer": ("0e8a16", "CommitFest: Ready for Committer"),
    "status:committed": ("1f883d", "CommitFest: Committed"),
    "status:moved": ("bfd4f2", "CommitFest: moved to another commitfest"),
    "status:returned-with-feedback": ("e99695", "CommitFest: Returned with Feedback"),
    "status:rejected": ("b60205", "CommitFest: Rejected"),
    "status:withdrawn": ("6e7781", "CommitFest: Withdrawn"),
    "area:storage": ("0366d6", "Storage, access methods, buffers, or I/O"),
    "area:wal": ("006b75", "Write-ahead logging and recovery"),
    "area:replication": ("0b7fab", "Physical or logical replication"),
    "area:vacuum": ("1d76db", "VACUUM or autovacuum"),
    "area:planner": ("7057ff", "Planner and optimizer"),
    "area:executor": ("8a63d2", "Executor"),
    "area:sql": ("c5def5", "SQL language or commands"),
    "area:docs": ("0075ca", "Documentation"),
    "area:testing": ("bfdadc", "Tests and buildfarm"),
    "area:security": ("d93f0b", "Authentication, authorization, or security"),
}

STATUS_LABELS = {name for name in LABEL_DEFINITIONS if name.startswith("status:")}


def status_label(status: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", status.lower()).strip("-")
    aliases = {
        "moved-to-next-cf": "moved",
        "moved-to-different-cf": "moved",
        "returned-with-feedback": "returned-with-feedback",
    }
    return f"status:{aliases.get(normalized, normalized)}"


_AREA_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("area:wal", ("wal", "xlog", "recovery", "checkpoint")),
    ("area:replication", ("replication", "logical decoding", "subscriber", "publication", "slot")),
    ("area:vacuum", ("vacuum", "autovacuum", "freeze")),
    ("area:planner", ("planner", "optimizer", "plan ", "join", "cost", "selectivity", "pathkey")),
    ("area:executor", ("executor", "exec", "tuple slot", "aggregate")),
    ("area:storage", ("buffer", "bufmgr", "storage", "pageinspect", "heapam", "table am", "index", "aio", "i/o")),
    ("area:docs", ("documentation", " doc:", "docs", "sgml")),
    ("area:testing", ("test", "tap", "buildfarm", "flaky")),
    ("area:security", ("security", "authentication", "authorization", "scram", "ssl")),
    ("area:sql", ("sql", "ddl", "dml", "create table", "alter table")),
)


def subsystem_labels(*texts: str) -> set[str]:
    haystack = " ".join(texts).lower()
    return {
        label
        for label, needles in _AREA_RULES
        if any(needle in haystack for needle in needles)
    }


def commitfest_tag_labels(tags: Iterable[str]) -> set[str]:
    labels: set[str] = set()
    for tag in tags:
        lowered = tag.lower()
        if "doc" in lowered:
            labels.add("area:docs")
        if "test" in lowered:
            labels.add("area:testing")
        if "sql" in lowered or "ddl" in lowered:
            labels.add("area:sql")
        if "security" in lowered:
            labels.add("area:security")
        if "performance" in lowered:
            labels.add("type:performance")
    return labels


def definition(label: str) -> tuple[str, str]:
    if label in LABEL_DEFINITIONS:
        return LABEL_DEFINITIONS[label]
    if label.startswith("cf:"):
        return "fef2c0", "PostgreSQL CommitFest"
    if label == "type:performance":
        return "ff9f1c", "Performance-related patch"
    return "ededed", "Managed by pg_hub"
