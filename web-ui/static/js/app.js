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

function Router() {
    const route = currentRoute.value;
    const [base, ...rest] = route.replace('#/', '').split('/');
    const param = rest.join('/');

    switch (base) {
        case '': return html`<${DashboardPage} />`;
        case 'knowledge': return html`<${KnowledgePage} param=${param} />`;
        case 'memories': return html`<${MemoriesPage} param=${param} />`;
        case 'projects': return html`<${ProjectsPage} param=${param} />`;
        case 'search': return html`<${SearchPage} />`;
        case 'archive': return html`<${ArchivePage} />`;
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
