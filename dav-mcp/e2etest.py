"""End-to-end test against a real CalDAV/CardDAV server (Radicale)."""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

BASE = "http://127.0.0.1:5232"
USER, PW = "davtest", "davpass"
HOME = f"{BASE}/{USER}"
FAILS = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


def dav(method, url, body=None, ctype="text/calendar; charset=utf-8", depth=None):
    headers = {"Content-Type": ctype}
    if depth:
        headers["Depth"] = depth
    return httpx.request(method, url, auth=(USER, PW), timeout=15,
                         content=body.encode() if body else None, headers=headers)


# ------------------------------------------------------------- fixtures
print("\nfixtures")

MKCOL_CAL = """<?xml version="1.0" encoding="utf-8"?>
<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:set><D:prop><D:displayname>Arbeit</D:displayname></D:prop></D:set>
</C:mkcalendar>"""

MKCOL_BOOK = """<?xml version="1.0" encoding="utf-8"?>
<D:mkcol xmlns:D="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">
  <D:set><D:prop>
    <D:resourcetype><D:collection/><CR:addressbook/></D:resourcetype>
    <D:displayname>Kontakte</D:displayname>
  </D:prop></D:set>
</D:mkcol>"""

def ensure(method, url, body, label):
    """Create a collection, tolerating one that already exists.

    Reruns must behave like first runs, so 'already there' counts as
    success — confirmed by a PROPFIND rather than assumed from the
    status code, which differs between servers.
    """
    created = dav(method, url, body, "application/xml")
    exists = httpx.request("PROPFIND", url, auth=(USER, PW), timeout=15,
                           headers={"Depth": "0"})
    check(label, exists.status_code == 207,
          f"create={created.status_code} propfind={exists.status_code}")


ensure("MKCALENDAR", f"{HOME}/arbeit/", MKCOL_CAL, "calendar available")
# A second calendar, so "which one did you mean" is a real question the
# code has to answer rather than a branch that never runs.
ensure("MKCALENDAR", f"{HOME}/privat/",
       MKCOL_CAL.replace("Arbeit", "Privat"), "second calendar available")
ensure("MKCOL", f"{HOME}/kontakte/", MKCOL_BOOK, "address book available")


def ics(uid, summary, start, end, location="", description="", attendee=""):
    extra = f"ATTENDEE;CN=Test:mailto:{attendee}\n" if attendee else ""
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\nBEGIN:VEVENT\n"
        f"UID:{uid}\nDTSTAMP:20260101T000000Z\n"
        f"DTSTART:{start}\nDTEND:{end}\nSUMMARY:{summary}\n"
        f"LOCATION:{location}\nDESCRIPTION:{description}\n{extra}"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


EVENTS = [
    ("messe", "Messe Jena Standabnahme", "20260901T090000Z", "20260901T120000Z",
     "Messegelände Jena", "Standgröße final klären", "sga@batix.com"),
    ("kompass", "Digital-Kompass Workshop", "20260903T130000Z", "20260903T170000Z",
     "Kundenbüro", "RKW-Förderung besprechen", "r.kaestner@jasaa.de"),
    ("alt", "Altes Meeting", "20250301T100000Z", "20250301T110000Z",
     "Büro", "liegt weit vor dem Fenster", ""),
    ("urlaub", "Gran Canaria", "20261020T000000Z", "20261027T000000Z",
     "Las Palmas", "Finca gebucht", ""),
]
def wipe(collection: str) -> int:
    """Empty a collection so every run starts from the same state.

    This suite creates events and contacts, so without a reset the
    second run sees the first run's leftovers and the counts drift.
    Deleting here, in the harness, keeps the server itself free of any
    delete tool.
    """
    listing = httpx.request("PROPFIND", collection, auth=(USER, PW), timeout=15,
                            headers={"Depth": "1"})
    if listing.status_code != 207:
        return 0
    from xml.etree import ElementTree as ET

    removed = 0
    root = ET.fromstring(listing.content)
    for node in root.findall("{DAV:}response"):
        href = node.find("{DAV:}href")
        if href is None or not href.text:
            continue
        path = href.text.strip()
        if path.rstrip("/").endswith(collection.rstrip("/").rsplit("/", 1)[-1]):
            continue  # the collection itself
        url = BASE + path if path.startswith("/") else path
        if httpx.request("DELETE", url, auth=(USER, PW),
                         timeout=15).status_code < 400:
            removed += 1
    return removed


