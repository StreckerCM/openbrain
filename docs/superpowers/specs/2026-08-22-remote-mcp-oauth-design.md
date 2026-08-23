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
A path allowlist on the public ingress would also keep `/api/*` private, but it is
configuration, and this project has already been burned once by a security property
that depended on configuration being right. Splitting the listeners narrows what a
misconfigured ingress rule can do: because `mcp-gateway` still runs as one container on
one compose network, `cloudflared` has network-level reachability to port 3002 the same
way it does to 3001 — Compose's default network puts every service on the same bridge
with no per-service network isolation. The guarantee the split actually buys is that an
ingress rule targeting port 3001 cannot reach the write API *whatever path it allows*,
because that listener 404s everything outside `/mcp` and the metadata prefix. Before the
split, a permissive or catch-all ingress rule pointing at the gateway would have exposed
`/api/*` directly. After it, exposing `/api/*` requires someone to explicitly write an
ingress rule that targets `:3002`. That is a narrower guarantee than "no route exists,"
and it is still worth having. See Risks for the container-split option that would close
this gap, and why it is not being done now.

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

1. Read `Authorization`. Missing, not `Bearer`, or a credential containing non-ASCII bytes
   → `401` with the challenge above. The non-ASCII check runs before the value ever reaches
   `hmac.compare_digest`, which raises `TypeError` on non-ASCII `str` operands rather than
   returning `False` — a credential the server cannot even parse as a token is treated as an
   invalid credential, not a server error.
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

Concurrent fetch attempts are serialized behind an `asyncio.Lock`. A caller that decides a
fetch is needed (cold cache, expired cache, or an unknown `kid`) but finds one already in
flight joins it — awaiting the lock and then reading the cache the in-flight fetch just
populated — rather than treating itself as separately rate-limited and failing closed. This
matters because the refetch-rate-limit timestamp is written synchronously at the very start
of a fetch, before its first `await`; without lock-aware joining, every request in a
cold-start burst except the first would see the rate limit as already exhausted by the
in-flight fetch and raise `JWKSUnavailable`, turning every process restart into a burst of
spurious `503`s for the duration of one JWKS fetch.

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

### Topology

This mirrors the pattern already running for `auth.streckercm.com`: Cloudflare Tunnel
from the public internet, NPMplus for LAN and tailnet clients, one hostname resolved
differently depending on where you ask.

| Origin | Path |
|---|---|
| Public internet | Cloudflare edge → tunnel → `cloudflared` sidecar → `mcp-gateway:3001` |
| LAN / tailnet | NPMplus on CT 127 → CT 115 tailnet address → published MCP port |

A `cloudflared` service joins the compose project and reaches `http://mcp-gateway:3001`
over the internal docker network. Its ingress rule permits `/mcp` and
`/.well-known/oauth-protected-resource*` and nothing else. `mcp-gateway` and
`cloudflared` share Compose's default network with no per-service isolation, so
`cloudflared` *can* reach `mcp-gateway:3002` at the network layer — the write API is not
unreachable "by construction." What the ingress rule actually guarantees is narrower:
because the rule targets port 3001 and that listener 404s anything outside `/mcp` and
the metadata prefix, the write API stays private unless someone explicitly adds a rule
that targets `:3002`. That is the only line of defence here, not a second one behind a
network-level first line. See Risks for the container-split that would add a real
network boundary, and the cost of doing so.

### Split DNS is now safe, and that is a consequence of D1

The earlier Cloudflare Access attempt failed *because* of split DNS: LAN clients resolved
the hostname to the local address, never traversed the edge, and were never authenticated.
That failure mode does not exist here. The gateway validates tokens itself, so a request
arriving via NPMplus on the tailnet faces exactly the same check as one arriving via the
Cloudflare edge.

This is worth stating plainly because it inverts the earlier guidance in the project's
notes. `openbrain-mcp.streckercm.com` **may** be added to local DNS. Local clients get a
direct path with lower latency that keeps working when the internet does not — which for
a homelab knowledge base is a real benefit, not a micro-optimization.

The safety of this rests entirely on D1. If authentication is ever disabled or bypassed
on the assumption that "the tunnel protects it," the local path is wide open and nothing
will signal that. Test 20 in §9 exists to catch precisely that, and is the reason it must
be run from inside the LAN rather than only from off-network.

### What Cloudflare contributes

Cloudflare is not part of the authentication decision. It still earns its place:

