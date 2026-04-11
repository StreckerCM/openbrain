# OpenBrain Web UI Dashboard — Design Spec

**Date:** 2026-04-11
**Status:** Approved
**Supersedes:** `2026-04-08-web-ui-design.md` (pre-lifecycle-refactor)

## Overview

A full CRUD management dashboard for the OpenBrain knowledge base, served as a static SPA from an Nginx container. Provides browse, search, create, edit, archive, unarchive, link management, and hard delete capabilities for knowledge entries, memories, and projects.

**Primary audience:** Single power user (repo owner), but presentable enough for anyone deploying from the public repo.

## Architecture

### Containers (4 required)

| Container | Image | Internal Port | External Port | Purpose |
|-----------|-------|--------------|---------------|---------|
| `db` | pgvector/pgvector:pg17 | 5432 | 5433 | PostgreSQL + pgvector |
| `mcp-gateway` | ./mcp-gateway | 3001 | 3007 | MCP tools + REST write API + search API |
| `web-ui` | nginx:alpine | 80 | 3010 | Static SPA + reverse proxy |
| `embedder` | ./embedder | — | — | Background embedding service |

PostgREST (3006) and adminer (3008) remain as optional dev/debug services but are not required by the UI.

The `search-service` container from the original web-ui spec is eliminated — search is folded into mcp-gateway.

### Nginx Routing (web-ui container)

```nginx
location /                    → static SPA (index.html fallback for SPA routing)
location /api/read/           → proxy_pass http://postgrest:3000/    (reads)
location /api/write/          → proxy_pass http://mcp-gateway:3001/api/   (writes)
location /api/search          → proxy_pass http://mcp-gateway:3001/api/search   (search)
```

The `/api/read/` and `/api/write/` split creates a security boundary at the Nginx layer. PostgREST runs with an anonymous read-only role (SELECT only), so write attempts against `/api/read/` are rejected by PostgREST itself.

## Frontend Stack

### Technology

- **Preact 10** (~4KB) — React-compatible component library
- **HTM** (~1KB) — tagged template literals for JSX-like syntax, no build step required
- **Preact Hooks** (~2KB) — component-level state and effects
- **Preact Signals** (~1KB) — lightweight reactive global state
- **marked.js** (~12KB) — markdown rendering

All vendor libraries served as local files from `web-ui/static/vendor/` (no CDN dependency). Total vendor weight: ~20KB gzipped. The dashboard works fully offline from the local network.

No build step. All JS uses ES modules loaded natively by the browser.

### Theme

Dark only. Color palette based on GitHub's dark theme:
- Background: `#0d1117`
- Surface: `#161b22`
- Border: `#2a2a3a`
- Text primary: `#e2e8f0`
- Text secondary: `#94a3b8`
- Accent: `#4f46e5` (indigo)
- Success: `#22c55e`
- Warning: `#f59e0b`
- Danger: `#ef4444`

### File Structure

```
web-ui/
├── Dockerfile
├── nginx.conf
└── static/
    ├── index.html                  # SPA shell (sidebar container, content container)
    ├── css/
    │   └── style.css               # Dark theme, responsive, all components
    ├── js/
    │   ├── app.js                  # Router, app shell, sidebar logic
    │   ├── lib/
    │   │   ├── api.js              # API client (PostgREST reads, gateway writes)
    │   │   ├── state.js            # Global reactive state (Preact signals)
    │   │   └── markdown.js         # marked.js wrapper + config
    │   ├── components/
    │   │   ├── sidebar.js          # Nav sidebar + hamburger toggle
    │   │   ├── modal.js            # Confirmation/form modal
    │   │   ├── tag-chips.js        # Project link tags (add/remove/read-only modes)
    │   │   ├── entity-list.js      # Reusable filterable list with pagination
    │   │   ├── entity-form.js      # Reusable create/edit form
    │   │   ├── markdown-editor.js  # Textarea + live preview toggle
    │   │   ├── search-bar.js       # Search input component
    │   │   └── toast.js            # Toast notification system
    │   └── pages/
    │       ├── dashboard.js        # Stats, recent activity, orphan alerts
    │       ├── knowledge.js        # List + detail + create/edit
    │       ├── memories.js         # List + detail + create/edit
    │       ├── projects.js         # List + detail + create/edit
    │       ├── search.js           # Global search results
    │       └── archive.js          # Archived items + bulk delete
    └── vendor/
        ├── preact.min.js
        ├── htm.min.js
        ├── preact-hooks.min.js
        ├── preact-signals.min.js
        └── marked.min.js
```

### Routing

Hash-based routing (no server-side routing needed):

