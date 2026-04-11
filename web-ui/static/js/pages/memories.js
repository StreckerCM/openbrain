import { h, Fragment } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate, addToast } from '../lib/state.js';
import {
    readMemories, readMemoryById, createMemory, updateMemory,
    archiveItem, linkToProject, unlinkFromProject, readProjects,
} from '../lib/api.js';
import { renderMarkdown } from '../lib/markdown.js';
import { EntityList } from '../components/entity-list.js';
import { EntityForm } from '../components/entity-form.js';
import { ProjectChips } from '../components/tag-chips.js';

const html = htm.bind(h);

const MEMORY_TYPES = ['user', 'feedback', 'project', 'reference'];

const TYPE_COLORS = {
    user: 'user',
    feedback: 'feedback',
    project: 'project',
    reference: 'reference',
};

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const diff = Math.max(0, Date.now() - new Date(dateStr).getTime());
    const days = Math.floor(diff / 86400000);
    if (days > 0) return days === 1 ? '1d ago' : `${days}d ago`;
    const hours = Math.floor(diff / 3600000);
    if (hours > 0) return `${hours}h ago`;
    const mins = Math.floor(diff / 60000);
    return mins > 0 ? `${mins}m ago` : 'now';
}

// --- List View ---

function MemoriesList() {
    const [allProjects, setAllProjects] = useState([]);

    useEffect(() => {
        readProjects('status=in.(active,system)&order=name.asc')
            .then(data => setAllProjects(Array.isArray(data) ? data : []))
            .catch(() => {});
    }, []);

    function fetchFn(page, pageSize, filters) {
        const params = ['status=eq.active', 'order=updated_at.desc'];
        const offset = (page - 1) * pageSize;
        params.push(`offset=${offset}`, `limit=${pageSize}`);

        if (filters.search) {
            params.push(`or=(name.ilike.*${encodeURIComponent(filters.search)}*,description.ilike.*${encodeURIComponent(filters.search)}*)`);
        }
        if (filters.memory_type) {
            params.push(`memory_type=eq.${encodeURIComponent(filters.memory_type)}`);
        }
        if (filters.project) {
            params.push(`projects=cs.{${encodeURIComponent(filters.project)}}`);
        }
        return readMemories(params.join('&'));
    }

    const columns = [
        {
            label: 'Name',
            render: item => html`
                <div>
                    <div style="font-weight:500;">${item.name || 'Untitled'}</div>
                    ${item.description && html`
                        <div style="font-size:12px; color:var(--text-3); margin-top:2px;">${item.description.slice(0, 80)}${item.description.length > 80 ? '...' : ''}</div>
                    `}
                    ${(!item.projects || item.projects.length === 0) && html`
                        <span class="badge badge-warning" style="font-size:10px;">orphan</span>
                    `}
                </div>
            `,
        },
        {
            label: 'Type',
            render: item => {
                const color = TYPE_COLORS[item.memory_type] || 'blue';
                return html`<span class=${'badge badge-' + color}>${item.memory_type || '-'}</span>`;
            },
        },
        {
            label: 'Projects',
            render: item => html`
                <div class="flex flex-wrap gap-4">
                    ${(item.projects || []).map(p => html`
                        <span key=${p} class="chip chip-project" style="font-size:11px;">${p}</span>
                    `)}
                </div>
            `,
        },
        {
            label: 'Updated',
            render: item => timeAgo(item.updated_at),
        },
    ];

    function filters(values, update) {
        return html`
            <${Fragment}>
                <input
                    class="filter-input"
                    type="text"
                    placeholder="Search..."
                    value=${values.search || ''}
                    onInput=${e => update('search', e.target.value)}
                />
                <select class="filter-select" value=${values.memory_type || ''} onChange=${e => update('memory_type', e.target.value)}>
                    <option value="">All types</option>
                    ${MEMORY_TYPES.map(t => html`<option key=${t} value=${t}>${t}</option>`)}
                </select>
                <select class="filter-select" value=${values.project || ''} onChange=${e => update('project', e.target.value)}>
                    <option value="">All projects</option>
                    ${allProjects.map(p => html`<option key=${p.name} value=${p.name}>${p.name}</option>`)}
                </select>
                <button class="btn btn-primary" onClick=${() => navigate('#/memories/new')}>+ New Memory</button>
            <//>
        `;
    }

    return html`
        <div>
            <h1 class="page-title">Memories</h1>
            <${EntityList}
                fetchFn=${fetchFn}
                columns=${columns}
                gridTemplate="2.5fr 0.8fr 1.5fr 0.8fr"
                detailRoute=${item => '#/memories/' + item.id}
                filters=${filters}
                emptyMessage="No memories found."
            />
        </div>
    `;
}

// --- Detail View ---

