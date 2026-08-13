# Tag Browsing & Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tag browsing (weighted cloud on dashboard + dedicated Tags page) and tag management (merge, rename, delete) to the OpenBrain dashboard.

**Architecture:** New PostgreSQL view (`tag_stats`) for read-side tag aggregation via PostgREST. Three new Starlette route handlers on the FastAPI gateway for merge/rename/delete writes. Two new frontend components (`TagCloudWidget`, `TagsPage`) plus dashboard integration. All follows existing patterns.

**Tech Stack:** PostgreSQL (LATERAL UNNEST), PostgREST, Python/asyncpg/Starlette, Preact/htm, CSS custom properties.

**Spec:** `docs/superpowers/specs/2026-04-12-tag-browsing-management-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `init.sql` | Modify | Add `tag_stats` view |
| `mcp-gateway/server.py` | Modify | Add `rest_tags_merge`, `rest_tags_rename`, `rest_tags_delete` handlers + routes |
| `web-ui/static/js/lib/api.js` | Modify | Add `fetchTagStats`, `mergeTags`, `renameTag`, `deleteTag` functions |
| `web-ui/static/js/components/tag-cloud.js` | **Create** | `TagCloudWidget` component |
| `web-ui/static/js/pages/tags.js` | **Create** | Full Tags management page |
| `web-ui/static/js/pages/dashboard.js` | Modify | Integrate tag cloud widget |
| `web-ui/static/js/components/sidebar.js` | Modify | Add Tags nav item |
| `web-ui/static/js/app.js` | Modify | Add `#/tags` route |
| `web-ui/static/css/style.css` | Modify | Add tag cloud + tags page styles |

---

### Task 1: Add `tag_stats` PostgreSQL View

**Files:**
- Modify: `init.sql:299-302` (drop section) and `init.sql:328` (after `recent_activity` view)

- [ ] **Step 1: Add the view to init.sql**

In `init.sql`, add to the drop section (around line 302):

```sql
DROP VIEW IF EXISTS tag_stats CASCADE;
```

Then after the `recent_activity` view (around line 328), add:

```sql
-- View: distinct tags with usage counts and last-used timestamps
CREATE OR REPLACE VIEW tag_stats AS
SELECT
    tag,
    COUNT(*) AS entry_count,
    MAX(k.updated_at) AS last_used
FROM knowledge k,
     LATERAL UNNEST(k.tags) AS tag
WHERE k.status = 'active'
  AND k.tags != '{}'
GROUP BY tag
ORDER BY entry_count DESC, tag ASC;
```

- [ ] **Step 2: Test locally with Docker**

```bash
docker compose down && docker compose up -d
```

Wait for containers to start, then verify the view works:

```bash
curl -s "http://localhost:3000/tag_stats?limit=5" | jq .
```

Expected: JSON array of `{ "tag": "ISCWSA", "entry_count": 14, "last_used": "..." }` objects sorted by count descending.

Also test filtering:

```bash
curl -s "http://localhost:3000/tag_stats?tag=ilike.*mwd*" | jq .
curl -s "http://localhost:3000/tag_stats?entry_count=eq.1&limit=5" | jq .
```

- [ ] **Step 3: Commit**

```bash
git add init.sql
git commit -m "feat: add tag_stats PostgreSQL view for tag aggregation"
```

---

### Task 2: Add Tag Management API Endpoints

**Files:**
- Modify: `mcp-gateway/server.py:1796-1818` (before `rest_routes` list and in the routes list)

- [ ] **Step 1: Add the three handler functions**

Add these above the `rest_routes` list (around line 1798), after the `rest_bulk_delete` function:

```python
# --- Tag Management REST ---


async def rest_tags_merge(request: Request) -> JSONResponse:
    body = await request.json()
    source_tags = body.get("source_tags")
    target_tag = body.get("target_tag")
    if not source_tags or not isinstance(source_tags, list) or not target_tag:
        return _err("source_tags (array) and target_tag (string) are required", 400)
    if not target_tag.strip():
        return _err("target_tag cannot be empty", 400)
    target_tag = target_tag.strip()

    pool = _get_pool()
    # Find all knowledge entries that have any of the source tags
    rows = await pool.fetch(
        "SELECT id, tags FROM knowledge WHERE status = 'active' AND tags && $1",
        source_tags,
    )
    updated = 0
    for row in rows:
        old_tags = list(row["tags"])
        new_tags = [t for t in old_tags if t not in source_tags]
        if target_tag not in new_tags:
            new_tags.append(target_tag)
        if new_tags != old_tags:
            await pool.execute(
                "UPDATE knowledge SET tags = $1, updated_at = NOW() WHERE id = $2",
                new_tags, row["id"],
            )
            updated += 1
    return _json({"updated_count": updated, "target_tag": target_tag})


async def rest_tags_rename(request: Request) -> JSONResponse:
    body = await request.json()
    old_tag = body.get("old_tag")
    new_tag = body.get("new_tag")
    if not old_tag or not new_tag:
        return _err("old_tag and new_tag are required", 400)
    new_tag = new_tag.strip()
    if not new_tag:
        return _err("new_tag cannot be empty", 400)

    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, tags FROM knowledge WHERE status = 'active' AND $1 = ANY(tags)",
        old_tag,
    )
    updated = 0
    for row in rows:
        old_tags = list(row["tags"])
        new_tags = [new_tag if t == old_tag else t for t in old_tags]
        # Deduplicate in case new_tag already existed
        seen = set()
        deduped = []
        for t in new_tags:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        if deduped != old_tags:
            await pool.execute(
                "UPDATE knowledge SET tags = $1, updated_at = NOW() WHERE id = $2",
                deduped, row["id"],
            )
            updated += 1
    return _json({"updated_count": updated, "old_tag": old_tag, "new_tag": new_tag})


async def rest_tags_delete(request: Request) -> JSONResponse:
    body = await request.json()
    tag = body.get("tag")
    if not tag:
        return _err("tag is required", 400)

    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, tags FROM knowledge WHERE status = 'active' AND $1 = ANY(tags)",
        tag,
    )
    updated = 0
    for row in rows:
        old_tags = list(row["tags"])
        new_tags = [t for t in old_tags if t != tag]
        if new_tags != old_tags:
            await pool.execute(
                "UPDATE knowledge SET tags = $1, updated_at = NOW() WHERE id = $2",
                new_tags, row["id"],
            )
            updated += 1
    return _json({"updated_count": updated, "deleted_tag": tag})
```

- [ ] **Step 2: Register the routes**

Add three lines to the `rest_routes` list (around line 1818):

```python
rest_routes = [
    Route("/api/knowledge", rest_knowledge_create, methods=["POST"]),
    # ... existing routes ...
    Route("/api/bulk-delete", rest_bulk_delete, methods=["DELETE"]),
    Route("/api/tags/merge", rest_tags_merge, methods=["POST"]),
    Route("/api/tags/rename", rest_tags_rename, methods=["POST"]),
    Route("/api/tags/delete", rest_tags_delete, methods=["POST"]),
]
```

- [ ] **Step 3: Rebuild and test with curl**

```bash
docker compose up -d --build mcp-gateway
```

Test merge:

```bash
curl -s -X POST http://localhost:8000/api/tags/merge \
  -H "Content-Type: application/json" \
  -d '{"source_tags": ["error-model"], "target_tag": "error-model-test"}' | jq .
```

Expected: `{ "updated_count": 13, "target_tag": "error-model-test" }`

Reverse it:

```bash
curl -s -X POST http://localhost:8000/api/tags/rename \
  -H "Content-Type: application/json" \
  -d '{"old_tag": "error-model-test", "new_tag": "error-model"}' | jq .
```

Test delete (use a safe tag):

```bash
curl -s -X POST http://localhost:8000/api/tags/delete \
  -H "Content-Type: application/json" \
  -d '{"tag": "some-unused-tag"}' | jq .
```

Expected: `{ "updated_count": 0, "deleted_tag": "some-unused-tag" }`

Test validation:

```bash
curl -s -X POST http://localhost:8000/api/tags/merge \
  -H "Content-Type: application/json" \
  -d '{"source_tags": [], "target_tag": "x"}' | jq .
```

Expected: 400 error.

- [ ] **Step 4: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add tag merge/rename/delete REST endpoints"
```

---

### Task 3: Add API Functions to Frontend

**Files:**
- Modify: `web-ui/static/js/lib/api.js` (add after the existing write functions, around line 180)

- [ ] **Step 1: Add the tag API functions**

Add to `api.js` after the existing write functions:

```javascript
// --- Tag Stats (Read via PostgREST) ---

export function fetchTagStats(params = '') {
    return request(`${READ_BASE}/tag_stats?${params}`);
}

// --- Tag Management (Write) ---

export function mergeTags(sourceTags, targetTag) {
    return request(`${WRITE_BASE}/tags/merge`, {
        method: 'POST',
        body: JSON.stringify({ source_tags: sourceTags, target_tag: targetTag }),
    });
}

export function renameTag(oldTag, newTag) {
    return request(`${WRITE_BASE}/tags/rename`, {
        method: 'POST',
        body: JSON.stringify({ old_tag: oldTag, new_tag: newTag }),
    });
}

export function deleteTag(tag) {
    return request(`${WRITE_BASE}/tags/delete`, {
        method: 'POST',
        body: JSON.stringify({ tag }),
    });
}
```

- [ ] **Step 2: Commit**

```bash
git add web-ui/static/js/lib/api.js
git commit -m "feat: add tag stats and management API functions"
```

---

### Task 4: Build TagCloudWidget Component

**Files:**
- Create: `web-ui/static/js/components/tag-cloud.js`
- Modify: `web-ui/static/css/style.css`

- [ ] **Step 1: Create the component**

Create `web-ui/static/js/components/tag-cloud.js`:

```javascript
import { h } from 'preact';
import htm from 'htm';
import { navigate } from '../lib/state.js';

const html = htm.bind(h);

function computeFontSize(count, minCount, maxCount) {
    if (maxCount === minCount) return 16;
    const minSize = 12;
    const maxSize = 26;
    // Logarithmic scale for better distribution
    const logMin = Math.log(minCount || 1);
    const logMax = Math.log(maxCount || 1);
    const logCount = Math.log(count || 1);
    const ratio = logMax === logMin ? 0.5 : (logCount - logMin) / (logMax - logMin);
    return Math.round(minSize + ratio * (maxSize - minSize));
}

function computeOpacity(count, maxCount) {
    const min = 0.45;
    const max = 1.0;
    if (!maxCount) return max;
    const ratio = Math.log(count || 1) / Math.log(maxCount || 1);
    return min + ratio * (max - min);
}

export function TagCloudWidget({ tags = [], limit, onTagClick, showViewAll = false, totalCount = 0 }) {
    if (!tags || tags.length === 0) {
        return html`<div style="color:var(--text-3);font-size:13px;">No tags yet</div>`;
    }

    const displayed = limit ? tags.slice(0, limit) : tags;
    const counts = displayed.map(t => t.entry_count);
    const minCount = Math.min(...counts);
    const maxCount = Math.max(...counts);

    function handleClick(tag) {
        if (onTagClick) {
            onTagClick(tag);
        } else {
            navigate('#/knowledge?tag=' + encodeURIComponent(tag));
        }
    }

    return html`
        <div>
            <div class="tag-cloud">
                ${displayed.map(t => {
                    const size = computeFontSize(t.entry_count, minCount, maxCount);
                    const opacity = computeOpacity(t.entry_count, maxCount);
                    return html`
                        <a key=${t.tag}
                           class="tag-cloud-item"
                           style=${'font-size:' + size + 'px;opacity:' + opacity.toFixed(2)}
                           onClick=${() => handleClick(t.tag)}
                           title=${t.tag + ' (' + t.entry_count + ' entries)'}>
                            ${t.tag}
                        </a>
                    `;
                })}
            </div>
            ${showViewAll && html`
                <div style="margin-top:12px;text-align:right;">
                    <a class="link-subtle" onClick=${() => navigate('#/tags')}>
                        View all ${totalCount || tags.length} tags →
                    </a>
                </div>
            `}
        </div>
    `;
}
```

- [ ] **Step 2: Add CSS styles**

Add to the end of `web-ui/static/css/style.css`:

```css
/* --- Tag Cloud --- */
.tag-cloud {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: baseline;
    line-height: 2;
}
.tag-cloud-item {
    color: var(--tag-text);
    cursor: pointer;
    text-decoration: none;
    font-weight: 500;
    transition: opacity 0.15s;
}
.tag-cloud-item:hover {
    opacity: 1 !important;
    text-decoration: underline;
}
.link-subtle {
    font-size: 12px;
    color: var(--accent);
    cursor: pointer;
    text-decoration: none;
}
.link-subtle:hover {
    text-decoration: underline;
}

/* --- Tags Page --- */
.tags-filter-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
}
.tags-filter-bar .filter-pills {
    display: flex;
    gap: 6px;
}
.filter-pill {
    background: var(--surface);
    border: 1px solid transparent;
    padding: 4px 12px;
    border-radius: 16px;
    font-size: 12px;
    color: var(--text-2);
    cursor: pointer;
}
.filter-pill.active {
    border-color: var(--accent);
    color: var(--text-1);
}
.tags-table {
    width: 100%;
    border-collapse: collapse;
}
.tags-table th {
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    color: var(--text-3);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
}
.tags-table td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border-subtle);
    font-size: 13px;
}
.tags-table tr:hover {
    background: rgba(255,255,255,0.02);
}
.tags-table tr.selected {
    background: var(--surface);
}
.tags-table .tag-name {
    color: var(--tag-text);
    cursor: pointer;
}
.tags-table .tag-name:hover {
    text-decoration: underline;
}
.bulk-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    font-size: 12px;
}
.bulk-bar .actions {
    display: flex;
    gap: 8px;
}
```

- [ ] **Step 3: Commit**

```bash
git add web-ui/static/js/components/tag-cloud.js web-ui/static/css/style.css
git commit -m "feat: add TagCloudWidget component and tag styles"
```

---

### Task 5: Integrate Tag Cloud into Dashboard

**Files:**
- Modify: `web-ui/static/js/pages/dashboard.js`

- [ ] **Step 1: Add import and state**

At the top of `dashboard.js`, add the import:

```javascript
import { TagCloudWidget } from '../components/tag-cloud.js';
```

Add to the existing imports from api.js:

```javascript
import { fetchCounts, fetchArchivedCounts, readRecentActivity, readOrphanedItems, fetchTagStats } from '../lib/api.js';
```

- [ ] **Step 2: Add tag state and fetch**

Inside `DashboardPage()`, add a new state variable after the existing ones (around line 24):

```javascript
const [topTags, setTopTags] = useState([]);
```

In the `Promise.all` block (around line 29), add a fifth fetch:

```javascript
Promise.all([
    fetchCounts().catch(() => ({ knowledge: 0, memories: 0, projects: 0 })),
    fetchArchivedCounts().catch(() => ({ knowledge: 0, memories: 0, projects: 0 })),
    readRecentActivity(10).catch(() => []),
    readOrphanedItems().catch(() => []),
    fetchTagStats('order=entry_count.desc&limit=20').catch(() => []),
]).then(([c, ac, act, orph, tags]) => {
    if (!cancelled) {
        setCounts(c);
        setArchivedCounts(ac);
        setActivity(Array.isArray(act) ? act : []);
        setOrphans(Array.isArray(orph) ? orph : []);
        setTopTags(Array.isArray(tags) ? tags : []);
        setLoading(false);
    }
});
```

- [ ] **Step 3: Render the widget**

In the return JSX, add the tag cloud section between the `stats-grid` div (closing around line 77) and the `two-col` div (opening around line 79):

```javascript
<div class="card" style="margin-bottom:20px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <span style="font-size:14px;font-weight:600;">Top Tags</span>
    </div>
    <${TagCloudWidget}
        tags=${topTags}
        limit=${20}
        showViewAll=${true}
        totalCount=${0}
    />
</div>
```

- [ ] **Step 4: Test in browser**

```bash
docker compose up -d --build
```

Open `http://localhost:8080` (or your dashboard URL). Verify:
- Tag cloud appears below stats cards
- Tags are sized by count (ISCWSA largest)
- Clicking a tag navigates to `#/knowledge?tag=ISCWSA`
- "View all tags →" link navigates to `#/tags` (will 404 until Task 7)

- [ ] **Step 5: Commit**

```bash
git add web-ui/static/js/pages/dashboard.js
git commit -m "feat: add top tags cloud widget to dashboard"
```

---

### Task 6: Build the Tags Management Page

**Files:**
- Create: `web-ui/static/js/pages/tags.js`

- [ ] **Step 1: Create the full Tags page**

Create `web-ui/static/js/pages/tags.js`:

```javascript
import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { fetchTagStats, mergeTags, renameTag, deleteTag } from '../lib/api.js';
import { navigate, addToast } from '../lib/state.js';
import { TagCloudWidget } from '../components/tag-cloud.js';
import { Modal } from '../components/modal.js';

const html = htm.bind(h);

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const diff = Math.max(0, Date.now() - new Date(dateStr).getTime());
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
}

export function TagsPage() {
    const [allTags, setAllTags] = useState([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState('');
    const [filter, setFilter] = useState('all'); // 'all' | 'once' | 'multi'
    const [sort, setSort] = useState('count'); // 'count' | 'alpha'
    const [selected, setSelected] = useState(new Set());
    const [modal, setModal] = useState(null); // null | 'merge' | 'rename' | 'delete'
    const [mergeTarget, setMergeTarget] = useState('');

    async function loadTags() {
        setLoading(true);
        try {
            const data = await fetchTagStats('order=entry_count.desc,tag.asc');
            setAllTags(Array.isArray(data) ? data : []);
        } catch (_) {
            setAllTags([]);
        } finally {
            setLoading(false);
        }
    }

    useEffect(() => { loadTags(); }, []);

    // Filtering and sorting
    let filtered = allTags;
    if (search) {
        const q = search.toLowerCase();
        filtered = filtered.filter(t => t.tag.toLowerCase().includes(q));
    }
    if (filter === 'once') filtered = filtered.filter(t => t.entry_count === 1);
    if (filter === 'multi') filtered = filtered.filter(t => t.entry_count > 1);
    if (sort === 'alpha') {
        filtered = [...filtered].sort((a, b) => a.tag.localeCompare(b.tag));
    }

    const counts = { all: allTags.length, once: allTags.filter(t => t.entry_count === 1).length, multi: allTags.filter(t => t.entry_count > 1).length };

    function toggleSelect(tag) {
        const next = new Set(selected);
        if (next.has(tag)) next.delete(tag); else next.add(tag);
        setSelected(next);
    }

    function toggleSelectAll() {
        if (selected.size === filtered.length) {
            setSelected(new Set());
        } else {
            setSelected(new Set(filtered.map(t => t.tag)));
        }
    }

    function openMerge() {
        // Pre-populate target with the most-used selected tag
        const selectedTags = allTags.filter(t => selected.has(t.tag));
        selectedTags.sort((a, b) => b.entry_count - a.entry_count);
        const best = selectedTags[0]?.tag || '';
        setMergeTarget(best.toLowerCase().replace(/\s+/g, '-'));
        setModal('merge');
    }

    function openRename() {
        const tag = [...selected][0] || '';
        setMergeTarget(tag);
        setModal('rename');
    }

    async function handleMerge() {
        const sources = [...selected];
        try {
            const result = await mergeTags(sources, mergeTarget);
            addToast(`Merged ${sources.length} tags → "${mergeTarget}" (${result.updated_count} entries updated)`, 'success');
            setSelected(new Set());
            setModal(null);
            loadTags();
        } catch (_) { /* toast handled by api.js */ }
    }

    async function handleRename() {
        const oldTag = [...selected][0];
        try {
            const result = await renameTag(oldTag, mergeTarget);
            addToast(`Renamed "${oldTag}" → "${mergeTarget}" (${result.updated_count} entries updated)`, 'success');
            setSelected(new Set());
            setModal(null);
            loadTags();
        } catch (_) {}
    }

    async function handleDelete() {
        const tags = [...selected];
        let total = 0;
        for (const tag of tags) {
            try {
                const result = await deleteTag(tag);
                total += result.updated_count;
            } catch (_) {}
        }
        addToast(`Deleted ${tags.length} tag(s) from ${total} entries`, 'success');
        setSelected(new Set());
        setModal(null);
        loadTags();
    }

    // Count affected entries for modal display
    const selectedEntryCount = allTags
        .filter(t => selected.has(t.tag))
        .reduce((sum, t) => sum + t.entry_count, 0);

    if (loading) {
        return html`<div class="loading-center"><div class="spinner"></div></div>`;
    }

    return html`
        <div>
            <div class="page-header"><h1 class="page-title">Tags</h1></div>

            <!-- Filter bar -->
            <div class="tags-filter-bar">
                <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
                    <input
                        class="input"
                        style="width:240px;"
                        placeholder="Search tags..."
                        value=${search}
                        onInput=${e => setSearch(e.target.value)}
                    />
                    <div class="filter-pills">
                        <span class=${'filter-pill' + (filter === 'all' ? ' active' : '')} onClick=${() => setFilter('all')}>All (${counts.all})</span>
                        <span class=${'filter-pill' + (filter === 'once' ? ' active' : '')} onClick=${() => setFilter('once')}>Used once (${counts.once})</span>
                        <span class=${'filter-pill' + (filter === 'multi' ? ' active' : '')} onClick=${() => setFilter('multi')}>Multi-use (${counts.multi})</span>
                    </div>
                </div>
                <div style="display:flex;gap:6px;align-items:center;">
                    <span style="font-size:11px;color:var(--text-3);">Sort:</span>
                    <span class=${'filter-pill' + (sort === 'count' ? ' active' : '')} onClick=${() => setSort('count')}>Count ↓</span>
                    <span class=${'filter-pill' + (sort === 'alpha' ? ' active' : '')} onClick=${() => setSort('alpha')}>A–Z</span>
                </div>
            </div>

            <!-- Tag cloud -->
            <div class="card" style="margin-bottom:20px;">
                <${TagCloudWidget} tags=${filtered} />
            </div>

            <!-- Bulk action bar -->
            ${selected.size > 0 && html`
                <div class="bulk-bar">
                    <span>${selected.size} tag${selected.size > 1 ? 's' : ''} selected</span>
                    <div class="actions">
                        ${selected.size >= 2 && html`
                            <button class="btn btn-sm btn-primary" onClick=${openMerge}>Merge into one</button>
                        `}
                        ${selected.size === 1 && html`
                            <button class="btn btn-sm btn-secondary" onClick=${openRename}>Rename</button>
                        `}
                        <button class="btn btn-sm btn-danger" onClick=${() => setModal('delete')}>Delete</button>
                    </div>
                </div>
            `}

            <!-- Tag table -->
            <div class="card" style="padding:0;overflow:hidden;">
                <table class="tags-table">
                    <thead>
                        <tr>
                            <th style="width:32px;">
                                <input type="checkbox"
                                    checked=${selected.size === filtered.length && filtered.length > 0}
                                    onChange=${toggleSelectAll} />
                            </th>
                            <th>Tag</th>
                            <th style="width:80px;">Entries</th>
                            <th style="width:120px;">Last used</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${filtered.map(t => html`
                            <tr key=${t.tag} class=${selected.has(t.tag) ? 'selected' : ''}>
                                <td>
                                    <input type="checkbox"
                                        checked=${selected.has(t.tag)}
                                        onChange=${() => toggleSelect(t.tag)} />
                                </td>
                                <td>
                                    <span class="tag-name"
                                        onClick=${() => navigate('#/knowledge?tag=' + encodeURIComponent(t.tag))}>
                                        ${t.tag}
                                    </span>
                                </td>
                                <td>${t.entry_count}</td>
                                <td style="color:var(--text-3);font-size:12px;">${timeAgo(t.last_used)}</td>
                            </tr>
                        `)}
                    </tbody>
                </table>
                ${filtered.length === 0 && html`
                    <div style="padding:20px;text-align:center;color:var(--text-3);font-size:13px;">
                        ${search ? 'No tags match your search' : 'No tags found'}
                    </div>
                `}
            </div>

            <!-- Merge Modal -->
            ${modal === 'merge' && html`
                <${Modal} title="Merge Tags" onClose=${() => setModal(null)} actions=${html`
                    <button class="btn btn-secondary" onClick=${() => setModal(null)}>Cancel</button>
                    <button class="btn btn-primary" onClick=${handleMerge} disabled=${!mergeTarget.trim()}>Merge tags</button>
                `}>
                    <div style="margin-bottom:12px;">
                        <div style="font-size:12px;color:var(--text-3);margin-bottom:6px;">Source tags (will be removed):</div>
                        <div class="flex flex-wrap gap-4">
                            ${[...selected].map(t => html`
                                <span key=${t} class="chip chip-tag" style="text-decoration:line-through;opacity:0.6;">${t}</span>
                            `)}
                        </div>
                    </div>
                    <div style="margin-bottom:12px;">
                        <div style="font-size:12px;color:var(--text-3);margin-bottom:6px;">Merge into:</div>
                        <input class="input" style="width:100%;" value=${mergeTarget} onInput=${e => setMergeTarget(e.target.value)} />
                    </div>
                    <div style="padding:10px 12px;background:var(--bg);border-radius:6px;font-size:12px;color:var(--text-2);">
                        This will update up to ${selectedEntryCount} knowledge entries.
                    </div>
                </${Modal}>
            `}

            <!-- Rename Modal -->
            ${modal === 'rename' && html`
                <${Modal} title="Rename Tag" onClose=${() => setModal(null)} actions=${html`
                    <button class="btn btn-secondary" onClick=${() => setModal(null)}>Cancel</button>
                    <button class="btn btn-primary" onClick=${handleRename} disabled=${!mergeTarget.trim()}>Rename</button>
                `}>
                    <div style="margin-bottom:12px;">
                        <div style="font-size:12px;color:var(--text-3);margin-bottom:6px;">Current name:</div>
                        <span class="chip chip-tag">${[...selected][0]}</span>
                    </div>
                    <div style="margin-bottom:12px;">
                        <div style="font-size:12px;color:var(--text-3);margin-bottom:6px;">New name:</div>
                        <input class="input" style="width:100%;" value=${mergeTarget} onInput=${e => setMergeTarget(e.target.value)} />
                    </div>
                    <div style="padding:10px 12px;background:var(--bg);border-radius:6px;font-size:12px;color:var(--text-2);">
                        This will update ${selectedEntryCount} knowledge entries.
                    </div>
                </${Modal}>
            `}

            <!-- Delete Modal -->
            ${modal === 'delete' && html`
                <${Modal} title="Delete Tags" onClose=${() => setModal(null)} actions=${html`
                    <button class="btn btn-secondary" onClick=${() => setModal(null)}>Cancel</button>
                    <button class="btn btn-danger" onClick=${handleDelete}>Delete</button>
                `}>
                    <div style="margin-bottom:12px;">
                        <div style="font-size:12px;color:var(--text-3);margin-bottom:6px;">Tags to delete:</div>
                        <div class="flex flex-wrap gap-4">
                            ${[...selected].map(t => html`
                                <span key=${t} class="chip chip-tag" style="opacity:0.6;">${t}</span>
                            `)}
                        </div>
                    </div>
                    <div style="padding:10px 12px;background:var(--bg);border-radius:6px;font-size:12px;color:var(--warning);">
                        This will remove ${selected.size} tag${selected.size > 1 ? 's' : ''} from up to ${selectedEntryCount} knowledge entries.
                    </div>
                </${Modal}>
            `}
        </div>
    `;
}
```

