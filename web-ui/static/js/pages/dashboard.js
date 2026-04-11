import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { navigate } from '../lib/state.js';
import { fetchCounts, fetchArchivedCounts, readRecentActivity, readOrphanedItems } from '../lib/api.js';

const html = htm.bind(h);

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    const diff = Math.max(0, now - then);
    const seconds = Math.floor(diff / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) return days === 1 ? '1 day ago' : `${days} days ago`;
    if (hours > 0) return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
    if (minutes > 0) return minutes === 1 ? '1 minute ago' : `${minutes} minutes ago`;
    return 'just now';
}

export function DashboardPage() {
    const [counts, setCounts] = useState(null);
    const [archivedCounts, setArchivedCounts] = useState(null);
    const [activity, setActivity] = useState([]);
    const [orphans, setOrphans] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);

        Promise.all([
            fetchCounts().catch(() => ({ knowledge: 0, memories: 0, projects: 0 })),
            fetchArchivedCounts().catch(() => ({ knowledge: 0, memories: 0, projects: 0 })),
            readRecentActivity(10).catch(() => []),
            readOrphanedItems().catch(() => []),
        ]).then(([c, ac, act, orph]) => {
            if (!cancelled) {
                setCounts(c);
                setArchivedCounts(ac);
                setActivity(Array.isArray(act) ? act : []);
                setOrphans(Array.isArray(orph) ? orph : []);
                setLoading(false);
            }
        });

        return () => { cancelled = true; };
    }, []);

    if (loading) {
        return html`
            <div class="loading-center">
                <div class="spinner"></div>
            </div>
        `;
    }

    const orphanCount = orphans.length;

    return html`
        <div>
            <h1 class="page-title">Dashboard</h1>

            <div class="stats-row">
                <div class="stat-card" onClick=${() => navigate('#/knowledge')}>
                    <div class="stat-value">${counts?.knowledge ?? 0}</div>
                    <div class="stat-label">Knowledge</div>
                </div>
                <div class="stat-card" onClick=${() => navigate('#/memories')}>
                    <div class="stat-value">${counts?.memories ?? 0}</div>
                    <div class="stat-label">Memories</div>
                </div>
                <div class="stat-card" onClick=${() => navigate('#/projects')}>
                    <div class="stat-value">${counts?.projects ?? 0}</div>
                    <div class="stat-label">Projects</div>
                </div>
                <div class="stat-card" onClick=${() => navigate('#/archive')}>
                    <div class="stat-value">${(archivedCounts?.knowledge ?? 0) + (archivedCounts?.memories ?? 0) + (archivedCounts?.projects ?? 0)}</div>
                    <div class="stat-label">Archived</div>
                </div>
            </div>

            ${orphanCount > 0 && html`
                <div class="alert alert-warning" style="margin-bottom:20px;">
                    <strong>${orphanCount} orphaned item${orphanCount !== 1 ? 's' : ''}</strong> found --
                    items not linked to any project.
                    ${orphans.slice(0, 5).map(o => html`
                        <div key=${o.id + o.type} style="margin-top:4px; font-size:13px;">
                            <span class=${'badge badge-' + o.type}>${o.type}</span>
                            ${' '}
                            <a
                                href=${'#/' + (o.type === 'knowledge' ? 'knowledge' : 'memories') + '/' + o.id}
                                onClick=${(e) => { e.preventDefault(); navigate('#/' + (o.type === 'knowledge' ? 'knowledge' : 'memories') + '/' + o.id); }}
                            >
                                ${o.title || o.name || 'Untitled'}
                            </a>
                        </div>
                    `)}
                    ${orphanCount > 5 && html`
                        <div style="margin-top:4px; font-size:12px; color:var(--text-3);">
                            ...and ${orphanCount - 5} more
                        </div>
                    `}
                </div>
            `}

            <h2 class="section-title">Recent Activity</h2>
            ${activity.length === 0 && html`
                <div class="empty-state">No recent activity.</div>
            `}
            ${activity.length > 0 && html`
                <div class="activity-list">
                    ${activity.map((item, i) => {
                        const route = item.type === 'knowledge' ? 'knowledge' : item.type === 'memory' ? 'memories' : 'projects';
                        const id = item.type === 'project' ? item.name : item.id;
                        return html`
                            <div
                                key=${i}
                                class="activity-item"
                                onClick=${() => navigate('#/' + route + '/' + id)}
                            >
                                <span class=${'badge badge-' + item.type}>${item.type}</span>
                                <span class="activity-title">${item.title || item.name || 'Untitled'}</span>
                                <span class="activity-time">${timeAgo(item.updated_at)}</span>
                            </div>
                        `;
                    })}
                </div>
            `}
        </div>
    `;
}
