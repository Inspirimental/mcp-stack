"""Flask application: discovery, registration, authorize, token.

Route layout (with issuer https://host/auth):

    /.well-known/oauth-authorization-server/auth   RFC 8414 discovery
    /auth/.well-known/openid-configuration         OIDC-style discovery
    /auth/jwks.json                                public signing keys
    /auth/register                                 RFC 7591 registration
    /auth/authorize                                start of the user flow
    /auth/login                                    password + TOTP
    /auth/consent                                  explicit approval
    /auth/token                                    code and refresh exchange
    /auth/revoke                                   token revocation
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, render_template_string, request, session

from .config import Config, load_config
from .keys import load_or_create_key, public_jwks
from .oauth import (
    OAuthError,
    authenticate_client,
    mint_access_token,
    register_client,
    validate_resource,
    validate_scope,
    verify_pkce,
)
from .store import Store, new_token
from .user import LockedOut, verify_login

logger = logging.getLogger(__name__)

LOGIN_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anmeldung</title>
<style>
 :root{color-scheme:light dark}
 body{font-family:system-ui,sans-serif;max-width:22rem;margin:12vh auto;padding:0 1rem}
 h1{font-size:1.25rem;margin-bottom:.25rem}
 p.sub{color:#666;margin-top:0;font-size:.9rem}
 label{display:block;margin:.9rem 0 .2rem;font-size:.85rem;font-weight:600}
 input{width:100%;padding:.55rem;border:1px solid #999;border-radius:.35rem;
       font-size:1rem;background:transparent;color:inherit}
 button{margin-top:1.2rem;width:100%;padding:.6rem;border:0;border-radius:.35rem;
        background:#2563eb;color:#fff;font-size:1rem;cursor:pointer}
 .err{background:#fee;border:1px solid #f99;color:#900;padding:.6rem;
      border-radius:.35rem;margin-bottom:1rem;font-size:.9rem}
 @media(prefers-color-scheme:dark){.err{background:#3b1111;border-color:#a33;color:#fbb}}
</style>
<h1>Anmeldung</h1>
<p class="sub">{{ resource }}</p>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post" action="{{ prefix }}/login">
 <input type="hidden" name="next" value="{{ next_url }}">
 <label for="u">Benutzername</label>
 <input id="u" name="username" autocomplete="username" autofocus required>
 <label for="p">Passwort</label>
 <input id="p" name="password" type="password" autocomplete="current-password" required>
 <label for="t">Code aus der Authenticator-App</label>
 <input id="t" name="totp" inputmode="numeric" pattern="[0-9 ]*"
        autocomplete="one-time-code" required>
 <button type="submit">Anmelden</button>
</form>
"""