- [ ] **Step 2: Commit**

```bash
git add web-ui/static/js/pages/tags.js
git commit -m "feat: add Tags management page with cloud, table, and bulk actions"
```

---

### Task 7: Wire Up Navigation and Route

**Files:**
- Modify: `web-ui/static/js/app.js`
- Modify: `web-ui/static/js/components/sidebar.js`

- [ ] **Step 1: Add route to app.js**

In `app.js`, add the import at the top (around line 14):

```javascript
import { TagsPage } from './pages/tags.js';
```

Add a case to the router switch (around line 33, before `default`):

```javascript
case 'tags': return html`<${TagsPage} query=${query} />`;
```

- [ ] **Step 2: Add nav item to sidebar.js**

In `sidebar.js`, add Tags to the `NAV_ITEMS` array (around line 12):

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

- [ ] **Step 3: Test in browser**

Rebuild and open the dashboard:

```bash
docker compose up -d --build
```

Verify:
- "Tags" appears in the sidebar navigation
- Clicking it navigates to `#/tags`
- Tags page loads with cloud, filter bar, and table
- Clicking a tag in the cloud navigates to `#/knowledge?tag={tag}`
- Select 2 tags → Merge button appears → merge dialog works
- Select 1 tag → Rename button appears → rename dialog works
- Select any tags → Delete button appears → delete dialog works
- Dashboard "View all tags →" link navigates to `#/tags`

