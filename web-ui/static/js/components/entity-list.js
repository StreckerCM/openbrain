import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate } from '../lib/state.js';

const html = htm.bind(h);

const PAGE_SIZE = 20;

export function EntityList({ fetchFn, columns = [], gridTemplate, detailRoute, filters, emptyMessage = 'No items found.' }) {
    const [items, setItems] = useState([]);
    const [page, setPage] = useState(1);
    const [loading, setLoading] = useState(true);
    const [hasMore, setHasMore] = useState(false);
    const [filterValues, setFilterValues] = useState({});

    useEffect(() => {
        setPage(1);
    }, [filterValues]);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);

        fetchFn(page, PAGE_SIZE, filterValues)
            .then(data => {
                if (!cancelled) {
                    const arr = Array.isArray(data) ? data : [];
                    setItems(arr);
                    setHasMore(arr.length === PAGE_SIZE);
                    setLoading(false);
                }
            })
            .catch(() => {
                if (!cancelled) {
                    setItems([]);
                    setLoading(false);
                }
            });

        return () => { cancelled = true; };
    }, [page, filterValues]);

    function updateFilter(key, value) {
        setFilterValues(prev => ({ ...prev, [key]: value }));
    }

    function handleRowClick(item) {
        if (detailRoute) navigate(detailRoute(item));
    }

    const gridStyle = gridTemplate ? { gridTemplateColumns: gridTemplate } : {};

    return html`
        <div>
            ${filters && html`
                <div class="filter-bar">
                    ${filters(filterValues, updateFilter)}
                </div>
            `}

            ${loading && html`
                <div class="loading-center">
                    <div class="spinner"></div>
                </div>
            `}

            ${!loading && items.length === 0 && html`
                <div class="empty-state">${emptyMessage}</div>
            `}

            ${!loading && items.length > 0 && html`
                <div class="data-table">
                    <div class="table-header" style=${gridStyle}>
                        ${columns.map(col => html`
                            <div key=${col.label}>${col.label}</div>
                        `)}
                    </div>
                    ${items.map((item, i) => html`
                        <div
                            key=${item.id || i}
                            class="table-row"
                            style=${gridStyle}
                            onClick=${() => handleRowClick(item)}
                        >
                            ${columns.map(col => html`
                                <div key=${col.label}>${col.render(item)}</div>
                            `)}
                        </div>
                    `)}
                </div>
            `}

            ${!loading && (page > 1 || hasMore) && html`
                <div class="pagination">
                    <span>Page ${page}</span>
                    <div class="pagination-buttons">
                        <button
                            class="page-btn"
                            disabled=${page === 1}
                            onClick=${() => setPage(p => Math.max(1, p - 1))}
                        >
                            Prev
                        </button>
                        <button
                            class="page-btn"
                            disabled=${!hasMore}
                            onClick=${() => setPage(p => p + 1)}
                        >
                            Next
                        </button>
                    </div>
                </div>
            `}
        </div>
    `;
}
