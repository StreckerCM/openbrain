import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate, addToast } from '../lib/state.js';
import {
    readProjects, readProjectByName, createProject, updateProject,
    archiveItem, readKnowledge, readMemories, unlinkFromProject,
} from '../lib/api.js';
import { renderMarkdown } from '../lib/markdown.js';
import { EntityForm } from '../components/entity-form.js';

const html = htm.bind(h);

// --- List View (Card Grid) ---

function ProjectList() {
    const [projects, setProjects] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        readProjects('status=in.(active,system)&order=name.asc')
            .then(data => {
                if (!cancelled) {
                    const arr = Array.isArray(data) ? data : [];
                    // Ensure "general" project is first
                    arr.sort((a, b) => {
                        if (a.name === 'general') return -1;
                        if (b.name === 'general') return 1;
                        return a.name.localeCompare(b.name);
                    });
                    setProjects(arr);
                    setLoading(false);
                }
            })
            .catch(() => { if (!cancelled) { setProjects([]); setLoading(false); } });
        return () => { cancelled = true; };
    }, []);

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;

    return html`
        <div>
            <div class="detail-header">
                <h1 class="page-title">Projects</h1>
                <div class="detail-actions">
                    <button class="btn btn-primary" onClick=${() => navigate('#/projects/new')}>+ New Project</button>
                </div>
            </div>

            ${projects.length === 0 && html`
                <div class="empty-state">No projects found.</div>
            `}

            <div class="project-grid">
                ${projects.map(p => html`
                    <div
                        key=${p.name}
                        class="card project-card"
                        onClick=${() => navigate('#/projects/' + encodeURIComponent(p.name))}
                    >
                        <div class="flex justify-between items-center" style="margin-bottom:8px;">
                            <span style="font-size:16px;font-weight:600;color:var(--text-1);">${p.name}</span>
                            <span class=${'badge badge-' + (p.status === 'system' ? 'system' : 'active')}>
                                ${p.status}
                            </span>
                        </div>
                        ${p.description && html`
                            <div class="project-card-desc">
                                ${p.description.length > 120 ? p.description.slice(0, 120) + '...' : p.description}
                            </div>
                        `}
                        ${p.tech_stack && p.tech_stack.length > 0 && html`
                            <div class="flex flex-wrap gap-4" style="margin-top:8px;">
                                ${p.tech_stack.slice(0, 5).map(t => html`
                                    <span key=${t} class="chip chip-tag">${t}</span>
                                `)}
                                ${p.tech_stack.length > 5 && html`
                                    <span class="chip chip-tag">+${p.tech_stack.length - 5}</span>
                                `}
                            </div>
                        `}
                        <div class="project-card-footer">
                            ${p.knowledge_count != null && html`<span>${p.knowledge_count} knowledge</span>`}
                            ${p.memory_count != null && html`<span>${p.memory_count} memories</span>`}
                        </div>
                    </div>
                `)}
            </div>
        </div>
    `;
}

// --- Detail View ---

