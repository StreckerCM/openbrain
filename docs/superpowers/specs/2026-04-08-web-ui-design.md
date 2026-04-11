# OpenBrain Web UI — Design Spec

**Date:** 2026-04-08
**Status:** Draft
**Phase:** 2

## Overview

A browser-based UI for searching and browsing the OpenBrain knowledge base. Primarily a personal tool — clean, utilitarian, and user-friendly. Agents continue using the MCP gateway; this UI is for human access.

## Goals

1. **Unified semantic search** across knowledge, shared resources, and memories from a single search box
2. **Project browsing** — view all projects, drill into a project to see its knowledge, resources, and memories
3. **Keep MCP gateway untouched** — the UI is a separate stack that talks directly to the database

## Non-Goals

- Activity feed / recent changes (deferred to a future version)
- Write/edit capabilities (read-only for now — agents handle writes via MCP)
- Authentication (access already restricted by network — LAN, Tailscale, WireGuard)

## Architecture

```
Browser
  |
Nginx (web-ui container, port 3010 external / 80 internal)
  |-- /              -> static HTML/CSS/JS (SPA)
  |-- /api/          -> proxy to PostgREST (internal port 3000)
  |-- /api/search    -> proxy to search-service (internal port 3002)

PostgREST (existing, port 3006 external / 3000 internal)
  -> handles listing, filtering, project details

search-service (new Python container, port 3009 external / 3002 internal)
  -> receives search text, calls OpenAI for query embedding, queries pgvector
  -> returns ranked results grouped by type
```

### Component Responsibilities

**web-ui (new container):** Nginx serving a static single-page application. Also acts as a reverse proxy so the frontend talks to a single origin — Nginx routes `/api/` to PostgREST and `/api/search` to the search service. This avoids CORS issues.

**search-service (new container):** Minimal Python service (Starlette) with two dependencies: `httpx` (OpenAI embedding calls) and `asyncpg` (DB queries). Exposes a single `/search` endpoint. Shares the same `OPENAI_API_KEY` and DB credentials as the embedder.

**PostgREST (existing):** Already in the stack. Handles all listing and filtering queries from the frontend (projects, knowledge, shared resources, memories).

**No changes to:** mcp-gateway, embedder, db, adminer, or docs containers.

## Pages & Navigation

Top nav bar with two entries: **Search** | **Projects**

### 1. Search (home page, `/`)

- Single search box, prominent and centered
- "Exact match" checkbox below the search box
- Type filter chips: All | Knowledge | Shared Resources | Memories
- Results displayed below, grouped by type with counts per type
- Each result shows: title/name, content snippet, project tag, type badge
- Clicking a result expands it inline to show full content and metadata (tags, category, dates)
- Semantic search by default; exact match uses case-insensitive text matching

### 2. Projects (`/projects`)

- Card grid showing all registered projects
- Each card displays: project name, description, tech stack badges
- Clicking a card navigates to the project detail view

### 3. Project Detail (`/projects/:name`)

- Header: project name, description, tech stack badges
- Three tabbed sections:
  - **Knowledge** — entries filtered to this project, sortable by date and category
  - **Shared Resources** — resources linked to this project
  - **Memories** — memories scoped to this project
- Each tab shows a filterable list with expandable rows for full content

## Search Service API

### `POST /search`

**Request:**
```json
{
  "query": "minimum curvature shape factor",
  "exact": false,
  "types": ["knowledge", "shared_resources", "memories"]
}
```

- `query` (string, required): Search text
- `exact` (boolean, default: false): If true, uses `ILIKE` text matching instead of vector similarity
- `types` (string array, optional): Filter to specific tables. Default: all three

**Response:**
```json
{
  "results": {
    "knowledge": [
      {
        "id": 58,
        "title": "Minimum Curvature Shape Factor...",
        "content": "...",
        "project": "DownholePro",
        "category": "algorithms",
        "tags": ["minimum-curvature", "shape-factor"],
        "similarity": 0.89,
        "updated_at": "2026-03-19T03:40:09Z"
      }
    ],
    "shared_resources": [...],
    "memories": [...]
  }
}
```