- **No inbound port forward.** The tunnel is an outbound connection from the homelab.
  There is no listening port on the public interface to find, scan, or forward. This is
  the single largest security contribution, and it is structural.
- **DDoS absorption and TLS termination** at the edge, with certificate management.
- **Rate limiting**, scoped to `/mcp`.
- **Geo or ASN restriction**, if the set of countries you use agents from is small.
- **Edge request logging**, independent of the gateway's own logs.

Two settings need care rather than defaults:

**WAF managed rules.** MCP traffic is JSON-RPC in POST bodies. Managed rulesets can
false-positive on payloads that contain code, SQL fragments, or shell snippets — which is
routine content for a knowledge base about software. Start in log-only mode, review what
matches over a week of real use, and enable blocking only for rules that produced no
false positives. A WAF that silently eats `add_knowledge` calls will look like an
intermittent client bug.

**Rate limiting thresholds.** An agent working through a task issues tool calls in
bursts, not at a steady rate. Set the threshold well above anything a single agent
produces — this is a backstop against automated abuse of a public endpoint, not a quota.

Streamable HTTP may hold long-lived connections. Cloudflare's proxy timeouts and
buffering behavior for streaming responses need verifying against real MCP traffic rather
than assumed; see §9 test 25 and the Risks section.

## 6. Port Binding

Every service currently publishes on `0.0.0.0`. Bind addresses become parameterized so
the private set can be pinned without editing the compose file per host.

**Both CT 115 and CT 127 join the tailnet**, and `PRIVATE_BIND` becomes CT 115's tailnet
address. This is the tightest option that still works: NPMplus on CT 127 reaches CT 115
over the tailnet rather than the LAN, and the web UI becomes reachable from the laptop and
phone without being public — which is the stated goal for the UI, achieved without any
ingress at all.

```yaml
# .env
PRIVATE_BIND=100.x.y.z   # CT 115 tailnet address
```

| Port | Service | Binding |
|---|---|---|
| 5433 | postgres | `${PRIVATE_BIND}` |
| 3006 | postgrest | `${PRIVATE_BIND}` |
| 3008 | adminer | `${PRIVATE_BIND}` |
| 3009 | docs | `${PRIVATE_BIND}` |
| 3010 | web-ui | `${PRIVATE_BIND}` |
| 3011 | mcp-gateway API (3002) | `${PRIVATE_BIND}` |
| 3007 | mcp-gateway MCP (3001) | `${PRIVATE_BIND}` — for the NPMplus local path only. The public path goes through the cloudflared sidecar over the docker network and does not use this port. |

Binding only to the tailnet address means the stack is unreachable locally if tailscaled
stops. If that tradeoff is unwelcome, docker accepts a second published entry per
container port, so a LAN address can be added alongside as a fallback. Do not add
`0.0.0.0` back as the fallback.

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

# CT 115 tailnet address. Every service except the tunnel binds here.
PRIVATE_BIND=100.x.y.z

# Path on the host to the tunnel credentials JSON from
# `cloudflared tunnel create`. Kept outside the repository.
CLOUDFLARED_CREDENTIALS_FILE=/etc/cloudflared/openbrain-mcp.json
```

A locally-managed tunnel is used rather than a dashboard-managed one with a connector
token, so the ingress rules — the definition of the public surface — live in
`cloudflared/config.yml` under version control and are reviewable in a diff, rather than
in a web UI where a change leaves no trace.

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
| 19 | `curl https://openbrain-mcp.streckercm.com/mcp` with no token, **from a phone on cellular** | `401` with the challenge — not a Cloudflare error page, not a success |
| 20 | Same, **from inside the LAN** (resolves via NPMplus) | `401` — proves enforcement is path-independent |
| 21 | Same, **from the tailnet** | `401` |
| 22 | Valid token over the Cloudflare path, and over the NPMplus path | Both `200`, identical tool list |
| 23 | `https://openbrain-mcp.streckercm.com/api/bulk-delete`, public path | `404` |
| 24 | Web UI hostname from off-tailnet, off-LAN | Unreachable |
| 25 | A long-running streaming MCP response over the Cloudflare path | Completes without truncation or idle timeout |
| 26 | A tool call whose payload contains code and SQL fragments, WAF in log-only | No block; review what the managed ruleset matched |
| 27 | claude.ai custom connector, full OAuth flow | Tools usable |
| 28 | ChatGPT developer-mode connector | Tools usable |
| 29 | Claude Code via static token | Tools usable |

