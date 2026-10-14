"""Offline self-test: everything that does not need a live IMAP server."""

import asyncio
import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

FAILS = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


# ---------------------------------------------------------------- config
print("\nconfig")
tmp = Path(tempfile.mkdtemp())
cfg_file = tmp / "config.toml"
cfg_file.write_text(
    f"""
index_path = "{tmp}/headers.db"

[[account]]
name = "addonware"
host = "imap.mailbox.org"
username = "beck@addonware.de"
password_env = "TEST_AOW_PW"

[[account]]
name = "icloud"
host = "imap.mail.me.com"
username = "tim@icloud.com"
password_env = "TEST_ICLOUD_PW"
writable = true
"""
)

from imap_mcp.config import ConfigError, load_config

try:
    load_config(cfg_file)
    check("missing password rejected", False, "(no error raised)")
except ConfigError as exc:
    check("missing password rejected", "TEST_AOW_PW" in str(exc))

os.environ["TEST_AOW_PW"] = "secret-a"
os.environ["TEST_ICLOUD_PW"] = "secret-b"
config = load_config(cfg_file)

check("two accounts loaded", len(config.accounts) == 2)
check("writable defaults to false", config.accounts[0].writable is False)
check("writable honoured when set", config.accounts[1].writable is True)
check("password came from env", config.accounts[0].password == "secret-a")
check("lookup is case-insensitive", config.account("ADDONWARE").name == "addonware")
check(
    "default exclusions applied",
    config.accounts[0].excluded("Trash") and config.accounts[0].excluded("INBOX/Junk"),
)
check("normal folder not excluded", not config.accounts[0].excluded("Vertrieb"))
try:
    config.account(None)
    check("ambiguous account rejected", False)
except ConfigError:
    check("ambiguous account rejected", True)
try:
    config.account("nope")
    check("unknown account rejected", False)
except ConfigError:
    check("unknown account rejected", True)
check("resolve(None) returns all", len(config.resolve(None)) == 2)

# ---------------------------------------------------------------- search
print("\nsearch criteria")
from imap_mcp.search import build_criteria, split_terms

check('quoted phrase kept whole', split_terms('"quarterly report" budget') ==
      ["quarterly report", "budget"])

crit, non_ascii = build_criteria("angebot 2026", scope="body")
check("terms ANDed as separate BODY keys",
      crit == ["NOT", "DELETED", "BODY", "angebot", "BODY", "2026"], crit)
check("ascii query needs no charset", non_ascii is False)

crit, non_ascii = build_criteria("Grundstück", scope="all")
check("umlaut flags utf-8 charset", non_ascii is True)
check("scope all maps to TEXT", "TEXT" in crit)

crit, _ = build_criteria("x", scope="subject", since="2026-01-15",
                         unread_only=True, from_addr="a@b.de")
check("since parsed", any(getattr(c, "year", None) == 2026 for c in crit), crit)
check("unread filter added", "UNSEEN" in crit)
check("from filter added", "FROM" in crit and "a@b.de" in crit)

crit, _ = build_criteria("x", scope="subject", since="15.01.2026")
check("german date format parsed", any(getattr(c, "year", None) == 2026 for c in crit))

try:
    build_criteria("x", scope="bogus")
    check("bad scope rejected", False)
except ValueError:
    check("bad scope rejected", True)

try:
    build_criteria("x", since="nonsense")
    check("bad date rejected", False)
except ValueError:
    check("bad date rejected", True)

# ---------------------------------------------------------------- index
print("\nheader index")
from imap_mcp.index import HeaderIndex, escape_fts

check("fts injection neutralised", escape_fts('foo" OR bar') == '"foo""" "OR" "bar"',
      escape_fts('foo" OR bar'))
check("prefix search survives", escape_fts("meet*") == '"meet"*')

