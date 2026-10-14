"""Configuration for dav-mcp.

Same shape as imap-mcp on purpose: passwords come from the environment,
never the config file, and the OAuth block is identical so both services
sit behind the same authorization server.

One thing worth knowing before you fill this in: a provider's mail
password is not necessarily its calendar password. mailbox.org keeps
two separate lists — a Mail-App-Passwort covers IMAP and SMTP only,
while CalDAV and CardDAV need an Applikationspasswort of type
"Kalender- und Adressbuch-Client". iCloud is the friendlier case: one
app-specific password covers all three.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("DAV_MCP_CONFIG", "~/.config/dav-mcp/config.toml")
).expanduser()


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or malformed."""


# Every key the config may contain. Anything else is a mistake worth
# reporting: a misplaced or misspelled key would otherwise be ignored in
# silence, and the server would run with a default nobody chose. That is
# how `timezone` ends up inside an [[account]] block and appointments
# land two hours off.
ROOT_KEYS = {"timezone", "account", "auth"}
ACCOUNT_KEYS = {
    "name", "username", "password_env", "caldav_url", "carddav_url",
    "writable", "timeout", "contacts_cache_seconds",
}
AUTH_KEYS = {
    "enabled", "base_url", "resource_base_url", "authorization_servers",
    "jwks_uri", "public_key", "issuer", "audience", "algorithm",
    "required_scopes", "advertised_scopes", "resource_name",
}


def _reject_unknown(found, allowed: set[str], where: str) -> None:
    unknown = sorted(set(found) - allowed)
    if not unknown:
        return
    hints = []
    for key in unknown:
        if key in ROOT_KEYS and where.startswith("[[account]]"):
            hints.append(
                f"'{key}' belongs at the top of the file, before the first "
                f"[[account]] block — inside one it applies to that account "
                f"and is ignored."
            )
    raise ConfigError(
        f"Unknown key(s) in {where}: {', '.join(unknown)}. "
        + (" ".join(hints) if hints else f"Allowed: {', '.join(sorted(allowed))}.")
    )


def _env_name(account: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in account).upper()
    return f"DAV_MCP_{safe}_PASSWORD"


@dataclass(frozen=True)
class Account:
    """One provider account, with a calendar and/or contacts endpoint."""

    name: str
    username: str
    password: str
    caldav_url: str = ""
    carddav_url: str = ""
    # Opt-in write access, off unless the config says otherwise. The
    # write tools refuse an account that has not enabled it, regardless
    # of what scopes the presented token carries.
    writable: bool = False
    timeout: int = 30
    # Address books are fetched whole and searched locally, so the
    # result is cached for this long rather than refetched per query.
    contacts_cache_seconds: int = 900

    @property
    def has_calendars(self) -> bool:
        return bool(self.caldav_url)

    @property
    def has_contacts(self) -> bool:
        return bool(self.carddav_url)


@dataclass(frozen=True)
class AuthConfig:
    """OAuth resource-server settings — see mcp-auth."""

    enabled: bool = False
    base_url: str = ""
    # Normally left empty: the advertised resource is base_url plus the
    # endpoint's mount path. Only set this when the server sits behind a
    # proxy that rewrites the path, so the two would otherwise disagree.
    resource_base_url: str = ""
    authorization_servers: tuple[str, ...] = ()
    jwks_uri: str = ""
    public_key: str = ""
    issuer: str = ""
    audience: str = ""
    algorithm: str = ""
    # The baseline every token must carry to reach this server at all.
    required_scopes: tuple[str, ...] = ()
    # Everything a client may ask for, advertised in the metadata.
    # Write scopes belong here but not in required_scopes: a read-only
    # token should still be able to connect, it just cannot write.
    # Removing a scope here is how you take a capability away.
    advertised_scopes: tuple[str, ...] = ()
    resource_name: str = "dav-mcp"


@dataclass
class Config:
    accounts: list[Account] = field(default_factory=list)
    auth: AuthConfig = field(default_factory=AuthConfig)
    # How to read a time given without a zone, e.g. "2026-09-01 14:00".
    # This matters most when writing: an appointment created as UTC
    # would land two hours off in a German summer. Reading is unaffected
    # because providers return zoned timestamps.
    timezone: str = "UTC"

    def account(self, name: str | None) -> Account:
        if name is None:
            if len(self.accounts) == 1:
                return self.accounts[0]
            raise ConfigError(
                "Multiple accounts configured; specify one of: "
                + ", ".join(a.name for a in self.accounts)
            )
        for acct in self.accounts:
            if acct.name.lower() == name.lower():
                return acct
        raise ConfigError(
            f"Unknown account {name!r}. Configured: "
            + ", ".join(a.name for a in self.accounts)
        )

    def resolve(self, names: list[str] | None) -> list[Account]:
        if not names:
            return list(self.accounts)
        return [self.account(n) for n in names]


