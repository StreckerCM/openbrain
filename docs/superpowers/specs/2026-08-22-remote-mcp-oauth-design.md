# Remote MCP Access — OAuth Resource Server Design

**Date:** 2026-08-22
**Scope:** Make `/mcp` reachable from the public internet with OAuth 2.1 authentication, while keeping the web UI, PostgREST, Adminer, and the write REST API reachable only from the LAN/tailnet.
**Out of scope (separate spec):** MCP tool-surface review for web clients; automatic capture/consolidation.

## Summary

OpenBrain's value is a single knowledge base that every agent shares — Claude Code on
five machines, claude.ai in a browser, ChatGPT, and headless agents on the VPS and
homelab. That requires the MCP endpoint to be reachable over public HTTPS. It does not
require, and must not include, exposing the web UI or the unauthenticated write API.

This spec adds OAuth 2.1 bearer-token authentication to the MCP endpoint, using the
existing Authentik instance at `https://auth.streckercm.com` as the Authorization
Server and the OpenBrain gateway as the Resource Server. It also separates the MCP
endpoint from the write REST API onto different listeners so the public ingress cannot
reach the write API even if its configuration drifts.

## Motivation

### The goal requires a server

Research happens in whatever client is at hand — the browser on a phone, ChatGPT on a
laptop, Claude Code on the desktop — and every one of those agents should know what the
others learned. Plugin-based memory systems that keep state in a local SQLite file
cannot do this: browser-hosted clients run on someone else's infrastructure and can only
reach a remote MCP server over public HTTPS. A server is the only architecture that
satisfies the goal.

### The goal also requires OAuth specifically

Static bearer tokens cannot serve the browser clients:

- **claude.ai** custom connectors accept an OAuth Client ID and Secret in Advanced
  settings. There is no field for an `Authorization: Bearer` header
  ([anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112)).
- **ChatGPT** developer-mode connectors support Streamable HTTP and SSE with auth of
  "OAuth or none," and cannot reach private networks.

So OAuth is a requirement, not a preference. A static-token path is still needed for
headless agents on the VPS and homelab, where no browser exists to complete a flow.

### Enforcement must live in the application

An earlier attempt used Cloudflare Access in front of the gateway. It appeared to work
and enforced nothing: the OpenBrain hostname resolves to the LAN address in local DNS,
so inside-LAN traffic never reached Cloudflare's edge. Any control that depends on
traffic taking a particular network path is one DNS record away from being bypassed.

Validating tokens inside the gateway makes enforcement path-independent. A request
arriving over the tunnel, the tailnet, the LAN, or localhost is subject to the same
check.

## Current State

Everything below is unauthenticated today.

| Port | Service | Bind | Exposure risk |
|---|---|---|---|
| 5433 | postgres | `0.0.0.0` | Direct database access |
| 3006 | postgrest | `0.0.0.0` | Unauthenticated full read of every table |
| 3007 | mcp-gateway | `0.0.0.0` | `/mcp` **and** the write API |
| 3008 | adminer | `0.0.0.0` | Database admin console |
| 3009 | docs | `0.0.0.0` | Low |
| 3010 | web-ui | `0.0.0.0` | The dashboard |

The gateway serves two different applications on one port (`mcp-gateway/server.py`, in
`_combined_app`):

```python
if path.startswith("/api"):
    await rest_app(scope, receive, send)   # unauthenticated writes
else:
    await mcp_asgi(scope, receive, send)   # /mcp
```

`rest_routes` includes `POST /api/knowledge`, `DELETE /api/bulk-delete`, and
`POST /api/tags/delete`, among others. There is no authentication anywhere in
`server.py` — the only credential it holds is the OpenAI key it sends outbound.

Note also that the `else` branch is a catch-all: every path that is not `/api` is handed
to the MCP application, so the public surface of that port is larger than `/mcp`.

The web UI does not depend on port 3007. Its own nginx proxies `/api/write/` and
`/api/search` to `http://mcp-gateway:3001` over the compose network
(`web-ui/nginx.conf`), so the published port exists only for external MCP clients.

## Design Decisions

**D1 — Authentik is the Authorization Server; the gateway is a Resource Server.**
This is the role split the MCP authorization spec defines. The gateway never handles
credentials, never runs a login flow, and never stores a password. It validates
signatures.