index = HeaderIndex(tmp / "headers.db")
rows = [
    {"subject": "Angebot Grundstück Jena", "sender": "Sandy <sandy@beratungsboot.de>",
     "recipients": "beck@addonware.de", "account": "addonware", "folder": "INBOX",
     "uid": 101, "date": "2026-08-01T09:00:00", "message_id": "<a@x>",
     "flags": "\\Seen"},
    {"subject": "Rechnung 611843", "sender": "Batix <billing@batix.com>",
     "recipients": "beck@addonware.de", "account": "addonware", "folder": "Kunden",
     "uid": 102, "date": "2026-07-01T09:00:00", "message_id": "<b@x>", "flags": ""},
    {"subject": "Messe Vorbereitung", "sender": "Jörg <jf@batix.com>",
     "recipients": "beck@addonware.de", "account": "icloud", "folder": "INBOX",
     "uid": 103, "date": "2026-06-01T09:00:00", "message_id": "<c@x>", "flags": ""},
]
check("rows inserted", index.add_headers(rows) == 3)

hits = index.search("Angebot")
check("subject hit found", len(hits) == 1 and hits[0]["uid"] == 101, hits)
check("read flag decoded", hits and hits[0]["read"] is True)

check("diacritics folded", len(index.search("Grundstuck")) == 1,
      index.search("Grundstuck"))
check("sender searchable", len(index.search("batix")) == 2)
check("account filter works",
      len(index.search("batix", accounts=["addonware"])) == 1)
check("folder filter works",
      len(index.search("batix", folders=["Kunden"])) == 1)
check("terms are ANDed", len(index.search("Messe Vorbereitung")) == 1)
check("nonsense finds nothing", index.search("zzzznothing") == [])
check("empty query is safe", index.search("   ") == [])

index.add_headers([rows[0]])
check("re-insert does not duplicate", len(index.search("Angebot")) == 1)

# Narrowing arguments must apply to the index, not only the live path.
check("since filter applies to the index",
      len(index.search("batix", since="2026-07-01")) == 1,
      index.search("batix", since="2026-07-01"))
check("since filter excludes older mail",
      index.search("batix", since="2026-09-01") == [])
check("before filter applies to the index",
      len(index.search("batix", before="2026-07-01")) == 1)
check("since and before combine",
      len(index.search("batix", since="2026-06-01", before="2026-08-01")) == 2)
check("from_addr filter applies to the index",
      len(index.search("batix", from_addr="jf@batix.com")) == 1,
      index.search("batix", from_addr="jf@batix.com"))
check("from_addr that matches nobody returns nothing",
      index.search("batix", from_addr="niemand@example.com") == [])

index.record_sync("addonware", "INBOX", uidvalidity=42, max_uid=101, message_count=1)
state = index.folder_state("addonware", "INBOX")
check("folder state recorded", state is not None and state["uidvalidity"] == 42)

index.reset_folder("addonware", "INBOX")
check("reset drops rows", index.search("Angebot") == [])
check("reset drops state", index.folder_state("addonware", "INBOX") is None)
check("other folders untouched", len(index.search("Rechnung")) == 1)

stats = index.stats()
check("stats report headers", stats["indexed_headers"] == 2, stats)

# ---------------------------------------------------------------- auth
print("\nauth configuration")


def load_auth(block: str):
    p = tmp / "auth.toml"
    p.write_text(f"""
index_path = "{tmp}/headers.db"
{block}

[[account]]
name = "a"
host = "h"
username = "u"
password_env = "TEST_AOW_PW"
""")
    return load_config(p)


check("auth is off unless enabled", load_auth("").auth.enabled is False)

good = """
[auth]
enabled = true
base_url = "https://mcp.timbeck.de"
authorization_servers = ["https://auth.timbeck.de"]
jwks_uri = "https://auth.timbeck.de/.well-known/jwks.json"
issuer = "https://auth.timbeck.de"
audience = "https://mcp.timbeck.de"
required_scopes = ["mail:read"]
"""
cfg_auth = load_auth(good).auth
check("auth block parsed", cfg_auth.enabled is True)
check("trailing slash stripped from base_url",
      cfg_auth.base_url == "https://mcp.timbeck.de")