```
#/                          → Dashboard
#/knowledge                 → Knowledge list
#/knowledge/new             → Create knowledge
#/knowledge/:id             → Knowledge detail
#/knowledge/:id/edit        → Edit knowledge
#/memories                  → Memories list
#/memories/new              → Create memory
#/memories/:id              → Memory detail
#/memories/:id/edit         → Edit memory
#/projects                  → Projects list
#/projects/new              → Create project
#/projects/:name            → Project detail
#/projects/:name/edit       → Edit project
#/search                    → Global search (with ?q= param)
#/archive                   → Archive view
```

### Navigation

Persistent left sidebar (180px) with sections:
1. Dashboard
2. Knowledge
3. Memories
4. Projects
5. Search
6. Archive (separated by divider)

**Responsive behavior:** At ≤768px viewport, sidebar collapses behind a hamburger menu button. Tapping hamburger slides sidebar in as an overlay with a backdrop. Tapping backdrop or a nav item closes it.

### Shared Components

- **`entity-list.js`** — Configurable table with text filter, dropdown filters, active/all toggle, and pagination. Used by Knowledge, Memories, and Archive pages with different column configs.
- **`entity-form.js`** — Create/edit form template with title input, metadata fields, project link tag chips, and markdown editor. Used by Knowledge and Memories. Project forms are different enough to be standalone.
- **`tag-chips.js`** — Project link tags rendered as colored chips. Three modes: read-only (list views), removable (detail views, with × to unlink), and editable (forms, with × to remove and "+ Add project" dropdown to add).
- **`modal.js`** — Centered overlay modal for confirmations and small forms. Used for delete confirmation, project link dropdown, etc.
- **`markdown-editor.js`** — Textarea with monospace font and a Write/Preview toggle. Preview renders content through marked.js.

### State Management

Preact Signals for global state:
- `currentRoute` — current hash route
- `sidebarOpen` — mobile sidebar visibility
- `toasts` — active toast notifications

Page-level state stays local to components via `useState`/`useEffect` hooks. No global entity caching — each page fetches fresh data on mount. After a successful write operation, the current view refetches its data.

## Pages

### Dashboard (#/)

**Stats cards row** — 4 cards showing counts of active knowledge, memories, projects, and orphans. Each card shows the active count prominently with archived/system count below. Orphan card highlighted in amber when count > 0. Cards are clickable, navigating to the respective list page.

**Recent activity** — Last 10 created/updated items across all types, fetched from the `recent_activity` DB view. Each row shows type (color-coded badge), title, and relative timestamp. Rows are clickable, navigating to the detail view.

**Orphan alerts** — Panel showing items with no active project links. Each item clickable to its detail view. "View all orphans →" link navigates to a filtered list. Only visible when orphan count > 0.

### Knowledge (#/knowledge)

**List view** — Table with columns: Title, Category, Projects (tag chips), Updated. Filter bar with text input (filters by title via ILIKE), category dropdown, project dropdown, and active/all status toggle. Pagination at bottom (20 items per page). Orphaned items show an amber "⚠ orphan" indicator next to the title. "+ New Entry" button in header.

**Detail view** — Breadcrumb navigation (Knowledge / {title}). Action buttons: Edit, Archive (amber). Metadata row: category, provenance project, created date, updated date, URL (clickable link). Project link tags with × to unlink and "+ Add project" button. Content tags in green chips. Rendered markdown content in a card below.

**Create/Edit form** — Fields: Title (required), Category (dropdown), URL (optional), Project links (tag chips with add/remove), Tags (comma-separated input), Content (required, markdown editor with Write/Preview toggle). Save and Cancel buttons. On create, default project is "general" (pre-selected in project chips).

### Memories (#/memories)

Same pattern as Knowledge with these differences:
- **Type** column instead of Category, showing color-coded badges: user (green), feedback (amber), project (purple), reference (blue)
- **Description** shown as a subtitle under the name in list rows
- **Form fields:** Memory Type (dropdown: user/feedback/project/reference), Name (required), Description (optional), Project links, Content (required, markdown editor)
- No URL or tags fields

### Projects (#/projects)

**List view** — Card grid (not table) with project name, description snippet, tech stack tags, status badge, and counts of linked knowledge + memories. "+ New Project" button. The `general` system project always appears first with a "system" badge.

**Detail view** — Project name, description, metadata row (status badge, orphan policy, tech stack, repo URL as clickable link). Rendered notes section. Tabbed linked-entities view with two tabs: Knowledge (count) and Memories (count). Each tab shows a list of linked items with title, type/category, timestamp, and an "Unlink" action. Items are clickable to their detail pages.

