import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import { navigate } from '../lib/state.js';
import { searchItems } from '../lib/api.js';
import { SearchBar } from '../components/search-bar.js';

const html = htm.bind(h);

function similarityClass(score) {
    if (score >= 0.9) return 'similarity-high';
    if (score >= 0.75) return 'similarity-medium';
    return 'similarity-low';
}

function snippet(text, maxLen = 200) {
    if (!text) return '';
    if (text.length <= maxLen) return text;
    return text.slice(0, maxLen) + '...';
}

export function SearchPage() {
    const [results, setResults] = useState(null);
    const [loading, setLoading] = useState(false);

    async function handleSearch(query, mode, types) {
        setLoading(true);
        try {
            const data = await searchItems(query, mode, types);
            const arr = Array.isArray(data) ? data : (data?.results || []);
            // Sort by similarity descending
            arr.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
            setResults(arr);
        } catch (_) {
            setResults([]);
        } finally {
            setLoading(false);
        }
    }

    function getRoute(item) {
        if (item.type === 'knowledge') return '#/knowledge/' + item.id;
        if (item.type === 'memory' || item.type === 'memories') return '#/memories/' + item.id;
        return '#/';
    }

    return html`
        <div>
            <h1 class="page-title">Search</h1>

            <${SearchBar} onSearch=${handleSearch} />

            ${loading && html`
                <div class="loading-center" style="margin-top:24px;">
                    <div class="spinner"></div>
                </div>
            `}

            ${!loading && results !== null && results.length === 0 && html`
                <div class="empty-state" style="margin-top:24px;">No results found.</div>
            `}

            ${!loading && results !== null && results.length > 0 && html`
                <div style="margin-top:20px;">
                    <div style="font-size:13px; color:var(--text-3); margin-bottom:12px;">
                        ${results.length} result${results.length !== 1 ? 's' : ''}
                    </div>
                    ${results.map((item, i) => {
                        const simPct = item.similarity != null ? Math.round(item.similarity * 100) : null;
                        const projects = item.projects || [];
                        const simCls = item.similarity != null ? similarityClass(item.similarity) : '';
                        return html`
                            <div
                                key=${item.id || i}
                                class="card search-result"
                                onClick=${() => navigate(getRoute(item))}
                                style="margin-bottom:8px;"
                            >
                                <div class="flex items-center justify-between" style="margin-bottom:8px;">
                                    <div class="flex items-center gap-8">
                                        <span class=${'badge badge-' + (item.type || 'knowledge')}>${item.type || 'unknown'}</span>
                                        <span style="font-weight:500; font-size:14px;">${item.title || item.name || 'Untitled'}</span>
                                    </div>
                                    ${simPct != null && html`
                                        <span class=${'badge ' + simCls}>${simPct}%</span>
                                    `}
                                </div>
                                ${item.content && html`
                                    <div style="font-size:13px; color:var(--text-2); margin-bottom:8px; line-height:1.5;">
                                        ${snippet(item.content)}
                                    </div>
                                `}
                                <div class="flex items-center flex-wrap gap-8" style="font-size:11px; color:var(--text-3);">
                                    ${projects.map(p => html`
                                        <span key=${p} class="chip chip-project">${p}</span>
                                    `)}
                                    ${item.category && html`
                                        <span class="badge">${item.category}</span>
                                    `}
                                    ${item.updated_at && html`
                                        <span>${new Date(item.updated_at).toLocaleDateString()}</span>
                                    `}
                                </div>
                            </div>
                        `;
                    })}
                </div>
            `}
        </div>
    `;
}