check("scopes parsed", cfg_auth.required_scopes == ("mail:read",))

for label, missing_key in [
    ("audience", "audience"),
    ("base_url", "base_url"),
    ("authorization_servers", "authorization_servers"),
]:
    block = "\n".join(
        line for line in good.splitlines() if not line.startswith(missing_key)
    )
    try:
        load_auth(block)
        check(f"missing {label} rejected", False, "(no error)")
    except ConfigError as exc:
        check(f"missing {label} rejected", missing_key in str(exc))

no_keys = "\n".join(
    line for line in good.splitlines() if not line.startswith("jwks_uri")
)
try:
    load_auth(no_keys)
    check("missing signing key rejected", False)
except ConfigError as exc:
    check("missing signing key rejected", "jwks_uri" in str(exc))

single = good.replace(
    'authorization_servers = ["https://auth.timbeck.de"]',
    'authorization_servers = "https://auth.timbeck.de"',
)
check("single authorization server accepted as string",
      load_auth(single).auth.authorization_servers
      == ("https://auth.timbeck.de",))

from imap_mcp.server import build_auth

check("no provider built when auth is off", build_auth(load_auth("")) is None)
provider = build_auth(load_auth(good))
check("resource provider built when auth is on", provider is not None)

# ------------------------------------------------------- adaptive fetch
print("\nadaptive fetch (isolating unreadable messages)")
from imapclient.exceptions import IMAPClientError

from imap_mcp.sync import _fetch_adaptive


class FakeClient:
    """Raises the way iCloud does when a batch contains a bad message."""

    def __init__(self, poison):
        self.poison = set(poison)
        self.calls = 0

    def fetch(self, uids, keys):
        self.calls += 1
        if self.poison & set(uids):
            raise IMAPClientError(
                "Server replied with a response that violates the IMAP protocol"
            )
        return {u: {b"ENVELOPE": object()} for u in uids}


clean = FakeClient(poison=[])
data, failed = _fetch_adaptive(clean, list(range(1, 501)))
check("clean batch fetched in one call", clean.calls == 1 and len(data) == 500)
check("clean batch reports no failures", failed == [])

poisoned = FakeClient(poison=[137])
data, failed = _fetch_adaptive(poisoned, list(range(1, 501)))
check("one bad message does not sink the batch", len(data) == 499, len(data))
check("bad message identified exactly", failed == [137], failed)
check("bisection is cheap, not linear", poisoned.calls < 40, poisoned.calls)

multi = FakeClient(poison=[3, 200, 499])
data, failed = _fetch_adaptive(multi, list(range(1, 501)))
check("several bad messages isolated", sorted(failed) == [3, 200, 499], failed)
check("everything else still indexed", len(data) == 497, len(data))

all_bad = FakeClient(poison=list(range(1, 9)))
data, failed = _fetch_adaptive(all_bad, list(range(1, 9)))
check("entirely unreadable range degrades cleanly",
      data == {} and sorted(failed) == list(range(1, 9)), failed)
check("empty input is safe", _fetch_adaptive(clean, []) == ({}, []))

# Failures must be recorded, not just logged.
index.record_failures("icloud", "Sent Messages", [137, 200])
gaps = index.failures()
check("gaps recorded", gaps and gaps[0]["unreadable_messages"] == 2, gaps)
check("gaps surface in stats", index.stats()["unreadable_messages"] == 2)
index.record_failures("icloud", "Sent Messages", [137])
check("re-recording does not double count",
      index.stats()["unreadable_messages"] == 2, index.stats())
index.clear_failures("icloud", "Sent Messages")
check("gaps clear once a folder syncs cleanly",
      index.stats()["unreadable_messages"] == 0)

