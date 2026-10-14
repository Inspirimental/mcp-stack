"""Configuration loading for imap-mcp.

Design rules:

* Passwords NEVER live in the config file. Each account names an
  environment variable that holds its app password. This keeps the
  TOML checked-in-able and lets Docker inject secrets.
* Write access is opt-in per account. ``writable = false`` is the
  default, and the two state-changing tools refuse to run without it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("IMAP_MCP_CONFIG", "~/.config/imap-mcp/config.toml")
).expanduser()

# Folders that are noise for search by default.
DEFAULT_EXCLUDED = ["Trash", "Junk", "Spam", "Drafts", "Papierkorb", "Entwürfe"]


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or malformed."""


def _env_name(account: str) -> str:
    """Default env var for an account's password: IMAP_MCP_<NAME>_PASSWORD."""
    safe = "".join(c if c.isalnum() else "_" for c in account).upper()
    return f"IMAP_MCP_{safe}_PASSWORD"


@dataclass(frozen=True)
class Account:
    """One IMAP mailbox."""

    name: str
    host: str
    username: str
    password: str
    port: int = 993
    ssl: bool = True
    # Opt-in write access. Guards mark() and move().
    writable: bool = False
    default_folder: str = "INBOX"
    exclude_folders: tuple[str, ...] = ()
    # Some servers (notably iCloud) are slow; allow a per-account override.
    timeout: int = 30
    # 0 means "use the global fetch_batch_size". Servers that return
    # malformed FETCH responses do better with smaller batches.
    fetch_batch_size: int = 0

    def excluded(self, folder: str) -> bool:
        """True if this folder should be skipped by search and indexing."""
        leaf = folder.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        lowered = {f.lower() for f in self.exclude_folders}
        return folder.lower() in lowered or leaf.lower() in lowered


@dataclass(frozen=True)
class AuthConfig:
    """OAuth resource-server settings.

    imap-mcp does not issue tokens. It verifies tokens signed by an
    authorization server you already run, and advertises that server via
    RFC 9728 so MCP clients can find it. Claude requires the
    authorization server to support Dynamic Client Registration — that
    requirement sits with the authorization server, not here.
    """

    enabled: bool = False
    # Public URL of this server, e.g. https://mcp.timbeck.de
    base_url: str = ""
    # Authorization server(s) allowed to issue tokens for this resource.
    authorization_servers: tuple[str, ...] = ()
    # Where to fetch the signing keys. Alternative: a PEM public key.
    jwks_uri: str = ""
    public_key: str = ""
    # Expected 'iss' claim.
    issuer: str = ""
    # Expected 'aud' claim — must identify THIS server, not the client.
    # Without it a token minted for another service would be accepted here.
    audience: str = ""
    algorithm: str = ""
    required_scopes: tuple[str, ...] = ()
    resource_name: str = "imap-mcp"


@dataclass
class Config:
    accounts: list[Account] = field(default_factory=list)
    auth: AuthConfig = field(default_factory=AuthConfig)
    index_path: Path = Path("~/.local/share/imap-mcp/headers.db").expanduser()
    # Header index depth per folder. Headers are small; 20k is a few MB.
    index_max_per_folder: int = 20000
    fetch_batch_size: int = 500

    def account(self, name: str | None) -> Account:
        """Look up an account by name, case-insensitively.

        With no name and exactly one account configured, that one is
        returned. With several accounts, a name is required — guessing
        would silently search the wrong mailbox.
        """
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
        """Resolve a list of account names; None means all accounts."""
        if not names:
            return list(self.accounts)
        return [self.account(n) for n in names]


def load_config(path: Path | None = None) -> Config:
    """Read and validate the TOML config."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"No config at {path}. Copy config.example.toml there, or set "
            "IMAP_MCP_CONFIG to point somewhere else."
        )

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg = Config()

    if index_path := raw.get("index_path"):
        cfg.index_path = Path(index_path).expanduser()
    cfg.index_max_per_folder = int(
        raw.get("index_max_per_folder", cfg.index_max_per_folder)
    )
    cfg.fetch_batch_size = int(raw.get("fetch_batch_size", cfg.fetch_batch_size))

    auth_raw = raw.get("auth") or {}
    if auth_raw.get("enabled"):
        missing = [
            k for k in ("base_url", "authorization_servers", "audience")
            if not auth_raw.get(k)
        ]
        if missing:
            raise ConfigError(
                "[auth] is enabled but missing: " + ", ".join(missing) + ". "
                "'audience' in particular is not optional — without it this "
                "server would accept tokens minted for a different service."
            )
        if not auth_raw.get("jwks_uri") and not auth_raw.get("public_key"):
            raise ConfigError(
                "[auth] needs either 'jwks_uri' or 'public_key' to verify "
                "token signatures."
            )
        servers = auth_raw["authorization_servers"]
        if isinstance(servers, str):
            servers = [servers]
        cfg.auth = AuthConfig(
            enabled=True,
            base_url=auth_raw["base_url"].rstrip("/"),
            authorization_servers=tuple(servers),
            jwks_uri=auth_raw.get("jwks_uri", ""),
            public_key=auth_raw.get("public_key", ""),
            issuer=auth_raw.get("issuer", ""),
            audience=auth_raw["audience"],
            algorithm=auth_raw.get("algorithm", ""),
            required_scopes=tuple(auth_raw.get("required_scopes", ())),
            resource_name=auth_raw.get("resource_name", "imap-mcp"),
        )

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

        for required in ("host", "username"):
            if not entry.get(required):
                raise ConfigError(f"Account {name!r} is missing '{required}'.")

        env_var = entry.get("password_env") or _env_name(name)
        password = os.environ.get(env_var)
        if not password:
            raise ConfigError(
                f"Account {name!r}: environment variable {env_var} is unset. "
                "Passwords are read from the environment, never from the "
                "config file."
            )

        excluded = entry.get("exclude_folders")
        if excluded is None:
            excluded = DEFAULT_EXCLUDED

        cfg.accounts.append(
            Account(
                name=name,
                host=entry["host"],
                port=int(entry.get("port", 993)),
                username=entry["username"],
                password=password,
                ssl=bool(entry.get("ssl", True)),
                writable=bool(entry.get("writable", False)),
                default_folder=entry.get("default_folder", "INBOX"),
                exclude_folders=tuple(excluded),
                timeout=int(entry.get("timeout", 30)),
                fetch_batch_size=int(entry.get("fetch_batch_size", 0)),
            )
        )

    cfg.index_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg
