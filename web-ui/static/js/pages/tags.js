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
    const [sortBy, setSortBy] = useState('count'); // 'count' | 'tag'
    const [sortDir, setSortDir] = useState('desc'); // 'asc' | 'desc'
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
    filtered = [...filtered].sort((a, b) => {
        let cmp;
        if (sortBy === 'tag') {
            cmp = a.tag.localeCompare(b.tag);
        } else {
            cmp = a.entry_count - b.entry_count;
        }
        return sortDir === 'asc' ? cmp : -cmp;
    });

    const counts = { all: allTags.length, once: allTags.filter(t => t.entry_count === 1).length, multi: allTags.filter(t => t.entry_count > 1).length };

    function toggleSort(col) {
        if (sortBy === col) {
            setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
        } else {
            setSortBy(col);
            setSortDir(col === 'tag' ? 'asc' : 'desc');
        }
    }

    function sortArrow(col) {
        if (sortBy !== col) return '';
        return sortDir === 'asc' ? ' ↑' : ' ↓';
    }

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
                            <th class="sortable-th" onClick=${() => toggleSort('tag')}>Tag${sortArrow('tag')}</th>
                            <th class="sortable-th" style="width:80px;" onClick=${() => toggleSort('count')}>Entries${sortArrow('count')}</th>
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
