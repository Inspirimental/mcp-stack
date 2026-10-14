"""Building IMAP SEARCH criteria.

IMAP's own search runs on the provider's servers, so message bodies
never have to be copied locally. Criteria in a list are implicitly
ANDed by the protocol, which is what we want for multi-word queries:
``["BODY", "angebot", "BODY", "2026"]`` means both words must appear.
"""

from __future__ import annotations

import shlex
from datetime import date, datetime

# Which IMAP key each scope maps to. TEXT covers headers *and* body.
SCOPE_KEYS = {
    "all": "TEXT",
    "body": "BODY",
    "subject": "SUBJECT",
    "sender": "FROM",
    "recipient": "TO",
}


def split_terms(query: str) -> list[str]:
    """Split a query into terms, honouring "quoted phrases"."""
    query = query.strip()
    if not query:
        return []
    try:
        terms = shlex.split(query)
    except ValueError:
        # Unbalanced quote — fall back to whitespace splitting.
        terms = query.split()
    return [t for t in terms if t]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse date {value!r}. Use YYYY-MM-DD (e.g. 2026-08-01)."
    )


def build_criteria(
    query: str,
    scope: str = "all",
    since: str | None = None,
    before: str | None = None,
    from_addr: str | None = None,
    subject: str | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
) -> tuple[list, bool]:
    """Assemble IMAP SEARCH criteria.

    Returns the criteria list and whether any term is non-ASCII (which
    decides if the request needs an explicit UTF-8 charset).
    """
    key = SCOPE_KEYS.get(scope)
    if key is None:
        raise ValueError(
            f"Unknown scope {scope!r}. Use one of: {', '.join(SCOPE_KEYS)}"
        )

    criteria: list = ["NOT", "DELETED"]
    non_ascii = False

    for term in split_terms(query):
        criteria.extend([key, term])
        if not term.isascii():
            non_ascii = True

    # Extra narrowing filters, ANDed on top of the free-text terms.
    if from_addr:
        criteria.extend(["FROM", from_addr])
        non_ascii = non_ascii or not from_addr.isascii()
    if subject:
        criteria.extend(["SUBJECT", subject])
        non_ascii = non_ascii or not subject.isascii()
    if unread_only:
        criteria.append("UNSEEN")
    if flagged_only:
        criteria.append("FLAGGED")

    if (since_date := _parse_date(since)) is not None:
        criteria.extend(["SINCE", since_date])
    if (before_date := _parse_date(before)) is not None:
        criteria.extend(["BEFORE", before_date])

    # A bare ["NOT", "DELETED"] with no other term matches everything,
    # which is a legitimate "list the folder" request.
    return criteria, non_ascii
