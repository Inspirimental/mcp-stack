"""RSA signing key handling and the JWKS document.

The key is generated once and kept on disk. Tokens are signed with
RS256, so the resource server only ever needs the public half — it
fetches it from the JWKS endpoint and never sees the private key.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)


def load_or_create_key(path: Path) -> str:
    """Return the PEM private key, generating it on first run."""
    if path.exists():
        return path.read_text()

    logger.info("Generating a new RSA signing key at %s", path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pem)
    # The key must not be readable by other users on the host.
    path.chmod(0o600)
    return pem


def key_id(private_pem: str) -> str:
    """Stable key id derived from the public key."""
    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    public_der = private.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_der).hexdigest()[:16]


def signing_key(private_pem: str):
    """The RSA key object used to sign tokens."""
    from joserfc.jwk import RSAKey

    return RSAKey.import_key(private_pem)


def public_jwks(private_pem: str) -> dict:
    """The JWKS document served at /jwks.json.

    `private=False` is what keeps the private exponent out of the
    published document — the resource server only ever needs the public
    half.
    """
    data = signing_key(private_pem).as_dict(private=False)
    data.update({"use": "sig", "alg": "RS256", "kid": key_id(private_pem)})
    return {"keys": [data]}