**Create/Edit form** — Fields: Name (required, immutable on edit), Description, Repo URL, Tech Stack (comma-separated), Notes (markdown editor), Orphan Policy (dropdown: archive/reassign). The `general` project cannot be edited or archived — Edit and Archive buttons are hidden.

### Search (#/search)

**Search bar** — Large text input with Search button. Mode toggle: Semantic (default) / Exact. Type filter chips: All (default) / Knowledge / Memories. Result count displayed.

**Results** — Cards sorted by similarity score. Each card shows: type badge (color-coded), title, content snippet (first ~200 chars), similarity percentage (color-coded: green ≥90%, yellow-green ≥75%, amber ≥60%), project link chips, category/type, and relative timestamp. Clicking a result navigates to its detail page.

**Search request:** `POST /api/search` with body `{query, mode: "semantic"|"exact", types: ["knowledge","memories"]}`.

**Search response:** `{results: {knowledge: [...], memories: [...]}}` with each item including a `similarity` score (0-1).

### Archive (#/archive)

**Unified view** of all archived items across all types. Type filter tabs at top showing counts: All, Knowledge (N), Memories (N), Projects (N).

**Checkbox selection** — Each item has a checkbox. "Select all" checkbox in header. Items show: checkbox, type badge, title, archived date, provenance project. Each item has an individual "Restore" button (triggers unarchive).

**Bulk action bar** — Appears at bottom when ≥1 item is selected. Shows selection count, "Restore Selected" button (green), and "Permanently Delete" button (red). The delete button opens a confirmation modal: "Permanently delete N items? This cannot be undone." with Cancel and Delete buttons.

**Hard delete safety:** The mcp-gateway REST endpoint validates that every item in a delete request has `status = 'archived'`. Attempting to delete an active item returns 400.

## API Design

### mcp-gateway REST Endpoints

Added alongside the existing `/mcp/` Streamable HTTP endpoint. These reuse the same internal Python async functions that the MCP tools call.

#### Knowledge

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/knowledge` | `{title, content, project?, category?, tags?, url?}` | Reuses `add_knowledge` logic, auto-creates project_link |
| PUT | `/api/knowledge/:id` | `{title?, content?, category?, tags?, url?}` | New — partial update, web-only |
| DELETE | `/api/knowledge/:id` | — | Hard delete, validates status=archived |

#### Memories

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/memories` | `{memory_type, name, content, project?, description?}` | Reuses `save_memory` logic |
| PUT | `/api/memories/:id` | `{name?, content?, description?}` | New — partial update, web-only |
| DELETE | `/api/memories/:id` | — | Hard delete, validates status=archived |

#### Projects

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/projects` | `{name, description?, repo_url?, tech_stack?, notes?, orphan_policy?}` | Reuses `add_project` logic |
| PUT | `/api/projects/:name` | `{description?, repo_url?, tech_stack?, notes?, orphan_policy?}` | Reuses `update_project` logic |
| DELETE | `/api/projects/:name` | — | Hard delete, validates status=archived, rejects `general` |

#### Lifecycle

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/archive/:type/:id` | — | type = knowledge\|memory\|project. Reuses existing archive logic |
| POST | `/api/unarchive/:type/:id` | — | Reuses existing unarchive logic |

#### Links

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/link` | `{project, knowledge_id? \| memory_id?}` | Reuses `link_to_project` logic |
| DELETE | `/api/link` | `{project, knowledge_id? \| memory_id?}` | Reuses `unlink_from_project` logic |

#### Search

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/search` | `{query, mode?, types?}` | Reuses existing search logic from MCP tools |

#### Bulk Operations

| Method | Path | Body | Notes |
|--------|------|------|-------|
| DELETE | `/api/bulk-delete` | `{items: [{type, id}, ...]}` | All items must be archived. Transactional. |

### New Database Objects for PostgREST

These views and functions enable efficient read queries that PostgREST can't do with raw table joins:

#### `knowledge_with_projects` view

Joins knowledge → project_links → projects. Returns all knowledge columns plus an array of linked project names. Enables filtering knowledge by project via PostgREST's `cs` (contains) operator.

```sql
CREATE OR REPLACE VIEW knowledge_with_projects AS
SELECT k.*,
       COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS projects
FROM knowledge k
LEFT JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
LEFT JOIN projects p ON p.id = pl.project_id
GROUP BY k.id;
```

#### `memories_with_projects` view

Same pattern for memories.

