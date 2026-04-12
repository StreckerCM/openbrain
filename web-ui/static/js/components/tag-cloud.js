import { h } from 'preact';
import htm from 'htm';
import { navigate } from '../lib/state.js';

const html = htm.bind(h);

function computeFontSize(count, minCount, maxCount) {
    if (maxCount === minCount) return 16;
    const minSize = 12;
    const maxSize = 26;
    // Logarithmic scale for better distribution
    const logMin = Math.log(minCount || 1);
    const logMax = Math.log(maxCount || 1);
    const logCount = Math.log(count || 1);
    const ratio = logMax === logMin ? 0.5 : (logCount - logMin) / (logMax - logMin);
    return Math.round(minSize + ratio * (maxSize - minSize));
}

function computeOpacity(count, maxCount) {
    const min = 0.45;
    const max = 1.0;
    if (!maxCount) return max;
    const ratio = Math.log(count || 1) / Math.log(maxCount || 1);
    return min + ratio * (max - min);
}

export function TagCloudWidget({ tags = [], limit, onTagClick, showViewAll = false, totalCount = 0 }) {
    if (!tags || tags.length === 0) {
        return html`<div style="color:var(--text-3);font-size:13px;">No tags yet</div>`;
    }

    const displayed = limit ? tags.slice(0, limit) : tags;
    const counts = displayed.map(t => t.entry_count);
    const minCount = Math.min(...counts);
    const maxCount = Math.max(...counts);

    function handleClick(tag) {
        if (onTagClick) {
            onTagClick(tag);
        } else {
            navigate('#/knowledge?tag=' + encodeURIComponent(tag));
        }
    }

    return html`
        <div>
            <div class="tag-cloud">
                ${displayed.map(t => {
                    const size = computeFontSize(t.entry_count, minCount, maxCount);
                    const opacity = computeOpacity(t.entry_count, maxCount);
                    return html`
                        <a key=${t.tag}
                           class="tag-cloud-item"
                           style=${'font-size:' + size + 'px;opacity:' + opacity.toFixed(2)}
                           onClick=${() => handleClick(t.tag)}
                           title=${t.tag + ' (' + t.entry_count + ' entries)'}>
                            ${t.tag}
                        </a>
                    `;
                })}
            </div>
            ${showViewAll && html`
                <div style="margin-top:12px;text-align:right;">
                    <a class="link-subtle" onClick=${() => navigate('#/tags')}>
                        View all ${totalCount || tags.length} tags →
                    </a>
                </div>
            `}
        </div>
    `;
}