function ProjectDetail({ name }) {
    const [project, setProject] = useState(null);
    const [loading, setLoading] = useState(true);
    const [activeTab, setActiveTab] = useState('knowledge');
    const [linkedKnowledge, setLinkedKnowledge] = useState([]);
    const [linkedMemories, setLinkedMemories] = useState([]);
    const [tabLoading, setTabLoading] = useState(false);

    const decodedName = decodeURIComponent(name);
    const isGeneral = decodedName === 'general';

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        readProjectByName(decodedName).then(data => {
            if (!cancelled) {
                const entry = Array.isArray(data) ? data[0] : data;
                setProject(entry || null);
                setLoading(false);
            }
        }).catch(() => { if (!cancelled) { setProject(null); setLoading(false); } });
        return () => { cancelled = true; };
    }, [name]);

    useEffect(() => {
        if (!project) return;
        let cancelled = false;
        setTabLoading(true);

        const fetchLinked = activeTab === 'knowledge'
            ? readKnowledge(`projects=cs.{${encodeURIComponent(decodedName)}}&status=eq.active&order=updated_at.desc&limit=50`)
            : readMemories(`projects=cs.{${encodeURIComponent(decodedName)}}&status=eq.active&order=updated_at.desc&limit=50`);

        fetchLinked.then(data => {
            if (!cancelled) {
                const arr = Array.isArray(data) ? data : [];
                if (activeTab === 'knowledge') setLinkedKnowledge(arr);
                else setLinkedMemories(arr);
                setTabLoading(false);
            }
        }).catch(() => {
            if (!cancelled) {
                if (activeTab === 'knowledge') setLinkedKnowledge([]);
                else setLinkedMemories([]);
                setTabLoading(false);
            }
        });

        return () => { cancelled = true; };
    }, [project, activeTab]);

    async function handleArchive() {
        try {
            await archiveItem('project', decodedName);
            addToast('Project archived.', 'success');
            navigate('#/projects');
        } catch (_) {}
    }

    async function handleUnlink(type, entityId) {
        try {
            if (type === 'knowledge') {
                await unlinkFromProject(decodedName, entityId, null);
            } else {
                await unlinkFromProject(decodedName, null, entityId);
            }
            addToast('Unlinked successfully.', 'success');
            // Refresh current tab
            const fetchLinked = type === 'knowledge'
                ? readKnowledge(`projects=cs.{${encodeURIComponent(decodedName)}}&status=eq.active&order=updated_at.desc&limit=50`)
                : readMemories(`projects=cs.{${encodeURIComponent(decodedName)}}&status=eq.active&order=updated_at.desc&limit=50`);
            const data = await fetchLinked;
            const arr = Array.isArray(data) ? data : [];
            if (type === 'knowledge') setLinkedKnowledge(arr);
            else setLinkedMemories(arr);
        } catch (_) {}
    }

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;
    if (!project) return html`<div class="empty-state">Project not found.</div>`;

    const techStack = project.tech_stack || [];
    const currentItems = activeTab === 'knowledge' ? linkedKnowledge : linkedMemories;

    return html`
        <div>
            <div class="breadcrumb">
                <a href="#/projects" onClick=${e => { e.preventDefault(); navigate('#/projects'); }}>Projects</a>
                <span> / ${project.name}</span>
            </div>

            <div class="detail-header">
                <h1 class="page-title">${project.name}</h1>
                ${!isGeneral && html`
                    <div class="detail-actions">
                        <button class="btn btn-secondary" onClick=${() => navigate('#/projects/' + encodeURIComponent(name) + '/edit')}>Edit</button>
                        <button class="btn btn-danger" onClick=${handleArchive}>Archive</button>
                    </div>
                `}
            </div>

            <div class="meta-row">
                <span class=${'badge badge-' + (project.status === 'system' ? 'blue' : 'green')}>${project.status}</span>
                ${project.orphan_policy && html`<span class="meta-item">Orphan policy: ${project.orphan_policy}</span>`}
                ${project.repo_url && html`<a class="meta-item meta-link" href=${project.repo_url} target="_blank" rel="noopener">${project.repo_url}</a>`}
            </div>

            ${techStack.length > 0 && html`
                <div style="margin:16px 0;">
                    <label class="form-label">Tech Stack</label>
                    <div class="flex flex-wrap gap-4">
                        ${techStack.map(t => html`<span key=${t} class="chip chip-tag">${t}</span>`)}
                    </div>
                </div>
            `}

            ${project.description && html`
                <div style="margin:16px 0; color:var(--text-2);">${project.description}</div>
            `}

            ${project.notes && html`
                <div style="margin:16px 0;">
                    <label class="form-label">Notes</label>
                    <div class="md-content" dangerouslySetInnerHTML=${{ __html: renderMarkdown(project.notes) }}></div>
                </div>
            `}

            <div style="margin-top:24px;">
                <div class="tabs">
                    <div
                        class=${'tab' + (activeTab === 'knowledge' ? ' active' : '')}
                        onClick=${() => setActiveTab('knowledge')}
                    >
                        Knowledge <span class="tab-count">${linkedKnowledge.length}</span>
                    </div>
                    <div
                        class=${'tab' + (activeTab === 'memories' ? ' active' : '')}
                        onClick=${() => setActiveTab('memories')}
                    >
                        Memories <span class="tab-count">${linkedMemories.length}</span>
                    </div>
                </div>

                ${tabLoading && html`<div class="loading-center"><div class="spinner"></div></div>`}

                ${!tabLoading && currentItems.length === 0 && html`
                    <div class="empty-state">No linked ${activeTab} items.</div>
                `}

                ${!tabLoading && currentItems.length > 0 && html`
                    <div class="flex flex-col gap-8">
                        ${currentItems.map(item => html`
                            <div key=${item.id} class="card flex justify-between items-center" style="padding:10px 14px;">
                                <div>
                                    <a
                                        href=${'#/' + activeTab + '/' + item.id}
                                        onClick=${e => { e.preventDefault(); navigate('#/' + activeTab + '/' + item.id); }}
                                        style="font-size:13px;"
                                    >
                                        ${item.title || item.name || 'Untitled'}
                                    </a>
                                    <span style="color:var(--text-3);font-size:11px;margin-left:8px;">
                                        ${item.category || item.memory_type || ''}
                                    </span>
                                </div>
                                <div class="flex gap-8 items-center">
                                    <span style="color:var(--text-3);font-size:11px;">${item.updated_at ? new Date(item.updated_at).toLocaleDateString() : ''}</span>
                                    <button
                                        class="btn btn-sm btn-secondary"
                                        style="color:var(--danger);"
                                        onClick=${(e) => { e.stopPropagation(); handleUnlink(activeTab, item.id); }}
                                        title="Unlink from project"
                                    >
                                        Unlink
                                    </button>
                                </div>
                            </div>
                        `)}
                    </div>
                `}
            </div>
        </div>
    `;
}

