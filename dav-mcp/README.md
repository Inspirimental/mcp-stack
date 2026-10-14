# dav-mcp

MCP server for CalDAV calendars and CardDAV address books across
several accounts. Reading is always available; writing is opt-in twice
over and never deletes. The sibling of `imap-mcp`: same shape, same
authorization server, different resource.

## Writing: two gates, and no deleting

Reading needs nothing beyond a valid token. Writing needs **both**:

1. the token carries the write scope (`calendar:write`, `contacts:write`)
2. the account sets `writable = true` in the config

Neither substitutes for the other. The scope says what a caller may ask
for; the flag says what you have allowed at all. A test asserts that a
read-scoped token cannot reach a write tool
(`READ SCOPE CANNOT REACH A WRITE TOOL`).

**There is no delete tool** — not for events, not for contacts. CalDAV
has no wastebasket, and an assistant that can delete a calendar entry
can delete the wrong one irrecoverably. A test asserts their absence
(`NO DELETE TOOLS EXPOSED`), so adding one later has to be a decision
someone makes on purpose.

### Recurring events

`list_events` expands a series into its occurrences, but every
occurrence carries the **same UID**. A change made through that UID
therefore hits the whole series, not the one date you were looking at.

`update_event` refuses a recurring event unless you pass
`whole_series=true`. The refusal message says why. This is the single
most common way calendar integrations quietly wreck a schedule.

### Times without a zone

`timezone` in the config decides how "2026-09-01 14:00" is read. It
defaults to UTC, which is almost certainly wrong for you — set it. When
reading, this never matters, because providers return zoned timestamps.
When writing, it is the difference between 14:00 and 16:00.

### Unknown keys are rejected

The config loader refuses any key it does not know, at every level. A
misplaced or misspelled setting would otherwise be ignored in silence
and the server would run with a default nobody chose — `timezone`
inside an `[[account]]` block being the expensive example, since it
looks plausible and costs two hours per appointment.

### Replacing versus adding

`update_contact` REPLACES the email and phone lists rather than
appending to them. Read the contact first and pass back what you want
to keep. This is stated in the tool description too, because it is the
kind of thing an assistant gets wrong by being helpful.

## How the two protocols are used differently

**Calendars — the server does the work.** A date range is exactly what
CalDAV is good at: one request, and the provider expands recurring
events into their occurrences inside the window. Every calendar tool
therefore takes a window, defaulting to today plus four weeks.

**Contacts — the client does the work.** CardDAV's search filters are
optional and providers disagree about them, so address books are fetched
whole, parsed once and searched locally. That is affordable: vCards are
small and a personal address book runs to thousands of entries, not
millions. The parsed result is cached per account (15 minutes by
default), so a series of questions costs one fetch.

Free-text calendar search is likewise local, over whatever the window
returned. It reports how many events it scanned, and says so when
nothing matched, because "no hits" and "wrong window" look identical
otherwise.

## Passwords: not always the same one as for mail

| Provider | Calendar and contacts password |
|---|---|
| iCloud | The same app-specific password as for IMAP works. |
| mailbox.org | A **different** one. The Mail-App-Passwort covers IMAP and SMTP only. Create an *Applikationspasswort* of type "Kalender- und Adressbuch-Client (CalDAV/CardDAV)" under Einstellungen → Sicherheit → Applikationspasswörter — a separate list from the mail passwords. |

A mail-only password fails with a plain 401, which reads like a typo
rather than a category error. The server's message says as much.

## Endpoints

```toml
# iCloud
caldav_url  = "https://caldav.icloud.com"
carddav_url = "https://contacts.icloud.com"

# mailbox.org — one host serves both
caldav_url  = "https://dav.mailbox.org"
carddav_url = "https://dav.mailbox.org"
```

Discovery is automatic from there: principal → home set → collections.

## Setup

```bash
cp config.example.toml config.toml    # usernames and endpoints
cp .env.example .env && chmod 600 .env
dav-mcp check
#   OK   icloud           4 Kalender, 1 Adressbuch/-bücher
#   OK   addonware        2 Kalender, 1 Adressbuch/-bücher
```

`check` logs in, counts collections, and exits. Run it before wiring
anything up.

## Tools

| Tool | Purpose |
|---|---|
| `list_accounts` | Configured accounts and what each offers |
| `list_calendars` | Calendars per account |
| `list_events` | Events in a window, recurrences expanded |
| `search_events` | Text search within a window |
| `list_addressbooks` | Address books per account |
| `search_contacts` | Search name, company, email, phone, address, notes |
| `refresh_contacts` | Discard the cache and re-fetch |
| `create_event` | New appointment — needs `calendar:write` |
| `update_event` | Change one — needs `calendar:write` |
| `create_contact` | New contact — needs `contacts:write` |
| `update_contact` | Change one — needs `contacts:write` |

## Authentication

Identical to `imap-mcp`: enable the `[auth]` block and this becomes an
OAuth 2.1 resource server verifying tokens from your own authorization
server.

**Its `audience` must differ from the mail server's.** That difference
is the whole point: a token minted for `…/dav` is rejected by
`…/mcp` and vice versa, so a calendar connector cannot read mail. In
`mcp-auth`, list both resources:

```
MCP_AUTH_RESOURCE=https://mcp.timbeck.de/mcp,https://mcp.timbeck.de/dav
MCP_AUTH_SCOPES=mail:read,calendar:read,calendar:write,contacts:read,contacts:write
```

## Tests

```bash
python3 selftest.py    # offline: config, dates, matching
python3 e2etest.py     # needs a CalDAV/CardDAV server on 127.0.0.1:5232
```

146 checks in total, and both suites run with DeprecationWarning treated as an error. The end-to-end suite was written against a throwaway Radicale instance
and covers what only a real server exercises: collection discovery,
window boundaries, recurrence handling, vCard parsing with umlauts, and
the fact that an event outside the requested window does not appear,
and every write path including the guards that refuse one.

Both suites are rerunnable. The end-to-end run empties its collections
before seeding them, so the second run sees exactly what the first one
did — a suite that only passes on a fresh server is a suite that stops
being checked.
