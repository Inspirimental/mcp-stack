"""End-to-end test against a real Dovecot IMAP server on 127.0.0.1:1143."""

import asyncio
import imaplib
import os
import sys
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

HOST, PORT, USER, PW = "127.0.0.1", 1143, "testuser", "testpass"
FAILS = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


def make_msg(subject, sender, body, attach=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "beck@addonware.de"
    msg["Date"] = "Mon, 03 Aug 2026 09:12:00 +0200"
    msg.set_content(body)
    if attach:
        msg.add_attachment(
            attach[1], maintype="application", subtype="pdf", filename=attach[0]
        )
    return msg


# ------------------------------------------------------------ fixtures
print("\nfixtures")
imap = imaplib.IMAP4(HOST, PORT)
imap.login(USER, PW)
for folder in ("Kunden", "Vertrieb", "Trash"):
    imap.create(folder)

FIXTURES = [
    ("INBOX", make_msg(
        "Angebot für Grundstück Jena",
        "Sandy Wohlfarth <ahoi@beratungsboot.de>",
        "Hallo Tim,\n\nanbei das Angebot über 4.500 EUR für das Grundstück.\n\nGrüße",
        ("Angebot_2026.pdf", b"%PDF-1.4 fake-angebot"),
    )),
    ("INBOX", make_msg(
        "Messe Vorbereitung",
        "Jörg Flügge <jf@batix.com>",
        "Wir sollten die Standgröße klären. Budget liegt bei 12000 Euro.",
    )),
    ("Kunden", make_msg(
        "Rechnung 611843 zu Vertrag S 800/21",
        "Batix Software GmbH <billing@batix.com>",
        "Anbei die Rechnung 611843. Zahlbar innerhalb von 14 Tagen.",
    )),
    ("Vertrieb", make_msg(
        "Nachfassen Digital-Kompass",
        "Tim Beck <beck@addonware.de>",
        "Erinnerung: beim Kunden zur Ausschreibung nachfassen.",
    )),
    ("Trash", make_msg(
        "Geloeschte Nachricht",
        "spam@example.com",
        "Diese Nachricht liegt im Papierkorb und darf nicht gefunden werden.",
    )),
]
for folder, msg in FIXTURES:
    imap.append(folder, "", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
imap.logout()
check(f"{len(FIXTURES)} messages appended", True)

# ------------------------------------------------------------ config
tmp = Path(tempfile.mkdtemp())
cfg_file = tmp / "config.toml"
cfg_file.write_text(f"""
index_path = "{tmp}/headers.db"

[[account]]
name = "testbox"
host = "{HOST}"
port = {PORT}
ssl = false
username = "{USER}"
password_env = "E2E_PW"
writable = true
exclude_folders = ["Trash", "Junk"]
""")
os.environ["E2E_PW"] = PW

from imap_mcp.config import load_config
from imap_mcp import server as srv

config = load_config(cfg_file)
srv.configure(config)

# ------------------------------------------------------------ tests
async def run():
    print("\nconnection and folders")
    folders = await srv.list_folders()
    names = {f["folder"] for f in folders if "folder" in f}
    check("folders listed", {"INBOX", "Kunden", "Vertrieb"} <= names, names)
    check("excluded folder hidden", "Trash" not in names, names)
    inbox = next(f for f in folders if f["folder"] == "INBOX")
    check("message count read", inbox["messages"] == 2, inbox)
    check("unread count read", inbox["unread"] == 2, inbox)

    print("\nheader index")
    result = await srv.refresh_index()
    added = result["accounts"][0]["headers_added"]
    check("headers indexed", added == 4, result)
    check("no errors during sync", not result["accounts"][0]["errors"], result)

    hits = await srv.search(query="Rechnung", scope="headers")
    check("index finds subject", len(hits["results"]) == 1, hits)
    check("index returns folder", hits["results"][0]["folder"] == "Kunden", hits)

    hits = await srv.search(query="Grundstuck", scope="headers")
    check("index folds umlauts", len(hits["results"]) == 1, hits)

    hits = await srv.search(query="batix", scope="headers")
    check("index searches sender", len(hits["results"]) == 2, hits)

    hits = await srv.search(query="Papierkorb Geloeschte", scope="headers")
    check("excluded folder not indexed", hits["results"] == [], hits)

    # Second sync must be a no-op — this is the incremental path.
    again = await srv.refresh_index()
    check("re-sync adds nothing", again["accounts"][0]["headers_added"] == 0, again)

    print("\nlive IMAP search")
    hits = await srv.search(query="Standgröße", scope="body")
    check("live body search finds text", len(hits["results"]) == 1, hits)
    check("live result marked as live",
          hits["results"] and hits["results"][0]["source"] == "live", hits)

    hits = await srv.search(query="4.500", scope="body")
    check("body-only phrase not in index",
          len(hits["results"]) == 1, hits)
    header_only = await srv.search(query="4.500", scope="headers")
    check("same phrase absent from header index",
          header_only["results"] == [], header_only)

    hits = await srv.search(query="Zahlbar", scope="body", folders=["Kunden"])
    check("folder-scoped live search", len(hits["results"]) == 1, hits)

    hits = await srv.search(query="Papierkorb", scope="body")
    check("live search skips excluded folders", hits["results"] == [], hits)

    hits = await srv.search(query="nachfassen", scope="body")
    check("live search is case-insensitive", len(hits["results"]) == 1, hits)

    hits = await srv.search(query="Angebot", scope="subject")
    check("subject scope works", len(hits["results"]) == 1, hits)

    hits = await srv.search(query="batix", scope="sender")
    check("sender scope works", len(hits["results"]) == 2, hits)

    hits = await srv.search(query="Angebot", scope="body", since="2026-01-01")
    check("since filter passes through", len(hits["results"]) == 1, hits)
    hits = await srv.search(query="Angebot", scope="body", since="2027-01-01")
    check("future since filter excludes", hits["results"] == [], hits)

    hits = await srv.search(query="Angebot", scope="body", unread_only=True)
    check("unread filter works", len(hits["results"]) == 1, hits)

    print("\nreading")
    target = (await srv.search(query="Grundstück", scope="subject"))["results"][0]
    uid, folder = target["uid"], target["folder"]
    check("message is unread before reading", target["read"] is False, target)

    mail = await srv.get_email(account="testbox", folder=folder, uid=uid)
    check("subject decoded", mail["subject"] == "Angebot für Grundstück Jena",
          mail["subject"])
    check("body returned", "4.500 EUR" in mail["content"], mail["content"][:80])
    check("attachment listed", len(mail["attachments"]) == 1, mail["attachments"])
    check("attachment named",
          mail["attachments"][0]["filename"] == "Angebot_2026.pdf")

    # The central read-only claim: BODY.PEEK must not set \Seen.
    after = await srv.search(query="Grundstück", scope="subject")
    check("READING DID NOT MARK AS SEEN",
          after["results"][0]["read"] is False, after["results"][0])

    att = await srv.get_attachment(account="testbox", folder=folder, uid=uid,
                                   filename="Angebot_2026.pdf")
    import base64
    check("attachment content correct",
          base64.b64decode(att["content_base64"]) == b"%PDF-1.4 fake-angebot", att)
    check("attachment mime correct", att["mime_type"] == "application/pdf", att)

    try:
        await srv.get_attachment(account="testbox", folder=folder, uid=uid,
                                 filename="gibtsnicht.pdf")
        check("unknown attachment rejected", False)
    except ValueError as exc:
        check("unknown attachment rejected", "No attachment named" in str(exc))

    try:
        await srv.get_email(account="testbox", folder=folder, uid=999999)
        check("unknown uid rejected", False)
    except ValueError as exc:
        check("unknown uid rejected", "No message with UID" in str(exc))

    print("\nwrite operations")
    res = await srv.mark(account="testbox", folder=folder, uids=[uid], action="read")
    check("mark accepted on writable account", res["action"] == "read", res)
    after = await srv.search(query="Grundstück", scope="subject")
    check("message now flagged as seen", after["results"][0]["read"] is True, after)
    # The index must not keep serving the pre-mark flag state.
    indexed = await srv.search(query="Grundstück", scope="headers")
    check("index flags updated after mark",
          indexed["results"] and indexed["results"][0]["read"] is True, indexed)

    await srv.mark(account="testbox", folder=folder, uids=[uid], action="flagged")
    after = await srv.search(query="Grundstück", scope="subject")
    check("flag applied", after["results"][0]["flagged"] is True, after)

    await srv.mark(account="testbox", folder=folder, uids=[uid], action="unread")
    after = await srv.search(query="Grundstück", scope="subject")
    check("unread restored", after["results"][0]["read"] is False, after)

    res = await srv.move(account="testbox", folder="INBOX", uids=[uid],
                         target_folder="Vertrieb")
    check("move reported success", res["moved"] == 1, res)
    moved = await srv.search(query="Grundstück", scope="subject",
                             folders=["Vertrieb"])
    check("message arrived in target folder", len(moved["results"]) == 1, moved)
    gone = await srv.search(query="Grundstück", scope="subject", folders=["INBOX"])
    check("message left source folder", gone["results"] == [], gone)

    try:
        await srv.move(account="testbox", folder="Vertrieb", uids=[1],
                       target_folder="GibtsNicht")
        check("move to unknown folder rejected", False)
    except ValueError as exc:
        check("move to unknown folder rejected", "does not exist" in str(exc))

    print("\nindex maintenance")
    stats = await srv.index_status()
    check("all folders still tracked after move", stats["folders"] >= 3, stats)
    check("status reports a sync time", stats["last_sync"] is not None, stats)

    # Only the moved message should have been dropped, not the folder.
    stale = await srv.search(query="Grundstück", scope="headers",
                             folders=["INBOX"])
    check("moved message dropped from source index", stale["results"] == [], stale)
    survivor = await srv.search(query="Messe", scope="headers", folders=["INBOX"])
    check("other INBOX rows survived the move",
          len(survivor["results"]) == 1, survivor)

    resynced = await srv.refresh_index()
    check("incremental resync picks up the move",
          resynced["accounts"][0]["headers_added"] == 1, resynced)
    found = await srv.search(query="Grundstück", scope="headers")
    check("moved message indexed under target folder",
          found["results"] and found["results"][0]["folder"] == "Vertrieb", found)


asyncio.run(run())

print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("All end-to-end checks passed.")
