# Deploy sheet — remote MCP with OAuth, CT 115

**Target:** CT 115, `/docker/openbrain`, reachable as `docker.lan.streckercm.com` (192.168.72.129)
**Source:** branch `development` (`da90e51`) — `main` is deliberately left at `8f45494` as the revert point
**Written:** 2026-08-23

> **Status 2026-08-23: Phases 0-4 are DONE on CT 115 and the claude.ai connector is live.**
> A real Authentik JWT passes signature, issuer, expiry and audience validation; `POST /mcp`
> returns 200 with zero `[auth]` rejections. What remains is Phase 5's off-network check and the
> Cloudflare edge controls. Everything below has been executed at least once — the corrections
> marked **Learned the hard way** are things that actually broke.

This replaces the runbook at the end of `docs/superpowers/plans/2026-08-22-remote-mcp-oauth.md`,
which assumed everything lands in one cutover. It does not have to, and it should not: the
refactor and the authentication are independent risks and separating them tells you which one
broke something.

## Access

Direct SSH to the LXC, as the `docker` user:

```bash
ssh docker@192.168.72.129
```

Notes that matter for every command below:

- The repo at `/docker/openbrain` is **root-owned**, and `.env` is `0600 root`. So `git` and
  `docker compose` both need `sudo` — compose cannot even read `.env` otherwise.
- Passwordless `sudo` works for this user.
- The docker socket is `root:docker 660` and the user's primary group is `docker`, so bare
  `docker` commands work; it is only `.env` and the repo files that force `sudo`.
- Going in via `ssh streckercm@pve` + `sudo pct exec 115` also works and runs as root, but the
  direct route is preferred — it does not need the hypervisor and does not run everything as root.
- Phase 0 artifacts written under `/root/` are readable with `sudo cat`.

## Before you start

**The stack is serving real data.** Nothing here is reversible without a backup.

**Auth defaults to ON.** The moment the new gateway starts without `MCP_AUTH_ENABLED=false`,
every existing client stops working until it presents a credential. Phase 1 keeps it off on
purpose.

**One decision you must make, in Phase 1.** `PRIVATE_BIND` is a single value applied to every
published port. Which address you choose determines what stays reachable:

| Value | PostgREST 3006 / Adminer 3008 / Postgres 5433 | web-ui 3010, docs 3009, gateway 3007 |
|---|---|---|
| `192.168.72.129` (LAN) | Still LAN-wide, as today | NPMplus keeps working |
| CT 115 tailnet address | Tailnet only | NPMplus works — it is already on the tailnet (100.64.0.5) |
| `127.0.0.1` | Closed | **NPMplus breaks** |

**Tailnet status, checked 2026-08-23 from `pve`:** NPMplus is already enrolled as `npmplus`
(100.64.0.5). CT 115 is **not** on the tailnet. So only one machine needs enrolling, not the two
the plan assumed — and once CT 115 joins, `PRIVATE_BIND` can be its tailnet address and NPMplus
reaches it over the tailnet with no LAN exposure at all. That is the option worth taking.

The security win needs the tailnet. But note that the web UI reaches PostgREST over the Docker
network (`proxy_pass http://postgrest:3000` in `web-ui/nginx.conf`), not the published port — so
3006 exists only for **direct** PostgREST clients. `docs/readme.md` advertises PostgREST as a
second agent interface, so decide whether anything actually uses it before closing it.

Right now `curl http://docker.lan.streckercm.com:3006/` and `:3008/` both return 200 from any LAN
host. That is the exposure this closes.

---

## Phase 0 — Backup and record the rollback point

```bash
cd /docker/openbrain

# Where you are now, so you can get back
git rev-parse HEAD | tee /root/openbrain-rollback-commit.txt
docker compose config > /root/openbrain-rollback-compose.yml
cp .env /root/openbrain-rollback.env
docker compose exec mcp-gateway pip freeze > /root/openbrain-rollback-pins.txt

# Database
docker compose exec -T db pg_dump -U openbrain openbrain \
  | gzip > /root/openbrain-$(date +%F-%H%M).sql.gz
ls -lh /root/openbrain-*.sql.gz
```

**Verified state of CT 115, 2026-08-23** (checked via `pct exec 115` from pve):

