"""CalDAV access.

Date-range queries go to the server, which is what CalDAV is good at:
asking for "everything between two dates" is a single request and the
provider expands recurring events for us. Free-text search is done
locally on the fetched range — CalDAV's own text-match filter exists but
providers implement it inconsistently enough that a portable server
should not rely on it.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from caldav import DAVClient

from .config import Account

logger = logging.getLogger(__name__)


class DavError(RuntimeError):
    """Raised when a provider cannot be reached or authenticated."""


_clients: dict[str, DAVClient] = {}
_lock = threading.Lock()


def client_for(account: Account) -> DAVClient:
    """One DAVClient per account, created on first use."""
    with _lock:
        existing = _clients.get(account.name)
        if existing is not None:
            return existing
        try:
            client = DAVClient(
                url=account.caldav_url,
                username=account.username,
                password=account.password,
                timeout=account.timeout,
            )
            # Force a round-trip now so failures surface here, with a
            # message naming the account, rather than deep inside a tool.
            client.principal()
        except Exception as exc:
            raise DavError(
                f"{account.name}: CalDAV login failed ({exc}). Check the "
                f"username and the app password. Note that some providers "
                f"issue separate passwords for mail and for calendars."
            ) from exc
        _clients[account.name] = client
        return client


def close_all() -> None:
    with _lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception:
                pass
        _clients.clear()


def calendar_label(cal) -> str:
    """The display name of a calendar, with a usable fallback.

    caldav deprecated the `.name` attribute in favour of
    get_display_name(). Some collections have no display name at all,
    so the last path segment stands in — better a URL fragment than an
    empty label the user cannot match to anything.
    """
    try:
        name = cal.get_display_name()
    except Exception:
        name = None
    if name:
        return str(name)
    return str(cal.url).rstrip("/").rsplit("/", 1)[-1]


def zone(name: str = "UTC"):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def parse_when(
    value: str | None, default: datetime, tz_name: str = "UTC"
) -> datetime:
    """Accept a date or a datetime, always return an aware datetime.

    A value without a zone is read in `tz_name`, not UTC. Getting this
    wrong is invisible when reading and two hours off when writing.
    """
    if not value:
        return default
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=zone(tz_name))
        except ValueError:
            continue
    raise ValueError(
        f"Cannot read the date {value!r}. Use YYYY-MM-DD or "
        f"YYYY-MM-DDTHH:MM:SS."
    )


def parse_day(value: str) -> date:
    """Parse a plain calendar day, for all-day events."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot read the day {value!r}. Use YYYY-MM-DD.")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    inner = getattr(value, "to_ical", None)
    if callable(inner):
        raw = inner()
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    return str(value)


def _stamp(value: Any) -> str | None:
    """Render DTSTART/DTEND, which may be a date or a datetime."""
    dt = getattr(value, "dt", None)
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


def _people(component: Any, key: str) -> list[str]:
    raw = component.get(key)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        text = _as_text(item)
        out.append(text.replace("mailto:", "").replace("MAILTO:", ""))
    return out


def event_to_dict(account: str, calendar: str, obj: Any) -> dict | None:
    try:
        component = obj.icalendar_component
    except Exception:
        return None
    if component is None:
        return None

    start = component.get("dtstart")
    end = component.get("dtend") or component.get("due")
    all_day = bool(
        start is not None
        and getattr(start, "dt", None) is not None
        and not isinstance(start.dt, datetime)
    )
    return {
        "account": account,
        "calendar": calendar,
        "uid": _as_text(component.get("uid")),
        "summary": _as_text(component.get("summary")),
        "start": _stamp(start),
        "end": _stamp(end),
        "all_day": all_day,
        "location": _as_text(component.get("location")),
        "description": _as_text(component.get("description"))[:2000],
        "status": _as_text(component.get("status")),
        "organizer": _as_text(component.get("organizer")).replace("mailto:", ""),
        "attendees": _people(component, "attendee"),
    }


