import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate, addToast } from '../lib/state.js';
import {
    readKnowledge, readMemories, readProjects,
    unarchiveItem, bulkDelete,
} from '../lib/api.js';
import { ConfirmModal } from '../components/modal.js';

const html = htm.bind(h);

function apiType(t) {
    if (t === 'memories') return 'memory';
    if (t === 'projects') return 'project';
    return t;
}

export function ArchivePage() {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [filter, setFilter] = useState('all');
    const [selected, setSelected] = useState(new Set());
    const [showDeleteModal, setShowDeleteModal] = useState(false);

    useEffect(() => {
        loadArchived();
    }, []);

    async function loadArchived() {
        setLoading(true);
        setSelected(new Set());
        try {
            const [knowledge, memories, projects] = await Promise.all([
                readKnowledge('status=eq.archived&order=updated_at.desc').catch(() => []),
                readMemories('status=eq.archived&order=updated_at.desc').catch(() => []),
                readProjects('status=eq.archived&order=updated_at.desc').catch(() => []),
            ]);

            const all = [];
            (Array.isArray(knowledge) ? knowledge : []).forEach(k =>
                all.push({ ...k, _type: 'knowledge', _key: 'knowledge-' + k.id })
            );
            (Array.isArray(memories) ? memories : []).forEach(m =>
                all.push({ ...m, _type: 'memories', _key: 'memories-' + m.id })
            );
            (Array.isArray(projects) ? projects : []).forEach(p =>
                all.push({ ...p, _type: 'projects', _key: 'projects-' + (p.name || p.id) })
            );

            all.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
            setItems(all);
        } catch (_) {
            setItems([]);
        } finally {
            setLoading(false);
        }
    }

    function filteredItems() {
        if (filter === 'all') return items;
        return items.filter(i => i._type === filter);
    }

    function countByType(type) {
        return items.filter(i => i._type === type).length;
    }

    function toggleSelect(key) {
        setSelected(prev => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            return next;
        });
    }

    function toggleAll() {
        const visible = filteredItems();
        const allSelected = visible.every(i => selected.has(i._key));
        if (allSelected) {
            setSelected(prev => {
                const next = new Set(prev);
                visible.forEach(i => next.delete(i._key));
                return next;
            });
        } else {
            setSelected(prev => {
                const next = new Set(prev);
                visible.forEach(i => next.add(i._key));
                return next;
            });
        }
    }

    function getSelectedItems() {
        return items.filter(i => selected.has(i._key));
    }

    async function handleRestore(item) {
        try {
            const id = item._type === 'projects' ? (item.name || item.id) : item.id;
            await unarchiveItem(apiType(item._type), id);
            addToast('Item restored.', 'success');
            await loadArchived();
        } catch (_) {}
    }

    async function handleRestoreSelected() {
        const toRestore = getSelectedItems();
        try {
            for (const item of toRestore) {
                const id = item._type === 'projects' ? (item.name || item.id) : item.id;
                await unarchiveItem(apiType(item._type), id);
            }
            addToast(`${toRestore.length} item${toRestore.length !== 1 ? 's' : ''} restored.`, 'success');
            await loadArchived();
        } catch (_) {}
    }

    async function handleBulkDelete() {
        const toDelete = getSelectedItems().map(item => ({
            type: apiType(item._type),
            id: item._type === 'projects' ? (item.name || item.id) : item.id,
        }));
        try {
            await bulkDelete(toDelete);
            addToast(`${toDelete.length} item${toDelete.length !== 1 ? 's' : ''} permanently deleted.`, 'success');
            setShowDeleteModal(false);
            await loadArchived();
        } catch (_) {
            setShowDeleteModal(false);
        }
    }

    if (loading) {
        return html`
            <div>
                <h1 class="page-title">Archive</h1>
                <div class="loading-center"><div class="spinner"></div></div>
            </div>
        `;
    }

    const visible = filteredItems();
    const selectedCount = selected.size;

    return html`
        <div>
            <h1 class="page-title">Archive</h1>

            <div class="tabs">
                <span class=${'tab' + (filter === 'all' ? ' active' : '')} onClick=${() => setFilter('all')}>
                    All <span class="tab-count">${items.length}</span>
                </span>
                <span class=${'tab' + (filter === 'knowledge' ? ' active' : '')} onClick=${() => setFilter('knowledge')}>
                    Knowledge <span class="tab-count">${countByType('knowledge')}</span>
                </span>
                <span class=${'tab' + (filter === 'memories' ? ' active' : '')} onClick=${() => setFilter('memories')}>
                    Memories <span class="tab-count">${countByType('memories')}</span>
                </span>
                <span class=${'tab' + (filter === 'projects' ? ' active' : '')} onClick=${() => setFilter('projects')}>
                    Projects <span class="tab-count">${countByType('projects')}</span>
                </span>
            </div>

            ${selectedCount > 0 && html`
                <div class="bulk-bar">
                    <span>${selectedCount} selected</span>
                    <div class="flex gap-8">
                        <button class="btn btn-secondary btn-sm" onClick=${handleRestoreSelected}>Restore Selected</button>
                        <button class="btn btn-danger btn-sm" onClick=${() => setShowDeleteModal(true)}>Permanently Delete</button>
                    </div>
                </div>
            `}

            ${visible.length === 0 && html`
                <div class="empty-state">No archived items.</div>
            `}

            ${visible.length > 0 && html`
                <div>
                    <div class="card archive-row flex items-center gap-12" style="padding:10px 16px; margin-bottom:4px; font-size:11px; text-transform:uppercase; color:var(--text-3);">
                        <input
                            type="checkbox"
                            checked=${visible.length > 0 && visible.every(i => selected.has(i._key))}
                            onChange=${toggleAll}
                            style="width:16px; height:16px; cursor:pointer;"
                        />
                        <span class="archive-col-type" style="width:90px;">Type</span>
                        <span style="flex:1;">Title</span>
                        <span class="archive-col-date" style="width:100px;">Archived</span>
                        <span class="archive-col-prov" style="width:100px;">Provenance</span>
                        <span class="archive-col-action" style="width:80px;"></span>
                    </div>
                    ${visible.map(item => html`
                        <div key=${item._key} class="card archive-row flex items-center gap-12" style="padding:10px 16px; margin-bottom:4px;">
                            <input
                                type="checkbox"
                                checked=${selected.has(item._key)}
                                onChange=${() => toggleSelect(item._key)}
                                style="width:16px; height:16px; cursor:pointer;"
                            />
                            <span class="archive-col-type" style="width:90px;">
                                <span class=${'badge badge-' + item._type}>${item._type}</span>
                            </span>
                            <span style="flex:1; font-size:13px; font-weight:500;">${item.title || item.name || 'Untitled'}</span>
                            <span class="archive-col-date" style="width:100px; font-size:12px; color:var(--text-3);">
                                ${item.updated_at ? new Date(item.updated_at).toLocaleDateString() : '-'}
                            </span>
                            <span class="archive-col-prov" style="width:100px; font-size:12px; color:var(--text-3);">${item.provenance || '-'}</span>
                            <span class="archive-col-action" style="width:80px;">
                                <button class="btn btn-sm btn-secondary" onClick=${() => handleRestore(item)}>Restore</button>
                            </span>
                        </div>
                    `)}
                </div>
            `}

            ${showDeleteModal && html`
                <${ConfirmModal}
                    title="Permanently Delete"
                    message=${`Are you sure you want to permanently delete ${selectedCount} item${selectedCount !== 1 ? 's' : ''}? This cannot be undone.`}
                    confirmLabel="Delete Forever"
                    onConfirm=${handleBulkDelete}
                    onCancel=${() => setShowDeleteModal(false)}
                />
            `}
        </div>
    `;
}
