import { h, render } from 'preact';
import htm from 'htm';

const html = htm.bind(h);

import { currentRoute } from './lib/state.js';
import { Sidebar } from './components/sidebar.js';
import { ToastContainer } from './components/toast.js';
import { DashboardPage } from './pages/dashboard.js';
import { KnowledgePage } from './pages/knowledge.js';
import { MemoriesPage } from './pages/memories.js';
import { ProjectsPage } from './pages/projects.js';
import { SearchPage } from './pages/search.js';
import { ArchivePage } from './pages/archive.js';
import { TagsPage } from './pages/tags.js';

function parseRoute(route) {
    const path = route.replace('#/', '').split('?')[0];
    const qsRaw = route.split('?')[1] || '';
    const query = Object.fromEntries(new URLSearchParams(qsRaw));
    const [base, ...rest] = path.split('/');
    return { base: base || '', param: rest.join('/'), query };
}

function Router() {
    const { base, param, query } = parseRoute(currentRoute.value);

    switch (base) {
        case '': return html`<${DashboardPage} />`;
        case 'knowledge': return html`<${KnowledgePage} param=${param} query=${query} />`;
        case 'memories': return html`<${MemoriesPage} param=${param} query=${query} />`;
        case 'projects': return html`<${ProjectsPage} param=${param} query=${query} />`;
        case 'search': return html`<${SearchPage} query=${query} />`;
        case 'archive': return html`<${ArchivePage} />`;
        case 'tags': return html`<${TagsPage} query=${query} />`;
        default: return html`<${DashboardPage} />`;
    }
}

function App() {
    return html`
        <div class="app-layout">
            <${Sidebar} />
            <main class="main-content">
                <${Router} />
            </main>
            <${ToastContainer} />
        </div>
    `;
}

render(html`<${App} />`, document.getElementById('app'));
