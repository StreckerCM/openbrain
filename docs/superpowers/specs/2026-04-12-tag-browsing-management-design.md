# Tag Browsing & Management — Design Spec

**Date:** 2026-04-12
**Status:** Draft

## Summary

Add tag browsing and management capabilities to the OpenBrain dashboard. Two new UI surfaces — a weighted tag cloud widget on the dashboard home page and a dedicated Tags management page — plus backend support for tag aggregation, merge, rename, and delete operations.

## Motivation

The knowledge base has **262 distinct tags** across 73 entries, with **195 tags used only once**. There is currently no way to discover tags, browse knowledge by tag, or clean up tag inconsistencies (e.g., `Directional Drilling` vs `Directional-Drilling`). The existing `TagChips` component renders tags on detail views but tags are not clickable or filterable from the UI.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where to browse tags | Dashboard widget + dedicated page | Quick access from home for discovery, full page for management |
| Dashboard widget style | Large weighted cloud, top 20 tags | Visually prominent, not just compact chips |
| Tags page layout | Cloud at top + sortable table with bulk actions | Best for both discovery and management |
| Management operations | Merge, rename, delete | Merge is the key feature — handles inconsistency cleanup |
| Backend for reads | PostgreSQL view via PostgREST | Follows existing read pattern |
| Backend for writes | FastAPI endpoints | Needs batch updates + re-embedding, follows existing write pattern |

---

## 1. Database Changes

### 1.1 New View: `tag_stats`

Unnests the `tags` array from the `knowledge` table and aggregates usage counts.

```sql
CREATE OR REPLACE VIEW tag_stats AS
SELECT
    tag,
    COUNT(*) AS entry_count,
    MAX(k.updated_at) AS last_used
FROM knowledge k,
     LATERAL UNNEST(k.tags) AS tag
WHERE k.status = 'active'
GROUP BY tag
ORDER BY entry_count DESC, tag ASC;
```

Exposed via PostgREST at `GET /api/read/tag_stats`.

Supports:
- `?order=entry_count.desc` (default)
- `?order=tag.asc` (alphabetical)
- `?tag=ilike.*search*` (search/filter)
- `?limit=20` (for dashboard widget)

### 1.2 Migration

Add the view to `mcp-gateway/init.sql` in the migration section. Include `NOTIFY pgrst, 'reload schema'` after creation so PostgREST picks it up.

---

## 2. Backend API Endpoints

All new endpoints on the FastAPI gateway (`mcp-gateway/server.py`), under `/api/write/tags/`.

### 2.1 POST `/api/write/tags/merge`

Merge two or more tags into a single target tag across all knowledge entries.

**Request:**
```json
{
    "source_tags": ["Directional Drilling", "Directional-Drilling"],
    "target_tag": "directional-drilling"
}
```

**Behavior:**
1. Find all knowledge entries where `tags` contains any of `source_tags`
2. For each entry: remove all `source_tags` from the array, add `target_tag` (if not already present)
3. Update the entry's `updated_at` timestamp
4. Trigger re-embedding for each modified entry (content changed contextually)
5. Return `{ "updated_count": N, "target_tag": "directional-drilling" }`

**Edge cases:**
- If `target_tag` already exists on an entry, just remove the source tags (no duplicate)
- If `source_tags` contains `target_tag`, treat it as a rename of the others into the target
- Empty `source_tags` array → 400 error

### 2.2 POST `/api/write/tags/rename`

Rename a single tag across all knowledge entries.

**Request:**
```json
{
    "old_tag": "Directional Drilling",
    "new_tag": "directional-drilling"
}
```

**Behavior:**
1. Find all knowledge entries where `tags` contains `old_tag`
2. Replace `old_tag` with `new_tag` in each entry's tag array
3. Trigger re-embedding for each modified entry
4. Return `{ "updated_count": N, "old_tag": "...", "new_tag": "..." }`

**Note:** This is effectively a merge with one source tag. Could be implemented as a convenience wrapper around the merge logic.

### 2.3 POST `/api/write/tags/delete`

Remove a tag from all knowledge entries.

**Request:**
```json
{
    "tag": "deprecated-tag"
}
```

**Behavior:**
1. Find all knowledge entries where `tags` contains `tag`
2. Remove `tag` from each entry's tag array
3. Trigger re-embedding for each modified entry
4. Return `{ "updated_count": N, "deleted_tag": "..." }`

### 2.4 GET `/api/read/tag_stats` (via PostgREST)

No custom endpoint needed — the PostgreSQL view handles this directly through PostgREST.

---

## 3. Frontend — Dashboard Widget

### 3.1 Location

New section on the `DashboardPage` component (`web-ui/static/js/pages/dashboard.js`), positioned below the existing stats cards row and above the recent activity section.

### 3.2 Component: `TagCloudWidget`

**New file:** `web-ui/static/js/components/tag-cloud.js`

**Props:**
- `tags` — array of `{ tag, entry_count, last_used }` from `tag_stats`
- `limit` — max tags to show (default: 20)
- `onTagClick` — callback, navigates to `#/knowledge?tag={tag}`
- `showViewAll` — boolean, shows "View all N tags →" link to `#/tags`

**Rendering:**
- Tags sized proportionally to `entry_count` using a logarithmic scale
- Min font size: 12px, max font size: 26px
- Uses existing `--tag-text` color (`#7ee787`), opacity fades for lower counts
- Click any tag → navigate to `#/knowledge?tag={tag}` (filtered knowledge list)

**Data fetching:**
- `GET /api/read/tag_stats?order=entry_count.desc&limit=20`
- Fetched alongside existing dashboard data in `DashboardPage`'s `useEffect`

