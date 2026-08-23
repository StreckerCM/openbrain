# Deploy sheet — remote MCP with OAuth, CT 115

**Target:** CT 115, `/docker/openbrain`, reachable as `docker.lan.streckercm.com` (192.168.72.129)
**Source:** branch `development` (`da90e51`) — `main` is deliberately left at `8f45494` as the revert point
**Written:** 2026-08-23

This replaces the runbook at the end of `docs/superpowers/plans/2026-08-22-remote-mcp-oauth.md`,
which assumed everything lands in one cutover. It does not have to, and it should not: the
refactor and the authentication are independent risks and separating them tells you which one
broke something.

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

**Check the working tree before pulling.** It should be clean as of `243c606`. If
`git status` shows local edits to `docker-compose.yml`, stop and investigate — this deploy
changes that file, and something re-diverged.

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

`cloudflared` is not started yet, so leave `CLOUDFLARED_CREDENTIALS_FILE` unset — but note the
compose file marks it required (`:?`), which means **`docker compose` commands will fail until it
is set**. Either set it to a placeholder path now, or add `--scale cloudflared=0`. Simplest:

```bash
echo 'CLOUDFLARED_CREDENTIALS_FILE=/root/cloudflared-placeholder.json' >> .env
touch /root/cloudflared-placeholder.json
```

Then:

```bash
docker compose build mcp-gateway
docker compose up -d
docker compose ps
docker compose logs --tail=30 mcp-gateway
```

Expect the gateway log to say `MCP listener on :3001, private API listener on :3002`.

**Verify Phase 1.** Two new published ports exist: 3007 → MCP, 3011 → the private write API.

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3007/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'          # 200 — auth is off
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:3007/api/bulk-delete
                                                                # 404 — the catch-all is gone
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3010/ # 200 — web UI
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
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3007/mcp   # 401
curl -s -i -X POST http://127.0.0.1:3007/mcp | grep -i www-authenticate      # challenge present
curl -s http://127.0.0.1:3007/.well-known/oauth-protected-resource           # metadata, no auth
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3007/mcp \
  -H "Authorization: Bearer obk_YOUR_TOKEN_HERE" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'                        # 200
```

Confirm your Claude Code clients still work. **Stop here and fix before continuing if they do
not** — everything after this assumes a working authenticated path.

---

## Phase 3 — Authentik provider

In the Authentik admin UI, create an **OAuth2/OpenID Provider** named `OpenBrain MCP`:

| Setting | Value |
|---|---|
| Client type | Confidential |
| Redirect URIs | The callback URLs claude.ai and ChatGPT show during connector setup |
| Signing key | An RS256 certificate |
| Scopes | `openid`, `profile`, `email`, plus custom `openbrain:read` and `openbrain:write` |
| Subject mode | Based on user ID |

Create an **Application** with slug `openbrain-mcp` — this fixes the discovery URLs used above.
**Bind a policy restricting it to your account or a dedicated group.** Without one, every
Authentik user can mint a working token.

Verify discovery resolves:

```bash
curl -s https://auth.streckercm.com/application/o/openbrain-mcp/.well-known/openid-configuration \
  | head -c 400
```

**The check that gates everything else.** Obtain a real token and decode it:

```bash
echo '<paste JWT>' | cut -d. -f2 | base64 -d 2>/dev/null | python3 -m json.tool
```

`aud` **must** contain `https://openbrain-mcp.streckercm.com/mcp`. Authentik's handling of the
`resource` parameter is unverified for 2026.5.2 — if the audience is wrong, add a scope mapping
that injects it. The gateway rejects any token not minted for it, so nothing works until this is
right.

---

## Phase 4 — Public ingress

```bash
cloudflared tunnel login
cloudflared tunnel create openbrain-mcp
```

Put the credentials JSON somewhere outside the repo, point `CLOUDFLARED_CREDENTIALS_FILE` at it,
and replace `TUNNEL_ID` in `cloudflared/config.yml` with the real tunnel ID. Add the DNS record
for `openbrain-mcp.streckercm.com` pointing at the tunnel.

**Do not add this hostname to local DNS yet.** Keep it public-only until the off-network checks in
Phase 5 pass, so you are testing the path you think you are testing.

```bash
docker compose up -d cloudflared
docker compose logs --tail=20 cloudflared
```

---

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

## Known limits you are accepting

- Any valid token gets **all 19 tools**, full read and write. No per-tool scoping.
- A static token carries **both scopes and never expires**. Rotate by editing `MCP_STATIC_TOKENS`.
- `MCP_REQUIRED_SCOPES=""` disables scope enforcement entirely. The audience check still gates
  who can obtain a usable token.
- With Authentik down and the key cache expired, one request per 30s window waits up to 10s before
  succeeding with the cached key.
- **`/api/*` on port 3011 is unauthenticated** and reachable by any container on the Docker
  network. The cloudflared ingress rules are what keep it off the internet — that is a rule, not a
  network boundary. Re-check this against CT 115's actual compose file, which is known to diverge
  from the repo on networking.

## Rollback at any point

```bash
cd /docker/openbrain
git checkout $(cat /root/openbrain-rollback-commit.txt)
cp /root/openbrain-rollback.env .env
docker compose up -d --build
# Database, only if needed:
# gunzip -c /root/openbrain-<stamp>.sql.gz | docker compose exec -T db psql -U openbrain openbrain
```
