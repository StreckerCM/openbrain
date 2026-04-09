/* components.js — render functions for all UI elements */

const Components = {
  /** Truncate text to maxLen characters */
  snippet(text, maxLen = 150) {
    if (!text) return "";
    return text.length > maxLen ? text.slice(0, maxLen) + "..." : text;
  },

  /** Format ISO date to readable string */
  formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
  },

  /** Render a type badge */
  badge(type) {
    const labels = {
      knowledge: "Knowledge",
      shared_resources: "Resources",
      memories: "Memory",
    };
    return `<span class="badge badge-${type}">${labels[type] || type}</span>`;
  },

  /** Render tag pills */
  tags(tagList) {
    if (!tagList || tagList.length === 0) return "";
    return tagList.map((t) => `<span class="tag">${t}</span>`).join(" ");
  },

  /** Render a similarity score */
  similarityLabel(score) {
    if (score == null) return "";
    return `<span class="similarity">${(score * 100).toFixed(0)}% match</span>`;
  },

  /** Render a single search result card */
  resultCard(item, type) {
    const id = `result-${type}-${item.id}`;
    const title = item.title || item.name || "Untitled";
    const project = item.project || (item.projects ? item.projects.join(", ") : "");
    const content = item.content || item.description || "";
    const tagHtml = Components.tags(item.tags);
    const simHtml = Components.similarityLabel(item.similarity);
    const projectBadge = project ? `<span class="tag">${project}</span>` : "";

    return `
      <div class="card" onclick="App.toggleExpand('${id}')">
        <div class="card-header">
          <span class="card-title">${title}</span>
          <span>${Components.badge(type)} ${simHtml}</span>
        </div>
        <div class="card-snippet">${Components.snippet(content)}</div>
        <div class="card-meta">
          ${projectBadge} ${tagHtml}
          <span class="similarity">${Components.formatDate(item.updated_at)}</span>
        </div>
        <div class="card-expanded" id="${id}" style="display:none;">${content}</div>
      </div>`;
  },

  /** Render a group of results for one type */
  resultGroup(type, items) {
    if (!items || items.length === 0) return "";
    const labels = {
      knowledge: "Knowledge",
      shared_resources: "Shared Resources",
      memories: "Memories",
    };
    const cards = items.map((item) => Components.resultCard(item, type)).join("");
    return `
      <div class="result-group">
        <div class="result-group-header">
          ${labels[type] || type}
          <span class="result-count">${items.length}</span>
        </div>
        ${cards}
      </div>`;
  },

  /** Render the search page */
  searchPage() {
    return `
      <div class="search-container">
        <div class="search-box">
          <input type="text" class="search-input" id="searchInput"
                 placeholder="Search knowledge, resources, memories..."
                 onkeydown="if(event.key==='Enter') App.doSearch()">
          <button class="search-btn" onclick="App.doSearch()">Search</button>
        </div>
        <div class="search-options">
          <label><input type="checkbox" id="exactMatch"> Exact match</label>
        </div>
      </div>
      <div class="filter-chips" id="filterChips">
        <button class="chip active" data-type="all" onclick="App.setFilter('all')">All</button>
        <button class="chip" data-type="knowledge" onclick="App.setFilter('knowledge')">Knowledge</button>
        <button class="chip" data-type="shared_resources" onclick="App.setFilter('shared_resources')">Resources</button>
        <button class="chip" data-type="memories" onclick="App.setFilter('memories')">Memories</button>
      </div>
      <div id="searchResults">
        <div class="empty-state">Enter a search query to find entries across the knowledge base.</div>
      </div>`;
  },

  /** Render a project card for the grid */
  projectCard(project) {
    const techHtml = (project.tech_stack || [])
      .map((t) => `<span class="tag">${t}</span>`)
      .join(" ");
    return `
      <div class="project-card" onclick="location.hash='#/projects/${encodeURIComponent(project.name)}'">
        <h3>${project.name}</h3>
        <p>${Components.snippet(project.description, 120)}</p>
        <div class="tech-stack">${techHtml}</div>
      </div>`;
  },

  /** Render the projects grid page */
  projectsPage(projects) {
    if (!projects || projects.length === 0) {
      return `<h2>Projects</h2><div class="empty-state">No projects found.</div>`;
    }
    const cards = projects.map(Components.projectCard).join("");
    return `<h2 style="margin-bottom:1.5rem;">Projects</h2><div class="project-grid">${cards}</div>`;
  },

  /** Render the project detail page */
  projectDetailPage(project, knowledge, resources, memories) {
    const techHtml = (project.tech_stack || [])
      .map((t) => `<span class="tag">${t}</span>`)
      .join(" ");

    const knowledgeList = knowledge.map((item) => Components.resultCard(item, "knowledge")).join("")
      || `<div class="empty-state">No knowledge entries.</div>`;
    const resourceList = resources.map((item) => Components.resultCard(item, "shared_resources")).join("")
      || `<div class="empty-state">No shared resources.</div>`;
    const memoryList = memories.map((item) => Components.resultCard(item, "memories")).join("")
      || `<div class="empty-state">No memories.</div>`;

    return `
      <div class="project-header">
        <h1>${project.name}</h1>
        <p>${project.description || ""}</p>
        <div class="tech-stack">${techHtml}</div>
      </div>
      <div class="tabs">
        <button class="tab active" onclick="App.switchTab('knowledge', this)">
          Knowledge<span class="tab-count">${knowledge.length}</span>
        </button>
        <button class="tab" onclick="App.switchTab('resources', this)">
          Resources<span class="tab-count">${resources.length}</span>
        </button>
        <button class="tab" onclick="App.switchTab('memories', this)">
          Memories<span class="tab-count">${memories.length}</span>
        </button>
      </div>
      <div id="tab-knowledge">${knowledgeList}</div>
      <div id="tab-resources" style="display:none;">${resourceList}</div>
      <div id="tab-memories" style="display:none;">${memoryList}</div>`;
  },
};