wiped = wipe(f"{HOME}/arbeit/") + wipe(f"{HOME}/kontakte/")
check(f"collections reset ({wiped} leftover objects removed)", True)

SERIES = ("serie", "Wöchentliches Jour Fixe")
dav("PUT", f"{HOME}/arbeit/{SERIES[0]}.ics",
    "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\nBEGIN:VEVENT\n"
    f"UID:{SERIES[0]}\nDTSTAMP:20260101T000000Z\n"
    "DTSTART:20260902T100000Z\nDTEND:20260902T110000Z\n"
    f"SUMMARY:{SERIES[1]}\nRRULE:FREQ=WEEKLY;COUNT=6\n"
    "END:VEVENT\nEND:VCALENDAR\n")

for uid, summary, start, end, loc, desc, att in EVENTS:
    r = dav("PUT", f"{HOME}/arbeit/{uid}.ics",
            ics(uid, summary, start, end, loc, desc, att))
    if r.status_code not in (201, 204):
        check(f"event {uid} stored", False, r.status_code)
check(f"{len(EVENTS)} events stored", True)

VCARDS = [
    ("sascha", "Sascha Greiner-Adam", "Batix Software GmbH", "sga@batix.com",
     "+49 3641 1234"),
    ("ralph", "Ralph Kästner", "JASAA GmbH", "r.kaestner@jasaa.de",
     "+49 170 9999"),
    ("sandy", "Sandy Wohlfarth", "Beratungsboot", "ahoi@beratungsboot.de", ""),
]
for uid, fn, org, email, tel in VCARDS:
    card = (
        "BEGIN:VCARD\nVERSION:3.0\n"
        f"UID:{uid}\nFN:{fn}\nN:{fn.split()[-1]};{fn.split()[0]};;;\n"
        f"ORG:{org}\nEMAIL;TYPE=WORK:{email}\n"
        + (f"TEL;TYPE=CELL:{tel}\n" if tel else "")
        + "END:VCARD\n"
    )
    r = dav("PUT", f"{HOME}/kontakte/{uid}.vcf", card, "text/vcard; charset=utf-8")
    if r.status_code not in (201, 204):
        check(f"vcard {uid} stored", False, r.status_code)
check(f"{len(VCARDS)} contacts stored", True)

# --------------------------------------------------------------- config
tmp = Path(tempfile.mkdtemp())
cfg_file = tmp / "config.toml"
cfg_file.write_text(f"""
timezone = "Europe/Berlin"

[[account]]
name = "testdav"
username = "{USER}"
caldav_url = "{HOME}"
carddav_url = "{HOME}"
password_env = "E2E_DAV_PW"
writable = true

[[account]]
name = "readonly"
username = "{USER}"
caldav_url = "{HOME}"
carddav_url = "{HOME}"
password_env = "E2E_DAV_PW"
""")
os.environ["E2E_DAV_PW"] = PW

from dav_mcp.config import load_config
from dav_mcp import server as srv

config = load_config(cfg_file)
srv.configure(config)