def list_calendars(account: Account) -> list[dict]:
    principal = client_for(account).principal()
    out = []
    for cal in principal.calendars():
        try:
            name = calendar_label(cal)
        except Exception:
            name = str(cal.url)
        out.append({"account": account.name, "calendar": name, "url": str(cal.url)})
    return out


def fetch_events(
    account: Account,
    calendars: list[str] | None,
    start: datetime,
    end: datetime,
    limit: int,
) -> tuple[list[dict], list[str]]:
    """Fetch events in a window. Returns (events, per-calendar errors)."""
    principal = client_for(account).principal()
    wanted = {c.lower() for c in calendars} if calendars else None

    events: list[dict] = []
    errors: list[str] = []
    for cal in principal.calendars():
        name = calendar_label(cal)
        if wanted and name.lower() not in wanted:
            continue
        try:
            # expand=True turns a recurrence rule into the individual
            # occurrences inside the window, which is what anyone asking
            # "what is on next week" actually means.
            found = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as exc:
            errors.append(f"{account.name}/{name}: {exc}")
            logger.warning("Calendar query failed for %s: %s", name, exc)
            continue
        for obj in found:
            row = event_to_dict(account.name, name, obj)
            if row is not None:
                events.append(row)

    events.sort(key=lambda e: e.get("start") or "")
    return events[: limit if limit > 0 else None], errors


