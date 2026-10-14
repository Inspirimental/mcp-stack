"""Configuration for the authorization server.

Everything sensitive comes from the environment. The single user's
password hash and TOTP secret are produced by `mcp-auth init-user` and
pasted into the environment — this server has no user database and no
registration flow, by design.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Environment variable {name} is required.")
    return value


@dataclass(frozen=True)
class Config:
    # Public issuer URL, including the path prefix, e.g.
    # https://mcp.timbeck.de/auth
    issuer: str
    # The resources this server mints tokens for (RFC 8707). A token
    # request naming anything outside this list is refused, which is
    # what keeps a calendar token from opening the mailboxes.
    resources: tuple[str, ...]
    scopes: tuple[str, ...]

    username: str
    password_hash: str
    totp_secret: str

    session_secret: str
    private_key_path: Path
    db_path: Path

    # Only these redirect URI prefixes may be registered. Dynamic Client
    # Registration is open by protocol necessity — Claude must register
    # itself — so this is the control that stops a stranger's client
    # from pointing the flow at their own callback.
    allowed_redirect_prefixes: tuple[str, ...]

    access_token_ttl: int = 3600
    refresh_token_ttl: int = 60 * 60 * 24 * 14
    auth_code_ttl: int = 120
    session_ttl: int = 60 * 60 * 12

    # Failed logins allowed per window before the account is locked out.
    max_login_attempts: int = 5
    lockout_seconds: int = 900

    @property
    def prefix(self) -> str:
        """Path prefix of the issuer, e.g. '/auth' (empty for root)."""
        from urllib.parse import urlparse

        return urlparse(self.issuer).path.rstrip("/")

    def url(self, path: str) -> str:
        return f"{self.issuer.rstrip('/')}/{path.lstrip('/')}"


def load_config() -> Config:
    issuer = _require("MCP_AUTH_ISSUER").rstrip("/")
    if not issuer.startswith("https://") and "localhost" not in issuer:
        raise ConfigError(
            f"MCP_AUTH_ISSUER must be https (got {issuer!r}). OAuth "
            "credentials over plain HTTP are not protected in transit."
        )

    # One value or a comma-separated list. A single value keeps working
    # exactly as before.
    resources = tuple(
        r.strip().rstrip("/")
        for r in _require("MCP_AUTH_RESOURCE").split(",")
        if r.strip()
    )
    if len(set(resources)) != len(resources):
        raise ConfigError("MCP_AUTH_RESOURCE contains duplicates.")

    raw_prefixes = os.environ.get("MCP_AUTH_ALLOWED_REDIRECT_PREFIXES", "")
    prefixes = tuple(p.strip() for p in raw_prefixes.split(",") if p.strip())
    if not prefixes:
        raise ConfigError(
            "MCP_AUTH_ALLOWED_REDIRECT_PREFIXES is required. Dynamic Client "
            "Registration is open by necessity; without this allowlist any "
            "party could register a client that redirects your "
            "authorization codes to their own server. For Claude, set it "
            "to https://claude.ai/api/mcp/auth_callback"
        )

    scopes = tuple(
        s.strip()
        for s in os.environ.get("MCP_AUTH_SCOPES", "mail:read").split(",")
        if s.strip()
    )

    return Config(
        issuer=issuer,
        resources=resources,
        scopes=scopes,
        username=_require("MCP_AUTH_USERNAME"),
        password_hash=_require("MCP_AUTH_PASSWORD_HASH"),
        totp_secret=_require("MCP_AUTH_TOTP_SECRET"),
        session_secret=_require("MCP_AUTH_SESSION_SECRET"),
        private_key_path=Path(
            os.environ.get("MCP_AUTH_PRIVATE_KEY", "/data/signing-key.pem")
        ),
        db_path=Path(os.environ.get("MCP_AUTH_DB", "/data/auth.db")),
        allowed_redirect_prefixes=prefixes,
        access_token_ttl=int(os.environ.get("MCP_AUTH_ACCESS_TTL", 3600)),
    )


def redirect_uri_allowed(uri: str, cfg: Config) -> bool:
    """Strict prefix match against the allowlist, https only."""
    if not uri.startswith("https://"):
        # Loopback is permitted for local MCP clients per OAuth 2.1.
        if not (
            uri.startswith("http://127.0.0.1")
            or uri.startswith("http://localhost")
        ):
            return False
    return any(uri.startswith(p) for p in cfg.allowed_redirect_prefixes)