function MemoryDetail({ id }) {
    const [item, setItem] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        readMemoryById(id).then(data => {
            if (!cancelled) {
                const entry = Array.isArray(data) ? data[0] : data;
                setItem(entry || null);
                setLoading(false);
            }
        }).catch(() => { if (!cancelled) { setItem(null); setLoading(false); } });
        return () => { cancelled = true; };
    }, [id]);

    async function handleArchive() {
        try {
            await archiveItem('memory', id);
            addToast('Memory archived.', 'success');
            navigate('#/memories');
        } catch (_) {}
    }

    async function handleUnlink(project) {
        try {
            await unlinkFromProject(project, null, id);
            addToast('Unlinked from ' + project, 'success');
            const data = await readMemoryById(id);
            setItem(Array.isArray(data) ? data[0] : data);
        } catch (_) {}
    }

    async function handleLink(project) {
        try {
            await linkToProject(project, null, id);
            addToast('Linked to ' + project, 'success');
            const data = await readMemoryById(id);
            setItem(Array.isArray(data) ? data[0] : data);
        } catch (_) {}
    }

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;
    if (!item) return html`<div class="empty-state">Memory not found.</div>`;

    const projects = item.projects || [];
    const typeColor = TYPE_COLORS[item.memory_type] || 'blue';

    return html`
        <div>
            <div class="breadcrumb">
                <a href="#/memories" onClick=${e => { e.preventDefault(); navigate('#/memories'); }}>Memories</a>
                <span> / ${item.name || 'Untitled'}</span>
            </div>

            <div class="detail-header">
                <h1 class="page-title">${item.name || 'Untitled'}</h1>
                <div class="detail-actions">
                    <button class="btn btn-secondary" onClick=${() => navigate('#/memories/' + id + '/edit')}>Edit</button>
                    <button class="btn btn-danger" onClick=${handleArchive}>Archive</button>
                </div>
            </div>

            <div class="meta-row">
                <span class=${'badge badge-' + typeColor}>${item.memory_type || '-'}</span>
                ${item.provenance && html`<span class="meta-item">Provenance: ${item.provenance}</span>`}
                <span class="meta-item">Created: ${new Date(item.created_at).toLocaleDateString()}</span>
                <span class="meta-item">Updated: ${new Date(item.updated_at).toLocaleDateString()}</span>
            </div>

            ${item.description && html`
                <div style="margin:16px 0; color:var(--text-2); font-size:14px;">${item.description}</div>
            `}

            <div style="margin:16px 0;">
                <label class="form-label">Projects</label>
                <${ProjectChips}
                    projects=${projects.map(p => typeof p === 'string' ? { name: p } : p)}
                    onRemove=${handleUnlink}
                    onAdd=${handleLink}
                />
            </div>

            ${item.content && html`
                <div class="md-content" dangerouslySetInnerHTML=${{ __html: renderMarkdown(item.content) }}></div>
            `}
        </div>
    `;
}

// --- Create/Edit Form ---

function MemoryForm({ id }) {
    const isEdit = !!id;
    const [values, setValues] = useState({
        memory_type: 'user',
        name: '',
        description: '',
        projects: ['General'],
        content: '',
    });
    const [loading, setLoading] = useState(isEdit);

    useEffect(() => {
        if (!isEdit) return;
        let cancelled = false;
        readMemoryById(id).then(data => {
            if (cancelled) return;
            const entry = Array.isArray(data) ? data[0] : data;
            if (entry) {
                setValues({
                    memory_type: entry.memory_type || 'user',
                    name: entry.name || '',
                    description: entry.description || '',
                    projects: entry.projects || [],
                    content: entry.content || '',
                });
            }
            setLoading(false);
        }).catch(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [id]);

    async function handleSubmit() {
        const payload = {
            memory_type: values.memory_type,
            name: values.name,
            description: values.description || null,
            projects: Array.isArray(values.projects)
                ? values.projects.map(p => p.name || p)
                : [],
            content: values.content,
        };

        try {
            if (isEdit) {
                await updateMemory(id, payload);
                addToast('Memory updated.', 'success');
                navigate('#/memories/' + id);
            } else {
                const result = await createMemory(payload);
                addToast('Memory created.', 'success');
                const newId = result?.id || result?.[0]?.id;
                navigate(newId ? '#/memories/' + newId : '#/memories');
            }
        } catch (_) {}
    }

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;

    const fields = [
        {
            name: 'memory_type', label: 'Type', type: 'select',
            options: MEMORY_TYPES.map(t => ({ value: t, label: t })),
        },
        { name: 'name', label: 'Name', required: true },
        { name: 'description', label: 'Description', placeholder: 'Brief description...' },
        { name: 'projects', label: 'Projects', type: 'projects' },
        { name: 'content', label: 'Content', type: 'markdown' },
    ];

    return html`
        <div>
            <div class="breadcrumb">
                <a href="#/memories" onClick=${e => { e.preventDefault(); navigate('#/memories'); }}>Memories</a>
                <span> / ${isEdit ? 'Edit' : 'New Memory'}</span>
            </div>
            <h1 class="page-title">${isEdit ? 'Edit Memory' : 'New Memory'}</h1>
            <${EntityForm}
                fields=${fields}
                values=${values}
                onChange=${setValues}
                onSubmit=${handleSubmit}
                onCancel=${() => navigate(isEdit ? '#/memories/' + id : '#/memories')}
                submitLabel=${isEdit ? 'Update' : 'Create'}
            />
        </div>
    `;
}

// --- Router ---

export function MemoriesPage({ param = '' }) {
    if (param === 'new') return html`<${MemoryForm} />`;
    if (param.endsWith('/edit')) {
        const id = param.replace('/edit', '');
        return html`<${MemoryForm} id=${id} />`;
    }
    if (param && !isNaN(param)) return html`<${MemoryDetail} id=${param} />`;
    return html`<${MemoriesList} />`;
}
