"""Command line entry point.

    imap-mcp serve          run the MCP server (stdio, or HTTP via env)
    imap-mcp check          test login to every configured account
    imap-mcp index          refresh the header index
    imap-mcp status         show index freshness per folder
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(args) -> "tuple":
    from .index import HeaderIndex
    from .pool import SessionRegistry

    config = load_config(Path(args.config) if args.config else None)
    return config, SessionRegistry(config.accounts), HeaderIndex(config.index_path)


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
    """Verify that every account can log in and list folders."""
    from .sync import selectable_folders

    config, registry, _ = _load(args)
    failures = 0
    for account in config.accounts:
        session = registry.get(account)
        try:
            with session.session() as client:
                folders = selectable_folders(client, account)
            mode = "read+write" if account.writable else "read-only"
            print(f"  OK   {account.name:<20} {len(folders):>3} folders  ({mode})")
        except Exception as exc:
            failures += 1
            print(f"  FAIL {account.name:<20} {exc}")
    registry.close_all()

    if failures:
        print(f"\n{failures} account(s) failed.", file=sys.stderr)
    return 1 if failures else 0


def cmd_index(args) -> int:
    from .sync import sync_account

    config, registry, index = _load(args)
    accounts = config.resolve(args.accounts)

    def progress(message: str) -> None:
        if args.verbose:
            print(f"\r{message:<70}", end="", flush=True)

    total = 0
    for account in accounts:
        result = sync_account(registry.get(account), index, account, config, progress)
        if args.verbose:
            print()
        total += result["headers_added"]
        line = (
            f"  {result['account']:<20} "
            f"{result['headers_added']:>6} new headers, "
            f"{result['folders']} folders"
        )
        if result["unreadable_messages"]:
            line += f", {result['unreadable_messages']} unreadable"
        print(line)
        for err in result["errors"]:
            print(f"      ! {err}", file=sys.stderr)

    print(f"\nIndexed {total} new headers.")
    registry.close_all()
    index.close()
    return 0


def cmd_reindex(args) -> int:
    """Force specific folders (or a whole account) to be read again.

    The incremental sync only asks for UIDs above the last one it saw,
    so messages skipped during an earlier run are never revisited. This
    resets the watermark for the named folders and pulls them fresh.
    """
    from .sync import selectable_folders, sync_account

    config, registry, index = _load(args)
    account = config.account(args.account)
    session = registry.get(account)

    if args.folders:
        folders = args.folders
    else:
        with session.session() as client:
            folders = selectable_folders(client, account)

    for folder in folders:
        index.reset_folder(account.name, folder)
        index.clear_failures(account.name, folder)
        print(f"  reset {account.name}/{folder}")

    print(f"\nRe-reading {len(folders)} folder(s)...")

    def progress(message: str) -> None:
        if args.verbose:
            print(f"\r{message:<70}", end="", flush=True)

    result = sync_account(session, index, account, config, progress)
    if args.verbose:
        print()
    print(
        f"  {result['account']:<20} {result['headers_added']:>6} headers, "
        f"{result['unreadable_messages']} unreadable"
    )
    for err in result["errors"]:
        print(f"      ! {err}", file=sys.stderr)

    registry.close_all()
    index.close()
    return 0


def cmd_status(args) -> int:
    config, registry, index = _load(args)
    stats = index.stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    index.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="imap-mcp",
        description="MCP server for searching several IMAP mailboxes at once.",
    )
    parser.add_argument(
        "-c", "--config", help="Path to config.toml (default: $IMAP_MCP_CONFIG)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="Test login to every account").set_defaults(
        func=cmd_check
    )
    index_parser = sub.add_parser("index", help="Refresh the header index")
    index_parser.add_argument(
        "accounts", nargs="*", default=None, help="Accounts to index (default: all)"
    )
    index_parser.set_defaults(func=cmd_index)

    reindex_parser = sub.add_parser(
        "reindex", help="Re-read folders from scratch (repairs index gaps)"
    )
    reindex_parser.add_argument("account", help="Account name")
    reindex_parser.add_argument(
        "folders", nargs="*", help="Folder names (default: every folder)"
    )
    reindex_parser.set_defaults(func=cmd_reindex)

    sub.add_parser("status", help="Show index freshness").set_defaults(func=cmd_status)

    args = parser.parse_args()
    _setup_logging(args.verbose)

    if not getattr(args, "command", None):
        args.func = cmd_serve
        args.accounts = None

    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
