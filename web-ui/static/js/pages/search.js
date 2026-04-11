import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import { navigate } from '../lib/state.js';
import { searchItems } from '../lib/api.js';
import { SearchBar } from '../components/search-bar.js';

const html = htm.bind(h);

function similarityColor(score) {
    if (score >= 0.8) return 'var(--green)';
    if (score >= 0.6) return 'var(--amber)';
    return 'var(--text-3)';
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
                        return html`
                            <div
                                key=${item.id || i}
                                class="search-result-card"
                                onClick=${() => navigate(getRoute(item))}
                            >
                                <div class="search-result-header">
                                    <span class=${'badge badge-' + (item.type || 'knowledge')}>${item.type || 'unknown'}</span>
                                    <span class="search-result-title">${item.title || item.name || 'Untitled'}</span>
                                    ${simPct != null && html`
                                        <span
                                            class="search-result-similarity"
                                            style=${'color:' + similarityColor(item.similarity)}
                                        >
                                            ${simPct}%
                                        </span>
                                    `}
                                </div>
                                ${item.content && html`
                                    <div class="search-result-snippet">${snippet(item.content)}</div>
                                `}
                                ${projects.length > 0 && html`
                                    <div class="flex flex-wrap gap-4" style="margin-top:6px;">
                                        ${projects.map(p => html`
                                            <span key=${p} class="chip chip-project" style="font-size:11px;">${p}</span>
                                        `)}
                                    </div>
                                `}
                                ${item.category && html`
                                    <span class="meta-item" style="margin-top:4px; font-size:11px;">Category: ${item.category}</span>
                                `}
                            </div>
                        `;
                    })}
                </div>
            `}
        </div>
    `;
}
