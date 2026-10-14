"""Command line: create the user's credentials, run the server."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import secrets
import sys


def cmd_init_user(args) -> int:
    """Produce the environment lines for the single user account.

    Nothing is written to disk: the values are printed once so you can
    put them in your .env yourself. The password is hashed with Argon2id
    and never stored in the clear.
    """
    import pyotp
    import qrcode

    from .user import hash_password

    username = args.username or input("Benutzername: ").strip()
    if not username:
        print("Benutzername darf nicht leer sein.", file=sys.stderr)
        return 2

    password = getpass.getpass("Passwort: ")
    if len(password) < 12:
        print("Passwort muss mindestens 12 Zeichen haben.", file=sys.stderr)
        return 2
    if password != getpass.getpass("Passwort wiederholen: "):
        print("Passwörter stimmen nicht überein.", file=sys.stderr)
        return 2

    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=args.label
    )

    print("\nQR-Code für die Authenticator-App:\n")
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print(f"\nFalls der QR-Code nicht lesbar ist, Schlüssel manuell eintragen:")
    print(f"  {secret}\n")

    print("Diese Zeilen in die .env übernehmen:\n")
    print(f"MCP_AUTH_USERNAME={username}")
    print(f"MCP_AUTH_PASSWORD_HASH='{hash_password(password)}'")
    print(f"MCP_AUTH_TOTP_SECRET={secret}")
    print(f"MCP_AUTH_SESSION_SECRET={secrets.token_urlsafe(48)}")

    print(
        "\nPrüfe die Authenticator-App, BEVOR du den Server startest — "
        "ohne gültigen TOTP-Code kommst du sonst nicht mehr hinein.\n"
        "Verifizieren mit:  mcp-auth check-totp"
    )
    return 0


def cmd_check_totp(args) -> int:
    """Confirm the authenticator app matches the configured secret."""
    import pyotp

    secret = os.environ.get("MCP_AUTH_TOTP_SECRET", "").strip()
    if not secret:
        print("MCP_AUTH_TOTP_SECRET ist nicht gesetzt.", file=sys.stderr)
        return 2
    code = input("Aktueller Code aus der App: ").strip().replace(" ", "")
    if pyotp.TOTP(secret).verify(code, valid_window=1):
        print("Stimmt überein.")
        return 0
    print("Passt nicht. Uhrzeit des Geräts prüfen.", file=sys.stderr)
    return 1


def _print_client(c, datetime) -> None:
    when = datetime.datetime.fromtimestamp(c["created_at"]).isoformat(" ", "seconds")
    name = c["metadata"].get("client_name") or "(ohne Namen)"
    mark = "aktiv " if c["active_tokens"] else "unbenutzt"
    print(
        f"  [{mark}] {c['client_id']}  {when}  {name}\n"
        f"      Tokens: {c['tokens']} gesamt, {c['active_tokens']} gültig"
    )
    for uri in c["metadata"].get("redirect_uris", []):
        print(f"      -> {uri}")


def cmd_clients(args) -> int:
    """List, delete or prune clients that registered themselves."""
    import datetime

    from .config import load_config
    from .store import Store

    cfg = load_config()
    store = Store(cfg.db_path)

    if args.delete:
        target = next(
            (c for c in store.list_clients() if c["client_id"] == args.delete), None
        )
        if target is None:
            print(f"Nicht gefunden: {args.delete}", file=sys.stderr)
            return 1
        if target["active_tokens"] and not args.force:
            print(
                f"{args.delete} hat {target['active_tokens']} gültige(n) Token — "
                f"das ist sehr wahrscheinlich deine laufende Verbindung.\n"
                f"Löschen würde sie sofort trennen. Mit --force trotzdem.",
                file=sys.stderr,
            )
            return 2
        store.delete_client(args.delete)
        print(f"Gelöscht: {args.delete}")
        return 0

    if args.prune:
        days = args.older_than
        stale = store.prunable_clients(min_age_seconds=days * 86400)
        if not stale:
            print(
                f"Nichts zu entfernen — keine Registrierung ohne gültige Tokens, "
                f"die älter als {days} Tag(e) ist."
            )
            return 0
        print(f"{len(stale)} Registrierung(en) ohne gültige Tokens:\n")
        for c in stale:
            _print_client(c, datetime)
        if not args.yes:
            print(
                "\nNichts gelöscht. Zum Ausführen dasselbe Kommando mit --yes.\n"
                "Registrierungen mit gültigen Tokens sind hier nie dabei."
            )
            return 0
        for c in stale:
            store.delete_client(c["client_id"])
        print(f"\n{len(stale)} Registrierung(en) gelöscht.")
        return 0

    clients = store.list_clients()
    if not clients:
        print("Noch kein Client registriert.")
        return 0
    for c in clients:
        _print_client(c, datetime)
    print(
        "\nMehrere Einträge sind normal: Dynamic Client Registration kennt "
        "keine Idempotenz, jede Neuverbindung legt eine neue an.\n"
        "Aufräumen mit:  mcp-auth clients --prune --older-than 7"
    )
    return 0


def cmd_revoke_all(args) -> int:
    from .config import load_config
    from .store import Store

    cfg = load_config()
    n = Store(cfg.db_path).revoke_all()
    print(f"{n} Refresh-Token widerrufen. Bestehende Access-Tokens laufen "
          f"innerhalb von {cfg.access_token_ttl // 60} Minuten ab.")
    return 0


def cmd_serve(args) -> int:
    from .app import create_app

    app = create_app()
    app.run(host=args.host, port=args.port)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-auth",
        description="OAuth 2.1 authorization server for a single user.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-user", help="Create password hash and TOTP secret")
    p.add_argument("--username")
    p.add_argument("--label", default="mcp.timbeck.de",
                   help="Name shown in the authenticator app")
    p.set_defaults(func=cmd_init_user)

    sub.add_parser("check-totp", help="Verify the authenticator app").set_defaults(
        func=cmd_check_totp)

    p = sub.add_parser("clients", help="List, delete or prune registered clients")
    p.add_argument("--delete", metavar="CLIENT_ID")
    p.add_argument("--force", action="store_true",
                   help="Delete even a client that holds valid tokens")
    p.add_argument("--prune", action="store_true",
                   help="Remove registrations that hold no valid token")
    p.add_argument("--older-than", type=int, default=7, metavar="DAYS",
                   help="With --prune: only registrations at least this old "
                        "(default 7)")
    p.add_argument("--yes", action="store_true",
                   help="With --prune: actually delete instead of listing")
    p.set_defaults(func=cmd_clients)

    sub.add_parser("revoke-all", help="Revoke every refresh token").set_defaults(
        func=cmd_revoke_all)

    p = sub.add_parser("serve", help="Run the server (development only)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