async def run():
    print("\naccounts and collections")
    accounts = await srv.list_accounts()
    check("accounts listed", len(accounts) == 2)
    check("writable flag surfaced",
          accounts[0]["writable"] is True and accounts[1]["writable"] is False,
          accounts)
    check("no password in the output",
          not any("davpass" in str(v) for v in accounts[0].values()), accounts)

    cals = await srv.list_calendars(account="testdav")
    names = {c.get("calendar") for c in cals}
    check("calendar discovered", any("arbeit" in (n or "").lower() or
                                     "Arbeit" == n for n in names), cals)

    books = await srv.list_addressbooks(account="testdav")
    check("address book discovered", len(books) >= 1 and "error" not in books[0], books)

    print("\ncalendar window")
    res = await srv.list_events(accounts=["testdav"], start="2026-08-25", end="2026-09-10")
    titles = [e["summary"] for e in res["events"]]
    check("events inside the window returned",
          "Messe Jena Standabnahme" in titles and
          "Digital-Kompass Workshop" in titles, titles)
    check("EVENT OUTSIDE THE WINDOW EXCLUDED", "Altes Meeting" not in titles, titles)
    check("later event excluded too", "Gran Canaria" not in titles, titles)
    check("sorted by start",
          [e["start"] for e in res["events"]] ==
          sorted(e["start"] for e in res["events"]), titles)
    check("window echoed back", res["window"]["start"].startswith("2026-08-25"))
    check("no errors", "errors" not in res, res.get("errors"))

    first = next(e for e in res["events"] if e["summary"] == "Messe Jena Standabnahme")
    check("location returned", first["location"] == "Messegelände Jena", first)
    check("description returned", "Standgröße" in first["description"], first)
    check("attendee returned",
          any("sga@batix.com" in a for a in first["attendees"]), first)
    check("uid returned", first["uid"] == "messe")
    check("start is an ISO timestamp", first["start"].startswith("2026-09-01T"))

    res = await srv.list_events(accounts=["testdav"], start="2025-01-01", end="2027-01-01", limit=2)
    check("limit respected", len(res["events"]) == 2, len(res["events"]))

    try:
        await srv.list_events(accounts=["testdav"], start="2026-09-10", end="2026-09-01")
        check("reversed window rejected", False)
    except ValueError as exc:
        check("reversed window rejected", "after" in str(exc))
    try:
        await srv.list_events(accounts=["testdav"], start="irgendwann")
        check("unparseable date rejected", False)
    except ValueError as exc:
        check("unparseable date rejected", "date" in str(exc).lower())

    res = await srv.list_events(accounts=["testdav"], start="01.09.2026", end="10.09.2026")
    check("german date format accepted", len(res["events"]) >= 2, res["events"])

    print("\ncalendar search")
    res = await srv.search_events(accounts=["testdav"], query="Standgröße", start="2026-01-01",
                                  end="2027-01-01")
    check("search finds text in the description", len(res["events"]) == 1, res)
    res = await srv.search_events(accounts=["testdav"], query="Gran Canaria", start="2026-01-01",
                                  end="2027-01-01")
    check("search finds a title", len(res["events"]) == 1, res)
    res = await srv.search_events(accounts=["testdav"], query="jasaa", start="2026-01-01", end="2027-01-01")
    check("search matches an attendee", len(res["events"]) == 1, res)
    res = await srv.search_events(accounts=["testdav"], query="Messe Standgröße", start="2026-01-01",
                                  end="2027-01-01")
    check("terms are ANDed", len(res["events"]) == 1, res)
    res = await srv.search_events(accounts=["testdav"], query="Messe Gran", start="2026-01-01",
                                  end="2027-01-01")
    check("AND across different events finds nothing", res["events"] == [], res)
    res = await srv.search_events(accounts=["testdav"], query="gibtsnicht", start="2026-01-01",
                                  end="2027-01-01")
    check("no match explains the window", "note" in res, res)
    try:
        await srv.search_events(accounts=["testdav"], query="   ")
        check("empty query rejected", False)
    except ValueError:
        check("empty query rejected", True)

    print("\ncontacts")
    res = await srv.search_contacts(accounts=["testdav"], query="Batix")
    check("search by company", len(res["contacts"]) == 1, res)
    check("name returned", res["contacts"][0]["name"] == "Sascha Greiner-Adam", res)
    check("email returned",
          "sga@batix.com" in res["contacts"][0]["emails"], res)
    check("organisation returned",
          "Batix" in res["contacts"][0]["organisation"], res)

    res = await srv.search_contacts(accounts=["testdav"], query="r.kaestner@jasaa.de")
    check("search by email address", len(res["contacts"]) == 1, res)
    res = await srv.search_contacts(accounts=["testdav"], query="9999")
    check("search by phone number", len(res["contacts"]) == 1, res)
    res = await srv.search_contacts(accounts=["testdav"], query="Kästner")
    check("umlaut in the name found", len(res["contacts"]) == 1, res)
    res = await srv.search_contacts(accounts=["testdav"], query="niemand")
    check("no match returns empty", res["contacts"] == [], res)
    check("scanned count reported", res["scanned"] == 3, res)

    res = await srv.refresh_contacts(accounts=["testdav"])
    check("refresh re-reads the address book",
          res["accounts"][0]["contacts"] == 3, res)


    print("\nwrite guards")
    from dav_mcp.calendars import NotWritable as CalNotWritable

    try:
        await srv.create_event(account="readonly", calendar="Arbeit",
                               summary="Darf nicht", start="2026-09-15 10:00")
        check("READ-ONLY ACCOUNT REFUSES WRITES", False, "(event created!)")
    except CalNotWritable as exc:
        check("READ-ONLY ACCOUNT REFUSES WRITES", "read-only" in str(exc))

    print("\ncreating events")
    made = await srv.create_event(
        account="testdav", calendar="Arbeit", summary="Neuer Termin",
        start="2026-09-15 14:00", end="2026-09-15 15:30",
        location="Weimar", description="von dav-mcp angelegt")
    check("event created", made["summary"] == "Neuer Termin", made)
    check("location stored", made["location"] == "Weimar", made)
    NEW_UID = made["uid"]

    # Europe/Berlin is UTC+2 in September, so 14:00 local is 12:00 UTC.
    check("TIMEZONE APPLIED, NOT UTC",
          made["start"].startswith("2026-09-15T14:00") or
          "T12:00:00+00:00" in made["start"], made["start"])

    listed = await srv.list_events(accounts=["testdav"],
                                   start="2026-09-15", end="2026-09-16")
    check("created event is really on the server",
          any(e["uid"] == NEW_UID for e in listed["events"]), listed["events"])

    allday = await srv.create_event(
        account="testdav", calendar="Arbeit", summary="Ganztag",
        start="2026-09-20", all_day=True)
    check("all-day event created", allday["all_day"] is True, allday)
    check("all-day start is a plain date", allday["start"] == "2026-09-20", allday)
    check("ALL-DAY END IS EXCLUSIVE", allday["end"] == "2026-09-21", allday)

    try:
        await srv.create_event(account="testdav", calendar="Arbeit",
                               summary="Rückwärts", start="2026-09-15 15:00",
                               end="2026-09-15 14:00")
        check("end before start rejected", False)
    except ValueError as exc:
        check("end before start rejected", "after" in str(exc))

    try:
        await srv.create_event(account="testdav", summary="Ohne Kalender",
                               start="2026-09-15 10:00")
        check("ambiguous calendar rejected", False)
    except ValueError as exc:
        check("ambiguous calendar rejected", "named" in str(exc), str(exc)[:80])

    print("\nupdating events")
    changed = await srv.update_event(account="testdav", calendar="Arbeit",
                                     uid=NEW_UID, summary="Umbenannt",
                                     location="Jena")
    check("summary updated", changed["summary"] == "Umbenannt", changed)
    check("location updated", changed["location"] == "Jena", changed)
    check("changed fields reported",
          set(changed["changed_fields"]) == {"summary", "location"}, changed)
    check("untouched field kept",
          "dav-mcp" in changed["description"], changed["description"])

    try:
        await srv.update_event(account="testdav", calendar="Arbeit", uid=NEW_UID)
        check("empty update rejected", False)
    except ValueError as exc:
        check("empty update rejected", "Nothing to change" in str(exc))

    try:
        await srv.update_event(account="testdav", calendar="Arbeit",
                               uid="gibtsnicht", summary="x")
        check("unknown uid rejected", False)
    except ValueError as exc:
        check("unknown uid rejected", "No event with UID" in str(exc))

    print("\nrecurring series")
    try:
        await srv.update_event(account="testdav", calendar="Arbeit",
                               uid="serie", summary="Verschoben")
        check("SERIES REFUSED WITHOUT whole_series", False, "(changed!)")
    except CalNotWritable as exc:
        check("SERIES REFUSED WITHOUT whole_series",
              "whole_series" in str(exc), str(exc)[:80])

    ok = await srv.update_event(account="testdav", calendar="Arbeit",
                                uid="serie", summary="Jour Fixe neu",
                                whole_series=True)
    check("series changed when asked explicitly",
          ok["summary"] == "Jour Fixe neu", ok)
    check("result flags it as a series", ok["whole_series"] is True, ok)

    print("\ncontacts, writing")
    from dav_mcp.contacts import NotWritable as CardNotWritable

    try:
        await srv.create_contact(account="readonly", addressbook="Kontakte",
                                 name="Darf nicht")
        check("read-only account refuses contact writes", False)
    except CardNotWritable as exc:
        check("read-only account refuses contact writes", "read-only" in str(exc))

    person = await srv.create_contact(
        account="testdav", addressbook="Kontakte", name="Neue Person",
        emails=["neu@example.de"], phones=["+49 111 2222"],
        organisation="Beispiel GmbH", title="Geschäftsführung")
    check("contact created", person["name"] == "Neue Person", person)
    check("email stored", "neu@example.de" in person["emails"], person)
    check("organisation stored", "Beispiel" in person["organisation"], person)
    PERSON_UID = person["uid"]

    found = await srv.search_contacts(accounts=["testdav"], query="Beispiel")
    check("created contact is really on the server",
          any(c["uid"] == PERSON_UID for c in found["contacts"]), found)

    try:
        await srv.create_contact(account="testdav", addressbook="Kontakte", name="")
        check("nameless contact rejected", False)
    except ValueError as exc:
        check("nameless contact rejected", "name" in str(exc))

    updated = await srv.update_contact(
        account="testdav", uid=PERSON_UID, title="Prokura",
        emails=["neu@example.de", "zweite@example.de"])
    check("title updated", updated["title"] == "Prokura", updated)
    check("EMAILS REPLACED WHOLESALE",
          len(updated["emails"]) == 2, updated["emails"])
    check("untouched field kept", updated["name"] == "Neue Person", updated)
    check("phone kept", "+49 111 2222" in " ".join(updated["phones"]), updated)

    try:
        await srv.update_contact(account="testdav", uid=PERSON_UID)
        check("empty contact update rejected", False)
    except ValueError as exc:
        check("empty contact update rejected", "Nothing to change" in str(exc))

    try:
        await srv.update_contact(account="testdav", uid="gibtsnicht", name="x")
        check("unknown contact uid rejected", False)
    except ValueError as exc:
        check("unknown contact uid rejected", "No contact with UID" in str(exc))

    print("\nauth wiring")
    check("no provider without an [auth] block", srv.build_auth(config) is None)
    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    check("all tools registered",
          {"list_accounts", "list_calendars", "list_events", "search_events",
           "list_addressbooks", "search_contacts", "refresh_contacts"} <= names,
          sorted(names))
    check("write tools registered",
          {"create_event", "update_event", "create_contact",
           "update_contact"} <= names, sorted(names))
    check("NO DELETE TOOLS EXPOSED",
          not any("delete" in n or "remove" in n for n in names), sorted(names))

    from dav_mcp.server import needs
    read_only_token = type("T", (), {"scopes": ["calendar:read"]})()
    ctx_no_write = type("C", (), {"token": read_only_token})()
    ctx_none = type("C", (), {"token": None})()
    import dataclasses
    from dav_mcp.config import AuthConfig
    with_auth = dataclasses.replace(config, auth=AuthConfig(enabled=True))
    import dav_mcp.server as _srv
    original = _srv._config
    _srv._config = with_auth
    try:
        check("READ SCOPE CANNOT REACH A WRITE TOOL",
              needs("calendar:write")(ctx_no_write) is False)
        check("read scope reaches a read tool",
              needs("calendar:read")(ctx_no_write) is True)
        check("absent token is refused when auth is on",
              needs("calendar:read")(ctx_none) is False)
    finally:
        _srv._config = original
    check("scope check is permissive when auth is off",
          needs("calendar:write")(ctx_none) is True)


asyncio.run(run())

print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("All end-to-end checks passed.")
