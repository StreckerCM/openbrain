#!/usr/bin/env node
// SessionStart hook: pre-load recent OpenBrain memories as session context.
//
// Reads the openbrain MCP server config out of ~/.claude.json (so the URL and
// token live in exactly one place) and calls list_memories over Streamable
// HTTP. The server runs stateless, so a bare tools/call works with no
// initialize handshake.
//
// Every failure path exits 0 with no output. A session must never fail to
// start because the brain host is down.
//
// That silence used to be total, which made a broken hook indistinguishable
// from one that simply found nothing. So: errors are always appended to
// openbrain-preload.log next to this file, and OPENBRAIN_HOOK_DEBUG=1 (or
// --debug) additionally traces every decision to stderr. Neither changes the
// exit code. Note the log can only report failures the script itself reaches —
// if the interpreter never starts, the harness debug log is the only witness.

import { readFileSync, appendFileSync, statSync, renameSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, basename } from 'node:path'

const PROJECT_LIMIT = 12
const RECENT_LIMIT = 8
const PREVIEW_CHARS = 240
const TIMEOUT_MS = 8000
const LOG_MAX_BYTES = 256 * 1024

const BACKSLASH = String.fromCharCode(92)
const NL = String.fromCharCode(10)

const DEBUG =
  process.env.OPENBRAIN_HOOK_DEBUG === '1' || process.argv.includes('--debug')

const LOG_PATH = join(homedir(), '.claude', 'hooks', 'openbrain-preload.log')

// Best-effort throughout: logging must never be the thing that breaks a
// session start, so every failure here is swallowed.
const note = (level, msg) => {
  if (DEBUG) process.stderr.write('[openbrain-preload] ' + level + ': ' + msg + NL)
  if (level === 'debug' && !DEBUG) return
  try {
    try {
      if (statSync(LOG_PATH).size > LOG_MAX_BYTES) {
        renameSync(LOG_PATH, LOG_PATH + '.1')
      }
    } catch {}
    appendFileSync(
      LOG_PATH,
      new Date().toISOString() + ' ' + level + ' ' + msg + NL,
    )
  } catch {}
}

// A skip is normal (server off for this directory, nothing stored yet); an
// error is not. Only the latter reaches the log unless debugging.
const bail = (reason) => {
  note('debug', 'skip: ' + reason)
  process.exit(0)
}

const readStdin = async () => {
  const chunks = []
  for await (const c of process.stdin) chunks.push(c)
  return Buffer.concat(chunks).toString('utf8')
}

// cwd keys in .claude.json use forward slashes; hook input uses backslashes.
const norm = (p) =>
  String(p || '')
    .split(BACKSLASH)
    .join('/')
    .replace(/\/+$/, '')
    .toLowerCase()

// "Football-Pick-Em" and "football_pick_em" should match the same repo.
const slug = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '')

const call = async (cfg, name, args) => {
  const res = await fetch(cfg.url, {
    method: 'POST',
    headers: {
      ...(cfg.headers || {}),
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: { name, arguments: args },
    }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  })
  if (!res.ok) throw new Error('http ' + res.status)
  const body = await res.text()

  // Response uses SSE framing even for a single reply: "event: ...\ndata: {...}"
  const line = body.split('\n').find((l) => l.startsWith('data: '))
  const env = JSON.parse(line ? line.slice(6) : body)
  if (env.error) throw new Error(env.error.message || 'rpc error')

  const text = env.result?.content?.[0]?.text
  const rows = text ? JSON.parse(text) : []
  return Array.isArray(rows) ? rows : []
}

const render = (rows) =>
  rows
    .map((m) => {
      const head = '- [' + (m.memory_type || '?') + '] ' + m.name
      const desc = m.description ? ' — ' + m.description : ''
      const raw = (m.content || '').replace(/\s+/g, ' ').trim()
      const clipped =
        raw.length > PREVIEW_CHARS ? raw.slice(0, PREVIEW_CHARS) + '…' : raw
      return head + desc + (raw ? '\n  ' + clipped : '')
    })
    .join('\n')

try {
  const input = JSON.parse((await readStdin()) || '{}')
  const cwd = input.cwd || process.cwd()

  const cfg = JSON.parse(readFileSync(join(homedir(), '.claude.json'), 'utf8'))

  note('debug', 'cwd=' + cwd)

  const server = cfg.mcpServers?.openbrain
  if (!server?.url) bail('no openbrain server url in ~/.claude.json')

  // Honour the per-project toggle: if openbrain is switched off for this
  // directory, stay silent, exactly as the CLAUDE.md guard clause does.
  const target = norm(cwd)
  const entry = Object.entries(cfg.projects || {}).find(
    ([k]) => norm(k) === target,
  )
  if ((entry?.[1]?.disabledMcpServers || []).includes('openbrain')) {
    bail('openbrain disabled for this directory')
  }

  const projects = await call(server, 'list_projects', {})
  const here = slug(basename(target))
  const match = projects.find((p) => slug(p.name) === here)
  note(
    'debug',
    projects.length +
      ' projects; "' +
      here +
      '" ' +
      (match ? 'matched ' + match.name : 'matched nothing'),
  )

  const sections = []
  const shown = new Set()

  if (match) {
    const rows = await call(server, 'list_memories', {
      project: match.name,
      limit: PROJECT_LIMIT,
    })
    if (rows.length) {
      rows.forEach((m) => shown.add(m.id))
      sections.push('### Project: ' + match.name + '\n\n' + render(rows))
    }
  }

  const recent = await call(server, 'list_memories', { limit: RECENT_LIMIT })
  const fresh = recent.filter((m) => !shown.has(m.id))
  if (fresh.length) {
    sections.push(
      '### Most recently updated (all projects)\n\n' + render(fresh),
    )
  }

  if (!sections.length) bail('no memories to show')

  const header = match
    ? 'This directory maps to the **' + match.name + '** project.'
    : 'This directory does not map to a known OpenBrain project, so only recent cross-project memories are shown.'

  const context = [
    '## OpenBrain (pre-loaded)',
    '',
    header,
    'These are previews, not full records — call `mcp__openbrain__recall_memory`',
    'or `mcp__openbrain__search_knowledge` for full text, or for anything not listed here.',
    'They reflect what was true when written; verify any file, host, or flag they name.',
    '',
    ...sections,
  ].join('\n')

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext: context,
      },
    }),
  )
  note('debug', 'emitted ' + context.length + ' chars')
} catch (err) {
  // fetch() reports a bare "TypeError: fetch failed" and hides the useful part
  // (ECONNREFUSED, DNS failure, cert error) one level down in .cause.
  const cause = err?.cause ? ' | cause: ' + (err.cause.message || err.cause) : ''
  note('error', ((err && err.stack) || String(err)) + cause)
  process.exit(0)
}
