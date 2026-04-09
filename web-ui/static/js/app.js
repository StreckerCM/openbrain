/* app.js — router, API client, search controller */

const API = {
  async search(query, exact, types) {
    const resp = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, exact, types }),
    });
    return resp.json();
  },

  async listProjects() {
    const resp = await fetch("/api/projects?select=id,name,description,tech_stack&order=name");
    return resp.json();
  },

  async getProject(name) {
    const resp = await fetch(`/api/projects?name=eq.${encodeURIComponent(name)}&select=id,name,description,repo_url,tech_stack,notes,created_at,updated_at`);
    const rows = await resp.json();
    return rows[0] || null;
  },

  async getProjectKnowledge(name) {
    const resp = await fetch(`/api/knowledge?project=eq.${encodeURIComponent(name)}&select=id,project,category,title,content,tags,created_at,updated_at&order=updated_at.desc`);
    return resp.json();
  },

  async getProjectResources(name) {
    const resp = await fetch(`/api/shared_resources?projects=cs.{${encodeURIComponent(name)}}&select=id,resource_type,name,description,url,projects,metadata,created_at,updated_at&order=updated_at.desc`);
    return resp.json();
  },

  async getProjectMemories(name) {
    const resp = await fetch(`/api/memories?project=eq.${encodeURIComponent(name)}&select=id,memory_type,name,description,content,project,created_at,updated_at&order=updated_at.desc`);
    return resp.json();
  },
};

const App = {
  currentFilter: "all",

  async init() {
    window.addEventListener("hashchange", () => App.route());
    App.route();
  },

  async route() {
    const hash = location.hash || "#/";
    const app = document.getElementById("app");

    // Update active nav link
    document.querySelectorAll(".nav-link").forEach((link) => {
      link.classList.toggle("active", link.getAttribute("href") === hash ||
        (hash.startsWith("#/projects") && link.dataset.route === "projects"));
    });
    if (hash === "#/" || hash === "") {
      document.querySelector('[data-route="search"]').classList.add("active");
    }

    if (hash.startsWith("#/projects/")) {
      const name = decodeURIComponent(hash.replace("#/projects/", ""));
      app.innerHTML = `<div class="loading">Loading project...</div>`;
      await App.showProjectDetail(name);
    } else if (hash === "#/projects") {
      app.innerHTML = `<div class="loading">Loading projects...</div>`;
      await App.showProjects();
    } else {
      app.innerHTML = Components.searchPage();
      document.getElementById("searchInput").focus();
    }
  },

  async showProjects() {
    const projects = await API.listProjects();
    document.getElementById("app").innerHTML = Components.projectsPage(projects);
  },

  async showProjectDetail(name) {
    const [project, knowledge, resources, memories] = await Promise.all([
      API.getProject(name),
      API.getProjectKnowledge(name),
      API.getProjectResources(name),
      API.getProjectMemories(name),
    ]);
    if (!project) {
      document.getElementById("app").innerHTML = `<div class="empty-state">Project "${name}" not found.</div>`;
      return;
    }
    document.getElementById("app").innerHTML = Components.projectDetailPage(project, knowledge, resources, memories);
  },

  async doSearch() {
    const query = document.getElementById("searchInput").value.trim();
    if (!query) return;

    const exact = document.getElementById("exactMatch").checked;
    const types =
      App.currentFilter === "all"
        ? ["knowledge", "shared_resources", "memories"]
        : [App.currentFilter];

    const resultsDiv = document.getElementById("searchResults");
    resultsDiv.innerHTML = `<div class="loading">Searching...</div>`;

    const data = await API.search(query, exact, types);
    const results = data.results || {};

    let html = "";
    for (const type of ["knowledge", "shared_resources", "memories"]) {
      if (results[type]) {
        html += Components.resultGroup(type, results[type]);
      }
    }

    resultsDiv.innerHTML = html || `<div class="empty-state">No results found.</div>`;
  },

  setFilter(type) {
    App.currentFilter = type;
    document.querySelectorAll(".chip").forEach((chip) => {
      chip.classList.toggle("active", chip.dataset.type === type);
    });
    // Re-run search if there's a query
    const input = document.getElementById("searchInput");
    if (input && input.value.trim()) {
      App.doSearch();
    }
  },

  toggleExpand(id) {
    const el = document.getElementById(id);
    if (el) {
      el.style.display = el.style.display === "none" ? "block" : "none";
    }
  },

  switchTab(tab, btn) {
    // Hide all tab panes
    document.querySelectorAll("[id^='tab-']").forEach((el) => (el.style.display = "none"));
    // Show selected
    document.getElementById(`tab-${tab}`).style.display = "block";
    // Update active tab button
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
  },
};

document.addEventListener("DOMContentLoaded", App.init);
