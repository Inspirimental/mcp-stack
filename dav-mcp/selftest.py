"""Offline self-test: configuration, parsing and matching."""

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

FAILS = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


tmp = Path(tempfile.mkdtemp())
from dav_mcp.config import ConfigError, load_config


def write(block: str) -> Path:
    p = tmp / "config.toml"
    p.write_text(block)
    return p


print("\nconfig")
base = """
[[account]]
name = "icloud"
username = "tim@icloud.com"
caldav_url = "https://caldav.icloud.com/"
carddav_url = "https://contacts.icloud.com"
password_env = "TEST_DAV_PW"
"""

try:
    load_config(write(base))
    check("missing password rejected", False)
except ConfigError as exc:
    check("missing password rejected", "TEST_DAV_PW" in str(exc))

os.environ["TEST_DAV_PW"] = "geheim"
cfg = load_config(write(base))
check("account loaded", len(cfg.accounts) == 1)
check("trailing slash stripped from url",
      cfg.accounts[0].caldav_url == "https://caldav.icloud.com")
check("calendars and contacts both detected",
      cfg.accounts[0].has_calendars and cfg.accounts[0].has_contacts)
check("password from environment", cfg.accounts[0].password == "geheim")

only_cal = base.replace('carddav_url = "https://contacts.icloud.com"', "")
cfg2 = load_config(write(only_cal))
check("calendar-only account allowed",
      cfg2.accounts[0].has_calendars and not cfg2.accounts[0].has_contacts)

neither = base.replace('caldav_url = "https://caldav.icloud.com/"', "").replace(
    'carddav_url = "https://contacts.icloud.com"', "")
try:
    load_config(write(neither))
    check("account without any endpoint rejected", False)
except ConfigError as exc:
    check("account without any endpoint rejected", "carddav_url" in str(exc))

try:
    load_config(write(base + base))
    check("duplicate account name rejected", False)
except ConfigError as exc:
    check("duplicate account name rejected", "Duplicate" in str(exc))

try:
    cfg.account(None)
    check("single account resolves without a name", True)
except ConfigError:
    check("single account resolves without a name", False)

two = base + base.replace('name = "icloud"', 'name = "work"').replace(
    "TEST_DAV_PW", "TEST_DAV_PW2")
os.environ["TEST_DAV_PW2"] = "geheim2"
cfg3 = load_config(write(two))
try:
    cfg3.account(None)
    check("ambiguous account rejected", False)
except ConfigError:
    check("ambiguous account rejected", True)
check("resolve(None) returns all", len(cfg3.resolve(None)) == 2)

print("\nauth block")
auth = base + """
[auth]
enabled = true
base_url = "https://mcp.timbeck.de"
authorization_servers = ["https://mcp.timbeck.de/auth"]
jwks_uri = "https://mcp.timbeck.de/auth/jwks.json"
issuer = "https://mcp.timbeck.de/auth"
audience = "https://mcp.timbeck.de/dav"
required_scopes = ["calendar:read"]
"""
cfg4 = load_config(write(auth))
check("auth parsed", cfg4.auth.enabled is True)
check("AUDIENCE DIFFERS FROM THE MAIL SERVER",
      cfg4.auth.audience == "https://mcp.timbeck.de/dav")
check("auth off by default", cfg.auth.enabled is False)
# FastMCP appends the mount path itself; presetting this would produce
# .../oauth-protected-resource/dav/dav.
check("resource_base_url stays empty by default",
      cfg4.auth.resource_base_url == "", cfg4.auth.resource_base_url)
explicit = auth + '\nresource_base_url = "https://proxy.example/dav"\n'
check("resource_base_url honoured when given",
      load_config(write(explicit)).auth.resource_base_url
      == "https://proxy.example/dav")

for key in ("audience", "base_url", "authorization_servers"):
    broken = "\n".join(l for l in auth.splitlines() if not l.startswith(key))
    try:
        load_config(write(broken))
        check(f"missing {key} rejected", False)
    except ConfigError as exc:
        check(f"missing {key} rejected", key in str(exc))

no_key = "\n".join(l for l in auth.splitlines() if not l.startswith("jwks_uri"))
try:
    load_config(write(no_key))
    check("missing signing key rejected", False)
except ConfigError as exc:
    check("missing signing key rejected", "jwks_uri" in str(exc))

from dav_mcp.server import build_auth
check("provider built when auth is on", build_auth(cfg4) is not None)
check("no provider when auth is off", build_auth(cfg) is None)

print("\nunknown keys")
misplaced = base + 'timezone = "Europe/Berlin"\n'
try:
    load_config(write(misplaced))
    check("TIMEZONE INSIDE AN ACCOUNT IS REJECTED", False, "(silently ignored!)")
except ConfigError as exc:
    check("TIMEZONE INSIDE AN ACCOUNT IS REJECTED", "timezone" in str(exc))
    check("and the message says where it belongs",
          "top of the file" in str(exc), str(exc)[:90])

try:
    load_config(write(base + "writeable = true\n"))
    check("typo in an account key rejected", False)
except ConfigError as exc:
    check("typo in an account key rejected", "writeable" in str(exc))

try:
    load_config(write('timezoen = "Europe/Berlin"\n' + base))
    check("typo at the top level rejected", False)
except ConfigError as exc:
    check("typo at the top level rejected", "timezoen" in str(exc))

try:
    load_config(write(auth + "audiance = \"x\"\n"))
    check("typo in the auth block rejected", False)
except ConfigError as exc:
    check("typo in the auth block rejected", "audiance" in str(exc))

check("a correct file still loads",
      load_config(write('timezone = "Europe/Berlin"\n' + base)).timezone
      == "Europe/Berlin")

