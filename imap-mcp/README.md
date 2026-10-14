# imap-mcp

One MCP server for several IMAP mailboxes. Search across all of them in a
single query, read messages, and — only where you explicitly allow it —
mark and move mail.

Built as an alternative to Apple-Mail-based MCP servers, which need macOS
Full Disk Access to reach `~/Library/Mail`, only work while the Mac is
running, and depend on a local index that fails silently when the
permission is missing.

## Design decisions worth knowing

**No Full Disk Access, no Mail.app.** The server talks IMAP to the
providers directly. It runs anywhere — a Linux box, a container — and
works whether or not your laptop is on.

**Hybrid search.** Two paths, chosen per query:

| Scope | Where it runs | Speed | Searches |
|---|---|---|---|
| `headers` (default) | local SQLite FTS5 index | milliseconds | subject, sender, recipient |
| `all`, `body`, `subject`, `sender`, `recipient` | live on the provider | seconds | including message text |

The local index stores **envelopes only** — subject, sender, recipients,
date, message-id, flags. Message bodies are never written to disk. Body
search always goes live via IMAP `SEARCH`, so the text stays on the
provider's servers. That is the whole point of the split: you get fast
lookup without keeping a second copy of your mail.

**Reading never changes state.** Folders are selected read-only and
bodies are fetched with `BODY.PEEK[]`, so opening a message does not mark
it as seen.

**Writes are opt-in per account.** `mark` and `move` refuse to run
unless that account sets `writable = true`. There is no delete tool and
no SMTP — the server cannot send mail or destroy anything.

**Passwords are never in the config file.** Each account names an
environment variable instead. Use app-specific passwords, not your main
account password.

## Setup

```bash
cp config.example.toml config.toml    # edit hosts and usernames
cp .env.example .env                  # fill in app passwords, chmod 600
```

App passwords:

- **mailbox.org** — Einstellungen → Sicherheit → Mail-App-Passwörter.
  Required once 2FA is on; make sure IMAP is permitted for the password.
- **iCloud** — appleid.apple.com → Sign-In and Security → App-Specific
  Passwords. The regular Apple ID password will not authenticate.

Verify before wiring anything up:

```bash
imap-mcp check
#   OK   addonware            27 folders  (read-only)
#   OK   icloud               14 folders  (read-only)
```

Build the header index (first run takes a few minutes, later runs are
incremental):

```bash
imap-mcp index --verbose
imap-mcp status
```

### When the server returns garbage

Some providers — iCloud is the usual culprit — answer certain FETCH
requests with output that violates the IMAP grammar. Rather than let one
bad message take a whole batch of 500 down with it, the fetch bisects the
range until the offending UIDs are isolated. Everything around them is
still indexed, and the unreadable UIDs are recorded rather than logged
and forgotten:

```bash
imap-mcp status | jq '.unreadable_messages, .gaps'
```

`gaps` names the account, folder and count. An index with holes it can
name is fine; an index with holes it cannot is not.

To pull folders that were skipped by an earlier run, reset their
watermark and read them again — the incremental sync alone will not
revisit them, because it only asks for UIDs above the last one it saw:

```bash
imap-mcp reindex icloud "Sent Messages" "Ebay"   # named folders
imap-mcp reindex icloud                          # the whole account
```

## Running

**Local, stdio:**

```bash
imap-mcp serve
```

**On a server, over HTTP:**

```bash
docker compose up -d
```

The compose file starts two containers: the MCP server, and an indexer
that runs the incremental sync hourly. They share the index through a
named volume with SQLite in WAL mode.

The MCP endpoint is `POST /mcp` (streamable HTTP), so a reverse proxy
entry looks like `https://mcp.example.de/mcp` → `127.0.0.1:8770/mcp`.

Port 8770 is published on `127.0.0.1` only.

## Authentication

**A reverse proxy cannot do this for you.** Claude's custom connectors
support exactly two modes: authless, or OAuth with Dynamic Client
Registration. There is no field for a username, a bearer token, or a
pre-registered client ID. HTTP basic auth, an API key header, and
cookie-based `forward_auth` all fail — the MCP client has no way to
present any of them.

So authentication lives in the server. Enable the `[auth]` block and
imap-mcp becomes an OAuth 2.1 resource server:

- unauthenticated requests get `401` with a `WWW-Authenticate` header
- `/.well-known/oauth-protected-resource/mcp` advertises your
  authorization server (RFC 9728)
- incoming tokens are verified against your JWKS, checking issuer,
  signature, expiry, audience and scopes

imap-mcp does not issue tokens and has no user database. You bring an
authorization server. It must:

1. support **Dynamic Client Registration** (RFC 7591) or Client ID
   Metadata Documents — Claude registers itself and cannot be given a
   client ID
2. publish discovery metadata (RFC 8414 or OpenID Connect Discovery)
3. honour the `resource` parameter (RFC 8707) and mint tokens whose
   `aud` is **this** server's URL
4. publish a JWKS endpoint

Point three is the one that bites: an authorization server already
issuing tokens for a different service usually stamps that service as
the audience. It has to be able to issue for a second resource, or the
tokens will be rejected here — deliberately, because accepting them
would let any token in your estate read your mail.

With `enabled = false` the server logs a warning on every start. That is
only acceptable for stdio or a loopback-only bind.

## Tests

```bash
python3 selftest.py    # offline: config, criteria, index, MIME parsing
python3 e2etest.py     # needs an IMAP server on 127.0.0.1:1143
```

`selftest.py` runs anywhere. `e2etest.py` was written against a throwaway
Dovecot instance and covers the parts that only a real server exercises:
UTF-8 search, incremental sync, UID handling across a move, and the
guarantee that reading a message does not mark it as seen.

## Tools

| Tool | Purpose |
|---|---|
| `list_accounts` | Configured mailboxes and their write permission |
| `list_folders` | Folders with message and unread counts |
| `search` | Cross-account search, hybrid index/live |
| `get_email` | Full message with body and attachment list |
| `get_attachment` | One attachment, base64, up to 5 MB |
| `index_status` | Index freshness per folder, plus any gaps |
| `refresh_index` | Incremental header sync |
| `mark` | Read/unread, flagged/unflagged — needs `writable` |
| `move` | Move to another folder — needs `writable` |

## Notes and limits

Live search does one round-trip per folder per account. With three
accounts and 27 folders that is a few seconds. Pass `folders=[...]` to
narrow it when you know where to look.

IMAP `SEARCH` quality varies by provider. Both mailbox.org (Dovecot) and
iCloud support `BODY` and `TEXT` searches; substring matching and
stemming behaviour differ, so results are not identical across accounts.

`move` invalidates UIDs on both sides, so the affected folders are
dropped from the index and need a `refresh_index` afterwards.

The first index pass is capped at `index_max_per_folder` (default 20000)
newest messages per folder. Raise it if you need deeper history in the
fast path; body search reaches everything regardless.
