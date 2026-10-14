"""The single user: password verification, TOTP, and lockout.

There is one account and no registration. Credentials live in the
environment as an Argon2id hash and a TOTP secret, both produced by
`mcp-auth init-user`. The plaintext password is never stored anywhere.
"""

from __future__ import annotations

import hmac
import logging

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from .config import Config
from .store import Store

logger = logging.getLogger(__name__)

hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return hasher.hash(password)


class LockedOut(RuntimeError):
    """Too many failed attempts."""


def verify_login(
    username: str, password: str, totp_code: str, cfg: Config, store: Store
) -> bool:
    """Check username, password and TOTP code together.

    Failures are counted and the account locks for a while once the
    threshold is hit. Every failure path costs the same Argon2 work, so
    a wrong username cannot be distinguished from a wrong password by
    timing.
    """
    if store.recent_failures(cfg.lockout_seconds) >= cfg.max_login_attempts:
        raise LockedOut(
            f"Too many failed attempts. Locked for "
            f"{cfg.lockout_seconds // 60} minutes."
        )

    ok = True

    # compare_digest keeps the username check constant-time.
    if not hmac.compare_digest(username.strip(), cfg.username):
        ok = False

    try:
        hasher.verify(cfg.password_hash, password)
    except (VerifyMismatchError, VerificationError):
        ok = False
    except Exception as exc:  # malformed hash in the environment
        logger.error("Password hash is unusable: %s", exc)
        ok = False

    # Always evaluate TOTP so the response time does not reveal which
    # factor failed. valid_window=1 tolerates one 30s step of clock drift.
    totp = pyotp.TOTP(cfg.totp_secret)
    if not totp.verify(totp_code.strip().replace(" ", ""), valid_window=1):
        ok = False

    store.record_login(ok)
    if ok:
        store.clear_failures()
    else:
        logger.warning("Failed login attempt for %r", username[:40])
    return ok


def provisioning_uri(secret: str, username: str, issuer_label: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=issuer_label
    )


def new_totp_secret() -> str:
    return pyotp.random_base32()
