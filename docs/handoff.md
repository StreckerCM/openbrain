# OpenBrain handoff

**Updated:** 2026-08-30

Read this first if you're picking up OpenBrain work. It covers what's deployed, what's open, and
the things that cost time to discover.

## Current state

The MCP endpoint is reachable from the public internet with OAuth 2.1 authentication. The web UI,
PostgREST, Adminer, and the write REST API stay on the LAN. The claude.ai connector works.

| Item | State |
|---|---|
| Open PR | **#22** — `development` → `main`, 43 commits, 91 tests |
| `main` | Held back deliberately as the revert point |
| `development` | Current work; CT 115 runs from this branch |
| CT 115 deployed at | `c166360` — **up to date with `development`** as of 2026-08-30 |

Deploy phases 0 through 4 are done. Phase 5 has one item left: **send a request from a phone on
cellular.** Every test so far ran from the LAN or from Anthropic's servers, so that path is
unproven.

**Shipped 2026-08-30 (PR #23):** `update_memory`, bringing the tool count to **20**. Memories
previously could only be corrected by archive-and-re-save, which loses the ID and so breaks
`[[name]]` references and project links. The same PR moved the `VALID_MEMORY_TYPES` guard into
`_db_update_memory` — it was enforced on insert but not on update, so `PUT /api/memories/{id}`
could already write a type outside the vocabulary, and such a row silently drops out of
`recall_memory`'s `memory_type` filter and the web UI facets. Verified live against
`brain.streckercm.com`: guard rejects, `memory_id` alias works, partial update leaves untouched
fields alone, and re-embedding regenerates (the edited memory returns at 0.61 similarity for a
query phrased against its new wording).

## How to reach things

```bash
ssh docker@192.168.72.129        # CT 115, the docker host
```

Passwordless `sudo` works. The repo at `/docker/openbrain` is root-owned and `.env` is `0600 root`,
so `git` and `docker compose` both need `sudo` — compose can't read `.env` otherwise. The docker
socket is group-accessible, so bare `docker` commands don't.

Reach the other containers through the Proxmox host:

```bash
ssh streckercm@192.168.72.102    # pve
sudo pct exec <id> -- <command>  # passwordless for pct, not for other commands
```

| ID | Host | Notes |
|---|---|---|
| 115 | docker | OpenBrain, 35 containers, `docker.lan.streckercm.com` |
| 118 | authentik | Runs natively, not in docker. Use `journalctl -u authentik-server`. |
| 126 | cloudflared | Dashboard-managed tunnel, `--token`, no local config file |
| 127 | npmplus | Reverse proxy, on the tailnet at `100.64.0.5` |

CT 115 is **not** on the tailnet. NPMplus already is. `PRIVATE_BIND` is currently the LAN address
`192.168.72.129`; moving it to a tailnet address would close PostgREST and Adminer to the LAN, but
first confirm nothing queries PostgREST directly — `docs/readme.md` advertises it as a second agent
interface.

## UNVERIFIED — test this first

**A SessionStart hook was wired up on 2026-08-30 but has never run in a real session.** It was
tested only by piping hook-shaped JSON into it by hand. Confirming it fires for real is the first
thing to do in the next session, and it needs a *fresh* session — the hook runs at session start,
so the session that created it could not exercise it.

**How to test.** Close this session. Open a new one in `E:\GitHub\openbrain`. Near the top of the
context you should see a block headed `## OpenBrain (pre-loaded)` saying the directory maps to the
**OpenBrain** project, followed by that project's memories and a list of recent cross-project ones.
Ask something like "what do you already know about this project?" — the answer should draw on those
memories without any tool call.

Then check the negative case: open a session in `E:\GitHub\APEX-TestUtility` (or any repo in the
Salesforce list below) and confirm **no** OpenBrain block appears and no `mcp__openbrain__*` tools
are present.

If nothing appears in either case, run the hook by hand to see the error — it is written to exit 0
silently on every failure path, which is right for a session start but hides problems:

```bash
node -e 'const os=require("os"),p=require("path"),fs=require("fs");const B=String.fromCharCode(92);
const f=p.join(os.tmpdir(),"ob.json");
fs.writeFileSync(f,JSON.stringify({cwd:"E:"+B+"GitHub"+B+"openbrain",source:"startup"}));console.log(f)'
# then feed that file to the hook on stdin:
node ~/.claude/hooks/openbrain-preload.mjs < <that path>
```

Note the escaping trap: building that JSON with `echo` or `printf` in Git Bash eats the backslashes
and produces an invalid `cwd`, which looks exactly like a hook bug. Build it with `JSON.stringify`
and `String.fromCharCode(92)` as above.

## How agents reach OpenBrain unprompted

Two layers, so recall does not depend on the model remembering to ask.

**`~/.claude/CLAUDE.md`** — a user-level policy loaded into every session. It opens with a guard
clause: the section applies only when `mcp__openbrain__*` tools are present, so it is inert
wherever the server is off. It says when to recall (questions turning on recorded history rather
than readable code), when to save (decisions with rationale, hard-won constraints, carry-forward
state), and where the boundary sits against the per-project file memory under
`~/.claude/projects/*/memory/` — local is *how to work with me*, OpenBrain is *the work itself*.

**`~/.claude/hooks/openbrain-preload.mjs`** — a SessionStart hook registered in
`~/.claude/settings.json` under matcher `startup|resume|clear`, timeout 15s. It maps the cwd
basename to a project by slug, pulls that project's 12 most recent memories plus the 8 most recent
overall, and returns them as `hookSpecificOutput.additionalContext`. Previews cap at 240 characters
with a pointer to `recall_memory` / `search_knowledge` for full text.

CLAUDE.md is an instruction the model may skip when it feels confident; the hook is deterministic.
So recall is automatic and saving stays model-discretion, since only the model knows what is worth
keeping.

Three implementation facts worth not rediscovering:

- The gateway's Streamable HTTP transport is **stateless** — it returns no `mcp-session-id`, so a
  bare `tools/call` works with no `initialize` handshake. The hook depends on this. Replies still
  use SSE framing (`event: message` / `data: {...}`) even for a single response.
- The hook reads the URL and bearer token out of `~/.claude.json` at runtime rather than embedding
  them, so there is one copy of the token.
- Only `/mcp` is reachable from outside. `brain.streckercm.com` serves the SPA as a catch-all, so
  probing `/rest/memories` returns 200 with web-UI HTML — that is **not** evidence PostgREST is
  exposed.

**Per-project gating.** Claude Code records per-directory opt-outs at
`projects["<path>"].disabledMcpServers` in `~/.claude.json` (path keys use forward slashes). The
hook reads that same key, so both layers agree. As of 2026-08-30 `openbrain` is disabled in nine
Salesforce repos: `APEX-Documentation-Project-FY2026`, `APEX-TestUtility`, `Chris_Dev_Org`,
`Custom_Product_Configuation_LWC`, `SF-Deployment-GUID`, `Salesforce_to_SharePoint_Integration`,
`hipoint_App_Integration`, `Salesforce-Sandbox-Utility`, `Salesforce-to-SQL-Server-Interface`.
`MilestoneWidget` is an SFDX repo but was deliberately left enabled, because it is an OpenBrain
project with its own memories.

Do not confuse `disabledMcpServers` with `disabledMcpjsonServers` — the latter is the separate
approval list for servers defined in a repo's `.mcp.json`.

An `OpenBrain` project was created in the store on 2026-08-30 (id 120); before that this repo
matched nothing and got only the cross-project fallback. Repos whose directory name differs from
their project name still get only the fallback until a mapping table is added.

## Two front doors

Both terminate at the same gateway, the same 20 tools, and the same database.

| Client | Endpoint | Credential |
|---|---|---|
| claude.ai, and any claude.ai surface | `openbrain-mcp.streckercm.com/mcp` | OAuth via Authentik |
| Claude Code on your machines | `brain.streckercm.com/mcp` | Static bearer token |

The static token lives in `MCP_STATIC_TOKENS` on CT 115. Read it with:

```bash
ssh docker@192.168.72.129 'sudo grep ^MCP_STATIC_TOKENS= /docker/openbrain/.env'
```

It's comma-separated, so you can add a new token before removing the old one and rotate without
downtime.

`MCP_RESOURCE_URI` names the `openbrain-mcp` hostname, but Claude Code connects through
`brain.streckercm.com`. That works only because static tokens skip audience validation. If you move
those clients to OAuth, they must use the `openbrain-mcp` hostname or their tokens fail the
audience check.

## Debugging

Auth decisions:

```bash
ssh docker@192.168.72.129 'cd /docker/openbrain && sudo docker compose logs mcp-gateway 2>&1 | grep -F "[auth]" | tail -20'
```

Use `grep -F` and avoid a short `--tail`. Only four paths log: JWKS fetch failure, malformed token,
missing `kid`, and JWT decode failure — where audience, issuer, expiry, and signature rejections all
surface.

**A request with no `Authorization` header logs nothing.** A 401 with no `[auth]` line means the
client never sent a credential, which is a client-config problem rather than a token problem.
Success is silent too.

To see what a client is actually requesting, drop the filter:

```bash
ssh docker@192.168.72.129 'cd /docker/openbrain && sudo docker compose logs --since 20m mcp-gateway 2>&1 | grep -E "INFO:|\[auth\]" | tail -25'
```

That's what caught a connector URL missing its `/mcp` path.

Read Authentik's provider config straight from its database rather than the admin UI:

```bash
ssh streckercm@192.168.72.102 "sudo pct exec 118 -- su postgres -c \"psql -d authentik -c 'SELECT * FROM authentik_providers_oauth2_oauth2provider'\""
```

The redirect URI column is `_redirect_uris` with a leading underscore.

Testing the public hostname from inside the LAN needs `--resolve`, because Technitium is
authoritative for `streckercm.com` and returns NXDOMAIN for `openbrain-mcp` by design:

```bash
curl --resolve openbrain-mcp.streckercm.com:443:104.21.74.64 https://openbrain-mcp.streckercm.com/.well-known/oauth-protected-resource
```

## Things that cost time

Each of these looked like a different problem than it was.

**Build `web-ui` as well as `mcp-gateway`.** The web UI image bakes in `nginx.conf`, and
`docker compose up -d` reuses an existing image. Build only the gateway and reads keep working while
every write returns 404 — which reads like an application bug, not a stale image.

**The audience claim rides on `openbrain:read`.** Authentik evaluates a scope mapping only when the
client requests that scope, and the gateway's metadata advertises only `openbrain:read` and
`openbrain:write`. A dedicated `openbrain:aud` scope never fires, so `aud` goes missing and every
token gets a bare 401.

**The connector URL needs the `/mcp` path.** Both `.well-known` documents live at the host root, so
OAuth discovery and consent complete before claude.ai posts to `/` and gets a 404. The error says
"couldn't connect to the server," which sounds like networking or auth.

**The gateway serves authorization-server metadata itself.** claude.ai probes
`/.well-known/oauth-authorization-server` on the resource server's origin instead of following
`authorization_servers`, and Authentik 2026.5 serves only the OIDC-style path. Without that shim the
flow dead-ends at a URL that doesn't exist. See PR #21.

**Cancelling an Authentik consent prompt logs you out** and drops you at `/if/user/#/library`, which
looks like a redirect-URI misconfiguration.

**`sudo` does not cover the shell around the command.** `sudo ls -lh /root/openbrain-*.sql.gz`
reports `No such file or directory` **even when the files exist** — the shell expands the glob as
`docker`, cannot read `/root`, and passes the literal pattern through. Likewise `sudo wc -l <
/root/file` fails on the redirect. Mid-deploy this reads as "the backup was never written," which
is exactly when you might re-run a dump or press on without a rollback artifact. Use
`sudo ls -lh /root/ | grep openbrain`, or wrap the whole thing in `sudo sh -c "... > /root/out"`.
Piping into `sudo tee` works, because `tee` is the elevated command.

**Shell scripts need LF endings.** `.gitattributes` enforces this. Without it, `core.autocrlf` gives
`entrypoint.sh` a CRLF shebang and the container dies with `exec /entrypoint.sh: no such file or
directory`. Only Windows clones hit this.

## Follow-on work

Items C, D, and E in `docs/superpowers/plans/2026-08-22-remote-mcp-oauth.md` are open. Item C is the
substantive one.

**C. Move the MCP tools onto `db.py`.** The 20 MCP tools contain 32 raw SQL sites reimplementing
logic the REST handlers already call through the shared layer, which has 6. Link, unlink, archive,
unarchive, project create and update, and search each exist twice. An audit on 2026-08-22 found no
behavioural drift between the copies, but search is where drift would hurt most — a divergence there
returns worse results rather than an error.

Write characterization tests against current behaviour before each move. Take the groups in this
order: link and unlink, then archive and unarchive, then project operations, then search. Decide
deliberately whether the search implementations converge on limit, `include_archived`, and the
filter set, rather than picking one side by accident.

**D. Split `tools.py` and `rest.py`.** Optional and cosmetic once C is done.

**E. Decide how `save_memory` handles duplicates.** It's a plain `INSERT` and `memories.name` has no
unique constraint, so saving twice with the same name creates two rows. Duplicates dilute search
results.

Two smaller items:

- **`requirements.txt` pins `mcp[http]==1.27.0`, and that extra doesn't exist.** Pip warns and
  ignores it. Harmless, but a pin claiming a dependency that isn't real is misleading in a file whose
  purpose is precision.
- **`extract_scopes` raises `TypeError` on malformed claims**, surfacing as a 500. Only reachable
  after signature verification, so Authentik is the only party who can shape them.

## Limits to keep in mind

- Any valid token gets **all 20 tools**, full read and write. There's no per-tool scoping.
- Static tokens carry both scopes and never expire.
- Setting `MCP_REQUIRED_SCOPES=""` disables scope enforcement. The audience check still gates who
  can obtain a usable token.
- `/api/*` is unauthenticated. It's private because container port 3002 isn't published, not because
  a rule blocks it.
- Prompt injection reaches further now that browser agents can write to the shared store. Content one
  agent ingests from a web page can become instructions another agent reads later.
- The Cloudflare WAF is a paid add-on and isn't enabled. See the deploy sheet for why that costs
  little here.

## Where things are written down

| Document | Contents |
|---|---|
| `docs/deploy-remote-mcp-ct115.md` | The operational record. Five phases, corrections marked where they broke, diagnostics, traffic baseline, Cloudflare decisions. |
| `docs/superpowers/specs/2026-08-22-remote-mcp-oauth-design.md` | Design decisions and their reasoning, including two corrections made after review found the original claims false. |
| `docs/superpowers/plans/2026-08-22-remote-mcp-oauth.md` | The implementation plan and the follow-on checklist. |
| `docs/readme.md` | Agent-facing reference. Partly stale — it predates authentication. |
