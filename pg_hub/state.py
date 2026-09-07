from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Iterable


@dataclass(frozen=True)
class ThreadMirror:
    thread_id: str
    pr_number: int
    branch: str
    title: str
    latest_patch_fingerprint: str


def normalize_title(value: str) -> str:
    value = re.sub(r"^(?:\s*(?:re|fwd?)\s*:\s*)+", "", value, flags=re.I)
    value = re.sub(r"\[(?:patch|rfc)(?:\s+v?\d+)?[^]]*\]", "", value, flags=re.I)
    value = re.sub(r"\bv\d+\b", "", value, flags=re.I)
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (source, source_id)
            );
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_mirrors (
                thread_id TEXT PRIMARY KEY,
                pr_number INTEGER NOT NULL,
                branch TEXT NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                latest_patch_fingerprint TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS commitfest_links (
                patch_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_labels (
                thread_id TEXT NOT NULL,
                label TEXT NOT NULL,
                PRIMARY KEY (thread_id, label)
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def cursor(self, source: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM cursors WHERE source = ?", (source,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_cursor(self, source: str, value: str) -> None:
        self.connection.execute(
            """INSERT INTO cursors(source, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (source, value, self._now()),
        )
        self.connection.commit()

    def event_is_current(self, source: str, source_id: str, fingerprint: str) -> bool:
        row = self.connection.execute(
            "SELECT fingerprint FROM events WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        return bool(row and row["fingerprint"] == fingerprint)

    def record_event(self, source: str, source_id: str, fingerprint: str) -> None:
        self.connection.execute(
            """INSERT INTO events(source, source_id, fingerprint, processed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source, source_id) DO UPDATE SET
                 fingerprint=excluded.fingerprint, processed_at=excluded.processed_at""",
            (source, source_id, fingerprint, self._now()),
        )
        self.connection.commit()

    def thread(self, thread_id: str) -> ThreadMirror | None:
        row = self.connection.execute(
            "SELECT * FROM thread_mirrors WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return self._thread_from_row(row)

    def all_threads(self) -> Iterable[ThreadMirror]:
        rows = self.connection.execute("SELECT * FROM thread_mirrors").fetchall()
        return tuple(self._thread_from_row(row) for row in rows)

    @staticmethod
    def _thread_from_row(row: sqlite3.Row | None) -> ThreadMirror | None:
        if row is None:
            return None
        return ThreadMirror(
            thread_id=str(row["thread_id"]),
            pr_number=int(row["pr_number"]),
            branch=str(row["branch"]),
            title=str(row["title"]),
            latest_patch_fingerprint=str(row["latest_patch_fingerprint"]),
        )

    def save_thread(self, mirror: ThreadMirror) -> None:
        self.connection.execute(
            """INSERT INTO thread_mirrors(
                   thread_id, pr_number, branch, title, normalized_title,
                   latest_patch_fingerprint
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 pr_number=excluded.pr_number,
                 branch=excluded.branch,
                 title=excluded.title,
                 normalized_title=excluded.normalized_title,
                 latest_patch_fingerprint=excluded.latest_patch_fingerprint""",
            (
                mirror.thread_id,
                mirror.pr_number,
                mirror.branch,
                mirror.title,
                normalize_title(mirror.title),
                mirror.latest_patch_fingerprint,
            ),
        )
        self.connection.commit()

    def find_thread_by_title(self, title: str) -> ThreadMirror | None:
        normalized = normalize_title(title)
        if not normalized:
            return None
        row = self.connection.execute(
            "SELECT * FROM thread_mirrors WHERE normalized_title=? LIMIT 1",
            (normalized,),
        ).fetchone()
        if row:
            return self._thread_from_row(row)
        # Commit subjects often shorten the mail title. A conservative token
        # containment fallback is useful for a demo and avoids random matches.
        wanted = set(normalized.split())
        if len(wanted) < 3:
            return None
        best: tuple[float, sqlite3.Row] | None = None
        for candidate in self.connection.execute("SELECT * FROM thread_mirrors"):
            tokens = set(str(candidate["normalized_title"]).split())
            score = len(wanted & tokens) / max(len(wanted), len(tokens), 1)
            if score >= 0.72 and (best is None or score > best[0]):
                best = (score, candidate)
        return self._thread_from_row(best[1]) if best else None

    def link_commitfest(self, patch_id: str, thread_id: str) -> None:
        self.connection.execute(
            """INSERT INTO commitfest_links(patch_id, thread_id) VALUES (?, ?)
               ON CONFLICT(patch_id) DO UPDATE SET thread_id=excluded.thread_id""",
            (patch_id, thread_id),
        )
        self.connection.commit()

    def commitfest_thread(self, patch_id: str) -> ThreadMirror | None:
        row = self.connection.execute(
            """SELECT t.* FROM thread_mirrors t
               JOIN commitfest_links c ON c.thread_id=t.thread_id
               WHERE c.patch_id=?""",
            (patch_id,),
        ).fetchone()
        return self._thread_from_row(row)

    def labels(self, thread_id: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT label FROM thread_labels WHERE thread_id=?", (thread_id,)
        ).fetchall()
        return {str(row["label"]) for row in rows}

    def set_labels(self, thread_id: str, labels: Iterable[str]) -> None:
        self.connection.execute("DELETE FROM thread_labels WHERE thread_id=?", (thread_id,))
        self.connection.executemany(
            "INSERT INTO thread_labels(thread_id, label) VALUES (?, ?)",
            ((thread_id, label) for label in sorted(set(labels))),
        )
        self.connection.commit()
