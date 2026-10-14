"""Command line entry point.

    dav-mcp serve     run the MCP server (stdio, or HTTP via env)
    dav-mcp check     test login and list what each account offers
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config


def cmd_serve(args) -> int:
    from .server import _transport_from_env, build_server

    server = build_server(args.config)
    transport, options = _transport_from_env()
    if transport == "http":
        print(
            f"Serving MCP over HTTP on {options['host']}:{options['port']}",
            file=sys.stderr,
        )
        server.run(transport="http", **options)
    else:
        server.run()
    return 0


def cmd_check(args) -> int:
    """Verify every account can log in, and count what it exposes."""
    from . import calendars, contacts

    config = load_config(Path(args.config) if args.config else None)
    failures = 0

    for account in config.accounts:
        parts = []
        if account.has_calendars:
            try:
                found = calendars.list_calendars(account)
                parts.append(f"{len(found)} Kalender")
            except Exception as exc:
                parts.append(f"Kalender FEHLER: {exc}")
                failures += 1
        if account.has_contacts:
            try:
                books = contacts.discover_addressbooks(account)
                parts.append(f"{len(books)} Adressbuch/-bücher")
            except Exception as exc:
                parts.append(f"Kontakte FEHLER: {exc}")
                failures += 1
        status = "OK  " if "FEHLER" not in " ".join(parts) else "FAIL"
        print(f"  {status} {account.name:<16} " + ", ".join(parts))

    calendars.close_all()
    if failures:
        print(f"\n{failures} Prüfung(en) fehlgeschlagen.", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dav-mcp",
        description="Read-only MCP server for CalDAV and CardDAV.",
    )
    parser.add_argument("-c", "--config", help="Path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="Test each account").set_defaults(func=cmd_check)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not getattr(args, "command", None):
        args.func = cmd_serve

    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
