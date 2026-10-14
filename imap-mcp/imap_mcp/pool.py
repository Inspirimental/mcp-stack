"""IMAP connection handling.

One long-lived connection per account, guarded by a lock. IMAP
connections are stateful (a selected folder) and emphatically not
thread-safe, so every operation goes through ``session()``.

Reads always select folders with ``readonly=True`` and fetch bodies with
``BODY.PEEK[]`` so that merely looking at a message never marks it as
read. Only the explicit write tools re-select a folder writable.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from .config import Account

logger = logging.getLogger(__name__)


class ConnectionError_(RuntimeError):
    """Raised when an account cannot be reached or authenticated."""


class AccountSession:
    """A reusable, lock-guarded IMAP connection for one account."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self._lock = threading.RLock()
        self._client: IMAPClient | None = None
        self._selected: tuple[str, bool] | None = None

    # ---------- connection lifecycle ----------

    def _connect(self) -> IMAPClient:
        acct = self.account
        try:
            client = IMAPClient(
                acct.host,
                port=acct.port,
                ssl=acct.ssl,
                timeout=acct.timeout,
                use_uid=True,
            )
            client.login(acct.username, acct.password)
        except IMAPClientError as exc:
            raise ConnectionError_(
                f"{acct.name}: IMAP login failed ({exc}). Check the app "
                f"password in the environment and that IMAP is enabled."
            ) from exc
        except OSError as exc:
            raise ConnectionError_(
                f"{acct.name}: cannot reach {acct.host}:{acct.port} ({exc})."
            ) from exc

        logger.info("Connected to %s as %s", acct.host, acct.username)
        self._selected = None
        return client

    def _healthy(self, client: IMAPClient) -> bool:
        try:
            client.noop()
            return True
        except Exception:
            return False

    @contextmanager
    def session(self) -> Iterator[IMAPClient]:
        """Yield a live client, reconnecting once if the socket went stale."""
        with self._lock:
            if self._client is None or not self._healthy(self._client):
                if self._client is not None:
                    logger.info("Reconnecting to %s", self.account.host)
                    self.close()
                self._client = self._connect()
            try:
                yield self._client
            except (IMAPClientError, OSError):
                # Drop the connection so the next call starts clean.
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.logout()
                except Exception:
                    pass
                self._client = None
                self._selected = None

    # ---------- folder selection ----------

    def select(self, client: IMAPClient, folder: str, *, write: bool = False) -> dict:
        """Select a folder, skipping the round-trip if already selected.

        ``write=False`` selects read-only, which prevents the server from
        clearing \\Recent flags or otherwise mutating state.
        """
        want = (folder, write)
        if self._selected == want:
            return {}
        info = client.select_folder(folder, readonly=not write)
        self._selected = want
        return info


class SessionRegistry:
    """Holds one AccountSession per configured account."""

    def __init__(self, accounts: list[Account]) -> None:
        self._sessions = {a.name.lower(): AccountSession(a) for a in accounts}

    def get(self, account: Account) -> AccountSession:
        return self._sessions[account.name.lower()]

    def close_all(self) -> None:
        for session in self._sessions.values():
            session.close()
