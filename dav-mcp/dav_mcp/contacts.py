"""CardDAV access.

CardDAV has no useful server-side search: the protocol's addressbook
query filters are optional and providers disagree about them. So an
address book is fetched whole, parsed once, and searched locally — which
is fine, because vCards are small and a personal address book is
thousands of entries at most, not millions.

The parsed result is cached per account so a series of questions about
contacts costs one fetch rather than one per question.
"""

from __future__ import annotations

import logging
import threading
import time
from xml.etree import ElementTree as ET

import httpx
import vobject

from .config import Account

logger = logging.getLogger(__name__)

DAV = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
NS = {"d": DAV, "card": CARD}

PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop><card:addressbook-home-set/></d:prop></d:propfind>"""

PROPFIND_COLLECTIONS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
  <d:resourcetype/><d:displayname/></d:prop></d:propfind>"""

REPORT_ALL_CARDS = """<?xml version="1.0" encoding="utf-8"?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop><d:getetag/><card:address-data/></d:prop>
</card:addressbook-query>"""


class CardDavError(RuntimeError):
    pass


_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


def _absolute(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    root = "/".join(base.split("/")[:3])  # scheme://host
    return root + href


def _request(account: Account, method: str, url: str, body: str, depth: str = "0"):
    headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
    try:
        with httpx.Client(
            timeout=account.timeout,
            follow_redirects=True,
            auth=(account.username, account.password),
        ) as client:
            response = client.request(method, url, content=body.encode(), headers=headers)
    except httpx.HTTPError as exc:
        raise CardDavError(f"{account.name}: cannot reach {url} ({exc})") from exc

    if response.status_code in (401, 403):
        raise CardDavError(
            f"{account.name}: CardDAV rejected the credentials "
            f"({response.status_code}). Some providers issue separate app "
            f"passwords for mail and for contacts — a mail-only password "
            f"fails exactly like this."
        )
    if response.status_code >= 400:
        raise CardDavError(
            f"{account.name}: {method} {url} returned {response.status_code}."
        )
    return response


def _find_href(xml: bytes, path: str) -> str | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CardDavError(f"Malformed XML from the server: {exc}") from exc
    node = root.find(path, NS)
    if node is None:
        return None
    href = node.find("d:href", NS)
    return href.text.strip() if href is not None and href.text else None


def discover_addressbooks(account: Account) -> list[dict]:
    """Walk principal -> home set -> collections."""
    base = account.carddav_url
    start = base if base.endswith(("/", "carddav")) else base + "/"

    response = _request(account, "PROPFIND", start, PROPFIND_PRINCIPAL)
    principal = _find_href(
        response.content, ".//d:response/d:propstat/d:prop/d:current-user-principal"
    )
    if not principal:
        raise CardDavError(
            f"{account.name}: the server did not return a user principal. "
            f"Check that {base} is the right CardDAV entry point."
        )

    response = _request(account, "PROPFIND", _absolute(start, principal), PROPFIND_HOME)
    home = _find_href(
        response.content, ".//d:response/d:propstat/d:prop/card:addressbook-home-set"
    )
    if not home:
        raise CardDavError(f"{account.name}: no addressbook-home-set advertised.")

    home_url = _absolute(start, home)
    response = _request(account, "PROPFIND", home_url, PROPFIND_COLLECTIONS, depth="1")

    books = []
    root = ET.fromstring(response.content)
    for entry in root.findall("d:response", NS):
        href_node = entry.find("d:href", NS)
        if href_node is None or not href_node.text:
            continue
        rtype = entry.find(".//d:prop/d:resourcetype", NS)
        if rtype is None or rtype.find("card:addressbook", NS) is None:
            continue
        name_node = entry.find(".//d:prop/d:displayname", NS)
        href = href_node.text.strip()
        books.append(
            {
                "account": account.name,
                "addressbook": (name_node.text if name_node is not None
                                and name_node.text else
                                href.rstrip("/").rsplit("/", 1)[-1]),
                "url": _absolute(home_url, href),
            }
        )
    return books


def _parse_vcard(raw: str, account: str, book: str, href: str = "") -> dict | None:
    try:
        card = vobject.readOne(raw)
    except Exception:
        return None

    def one(field: str) -> str:
        value = getattr(card, field, None)
        return str(value.value) if value is not None else ""

    emails, phones = [], []
    for child in card.contents.get("email", []):
        emails.append(str(child.value))
    for child in card.contents.get("tel", []):
        phones.append(str(child.value))

    addresses = []
    for child in card.contents.get("adr", []):
        parts = [p for p in
                 (getattr(child.value, a, "") for a in
                  ("street", "city", "code", "country")) if p]
        if parts:
            addresses.append(", ".join(str(p) for p in parts))

    name = one("fn")
    if not name:
        n = getattr(card, "n", None)
        if n is not None:
            name = " ".join(
                str(p) for p in (n.value.given, n.value.family) if p
            ).strip()

    return {
        "account": account,
        "addressbook": book,
        # Needed to write the contact back; a parsed copy without it
        # could be searched but never corrected.
        "href": href,
        "uid": one("uid"),
        "name": name,
        "organisation": ", ".join(str(p) for p in getattr(
            getattr(card, "org", None), "value", []) or []),
        "title": one("title"),
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "note": one("note")[:500],
    }


def fetch_contacts(account: Account, force: bool = False) -> list[dict]:
    """All contacts of an account, cached for contacts_cache_seconds."""
    with _lock:
        cached = _cache.get(account.name)
        if cached and not force and time.time() - cached[0] < account.contacts_cache_seconds:
            return cached[1]

    contacts: list[dict] = []
    for book in discover_addressbooks(account):
        response = _request(
            account, "REPORT", book["url"], REPORT_ALL_CARDS, depth="1"
        )
        root = ET.fromstring(response.content)
        for entry in root.findall("d:response", NS):
            data = entry.find(".//card:address-data", NS)
            if data is None or not data.text:
                continue
            href_node = entry.find("d:href", NS)
            href = (href_node.text or "").strip() if href_node is not None else ""
            parsed = _parse_vcard(
                data.text, account.name, book["addressbook"],
                _absolute(book["url"], href),
            )
            if parsed and (parsed["name"] or parsed["emails"]):
                contacts.append(parsed)

    with _lock:
        _cache[account.name] = (time.time(), contacts)
    logger.info("Cached %d contacts for %s", len(contacts), account.name)
    return contacts


def matches(contact: dict, terms: list[str]) -> bool:
    haystack = " ".join(
        [
            contact.get("name", ""),
            contact.get("organisation", ""),
            contact.get("title", ""),
            contact.get("note", ""),
            " ".join(contact.get("emails", [])),
            " ".join(contact.get("phones", [])),
            " ".join(contact.get("addresses", [])),
        ]
    ).lower()
    return all(term.lower() in haystack for term in terms)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ------------------------------------------------------------- writing


class NotWritable(RuntimeError):
    """The account forbids this change."""


def _require_writable(account: Account) -> None:
    if not account.writable:
        raise NotWritable(
            f"Account {account.name!r} is read-only. Set writable = true "
            f"for it in the config and restart the server to allow writes."
        )


def addressbook_by_name(account: Account, name: str | None) -> dict:
    """Find one address book. With several present, a name is required."""
    books = discover_addressbooks(account)
    if not books:
        raise CardDavError(f"{account.name}: this account has no address book.")
    if name is None:
        if len(books) == 1:
            return books[0]
        raise ValueError(
            f"{account.name} has several address books, so one must be "
            "named: " + ", ".join(b["addressbook"] for b in books)
        )
    for book in books:
        if book["addressbook"].lower() == name.lower():
            return book
    raise ValueError(
        f"{account.name} has no address book called {name!r}. Available: "
        + ", ".join(b["addressbook"] for b in books)
    )


def build_vcard(
    uid: str,
    name: str,
    emails: list[str],
    phones: list[str],
    organisation: str = "",
    title: str = "",
    note: str = "",
) -> str:
    """Assemble a vCard 3.0 document."""
    card = vobject.vCard()
    card.add("uid").value = uid
    card.add("fn").value = name

    parts = name.split()
    n = card.add("n")
    n.value = vobject.vcard.Name(
        family=parts[-1] if len(parts) > 1 else name,
        given=" ".join(parts[:-1]) if len(parts) > 1 else "",
    )

    for address in emails or []:
        entry = card.add("email")
        entry.value = address
        entry.type_param = "INTERNET"
    for number in phones or []:
        entry = card.add("tel")
        entry.value = number
        entry.type_param = "CELL"
    if organisation:
        card.add("org").value = [organisation]
    if title:
        card.add("title").value = title
    if note:
        card.add("note").value = note

    return card.serialize()


def _put_card(account: Account, url: str, body: str, etag: str | None = None):
    headers = {"Content-Type": "text/vcard; charset=utf-8"}
    if etag:
        # Refuse the write if the card changed since we read it, rather
        # than silently overwriting someone else's edit.
        headers["If-Match"] = etag
    try:
        with httpx.Client(
            timeout=account.timeout,
            follow_redirects=True,
            auth=(account.username, account.password),
        ) as client:
            response = client.put(url, content=body.encode(), headers=headers)
    except httpx.HTTPError as exc:
        raise CardDavError(f"{account.name}: cannot reach {url} ({exc})") from exc

    if response.status_code == 412:
        raise CardDavError(
            "The contact changed on the server since it was read. Search "
            "for it again and repeat the change."
        )
    if response.status_code >= 400:
        raise CardDavError(
            f"{account.name}: PUT {url} returned {response.status_code}."
        )
    return response


def create_contact(account: Account, addressbook: str | None, **fields) -> dict:
    """Write a new contact and return it as parsed back."""
    import uuid

    _require_writable(account)
    if not fields.get("name"):
        raise ValueError("A contact needs at least a name.")

    book = addressbook_by_name(account, addressbook)
    uid = str(uuid.uuid4())
    body = build_vcard(uid=uid, **fields)
    url = book["url"].rstrip("/") + f"/{uid}.vcf"
    _put_card(account, url, body)

    clear_cache()
    parsed = _parse_vcard(body, account.name, book["addressbook"], url)
    return parsed or {"uid": uid, "href": url}


def update_contact(account: Account, uid: str, changes: dict) -> dict:
    """Change fields of an existing contact, found by its UID."""
    _require_writable(account)

    match = next(
        (c for c in fetch_contacts(account) if c.get("uid") == uid), None
    )
    if match is None:
        raise ValueError(
            f"No contact with UID {uid!r} in {account.name}. Search for it "
            "first; the UID comes from the search result."
        )
    if not match.get("href"):
        raise CardDavError(
            "This contact has no stored location, so it cannot be written "
            "back. Refresh the contacts and try again."
        )

    # Read the current card so the write is based on what is on the
    # server now, not on a cached copy that may be minutes old.
    try:
        with httpx.Client(
            timeout=account.timeout, follow_redirects=True,
            auth=(account.username, account.password),
        ) as client:
            current = client.get(match["href"])
    except httpx.HTTPError as exc:
        raise CardDavError(f"{account.name}: cannot read the contact ({exc})") from exc
    if current.status_code >= 400:
        raise CardDavError(
            f"{account.name}: reading the contact returned {current.status_code}."
        )
    etag = current.headers.get("ETag")

    merged = {
        "name": changes.get("name") or match.get("name", ""),
        "emails": changes.get("emails") if changes.get("emails") is not None
                  else match.get("emails", []),
        "phones": changes.get("phones") if changes.get("phones") is not None
                  else match.get("phones", []),
        "organisation": changes.get("organisation")
                        if changes.get("organisation") is not None
                        else match.get("organisation", ""),
        "title": changes.get("title") if changes.get("title") is not None
                 else match.get("title", ""),
        "note": changes.get("note") if changes.get("note") is not None
                else match.get("note", ""),
    }
    if not any(changes.get(k) is not None for k in merged):
        raise ValueError("Nothing to change — every field was left empty.")

    body = build_vcard(uid=uid, **merged)
    _put_card(account, match["href"], body, etag)

    clear_cache()
    parsed = _parse_vcard(body, account.name, match["addressbook"], match["href"])
    result = parsed or {"uid": uid}
    result["changed_fields"] = [k for k in merged if changes.get(k) is not None]
    return result
