import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';

const html = htm.bind(h);

const TYPE_OPTIONS = [
    { value: 'knowledge', label: 'Knowledge' },
    { value: 'memories', label: 'Memories' },
];

export function SearchBar({ onSearch, initialQuery = '' }) {
    const [query, setQuery] = useState(initialQuery);
    const [mode, setMode] = useState('semantic');
    const [types, setTypes] = useState(['knowledge', 'memories']);

    function toggleType(type) {
        setTypes(prev => {
            if (prev.includes(type)) {
                // Always keep at least one type selected
                if (prev.length === 1) return prev;
                return prev.filter(t => t !== type);
            }
            return [...prev, type];
        });
    }

    function handleSubmit(e) {
        e.preventDefault();
        if (query.trim()) {
            onSearch(query.trim(), mode, types);
        }
    }

    return html`
        <form onSubmit=${handleSubmit}>
            <div class="filter-bar" style="flex-direction:column; align-items:stretch;">
                <div class="flex gap-8" style="align-items:center;">
                    <input
                        class="filter-input"
                        type="text"
                        placeholder="Search..."
                        value=${query}
                        onInput=${e => setQuery(e.target.value)}
                        style="flex:1;"
                    />
                    <button type="submit" class="btn btn-primary">Search</button>
                </div>

                <div class="flex gap-8 flex-wrap" style="align-items:center; margin-top:8px;">
                    <span style="font-size:12px; color:var(--text-3);">Mode:</span>
                    <button
                        type="button"
                        class=${'md-toggle-btn' + (mode === 'semantic' ? ' active' : '')}
                        onClick=${() => setMode('semantic')}
                    >
                        Semantic
                    </button>
                    <button
                        type="button"
                        class=${'md-toggle-btn' + (mode === 'exact' ? ' active' : '')}
                        onClick=${() => setMode('exact')}
                    >
                        Exact
                    </button>

                    <span style="font-size:12px; color:var(--text-3); margin-left:8px;">Type:</span>
                    <button
                        type="button"
                        class=${'md-toggle-btn' + (types.includes('knowledge') && types.includes('memories') ? ' active' : '')}
                        onClick=${() => setTypes(['knowledge', 'memories'])}
                    >
                        All
                    </button>
                    ${TYPE_OPTIONS.map(opt => html`
                        <button
                            key=${opt.value}
                            type="button"
                            class=${'md-toggle-btn' + (types.includes(opt.value) && types.length === 1 ? ' active' : '')}
                            onClick=${() => setTypes([opt.value])}
                        >
                            ${opt.label}
                        </button>
                    `)}
                </div>
            </div>
        </form>
    `;
}