```sql
CREATE OR REPLACE VIEW memories_with_projects AS
SELECT m.*,
       COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS projects
FROM memories m
LEFT JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
LEFT JOIN projects p ON p.id = pl.project_id
GROUP BY m.id;
```

#### `recent_activity` view

UNION of knowledge and memories for the dashboard feed. Ordering and limiting are applied by the PostgREST query (`?order=updated_at.desc&limit=10`), not in the view itself.

```sql
CREATE OR REPLACE VIEW recent_activity AS
SELECT id, 'knowledge' AS type, title AS name, category AS subtype, updated_at
FROM knowledge WHERE status = 'active'
UNION ALL
SELECT id, 'memory' AS type, name, memory_type AS subtype, updated_at
FROM memories WHERE status = 'active';
```

#### `orphaned_items` function

Returns items with no active project links.

```sql
CREATE OR REPLACE FUNCTION orphaned_items()
RETURNS TABLE(id INT, type TEXT, name TEXT, updated_at TIMESTAMPTZ) AS $$
  SELECT k.id, 'knowledge', k.title, k.updated_at
  FROM knowledge k
  LEFT JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
  WHERE k.status = 'active' AND pl.id IS NULL
  UNION ALL
  SELECT m.id, 'memory', m.name, m.updated_at
  FROM memories m
  LEFT JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
  WHERE m.status = 'active' AND pl.id IS NULL
  ORDER BY updated_at DESC;
$$ LANGUAGE sql STABLE;
```

## Error Handling

### API Client

The `api.js` module wraps all fetch calls. On non-2xx responses, it extracts the error message from the JSON body:
- PostgREST returns `{message, details, hint}`
- mcp-gateway returns `{error, detail}`

Errors display as toast notifications at the top of the content area. Toasts auto-dismiss after 5 seconds and can be clicked to dismiss immediately.

### Specific Error Cases

| Scenario | Handling |
|----------|----------|
| Network error / container down | Toast: "Cannot reach server. Check that containers are running." |
| Item not found (404) | Redirect to list view with toast: "Item not found — it may have been deleted." |
| Hard delete non-archived item | Gateway rejects 400: "Item must be archived before deletion." |
| Archive `general` project | Gateway rejects 400: "System project cannot be archived." |
| Duplicate project name | Gateway rejects 409: "Project already exists." |
| Search embedding API failure | Automatic fallback to text search, toast: "Semantic search unavailable, using text search." |
| Stale data after write | Refetch current view data after any successful write. No optimistic updates. |

### Loading and Empty States

- **Loading:** Skeleton/spinner in the content area while fetching. Sidebar and nav remain interactive.
- **Empty states:** Each list view shows a friendly message with a link to the create form (e.g., "No knowledge entries yet. Create your first one.").

## Error Tracking (Sentry)

Single Sentry project for the entire OpenBrain stack, differentiated by service tags.

### Configuration

A `SENTRY_DSN` environment variable in docker-compose.yml, shared by all instrumented containers.

### Instrumented Services

| Service | SDK | Tags |
|---------|-----|------|
| mcp-gateway | `sentry-sdk[starlette]` (Python) | `service:mcp-gateway` |
| web-ui (frontend JS) | `@sentry/browser` standalone bundle (CDN or local copy in vendor/) | `service:web-ui` |
| embedder | `sentry-sdk` (Python) | `service:embedder` |

PostgREST is not instrumented (third-party binary). Its errors surface as failed API calls captured by the frontend Sentry.

### What Gets Captured

- **mcp-gateway:** Unhandled exceptions in REST endpoints and MCP tools, failed OpenAI API calls, database errors, slow transactions.
- **web-ui:** Uncaught JS exceptions, failed fetch calls (after retry exhaustion), rendering errors.
- **embedder:** Failed embedding API calls, database connection errors, processing failures.

## Migration from Existing Worktree

The `feat/phase2-web-ui` branch/worktree contains early work based on the pre-refactor schema. Key changes:

1. **Drop search-service** — eliminated, search folded into mcp-gateway
2. **Drop shared_resources** references — table no longer exists
3. **Replace PostgREST-only API** with hybrid PostgREST reads + mcp-gateway writes
4. **Add project_links UI** — tag chips throughout
5. **Add archive/unarchive flows** — tiered delete, Archive page
6. **Add create/edit forms** — full CRUD instead of read-only browse
7. **Replace vanilla JS** with Preact + HTM
8. **Add dark-only theme** — remove light theme and toggle
9. **Add Sentry integration** — all three services
10. **Add new DB views/functions** — for PostgREST read queries

The existing CSS color palette and general aesthetic can be adapted. The Nginx reverse proxy pattern carries over with updated routes.
