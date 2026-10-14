"""Header-only search index.

This is the "hybrid" half of the design. It stores **only** envelope
data — subject, sender, recipients, date, message-id, flags. Message
bodies are never written to disk; body search always goes live to the
provider. That keeps the local footprint to something you can describe
in a deletion policy in one sentence.

Storage is SQLite FTS5 with the unicode61 tokenizer and diacritic
folding, so "Grundstuck" finds "Grundstück".
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS headers_fts USING fts5(
    subject,
    sender,
    recipients,
    account UNINDEXED,
    folder UNINDEXED,
    uid UNINDEXED,
    date UNINDEXED,
    message_id UNINDEXED,
    flags UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS fetch_failures (
    account   TEXT NOT NULL,
    folder    TEXT NOT NULL,
    uid       INTEGER NOT NULL,
    last_seen TEXT,
    PRIMARY KEY (account, folder, uid)
);

CREATE TABLE IF NOT EXISTS folder_state (
    account      TEXT NOT NULL,
    folder       TEXT NOT NULL,
    uidvalidity  INTEGER,
    max_uid      INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    last_sync    TEXT,
    PRIMARY KEY (account, folder)
);
"""


def escape_fts(query: str) -> str:
    """Turn a user query into a safe FTS5 MATCH expression.

    Every term becomes a quoted string, which disables FTS5 operator
    parsing. Terms are implicitly ANDed. A trailing ``*`` survives as a
    prefix search because it sits outside the quotes.
    """
    from .search import split_terms

    parts = []
    for term in split_terms(query):
        prefix = term.endswith("*")
        core = term[:-1] if prefix else term
        core = core.replace('"', '""')
        if not core:
            continue
        parts.append(f'"{core}"*' if prefix else f'"{core}"')
    return " ".join(parts)


