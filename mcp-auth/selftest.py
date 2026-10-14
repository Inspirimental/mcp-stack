"""Full OAuth flow plus the attacks it has to survive."""

import base64
import hashlib
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

import pyotp

from mcp_auth.config import Config
from mcp_auth.user import hash_password

FAILS = []
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://mcp.timbeck.de/mcp"
ISSUER = "https://mcp.timbeck.de/auth"
PASSWORD = "korrekt-pferd-batterie-heftklammer"


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


tmp = Path(tempfile.mkdtemp())
TOTP_SECRET = pyotp.random_base32()
cfg = Config(
    issuer=ISSUER,
    resources=(RESOURCE,),
    scopes=("mail:read", "mail:write"),
    username="tim",
    password_hash=hash_password(PASSWORD),
    totp_secret=TOTP_SECRET,
    session_secret=secrets.token_urlsafe(32),
    private_key_path=tmp / "key.pem",
    db_path=tmp / "auth.db",
    allowed_redirect_prefixes=(CALLBACK,),
)

from mcp_auth.app import create_app

app = create_app(cfg)
app.config["TESTING"] = True


def totp_now():
    return pyotp.TOTP(TOTP_SECRET).now()


def pkce_pair():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


# ------------------------------------------------------------- discovery
print("\ndiscovery")
c = app.test_client()

for path in (
    "/.well-known/oauth-authorization-server/auth",
    "/auth/.well-known/openid-configuration",
):
    r = c.get(path)
    check(f"metadata at {path}", r.status_code == 200, r.status_code)
    if r.status_code == 200:
        m = r.get_json()
        check(f"  issuer correct ({path.split('/')[-1]})", m["issuer"] == ISSUER)

meta = c.get("/auth/.well-known/openid-configuration").get_json()
check("advertises S256 only", meta["code_challenge_methods_supported"] == ["S256"])
check("advertises registration endpoint",
      meta["registration_endpoint"].endswith("/auth/register"))
check("advertises resource indicators", meta["resource_indicators_supported"] is True)
check("no implicit or password grant",
      set(meta["grant_types_supported"]) == {"authorization_code", "refresh_token"})

r = c.get("/auth/jwks.json")
jwks = r.get_json()
check("jwks served", r.status_code == 200 and len(jwks["keys"]) == 1)
check("jwks is public only", "d" not in jwks["keys"][0], "PRIVATE KEY LEAKED")
check("jwks has kid and alg",
      jwks["keys"][0].get("kid") and jwks["keys"][0]["alg"] == "RS256")

