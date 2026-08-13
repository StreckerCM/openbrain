# Lifecycle Management & Schema Refactor Design

**Date:** 2026-04-10
**Scope:** Phase 1 — Schema migration, junction table, archive/lifecycle MCP tools
**Phase 2 (separate spec):** Web UI updates for archive/delete/unarchive flows

## Problem

The current OpenBrain schema is append-only with no way to retire stale information.
Memories and knowledge entries accumulate indefinitely, and outdated entries continue
to surface in search results. The `shared_resources` table uses a `TEXT[]` array for
project associations, which is hard to query and cannot represent per-link metadata
like archive status.

Additionally, the `shared_resources` table serves a dual purpose — it stores both
entity data (name, url, metadata) and relationship data (project associations). This
should be split into proper knowledge entries and a junction table.

## Final Table Set

| Table | Purpose |
|---|---|
| `projects` | Project registry |
| `knowledge` | All factual/reference content (absorbs shared_resources) |
| `memories` | Agent context (user, feedback, project, reference) |
| `project_links` | Junction: connects projects ↔ knowledge and projects ↔ memories |

### Dropped Tables

- `shared_resources` — absorbed by `knowledge` + `project_links`

## Schema Changes

### `knowledge` — add columns

| Column | Type | Notes |
|---|---|---|
| `url` | `TEXT` | Optional, queryable link (e.g., repo URL, docs page) |
| `status` | `TEXT NOT NULL DEFAULT 'active'` | `active` or `archived` |

The existing `project` column remains but its meaning shifts to **provenance**
("which project created this entry"), not ownership. Defaults to `general` when
omitted.

### `memories` — add column

| Column | Type | Notes |
|---|---|---|
| `status` | `TEXT NOT NULL DEFAULT 'active'` | `active` or `archived` |

The existing `project` column becomes provenance only. Defaults to `general`.

### `projects` — add columns

| Column | Type | Notes |
|---|---|---|
| `status` | `TEXT NOT NULL DEFAULT 'active'` | `active`, `archived`, or `system` |
| `orphan_policy` | `TEXT` | `archive` or `reassign`. NULL = use env var default |

The `system` status is reserved for the `general` project and prevents archiving.

### `project_links` — new table

```sql
CREATE TABLE IF NOT EXISTS project_links (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id),
    knowledge_id INT REFERENCES knowledge(id),
    memory_id INT REFERENCES memories(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT exactly_one_entity CHECK (
        (knowledge_id IS NOT NULL AND memory_id IS NULL) OR
        (knowledge_id IS NULL AND memory_id IS NOT NULL)
    )
);

-- Partial unique indexes to prevent duplicate links
CREATE UNIQUE INDEX project_links_knowledge_unique
    ON project_links (project_id, knowledge_id)
    WHERE knowledge_id IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX project_links_memory_unique
    ON project_links (project_id, memory_id)
    WHERE memory_id IS NOT NULL AND status = 'active';

-- Query indexes
CREATE INDEX project_links_project_idx ON project_links (project_id);
CREATE INDEX project_links_knowledge_idx ON project_links (knowledge_id) WHERE knowledge_id IS NOT NULL;
CREATE INDEX project_links_memory_idx ON project_links (memory_id) WHERE memory_id IS NOT NULL;
CREATE INDEX project_links_status_idx ON project_links (status);
```

### General Project — seed data

```sql
INSERT INTO projects (name, description, status)
VALUES ('general', 'Default project for non-project-specific knowledge and memories', 'system')
ON CONFLICT (name) DO NOTHING;
```

## Archive & Cascade Behavior

### Core Rules

1. MCP tools only expose `archive` — no hard delete via MCP.
2. Hard delete is a Phase 2 web UI feature with secondary confirmation.
3. All search/list/recall queries filter to `status = 'active'` by default.
4. The `general` project (status = `system`) cannot be archived.

### Cascade Matrix

| Action | Entity itself | Its project_links | Linked entities |
|---|---|---|---|
| Archive knowledge/memory | → archived | → all archived | n/a |
| Archive project | → archived | → all archived | unchanged (orphan policy applied) |
| Archive single link | unchanged | → that link archived | unchanged |
| Unarchive knowledge/memory | → active | unchanged (re-link manually) | n/a |
| Unarchive project | → active | unchanged (re-link manually) | unchanged |

### Orphan Handling

When archiving a project, after cascading its links, any knowledge/memory that now
has zero active `project_links` is considered orphaned.

**Environment variable:** `ORPHAN_POLICY`

| Value | Behavior |
|---|---|
| `archive` (default) | Orphaned entities are set to `status = 'archived'` |
| `reassign` | Orphaned entities get a new `project_link` to `general` |

