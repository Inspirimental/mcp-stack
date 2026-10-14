"""MCP tools over one or more IMAP mailboxes.

Read tools never mutate mailbox state: folders are selected read-only
and bodies are fetched with BODY.PEEK, so reading a message does not
mark it as seen. The two state-changing tools (``mark`` and ``move``)
refuse to run unless the account sets ``writable = true``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from imapclient.exceptions import IMAPClientError
from pydantic import Field

from .config import Config, ConfigError, load_config
from .index import HeaderIndex
from .messages import (
    envelope_to_dict,
    extract_attachment,
    parse_full_message,
    snippet,
)
from .pool import AccountSession, ConnectionError_, SessionRegistry
from .search import build_criteria
from .sync import selectable_folders, sync_account

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP(
    name="imap-mcp",
    instructions=(
        "Search and read email across several IMAP mailboxes at once. "
        "Prefer scope='headers' for fast sender/subject lookups; use "
        "scope='body' or 'all' when the wording inside a message matters "
        "— those run live against the mail provider and take a few "
        "seconds. Message bodies are never stored locally."
    ),
)

# Populated by main(); module-level so the tools can reach them.
_config: Config | None = None
_registry: SessionRegistry | None = None
_index: HeaderIndex | None = None

# Attachments larger than this are described but not returned inline.
MAX_INLINE_ATTACHMENT = 5 * 1024 * 1024


def configure(config: Config) -> None:
    """Wire up the shared state. Called once at startup."""
    global _config, _registry, _index
    _config = config
    _registry = SessionRegistry(config.accounts)
    _index = HeaderIndex(config.index_path)


def _state() -> tuple[Config, SessionRegistry, HeaderIndex]:
    if _config is None or _registry is None or _index is None:
        raise RuntimeError("Server not configured; call configure() first.")
    return _config, _registry, _index


def _require_writable(account: Any, operation: str) -> None:
    if not account.writable:
        raise ValueError(
            f"Account {account.name!r} is read-only, so {operation} is "
            f"refused. Set writable = true for that account in the config "
            f"and restart the server if you want to allow it."
        )


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------


@mcp.tool
async def list_accounts() -> list[dict]:
    """List the configured mailboxes and whether each allows writes.

    Returns one entry per account with its name, server, login and
    write permission. Use the names as the 'accounts' argument to
    search, or as 'account' elsewhere.
    """
    config, _, _ = _state()
    return [
        {
            "name": a.name,
            "host": a.host,
            "username": a.username,
            "writable": a.writable,
            "default_folder": a.default_folder,
        }
        for a in config.accounts
    ]


@mcp.tool
async def list_folders(
    account: Annotated[
        str | None,
        Field(description="Account name; omit to list folders of all accounts."),
    ] = None,
) -> list[dict]:
    """List mailbox folders, with message and unread counts.

    Folder names are what 'search', 'get_email' and 'move' expect.
    """
    config, registry, _ = _state()
    accounts = config.resolve([account] if account else None)

    def collect(acct) -> list[dict]:
        session = registry.get(acct)
        out = []
        with session.session() as client:
            for folder in selectable_folders(client, acct):
                try:
                    status = client.folder_status(
                        folder, ["MESSAGES", "UNSEEN"]
                    )
                    out.append(
                        {
                            "account": acct.name,
                            "folder": folder,
                            "messages": int(status.get(b"MESSAGES", 0)),
                            "unread": int(status.get(b"UNSEEN", 0)),
                        }
                    )
                except IMAPClientError as exc:
                    logger.warning("Status failed for %s: %s", folder, exc)
        return out

    results = await asyncio.gather(
        *(asyncio.to_thread(collect, a) for a in accounts),
        return_exceptions=True,
    )

    folders: list[dict] = []
    for acct, result in zip(accounts, results):
        if isinstance(result, BaseException):
            folders.append({"account": acct.name, "error": str(result)})
        else:
            folders.extend(result)
    return folders


@mcp.tool
async def search(
    query: Annotated[
        str,
        Field(description="Search words. Multiple words are ANDed; "
                          'use "quotes" for an exact phrase.'),
    ],
    scope: Annotated[
        Literal["headers", "all", "body", "subject", "sender", "recipient"],
        Field(description=(
            "'headers' uses the local subject/sender index and answers in "
            "milliseconds. 'all', 'body', 'subject', 'sender' and "
            "'recipient' run live IMAP searches on the provider — slower "
            "but current, and the only way to search message text."
        )),
    ] = "headers",
    accounts: Annotated[
        list[str] | None,
        Field(description="Limit to these accounts; omit to search all."),
    ] = None,
    folders: Annotated[
        list[str] | None,
        Field(description=(
            "Limit to these folder names. Strongly recommended for live "
            "scopes — searching every folder of every account means one "
            "round-trip per folder."
        )),
    ] = None,
    since: Annotated[
        str | None, Field(description="Only mail on/after this date (YYYY-MM-DD).")
    ] = None,
    before: Annotated[
        str | None, Field(description="Only mail before this date (YYYY-MM-DD).")
    ] = None,
    from_addr: Annotated[
        str | None, Field(description="Extra filter on the sender.")
    ] = None,
    unread_only: Annotated[bool, Field(description="Only unread mail.")] = False,
    flagged_only: Annotated[bool, Field(description="Only flagged mail.")] = False,
    limit: Annotated[int, Field(description="Maximum results.", ge=1, le=200)] = 20,
) -> dict:
    """Search across every configured mailbox at once.

    Returns matches with account, folder and uid — pass those three to
    'get_email' to read the full message.
    """
    config, registry, index = _state()
    targets = config.resolve(accounts)

    if scope == "headers":
        # Every narrowing argument has to reach the index too. Applying
        # them only on the live path would mean a date-filtered header
        # search silently returned mail from outside the window.
        rows = index.search(
            query,
            accounts=[a.name for a in targets],
            folders=folders,
            limit=limit,
            since=since,
            before=before,
            from_addr=from_addr,
        )
        if unread_only:
            rows = [r for r in rows if not r["read"]]
        if flagged_only:
            rows = [r for r in rows if r["flagged"]]
        return {
            "results": rows[:limit],
            "source": "local header index",
            "note": (
                "Subject and sender only. Re-run with scope='body' to "
                "search inside messages."
            )
            if not rows
            else None,
        }

    criteria, non_ascii = build_criteria(
        query,
        scope=scope,
        since=since,
        before=before,
        from_addr=from_addr,
        unread_only=unread_only,
        flagged_only=flagged_only,
    )

    def run(acct) -> list[dict]:
        session: AccountSession = registry.get(acct)
        found: list[dict] = []
        with session.session() as client:
            targets_folders = folders or selectable_folders(client, acct)
            for folder in targets_folders:
                if acct.excluded(folder):
                    continue
                try:
                    session.select(client, folder, write=False)
                    uids = client.search(
                        criteria, charset="UTF-8" if non_ascii else None
                    )
                except IMAPClientError as exc:
                    logger.warning("Search failed in %s/%s: %s", acct.name, folder, exc)
                    continue
                if not uids:
                    continue
                newest = sorted(int(u) for u in uids)[-limit:]
                try:
                    fetched = client.fetch(
                        newest, ["ENVELOPE", "FLAGS", "INTERNALDATE", "RFC822.SIZE"]
                    )
                except IMAPClientError as exc:
                    logger.warning("Fetch failed in %s/%s: %s", acct.name, folder, exc)
                    continue
                for uid, data in fetched.items():
                    envelope = data.get(b"ENVELOPE")
                    if envelope is None:
                        continue
                    row = envelope_to_dict(
                        int(uid),
                        acct.name,
                        folder,
                        envelope,
                        flags=data.get(b"FLAGS", ()),
                        internaldate=data.get(b"INTERNALDATE"),
                        size=data.get(b"RFC822.SIZE"),
                    )
                    row["source"] = "live"
                    found.append(row)
        return found

    gathered = await asyncio.gather(
        *(asyncio.to_thread(run, a) for a in targets),
        return_exceptions=True,
    )

    results: list[dict] = []
    errors: list[str] = []
    for acct, item in zip(targets, gathered):
        if isinstance(item, BaseException):
            errors.append(f"{acct.name}: {item}")
        else:
            results.extend(item)

    results.sort(key=lambda r: r.get("date") or "", reverse=True)
    payload: dict = {
        "results": results[:limit],
        "source": f"live IMAP search ({scope})",
        "searched_accounts": [a.name for a in targets],
    }
    if errors:
        payload["errors"] = errors
    return payload


@mcp.tool
async def get_email(
    account: Annotated[str, Field(description="Account name from the search result.")],
    folder: Annotated[str, Field(description="Folder name from the search result.")],
    uid: Annotated[int, Field(description="UID from the search result.")],
) -> dict:
    """Read one message in full, including body text and attachment list.

    Fetched with BODY.PEEK, so this does not mark the message as read.
    """
    config, registry, _ = _state()
    acct = config.account(account)
    session = registry.get(acct)

    def fetch_one() -> dict:
        with session.session() as client:
            session.select(client, folder, write=False)
            data = client.fetch([uid], ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"])
            if uid not in data:
                raise ValueError(
                    f"No message with UID {uid} in {account}/{folder}. "
                    "It may have been moved or deleted; search again."
                )
            entry = data[uid]
            raw = entry.get(b"BODY[]") or entry.get(b"RFC822") or b""
            parsed = parse_full_message(raw)
            flags = [
                f.decode("ascii", errors="replace") if isinstance(f, bytes) else str(f)
                for f in entry.get(b"FLAGS", ())
            ]
            parsed.update(
                {
                    "account": account,
                    "folder": folder,
                    "uid": uid,
                    "read": "\\Seen" in flags,
                    "flagged": "\\Flagged" in flags,
                }
            )
            return parsed

    return await asyncio.to_thread(fetch_one)


@mcp.tool
async def get_attachment(
    account: Annotated[str, Field(description="Account name.")],
    folder: Annotated[str, Field(description="Folder name.")],
    uid: Annotated[int, Field(description="Message UID.")],
    filename: Annotated[
        str, Field(description="Exact filename as reported by get_email.")
    ],
) -> dict:
    """Fetch one attachment, returned base64-encoded.

    Files above 5 MB are described but not transferred inline.
    """
    config, registry, _ = _state()
    acct = config.account(account)
    session = registry.get(acct)

    def fetch_attachment() -> dict:
        with session.session() as client:
            session.select(client, folder, write=False)
            data = client.fetch([uid], ["BODY.PEEK[]"])
            if uid not in data:
                raise ValueError(f"No message with UID {uid} in {account}/{folder}.")
            raw = data[uid].get(b"BODY[]") or b""
            found = extract_attachment(raw, filename)
            if found is None:
                raise ValueError(
                    f"No attachment named {filename!r} on that message. "
                    "Call get_email first to see the exact filenames."
                )
            payload, mime_type = found
            if len(payload) > MAX_INLINE_ATTACHMENT:
                return {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": len(payload),
                    "content": None,
                    "note": "Too large to return inline (limit 5 MB).",
                }
            return {
                "filename": filename,
                "mime_type": mime_type,
                "size": len(payload),
                "content_base64": base64.b64encode(payload).decode("ascii"),
            }

    return await asyncio.to_thread(fetch_attachment)


# --------------------------------------------------------------------------
# Index management
# --------------------------------------------------------------------------


@mcp.tool
async def index_status() -> dict:
    """Report how fresh the local header index is, per folder."""
    _, _, index = _state()
    return await asyncio.to_thread(index.stats)


@mcp.tool
async def refresh_index(
    accounts: Annotated[
        list[str] | None,
        Field(description="Accounts to refresh; omit for all."),
    ] = None,
) -> dict:
    """Pull new message headers into the local index.

    Incremental: only messages newer than the last run are fetched.
    Only envelopes are stored — never message bodies.
    """
    config, registry, index = _state()
    targets = config.resolve(accounts)

    results = await asyncio.gather(
        *(
            asyncio.to_thread(sync_account, registry.get(a), index, a, config)
            for a in targets
        ),
        return_exceptions=True,
    )

    summary = []
    for acct, item in zip(targets, results):
        if isinstance(item, BaseException):
            summary.append({"account": acct.name, "error": str(item)})
        else:
            summary.append(item)
    return {"accounts": summary}


# --------------------------------------------------------------------------
# Write tools — guarded by the per-account 'writable' switch
# --------------------------------------------------------------------------


@mcp.tool
async def mark(
    account: Annotated[str, Field(description="Account name.")],
    folder: Annotated[str, Field(description="Folder name.")],
    uids: Annotated[list[int], Field(description="Message UIDs to change.")],
    action: Annotated[
        Literal["read", "unread", "flagged", "unflagged"],
        Field(description="What to set on those messages."),
    ],
) -> dict:
    """Mark messages read/unread or flagged/unflagged.

    Requires the account to be configured with writable = true.
    """
    config, registry, index = _state()
    acct = config.account(account)
    _require_writable(acct, f"marking messages {action}")
    session = registry.get(acct)

    def apply() -> dict:
        with session.session() as client:
            session.select(client, folder, write=True)
            if action == "read":
                client.add_flags(uids, [b"\\Seen"])
            elif action == "unread":
                client.remove_flags(uids, [b"\\Seen"])
            elif action == "flagged":
                client.add_flags(uids, [b"\\Flagged"])
            else:
                client.remove_flags(uids, [b"\\Flagged"])

            # Keep the header index honest: without this, a message just
            # marked read keeps showing as unread in scope='headers'
            # results until the next sync.
            try:
                fetched = client.fetch(uids, ["FLAGS"])
                for uid, data in fetched.items():
                    flag_str = ",".join(
                        f.decode("ascii", errors="replace")
                        if isinstance(f, bytes)
                        else str(f)
                        for f in data.get(b"FLAGS", ())
                    )
                    index.update_flags(account, folder, [int(uid)], flag_str)
            except IMAPClientError as exc:
                logger.warning("Could not refresh index flags: %s", exc)

        return {"account": account, "folder": folder, "uids": uids, "action": action}

    return await asyncio.to_thread(apply)


@mcp.tool
async def move(
    account: Annotated[str, Field(description="Account name.")],
    folder: Annotated[str, Field(description="Source folder.")],
    uids: Annotated[list[int], Field(description="Message UIDs to move.")],
    target_folder: Annotated[str, Field(description="Destination folder.")],
) -> dict:
    """Move messages into another folder of the same account.

    Requires the account to be configured with writable = true. Uses the
    server's MOVE command where available, otherwise copy-then-delete.
    """
    config, registry, index = _state()
    acct = config.account(account)
    _require_writable(acct, "moving messages")
    session = registry.get(acct)

    def apply() -> dict:
        with session.session() as client:
            if not client.folder_exists(target_folder):
                raise ValueError(
                    f"Folder {target_folder!r} does not exist in {account}. "
                    "Call list_folders to see the exact names."
                )
            session.select(client, folder, write=True)
            if client.has_capability("MOVE"):
                client.move(uids, target_folder)
                method = "MOVE"
            else:
                client.copy(uids, target_folder)
                client.add_flags(uids, [b"\\Deleted"])
                client.expunge()
                method = "COPY+EXPUNGE"
        # The moved messages are gone from the source under these UIDs.
        # Only those rows are dropped — resetting the whole folder would
        # force a full re-index for the sake of a handful of messages.
        # The copies land in the target with fresh, higher UIDs, so the
        # next incremental sync picks them up on its own.
        removed = index.remove_uids(account, folder, uids)
        return {
            "account": account,
            "moved": len(uids),
            "from": folder,
            "to": target_folder,
            "method": method,
            "index_rows_removed": removed,
            "note": (
                f"Removed {removed} stale row(s) from the index. The moved "
                f"messages appear under {target_folder} after the next "
                f"refresh_index."
            ),
        }

    return await asyncio.to_thread(apply)


# --------------------------------------------------------------------------


def build_auth(config: Config):
    """Build the OAuth resource-server provider, or None if disabled.

    This server verifies tokens; it never issues them. Your own
    authorization server signs them, and RFC 9728 metadata points MCP
    clients at it. The audience check is what stops a token issued for
    some other service in your estate from working here.
    """
    if not config.auth.enabled:
        return None

    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    a = config.auth
    verifier = JWTVerifier(
        jwks_uri=a.jwks_uri or None,
        public_key=a.public_key or None,
        issuer=a.issuer or None,
        audience=a.audience,
        algorithm=a.algorithm or None,
        required_scopes=list(a.required_scopes) or None,
        base_url=a.base_url,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=list(a.authorization_servers),
        base_url=a.base_url,
        scopes_supported=list(a.required_scopes) or None,
        resource_name=a.resource_name,
    )


def build_server(config_path: str | None = None) -> FastMCP:
    """Load config and return the configured FastMCP instance."""
    from pathlib import Path

    try:
        config = load_config(Path(config_path) if config_path else None)
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    configure(config)

    auth = build_auth(config)
    if auth is not None:
        mcp.auth = auth
        logger.info(
            "OAuth enabled — issuer %s, audience %s",
            config.auth.issuer or "(any)",
            config.auth.audience,
        )
    else:
        logger.warning(
            "OAuth is DISABLED. Anything that can reach this port can read "
            "your mail. Only acceptable for stdio or a loopback-only bind."
        )

    logger.info(
        "Loaded %d account(s): %s",
        len(config.accounts),
        ", ".join(a.name for a in config.accounts),
    )
    return mcp


def _transport_from_env() -> tuple[str, dict]:
    """stdio by default; http when IMAP_MCP_HTTP_PORT is set."""
    port = os.environ.get("IMAP_MCP_HTTP_PORT")
    if port:
        return "http", {
            "host": os.environ.get("IMAP_MCP_HTTP_HOST", "127.0.0.1"),
            "port": int(port),
        }
    return "stdio", {}
