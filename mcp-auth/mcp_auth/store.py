"""SQLite storage for clients, authorization codes and refresh tokens.

Secrets are never stored in the clear: refresh tokens are kept as
SHA-256 digests, so a copy of this database does not hand anyone a
working token. Authorization codes are short-lived and single-use.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id        TEXT PRIMARY KEY,
    client_secret    TEXT,
    metadata         TEXT NOT NULL,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code             TEXT PRIMARY KEY,
    client_id        TEXT NOT NULL,
    redirect_uri     TEXT,
    scope            TEXT,
    resource         TEXT,
    code_challenge   TEXT,
    code_challenge_method TEXT,
    subject          TEXT NOT NULL,
    nonce            TEXT,
    expires_at       INTEGER NOT NULL,
    used             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash       TEXT PRIMARY KEY,
    client_id        TEXT NOT NULL,
    scope            TEXT,
    resource         TEXT,
    subject          TEXT NOT NULL,
    issued_at        INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL,
    revoked          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    at               INTEGER NOT NULL,
    ok               INTEGER NOT NULL
);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- clients (Dynamic Client Registration) ----------

    def add_client(self, client_id: str, client_secret: str | None, metadata: dict):
        self.conn.execute(
            "INSERT INTO clients (client_id, client_secret, metadata, created_at) "
            "VALUES (?, ?, ?, ?)",
            (client_id, client_secret, json.dumps(metadata), int(time.time())),
        )
        self.conn.commit()

    def get_client(self, client_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "client_secret": row["client_secret"],
            "metadata": json.loads(row["metadata"]),
            "created_at": row["created_at"],
        }

    def list_clients(self) -> list[dict]:
        """Clients with their token counts.

        Dynamic Client Registration has no idempotency: a client that
        re-runs discovery registers again, so this list grows with every
        reconnect. The token counts are what distinguish the live
        registration from the leftovers.
        """
        rows = self.conn.execute(
            """
            SELECT c.client_id, c.created_at, c.metadata,
                   count(r.token_hash) AS tokens,
                   coalesce(sum(CASE WHEN r.revoked = 0 THEN 1 ELSE 0 END), 0)
                       AS active
            FROM clients c
            LEFT JOIN refresh_tokens r ON r.client_id = c.client_id
            GROUP BY c.client_id
            ORDER BY c.created_at DESC
            """
        )
        return [
            {
                "client_id": r["client_id"],
                "created_at": r["created_at"],
                "metadata": json.loads(r["metadata"]),
                "tokens": int(r["tokens"]),
                "active_tokens": int(r["active"]),
            }
            for r in rows
        ]

    def prunable_clients(self, min_age_seconds: int = 0) -> list[dict]:
        """Registrations with no live token, old enough to be leftovers.

        Deliberately conservative: anything holding an unrevoked refresh
        token is a working connection and is never offered for deletion.
        """
        cutoff = int(time.time()) - min_age_seconds
        return [
            c
            for c in self.list_clients()
            if c["active_tokens"] == 0 and c["created_at"] <= cutoff
        ]

    def delete_client(self, client_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM clients WHERE client_id = ?", (client_id,)
        )
        self.conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE client_id = ?",
            (client_id,),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    # ---------- authorization codes ----------

    def add_code(self, **kw) -> None:
        self.conn.execute(
            "INSERT INTO auth_codes (code, client_id, redirect_uri, scope, "
            "resource, code_challenge, code_challenge_method, subject, nonce, "
            "expires_at) VALUES (:code, :client_id, :redirect_uri, :scope, "
            ":resource, :code_challenge, :code_challenge_method, :subject, "
            ":nonce, :expires_at)",
            kw,
        )
        self.conn.commit()

    def take_code(self, code: str) -> dict | None:
        """Fetch a code and immediately burn it.

        Single use is enforced here rather than at the caller, so a
        replayed code can never mint a second token even if two requests
        arrive at once.
        """
        row = self.conn.execute(
            "SELECT * FROM auth_codes WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return None
        cur = self.conn.execute(
            "UPDATE auth_codes SET used = 1 WHERE code = ? AND used = 0", (code,)
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None  # already used
        if row["expires_at"] < int(time.time()):
            return None
        return dict(row)

    def purge_expired(self) -> None:
        now = int(time.time())
        self.conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now - 600,))
        self.conn.execute(
            "DELETE FROM refresh_tokens WHERE expires_at < ?", (now,)
        )
        self.conn.commit()

    # ---------- refresh tokens ----------

    def add_refresh_token(
        self, token: str, client_id: str, scope: str, resource: str,
        subject: str, ttl: int,
    ) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO refresh_tokens (token_hash, client_id, scope, "
            "resource, subject, issued_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (digest(token), client_id, scope, resource, subject, now, now + ttl),
        )
        self.conn.commit()

    def get_refresh_token(self, token: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (digest(token),)
        ).fetchone()
        if row is None or row["revoked"] or row["expires_at"] < int(time.time()):
            return None
        return dict(row)

    def revoke_refresh_token(self, token: str) -> None:
        self.conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
            (digest(token),),
        )
        self.conn.commit()

    def revoke_all(self) -> int:
        cur = self.conn.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE revoked = 0"
        )
        self.conn.commit()
        return cur.rowcount or 0

    # ---------- login throttling ----------

    def record_login(self, ok: bool) -> None:
        self.conn.execute(
            "INSERT INTO login_attempts (at, ok) VALUES (?, ?)",
            (int(time.time()), 1 if ok else 0),
        )
        self.conn.commit()

    def recent_failures(self, window_seconds: int) -> int:
        since = int(time.time()) - window_seconds
        row = self.conn.execute(
            "SELECT count(*) AS n FROM login_attempts WHERE at > ? AND ok = 0",
            (since,),
        ).fetchone()
        return int(row["n"])

    def clear_failures(self) -> None:
        self.conn.execute("DELETE FROM login_attempts WHERE ok = 0")
        self.conn.commit()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