**Per-project override:** The `orphan_policy` column on `projects` takes precedence
over the env var when set. This allows different strategies on the same instance.

**Use case — project-specific knowledge (e.g., directional drilling):**
`orphan_policy = 'archive'` — archiving the project archives its specialized content
rather than polluting `general`.

**Use case — life management (e.g., contacts, meetings):**
`orphan_policy = 'reassign'` — archiving a category project keeps the data accessible
under `general`.

## MCP Tool Changes

### New Tools (8)

| Tool | Signature | Purpose |
|---|---|---|
| `archive_knowledge` | `(id)` | Archive knowledge + cascade links |
| `archive_memory` | `(id)` | Archive memory + cascade links |
| `archive_project` | `(name)` | Archive project + cascade links + orphan policy |
| `unarchive_knowledge` | `(id)` | Restore knowledge to active |
| `unarchive_memory` | `(id)` | Restore memory to active |
| `unarchive_project` | `(name)` | Restore project to active |
| `link_to_project` | `(project, knowledge_id?, memory_id?)` | Create a project_link |
| `unlink_from_project` | `(project, knowledge_id?, memory_id?)` | Archive a specific link |

### Modified Tools (9)

| Tool | Changes |
|---|---|
| `add_knowledge` | `project` → provenance (defaults to `general`); new `url` param; auto-creates `project_link` |
| `save_memory` | `project` → provenance (defaults to `general`); auto-creates `project_link` |
| `search_knowledge` | Filters `status = 'active'`; new `include_archived` param; project filter via JOIN |
| `list_knowledge` | Same active filter + `include_archived` param |
| `recall_memory` | Same active filter + `include_archived` param; project filter via JOIN |
| `list_memories` | Same active filter + `include_archived` param |
| `add_project` | New `orphan_policy` param |
| `list_projects` | Filters `status IN ('active', 'system')` by default; new `include_archived` param |
| `get_project` | Returns status and orphan_policy in output |

### New Tool (1)

| Tool | Signature | Purpose |
|---|---|---|
| `update_project` | `(name, description?, repo_url?, tech_stack?, notes?, orphan_policy?)` | Modify project fields; `name` is lookup key (not changeable via MCP) |

### Removed Tools (3)

- `add_shared_resource` — absorbed by `add_knowledge` with url + category
- `search_shared_resources` — use `search_knowledge`
- `list_shared_resources` — use `list_knowledge`

### Tool Count: 18 total (was 12)

- 9 carried forward (modified)
- 8 new (archive/unarchive/link tools)
- 1 new (update_project)
- 3 removed (shared_resource tools)

## Migration Strategy

All steps are ordered for safety — additive steps first, destructive step last.

### Step 1: Schema additions (non-destructive)

- Add `status` column to `knowledge`, `memories`, `projects`
- Add `url` column to `knowledge`
- Add `orphan_policy` column to `projects`
- Create `project_links` table with constraints and indexes
- Insert `general` project with `status = 'system'`

### Step 2: Backfill project_links from `knowledge.project`

- For each knowledge row, look up the project by name in `projects`
- Create the project if it doesn't exist (with `status = 'active'`)
- Insert a `project_link` row connecting them

### Step 3: Backfill project_links from `memories.project`

- Same approach for each memory with a non-null project
- Memories with `project IS NULL` get linked to `general`

### Step 4: Migrate `shared_resources` → `knowledge` + `project_links`

- For each shared_resource row:
  - Insert into `knowledge`: `category = resource_type`, `title = name`,
    `content = description`, `url = url`, `created_by_project = 'general'`
  - If `metadata` is non-empty, append as structured text block in content
  - For each project name in the `projects[]` array, create a `project_link`
  - Migrated entries will have `embedding = NULL`, picked up by embedder next cycle

### Step 5: Update search functions

- Drop `search_shared_resources` function
- Update `search_knowledge` to filter `status = 'active'` and JOIN through
  `project_links` when filtering by project
- Update `search_memories` similarly

### Step 6: Drop old table (destructive — verify migration first)

- Drop `shared_resources` table and its indexes

### Idempotency

The migration script must be safe to re-run. Use `IF NOT EXISTS`, `ON CONFLICT DO
NOTHING`, and guard destructive steps with existence checks.

## Embedder Changes

- Remove `shared_resources` from the `TABLES` list in `embed.py`
- Knowledge entries migrated from resources get embedded on the next poll cycle

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ORPHAN_POLICY` | `archive` | Global default for orphan handling on project archive |

## Phase 2 Boundary

The following are explicitly **out of scope** for this phase and will be covered in a
separate web UI spec:

- Hard delete with confirmation dialog
- Browse/archive/unarchive UI flows
- Orphan indicator in the UI
- Project link management UI
- Rename project (web UI only)