class HeaderIndex:
    """SQLite-backed store of message envelopes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # The server and the indexer are separate processes sharing this
        # file. WAL lets the reader work while the writer commits, and
        # busy_timeout absorbs the brief overlaps instead of raising
        # "database is locked".
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- folder bookkeeping ----------

    def folder_state(self, account: str, folder: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM folder_state WHERE account = ? AND folder = ?",
            (account, folder),
        )
        return cur.fetchone()

    def reset_folder(self, account: str, folder: str) -> None:
        """Drop everything we know about a folder (UIDVALIDITY changed)."""
        self._conn.execute(
            "DELETE FROM headers_fts WHERE account = ? AND folder = ?",
            (account, folder),
        )
        self._conn.execute(
            "DELETE FROM folder_state WHERE account = ? AND folder = ?",
            (account, folder),
        )
        self._conn.commit()

    def record_sync(
        self,
        account: str,
        folder: str,
        uidvalidity: int,
        max_uid: int,
        message_count: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO folder_state
                (account, folder, uidvalidity, max_uid, message_count, last_sync)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, folder) DO UPDATE SET
                uidvalidity = excluded.uidvalidity,
                max_uid = excluded.max_uid,
                message_count = excluded.message_count,
                last_sync = excluded.last_sync
            """,
            (
                account,
                folder,
                uidvalidity,
                max_uid,
                message_count,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    # ---------- writing ----------

    def add_headers(self, rows: list[dict]) -> int:
        """Insert envelope rows. Existing (account, folder, uid) are replaced."""
        if not rows:
            return 0

        # FTS5 has no UNIQUE constraint, so clear any prior copy first.
        for row in rows:
            self._conn.execute(
                "DELETE FROM headers_fts "
                "WHERE account = ? AND folder = ? AND uid = ?",
                (row["account"], row["folder"], row["uid"]),
            )

        self._conn.executemany(
            """
            INSERT INTO headers_fts
                (subject, sender, recipients, account, folder, uid,
                 date, message_id, flags)
            VALUES (:subject, :sender, :recipients, :account, :folder, :uid,
                    :date, :message_id, :flags)
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    def record_failures(self, account: str, folder: str, uids: list[int]) -> None:
        """Remember messages the server would not hand over.

        These are the holes in the index. They are kept so index_status
        can name them instead of leaving the index quietly incomplete.
        """
        if not uids:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.executemany(
            "INSERT INTO fetch_failures (account, folder, uid, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(account, folder, uid) DO UPDATE SET "
            "last_seen = excluded.last_seen",
            [(account, folder, int(u), stamp) for u in uids],
        )
        self._conn.commit()

    def clear_failures(self, account: str, folder: str) -> None:
        self._conn.execute(
            "DELETE FROM fetch_failures WHERE account = ? AND folder = ?",
            (account, folder),
        )
        self._conn.commit()

    def failures(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT account, folder, count(*) AS n, max(last_seen) AS last_seen "
            "FROM fetch_failures GROUP BY account, folder ORDER BY n DESC"
        )
        return [
            {
                "account": r["account"],
                "folder": r["folder"],
                "unreadable_messages": r["n"],
                "last_seen": r["last_seen"],
            }
            for r in cur
        ]

    def remove_uids(self, account: str, folder: str, uids: list[int]) -> int:
        """Drop specific messages from the index.

        Used after a move: only the messages that actually left the
        folder are removed. Wiping the whole folder would force a full
        re-index of up to index_max_per_folder headers for the sake of
        one message.
        """
        if not uids:
            return 0
        placeholders = ",".join("?" * len(uids))
        cur = self._conn.execute(
            f"DELETE FROM headers_fts WHERE account = ? AND folder = ? "
            f"AND uid IN ({placeholders})",
            [account, folder, *[int(u) for u in uids]],
        )
        self._conn.commit()
        return cur.rowcount or 0

    def update_flags(
        self, account: str, folder: str, uids: list[int], flags: str
    ) -> int:
        """Refresh the stored IMAP flags for specific messages.

        Without this, a message marked read would keep showing as
        unread in header-scope search results until the next sync.
        """
        if not uids:
            return 0
        placeholders = ",".join("?" * len(uids))
        cur = self._conn.execute(
            f"UPDATE headers_fts SET flags = ? WHERE account = ? AND folder = ? "
            f"AND uid IN ({placeholders})",
            [flags, account, folder, *[int(u) for u in uids]],
        )
        self._conn.commit()
        return cur.rowcount or 0

    def stored_flags(self, account: str, folder: str, uid: int) -> str | None:
        row = self._conn.execute(
            "SELECT flags FROM headers_fts WHERE account = ? AND folder = ? "
            "AND uid = ?",
            (account, folder, int(uid)),
        ).fetchone()
        return row["flags"] if row else None

    # ---------- reading ----------

    def search(
        self,
        query: str,
        accounts: list[str] | None = None,
        folders: list[str] | None = None,
        limit: int = 20,
        since: str | None = None,
        before: str | None = None,
        from_addr: str | None = None,
    ) -> list[dict]:
        match = escape_fts(query)
        if not match:
            return []

        sql = [
            "SELECT subject, sender, recipients, account, folder, uid,",
            "       date, message_id, flags, rank",
            "FROM headers_fts WHERE headers_fts MATCH ?",
        ]
        params: list = [match]

        if accounts:
            sql.append(
                "AND account IN (" + ",".join("?" * len(accounts)) + ")"
            )
            params.extend(accounts)
        if folders:
            sql.append("AND folder IN (" + ",".join("?" * len(folders)) + ")")
            params.extend(folders)

        # Dates are stored as ISO-8601, so a lexicographic comparison
        # against a bare YYYY-MM-DD is also a chronological one.
        # Rows with an empty date are excluded once a date filter is in
        # play — better to omit an undatable message than to present it
        # as if it fell inside the window.
        if since:
            sql.append("AND date >= ? AND date != ''")
            params.append(since)
        if before:
            sql.append("AND date < ? AND date != ''")
            params.append(before)
        if from_addr:
            sql.append("AND sender LIKE ?")
            params.append(f"%{from_addr}%")

        sql.append("ORDER BY rank LIMIT ?")
        params.append(limit)

        try:
            cur = self._conn.execute(" ".join(sql), params)
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Invalid search expression: {exc}") from exc

        results = []
        for row in cur:
            flags = (row["flags"] or "").split(",")
            results.append(
                {
                    "uid": row["uid"],
                    "account": row["account"],
                    "folder": row["folder"],
                    "subject": row["subject"],
                    "from": row["sender"],
                    "to": row["recipients"],
                    "date": row["date"],
                    "message_id": row["message_id"],
                    "read": "\\Seen" in flags,
                    "flagged": "\\Flagged" in flags,
                    "source": "index",
                    "score": -float(row["rank"]) if row["rank"] is not None else 0.0,
                }
            )
        return results

    def stats(self) -> dict:
        total = self._conn.execute(
            "SELECT count(*) AS n FROM headers_fts"
        ).fetchone()["n"]
        folders = self._conn.execute(
            "SELECT account, folder, message_count, last_sync "
            "FROM folder_state ORDER BY account, folder"
        ).fetchall()
        size_mb = (
            self.path.stat().st_size / (1024 * 1024) if self.path.exists() else 0.0
        )
        last = [f["last_sync"] for f in folders if f["last_sync"]]
        failures = self.failures()
        return {
            "indexed_headers": total,
            "folders": len(folders),
            "database_size_mb": round(size_mb, 2),
            "last_sync": max(last) if last else None,
            "unreadable_messages": sum(f["unreadable_messages"] for f in failures),
            "gaps": failures,
            "per_folder": [
                {
                    "account": f["account"],
                    "folder": f["folder"],
                    "messages": f["message_count"],
                    "last_sync": f["last_sync"],
                }
                for f in folders
            ],
        }
