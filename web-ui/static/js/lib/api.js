import { addToast } from './state.js';

const READ_BASE = '/api/read';
const WRITE_BASE = '/api/write';
const SEARCH_URL = '/api/search';

async function request(url, options = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            const msg = body.error || body.message || `Error ${resp.status}`;
            throw new Error(msg);
        }
        if (resp.status === 204) return null;
        return resp.json();
    } catch (err) {
        if (err.message === 'Failed to fetch') {
            addToast('Cannot reach server. Check that containers are running.', 'error');
        } else {
            addToast(err.message, 'error');
        }
        throw err;
    }
}

// --- Read API (PostgREST) ---

export function readKnowledge(params = '') {
    return request(`${READ_BASE}/knowledge_with_projects?${params}`);
}

export function readKnowledgeById(id) {
    return request(`${READ_BASE}/knowledge_with_projects?id=eq.${id}`);
}

export function readMemories(params = '') {
    return request(`${READ_BASE}/memories_with_projects?${params}`);
}

export function readMemoryById(id) {
    return request(`${READ_BASE}/memories_with_projects?id=eq.${id}`);
}

export function readProjects(params = '') {
    return request(`${READ_BASE}/projects?${params}`);
}

export function readProjectByName(name) {
    return request(`${READ_BASE}/projects?name=eq.${name}`);
}

export function readRecentActivity(limit = 10) {
    return request(`${READ_BASE}/recent_activity?order=updated_at.desc&limit=${limit}`);
}

export function readOrphanedItems() {
    return request(`${READ_BASE}/rpc/orphaned_items`);
}

export function readCount(table, filter = 'status=eq.active') {
    return request(`${READ_BASE}/${table}?${filter}&select=count`, {
        headers: { 'Prefer': 'count=exact', 'Range-Unit': 'items', 'Range': '0-0' },
    }).then(() => null).catch(() => null);
}

export async function fetchCounts() {
    try {
        const headers = { 'Prefer': 'count=exact' };
        const opts = { headers };
        const [kResp, mResp, pResp] = await Promise.all([
            fetch(`${READ_BASE}/knowledge?status=eq.active&select=id&limit=0`, opts),
            fetch(`${READ_BASE}/memories?status=eq.active&select=id&limit=0`, opts),
            fetch(`${READ_BASE}/projects?status=in.(active,system)&select=id&limit=0`, opts),
        ]);
        const parseCount = (resp) => {
            const range = resp.headers.get('Content-Range');
            if (range) {
                const match = range.match(/\/(\d+)/);
                return match ? parseInt(match[1]) : 0;
            }
            return 0;
        };
        return {
            knowledge: parseCount(kResp),
            memories: parseCount(mResp),
            projects: parseCount(pResp),
        };
    } catch (err) {
        addToast('Cannot reach server. Check that containers are running.', 'error');
        return { knowledge: 0, memories: 0, projects: 0 };
    }
}

export async function fetchArchivedCounts() {
    try {
        const headers = { 'Prefer': 'count=exact' };
        const opts = { headers };
        const [kResp, mResp, pResp] = await Promise.all([
            fetch(`${READ_BASE}/knowledge?status=eq.archived&select=id&limit=0`, opts),
            fetch(`${READ_BASE}/memories?status=eq.archived&select=id&limit=0`, opts),
            fetch(`${READ_BASE}/projects?status=eq.archived&select=id&limit=0`, opts),
        ]);
        const parseCount = (resp) => {
            const range = resp.headers.get('Content-Range');
            if (range) {
                const match = range.match(/\/(\d+)/);
                return match ? parseInt(match[1]) : 0;
            }
            return 0;
        };
        return {
            knowledge: parseCount(kResp),
            memories: parseCount(mResp),
            projects: parseCount(pResp),
        };
    } catch (err) {
        addToast('Cannot reach server. Check that containers are running.', 'error');
        return { knowledge: 0, memories: 0, projects: 0 };
    }
}

// --- Write API (mcp-gateway REST) ---

export function createKnowledge(data) {
    return request(`${WRITE_BASE}/knowledge`, { method: 'POST', body: JSON.stringify(data) });
}

export function updateKnowledge(id, data) {
    return request(`${WRITE_BASE}/knowledge/${id}`, { method: 'PUT', body: JSON.stringify(data) });
}

export function deleteKnowledge(id) {
    return request(`${WRITE_BASE}/knowledge/${id}`, { method: 'DELETE' });
}

export function createMemory(data) {
    return request(`${WRITE_BASE}/memories`, { method: 'POST', body: JSON.stringify(data) });
}

export function updateMemory(id, data) {
    return request(`${WRITE_BASE}/memories/${id}`, { method: 'PUT', body: JSON.stringify(data) });
}

export function deleteMemory(id) {
    return request(`${WRITE_BASE}/memories/${id}`, { method: 'DELETE' });
}

export function createProject(data) {
    return request(`${WRITE_BASE}/projects`, { method: 'POST', body: JSON.stringify(data) });
}

export function updateProject(name, data) {
    return request(`${WRITE_BASE}/projects/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(data) });
}

export function deleteProject(name) {
    return request(`${WRITE_BASE}/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

export function archiveItem(type, id) {
    return request(`${WRITE_BASE}/archive/${type}/${id}`, { method: 'POST' });
}

export function unarchiveItem(type, id) {
    return request(`${WRITE_BASE}/unarchive/${type}/${id}`, { method: 'POST' });
}

export function linkToProject(project, knowledgeId, memoryId) {
    const body = { project };
    if (knowledgeId) body.knowledge_id = knowledgeId;
    if (memoryId) body.memory_id = memoryId;
    return request(`${WRITE_BASE}/link`, { method: 'POST', body: JSON.stringify(body) });
}

export function unlinkFromProject(project, knowledgeId, memoryId) {
    const body = { project };
    if (knowledgeId) body.knowledge_id = knowledgeId;
    if (memoryId) body.memory_id = memoryId;
    return request(`${WRITE_BASE}/link`, { method: 'DELETE', body: JSON.stringify(body) });
}

export function searchItems(query, mode = 'semantic', types = ['knowledge', 'memories']) {
    return request(SEARCH_URL, {
        method: 'POST',
        body: JSON.stringify({ query, mode, types }),
    });
}

export function bulkDelete(items) {
    return request(`${WRITE_BASE}/bulk-delete`, {
        method: 'DELETE',
        body: JSON.stringify({ items }),
    });
}