def default_window() -> tuple[datetime, datetime]:
    """Today through four weeks out — the useful default for 'what's next'."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=28)


def matches(event: dict, terms: list[str]) -> bool:
    haystack = " ".join(
        [
            event.get("summary", ""),
            event.get("location", ""),
            event.get("description", ""),
            event.get("organizer", ""),
            " ".join(event.get("attendees", [])),
        ]
    ).lower()
    return all(term.lower() in haystack for term in terms)


# ------------------------------------------------------------- writing


class NotWritable(RuntimeError):
    """The account or the event forbids this change."""


def calendar_by_name(account: Account, name: str | None):
    """Find one calendar. With several present, a name is required."""
    principal = client_for(account).principal()
    calendars = principal.calendars()
    if not calendars:
        raise DavError(f"{account.name}: this account has no calendar.")

    if name is None:
        if len(calendars) == 1:
            return calendars[0]
        raise ValueError(
            f"{account.name} has several calendars, so one must be named: "
            + ", ".join(
                calendar_label(c)
                for c in calendars
            )
        )

    for cal in calendars:
        label = calendar_label(cal)
        if label.lower() == name.lower():
            return cal
    raise ValueError(
        f"{account.name} has no calendar called {name!r}. Available: "
        + ", ".join(
            calendar_label(c) for c in calendars
        )
    )


def is_recurring(component) -> bool:
    """True if this VEVENT is a series rather than a single appointment."""
    return any(
        component.get(key) is not None
        for key in ("RRULE", "RDATE", "RECURRENCE-ID")
    )


def build_event(
    uid: str,
    summary: str,
    start: str,
    end: str | None,
    all_day: bool,
    location: str,
    description: str,
    attendees: list[str],
    tz_name: str,
) -> str:
    """Assemble a VCALENDAR document for one event."""
    from icalendar import Calendar as ICal
    from icalendar import Event as IEvent

    cal = ICal()
    cal.add("prodid", "-//dav-mcp//EN")
    cal.add("version", "2.0")

    event = IEvent()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", summary)

    if all_day:
        first = parse_day(start)
        # DTEND is exclusive for all-day events: a one-day appointment
        # ends the following day. Writing the same date twice produces a
        # zero-length event that some clients then hide entirely.
        last = parse_day(end) + timedelta(days=1) if end else first + timedelta(days=1)
        event.add("dtstart", first)
        event.add("dtend", last)
    else:
        begins = parse_when(start, datetime.now(timezone.utc), tz_name)
        finishes = (
            parse_when(end, begins + timedelta(hours=1), tz_name)
            if end
            else begins + timedelta(hours=1)
        )
        if finishes <= begins:
            raise ValueError("The end of the event must be after its start.")
        event.add("dtstart", begins)
        event.add("dtend", finishes)

    if location:
        event.add("location", location)
    if description:
        event.add("description", description)
    for person in attendees or []:
        event.add("attendee", f"mailto:{person}")

    cal.add_component(event)
    return cal.to_ical().decode("utf-8")


def create_event(account: Account, calendar_name: str | None, tz_name: str, **fields):
    """Write a new event and return it as it now stands on the server."""
    import uuid

    if not account.writable:
        raise NotWritable(
            f"Account {account.name!r} is read-only. Set writable = true "
            f"for it in the config and restart the server to allow writes."
        )

    cal = calendar_by_name(account, calendar_name)
    uid = str(uuid.uuid4())
    ics = build_event(uid=uid, tz_name=tz_name, **fields)
    cal.save_event(ics)

    label = calendar_label(cal)
    stored = cal.event_by_uid(uid)
    return event_to_dict(account.name, label, stored) or {"uid": uid}


def update_event(
    account: Account,
    calendar_name: str | None,
    uid: str,
    changes: dict,
    whole_series: bool,
    tz_name: str,
):
    """Change fields of an existing event.

    A series is refused unless whole_series is set. Every occurrence of
    a recurring event shares one UID, so a change made through it hits
    the entire series — not the single occurrence the caller was most
    likely looking at.
    """
    if not account.writable:
        raise NotWritable(
            f"Account {account.name!r} is read-only. Set writable = true "
            f"for it in the config and restart the server to allow writes."
        )

    cal = calendar_by_name(account, calendar_name)
    try:
        resource = cal.event_by_uid(uid)
    except Exception as exc:
        raise ValueError(
            f"No event with UID {uid!r} in this calendar ({exc})."
        ) from exc

    component = resource.icalendar_component
    if is_recurring(component) and not whole_series:
        raise NotWritable(
            "This event is part of a recurring series, and every "
            "occurrence shares the same UID — changing it here would "
            "change the whole series, not the single date you are "
            "looking at. Pass whole_series=true if that is what you "
            "want, or edit the one occurrence in your calendar app."
        )

    applied = []
    if changes.get("summary") is not None:
        component["SUMMARY"] = changes["summary"]
        applied.append("summary")
    if changes.get("location") is not None:
        component["LOCATION"] = changes["location"]
        applied.append("location")
    if changes.get("description") is not None:
        component["DESCRIPTION"] = changes["description"]
        applied.append("description")

    if changes.get("start") or changes.get("end"):
        all_day = changes.get("all_day")
        if all_day is None:
            existing = component.get("dtstart")
            all_day = bool(
                existing is not None
                and not isinstance(getattr(existing, "dt", None), datetime)
            )
        if changes.get("start"):
            component.pop("DTSTART", None)
            if all_day:
                component.add("dtstart", parse_day(changes["start"]))
            else:
                component.add(
                    "dtstart",
                    parse_when(changes["start"], datetime.now(timezone.utc), tz_name),
                )
            applied.append("start")
        if changes.get("end"):
            component.pop("DTEND", None)
            if all_day:
                component.add("dtend", parse_day(changes["end"]) + timedelta(days=1))
            else:
                component.add(
                    "dtend",
                    parse_when(changes["end"], datetime.now(timezone.utc), tz_name),
                )
            applied.append("end")

    if not applied:
        raise ValueError("Nothing to change — every field was left empty.")

    component.pop("DTSTAMP", None)
    component.add("dtstamp", datetime.now(timezone.utc))
    resource.save()

    label = calendar_label(cal)
    refreshed = cal.event_by_uid(uid)
    result = event_to_dict(account.name, label, refreshed) or {"uid": uid}
    result["changed_fields"] = applied
    result["whole_series"] = bool(is_recurring(component))
    return result
