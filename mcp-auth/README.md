# mcp-auth

An OAuth 2.1 authorization server for exactly one person, built to
satisfy what Claude's custom connectors require: **Dynamic Client
Registration**. Claude cannot be handed a client ID or a bearer token —
it has to register itself — and that single fact rules out reverse-proxy
basic auth, API keys, and cookie-based `forward_auth`.

It issues signed JWTs, each bound to one resource. It has no user
database, no
registration flow, and no password reset. There is one account, its
credentials live in the environment, and the second factor is mandatory.

## What it does and does not do

**Does:** authorization code flow with mandatory PKCE (S256 only),
Dynamic Client Registration (RFC 7591) constrained by a redirect-URI
allowlist, RS256 JWT access tokens with the audience bound to the
resource (RFC 8707), rotating refresh tokens, discovery metadata
(RFC 8414 and OIDC-style), a JWKS endpoint, revocation, TOTP, and
brute-force lockout.

**Does not:** implicit flow, password grant, client credentials,
multiple users, self-service registration, or email. Each of those is
absent on purpose — every one is a way in.

## The parts that carry the weight

**Open registration is safe only because of the allowlist.** DCR must
accept strangers; that is the protocol. What stops abuse is
`MCP_AUTH_ALLOWED_REDIRECT_PREFIXES`: a client can only register a
callback that starts with a URL you already trust. Without it, anyone
could register a client and have your authorization code delivered to
their server. The server refuses to start if it is unset.

**The audience is the point of the whole exercise.** Every token carries
`aud` = the resource it was minted for. The resource server checks it. A
token for one service cannot be replayed against another, which is
exactly the failure mode that makes a shared bearer token a bad idea.

**Refresh tokens rotate and are stored hashed.** Presenting one kills
it and issues a replacement. A copy of `auth.db` yields no usable
tokens.

**Authorization codes are single-use, enforced in SQL.** The code is
burned inside the same statement that reads it, so two simultaneous
requests cannot both succeed.

## Setup

```bash
cp .env.example .env && chmod 600 .env
docker compose build
docker compose run --rm mcp-auth mcp-auth init-user --username tim
```

That prints a QR code for your authenticator app and the four
environment lines to paste into `.env`. The password itself is never
written anywhere — only its Argon2id hash.

**Scan the QR code and verify it before starting the server.** Without a
working TOTP code you cannot log in, and there is no recovery path by
design:

```bash
docker compose run --rm mcp-auth mcp-auth check-totp
```

Then start it and add the Caddy block from `Caddyfile.snippet`:

```bash
docker compose up -d
curl -s https://mcp.timbeck.de/.well-known/oauth-authorization-server/auth | jq .
```

## Wiring it to imap-mcp

In imap-mcp's `config.toml`:

```toml
[auth]
enabled = true
base_url = "https://mcp.timbeck.de"
authorization_servers = ["https://mcp.timbeck.de/auth"]
jwks_uri = "https://mcp.timbeck.de/auth/jwks.json"
issuer = "https://mcp.timbeck.de/auth"
audience = "https://mcp.timbeck.de/mcp"
required_scopes = ["mail:read"]
```

`audience` here and `MCP_AUTH_RESOURCE` there must be the same string.
If they differ, every token is rejected — correctly, but confusingly.

## Operations

```bash
mcp-auth clients                          # registrations with token counts
mcp-auth clients --prune --older-than 7   # show leftovers (dry run)
mcp-auth clients --prune --older-than 7 --yes
mcp-auth clients --delete c_xxxxx         # remove one, revoking its tokens
mcp-auth revoke-all                       # kill every refresh token
```

**Expect several registrations for one connector.** Dynamic Client
Registration has no idempotency — the protocol gives a client no way to
ask "am I already registered here?", only to register. So every
reconnect leaves another `client_id` behind, and the list grows.

The token counts tell live registrations from leftovers. `--prune` only
ever offers registrations holding no valid refresh token, and lists them
before deleting anything; `--delete` refuses a client with live tokens
unless you add `--force`. Anything you do not recognise should be
deleted — but check the callback URL first, since a registration that
passed the allowlist is by definition one you already trusted.

## Several resources

`MCP_AUTH_RESOURCE` takes a comma-separated list. Each entry is one
service this server may mint tokens for, and each token carries exactly
one of them as its audience.

With more than one configured, a token request must name which resource
it wants. The server refuses to guess: handing a mail token to something
that asked for a calendar would be rejected downstream anyway, after a
detour that is tedious to debug.

Rotating the password or TOTP secret means re-running `init-user` and
replacing the values in `.env`. Existing access tokens stay valid until
they expire — run `revoke-all` too if you want everything cut off now.

## Tests

```bash
python3 selftest.py
```

83 checks covering the full flow and the attacks it must survive: PKCE
downgrade, foreign redirect URIs, code replay, codes stolen by a second
client, refresh rotation, tampered tokens, scope and audience
confinement, storage hygiene, lockout, multi-resource isolation and
the pruning safeguards.