- [ ] **Step 4: Commit**

```bash
git add web-ui/static/js/app.js web-ui/static/js/components/sidebar.js
git commit -m "feat: wire up Tags page route and sidebar navigation"
```

---

### Task 8: End-to-End Testing

- [ ] **Step 1: Full rebuild**

```bash
docker compose down && docker compose up -d --build
```

- [ ] **Step 2: Verify tag_stats view**

```bash
curl -s "http://localhost:3000/tag_stats?limit=5" | jq .
```

Should return top 5 tags with counts.

- [ ] **Step 3: Verify dashboard widget**

Open dashboard in browser. Tag cloud should show top 20 tags sized by count.

- [ ] **Step 4: Verify Tags page**

Navigate to `#/tags`. Verify:
- All 262 tags load in cloud and table
- Search filters both cloud and table
- "Used once" filter shows 195 tags
- "Multi-use" filter shows 67 tags
- Sort toggle switches between count and alphabetical

- [ ] **Step 5: Test merge flow**

1. Select two tags in the table (e.g., `ISCWSA` and any other)
2. Click "Merge into one"
3. Verify modal shows source tags, editable target, and affected count
4. Cancel (don't actually merge ISCWSA in production data)

- [ ] **Step 6: Test rename flow**

1. Select one tag
2. Click "Rename"
3. Verify modal shows current name and input for new name
4. Cancel

- [ ] **Step 7: Test delete flow**

1. Select one or more tags
2. Click "Delete"
3. Verify warning shows tag count and affected entries
4. Cancel

- [ ] **Step 8: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: tag browsing end-to-end fixes"
```
