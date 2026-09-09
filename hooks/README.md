# Session hooks

`openbrain-preload.mjs` is a Claude Code **SessionStart** hook. It maps the session's working
directory to an OpenBrain project and pre-loads that project's recent memories into the context, so
recall does not depend on the model remembering to ask. See `docs/handoff.md` for the design and
for how it fits alongside the `~/.claude/CLAUDE.md` policy.

**This copy is the source of truth.** Claude Code reads hooks from `~/.claude/`, not from a repo, so
the file has to be installed. Copy it after every change here:

```bash
cp hooks/openbrain-preload.mjs ~/.claude/hooks/openbrain-preload.mjs
```

Register it once in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [
          {
            "type": "command",
            "command": "C:/nvm4w/nodejs/node.exe C:/Users/streckercm/.claude/hooks/openbrain-preload.mjs",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

**Use forward slashes in `command`, on Windows too.** Claude Code runs hook commands through bash,
which eats backslashes in a Windows path and turns `node.exe` into a command that does not exist.
That bug silently disabled this hook for a day; `docs/handoff.md` has the full account.

The hook needs an `openbrain` entry under `mcpServers` in `~/.claude.json` — it reads the URL and
bearer token from there at runtime, so no credential is stored in this repo. It honours the
per-directory `disabledMcpServers` opt-out, and exits 0 on every failure path so a session can
never fail to start because the brain host is down.

Debugging:

```bash
tail ~/.claude/hooks/openbrain-preload.log          # errors, always recorded
OPENBRAIN_HOOK_DEBUG=1 node hooks/openbrain-preload.mjs < payload.json   # full trace
```
