"""MCP tools over CalDAV calendars and CardDAV address books.

Read-only throughout: there is no tool here that creates, changes or
deletes anything. Adding one later is a deliberate decision, not an
oversight.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from . import calendars, contacts
from .config import Config, ConfigError, load_config

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP(
    name="dav-mcp",
    instructions=(
        "Read calendars and contacts across several CalDAV/CardDAV "
        "accounts. Calendar queries are date-range based — say which "
        "window you mean, or accept the default of today plus four "
        "weeks. Contact searches run against a locally cached copy of "
        "the address books."
    ),
)

_config: Config | None = None


def configure(config: Config) -> None:
    global _config
    _config = config


def _state() -> Config:
    if _config is None:
        raise RuntimeError("Server not configured; call configure() first.")
    return _config


def needs(*scopes: str):
    """Require these OAuth scopes for one tool.

    When OAuth is switched off — stdio, or a loopback-only bind — there
    is no token and no scope to check, so the per-account `writable`
    flag is the only gate. Refusing every write in that mode would make
    local use impossible; pretending a scope was verified would be
    worse. So the check states plainly which regime it is in.
    """

    def check(ctx) -> bool:
        config = _config
        if config is None or not config.auth.enabled:
            return True
        token = getattr(ctx, "token", None)
        if token is None:
            return False
        return set(scopes).issubset(set(token.scopes or []))

    return check


def _terms(query: str) -> list[str]:
    try:
        return [t for t in shlex.split(query.strip()) if t]
    except ValueError:
        return [t for t in query.split() if t]


# --------------------------------------------------------------- calendars


@mcp.tool
async def list_accounts() -> list[dict]:
    """List the configured accounts and what each one offers."""
    config = _state()
    return [
        {
            "name": a.name,
            "username": a.username,
            "calendars": a.has_calendars,
            "contacts": a.has_contacts,
            "writable": a.writable,
        }
        for a in config.accounts
    ]


@mcp.tool(auth=needs("calendar:read"))
async def list_calendars(
    account: Annotated[
        str | None, Field(description="Account name; omit for all accounts.")
    ] = None,
) -> list[dict]:
    """List available calendars per account."""
    config = _state()
    targets = [a for a in config.resolve([account] if account else None)
               if a.has_calendars]

    results = await asyncio.gather(
        *(asyncio.to_thread(calendars.list_calendars, a) for a in targets),
        return_exceptions=True,
    )
    out: list[dict] = []
    for acct, item in zip(targets, results):
        if isinstance(item, BaseException):
            out.append({"account": acct.name, "error": str(item)})
        else:
            out.extend(item)
    return out


@mcp.tool(auth=needs("calendar:read"))
async def list_events(
    start: Annotated[
        str | None,
        Field(description="Window start (YYYY-MM-DD). Default: today."),
    ] = None,
    end: Annotated[
        str | None,
        Field(description="Window end (YYYY-MM-DD). Default: 28 days out."),
    ] = None,
    accounts: Annotated[
        list[str] | None, Field(description="Limit to these accounts.")
    ] = None,
    calendar_names: Annotated[
        list[str] | None, Field(description="Limit to these calendars.")
    ] = None,
    limit: Annotated[int, Field(description="Maximum events.", ge=1, le=500)] = 50,
) -> dict:
    """List calendar events in a time window, sorted by start.

    Recurring events are expanded into their individual occurrences
    inside the window.
    """
    config = _state()
    targets = [a for a in config.resolve(accounts) if a.has_calendars]
    if not targets:
        return {"events": [], "note": "No account with a calendar is configured."}

    default_start, default_end = calendars.default_window()
    try:
        window_start = calendars.parse_when(start, default_start)
        window_end = calendars.parse_when(end, default_end)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if window_end <= window_start:
        raise ValueError("The end of the window must be after its start.")

    gathered = await asyncio.gather(
        *(
            asyncio.to_thread(
                calendars.fetch_events, a, calendar_names,
                window_start, window_end, limit,
            )
            for a in targets
        ),
        return_exceptions=True,
    )

    events: list[dict] = []
    errors: list[str] = []
    for acct, item in zip(targets, gathered):
        if isinstance(item, BaseException):
            errors.append(f"{acct.name}: {item}")
            continue
        found, per_calendar = item
        events.extend(found)
        errors.extend(per_calendar)

    events.sort(key=lambda e: e.get("start") or "")
    payload: dict = {
        "events": events[:limit],
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "searched_accounts": [a.name for a in targets],
    }
    if errors:
        payload["errors"] = errors
    return payload


@mcp.tool(auth=needs("calendar:read"))
async def search_events(
    query: Annotated[
        str,
        Field(description='Words to look for; multiple words are ANDed. '
                          'Use "quotes" for a phrase.'),
    ],
    start: Annotated[
        str | None, Field(description="Window start (YYYY-MM-DD). Default: today.")
    ] = None,
    end: Annotated[
        str | None, Field(description="Window end (YYYY-MM-DD). Default: 28 days out.")
    ] = None,
    accounts: Annotated[
        list[str] | None, Field(description="Limit to these accounts.")
    ] = None,
    limit: Annotated[int, Field(description="Maximum events.", ge=1, le=500)] = 50,
) -> dict:
    """Search calendar events by text within a time window.

    Matches on title, location, description, organiser and attendees.
    The window matters: this searches what falls inside it, so widen the
    dates when looking further back or ahead.
    """
    terms = _terms(query)
    if not terms:
        raise ValueError("The query is empty.")

    # A generous default window: a text search without dates usually
    # means "find this appointment", not "find it in the next 4 weeks".
    if start is None and end is None:
        default_start, _ = calendars.default_window()
        start = (default_start.replace(year=default_start.year - 1)).strftime("%Y-%m-%d")
        end = (default_start.replace(year=default_start.year + 1)).strftime("%Y-%m-%d")

    found = await list_events(
        start=start, end=end, accounts=accounts, calendar_names=None, limit=500
    )
    hits = [e for e in found["events"] if calendars.matches(e, terms)]
    payload = {
        "events": hits[:limit],
        "window": found.get("window"),
        "scanned": len(found["events"]),
    }
    if found.get("errors"):
        payload["errors"] = found["errors"]
    if not hits:
        payload["note"] = (
            "Nothing matched inside this window. Widen 'start' and 'end' "
            "if the appointment may lie outside it."
        )
    return payload


# ---------------------------------------------------------------- contacts


@mcp.tool(auth=needs("contacts:read"))
async def list_addressbooks(
    account: Annotated[
        str | None, Field(description="Account name; omit for all accounts.")
    ] = None,
) -> list[dict]:
    """List available address books per account."""
    config = _state()
    targets = [a for a in config.resolve([account] if account else None)
               if a.has_contacts]

    results = await asyncio.gather(
        *(asyncio.to_thread(contacts.discover_addressbooks, a) for a in targets),
        return_exceptions=True,
    )
    out: list[dict] = []
    for acct, item in zip(targets, results):
        if isinstance(item, BaseException):
            out.append({"account": acct.name, "error": str(item)})
        else:
            out.extend(item)
    return out


@mcp.tool(auth=needs("contacts:read"))
async def search_contacts(
    query: Annotated[
        str,
        Field(description="Name, email, phone number, company — multiple "
                          "words are ANDed."),
    ],
    accounts: Annotated[
        list[str] | None, Field(description="Limit to these accounts.")
    ] = None,
    limit: Annotated[int, Field(description="Maximum results.", ge=1, le=100)] = 20,
) -> dict:
    """Search contacts across all address books.

    Matches on name, organisation, job title, email, phone, address and
    notes. Runs against a cached copy — use refresh_contacts after
    changing something on the provider side.
    """
    terms = _terms(query)
    if not terms:
        raise ValueError("The query is empty.")

    config = _state()
    targets = [a for a in config.resolve(accounts) if a.has_contacts]

    gathered = await asyncio.gather(
        *(asyncio.to_thread(contacts.fetch_contacts, a) for a in targets),
        return_exceptions=True,
    )

    hits: list[dict] = []
    errors: list[str] = []
    scanned = 0
    for acct, item in zip(targets, gathered):
        if isinstance(item, BaseException):
            errors.append(f"{acct.name}: {item}")
            continue
        scanned += len(item)
        hits.extend(c for c in item if contacts.matches(c, terms))

    payload: dict = {"contacts": hits[:limit], "scanned": scanned}
    if errors:
        payload["errors"] = errors
    return payload


@mcp.tool(auth=needs("contacts:read"))
async def refresh_contacts(
    accounts: Annotated[
        list[str] | None, Field(description="Accounts to refresh; omit for all.")
    ] = None,
) -> dict:
    """Re-fetch the address books, discarding the cache."""
    config = _state()
    targets = [a for a in config.resolve(accounts) if a.has_contacts]

    gathered = await asyncio.gather(
        *(asyncio.to_thread(contacts.fetch_contacts, a, True) for a in targets),
        return_exceptions=True,
    )
    summary = []
    for acct, item in zip(targets, gathered):
        if isinstance(item, BaseException):
            summary.append({"account": acct.name, "error": str(item)})
        else:
            summary.append({"account": acct.name, "contacts": len(item)})
    return {"accounts": summary}



# ----------------------------------------------------------- writing
#
# Two gates, and both must open: the token needs the write scope, and
# the account needs writable = true in the config. Neither substitutes
# for the other — the scope says what the caller may ask for, the flag
# says what you have allowed at all.


@mcp.tool(auth=needs("calendar:write"))
async def create_event(
    summary: Annotated[str, Field(description="Title of the appointment.")],
    start: Annotated[
        str,
        Field(description="Start. 'YYYY-MM-DD HH:MM' for a timed event, "
                          "'YYYY-MM-DD' together with all_day=true."),
    ],
    end: Annotated[
        str | None,
        Field(description="End. Omitted means one hour after the start, "
                          "or the same day for an all-day event."),
    ] = None,
    account: Annotated[
        str | None, Field(description="Account; required if several are configured.")
    ] = None,
    calendar: Annotated[
        str | None,
        Field(description="Calendar name; required if the account has several."),
    ] = None,
    all_day: Annotated[bool, Field(description="Whole-day appointment.")] = False,
    location: Annotated[str, Field(description="Place.")] = "",
    description: Annotated[str, Field(description="Notes.")] = "",
    attendees: Annotated[
        list[str] | None, Field(description="Email addresses to invite.")
    ] = None,
) -> dict:
    """Create a calendar event.

    Times without a zone are read in the server's configured timezone,
    not UTC. The created event is read back from the server and
    returned, so what you see is what was actually stored.
    """
    config = _state()
    acct = config.account(account)
    if not acct.has_calendars:
        raise ValueError(f"Account {acct.name!r} has no calendar.")

    return await asyncio.to_thread(
        calendars.create_event,
        acct, calendar, config.timezone,
        summary=summary, start=start, end=end, all_day=all_day,
        location=location, description=description,
        attendees=attendees or [],
    )


@mcp.tool(auth=needs("calendar:write"))
async def update_event(
    uid: Annotated[str, Field(description="UID from a search or listing.")],
    account: Annotated[
        str | None, Field(description="Account; required if several are configured.")
    ] = None,
    calendar: Annotated[
        str | None, Field(description="Calendar holding the event.")
    ] = None,
    summary: Annotated[str | None, Field(description="New title.")] = None,
    start: Annotated[str | None, Field(description="New start.")] = None,
    end: Annotated[str | None, Field(description="New end.")] = None,
    location: Annotated[str | None, Field(description="New place.")] = None,
    description: Annotated[str | None, Field(description="New notes.")] = None,
    whole_series: Annotated[
        bool,
        Field(description="Required to change a recurring event. Every "
                          "occurrence shares one UID, so the change "
                          "applies to the entire series."),
    ] = False,
) -> dict:
    """Change an existing event. Only the fields you give are touched.

    A recurring event is refused unless whole_series is set: all its
    occurrences share a single UID, so a change made here would move the
    whole series rather than the one date you were looking at.
    """
    config = _state()
    acct = config.account(account)
    if not acct.has_calendars:
        raise ValueError(f"Account {acct.name!r} has no calendar.")

    return await asyncio.to_thread(
        calendars.update_event,
        acct, calendar, uid,
        {
            "summary": summary, "start": start, "end": end,
            "location": location, "description": description,
        },
        whole_series, config.timezone,
    )


@mcp.tool(auth=needs("contacts:write"))
async def create_contact(
    name: Annotated[str, Field(description="Full name.")],
    emails: Annotated[list[str] | None, Field(description="Email addresses.")] = None,
    phones: Annotated[list[str] | None, Field(description="Phone numbers.")] = None,
    organisation: Annotated[str, Field(description="Company.")] = "",
    title: Annotated[str, Field(description="Job title.")] = "",
    note: Annotated[str, Field(description="Free-text note.")] = "",
    account: Annotated[
        str | None, Field(description="Account; required if several are configured.")
    ] = None,
    addressbook: Annotated[
        str | None,
        Field(description="Address book; required if the account has several."),
    ] = None,
) -> dict:
    """Create a contact in an address book."""
    config = _state()
    acct = config.account(account)
    if not acct.has_contacts:
        raise ValueError(f"Account {acct.name!r} has no address book.")

    return await asyncio.to_thread(
        contacts.create_contact,
        acct, addressbook,
        name=name, emails=emails or [], phones=phones or [],
        organisation=organisation, title=title, note=note,
    )


@mcp.tool(auth=needs("contacts:write"))
async def update_contact(
    uid: Annotated[str, Field(description="UID from a contact search.")],
    account: Annotated[
        str | None, Field(description="Account; required if several are configured.")
    ] = None,
    name: Annotated[str | None, Field(description="New name.")] = None,
    emails: Annotated[
        list[str] | None,
        Field(description="Replaces ALL email addresses. Include the ones "
                          "to keep."),
    ] = None,
    phones: Annotated[
        list[str] | None,
        Field(description="Replaces ALL phone numbers. Include the ones "
                          "to keep."),
    ] = None,
    organisation: Annotated[str | None, Field(description="New company.")] = None,
    title: Annotated[str | None, Field(description="New job title.")] = None,
    note: Annotated[str | None, Field(description="New note.")] = None,
) -> dict:
    """Change an existing contact. Only the fields you give are touched.

    Note that emails and phones REPLACE the existing lists rather than
    adding to them — read the contact first and pass back the entries
    you want to keep.

    The write is conditional on the contact not having changed on the
    server since it was read; if it did, the call fails rather than
    overwriting someone else's edit.
    """
    config = _state()
    acct = config.account(account)
    if not acct.has_contacts:
        raise ValueError(f"Account {acct.name!r} has no address book.")

    return await asyncio.to_thread(
        contacts.update_contact, acct, uid,
        {
            "name": name, "emails": emails, "phones": phones,
            "organisation": organisation, "title": title, "note": note,
        },
    )


# ---------------------------------------------------------------------


def build_auth(config: Config):
    """OAuth resource-server provider, or None if disabled."""
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
        resource_base_url=a.resource_base_url or None,
        scopes_supported=list(a.advertised_scopes or a.required_scopes) or None,
        resource_name=a.resource_name,
    )


def build_server(config_path: str | None = None) -> FastMCP:
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
            config.auth.issuer or "(any)", config.auth.audience,
        )
    else:
        logger.warning(
            "OAuth is DISABLED. Anything that can reach this port can read "
            "your calendars and contacts. Only acceptable for stdio or a "
            "loopback-only bind."
        )

    logger.info(
        "Loaded %d account(s): %s",
        len(config.accounts), ", ".join(a.name for a in config.accounts),
    )
    return mcp


def _transport_from_env() -> tuple[str, dict]:
    """stdio by default; http when DAV_MCP_HTTP_PORT is set.

    DAV_MCP_HTTP_PATH sets where the MCP endpoint lives. It defaults to
    /dav rather than FastMCP's /mcp, because this server shares a
    hostname with the mail server and the two endpoints have to differ.
    """
    port = os.environ.get("DAV_MCP_HTTP_PORT")
    if port:
        return "http", {
            "host": os.environ.get("DAV_MCP_HTTP_HOST", "127.0.0.1"),
            "port": int(port),
            "path": os.environ.get("DAV_MCP_HTTP_PATH", "/dav"),
        }
    return "stdio", {}