print("\nwrite configuration")
check("writable off by default", cfg.accounts[0].writable is False)
w = load_config(write(base + "writable = true\n"))
check("writable honoured", w.accounts[0].writable is True)

check("timezone defaults to UTC", cfg.timezone == "UTC")
tz = load_config(write('timezone = "Europe/Berlin"\n' + base))
check("timezone parsed", tz.timezone == "Europe/Berlin")
try:
    load_config(write('timezone = "Mars/Olympus"\n' + base))
    check("unknown timezone rejected", False)
except ConfigError as exc:
    check("unknown timezone rejected", "Europe/Berlin" in str(exc))

adv = auth + '\nadvertised_scopes = ["calendar:read", "calendar:write"]\n'
cfg5 = load_config(write(adv))
check("advertised scopes parsed",
      cfg5.auth.advertised_scopes == ("calendar:read", "calendar:write"))
check("REQUIRED STAYS THE READ BASELINE",
      cfg5.auth.required_scopes == ("calendar:read",), cfg5.auth.required_scopes)
check("advertised falls back to required when absent",
      cfg4.auth.advertised_scopes == cfg4.auth.required_scopes)

print("\nall-day arithmetic")
from dav_mcp.calendars import build_event, is_recurring, parse_day

check("plain day parsed", parse_day("2026-09-20").isoformat() == "2026-09-20")
check("german day parsed", parse_day("20.09.2026").isoformat() == "2026-09-20")

ics = build_event(uid="x", summary="Ganztag", start="2026-09-20", end=None,
                  all_day=True, location="", description="", attendees=[],
                  tz_name="Europe/Berlin")
check("ALL-DAY END IS THE NEXT DAY", "DTEND;VALUE=DATE:20260921" in ics,
      [l for l in ics.splitlines() if "DTEND" in l])
check("all-day start has no time", "DTSTART;VALUE=DATE:20260920" in ics)

timed = build_event(uid="x", summary="Termin", start="2026-09-15 14:00",
                    end="2026-09-15 15:00", all_day=False, location="",
                    description="", attendees=["a@b.de"], tz_name="Europe/Berlin")
check("attendee written as mailto", "mailto:a@b.de" in timed)
check("timed event carries a time", "DTSTART;TZID=" in timed or "T14" in timed,
      [l for l in timed.splitlines() if "DTSTART" in l])

try:
    build_event(uid="x", summary="Rückwärts", start="2026-09-15 15:00",
                end="2026-09-15 14:00", all_day=False, location="",
                description="", attendees=[], tz_name="UTC")
    check("end before start rejected", False)
except ValueError:
    check("end before start rejected", True)

from icalendar import Calendar as ICal
series = ICal.from_ical(
    "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\n"
    "UID:s\nDTSTAMP:20260101T000000Z\nDTSTART:20260902T100000Z\n"
    "SUMMARY:S\nRRULE:FREQ=WEEKLY\nEND:VEVENT\nEND:VCALENDAR\n")
single = ICal.from_ical(
    "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\n"
    "UID:e\nDTSTAMP:20260101T000000Z\nDTSTART:20260902T100000Z\n"
    "SUMMARY:E\nEND:VEVENT\nEND:VCALENDAR\n")
check("recurrence detected", is_recurring(series.walk("VEVENT")[0]) is True)
check("single event not flagged", is_recurring(single.walk("VEVENT")[0]) is False)

print("\ndate parsing")
from dav_mcp.calendars import default_window, matches, parse_when

fallback = datetime(2020, 1, 1, tzinfo=timezone.utc)
check("iso date", parse_when("2026-09-01", fallback).strftime("%Y-%m-%d") == "2026-09-01")
berlin = parse_when("2026-09-01T14:00:00", fallback, "Europe/Berlin")
check("NAIVE TIME READ IN THE CONFIGURED ZONE",
      berlin.utcoffset().total_seconds() == 7200, berlin.isoformat())
check("same time as UTC differs by two hours",
      parse_when("2026-09-01T14:00:00", fallback, "UTC").utcoffset().total_seconds() == 0)
check("iso datetime",
      parse_when("2026-09-01T14:30:00", fallback).hour == 14)
check("german date", parse_when("01.09.2026", fallback).strftime("%Y-%m-%d") == "2026-09-01")
check("german datetime", parse_when("01.09.2026 14:30", fallback).hour == 14)
check("empty falls back", parse_when(None, fallback) == fallback)
check("result is timezone aware",
      parse_when("2026-09-01", fallback).tzinfo is not None)
try:
    parse_when("irgendwann", fallback)
    check("garbage rejected", False)
except ValueError:
    check("garbage rejected", True)

start, end = default_window()
check("default window is 28 days", (end - start).days == 28)

print("\nmatching")
event = {"summary": "Messe Jena", "location": "Messegelände",
         "description": "Standgröße klären", "organizer": "jf@batix.com",
         "attendees": ["sga@batix.com"]}
check("matches the title", matches(event, ["messe"]))
check("matches the description", matches(event, ["standgröße"]))
check("matches an attendee", matches(event, ["sga@batix.com"]))
check("all terms must match", not matches(event, ["messe", "urlaub"]))
check("case insensitive", matches(event, ["MESSE"]))

from dav_mcp.contacts import matches as contact_matches

contact = {"name": "Sascha Greiner-Adam", "organisation": "Batix Software GmbH",
           "title": "", "note": "", "emails": ["sga@batix.com"],
           "phones": ["+49 3641 1234"], "addresses": []}
check("contact by name", contact_matches(contact, ["greiner"]))
check("contact by company", contact_matches(contact, ["batix"]))
check("contact by phone fragment", contact_matches(contact, ["3641"]))
check("contact terms ANDed",
      not contact_matches(contact, ["batix", "jasaa"]))

print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("All checks passed.")