def _load_auth(raw: dict) -> AuthConfig:
    auth_raw = raw.get("auth") or {}
    if not auth_raw.get("enabled"):
        return AuthConfig()
    _reject_unknown(auth_raw, AUTH_KEYS, "[auth]")

    missing = [
        k for k in ("base_url", "authorization_servers", "audience")
        if not auth_raw.get(k)
    ]
    if missing:
        raise ConfigError(
            "[auth] is enabled but missing: " + ", ".join(missing) + ". "
            "'audience' in particular is not optional — without it this "
            "server would accept a token minted for a different service, "
            "such as your mail server."
        )
    if not auth_raw.get("jwks_uri") and not auth_raw.get("public_key"):
        raise ConfigError(
            "[auth] needs either 'jwks_uri' or 'public_key' to verify "
            "token signatures."
        )
    servers = auth_raw["authorization_servers"]
    if isinstance(servers, str):
        servers = [servers]
    return AuthConfig(
        enabled=True,
        base_url=auth_raw["base_url"].rstrip("/"),
        # Left unset by default. FastMCP appends the endpoint's mount
        # path to base_url on its own, so base_url=https://host plus a
        # mount at /dav already yields the resource https://host/dav.
        # Setting it to the full audience here would append the path
        # twice — .../oauth-protected-resource/dav/dav.
        resource_base_url=(auth_raw.get("resource_base_url") or "").rstrip("/"),
        authorization_servers=tuple(servers),
        jwks_uri=auth_raw.get("jwks_uri", ""),
        public_key=auth_raw.get("public_key", ""),
        issuer=auth_raw.get("issuer", ""),
        audience=auth_raw["audience"],
        algorithm=auth_raw.get("algorithm", ""),
        required_scopes=tuple(auth_raw.get("required_scopes", ())),
        advertised_scopes=tuple(
            auth_raw.get("advertised_scopes")
            or auth_raw.get("required_scopes", ())
        ),
        resource_name=auth_raw.get("resource_name", "dav-mcp"),
    )


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"No config at {path}. Copy config.example.toml there, or set "
            "DAV_MCP_CONFIG to point somewhere else."
        )

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    _reject_unknown(raw, ROOT_KEYS, "the top level")
    cfg = Config(auth=_load_auth(raw))

    tz_name = raw.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
    except Exception as exc:
        raise ConfigError(
            f"Unknown timezone {tz_name!r} ({exc}). Use an IANA name such "
            f"as 'Europe/Berlin'."
        ) from exc
    cfg.timezone = tz_name

    entries = raw.get("account")
    if not entries:
        raise ConfigError(f"No [[account]] blocks found in {path}.")

    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name")
        if not name:
            raise ConfigError("Every [[account]] needs a 'name'.")
        if name.lower() in seen:
            raise ConfigError(f"Duplicate account name: {name!r}")
        seen.add(name.lower())
        _reject_unknown(entry, ACCOUNT_KEYS, f"[[account]] {name!r}")

        if not entry.get("username"):
            raise ConfigError(f"Account {name!r} is missing 'username'.")

        caldav_url = (entry.get("caldav_url") or "").rstrip("/")
        carddav_url = (entry.get("carddav_url") or "").rstrip("/")
        if not caldav_url and not carddav_url:
            raise ConfigError(
                f"Account {name!r} has neither 'caldav_url' nor "
                "'carddav_url' — it would do nothing."
            )

        env_var = entry.get("password_env") or _env_name(name)
        password = os.environ.get(env_var)
        if not password:
            raise ConfigError(
                f"Account {name!r}: environment variable {env_var} is unset. "
                "Passwords are read from the environment, never from the "
                "config file."
            )

        cfg.accounts.append(
            Account(
                name=name,
                username=entry["username"],
                password=password,
                caldav_url=caldav_url,
                carddav_url=carddav_url,
                writable=bool(entry.get("writable", False)),
                timeout=int(entry.get("timeout", 30)),
                contacts_cache_seconds=int(
                    entry.get("contacts_cache_seconds", 900)
                ),
            )
        )

    return cfg