### 3.3 Stats Card

Add a "Tags" count to the existing stats row (Knowledge / Memories / Projects). Fetch total count from `tag_stats` or derive from the full count query.

---

## 4. Frontend — Tags Page

### 4.1 Route

**New file:** `web-ui/static/js/pages/tags.js`

**Route:** `#/tags` — added to `app.js` router switch and `sidebar.js` nav items.

### 4.2 Page Layout

Three sections, top to bottom:

#### Section A: Filter Bar

- **Search input** — filters tags by substring match (client-side filtering of loaded data, or `?tag=ilike.*query*` for server-side)
- **Quick filter pills:**
  - All (262)
  - Used once (195) — `?entry_count=eq.1`
  - Multi-use (67) — `?entry_count=gt.1`
- **Sort toggle:** Count ↓ / A–Z

#### Section B: Tag Cloud

- Full `TagCloudWidget` with all tags (no limit)
- Click a tag → navigates to `#/knowledge?tag={tag}` (consistent with dashboard widget behavior)

#### Section C: Tag Table

Sortable table with columns:

| Column | Description |
|--------|-------------|
| **Checkbox** | For bulk selection |
| **Tag** | Tag name (clickable → `#/knowledge?tag={tag}`) |
| **Entries** | Usage count |
| **Last used** | Relative time from `last_used` |

**Bulk action bar** — appears when 1+ tags are selected:
- **Merge into one** (enabled when 2+ selected) — opens merge modal
- **Rename** (enabled when exactly 1 selected) — opens rename modal
- **Delete** (enabled when 1+ selected) — opens confirm modal

**Pagination:** Load all tags at once (262 is small). Client-side filtering and sorting. No server-side pagination needed at this scale.

### 4.3 Merge Modal

**Component:** Reuse existing `Modal` component (`web-ui/static/js/components/modal.js`).

**Content:**
1. Shows source tags (struck through, red) that will be removed
2. Editable text input for target tag name — pre-populated with the most-used source tag (lowercase, hyphenated)
3. Info line: "This will update N knowledge entries and trigger re-embedding for each."
4. Confirm / Cancel buttons

**On confirm:**
- `POST /api/write/tags/merge` with `{ source_tags, target_tag }`
- On success: show toast, refresh tag list
- On error: show error toast

### 4.4 Rename Modal

Similar to merge but simpler:
1. Shows current tag name
2. Text input for new name
3. Info line with affected entry count
4. Confirm / Cancel

**On confirm:** `POST /api/write/tags/rename`

### 4.5 Delete Confirmation

Standard confirm dialog:
1. Lists tag(s) to be deleted
2. Warning: "This will remove the tag from N entries."
3. Confirm / Cancel

**On confirm:** `POST /api/write/tags/delete` (one call per tag, or batch if we add a batch endpoint)

---

## 5. API Module Changes

### 5.1 `web-ui/static/js/lib/api.js`

Add new functions:

```javascript
// --- Tag Stats (Read) ---
export async function fetchTagStats(params = {}) { ... }

// --- Tag Management (Write) ---
export async function mergeTags(sourceTags, targetTag) { ... }
export async function renameTag(oldTag, newTag) { ... }
export async function deleteTag(tag) { ... }
```

### 5.2 `web-ui/static/js/pages/knowledge.js`

The knowledge page already supports `?tag=` filtering in the URL params but the filter UI doesn't expose it. No changes needed — tag clicks from the cloud/table will navigate with the query param and the existing logic handles it.

---

## 6. Navigation Changes

### 6.1 Sidebar (`web-ui/static/js/components/sidebar.js`)

Add "Tags" to `NAV_ITEMS` array between "Search" and the Archive separator:

```javascript
const NAV_ITEMS = [
    { label: 'Dashboard', href: '#/' },
    { label: 'Knowledge', href: '#/knowledge' },
    { label: 'Memories', href: '#/memories' },
    { label: 'Projects', href: '#/projects' },
    { label: 'Search', href: '#/search' },
    { label: 'Tags', href: '#/tags' },
];
```

### 6.2 Router (`web-ui/static/js/app.js`)

Add case to the router switch:

```javascript
case 'tags': return html`<${TagsPage} query=${query} />`;
```

---

## 7. Files to Create / Modify

| File | Action |
|------|--------|
| `mcp-gateway/init.sql` | Add `tag_stats` view + NOTIFY pgrst |
| `mcp-gateway/server.py` | Add 3 tag management endpoints |
| `web-ui/static/js/components/tag-cloud.js` | **New** — TagCloudWidget component |
| `web-ui/static/js/pages/tags.js` | **New** — Tags page |
| `web-ui/static/js/pages/dashboard.js` | Add TagCloudWidget section |
| `web-ui/static/js/lib/api.js` | Add tag API functions |
| `web-ui/static/js/app.js` | Add tags route |
| `web-ui/static/js/components/sidebar.js` | Add Tags nav item |
| `web-ui/static/css/style.css` | Add tag cloud sizing styles |

---

## 8. Build Sequence

1. **Database** — Add `tag_stats` view to init.sql, test via PostgREST
2. **Backend** — Add merge/rename/delete endpoints to server.py, test with curl
3. **Component** — Build `TagCloudWidget`, test in isolation
4. **Dashboard** — Integrate widget into dashboard page
5. **Tags page** — Build full page with cloud, table, bulk actions
6. **Modals** — Merge, rename, delete dialogs
7. **Navigation** — Add route + sidebar entry
8. **Local testing** — Full end-to-end via Docker Compose
