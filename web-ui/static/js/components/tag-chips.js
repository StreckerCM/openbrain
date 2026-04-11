import { h } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import htm from 'htm';
import { readProjects } from '../lib/api.js';

const html = htm.bind(h);

export function ProjectChips({ projects = [], onRemove, onAdd, readOnly = false }) {
    const [dropdownOpen, setDropdownOpen] = useState(false);
    const [allProjects, setAllProjects] = useState([]);
    const [loading, setLoading] = useState(false);
    const dropdownRef = useRef(null);

    useEffect(() => {
        function handleClickOutside(e) {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
                setDropdownOpen(false);
            }
        }
        if (dropdownOpen) {
            document.addEventListener('mousedown', handleClickOutside);
        }
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [dropdownOpen]);

    async function openDropdown() {
        setDropdownOpen(true);
        setLoading(true);
        try {
            const data = await readProjects('status=in.(active,system)&order=name.asc');
            setAllProjects(Array.isArray(data) ? data : []);
        } catch (_) {
            setAllProjects([]);
        } finally {
            setLoading(false);
        }
    }

    const linkedNames = new Set((projects || []).map(p => p.name || p));
    const available = allProjects.filter(p => !linkedNames.has(p.name));

    return html`
        <div class="flex flex-wrap gap-4" style="align-items:center;">
            ${(projects || []).map(p => {
                const name = p.name || p;
                return html`
                    <span key=${name} class="chip chip-project">
                        ${name}
                        ${!readOnly && onRemove && html`
                            <span class="chip-remove" onClick=${() => onRemove(name)} title="Remove">×</span>
                        `}
                    </span>
                `;
            })}
            ${!readOnly && onAdd && html`
                <div style="position:relative;" ref=${dropdownRef}>
                    <span class="chip chip-add" onClick=${openDropdown}>+ Add project</span>
                    ${dropdownOpen && html`
                        <div style="
                            position:absolute; top:100%; left:0; margin-top:4px;
                            background:var(--surface); border:1px solid var(--border);
                            border-radius:6px; min-width:160px; z-index:50;
                            max-height:200px; overflow-y:auto;
                        ">
                            ${loading && html`<div style="padding:8px 12px; font-size:12px; color:var(--text-3);">Loading...</div>`}
                            ${!loading && available.length === 0 && html`
                                <div style="padding:8px 12px; font-size:12px; color:var(--text-3);">No projects available</div>
                            `}
                            ${!loading && available.map(p => html`
                                <div
                                    key=${p.name}
                                    style="padding:8px 12px; font-size:13px; cursor:pointer; transition:background 0.1s;"
                                    onMouseEnter=${e => e.currentTarget.style.background = 'rgba(255,255,255,0.05)'}
                                    onMouseLeave=${e => e.currentTarget.style.background = ''}
                                    onClick=${() => { onAdd(p.name); setDropdownOpen(false); }}
                                >
                                    ${p.name}
                                </div>
                            `)}
                        </div>
                    `}
                </div>
            `}
        </div>
    `;
}

export function TagChips({ tags = [], linkBase = '' }) {
    if (!tags || tags.length === 0) return null;

    return html`
        <div class="flex flex-wrap gap-4">
            ${tags.map(tag => linkBase
                ? html`<a key=${tag} class="chip chip-tag-link" href=${linkBase + encodeURIComponent(tag)}>${tag}</a>`
                : html`<span key=${tag} class="chip chip-tag">${tag}</span>`
            )}
        </div>
    `;
}
