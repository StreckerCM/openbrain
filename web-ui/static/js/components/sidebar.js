import { h } from 'preact';
import htm from 'htm';
import { currentRoute, sidebarOpen, navigate } from '../lib/state.js';

const html = htm.bind(h);

const NAV_ITEMS = [
    { label: 'Dashboard', href: '#/' },
    { label: 'Knowledge', href: '#/knowledge' },
    { label: 'Memories', href: '#/memories' },
    { label: 'Projects', href: '#/projects' },
    { label: 'Search', href: '#/search' },
];

const ARCHIVE_ITEM = { label: 'Archive', href: '#/archive' };

export function Sidebar() {
    const route = currentRoute.value;
    const isOpen = sidebarOpen.value;

    function handleNav(href) {
        navigate(href);
        sidebarOpen.value = false;
    }

    function closeBackdrop() {
        sidebarOpen.value = false;
    }

    function toggleSidebar() {
        sidebarOpen.value = !sidebarOpen.value;
    }

    return html`
        <>
            <button class="hamburger" onClick=${toggleSidebar} aria-label="Toggle sidebar">☰</button>
            <div class=${'sidebar-backdrop' + (isOpen ? ' open' : '')} onClick=${closeBackdrop}></div>
            <aside class=${'sidebar' + (isOpen ? ' open' : '')}>
                <div class="sidebar-logo">OpenBrain</div>
                <nav class="sidebar-nav">
                    ${NAV_ITEMS.map(item => html`
                        <a
                            key=${item.href}
                            class=${'sidebar-link' + (route === item.href ? ' active' : '')}
                            onClick=${(e) => { e.preventDefault(); handleNav(item.href); }}
                            href=${item.href}
                        >
                            ${item.label}
                        </a>
                    `)}
                    <div class="sidebar-divider">
                        <a
                            class=${'sidebar-link' + (route === ARCHIVE_ITEM.href ? ' active' : '')}
                            onClick=${(e) => { e.preventDefault(); handleNav(ARCHIVE_ITEM.href); }}
                            href=${ARCHIVE_ITEM.href}
                        >
                            ${ARCHIVE_ITEM.label}
                        </a>
                    </div>
                </nav>
            </aside>
        </>
    `;
}