// --- Create/Edit Form ---

function ProjectForm({ name }) {
    const isEdit = !!name;
    const decodedName = name ? decodeURIComponent(name) : '';
    const [values, setValues] = useState({
        name: '',
        description: '',
        repo_url: '',
        tech_stack: '',
        notes: '',
        orphan_policy: 'archive',
    });
    const [loading, setLoading] = useState(isEdit);

    useEffect(() => {
        if (!isEdit) return;
        let cancelled = false;
        readProjectByName(decodedName).then(data => {
            if (cancelled) return;
            const entry = Array.isArray(data) ? data[0] : data;
            if (entry) {
                setValues({
                    name: entry.name || '',
                    description: entry.description || '',
                    repo_url: entry.repo_url || '',
                    tech_stack: Array.isArray(entry.tech_stack) ? entry.tech_stack.join(', ') : (entry.tech_stack || ''),
                    notes: entry.notes || '',
                    orphan_policy: entry.orphan_policy || 'archive',
                });
            }
            setLoading(false);
        }).catch(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [name]);

    async function handleSubmit() {
        const payload = {
            name: values.name,
            description: values.description || null,
            repo_url: values.repo_url || null,
            tech_stack: values.tech_stack
                ? values.tech_stack.split(',').map(t => t.trim()).filter(Boolean)
                : [],
            notes: values.notes || null,
            orphan_policy: values.orphan_policy,
        };

        try {
            if (isEdit) {
                await updateProject(decodedName, payload);
                addToast('Project updated.', 'success');
                navigate('#/projects/' + encodeURIComponent(decodedName));
            } else {
                await createProject(payload);
                addToast('Project created.', 'success');
                navigate('#/projects/' + encodeURIComponent(payload.name));
            }
        } catch (_) {}
    }

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;

    const fields = [
        { name: 'name', label: 'Name', required: true, readOnly: isEdit },
        { name: 'description', label: 'Description', placeholder: 'Brief description...' },
        { name: 'repo_url', label: 'Repository URL', placeholder: 'https://...' },
        { name: 'tech_stack', label: 'Tech Stack', placeholder: 'Comma-separated (e.g., Python, Docker, PostgreSQL)' },
        { name: 'notes', label: 'Notes', type: 'markdown' },
        {
            name: 'orphan_policy', label: 'Orphan Policy', type: 'select',
            options: [
                { value: 'archive', label: 'Archive orphaned items' },
                { value: 'reassign', label: 'Reassign to general' },
            ],
        },
    ];

    return html`
        <div>
            <div class="breadcrumb">
                <a href="#/projects" onClick=${e => { e.preventDefault(); navigate('#/projects'); }}>Projects</a>
                <span> / ${isEdit ? 'Edit' : 'New Project'}</span>
            </div>
            <h1 class="page-title">${isEdit ? 'Edit Project' : 'New Project'}</h1>
            <${EntityForm}
                fields=${fields}
                values=${values}
                onChange=${setValues}
                onSubmit=${handleSubmit}
                onCancel=${() => navigate(isEdit ? '#/projects/' + encodeURIComponent(decodedName) : '#/projects')}
                submitLabel=${isEdit ? 'Update' : 'Create'}
            />
        </div>
    `;
}

// --- Router ---

export function ProjectsPage({ param = '' }) {
    if (param === 'new') return html`<${ProjectForm} />`;
    if (param.endsWith('/edit')) {
        const name = param.replace('/edit', '');
        return html`<${ProjectForm} name=${name} />`;
    }
    if (param) return html`<${ProjectDetail} name=${param} />`;
    return html`<${ProjectList} />`;
}