**D2 — Pre-registered OAuth client, not Dynamic Client Registration.**
Authentik 2026.5.2 does not support DCR; it shipped in 2026.8
([goauthentik/authentik#8751](https://github.com/goauthentik/authentik/issues/8751)).
No upgrade is required, because the MCP spec makes DCR optional and explicitly
deprecated, retained only for servers that do not support Client ID Metadata Documents.
Both claude.ai and ChatGPT accept a manually pre-registered client. Upgrading Authentik
is therefore optional and unrelated to this work.

**D3 — RS256 with JWKS, not a shared secret.**
The gateway fetches Authentik's public keys and verifies signatures. No secret is shared
between the two services, and key rotation requires no OpenBrain change.

**D4 — Two credential paths, one middleware.**
OAuth JWTs for interactive clients; optional static tokens for headless agents. Both are
checked in the same place so there is exactly one code path that can grant access.
Static tokens are disabled unless explicitly configured.

**D5 — The MCP endpoint and the write API listen on different ports.**
This is the one structural decision worth the extra code. A path allowlist on the public
ingress would also keep `/api/*` private, but it is configuration, and this project has
already been burned once by a security property that depended on configuration being
right. Two listeners make it impossible for the public ingress to reach the write API
regardless of how the ingress is configured, because it is not listening on that port.

**D6 — Fail closed on startup.**
If authentication is enabled but the issuer or JWKS URL is unset, the gateway refuses to
start rather than serving unauthenticated. Authentication defaults to enabled, which is
a deliberate breaking change for existing deployments — see Migration.

**D7 — `/api/*` stays unauthenticated, and stays private.**
Adding authentication to the write API means authenticating the web UI, which is a
separate project. This spec confines it to the private listener instead. That is a
smaller change with a stronger guarantee.

## 1. Listener Topology

The gateway process runs two uvicorn servers on one asyncio event loop.

| Listener | Port (container) | Serves | Auth |
|---|---|---|---|
| MCP | 3001 | `/mcp`, `/.well-known/oauth-protected-resource*` | Required |
| Internal API | 3002 | `/api/*` | None (private only) |

Anything else on the MCP listener returns `404`, replacing the current catch-all.

```python
async def _mcp_app(scope, receive, send):
    path = scope.get("path", "")
    if scope["type"] == "lifespan":
        await mcp_asgi(scope, receive, send)
        return
    if path.startswith("/.well-known/oauth-protected-resource"):
        await _resource_metadata(scope, receive, send)
        return
    if path == "/mcp" or path.startswith("/mcp/"):
        await _auth_middleware(mcp_asgi)(scope, receive, send)
        return
    await _not_found(scope, receive, send)
```

Both servers are started with `asyncio.gather` over two `uvicorn.Server.serve()`
coroutines, replacing the single `uvicorn.run` call at the bottom of `server.py`. The
existing combined lifespan (DB pool plus MCP session manager) is owned by the MCP
listener; the API listener reuses the same `AppContext`.

## 2. Protected Resource Metadata

The MCP spec requires the server to implement RFC 9728. Served unauthenticated on the
MCP listener at both `/.well-known/oauth-protected-resource` and
`/.well-known/oauth-protected-resource/mcp`, since clients differ on which they request:

```json
{
  "resource": "https://openbrain-mcp.streckercm.com/mcp",
  "authorization_servers": [
    "https://auth.streckercm.com/application/o/openbrain-mcp/"
  ],
  "scopes_supported": ["openbrain:read", "openbrain:write"],
  "bearer_methods_supported": ["header"]
}
```

Unauthenticated requests to `/mcp` receive:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://openbrain-mcp.streckercm.com/.well-known/oauth-protected-resource", scope="openbrain:read openbrain:write"
```

## 3. Authentication Middleware

Wraps the MCP app only. Order matters — cheapest and most decisive checks first.

1. Read `Authorization`. Missing or not `Bearer` → `401` with the challenge above.
2. If static tokens are configured and the value matches one under
   `hmac.compare_digest`, grant all scopes and proceed. Constant-time comparison
   is required; a plain `==` on a secret is a timing oracle.
3. Otherwise treat the value as a JWT:
   - Decode the header, read `kid`, resolve it against the cached JWKS. On an unknown
     `kid`, refetch once — this is how key rotation is absorbed without a restart.
   - Verify the RS256 signature.
   - Verify `iss` equals `MCP_OAUTH_ISSUER` exactly. No normalization: the MCP spec
     forbids scheme/host case folding, default-port elision, trailing-slash, and
     percent-encoding normalization before comparison.
   - Verify `aud` contains `MCP_RESOURCE_URI`. The spec is emphatic here — servers
     "MUST validate that access tokens were issued specifically for them as the intended
     audience" and "MUST NOT accept or transit any other tokens." This is the check that
     stops a token minted for some other Authentik application from working here.
   - Verify `exp` and `nbf`, with a small leeway for clock skew.
   - Any failure → `401`.
4. Verify the token's scopes include `MCP_REQUIRED_SCOPES`. Missing → `403` with
   `WWW-Authenticate: Bearer error="insufficient_scope", scope="...", resource_metadata="..."`.
5. Attach `sub` and the granted scopes to the ASGI scope for logging.

### JWKS caching

`PyJWT` ships `PyJWKClient`, but it fetches over blocking `urllib`, which would stall the
event loop on every cache miss inside an async server. Fetch the JWKS with the
`httpx.AsyncClient` already held in `AppContext`, cache the parsed `PyJWKSet` in memory
with a TTL (default 3600s), and refetch on unknown `kid` with a short floor between
refetches so an attacker cannot force unbounded outbound requests by sending junk `kid`
values.

A JWKS fetch failure while a cached set is still held is not fatal — serve from cache and
log. A failure with no cached set returns `503`, not `401`: the client's token may be
perfectly valid and telling it otherwise would send it into a pointless reauthorization
loop.

## 4. Authentik Configuration

Manual, one time, in the Authentik admin UI.

**OAuth2/OpenID Provider — "OpenBrain MCP"**

| Setting | Value |
|---|---|
| Client type | Confidential |
| Client ID / Secret | Generated; entered into each web client's Advanced settings |
| Redirect URIs | The callback URLs claude.ai and ChatGPT present during setup |
| Signing key | An RS256 certificate (enables JWKS validation) |
| Scopes | `openid`, `profile`, `email`, plus custom `openbrain:read`, `openbrain:write` |
| Subject mode | Based on user ID |

**Application** — slug `openbrain-mcp`, which fixes discovery at
`https://auth.streckercm.com/application/o/openbrain-mcp/.well-known/openid-configuration`
and JWKS at `.../application/o/openbrain-mcp/jwks/`.

Bind a policy restricting access to the owning user or a dedicated group. Without this,
every Authentik user can mint a working token.

**Audience.** The MCP spec requires clients to send
`resource=https://openbrain-mcp.streckercm.com/mcp` on both authorization and token
requests, whether or not the AS honors it. Authentik 2026.5's handling of the `resource`
parameter is unverified. If issued tokens do not carry the canonical URI in `aud`, add a
scope mapping that injects it. This must be confirmed by inspecting a real token before
the audience check is trusted — it is the single most likely integration failure.

## 5. Public Ingress

Requirement: `https://openbrain-mcp.streckercm.com` reaches the gateway's **MCP listener
only**.

Recommended realization is a `cloudflared` service inside the compose project, reaching
`http://mcp-gateway:3001` over the internal docker network. The MCP port then needs no
host publication at all, and the tunnel has no route to port 3002 to misconfigure.

If the existing public ingress is instead an NPMplus vhost on CT 127, that host reaches
CT 115 over published ports, so port 3001 must be published on an address CT 127 can
reach. The listener split still holds — NPMplus points at 3001 and never learns about
3002.

Per the split-DNS finding, `openbrain-mcp.streckercm.com` must **not** be added to local
DNS. Keeping it public-only means every client exercises the same path, and it makes the
off-LAN test in §8 meaningful.

## 6. Port Binding

Every service currently publishes on `0.0.0.0`. Bind addresses become parameterized so
the private set can be pinned to loopback or the tailnet address without editing the
compose file per host.

```yaml
# .env
PRIVATE_BIND=127.0.0.1   # or the CT 115 tailnet/LAN address NPMplus reaches
```

| Port | Service | Binding |
|---|---|---|
| 5433 | postgres | `${PRIVATE_BIND}` |
| 3006 | postgrest | `${PRIVATE_BIND}` |
| 3008 | adminer | `${PRIVATE_BIND}` |
| 3009 | docs | `${PRIVATE_BIND}` |
| 3010 | web-ui | `${PRIVATE_BIND}` |
| 3011 | mcp-gateway API (3002) | `${PRIVATE_BIND}` |
| 3007 | mcp-gateway MCP (3001) | Unpublished with a cloudflared sidecar; otherwise `${PRIVATE_BIND}` |

Adminer and PostgREST binding all interfaces are worth correcting on their own merits,
independent of this work — an unauthenticated full-database read and a database admin
console should not be listening on every interface of a container that is about to gain
a public ingress.

`web-ui/nginx.conf` changes its `/api/write/` and `/api/search` upstreams from
`mcp-gateway:3001` to `mcp-gateway:3002`. The `/api/read/` upstream to `postgrest:3000`
is unchanged.

## 7. Configuration

New variables in `.env.example`:

```bash
# Remote MCP authentication
MCP_AUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://auth.streckercm.com/application/o/openbrain-mcp/
MCP_OAUTH_JWKS_URL=https://auth.streckercm.com/application/o/openbrain-mcp/jwks/
MCP_RESOURCE_URI=https://openbrain-mcp.streckercm.com/mcp
MCP_REQUIRED_SCOPES=openbrain:read
MCP_JWKS_CACHE_TTL=3600

# Comma-separated tokens for headless agents. Empty disables the static path.
MCP_STATIC_TOKENS=

# Host interface for services that must not be publicly reachable
PRIVATE_BIND=127.0.0.1
```

## 8. Dependencies

Adds `pyjwt[crypto]` — the `crypto` extra pulls `cryptography`, required for RS256.

`mcp-gateway/requirements.txt` documents a hard-won rule: this service is pinned because
an unpinned rebuild pulled `mcp` 2.0.0, which removed `mcp.server.fastmcp` and
crash-looped the gateway. Adding a dependency means regenerating `constraints.txt` from a
`pip freeze` inside the `python:3.12-slim` build image, not from a development machine.
Capture a rollback reference before rebuilding:

```bash
docker compose exec mcp-gateway pip freeze > mcp-gateway-pins-known-good.txt
```

`cryptography` is a compiled wheel. Confirm it resolves on `python:3.12-slim` for the
deployment architecture rather than assuming the manylinux wheel is available.

## 9. Testing

Local Docker first, always. Nothing in this spec deploys to CT 115 before the matrix
below passes locally.

Local tests run against a stub Authorization Server: generate an RSA keypair in the test
fixture, serve a JWKS document from a local endpoint, and mint tokens with controlled
claims. This exercises every failure path without touching Authentik.

| # | Case | Expected |
|---|---|---|
| 1 | No `Authorization` header | `401` + `WWW-Authenticate` with `resource_metadata` and `scope` |
| 2 | Malformed / non-JWT token | `401` |
| 3 | Valid signature, wrong `aud` | `401` |
| 4 | Valid signature, wrong `iss` | `401` |
| 5 | Expired token | `401` |
| 6 | Signed by an unknown key | `401` |
| 7 | Valid token, missing required scope | `403`, `error="insufficient_scope"` |
| 8 | Valid token, correct scope | `200`, `tools/list` returns all 19 tools |
| 9 | Configured static token | `200` |
| 10 | Static token when `MCP_STATIC_TOKENS` empty | `401` |
| 11 | `GET /.well-known/oauth-protected-resource` | `200`, correct JSON, no auth required |
| 12 | `POST /api/bulk-delete` on the **MCP** listener | `404` |
| 13 | `POST /api/knowledge` on the **API** listener | `200` — web UI unaffected |
| 14 | Web UI end-to-end: create, edit, archive, tag merge | Unchanged |
| 15 | `MCP_AUTH_ENABLED=true` with issuer unset | Startup fails with a clear error |
| 16 | JWKS unreachable, cache warm | Requests still validate |
| 17 | JWKS unreachable, cache cold | `503`, not `401` |

Post-deploy, against production:

| # | Case | Expected |
|---|---|---|
| 18 | Inspect a real Authentik token's `aud` | Contains `MCP_RESOURCE_URI` — see §4 |
| 19 | `curl https://openbrain-mcp.streckercm.com/mcp` with no token, **from a phone on cellular** | `401`, not a Cloudflare login page and not a success |
| 20 | Same, from inside the LAN | `401` — proves enforcement is path-independent |
| 21 | `https://openbrain-mcp.streckercm.com/api/bulk-delete` | `404` |
| 22 | Web UI hostname from off-LAN | Unreachable |
| 23 | claude.ai custom connector, full OAuth flow | Tools usable |
| 24 | Claude Code via static token | Tools usable |

Test 20 is the one that would have caught the original Cloudflare Access failure. Do not
skip it because test 19 passed.

## 10. Migration

`MCP_AUTH_ENABLED` defaults to `true`, so the CT 115 deployment stops serving
unauthenticated MCP the moment it is updated. This is intended — the alternative default
fails open. Before deploying:

1. Configure the Authentik provider and application (§4).
2. Add the new variables to `/docker/openbrain/.env` on CT 115.
3. Generate a static token for existing Claude Code clients and add it to
   `MCP_STATIC_TOKENS` so they keep working across the cutover.
4. Update `.mcp.json` on each machine to send the token before restarting the stack.

CT 115's working tree should be clean as of `243c606`. If `git status` shows local
modifications to `docker-compose.yml`, investigate rather than stashing — the compose
file is changing in this work and a silent divergence will re-break networking.

## 11. Files to Create / Modify

| File | Change |
|---|---|
| `mcp-gateway/server.py` | Auth middleware, JWKS cache, resource metadata routes, listener split, replace catch-all with 404, dual-uvicorn startup |
| `mcp-gateway/requirements.txt` | Add `pyjwt[crypto]` pinned |
| `mcp-gateway/constraints.txt` | Regenerate from build-image `pip freeze` |
| `docker-compose.yml` | `${PRIVATE_BIND}` on private ports, publish API port, optional `cloudflared` service |
| `web-ui/nginx.conf` | `/api/write/` and `/api/search` upstreams → `mcp-gateway:3002` |
| `.env.example` | New variables from §7 |
| `README.md` | Remote access setup: Authentik provider, client configuration, static tokens |
| `mcp-gateway/test_auth.py` | New — the §9 local matrix |

## 12. Build Sequence

1. **Port binding hardening.** Independent of everything else and valuable on its own.
   Ship and verify first.
2. **Listener split.** Two ports, catch-all replaced with 404, nginx upstream updated.
   Verify the web UI is unaffected before adding any auth.
3. **Resource metadata endpoints and the 401 challenge**, with validation still stubbed
   to always-deny. Confirms discovery works before signature verification exists.
4. **Static token path.** Smallest real credential check; unblocks headless agents.
5. **JWT validation and JWKS caching.** The bulk of the work, against the stub AS.
6. **Authentik provider configuration**, and confirm `aud` on a real token (§4).
7. **Local end-to-end** — tests 1–17.
8. **Deploy to CT 115** following §10.
9. **Public ingress**, then tests 18–22.
10. **Client configuration** — claude.ai, ChatGPT, Claude Code — then tests 23–24.

Steps 1 and 2 are independently shippable and reduce standing exposure immediately. Do
not defer them behind the OAuth work.

## Open Questions

- **Existing public ingress.** Is `auth.streckercm.com` fronted by Cloudflare Tunnel or
  by NPMplus with a port forward? The answer decides §5's realization. The cloudflared
  sidecar is recommended either way, because it removes the possibility of the public
  ingress reaching port 3002.
- **`PRIVATE_BIND` value.** Loopback is tightest but breaks NPMplus on CT 127, which
  reaches CT 115 over published ports. If both containers are on the tailnet, the CT 115
  tailnet address is the right value.
- **Authentik `resource` parameter handling.** Unverified for 2026.5.2; determines
  whether a scope mapping is needed to set `aud`.
- **Gemini.** The MCP support found is Gemini Enterprise / Agent Platform and the
  experimental Python and JS SDKs. No evidence the consumer Gemini app supports custom
  remote MCP connectors. Nothing in this design depends on it, but the goal of "research
  in Gemini" may not be reachable through this path.

## Risks

**Blast radius is unchanged by this work.** Any holder of a valid token gets all 19
tools — full read and write across the entire knowledge base. There is no per-tool or
per-project scoping. `openbrain:read` and `openbrain:write` are defined here so that
scoping is possible later without a breaking change, but nothing enforces a split
between them yet. For a personal knowledge base this is an acceptable posture; it should
be a conscious one.

**Prompt injection reaches further than it used to.** Once browser-based agents can write
to the shared store, content one agent ingests from a web page can become instructions a
different agent reads later. That is a property of the shared-brain goal rather than of
this design, and it is not mitigated here.

**Recall, not storage, is the likely disappointment.** A shared store guarantees every
agent *can* see the same knowledge; nothing guarantees any of them *will look*. In
Claude Code that can be forced through `CLAUDE.md`. In claude.ai and ChatGPT the model
decides, largely from tool names and descriptions — and it is choosing among 19 tools.
Tool-surface design is likely to matter more to real usefulness than anything in this
spec. It is deliberately out of scope and should get its own.