**Semantic search flow:**
1. Receive query text
2. Call OpenAI `text-embedding-3-small` to get a 1536-dim vector
3. Run `SELECT ... ORDER BY embedding <=> $vector LIMIT 20` against each requested table
4. Group by type, return sorted by similarity score

**Exact match flow:**
1. Run `WHERE content ILIKE '%query%' OR title ILIKE '%query%' OR name ILIKE '%query%'` against each requested table
2. Group by type, return results

## Frontend Stack

**Plain HTML/CSS/JS — no framework, no build step.**

```
web-ui/
  static/
    index.html          # SPA shell with nav bar
    css/
      style.css         # Single stylesheet, CSS custom properties for theming
    js/
      app.js            # Hash-based router, search logic, API calls
      components.js     # Render functions (result cards, project cards, tabs)
  nginx.conf            # Nginx config with proxy rules
  Dockerfile            # Nginx-based image
```

**Routing:** Hash-based (`#/`, `#/projects`, `#/projects/DownholePro`). Nginx serves `index.html` for all paths; JS handles navigation.

**Styling:** CSS custom properties for a consistent color palette. Simple responsive grid. No CSS framework.

**Data fetching patterns:**
- Search: `POST /api/search` (search service)
- List projects: `GET /api/projects` (PostgREST)
- Project knowledge: `GET /api/knowledge?project=eq.DownholePro&order=updated_at.desc&select=id,project,category,title,content,tags,created_at,updated_at` (PostgREST)
- Project resources: `GET /api/shared_resources?projects=cs.{DownholePro}&order=updated_at.desc&select=id,resource_type,name,description,url,projects,metadata,created_at,updated_at` (PostgREST)
- Project memories: `GET /api/memories?project=eq.DownholePro&order=updated_at.desc&select=id,memory_type,name,description,content,project,created_at,updated_at` (PostgREST)

**Note:** All PostgREST queries use explicit `select=` to exclude the `embedding` column, which contains large 1536-dim vectors not needed by the frontend.

## Docker Configuration

### New services in `docker-compose.yml`

**search-service:**
```yaml
search-service:
  build: ./search-service
  ports:
    - "3009:3002"
  environment:
    - DATABASE_URL=postgresql://openbrain:${POSTGRES_PASSWORD}@db:5432/openbrain
    - OPENAI_API_KEY=${OPENAI_API_KEY}
  depends_on:
    db:
      condition: service_healthy
  restart: unless-stopped
```

**web-ui:**
```yaml
web-ui:
  build: ./web-ui
  ports:
    - "3010:80"
  depends_on:
    - postgrest
    - search-service
  restart: unless-stopped
```

### Nginx config (`web-ui/nginx.conf`)

```nginx
server {
    listen 80;

    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }

    location /api/search {
        proxy_pass http://search-service:3002/search;
    }

    location /api/ {
        proxy_pass http://postgrest:3000/;
    }
}
```

### Updated port map

| Service | External Port | Purpose |
|---------|--------------|---------|
| db | 5433 | PostgreSQL |
| mcp-gateway | 3007 | MCP endpoint (agents) |
| postgrest | 3006 | REST API |
| adminer | 3008 | DB browser |
| search-service | 3009 | Semantic search API |
| web-ui | 3010 | Browser UI |

### Reverse proxy

Nginx Proxy Manager routes `brain.streckercm.com` to the web-ui container (port 3010), similar to how `/mcp` currently routes to mcp-gateway.

## File Structure (new directories)

```
openbrain/
  search-service/
    server.py           # Starlette app with /search endpoint
    requirements.txt    # asyncpg, httpx, starlette, uvicorn
    Dockerfile
  web-ui/
    static/
      index.html
      css/style.css
      js/app.js
      js/components.js
    nginx.conf
    Dockerfile
```