CONSENT_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zugriff bestätigen</title>
<style>
 :root{color-scheme:light dark}
 body{font-family:system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem}
 h1{font-size:1.25rem}
 dl{background:rgba(127,127,127,.12);padding:1rem;border-radius:.5rem;font-size:.9rem}
 dt{font-weight:600;font-size:.75rem;text-transform:uppercase;color:#777}
 dd{margin:.15rem 0 .8rem;word-break:break-all}
 dd:last-child{margin-bottom:0}
 .row{display:flex;gap:.6rem;margin-top:1.2rem}
 button{flex:1;padding:.6rem;border:0;border-radius:.35rem;font-size:1rem;cursor:pointer}
 .ok{background:#2563eb;color:#fff}
 .no{background:transparent;border:1px solid #999;color:inherit}
</style>
<h1>Zugriff bestätigen</h1>
<p>Eine Anwendung möchte in deinem Namen auf diesen Dienst zugreifen.</p>
<dl>
 <dt>Anwendung</dt><dd>{{ client_name }}</dd>
 <dt>Rückleitung an</dt><dd>{{ redirect_uri }}</dd>
 <dt>Dienst</dt><dd>{{ resource }}</dd>
 <dt>Berechtigungen</dt><dd>{{ scope }}</dd>
</dl>
<form method="post" action="{{ prefix }}/consent">
 <input type="hidden" name="rid" value="{{ rid }}">
 <div class="row">
  <button class="no" name="approve" value="no" type="submit">Ablehnen</button>
  <button class="ok" name="approve" value="yes" type="submit">Erlauben</button>
 </div>
</form>
"""


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    store = Store(cfg.db_path)
    private_pem = load_or_create_key(cfg.private_key_path)
    prefix = cfg.prefix

    app = Flask(__name__)
    app.secret_key = cfg.session_secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.issuer.startswith("https://"),
        SESSION_COOKIE_NAME="mcp_auth_session",
    )
    app.extensions["mcp_auth"] = {"cfg": cfg, "store": store, "key": private_pem}

    # Pending authorization requests, keyed by a random id. Held in
    # memory on purpose: they live for one browser round-trip.
    pending: dict[str, dict] = {}

    def metadata() -> dict:
        return {
            "issuer": cfg.issuer,
            "authorization_endpoint": cfg.url("authorize"),
            "token_endpoint": cfg.url("token"),
            "registration_endpoint": cfg.url("register"),
            "revocation_endpoint": cfg.url("revoke"),
            "jwks_uri": cfg.url("jwks.json"),
            "scopes_supported": list(cfg.scopes),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "none", "client_secret_post", "client_secret_basic",
            ],
            "authorization_response_iss_parameter_supported": True,
            "resource_indicators_supported": True,
        }

    # ---------------------------------------------------------- discovery
    # RFC 8414 puts the path component of the issuer *after* the
    # well-known segment. Clients also try the OIDC-style URL, so both
    # are served.
    @app.get("/.well-known/oauth-authorization-server" + (prefix or ""))
    @app.get(prefix + "/.well-known/oauth-authorization-server")
    @app.get(prefix + "/.well-known/openid-configuration")
    def discovery():
        return jsonify(metadata())

    @app.get(prefix + "/jwks.json")
    def jwks():
        return jsonify(public_jwks(private_pem))

    # ------------------------------------------------------- registration
    @app.post(prefix + "/register")
    def register():
        try:
            body = request.get_json(force=True, silent=True) or {}
            return jsonify(register_client(body, cfg, store)), 201
        except OAuthError as exc:
            return (
                jsonify({"error": exc.error, "error_description": exc.description}),
                exc.status,
            )

    # ---------------------------------------------------------- authorize
    def _error_redirect(uri: str, error: str, desc: str, state: str | None):
        params = {"error": error, "error_description": desc, "iss": cfg.issuer}
        if state:
            params["state"] = state
        return redirect(f"{uri}{'&' if '?' in uri else '?'}{urlencode(params)}")

    @app.get(prefix + "/authorize")
    def authorize():
        args = request.args
        client_id = args.get("client_id", "")
        redirect_uri = args.get("redirect_uri", "")
        state = args.get("state")

        client = store.get_client(client_id)
        if client is None:
            return jsonify({"error": "invalid_client"}), 400
        # Never redirect to an unregistered URI — that check has to happen
        # before any error can be sent onward.
        if redirect_uri not in client["metadata"]["redirect_uris"]:
            return jsonify({"error": "invalid_request",
                            "error_description": "Unregistered redirect_uri."}), 400

        if args.get("response_type") != "code":
            return _error_redirect(
                redirect_uri, "unsupported_response_type",
                "Only the authorization code flow is supported.", state)

        challenge = args.get("code_challenge", "")
        method = args.get("code_challenge_method", "")
        if not challenge or method != "S256":
            return _error_redirect(
                redirect_uri, "invalid_request",
                "PKCE with code_challenge_method=S256 is required.", state)

        try:
            scope = validate_scope(args.get("scope", ""), cfg)
            resource = validate_resource(args.get("resource"), cfg)
        except OAuthError as exc:
            return _error_redirect(redirect_uri, exc.error, exc.description, state)

        rid = new_token(16)
        pending[rid] = {
            "client_id": client_id,
            "client_name": client["metadata"].get("client_name") or client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
            "resource": resource,
            "code_challenge": challenge,
            "code_challenge_method": method,
            "created": time.time(),
        }
        # Drop stale entries so this dict cannot grow without bound.
        for key, value in list(pending.items()):
            if time.time() - value["created"] > 900:
                pending.pop(key, None)

        if not _session_valid():
            return render_template_string(
                LOGIN_PAGE, prefix=prefix, error=None,
                next_url=f"{prefix}/consent?rid={rid}", resource=", ".join(cfg.resources))
        return _consent_page(rid)

    def _session_valid() -> bool:
        started = session.get("at")
        return bool(
            session.get("sub") == cfg.username
            and started
            and time.time() - started < cfg.session_ttl
        )

    def _consent_page(rid: str):
        req = pending.get(rid)
        if req is None:
            return jsonify({"error": "invalid_request",
                            "error_description": "This request expired."}), 400
        return render_template_string(
            CONSENT_PAGE, prefix=prefix, rid=rid,
            client_name=req["client_name"], redirect_uri=req["redirect_uri"],
            resource=req["resource"], scope=req["scope"])

    @app.post(prefix + "/login")
    def login():
        next_url = request.form.get("next", "")
        try:
            ok = verify_login(
                request.form.get("username", ""),
                request.form.get("password", ""),
                request.form.get("totp", ""),
                cfg, store)
        except LockedOut as exc:
            return render_template_string(
                LOGIN_PAGE, prefix=prefix, error=str(exc),
                next_url=next_url, resource=", ".join(cfg.resources)), 429

        if not ok:
            return render_template_string(
                LOGIN_PAGE, prefix=prefix,
                error="Anmeldung fehlgeschlagen.",
                next_url=next_url, resource=", ".join(cfg.resources)), 401

        session.clear()
        session["sub"] = cfg.username
        session["at"] = time.time()
        # Only ever redirect within this app.
        if not next_url.startswith(prefix + "/"):
            next_url = prefix + "/authorize"
        return redirect(next_url)

    @app.get(prefix + "/consent")
    def consent_get():
        if not _session_valid():
            rid = request.args.get("rid", "")
            return render_template_string(
                LOGIN_PAGE, prefix=prefix, error=None,
                next_url=f"{prefix}/consent?rid={rid}", resource=", ".join(cfg.resources))
        return _consent_page(request.args.get("rid", ""))

    @app.post(prefix + "/consent")
    def consent_post():
        if not _session_valid():
            return jsonify({"error": "access_denied"}), 401
        rid = request.form.get("rid", "")
        req = pending.pop(rid, None)
        if req is None:
            return jsonify({"error": "invalid_request",
                            "error_description": "This request expired."}), 400

        if request.form.get("approve") != "yes":
            return _error_redirect(
                req["redirect_uri"], "access_denied",
                "The user declined.", req["state"])

        code = new_token(32)
        store.add_code(
            code=code, client_id=req["client_id"],
            redirect_uri=req["redirect_uri"], scope=req["scope"],
            resource=req["resource"], code_challenge=req["code_challenge"],
            code_challenge_method=req["code_challenge_method"],
            subject=cfg.username, nonce=None,
            expires_at=int(time.time()) + cfg.auth_code_ttl)

        params = {"code": code, "iss": cfg.issuer}
        if req["state"]:
            params["state"] = req["state"]
        sep = "&" if "?" in req["redirect_uri"] else "?"
        return redirect(f"{req['redirect_uri']}{sep}{urlencode(params)}")

    # -------------------------------------------------------------- token
    @app.post(prefix + "/token")
    def token():
        try:
            return jsonify(_issue_token())
        except OAuthError as exc:
            return (
                jsonify({"error": exc.error, "error_description": exc.description}),
                exc.status,
            )

    def _client_credentials() -> tuple[str, str | None]:
        auth = request.authorization
        if auth and auth.username:
            return auth.username, auth.password
        return request.form.get("client_id", ""), request.form.get("client_secret")

    def _issue_token() -> dict:
        grant_type = request.form.get("grant_type", "")
        client_id, client_secret = _client_credentials()
        client = authenticate_client(client_id, client_secret, store)

        if grant_type == "authorization_code":
            record = store.take_code(request.form.get("code", ""))
            if record is None:
                raise OAuthError(
                    "invalid_grant", "Authorization code is unknown, expired "
                                     "or already used.")
            if record["client_id"] != client_id:
                raise OAuthError("invalid_grant", "Code was issued to another client.")
            if record["redirect_uri"] != request.form.get("redirect_uri", ""):
                raise OAuthError("invalid_grant", "redirect_uri does not match.")
            if not verify_pkce(
                request.form.get("code_verifier", ""),
                record["code_challenge"], record["code_challenge_method"]
            ):
                raise OAuthError("invalid_grant", "PKCE verification failed.")

            resource = validate_resource(
                request.form.get("resource") or record["resource"], cfg)
            scope, subject = record["scope"], record["subject"]

        elif grant_type == "refresh_token":
            presented = request.form.get("refresh_token", "")
            record = store.get_refresh_token(presented)
            if record is None:
                raise OAuthError(
                    "invalid_grant", "Refresh token is unknown, expired or revoked.")
            if record["client_id"] != client_id:
                raise OAuthError("invalid_grant", "Token belongs to another client.")
            # Rotation: the presented token dies here whether or not the
            # rest succeeds, so a stolen copy is good at most once.
            store.revoke_refresh_token(presented)
            resource = validate_resource(
                request.form.get("resource") or record["resource"], cfg)
            requested = request.form.get("scope", "")
            scope = validate_scope(requested, cfg) if requested else record["scope"]
            subject = record["subject"]
        else:
            raise OAuthError("unsupported_grant_type", f"{grant_type!r} is not supported.")

        access_token = mint_access_token(
            subject, client_id, scope, resource, cfg, private_pem)
        refresh_token = new_token(32)
        store.add_refresh_token(
            refresh_token, client_id, scope, resource, subject, cfg.refresh_token_ttl)
        store.purge_expired()

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": cfg.access_token_ttl,
            "refresh_token": refresh_token,
            "scope": scope,
        }

    @app.post(prefix + "/revoke")
    def revoke():
        store.revoke_refresh_token(request.form.get("token", ""))
        return "", 200  # RFC 7009: always 200, never confirm existence

    @app.get(prefix + "/health")
    def health():
        return jsonify({"status": "ok", "issuer": cfg.issuer})

    return app
