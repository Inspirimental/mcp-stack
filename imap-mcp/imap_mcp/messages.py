"""Turning IMAP wire data into plain Python dicts.

IMAP hands back RFC 2047 encoded headers (``=?UTF-8?Q?...?=``) and
MIME trees. Everything user-visible gets decoded here so the tools can
return clean UTF-8 strings.
"""

from __future__ import annotations

import email
import email.policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from typing import Any

MAX_BODY_CHARS = 100_000


def decode_mime(value: Any) -> str:
    """Decode an RFC 2047 header value to a plain string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            value = value.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def format_address(addr: Any) -> str:
    """Render an IMAPClient Address as ``Name <user@host>``."""
    if addr is None:
        return ""

    def part(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    mailbox = part(getattr(addr, "mailbox", ""))
    host = part(getattr(addr, "host", ""))
    name = decode_mime(getattr(addr, "name", None))
    email_addr = f"{mailbox}@{host}" if mailbox and host else mailbox or host
    if name and email_addr:
        return f"{name} <{email_addr}>"
    return name or email_addr


def format_addresses(addrs: Any) -> str:
    if not addrs:
        return ""
    return ", ".join(filter(None, (format_address(a) for a in addrs)))


def envelope_to_dict(
    uid: int,
    account: str,
    folder: str,
    envelope: Any,
    flags: tuple = (),
    internaldate: Any = None,
    size: int | None = None,
) -> dict:
    """Flatten an IMAP ENVELOPE into a result row."""
    flag_names = [
        f.decode("ascii", errors="replace") if isinstance(f, bytes) else str(f)
        for f in (flags or ())
    ]
    date = getattr(envelope, "date", None) or internaldate
    return {
        "uid": uid,
        "account": account,
        "folder": folder,
        "subject": decode_mime(getattr(envelope, "subject", b"")),
        "from": format_addresses(getattr(envelope, "from_", None)),
        "to": format_addresses(getattr(envelope, "to", None)),
        "cc": format_addresses(getattr(envelope, "cc", None)),
        "date": date.isoformat() if hasattr(date, "isoformat") else None,
        "message_id": decode_mime(getattr(envelope, "message_id", b"")),
        "read": "\\Seen" in flag_names,
        "flagged": "\\Flagged" in flag_names,
        "answered": "\\Answered" in flag_names,
        "size": size,
    }


def _best_text(msg: EmailMessage) -> str:
    """Pick the most readable body part: plain text, else stripped HTML."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        try:
            return plain.get_content()
        except Exception:
            payload = plain.get_payload(decode=True) or b""
            return payload.decode("utf-8", errors="replace")

    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        try:
            raw = html.get_content()
        except Exception:
            raw = (html.get_payload(decode=True) or b"").decode(
                "utf-8", errors="replace"
            )
        return strip_html(raw)

    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return ""


def strip_html(html: str) -> str:
    """Crude but dependency-free HTML to text."""
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_full_message(raw: bytes) -> dict:
    """Parse a complete RFC 822 message into content plus attachment list."""
    msg: EmailMessage = email.message_from_bytes(
        raw, policy=email.policy.default
    )  # type: ignore[assignment]

    attachments = []
    for part in msg.iter_attachments():
        filename = part.get_filename()
        if filename:
            filename = decode_mime(filename)
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            {
                "filename": filename or "(unnamed)",
                "mime_type": part.get_content_type(),
                "size": len(payload),
            }
        )

    body = _best_text(msg)
    truncated = len(body) > MAX_BODY_CHARS
    if truncated:
        body = body[:MAX_BODY_CHARS]

    return {
        "subject": decode_mime(msg.get("Subject")),
        "from": decode_mime(msg.get("From")),
        "to": decode_mime(msg.get("To")),
        "cc": decode_mime(msg.get("Cc")),
        "reply_to": decode_mime(msg.get("Reply-To")),
        "date": msg.get("Date"),
        "message_id": (msg.get("Message-ID") or "").strip(),
        "content": body,
        "content_truncated": truncated,
        "attachments": attachments,
    }


def extract_attachment(raw: bytes, filename: str) -> tuple[bytes, str] | None:
    """Pull one attachment out of a raw message by filename."""
    msg: EmailMessage = email.message_from_bytes(
        raw, policy=email.policy.default
    )  # type: ignore[assignment]
    for part in msg.iter_attachments():
        name = part.get_filename()
        if name and decode_mime(name) == filename:
            return part.get_payload(decode=True) or b"", part.get_content_type()
    return None


def snippet(text: str, query: str, width: int = 200) -> str:
    """A short excerpt centred on the first query term that appears."""
    if not text:
        return ""
    flat = " ".join(text.split())
    for term in query.split():
        term = term.strip('"')
        if not term:
            continue
        pos = flat.lower().find(term.lower())
        if pos >= 0:
            start = max(0, pos - width // 3)
            excerpt = flat[start : start + width]
            return ("…" if start else "") + excerpt + ("…" if len(flat) > start + width else "")
    return flat[:width] + ("…" if len(flat) > width else "")
