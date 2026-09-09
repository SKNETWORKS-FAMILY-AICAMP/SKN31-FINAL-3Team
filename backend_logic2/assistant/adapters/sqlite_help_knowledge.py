"""Small local help corpus indexed with SQLite FTS5."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from ..models import HelpMatch


_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_-]+")


def _safe_match_query(query: str) -> str:
    tokens = [token for token in _TOKEN_PATTERN.findall(query) if len(token) > 1]
    return " OR ".join(f'"{token}"' for token in tokens[:12])


class SQLiteHelpKnowledge:
    """Build a disposable FTS index from reviewed, versioned help articles."""

    def __init__(self, source_path: str | Path, database_path: str | Path):
        self.source_path = Path(source_path)
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_index()

    def _source_digest(self) -> str:
        return hashlib.sha256(self.source_path.read_bytes()).hexdigest()

    def _ensure_index(self) -> None:
        # JSON remains the source of truth. Rebuild the runtime-only index when
        # its digest changes instead of maintaining another editable database.
        digest = self._source_digest()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'source_digest'"
            ).fetchone()
            if row and row[0] == digest:
                return
            connection.execute("DROP TABLE IF EXISTS help_fts")
            connection.execute(
                "CREATE VIRTUAL TABLE help_fts USING fts5("
                "id UNINDEXED, title, keywords, content, target UNINDEXED, "
                "tokenize='unicode61')"
            )
            entries = json.loads(self.source_path.read_text(encoding="utf-8"))
            for entry in entries:
                connection.execute(
                    "INSERT INTO help_fts(id, title, keywords, content, target) VALUES (?, ?, ?, ?, ?)",
                    (
                        entry["id"],
                        entry["title"],
                        " ".join(entry.get("keywords", [])),
                        entry["content"],
                        entry.get("target"),
                    ),
                )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('source_digest', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (digest,),
            )
            connection.commit()

    def search(self, query: str, *, limit: int = 4) -> list[HelpMatch]:
        match_query = _safe_match_query(query)
        if not match_query:
            return []
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, title, content, target, bm25(help_fts) AS rank
                FROM help_fts
                WHERE help_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_query, max(1, min(limit, 10))),
            ).fetchall()
        return [
            HelpMatch(
                id=row["id"],
                title=row["title"],
                content=row["content"],
                target=row["target"] or None,
                score=float(-row["rank"]),
            )
            for row in rows
        ]
