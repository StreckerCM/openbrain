# OpenBrain handoff

**Updated:** 2026-09-08

Read this first if you're picking up OpenBrain work. It covers what's deployed, what's open, and
the things that cost time to discover.

## Current state

The MCP endpoint is reachable from the public internet with OAuth 2.1 authentication. The web UI,
PostgREST, Adminer, and the write REST API stay on the LAN. The static-token front door
(`brain.streckercm.com`) is healthy. **The claude.ai connector is currently broken — see the next
section.**

| Item | State |
|---|---|
| Open PR | **#22** — `development` → `main`, 46 commits, 91 tests, mergeable. The only open PR. |
| `main` | Held back deliberately as the revert point |
| `development` | Current work; CT 115 runs from this branch |
| CT 115 deployed at | `c166360` — two commits behind `development` (`8172df5`) as of 2026-09-08. Both are docs and hook files; nothing deployable changed, so no redeploy is owed. |

Verified live 2026-09-08: `brain.streckercm.com/mcp` answers `tools/list` with all **20** tools, and
the nine per-repo opt-outs below are still in place.

Deploy phases 0 through 4 are done. Phase 5 has one item left: **send a request from a phone on
cellular.** It needs a phone with wifi off and cannot be run from this machine, so it stays open.
Run it against `brain.streckercm.com` with the static token; testing `openbrain-mcp` is pointless
until the Access problem above is cleared, since that hostname 302s every request regardless of
network.