# ---------------------------------------------------------------- messages
print("\nmessage parsing")
from imap_mcp.messages import decode_mime, parse_full_message, snippet

check("rfc2047 decoded",
      decode_mime("=?UTF-8?Q?Angebot_f=C3=BCr_Gr=C3=BCndung?=") ==
      "Angebot für Gründung",
      decode_mime("=?UTF-8?Q?Angebot_f=C3=BCr_Gr=C3=BCndung?="))

msg = EmailMessage()
msg["Subject"] = "Angebot für Jena"
msg["From"] = "Sandy Wohlfarth <ahoi@beratungsboot.de>"
msg["To"] = "beck@addonware.de"
msg["Message-ID"] = "<test@example.de>"
msg.set_content("Hallo Tim,\n\nanbei das Angebot über 4.500 €.\n\nGrüße")
msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                   filename="Angebot_2026.pdf")
parsed = parse_full_message(msg.as_bytes())

check("subject decoded", parsed["subject"] == "Angebot für Jena", parsed["subject"])
check("body extracted", "4.500" in parsed["content"], parsed["content"][:60])
check("attachment listed", len(parsed["attachments"]) == 1)
check("attachment name right",
      parsed["attachments"][0]["filename"] == "Angebot_2026.pdf")
check("attachment mime right",
      parsed["attachments"][0]["mime_type"] == "application/pdf")

from imap_mcp.messages import extract_attachment
got = extract_attachment(msg.as_bytes(), "Angebot_2026.pdf")
check("attachment extracted", got is not None and got[0] == b"%PDF-1.4 fake")
check("missing attachment returns None",
      extract_attachment(msg.as_bytes(), "nope.pdf") is None)

html_msg = EmailMessage()
html_msg["Subject"] = "HTML only"
html_msg.set_content("<html><body><p>Hallo</p><p>Welt</p></body></html>",
                     subtype="html")
html_parsed = parse_full_message(html_msg.as_bytes())
check("html stripped to text",
      "Hallo" in html_parsed["content"] and "<p>" not in html_parsed["content"],
      html_parsed["content"][:60])

check("snippet centres on term",
      "Angebot" in snippet("Lorem ipsum " * 40 + "Angebot hier", "Angebot"))

# ---------------------------------------------------------------- server
print("\nserver wiring")
from imap_mcp import server as srv

srv.configure(config)


async def check_server():
    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    expected = {"list_accounts", "list_folders", "search", "get_email",
                "get_attachment", "index_status", "refresh_index", "mark", "move"}
    check("all tools registered", expected <= names, sorted(names))
    check("no send/delete tools exposed",
          not any(n in names for n in ("send", "send_email", "delete", "expunge")))

    accounts = await srv.list_accounts()
    check("list_accounts returns both", len(accounts) == 2)
    check("no password leaked in output",
          not any("secret" in str(v) for a in accounts for v in a.values()), accounts)
    check("writable surfaced", accounts[1]["writable"] is True)

    # The read-only guard is the compliance-relevant behaviour.
    try:
        await srv.mark(account="addonware", folder="INBOX", uids=[1], action="read")
        check("mark refused on read-only account", False, "(no error)")
    except ValueError as exc:
        check("mark refused on read-only account", "read-only" in str(exc))

    try:
        await srv.move(account="addonware", folder="INBOX", uids=[1],
                          target_folder="Kunden")
        check("move refused on read-only account", False, "(no error)")
    except ValueError as exc:
        check("move refused on read-only account", "read-only" in str(exc))

    # Header-scope search must not touch the network at all.
    result = await srv.search(query="Rechnung", scope="headers")
    check("header search works without network",
          result["results"] and result["results"][0]["uid"] == 102, result)
    check("header search reports its source", result["source"] == "local header index")

    result = await srv.search(query="Rechnung", scope="headers",
                                 accounts=["icloud"])
    check("header search respects account filter", result["results"] == [])


asyncio.run(check_server())

print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("All checks passed.")
