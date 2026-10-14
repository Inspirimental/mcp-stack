"""OAuth 2.1 primitives: PKCE, client registration, token minting.

The endpoint logic lives in app.py; this module holds the pieces that
are worth reading on their own when someone asks "how does this
actually verify anything".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time

from joserfc import jwt

from .config import Config, redirect_uri_allowed
from .keys import key_id, signing_key
from .store import Store, new_token

logger = logging.getLogger(__name__)

# OAuth 2.1 drops "plain": a downgrade to it would defeat PKCE entirely.
REQUIRED_CODE_CHALLENGE_METHOD = "S256"


class OAuthError(Exception):
    """An error to be rendered as an OAuth error response."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status


# --------------------------------------------------------------- PKCE


def verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    """Check a PKCE code_verifier against the stored challenge.

    S256 only. The comparison is constant-time so a wrong verifier
    cannot be narrowed down by timing.
    """
    if method != REQUIRED_CODE_CHALLENGE_METHOD:
        return False
    if not verifier or not (43 <= len(verifier) <= 128):
        return False
    if not re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier):
        return False
    computed = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return hmac.compare_digest(computed, challenge)


# ------------------------------------------- dynamic client registration


def register_client(metadata: dict, cfg: Config, store: Store) -> dict:
    """Handle an RFC 7591 registration request.

    Registration is open — Claude cannot be handed a client_id, so it
    must be able to create one. What keeps that safe is the redirect URI
    allowlist: a client may only be registered if every redirect URI it
    asks for is one we already trust. Without that check, open
    registration would let anyone aim your authorization codes at their
    own callback.
    """
    redirect_uris = metadata.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthError(
            "invalid_redirect_uri", "At least one redirect_uri is required."
        )

    for uri in redirect_uris:
        if not isinstance(uri, str) or not redirect_uri_allowed(uri, cfg):
            raise OAuthError(
                "invalid_redirect_uri",
                f"redirect_uri {uri!r} is not on this server's allowlist.",
            )

    grant_types = metadata.get("grant_types") or ["authorization_code"]
    for gt in grant_types:
        if gt not in ("authorization_code", "refresh_token"):
            raise OAuthError(
                "invalid_client_metadata", f"Unsupported grant_type {gt!r}."
            )

    # Public client (PKCE, no secret) unless it asks for secret-based auth.
    auth_method = metadata.get("token_endpoint_auth_method", "none")
    if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
        raise OAuthError(
            "invalid_client_metadata",
            f"Unsupported token_endpoint_auth_method {auth_method!r}.",
        )

    client_id = f"c_{secrets.token_urlsafe(16)}"
    client_secret = None if auth_method == "none" else new_token(32)

    stored = {
        "client_name": str(metadata.get("client_name", ""))[:120],
        "redirect_uris": redirect_uris,
        "grant_types": grant_types,
        "response_types": metadata.get("response_types") or ["code"],
        "token_endpoint_auth_method": auth_method,
        "scope": metadata.get("scope") or " ".join(cfg.scopes),
    }
    store.add_client(client_id, client_secret, stored)
    logger.info(
        "Registered client %s (%s)", client_id, stored["client_name"] or "unnamed"
    )

    response = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        **stored,
    }
    if client_secret:
        response["client_secret"] = client_secret
        response["client_secret_expires_at"] = 0
    return response


def authenticate_client(
    client_id: str, client_secret: str | None, store: Store
) -> dict:
    """Look up a client and check its secret if it has one."""
    client = store.get_client(client_id)
    if client is None:
        raise OAuthError("invalid_client", "Unknown client_id.", status=401)

    expected = client["client_secret"]
    if expected:
        if not client_secret or not hmac.compare_digest(client_secret, expected):
            raise OAuthError("invalid_client", "Bad client secret.", status=401)
    return client


# ---------------------------------------------------------- token minting


def mint_access_token(
    subject: str, client_id: str, scope: str, resource: str,
    cfg: Config, private_pem: str,
) -> str:
    """Sign a JWT access token for exactly one resource.

    'aud' is the resource the client asked for (RFC 8707). The resource
    server checks it, which is what stops a token minted for one service
    from being replayed against another.
    """
    now = int(time.time())
    header = {"alg": "RS256", "typ": "at+jwt", "kid": key_id(private_pem)}
    payload = {
        "iss": cfg.issuer,
        "sub": subject,
        "aud": resource,
        "client_id": client_id,
        "scope": scope,
        "iat": now,
        "nbf": now,
        "exp": now + cfg.access_token_ttl,
        "jti": secrets.token_urlsafe(16),
    }
    # The algorithm is pinned explicitly. Leaving it to the library's
    # default registry is how "alg" confusion attacks get in.
    return jwt.encode(header, payload, signing_key(private_pem), algorithms=["RS256"])


def validate_scope(requested: str, cfg: Config) -> str:
    """Reduce a requested scope string to what this server grants."""
    if not requested:
        return " ".join(cfg.scopes)
    asked = [s for s in requested.split() if s]
    unknown = [s for s in asked if s not in cfg.scopes]
    if unknown:
        raise OAuthError(
            "invalid_scope", f"Unknown scope(s): {', '.join(unknown)}"
        )
    return " ".join(asked)


def validate_resource(requested: str | None, cfg: Config) -> str:
    """Check the RFC 8707 resource parameter against what we serve.

    A token is minted for exactly one resource. With several configured
    and none requested, we refuse rather than pick: guessing would hand
    out a mail token to something that asked for a calendar, and the
    resource server would rightly reject it — after a confusing detour.
    """
    if not requested:
        if len(cfg.resources) == 1:
            # Defaulting is safer than minting an audience-less token,
            # which any resource server would be free to accept.
            return cfg.resources[0]
        raise OAuthError(
            "invalid_target",
            "This server issues tokens for several resources, so the "
            "request must name one via the 'resource' parameter: "
            + ", ".join(cfg.resources),
        )

    wanted = requested.rstrip("/")
    for known in cfg.resources:
        if wanted == known.rstrip("/"):
            return known
    raise OAuthError(
        "invalid_target",
        f"Unknown resource {requested!r}. This server issues tokens for: "
        + ", ".join(cfg.resources),
    )