# ---------------------------------------------------------- registration
print("\ndynamic client registration")
r = c.post("/auth/register", json={
    "client_name": "Claude", "redirect_uris": [CALLBACK],
    "grant_types": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_method": "none"})
check("registration accepted", r.status_code == 201, r.get_data(as_text=True)[:120])
client = r.get_json()
CLIENT_ID = client["client_id"]
check("client_id issued", bool(CLIENT_ID))
check("public client gets no secret", "client_secret" not in client)

r = c.post("/auth/register", json={
    "client_name": "Angreifer", "redirect_uris": ["https://evil.example/cb"]})
check("foreign redirect_uri rejected", r.status_code == 400, r.status_code)
check("rejection names the reason",
      r.get_json().get("error") == "invalid_redirect_uri")

r = c.post("/auth/register", json={"client_name": "x", "redirect_uris": []})
check("empty redirect_uris rejected", r.status_code == 400)

r = c.post("/auth/register", json={
    "redirect_uris": [CALLBACK], "grant_types": ["password"]})
check("password grant rejected at registration", r.status_code == 400)

# ------------------------------------------------------------- authorize
print("\nauthorize endpoint")
verifier, challenge = pkce_pair()
base = {"response_type": "code", "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK, "scope": "mail:read",
        "resource": RESOURCE, "state": "xyz123"}

r = c.get("/auth/authorize", query_string={**base, "client_id": "c_unknown"})
check("unknown client rejected", r.status_code == 400)

r = c.get("/auth/authorize",
          query_string={**base, "redirect_uri": "https://evil.example/cb"})
check("unregistered redirect_uri rejected", r.status_code == 400)
check("and NOT redirected to it", "evil.example" not in r.headers.get("Location", ""))

r = c.get("/auth/authorize", query_string=base)  # no PKCE
loc = r.headers.get("Location", "")
check("missing PKCE refused", "error=invalid_request" in loc, loc[:120])

r = c.get("/auth/authorize", query_string={
    **base, "code_challenge": "abc", "code_challenge_method": "plain"})
check("PKCE downgrade to plain refused",
      "error=invalid_request" in r.headers.get("Location", ""))

r = c.get("/auth/authorize", query_string={
    **base, "code_challenge": challenge, "code_challenge_method": "S256",
    "scope": "mail:read admin:everything"})
check("unknown scope refused",
      "error=invalid_scope" in r.headers.get("Location", ""))

r = c.get("/auth/authorize", query_string={
    **base, "code_challenge": challenge, "code_challenge_method": "S256",
    "resource": "https://someone-elses-server.de/mcp"})
check("foreign resource refused",
      "error=invalid_target" in r.headers.get("Location", ""))

good_authorize = {**base, "code_challenge": challenge,
                  "code_challenge_method": "S256"}
r = c.get("/auth/authorize", query_string=good_authorize)
check("valid request shows the login page",
      r.status_code == 200 and "Authenticator" in r.get_data(as_text=True))

# ----------------------------------------------------------------- login
print("\nlogin")
html = r.get_data(as_text=True)
rid = html.split("consent?rid=")[1].split('"')[0]

r = c.post("/auth/login", data={"username": "tim", "password": "falsch",
                                "totp": totp_now(), "next": f"/auth/consent?rid={rid}"})
check("wrong password refused", r.status_code == 401)

r = c.post("/auth/login", data={"username": "tim", "password": PASSWORD,
                                "totp": "000000", "next": f"/auth/consent?rid={rid}"})
check("wrong TOTP refused", r.status_code == 401)

r = c.post("/auth/login", data={"username": "eve", "password": PASSWORD,
                                "totp": totp_now(), "next": f"/auth/consent?rid={rid}"})
check("wrong username refused", r.status_code == 401)

r = c.post("/auth/login", data={"username": "tim", "password": PASSWORD,
                                "totp": totp_now(),
                                "next": f"/auth/consent?rid={rid}"})
check("correct credentials accepted", r.status_code == 302, r.status_code)
check("redirected to consent", "consent" in r.headers.get("Location", ""))

r = c.get(f"/auth/consent?rid={rid}")
body = r.get_data(as_text=True)
check("consent page shows the client", "Claude" in body)
check("consent page shows the resource", RESOURCE in body)
check("consent page shows the scope", "mail:read" in body)

# --------------------------------------------------------------- consent
print("\nconsent and code")
fresh = app.test_client()
r = fresh.post("/auth/consent", data={"rid": rid, "approve": "yes"})
check("consent without a session refused", r.status_code == 401, r.status_code)

r = c.post("/auth/consent", data={"rid": rid, "approve": "yes"})
check("approval redirects to the client", r.status_code == 302)
loc = r.headers["Location"]
check("redirect goes to the registered callback", loc.startswith(CALLBACK))
params = parse_qs(urlparse(loc).query)
CODE = params["code"][0]
check("code returned", bool(CODE))
check("state echoed back", params["state"][0] == "xyz123")
check("iss returned (RFC 9207)", params["iss"][0] == ISSUER)

# ----------------------------------------------------------------- token
print("\ntoken exchange")
form = {"grant_type": "authorization_code", "code": CODE,
        "redirect_uri": CALLBACK, "client_id": CLIENT_ID}

r = c.post("/auth/token", data={**form, "code_verifier": "wrong-verifier-value"})
check("wrong PKCE verifier refused", r.status_code == 400
      and r.get_json()["error"] == "invalid_grant", r.get_data(as_text=True)[:120])

r = c.post("/auth/token", data={**form, "code_verifier": verifier,
                                "redirect_uri": "https://claude.ai/other"})
check("mismatched redirect_uri refused",
      r.get_json().get("error") == "invalid_grant")

# The failed attempts above already burned the code — that is the point.
r = c.post("/auth/token", data={**form, "code_verifier": verifier})
check("code is single use, even after a failed attempt",
      r.get_json().get("error") == "invalid_grant", r.get_data(as_text=True)[:120])


def full_flow(client_id=CLIENT_ID, scope="mail:read", resource=RESOURCE):
    """Run authorize -> login -> consent -> code and return (code, verifier)."""
    cl = app.test_client()
    v, ch = pkce_pair()
    r = cl.get("/auth/authorize", query_string={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": CALLBACK, "scope": scope, "resource": resource,
        "state": "s", "code_challenge": ch, "code_challenge_method": "S256"})
    rid_ = r.get_data(as_text=True).split("consent?rid=")[1].split('"')[0]
    cl.post("/auth/login", data={"username": "tim", "password": PASSWORD,
                                 "totp": totp_now(),
                                 "next": f"/auth/consent?rid={rid_}"})
    r = cl.post("/auth/consent", data={"rid": rid_, "approve": "yes"})
    return parse_qs(urlparse(r.headers["Location"]).query)["code"][0], v, cl


code2, verifier2, cl2 = full_flow()
r = cl2.post("/auth/token", data={
    "grant_type": "authorization_code", "code": code2,
    "redirect_uri": CALLBACK, "client_id": CLIENT_ID, "code_verifier": verifier2})
check("valid exchange succeeds", r.status_code == 200, r.get_data(as_text=True)[:160])
tok = r.get_json()
check("access token returned", bool(tok.get("access_token")))
check("refresh token returned", bool(tok.get("refresh_token")))
check("token type is Bearer", tok["token_type"] == "Bearer")

# ------------------------------------------------------- token contents
print("\ntoken verification (what the resource server does)")
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet

key_set = KeySet.import_key_set(c.get("/auth/jwks.json").get_json())
claims = jose_jwt.decode(tok["access_token"], key_set, algorithms=["RS256"]).claims
check("signature verifies against the published JWKS", True)
check("issuer claim correct", claims["iss"] == ISSUER, claims.get("iss"))
check("AUDIENCE IS THE RESOURCE", claims["aud"] == RESOURCE, claims.get("aud"))
check("subject is the user", claims["sub"] == "tim")
check("scope carried", claims["scope"] == "mail:read")
check("client_id carried", claims["client_id"] == CLIENT_ID)
check("expiry set", claims["exp"] - claims["iat"] == cfg.access_token_ttl)
check("has jti for replay tracking", bool(claims.get("jti")))

tampered = tok["access_token"][:-3] + ("aaa" if not tok["access_token"].endswith("aaa") else "bbb")
try:
    jose_jwt.decode(tampered, key_set, algorithms=["RS256"])
    check("tampered token rejected", False, "(accepted!)")
except Exception:
    check("tampered token rejected", True)

# ---------------------------------------------------- cross-client theft
print("\ncross-client and scope isolation")
r = c.post("/auth/register", json={
    "client_name": "Zweiter Client", "redirect_uris": [CALLBACK]})
OTHER_ID = r.get_json()["client_id"]

code3, verifier3, cl3 = full_flow()
r = cl3.post("/auth/token", data={
    "grant_type": "authorization_code", "code": code3,
    "redirect_uri": CALLBACK, "client_id": OTHER_ID, "code_verifier": verifier3})
check("code stolen by another client refused",
      r.get_json().get("error") == "invalid_grant", r.get_data(as_text=True)[:120])

code4, verifier4, cl4 = full_flow(scope="mail:read mail:write")
r = cl4.post("/auth/token", data={
    "grant_type": "authorization_code", "code": code4,
    "redirect_uri": CALLBACK, "client_id": CLIENT_ID, "code_verifier": verifier4})
claims4 = jose_jwt.decode(r.get_json()["access_token"], key_set, algorithms=["RS256"]).claims
check("requested scopes granted exactly", claims4["scope"] == "mail:read mail:write")

# --------------------------------------------------------- refresh flow
print("\nrefresh token rotation")
old_refresh = tok["refresh_token"]
r = c.post("/auth/token", data={"grant_type": "refresh_token",
                                "refresh_token": old_refresh,
                                "client_id": CLIENT_ID})
check("refresh succeeds", r.status_code == 200, r.get_data(as_text=True)[:120])
refreshed = r.get_json()
new_refresh = refreshed["refresh_token"]
check("a new refresh token is issued", new_refresh != old_refresh)

r = c.post("/auth/token", data={"grant_type": "refresh_token",
                                "refresh_token": old_refresh,
                                "client_id": CLIENT_ID})
check("OLD REFRESH TOKEN IS DEAD (rotation)",
      r.get_json().get("error") == "invalid_grant", r.get_data(as_text=True)[:120])

r = c.post("/auth/token", data={"grant_type": "refresh_token",
                                "refresh_token": new_refresh,
                                "client_id": OTHER_ID})
check("refresh token bound to its client",
      r.get_json().get("error") == "invalid_grant")

claims5 = jose_jwt.decode(refreshed["access_token"], key_set, algorithms=["RS256"]).claims
check("refreshed token keeps the audience", claims5["aud"] == RESOURCE)

c.post("/auth/revoke", data={"token": new_refresh})
r = c.post("/auth/token", data={"grant_type": "refresh_token",
                                "refresh_token": new_refresh,
                                "client_id": CLIENT_ID})
check("revoked refresh token refused",
      r.get_json().get("error") == "invalid_grant")

r = c.post("/auth/token", data={"grant_type": "password",
                                "username": "tim", "password": PASSWORD,
                                "client_id": CLIENT_ID})
check("password grant not available",
      r.get_json().get("error") == "unsupported_grant_type")

# ---------------------------------------------------------- refresh store
print("\nstorage hygiene")
import sqlite3

raw = sqlite3.connect(str(cfg.db_path))
rows = raw.execute("SELECT token_hash FROM refresh_tokens").fetchall()
check("refresh tokens stored hashed, never in the clear",
      all(len(r[0]) == 64 and r[0] != new_refresh for r in rows), rows[:1])
codes = raw.execute("SELECT used FROM auth_codes").fetchall()
check("used codes marked", any(r[0] == 1 for r in codes))
raw.close()
check("signing key file is owner-only",
      oct(cfg.private_key_path.stat().st_mode)[-3:] == "600",
      oct(cfg.private_key_path.stat().st_mode))

# --------------------------------------------------------------- lockout
print("\nbrute-force lockout")
locked = app.test_client()
statuses = []
for _ in range(7):
    rr = locked.post("/auth/login", data={
        "username": "tim", "password": "falsch", "totp": "000000",
        "next": "/auth/authorize"})
    statuses.append(rr.status_code)
check("repeated failures end in lockout", 429 in statuses, statuses)

r = locked.post("/auth/login", data={"username": "tim", "password": PASSWORD,
                                     "totp": totp_now(), "next": "/auth/authorize"})
check("lockout blocks even the correct password", r.status_code == 429, r.status_code)

# ------------------------------------------------ client bookkeeping
print("\nclient bookkeeping")
listed = {c["client_id"]: c for c in
          __import__("mcp_auth.store", fromlist=["Store"]).Store(cfg.db_path).list_clients()}
check("token counts reported per client",
      listed[CLIENT_ID]["active_tokens"] >= 1, listed[CLIENT_ID])
check("unused registration shows zero active",
      listed[OTHER_ID]["active_tokens"] == 0, listed[OTHER_ID])

store_ = __import__("mcp_auth.store", fromlist=["Store"]).Store(cfg.db_path)
prunable = {c["client_id"] for c in store_.prunable_clients(min_age_seconds=0)}
check("PRUNE NEVER OFFERS THE LIVE CLIENT", CLIENT_ID not in prunable, prunable)
check("prune offers the unused registration", OTHER_ID in prunable)
check("age limit protects fresh registrations",
      store_.prunable_clients(min_age_seconds=86400) == [])

# ------------------------------------------------- several resources
print("\nseveral resources")
RES2 = "https://mcp.timbeck.de/dav"
import dataclasses
cfg2 = dataclasses.replace(cfg, resources=(RESOURCE, RES2),
                           db_path=tmp / "auth2.db",
                           scopes=("mail:read", "calendar:read"))
app2 = create_app(cfg2)
app2.config["TESTING"] = True
c2 = app2.test_client()

from mcp_auth.oauth import OAuthError, validate_resource

check("named resource accepted",
      validate_resource(RES2, cfg2) == RES2)
check("trailing slash tolerated",
      validate_resource(RES2 + "/", cfg2) == RES2)
try:
    validate_resource(None, cfg2)
    check("AMBIGUOUS REQUEST REFUSED, NOT GUESSED", False, "(guessed one)")
except OAuthError as exc:
    check("AMBIGUOUS REQUEST REFUSED, NOT GUESSED", exc.error == "invalid_target")
try:
    validate_resource("https://fremder-dienst.de/mcp", cfg2)
    check("foreign resource refused", False)
except OAuthError as exc:
    check("foreign resource refused", exc.error == "invalid_target")
check("single resource still defaults without the parameter",
      validate_resource(None, cfg) == RESOURCE)

# A full flow against the second resource must yield that audience.
r = c2.post("/auth/register", json={"client_name": "Claude",
                                    "redirect_uris": [CALLBACK]})
cid2 = r.get_json()["client_id"]
v2, ch2 = pkce_pair()
r = c2.get("/auth/authorize", query_string={
    "response_type": "code", "client_id": cid2, "redirect_uri": CALLBACK,
    "scope": "calendar:read", "resource": RES2, "state": "s",
    "code_challenge": ch2, "code_challenge_method": "S256"})
rid2 = r.get_data(as_text=True).split("consent?rid=")[1].split('"')[0]
c2.post("/auth/login", data={"username": "tim", "password": PASSWORD,
                             "totp": totp_now(), "next": f"/auth/consent?rid={rid2}"})
r = c2.post("/auth/consent", data={"rid": rid2, "approve": "yes"})
code_dav = parse_qs(urlparse(r.headers["Location"]).query)["code"][0]
r = c2.post("/auth/token", data={
    "grant_type": "authorization_code", "code": code_dav,
    "redirect_uri": CALLBACK, "client_id": cid2, "code_verifier": v2})
ks2 = KeySet.import_key_set(c2.get("/auth/jwks.json").get_json())
dav_claims = jose_jwt.decode(r.get_json()["access_token"], ks2,
                             algorithms=["RS256"]).claims
check("CALENDAR TOKEN CARRIES THE CALENDAR AUDIENCE",
      dav_claims["aud"] == RES2, dav_claims.get("aud"))
check("and NOT the mail audience", dav_claims["aud"] != RESOURCE)
check("calendar scope carried", dav_claims["scope"] == "calendar:read")

print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("All checks passed.")