**Shipped 2026-08-30 (PR #23):** `update_memory`, bringing the tool count to **20**. Memories
previously could only be corrected by archive-and-re-save, which loses the ID and so breaks
`[[name]]` references and project links. The same PR moved the `VALID_MEMORY_TYPES` guard into
`_db_update_memory` — it was enforced on insert but not on update, so `PUT /api/memories/{id}`
could already write a type outside the vocabulary, and such a row silently drops out of
`recall_memory`'s `memory_type` filter and the web UI facets. Verified live against
`brain.streckercm.com`: guard rejects, `memory_id` alias works, partial update leaves untouched
fields alone, and re-embedding regenerates (the edited memory returns at 0.61 similarity for a
query phrased against its new wording).

## BROKEN — Cloudflare Access is in front of the OAuth hostname (found 2026-09-08)

`openbrain-mcp.streckercm.com` is intercepted by Cloudflare Access at the edge. Every path returns
a 302 to `streckercm.cloudflareaccess.com/cdn-cgi/access/login/...` before the gateway is reached,
so the claude.ai connector cannot work. Reproduce:

```bash
# both return 302 with an HTML Access login redirect
curl -s -o /dev/null -w "%{http_code}\n" https://openbrain-mcp.streckercm.com/.well-known/oauth-protected-resource
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://openbrain-mcp.streckercm.com/mcp \
  -H "Authorization: Bearer $STATIC_TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# same token, other front door, still 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://brain.streckercm.com/mcp \
  -H "Authorization: Bearer $STATIC_TOKEN" ... 
```

A valid static token gets 302 too, which is the tell: Access rejects at the edge without consulting
the gateway, so this is not a token, audience, or scope problem. Discovery is what breaks first —
an MCP client needs `/.well-known/oauth-protected-resource` to return JSON unauthenticated and
`/mcp` to return a 401 carrying `WWW-Authenticate`. It gets HTML redirects for both.

**This directly contradicts a decision the design records.** See
`docs/superpowers/specs/2026-08-22-remote-mcp-oauth-design.md` under "Enforcement must live in the
application": an earlier Access attempt "appeared to work and enforced nothing," because split DNS
meant LAN traffic never reached Cloudflare's edge. Token validation was deliberately moved into the
gateway so enforcement is path-independent. Access in front of this hostname is the configuration
that decision rejected.

**Fix is in the Cloudflare dashboard, not this repo.** The tunnel on CT 126 is dashboard-managed
with no local config file, so nothing here needs changing. Remove the Access application covering
`openbrain-mcp.streckercm.com`, or at minimum add a bypass policy for `/.well-known/*` and `/mcp`.
Removing it outright matches the design; the gateway already authenticates every request itself.

Two things this session could not establish: **when** it broke, and **whether** Access was added
deliberately. The claude.ai connector was recorded as working around 2026-08-30, so the change is
more recent than that.

Also note: local DNS now resolves `openbrain-mcp.streckercm.com` to the Cloudflare addresses
(104.21.74.64, 172.67.199.236). The `--resolve` workaround documented further down is no longer
needed from the LAN, and the split-DNS condition that defeated the original Access attempt may no
longer hold — worth re-checking before drawing conclusions from either.

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

## Session preload hook — working, confirmed 2026-09-03

The harness log records `Hook SessionStart:startup (SessionStart) success` on 2026-09-03, delivering
3722 characters of `additionalContext`, twice. Project matching was re-verified against this
directory on 2026-09-08 (`9 projects; "openbrain" matched OpenBrain`), and the hook's own error log
has never had an entry. Recall is automatic now; treat this as done.

One loose end, cosmetic. Both observed successes ran in a directory mapping to no OpenBrain project,
so they returned the cross-project fallback. A session in `E:\GitHub\openbrain` receiving the
*project-matched* block has been produced on demand but not yet caught arriving in a live session.
If you want to close it: open a fresh session here and look for a block headed
`## OpenBrain (pre-loaded)` naming the **OpenBrain** project.

A quiet skip is normal and looks like this in the harness log — the hook exiting 0 with no output,
which is what a Salesforce repo with `openbrain` disabled produces:

```
Hook output does not start with {, treating as plain text
```

### Why it was dead for four days

Worth keeping, because the failure mode generalises to any hook on Windows.

The hook was registered 2026-08-30 and failed on every session until 2026-08-31.
`~/.claude/settings.json` held

```json
"command": "C:\\nvm4w\\nodejs\\node.exe C:\\Users\\streckercm\\.claude\\hooks\\openbrain-preload.mjs"
```

That is correct JSON for a Windows path and still wrong, because Claude Code runs hook commands
**through bash**, which collapses `\n`, `\U`, `\c` and the rest before anything executes:

```
Hook SessionStart:startup (SessionStart) error:
/usr/bin/bash: line 1: C:nvm4wnodejsnode.exe: command not found
```

**Fix: forward slashes.** Windows accepts them, bash leaves them alone.

```json
"command": "C:/nvm4w/nodejs/node.exe C:/Users/streckercm/.claude/hooks/openbrain-preload.mjs"
```

The script itself was never at fault — only the invocation. Because the hook exits 0 on every
failure path it reported nothing, so the symptom was an absent context block and no error anywhere.

**Where to look when it fails.** The hook exits 0 on every failure path — a session must never fail
to start because the brain host is down — but as of 2026-08-31 it is no longer silent about it.

Errors are always appended to `~/.claude/hooks/openbrain-preload.log`, with the `cause` unwrapped so
a network failure names the real reason instead of a bare `TypeError: fetch failed`. Routine skips
(server disabled for this directory, nothing stored yet) are not logged, so a non-empty log means
something is genuinely wrong. It rotates to `.log.1` past 256 KB.

For a full trace set `OPENBRAIN_HOOK_DEBUG=1` or pass `--debug`; every decision then goes to stderr
*and* the log — resolved cwd, project count, which project matched, and the emitted payload size.

```bash
tail ~/.claude/hooks/openbrain-preload.log
OPENBRAIN_HOOK_DEBUG=1 node ~/.claude/hooks/openbrain-preload.mjs < <payload file>
```

**That log cannot see a failure that happens before node starts** — exactly the class the 2026-08-30
bug belonged to. For those the harness log is the only witness:

```bash
# under happier-dev:
ls -t ~/.happier/cli/logs/subprocess/claude/*.log | head -1 | xargs grep -i hook
# plain terminal: run `claude --debug` and watch for "Hook SessionStart:startup"
```

`--setting-sources=user,project,local` is passed by happier-dev, so user-level hooks load normally
there. The harness is not a suspect; check the log before theorising about one.

To run the hook by hand, build the payload with `JSON.stringify` — `echo` and `printf` in Git Bash
eat the backslashes and produce an invalid `cwd`, which looks exactly like a hook bug:

```bash
node -e 'const os=require("os"),p=require("path"),fs=require("fs");const B=String.fromCharCode(92);
const f=p.join(os.tmpdir(),"ob.json");
fs.writeFileSync(f,JSON.stringify({cwd:"E:"+B+"GitHub"+B+"openbrain",source:"startup"}));console.log(f)'
# then feed that file to the hook on stdin:
node ~/.claude/hooks/openbrain-preload.mjs < <that path>
```

## How agents reach OpenBrain unprompted

Two layers, so recall does not depend on the model remembering to ask.

**`~/.claude/CLAUDE.md`** — a user-level policy loaded into every session. It opens with a guard
clause: the section applies only when `mcp__openbrain__*` tools are present, so it is inert
wherever the server is off. It says when to recall (questions turning on recorded history rather
than readable code), when to save (decisions with rationale, hard-won constraints, carry-forward
state), and where the boundary sits against the per-project file memory under
`~/.claude/projects/*/memory/` — local is *how to work with me*, OpenBrain is *the work itself*.

The hook is vendored at `hooks/openbrain-preload.mjs` in this repo as of 2026-08-31 — that copy is
the source of truth, and `hooks/README.md` covers installing and registering it. Claude Code only
reads `~/.claude/`, so edits there must be copied back, and vice versa.

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
hook reads that same key, so both layers agree. As of 2026-09-08 `openbrain` is disabled in nine
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

**A hook `command` with Windows backslashes never runs.** Claude Code executes hook commands
through bash, which collapses `\n` and `\U` in a path like
`C:\\nvm4w\\nodejs\\node.exe` down to `C:nvm4wnodejsnode.exe`. The JSON is valid, the hook
registers, the matcher fires — and the command is not found. A SessionStart hook that exits 0 on
failure then reports nothing, so it reads as "the harness does not run hooks" rather than "the path
is mangled." Use forward slashes in `settings.json`; Windows accepts them.

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

**C. Move the MCP tools onto `db.py`.** Still fully open; re-counted 2026-09-08. The 20 MCP tools contain 32 raw SQL sites reimplementing
logic the REST handlers already call through the shared layer, which has 6. Link, unlink, archive,
unarchive, project create and update, and search each exist twice. An audit on 2026-08-22 found no
behavioural drift between the copies, but search is where drift would hurt most — a divergence there
returns worse results rather than an error.

Write characterization tests against current behaviour before each move. Take the groups in this
order: link and unlink, then archive and unarchive, then project operations, then search. Decide
deliberately whether the search implementations converge on limit, `include_archived`, and the
filter set, rather than picking one side by accident.

**D. Split `server.py`.** Optional and cosmetic once C is done. Earlier versions of this handoff
called the targets `tools.py` and `rest.py`; no such files exist and none ever did. Everything is in
`mcp-gateway/server.py` (1648 lines): the 20 `@mcp.tool` functions first, then the 18 Starlette REST
routes from line 1038. `db.py` (579 lines) holds the 14 shared `_db_*` helpers both halves call.

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