Tests 20 and 21 are the ones that would have caught the original Cloudflare Access
failure. Do not skip them because 19 passed — 19 passing while 20 fails is exactly the
shape of the earlier bug, and the whole point of D1 is that both must now return `401`.

Test 25 matters because a truncated stream will present as a flaky client rather than as
an infrastructure problem, and the two are diagnosed very differently.

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
| `docker-compose.yml` | `${PRIVATE_BIND}` on private ports, publish API port, add `cloudflared` service |
| `cloudflared/config.yml` | New — tunnel ingress restricted to `/mcp` and the metadata paths |
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
9. **Tailnet enrollment** for CT 115 and CT 127, `PRIVATE_BIND` set, NPMplus vhost
   repointed at the CT 115 tailnet address. Verify the web UI and the local MCP path.
10. **cloudflared sidecar and public hostname**, then tests 18–26. WAF stays in log-only
    mode from here; rate limiting can be enabled immediately.
11. **Client configuration** — claude.ai, ChatGPT, Claude Code — then tests 27–29.
12. **WAF review** after a week of real traffic; enable blocking only for rules with no
    false positives.

Steps 1 and 2 are independently shippable and reduce standing exposure immediately. Do
not defer them behind the OAuth work.

## Open Questions

- **Authentik `resource` parameter handling.** Unverified for 2026.5.2; determines
  whether a scope mapping is needed to set `aud`. Resolved by test 18, which gates
  trusting the audience check at all.
- **Streaming through Cloudflare.** Whether the edge's proxy timeouts and buffering
  handle long-lived streamable-HTTP responses without truncation. Resolved by test 25. If
  it does not hold, the fallback is to confirm the transport degrades to discrete
  request/response cleanly rather than hanging.
- **WAF false-positive rate** on knowledge-base payloads. Resolved by a week of log-only
  operation (test 26) before any rule is set to block.

Resolved during design:

- Public ingress is Cloudflare Tunnel; LAN and tailnet go through NPMplus. §5 matches the
  pattern already running for `auth.streckercm.com`.
- `PRIVATE_BIND` is CT 115's tailnet address, with CT 127 joining the tailnet.
- Gemini is not a requirement. Its MCP support is Gemini Enterprise / Agent Platform and
  the experimental SDKs, with no evidence the consumer app supports custom remote
  connectors. Nothing here depends on it.

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

**Cloudflare becomes a dependency for remote access, and a source of silent failures.**
The tunnel is a hard dependency for every off-network client; the local NPMplus path is
the mitigation, and it is why keeping split DNS is worth doing rather than merely safe.
The subtler risk is that the WAF and rate limiter fail *quietly from the client's
perspective* — a blocked `add_knowledge` looks like a tool that didn't work, not like a
security control that fired. Log-only first, and check edge logs before debugging the
gateway whenever a tool call fails only from a remote client.

**The write API's isolation from the tunnel is a rule, not a network boundary.**
`cloudflared` and `mcp-gateway` run in the same compose project with no `networks:` key,
so they share Docker Compose's default network and `cloudflared` has the same
network-level reachability to `mcp-gateway:3002` as it does to `:3001`. The only thing
stopping the tunnel from reaching the write API is that its ingress config has no rule
naming port 3002. Closing this properly means splitting `mcp-gateway` into two
containers — one for the MCP listener, one for the write API — placed on separate
compose networks so there is a real network boundary instead of an omitted rule. That is
future work, not part of this design, because Task 2 deliberately unified lifespan
ownership: one process owns the DB connection pool and the MCP session manager so
startup ordering between them is a non-issue. Splitting into two containers means two
pools and two session managers, and reintroduces the startup-ordering problem Task 2
specifically removed. Worth doing if the write API ever needs to be reachable from
somewhere less trusted than it is today; not worth doing to fix a documentation
overclaim.

**Recall, not storage, is the likely disappointment.** A shared store guarantees every
agent *can* see the same knowledge; nothing guarantees any of them *will look*. In
Claude Code that can be forced through `CLAUDE.md`. In claude.ai and ChatGPT the model
decides, largely from tool names and descriptions — and it is choosing among 19 tools.
Tool-surface design is likely to matter more to real usefulness than anything in this
spec. It is deliberately out of scope and should get its own.
