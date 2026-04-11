import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate, addToast } from '../lib/state.js';
import {
    readKnowledge, readKnowledgeById, createKnowledge, updateKnowledge,
    archiveItem, linkToProject, unlinkFromProject, readProjects,
} from '../lib/api.js';
import { renderMarkdown } from '../lib/markdown.js';
import { EntityList } from '../components/entity-list.js';
import { EntityForm } from '../components/entity-form.js';
import { ProjectChips, TagChips } from '../components/tag-chips.js';

const html = htm.bind(h);

const CATEGORIES = ['general', 'reference', 'tutorial', 'guide'];

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    const diff = Math.max(0, now - then);
    const days = Math.floor(diff / 86400000);
    if (days > 0) return days === 1 ? '1d ago' : `${days}d ago`;
    const hours = Math.floor(diff / 3600000);
    if (hours > 0) return `${hours}h ago`;
    const mins = Math.floor(diff / 60000);
    return mins > 0 ? `${mins}m ago` : 'now';
}

// --- List View ---

function KnowledgeList() {
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
            params.push(`or=(title.ilike.*${encodeURIComponent(filters.search)}*,content.ilike.*${encodeURIComponent(filters.search)}*)`);
        }
        if (filters.category) {
            params.push(`category=eq.${encodeURIComponent(filters.category)}`);
        }
        if (filters.project) {
            params.push(`projects=cs.["${encodeURIComponent(filters.project)}"]`);
        }
        return readKnowledge(params.join('&'));
    }

    const columns = [
        {
            label: 'Title',
            render: item => html`
                <div>
                    <div style="font-weight:500;">${item.title || 'Untitled'}</div>
                    ${(!item.projects || item.projects.length === 0) && html`
                        <span class="badge badge-warning" style="font-size:10px;">orphan</span>
                    `}
                </div>
            `,
        },
        {
            label: 'Category',
            render: item => html`<span class="badge badge-category">${item.category || '-'}</span>`,
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
            <>
                <input
                    class="filter-input"
                    type="text"
                    placeholder="Search..."
                    value=${values.search || ''}
                    onInput=${e => update('search', e.target.value)}
                />
                <select class="filter-select" value=${values.category || ''} onChange=${e => update('category', e.target.value)}>
                    <option value="">All categories</option>
                    ${CATEGORIES.map(c => html`<option key=${c} value=${c}>${c}</option>`)}
                </select>
                <select class="filter-select" value=${values.project || ''} onChange=${e => update('project', e.target.value)}>
                    <option value="">All projects</option>
                    ${allProjects.map(p => html`<option key=${p.name} value=${p.name}>${p.name}</option>`)}
                </select>
                <button class="btn btn-primary" onClick=${() => navigate('#/knowledge/new')}>+ New Entry</button>
            </>
        `;
    }

    return html`
        <div>
            <h1 class="page-title">Knowledge</h1>
            <${EntityList}
                fetchFn=${fetchFn}
                columns=${columns}
                gridTemplate="2fr 1fr 1.5fr 0.8fr"
                detailRoute=${item => '#/knowledge/' + item.id}
                filters=${filters}
                emptyMessage="No knowledge entries found."
            />
        </div>
    `;
}

// --- Detail View ---

function KnowledgeDetail({ id }) {
    const [item, setItem] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        readKnowledgeById(id).then(data => {
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
            await archiveItem('knowledge', id);
            addToast('Entry archived.', 'success');
            navigate('#/knowledge');
        } catch (_) {}
    }

    async function handleUnlink(project) {
        try {
            await unlinkFromProject(project, id, null);
            addToast('Unlinked from ' + project, 'success');
            // Refresh
            const data = await readKnowledgeById(id);
            setItem(Array.isArray(data) ? data[0] : data);
        } catch (_) {}
    }

    async function handleLink(project) {
        try {
            await linkToProject(project, id, null);
            addToast('Linked to ' + project, 'success');
            const data = await readKnowledgeById(id);
            setItem(Array.isArray(data) ? data[0] : data);
        } catch (_) {}
    }

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;
    if (!item) return html`<div class="empty-state">Knowledge entry not found.</div>`;

    const projects = item.projects || [];
    const tags = item.tags || [];

    return html`
        <div>
            <div class="breadcrumb">
                <a href="#/knowledge" onClick=${e => { e.preventDefault(); navigate('#/knowledge'); }}>Knowledge</a>
                <span> / ${item.title || 'Untitled'}</span>
            </div>

            <div class="detail-header">
                <h1 class="page-title">${item.title || 'Untitled'}</h1>
                <div class="detail-actions">
                    <button class="btn btn-secondary" onClick=${() => navigate('#/knowledge/' + id + '/edit')}>Edit</button>
                    <button class="btn btn-danger" onClick=${handleArchive}>Archive</button>
                </div>
            </div>

            <div class="meta-row">
                ${item.category && html`<span class="badge badge-category">${item.category}</span>`}
                ${item.provenance && html`<span class="meta-item">Provenance: ${item.provenance}</span>`}
                ${item.url && html`<a class="meta-item meta-link" href=${item.url} target="_blank" rel="noopener">${item.url}</a>`}
                <span class="meta-item">Created: ${new Date(item.created_at).toLocaleDateString()}</span>
                <span class="meta-item">Updated: ${new Date(item.updated_at).toLocaleDateString()}</span>
            </div>

            <div style="margin:16px 0;">
                <label class="form-label">Projects</label>
                <${ProjectChips}
                    projects=${projects.map(p => typeof p === 'string' ? { name: p } : p)}
                    onRemove=${handleUnlink}
                    onAdd=${handleLink}
                />
            </div>

            ${tags.length > 0 && html`
                <div style="margin:16px 0;">
                    <label class="form-label">Tags</label>
                    <${TagChips} tags=${tags} />
                </div>
            `}

            ${item.content && html`
                <div class="md-content" dangerouslySetInnerHTML=${{ __html: renderMarkdown(item.content) }}></div>
            `}
        </div>
    `;
}

// --- Create/Edit Form ---

function KnowledgeForm({ id }) {
    const isEdit = !!id;
    const [values, setValues] = useState({
        title: '',
        category: 'general',
        url: '',
        projects: ['general'],
        tags: '',
        content: '',
    });
    const [loading, setLoading] = useState(isEdit);

    useEffect(() => {
        if (!isEdit) return;
        let cancelled = false;
        readKnowledgeById(id).then(data => {
            if (cancelled) return;
            const entry = Array.isArray(data) ? data[0] : data;
            if (entry) {
                setValues({
                    title: entry.title || '',
                    category: entry.category || 'general',
                    url: entry.url || '',
                    projects: entry.projects || [],
                    tags: Array.isArray(entry.tags) ? entry.tags.join(', ') : (entry.tags || ''),
                    content: entry.content || '',
                });
            }
            setLoading(false);
        }).catch(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [id]);

    async function handleSubmit() {
        const payload = {
            title: values.title,
            category: values.category,
            url: values.url || null,
            projects: Array.isArray(values.projects)
                ? values.projects.map(p => p.name || p)
                : [],
            tags: values.tags
                ? values.tags.split(',').map(t => t.trim()).filter(Boolean)
                : [],
            content: values.content,
        };

        try {
            if (isEdit) {
                await updateKnowledge(id, payload);
                addToast('Knowledge updated.', 'success');
                navigate('#/knowledge/' + id);
            } else {
                const result = await createKnowledge(payload);
                addToast('Knowledge created.', 'success');
                const newId = result?.id || result?.[0]?.id;
                navigate(newId ? '#/knowledge/' + newId : '#/knowledge');
            }
        } catch (_) {}
    }

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;

    const fields = [
        { name: 'title', label: 'Title', required: true },
        {
            name: 'category', label: 'Category', type: 'select',
            options: CATEGORIES.map(c => ({ value: c, label: c })),
        },
        { name: 'url', label: 'URL', placeholder: 'https://...' },
        { name: 'projects', label: 'Projects', type: 'projects' },
        { name: 'tags', label: 'Tags', placeholder: 'Comma-separated tags' },
        { name: 'content', label: 'Content', type: 'markdown' },
    ];

    return html`
        <div>
            <div class="breadcrumb">
                <a href="#/knowledge" onClick=${e => { e.preventDefault(); navigate('#/knowledge'); }}>Knowledge</a>
                <span> / ${isEdit ? 'Edit' : 'New Entry'}</span>
            </div>
            <h1 class="page-title">${isEdit ? 'Edit Knowledge' : 'New Knowledge Entry'}</h1>
            <${EntityForm}
                fields=${fields}
                values=${values}
                onChange=${setValues}
                onSubmit=${handleSubmit}
                onCancel=${() => navigate(isEdit ? '#/knowledge/' + id : '#/knowledge')}
                submitLabel=${isEdit ? 'Update' : 'Create'}
            />
        </div>
    `;
}

// --- Router ---

export function KnowledgePage({ param = '' }) {
    if (param === 'new') return html`<${KnowledgeForm} />`;
    if (param.endsWith('/edit')) {
        const id = param.replace('/edit', '');
        return html`<${KnowledgeForm} id=${id} />`;
    }
    if (param && !isNaN(param)) return html`<${KnowledgeDetail} id=${param} />`;
    return html`<${KnowledgeList} />`;
}
