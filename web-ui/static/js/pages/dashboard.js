import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate } from '../lib/state.js';
import { fetchCounts, fetchArchivedCounts, readRecentActivity, readOrphanedItems, fetchTagStats } from '../lib/api.js';
import { TagCloudWidget } from '../components/tag-cloud.js';

const html = htm.bind(h);

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const diff = Math.max(0, Date.now() - new Date(dateStr).getTime());
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
}

export function DashboardPage() {
    const [counts, setCounts] = useState(null);
    const [archivedCounts, setArchivedCounts] = useState(null);
    const [activity, setActivity] = useState([]);
    const [orphans, setOrphans] = useState([]);
    const [topTags, setTopTags] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        Promise.all([
            fetchCounts().catch(() => ({ knowledge: 0, memories: 0, projects: 0 })),
            fetchArchivedCounts().catch(() => ({ knowledge: 0, memories: 0, projects: 0 })),
            readRecentActivity(10).catch(() => []),
            readOrphanedItems().catch(() => []),
            fetchTagStats('order=entry_count.desc&limit=20').catch(() => []),
        ]).then(([c, ac, act, orph, tags]) => {
            if (!cancelled) {
                setCounts(c);
                setArchivedCounts(ac);
                setActivity(Array.isArray(act) ? act : []);
                setOrphans(Array.isArray(orph) ? orph : []);
                setTopTags(Array.isArray(tags) ? tags : []);
                setLoading(false);
            }
        });
        return () => { cancelled = true; };
    }, []);

    if (loading) {
        return html`<div class="loading-center"><div class="spinner"></div></div>`;
    }

    const orphanCount = orphans.length;

    return html`
        <div>
            <div class="page-header"><h1 class="page-title">Dashboard</h1></div>

            <div class="stats-grid">
                <div class="card stat-card" onClick=${() => navigate('#/knowledge')}>
                    <div class="stat-label">Knowledge</div>
                    <div class="stat-value">${counts?.knowledge ?? 0}</div>
                    <div class="stat-sub" style="color:var(--accent);">${archivedCounts?.knowledge ?? 0} archived</div>
                </div>
                <div class="card stat-card" onClick=${() => navigate('#/memories')}>
                    <div class="stat-label">Memories</div>
                    <div class="stat-value">${counts?.memories ?? 0}</div>
                    <div class="stat-sub" style="color:var(--accent);">${archivedCounts?.memories ?? 0} archived</div>
                </div>
                <div class="card stat-card" onClick=${() => navigate('#/projects')}>
                    <div class="stat-label">Projects</div>
                    <div class="stat-value">${counts?.projects ?? 0}</div>
                    <div class="stat-sub" style="color:var(--accent);">1 system</div>
                </div>
                <div class="card stat-card" style=${orphanCount > 0 ? 'border-color:var(--warning)' : ''} onClick=${() => navigate('#/knowledge')}>
                    <div class="stat-label">Orphans</div>
                    <div class="stat-value" style=${orphanCount > 0 ? 'color:var(--warning)' : ''}>${orphanCount}</div>
                    <div class="stat-sub" style="color:var(--warning);">${orphanCount > 0 ? 'need attention' : 'all linked'}</div>
                </div>
            </div>

            <div class="card" style="margin-bottom:20px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                    <span style="font-size:14px;font-weight:600;">Top Tags</span>
                </div>
                <${TagCloudWidget}
                    tags=${topTags}
                    limit=${20}
                    showViewAll=${true}
                />
            </div>

            <div class="two-col">
                <div class="card">
                    <div style="font-size:13px;font-weight:600;margin-bottom:12px;">Recent Activity</div>
                    ${activity.length === 0
                        ? html`<div style="color:var(--text-3);font-size:13px;">No recent activity</div>`
                        : html`
                            <div class="flex flex-col gap-10">
                                ${activity.map((item, i) => {
                                    const route = item.type === 'knowledge' ? 'knowledge' : 'memories';
                                    return html`
                                        <div key=${i} class="flex justify-between items-center"
                                             style="padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.03);cursor:pointer;"
                                             onClick=${() => navigate('#/' + route + '/' + item.id)}>
                                            <div>
                                                <span class=${'badge badge-' + item.type}>${item.type}</span>
                                                <span style="margin-left:8px;">${item.name}</span>
                                            </div>
                                            <div style="color:var(--text-3);font-size:11px;">${timeAgo(item.updated_at)}</div>
                                        </div>
                                    `;
                                })}
                            </div>
                        `
                    }
                </div>

                ${orphanCount > 0 && html`
                    <div class="card" style="border-color:var(--warning);">
                        <div style="font-size:13px;font-weight:600;color:var(--warning);margin-bottom:12px;">Orphaned Items</div>
                        <div style="font-size:12px;color:var(--text-2);margin-bottom:12px;">Not linked to any project</div>
                        <div class="flex flex-col gap-8">
                            ${orphans.slice(0, 5).map(item => html`
                                <div key=${item.id + item.type}
                                     style="padding:6px 8px;background:var(--bg);border-radius:4px;cursor:pointer;font-size:12px;"
                                     onClick=${() => navigate('#/' + (item.type === 'knowledge' ? 'knowledge' : 'memories') + '/' + item.id)}>
                                    ${item.name}
                                </div>
                            `)}
                        </div>
                        ${orphanCount > 5 && html`
                            <div style="margin-top:10px;font-size:11px;color:var(--accent);cursor:pointer;"
                                 onClick=${() => navigate('#/knowledge')}>View all orphans →</div>
                        `}
                    </div>
                `}
            </div>
        </div>
    `;
}
