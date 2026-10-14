"""Filling the header index from IMAP.

Incremental by UID: after the first pass, each run asks only for
``UID <last+1>:*``, which is a handful of round-trips even on a mailbox
with tens of thousands of messages.

UIDVALIDITY is checked on every folder. If the server changed it, the
old UIDs are meaningless and that folder is rebuilt from scratch —
otherwise stale rows would point at the wrong messages.
"""

from __future__ import annotations

import logging
from typing import Callable

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from .config import Account, Config
from .index import HeaderIndex
from .messages import decode_mime, format_addresses
from .pool import AccountSession

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

FETCH_KEYS = ["ENVELOPE", "FLAGS", "INTERNALDATE"]


def selectable_folders(client: IMAPClient, account: Account) -> list[str]:
    """List folders worth indexing, skipping containers and excluded names."""
    folders = []
    for flags, _delimiter, name in client.list_folders():
        flag_names = {
            f.decode("ascii", errors="replace") if isinstance(f, bytes) else str(f)
            for f in flags
        }
        if "\\Noselect" in flag_names or "\\NonExistent" in flag_names:
            continue
        if account.excluded(name):
            continue
        folders.append(name)
    return folders


def _envelope_rows(
    account: str, folder: str, fetched: dict
) -> list[dict]:
    rows = []
    for uid, data in fetched.items():
        envelope = data.get(b"ENVELOPE")
        if envelope is None:
            continue
        flags = data.get(b"FLAGS") or ()
        flag_str = ",".join(
            f.decode("ascii", errors="replace") if isinstance(f, bytes) else str(f)
            for f in flags
        )
        date = getattr(envelope, "date", None) or data.get(b"INTERNALDATE")
        rows.append(
            {
                "subject": decode_mime(getattr(envelope, "subject", b"")),
                "sender": format_addresses(getattr(envelope, "from_", None)),
                "recipients": format_addresses(getattr(envelope, "to", None)),
                "account": account,
                "folder": folder,
                "uid": int(uid),
                "date": date.isoformat() if hasattr(date, "isoformat") else "",
                "message_id": decode_mime(getattr(envelope, "message_id", b"")),
                "flags": flag_str,
            }
        )
    return rows


def _fetch_adaptive(
    client: IMAPClient, uids: list[int]
) -> tuple[dict, list[int]]:
    """Fetch envelopes, isolating the messages the server chokes on.

    iCloud in particular answers some FETCH requests with output that
    violates the IMAP grammar. Failing the whole batch would silently
    drop up to `fetch_batch_size` messages — that is how a search index
    ends up with holes nobody notices.

    So on failure the range is bisected until the offending messages are
    isolated to single UIDs. Everything around them still gets indexed,
    and the genuinely unreadable UIDs are returned to the caller instead
    of vanishing.
    """
    if not uids:
        return {}, []

    try:
        return client.fetch(uids, FETCH_KEYS), []
    except (IMAPClientError, OSError) as exc:
        if len(uids) == 1:
            logger.debug("UID %s is unreadable: %s", uids[0], exc)
            return {}, list(uids)

    mid = len(uids) // 2
    left_data, left_failed = _fetch_adaptive(client, uids[:mid])
    right_data, right_failed = _fetch_adaptive(client, uids[mid:])
    left_data.update(right_data)
    return left_data, left_failed + right_failed


def sync_folder(
    session: AccountSession,
    client: IMAPClient,
    index: HeaderIndex,
    account: Account,
    folder: str,
    cfg: Config,
    progress: ProgressFn | None = None,
) -> int:
    """Bring one folder's headers up to date. Returns rows added."""
    info = session.select(client, folder, write=False)
    if not info:
        # Already selected in this session; ask for the numbers explicitly.
        info = client.folder_status(folder, ["UIDVALIDITY", "MESSAGES"])
        uidvalidity = int(info.get(b"UIDVALIDITY", 0))
        exists = int(info.get(b"MESSAGES", 0))
    else:
        uidvalidity = int(info.get(b"UIDVALIDITY", 0))
        exists = int(info.get(b"EXISTS", 0))

    state = index.folder_state(account.name, folder)
    last_max = 0
    if state is not None:
        if state["uidvalidity"] != uidvalidity:
            logger.info(
                "UIDVALIDITY changed for %s/%s — rebuilding", account.name, folder
            )
            index.reset_folder(account.name, folder)
        else:
            last_max = int(state["max_uid"] or 0)

    if last_max:
        uids = client.search(["UID", f"{last_max + 1}:*"])
        # IMAP returns the highest UID even when nothing is new.
        uids = [u for u in uids if int(u) > last_max]
    else:
        uids = client.search(["NOT", "DELETED"])
        if len(uids) > cfg.index_max_per_folder:
            uids = sorted(int(u) for u in uids)[-cfg.index_max_per_folder :]

    uids = sorted(int(u) for u in uids)
    if not uids:
        index.record_sync(account.name, folder, uidvalidity, last_max, exists)
        return 0, 0

    added = 0
    failed: list[int] = []
    batch = account.fetch_batch_size or cfg.fetch_batch_size
    for start in range(0, len(uids), batch):
        chunk = uids[start : start + batch]
        fetched, chunk_failed = _fetch_adaptive(client, chunk)
        failed.extend(chunk_failed)
        rows = _envelope_rows(account.name, folder, fetched)
        added += index.add_headers(rows)
        if progress:
            progress(f"{account.name}/{folder}: {added}/{len(uids)}")

    if failed:
        # Never let a fetch failure disappear into the log. These UIDs are
        # recorded so index_status can report exactly what is missing.
        logger.warning(
            "%s/%s: %d message(s) could not be fetched: %s",
            account.name,
            folder,
            len(failed),
            ", ".join(str(u) for u in failed[:20])
            + ("…" if len(failed) > 20 else ""),
        )
        index.record_failures(account.name, folder, failed)
    else:
        index.clear_failures(account.name, folder)

    index.record_sync(
        account.name, folder, uidvalidity, max(uids[-1], last_max), exists
    )
    return added, len(failed)


def sync_account(
    session: AccountSession,
    index: HeaderIndex,
    account: Account,
    cfg: Config,
    progress: ProgressFn | None = None,
) -> dict:
    """Sync every selectable folder of one account."""
    added = 0
    unreadable = 0
    folders_done = 0
    errors: list[str] = []

    with session.session() as client:
        for folder in selectable_folders(client, account):
            try:
                folder_added, folder_failed = sync_folder(
                    session, client, index, account, folder, cfg, progress
                )
                added += folder_added
                unreadable += folder_failed
                folders_done += 1
            except (IMAPClientError, OSError) as exc:
                errors.append(f"{folder}: {exc}")
                logger.warning("Sync failed for %s/%s: %s", account.name, folder, exc)

    return {
        "account": account.name,
        "folders": folders_done,
        "headers_added": added,
        "unreadable_messages": unreadable,
        "errors": errors,
    }