| | |
|---|---|
| Path | `/docker/openbrain` — correct |
| Branch / commit | `main` at `4a5e567` (PR #14) |
| Behind `development` by | **42 commits** |
| Working tree | **Clean.** `docker-compose.yml` md5 matches the committed blob exactly. The
divergence warning in the old notes is resolved — a pull is safe. |
| Untracked | `docker-compose.yml.bak-20260716` only |
| Containers | all 7 up |
| `.env` keys | `OPENAI_API_KEY`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `PGRST_JWT_SECRET`, `SENTRY_DSN` — nothing else |

Because CT 115 sits at `4a5e567`, this pull also brings PRs #15 (MCP URL trailing slash), #16
(docs), #17 (the `add_project` duplicate-name fix) and #18 (spec/plan) — not just the auth work.
Larger blast radius than a one-PR deploy; worth knowing before you start.

Confirm nothing has changed since that check:

```bash
git status --short
git fetch origin
git log --oneline HEAD..origin/development | head
```

---

## Phase 1 — Deploy the code with authentication OFF

This proves the listener split, the port binding, and the `db.py` extraction independently of
anything to do with auth. If something breaks here, it is not the authentication.

```bash
cd /docker/openbrain
git checkout development   # or: git merge --ff-only origin/development
git pull origin development
```

Add to `.env` — note `MCP_AUTH_ENABLED=false` for this phase only:

```bash
PRIVATE_BIND=192.168.72.129        # your Phase-1 decision from the table above

MCP_AUTH_ENABLED=false
MCP_OAUTH_ISSUER=
MCP_OAUTH_JWKS_URL=
MCP_RESOURCE_URI=
MCP_REQUIRED_SCOPES=openbrain:read
MCP_JWKS_CACHE_TTL=3600
MCP_STATIC_TOKENS=
```

There is no `cloudflared` service in the compose project and no
`CLOUDFLARED_CREDENTIALS_FILE` to set — CT 126 already runs a dashboard-managed tunnel and will
front this hostname (Phase 4).

Then:

```bash
sudo docker compose build mcp-gateway web-ui
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=30 mcp-gateway
```

**Build BOTH changed images.** `web-ui`'s image bakes in `nginx.conf`, whose write upstreams
moved from port 3001 to 3002. `docker compose up -d` reuses an existing image, so building only
`mcp-gateway` leaves the web UI proxying at the old port: reads keep working while every write
returns 404. That half-failure reads like an application bug rather than a stale image. Hit live
on 2026-08-23.

Expect the gateway log to say `MCP listener on :3001, private API listener on :3002`.

With `PRIVATE_BIND` set to the LAN IP the ports bind to that interface **only** — `127.0.0.1`
will not answer. Every check below uses `$H`:

```bash
H=192.168.72.129
```

**Verify Phase 1.** Port 3007 now serves `/mcp` only. The write REST API moved to container port 3002 and is **not** published — reachable only from inside this host's docker network.

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://$H:3007/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'          # 200 — auth is off
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://$H:3007/api/bulk-delete
                                                                # 404 — the catch-all is gone
curl -s -o /dev/null -w "%{http_code}\n" http://$H:3010/ # 200 — web UI
```

Open the web UI through NPMplus and confirm create, edit, archive, and search still work. Those
exercise `/api/read/`, `/api/write/`, and `/api/search`, whose upstreams moved to port 3002.

**Rollback if this phase fails:**

```bash
git checkout $(cat /root/openbrain-rollback-commit.txt)
cp /root/openbrain-rollback.env .env
docker compose up -d --build
```

---

## Phase 2 — Turn on authentication with a static token

Generate a token **on CT 115** rather than reusing one from a chat log:

```bash
echo "obk_$(openssl rand -hex 32)"
```

**Update every client before restarting.** This is the step that breaks things if done out of
order. For each machine running Claude Code against OpenBrain, add the header — keeping the token
out of the tracked `.mcp.json`, either via `claude mcp add-json` at local scope or by referencing
an environment variable:

```json
{ "headers": { "Authorization": "Bearer ${OPENBRAIN_MCP_TOKEN}" } }
```

Then on CT 115:

```bash
sed -i 's/^MCP_AUTH_ENABLED=.*/MCP_AUTH_ENABLED=true/' .env
sed -i 's|^MCP_STATIC_TOKENS=.*|MCP_STATIC_TOKENS=obk_YOUR_TOKEN_HERE|' .env
sed -i 's|^MCP_RESOURCE_URI=.*|MCP_RESOURCE_URI=https://openbrain-mcp.streckercm.com/mcp|' .env
```

`MCP_OAUTH_ISSUER` and `MCP_OAUTH_JWKS_URL` are still blank, and auth is now enabled — **the
gateway will refuse to start.** That is the fail-closed design working. You have two options:

- Do Phase 3 first and set all three together, or
- Point issuer and JWKS at the Authentik application you are about to create; the values are
  predictable from the slug, and nothing validates them until a JWT actually arrives.

```bash
MCP_OAUTH_ISSUER=https://auth.streckercm.com/application/o/openbrain-mcp/
MCP_OAUTH_JWKS_URL=https://auth.streckercm.com/application/o/openbrain-mcp/jwks/
```

Restart and verify:

```bash
docker compose up -d
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://$H:3007/mcp   # 401
curl -s -i -X POST http://$H:3007/mcp | grep -i www-authenticate      # challenge present
curl -s http://$H:3007/.well-known/oauth-protected-resource           # metadata, no auth
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://$H:3007/mcp \
  -H "Authorization: Bearer obk_YOUR_TOKEN_HERE" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'                        # 200
```

Confirm your Claude Code clients still work. **Stop here and fix before continuing if they do
not** — everything after this assumes a working authenticated path.

---

## Phase 3 — Authentik provider

Authentik is 2026.5.2 and has **no Dynamic Client Registration** (that shipped in 2026.8), so the
client is registered by hand. That is fine — the MCP spec treats DCR as optional and deprecated,
and both claude.ai and ChatGPT accept a pre-registered client ID and secret.

Verified 2026-08-23: `https://auth.streckercm.com/application/o/openbrain-mcp/.well-known/openid-configuration`
returns **404**, so nothing exists yet.

### 3a. Scope mappings — create three

Applications → Property Mappings → Create → **Scope Mapping**, three times:

| Name | Scope name | Expression |
|---|---|---|
| OpenBrain read | `openbrain:read` | `return {}` |
| OpenBrain write | `openbrain:write` | `return {}` |

**Learned the hard way — put the audience on `openbrain:read`, not its own scope.** The obvious
design is a third `openbrain:aud` mapping. It does not work: authentik only evaluates a scope
mapping when that scope is **requested**, and the gateway's own metadata advertises
`scopes_supported: ["openbrain:read", "openbrain:write"]`. A client following that metadata never
asks for `openbrain:aud`, the mapping never fires, `aud` is absent, and every token is rejected
with a bare 401. So the read mapping's expression is:

```python
return {"aud": "https://openbrain-mcp.streckercm.com/mcp"}
```

`openbrain:read` is in `MCP_REQUIRED_SCOPES`, so any usable token necessarily carries it.

The third one is the load-bearing piece and the most likely thing to get wrong. The gateway
rejects any token whose `aud` is not exactly `https://openbrain-mcp.streckercm.com/mcp`, because
the MCP spec requires a resource server to accept only tokens minted for it. Authentik 2026.5 is
not known to honour the OAuth `resource` parameter, so the audience is injected by this mapping
rather than derived from the request.

### 3b. Provider

Applications → Providers → Create → **OAuth2/OpenID Provider**. Field list below is the complete
set from Authentik 2026.5.2's own API schema (`/api/v3/schema/`, `OAuth2ProviderRequest`), not a
summary. Only four are required by Authentik — `name`, `authorization_flow`, `invalidation_flow`,
`redirect_uris` — but several optional ones are load-bearing for us.

**Must be set to these values, or the integration fails:**

| Field | Value | Why |
|---|---|---|
| **Issuer mode** | **`per_provider`** — "Each provider has a different issuer, based on the application slug" | With `global` the `iss` claim becomes `https://auth.streckercm.com/application/o/` with no slug. The gateway compares `iss` **exactly, with no normalisation**, against `https://auth.streckercm.com/application/o/openbrain-mcp/`. Wrong value → every token 401s with nothing indicating why. |
| **Signing Key** | an **RS256** certificate | **Leaving this empty is not neutral.** Authentik then signs symmetrically with the client secret (HS256). The gateway pins `algorithms=["RS256"]` and validates via JWKS, so an unsigned-by-certificate provider fails every request. |
| Client type | `confidential` | claude.ai stores a client secret. |
| Property mappings | `openid`, `profile`, `email` + the three from 3a | The audience mapping must be here or `aud` never appears. |
| Redirect URIs | see below | Required field. |

**Leave at defaults unless you have a reason:**

| Field | Note |
|---|---|
| Authorization flow | Your explicit-consent flow. Required. |
| Invalidation flow | Required in 2026.5 — the default provider invalidation flow is fine. |
| Authentication flow | Optional; blank uses the default. |
| Client ID / Secret | Auto-generated. **Copy both** — they go into the claude.ai connector. |
| Grant types | Needs `authorization_code` and `refresh_token`. |
| Access code validity | Default (~1 min) is fine. |
| Access token validity | Default (~5 min) is fine; refresh covers longer sessions. Raising it lengthens the window a leaked token stays usable. |
| Refresh token validity | Default (~30 days). |
| Subject mode | Any works — the gateway only requires `sub` to be present. `hashed_user_id` is the default. |
| Include claims in id_token | Irrelevant here; the gateway validates the **access** token. |
| Encryption key | Leave empty. An encrypted JWT is not a JWS the gateway can verify. |
| Logout URI / method | Not used. |
| JWT federation sources/providers | Not used. |

Redirect URIs are entries of `{matching_mode, url}`, where matching mode is `strict` or `regex`:

| Matching mode | URL |
|---|---|
| `strict` | `https://claude.ai/api/mcp/auth_callback` |
| `strict` | `https://claude.com/api/mcp/auth_callback` |

The `.com` entry is Anthropic's documented future callback — allowlisting it now avoids a silent
break later. If you also want Claude Code to use OAuth instead of its static token, add a `regex`
entry such as `http://(localhost|127\.0\.0\.1):[0-9]+/callback`, because Claude Code redirects to
an ephemeral loopback port. Pinning `--callback-port` and using a `strict` entry is tighter if you
prefer.

### 3c. Application

Applications → Applications → Create:

| Setting | Value |
|---|---|
| Name | `OpenBrain MCP` |
| **Slug** | **`openbrain-mcp`** — must match exactly; it is what makes the discovery URLs in `.env` resolve |
| Provider | `OpenBrain MCP` |

**Bind a policy** restricting this application to your account or a dedicated group. Without one,
every Authentik user can mint a token that the gateway will accept.

### 3d. Verify

Discovery should now resolve:

```bash
curl -s https://auth.streckercm.com/application/o/openbrain-mcp/.well-known/openid-configuration | head -c 400
curl -s -o /dev/null -w "%{http_code}
" https://auth.streckercm.com/application/o/openbrain-mcp/jwks/
```

Then the check that gates Phase 4 — obtain a real access token and decode its payload:

```bash
echo '<paste JWT>' | cut -d. -f2 | base64 -d 2>/dev/null | python3 -m json.tool
```

Confirm all four:

- `aud` contains `https://openbrain-mcp.streckercm.com/mcp`
- `iss` is exactly `https://auth.streckercm.com/application/o/openbrain-mcp/` — trailing slash
  included, since the gateway compares without normalising
- `scope` includes `openbrain:read`
- the JWT header shows `"alg": "RS256"` and carries a `kid`

If `aud` is missing or wrong, the mapping in 3a is not attached to the provider. Everything else
can look perfect and every request will still return an undifferentiated 401.

## Phase 4 — Public ingress via the existing tunnel (CT 126)

CT 126 (`192.168.72.19`) already runs `cloudflared --no-autoupdate tunnel run --token …` as a
systemd service. It is a **dashboard-managed** tunnel, so there is no local config file to edit —
add the hostname in Cloudflare Zero Trust → Networks → Tunnels → your tunnel → Public Hostnames:

| Field | Value |
|---|---|
| Subdomain | `openbrain-mcp` |
| Domain | `streckercm.com` |
| Path | `mcp` |
| Service | `HTTP` → `192.168.72.129:3007` |

Add a second public hostname entry with path `.well-known/oauth-protected-resource` and the same
service, so OAuth discovery reaches the gateway. Everything not matched by a public hostname
returns Cloudflare's 404 — no catch-all rule is needed or wanted.

Because the tunnel runs on CT 126 rather than inside the compose project, port 3007 must stay
reachable from `192.168.72.19`. That is the same requirement NPMplus already imposes, which is why
`PRIVATE_BIND` cannot be loopback.

**Do not add this hostname to local DNS yet.** Keep it public-only until the off-network checks in
Phase 5 pass, so you are testing the path you think you are testing.

Verify the tunnel picked it up:

```bash
ssh streckercm@192.168.72.102 "sudo pct exec 126 -- journalctl -u cloudflared -n 20 --no-pager"
```

---

## Phase 4b — The claude.ai connector

Settings → Connectors → Add custom connector.

| Field | Value |
|---|---|
| **URL** | `https://openbrain-mcp.streckercm.com/mcp` — **the `/mcp` path is required** |
| Advanced → OAuth Client ID | from the authentik provider |
| Advanced → OAuth Client Secret | from the authentik provider |

**Learned the hard way — the URL must include `/mcp`.** Entering the bare host gets you a long way
before failing: both `.well-known` documents live at the host root, so OAuth discovery succeeds
and the consent flow completes, and only then does claude.ai `POST /` and get a 404. The error it
shows is *"Couldn't connect to the server. Check that the URL points to a valid MCP server"*,
which reads like a networking or auth fault rather than a missing path. The gateway log is
unambiguous — `POST / 404` next to `GET /.well-known/... 200`.

**Learned the hard way — the gateway must serve authorization-server metadata itself.** claude.ai
reads our RFC 9728 document, then probes `/.well-known/oauth-authorization-server` on the
**resource server's** origin rather than following the `authorization_servers` field to authentik.
authentik 2026.5 serves only the OIDC-style discovery path, so there is nothing to redirect to
either. PR #21 added a shim on the gateway that returns authentik's document, `issuer` unchanged —
rewriting it would break the RFC 9207 comparison the client makes against `iss` in the
authorization response. Without that shim the flow dead-ends at
`https://openbrain-mcp.streckercm.com/authorize`, which does not exist.

**On consent flows.** With `default-provider-authorization-explicit-consent`, cancelling the
consent prompt logs you out of authentik and dumps you at `/if/user/#/library`, which looks like a
redirect-URI misconfiguration and is not. If you are the only user, the implicit-consent flow
removes the failure mode entirely.

## Diagnostics that actually work

**Gateway auth decisions.** Use `grep -F` and do not use a short `--tail`; the interesting lines
scroll away fast:

```bash
ssh docker@192.168.72.129 'cd /docker/openbrain && sudo docker compose logs mcp-gateway 2>&1 | grep -F "[auth]" | tail -20'
```

Only four paths log: JWKS fetch failure, malformed token, missing `kid`, and JWT decode failure
(where audience, issuer, expiry and signature rejections all surface). **A request with no
`Authorization` header logs nothing** — so a 401 with no `[auth]` line means the client never sent
a credential, which is a client-config problem, not a token problem. Success is also silent.

**What the client is actually requesting** — this is what caught the missing `/mcp` path:

```bash
ssh docker@192.168.72.129 'cd /docker/openbrain && sudo docker compose logs --since 20m mcp-gateway 2>&1 | grep -E "INFO:|\[auth\]" | tail -25'
```

**authentik request and flow logs** — it runs natively on CT 118, not in docker:

```bash
ssh streckercm@192.168.72.102 "sudo pct exec 118 -- journalctl -u authentik-server --since '20 min ago' --no-pager | grep -iE 'authorize|redirect|invalid'"
```

**authentik provider config, read directly** — faster and more reliable than reading it back out
of the admin UI:

```bash
ssh streckercm@192.168.72.102 "sudo pct exec 118 -- su postgres -c \"psql -d authentik -tAF'|' -c \\\"SELECT cp.name, af.slug, af.designation, p.issuer_mode, p.client_type, (p.signing_key_id IS NOT NULL) FROM authentik_providers_oauth2_oauth2provider p JOIN authentik_core_provider cp ON cp.id=p.provider_ptr_id LEFT JOIN authentik_flows_flow af ON af.flow_uuid=cp.authorization_flow_id\\\"\""
```

The redirect URI column is `_redirect_uris` (leading underscore), and scope mappings join through
`authentik_core_provider_property_mappings`.

**Testing the public hostname from inside the LAN.** Technitium is authoritative for
`streckercm.com` and returns NXDOMAIN for `openbrain-mcp`, which is the intended public-only
state. Bypass it rather than adding a local record:

```bash
curl --resolve openbrain-mcp.streckercm.com:443:104.21.74.64 https://openbrain-mcp.streckercm.com/.well-known/oauth-protected-resource
```

That genuinely leaves the network and returns through the tunnel, so it is a real public-path
test, not a LAN shortcut.

## Phase 5 — Verification

The first three are the ones that matter. An unauthenticated request must be rejected from
**every** direction, because enforcement is supposed to be path-independent.

| # | From | Command | Expect |
|---|---|---|---|
| 1 | Phone on cellular | `POST https://openbrain-mcp.streckercm.com/mcp` | 401 |
| 2 | Inside the LAN | same | 401 |
| 3 | Tailnet | same | 401 |
| 4 | Anywhere | same, with a valid token | 200, 19 tools |
| 5 | Anywhere | `https://openbrain-mcp.streckercm.com/api/bulk-delete` | 404 |
| 6 | Off-tailnet | the web UI hostname | unreachable |
| 7 | Anywhere | a long streaming MCP response | completes, no truncation |

**Test 2 passing while test 1 passes is not enough.** In April, Cloudflare Access appeared to work
because LAN traffic never reached Cloudflare at all. Test 2 is the one that would have caught it.

Then the browser clients:

- claude.ai → custom connector at `https://openbrain-mcp.streckercm.com/mcp`, Client ID and Secret
  from Authentik under Advanced settings
- ChatGPT → Settings → Apps → Advanced → Developer mode, same URL

Finally, at the Cloudflare edge:

- **Rate limiting** scoped to `/mcp`, threshold well above what one agent produces in a burst
- **WAF in log-only mode.** Do not set anything to block yet — MCP payloads are JSON-RPC carrying
  code and SQL fragments, which is exactly what managed rulesets match on. Review after a week and
  enable blocking only for rules with no false positives.

---

## Phase 5 status

Done, verified 2026-08-23:

| Check | Result |
|---|---|
| Unauthenticated `POST /mcp` from inside the LAN, resolving publicly | **401** |
| Valid token over the Cloudflare path | **200**, 19 tools |
| `/api/bulk-delete` over the public hostname | **404** |
| `GET /` over the public hostname | **404** |
| Both metadata path forms | **200** |
| claude.ai connector, full OAuth flow | **200/202**, zero `[auth]` rejections |
| Web UI create / edit / archive / search | unaffected |
| Row counts before and after Phase 1 | identical (117 / 31 / 8 / 210) |

Still outstanding:

- **From a phone on cellular** — the one path never exercised. Everything so far came from the LAN
  or from Anthropic's servers.
- **A long streaming MCP response through the tunnel** — confirm no truncation or idle timeout.
- **Cloudflare rate limiting** scoped to `/mcp`, threshold well above a single agent's burst.
- **Cloudflare WAF in log-only mode.** Do not set anything to block yet: MCP payloads are JSON-RPC
  carrying code and SQL fragments, which is exactly what managed rulesets match. Review after a
  week and enable blocking only for rules with no false positives.

## Known limits you are accepting

- `MCP_RESOURCE_URI` is `https://openbrain-mcp.streckercm.com/mcp`, but the Claude Code clients
  still connect through `brain.streckercm.com`. That works only because static tokens skip
  audience validation entirely. If those clients are ever moved to OAuth, they must use the
  `openbrain-mcp` hostname or their tokens will fail the audience check.
- Any valid token gets **all 19 tools**, full read and write. No per-tool scoping.
- A static token carries **both scopes and never expires**. Rotate by editing `MCP_STATIC_TOKENS`.
- `MCP_REQUIRED_SCOPES=""` disables scope enforcement entirely. The audience check still gates
  who can obtain a usable token.
- With Authentik down and the key cache expired, one request per 30s window waits up to 10s before
  succeeding with the cached key.
- **`/api/*` is unauthenticated**, but it is no longer published to the host at all — only
  containers on CT 115's own docker network can reach it. That is a network boundary rather than a
  proxy rule, which is stronger than what the spec originally described.
- The Cloudflare tunnel's ingress rules now live in the **dashboard**, not in git. That is the
  cost of reusing CT 126: a change to what is publicly exposed leaves no diff and no review trail.
  Worth a periodic look at the tunnel's public hostname list.

## Rollback at any point

```bash
cd /docker/openbrain
git checkout $(cat /root/openbrain-rollback-commit.txt)
cp /root/openbrain-rollback.env .env
docker compose up -d --build
# Database, only if needed:
# gunzip -c /root/openbrain-<stamp>.sql.gz | docker compose exec -T db psql -U openbrain openbrain
```
