-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Project knowledge base table
CREATE TABLE IF NOT EXISTS knowledge (
    id SERIAL PRIMARY KEY,
    project TEXT NOT NULL DEFAULT 'General',
    category TEXT NOT NULL DEFAULT 'General',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT,
    tags TEXT[] DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Project registry
CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    repo_url TEXT,
    tech_stack TEXT[] DEFAULT '{}',
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    orphan_policy TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Memories table (persistent agent memory)
CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    memory_type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    project TEXT DEFAULT 'General',
    status TEXT NOT NULL DEFAULT 'active',
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes for vector similarity search
CREATE INDEX IF NOT EXISTS knowledge_embedding_idx ON knowledge USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS knowledge_project_idx ON knowledge (project);
CREATE INDEX IF NOT EXISTS knowledge_category_idx ON knowledge (category);

-- Indexes for memories
CREATE INDEX IF NOT EXISTS memories_type_idx ON memories (memory_type);
CREATE INDEX IF NOT EXISTS memories_project_idx ON memories (project);
CREATE INDEX IF NOT EXISTS memories_embedding_idx ON memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Project links junction table (many-to-many project-entity relationships)
CREATE TABLE IF NOT EXISTS project_links (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id),
    knowledge_id INT REFERENCES knowledge(id),
    memory_id INT REFERENCES memories(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT exactly_one_entity CHECK (
        (knowledge_id IS NOT NULL AND memory_id IS NULL) OR
        (knowledge_id IS NULL AND memory_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS project_links_knowledge_unique
    ON project_links (project_id, knowledge_id)
    WHERE knowledge_id IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS project_links_memory_unique
    ON project_links (project_id, memory_id)
    WHERE memory_id IS NOT NULL AND status = 'active';

CREATE INDEX IF NOT EXISTS project_links_project_idx ON project_links (project_id);
CREATE INDEX IF NOT EXISTS project_links_knowledge_idx ON project_links (knowledge_id) WHERE knowledge_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_links_memory_idx ON project_links (memory_id) WHERE memory_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_links_status_idx ON project_links (status);

-- ============================================================
-- Migration: ensure columns exist on older databases (idempotent)
-- ============================================================
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS orphan_policy TEXT;

-- Rename 'general' to 'General' if upgrading from older schema
UPDATE projects SET name = 'General' WHERE name = 'general';
UPDATE knowledge SET project = 'General' WHERE project = 'general';
UPDATE memories SET project = 'General' WHERE project = 'general';

-- Seed the default 'General' project
INSERT INTO projects (name, description, status)
VALUES ('General', 'Default project for non-project-specific knowledge and memories', 'system')
ON CONFLICT (name) DO UPDATE SET status = 'system';

-- Backfill project_links from knowledge.project (idempotent)
INSERT INTO projects (name, status)
SELECT DISTINCT k.project, 'active'
FROM knowledge k
WHERE k.project IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.name = k.project)
ON CONFLICT (name) DO NOTHING;

INSERT INTO project_links (project_id, knowledge_id, status)
SELECT p.id, k.id, 'active'
FROM knowledge k
JOIN projects p ON p.name = k.project
WHERE k.project IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.knowledge_id = k.id
  );

-- Backfill project_links from memories.project (idempotent)
INSERT INTO projects (name, status)
SELECT DISTINCT m.project, 'active'
FROM memories m
WHERE m.project IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.name = m.project)
ON CONFLICT (name) DO NOTHING;

INSERT INTO project_links (project_id, memory_id, status)
SELECT p.id, m.id, 'active'
FROM memories m
JOIN projects p ON p.name = m.project
WHERE m.project IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.memory_id = m.id
  );

-- Migrate shared_resources if table exists
DO $$
DECLARE
    sr RECORD;
    new_knowledge_id INT;
    proj_id INT;
    content_text TEXT;
    proj_name TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'shared_resources') THEN
        RETURN;
    END IF;

    FOR sr IN SELECT * FROM shared_resources LOOP
        content_text := COALESCE(sr.description, sr.name);
        IF sr.metadata IS NOT NULL AND sr.metadata::text != '{}' THEN
            content_text := content_text || E'\n\nMetadata:\n' || jsonb_pretty(sr.metadata);
        END IF;

        SELECT k.id INTO new_knowledge_id
        FROM knowledge k
        WHERE k.title = sr.name AND k.category = sr.resource_type
        LIMIT 1;

        IF new_knowledge_id IS NULL THEN
            INSERT INTO knowledge (project, category, title, content, url, status)
            VALUES ('General', sr.resource_type, sr.name, content_text, sr.url, 'active')
            RETURNING id INTO new_knowledge_id;
        END IF;

        IF sr.projects IS NOT NULL THEN
            FOREACH proj_name IN ARRAY sr.projects LOOP
                INSERT INTO projects (name, status)
                VALUES (proj_name, 'active')
                ON CONFLICT (name) DO NOTHING;

                SELECT p.id INTO proj_id FROM projects p WHERE p.name = proj_name;

                INSERT INTO project_links (project_id, knowledge_id, status)
                VALUES (proj_id, new_knowledge_id, 'active')
                ON CONFLICT DO NOTHING;
            END LOOP;
        ELSE
            SELECT p.id INTO proj_id FROM projects p WHERE p.name = 'General';
            INSERT INTO project_links (project_id, knowledge_id, status)
            VALUES (proj_id, new_knowledge_id, 'active')
            ON CONFLICT DO NOTHING;
        END IF;
    END LOOP;
END $$;

-- Drop shared_resources if it was migrated
DROP FUNCTION IF EXISTS search_shared_resources(vector(1536), INT, TEXT, TEXT);
DROP TABLE IF EXISTS shared_resources;

-- Status and URL indexes
CREATE INDEX IF NOT EXISTS knowledge_status_idx ON knowledge (status);
CREATE INDEX IF NOT EXISTS knowledge_url_idx ON knowledge (url) WHERE url IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_status_idx ON memories (status);

-- Cosine similarity search function for knowledge
CREATE OR REPLACE FUNCTION search_knowledge(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_project TEXT DEFAULT NULL,
    filter_status TEXT DEFAULT 'active'
)
RETURNS TABLE (
    id INT,
    project TEXT,
    category TEXT,
    title TEXT,
    content TEXT,
    url TEXT,
    tags TEXT[],
    status TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF filter_project IS NOT NULL THEN
        RETURN QUERY
        SELECT DISTINCT
            k.id, k.project, k.category, k.title, k.content, k.url, k.tags, k.status,
            1 - (k.embedding <=> query_embedding) AS similarity
        FROM knowledge k
        JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
        JOIN projects p ON p.id = pl.project_id AND p.name = filter_project
        WHERE k.status = filter_status
          AND k.embedding IS NOT NULL
        ORDER BY similarity DESC
        LIMIT match_count;
    ELSE
        RETURN QUERY
        SELECT
            k.id, k.project, k.category, k.title, k.content, k.url, k.tags, k.status,
            1 - (k.embedding <=> query_embedding) AS similarity
        FROM knowledge k
        WHERE k.status = filter_status
          AND k.embedding IS NOT NULL
        ORDER BY k.embedding <=> query_embedding
        LIMIT match_count;
    END IF;
END;
$$;

-- Semantic search function for memories
CREATE OR REPLACE FUNCTION search_memories(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_type TEXT DEFAULT NULL,
    filter_project TEXT DEFAULT NULL,
    filter_status TEXT DEFAULT 'active'
)
RETURNS TABLE (
    id INT,
    memory_type TEXT,
    name TEXT,
    description TEXT,
    content TEXT,
    project TEXT,
    status TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF filter_project IS NOT NULL THEN
        RETURN QUERY
        SELECT DISTINCT
            m.id, m.memory_type, m.name, m.description, m.content, m.project, m.status,
            1 - (m.embedding <=> query_embedding) AS similarity
        FROM memories m
        JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
        JOIN projects p ON p.id = pl.project_id AND p.name = filter_project
        WHERE (filter_type IS NULL OR m.memory_type = filter_type)
          AND m.status = filter_status
          AND m.embedding IS NOT NULL
        ORDER BY similarity DESC
        LIMIT match_count;
    ELSE
        RETURN QUERY
        SELECT
            m.id, m.memory_type, m.name, m.description, m.content, m.project, m.status,
            1 - (m.embedding <=> query_embedding) AS similarity
        FROM memories m
        WHERE (filter_type IS NULL OR m.memory_type = filter_type)
          AND m.status = filter_status
          AND m.embedding IS NOT NULL
        ORDER BY m.embedding <=> query_embedding
        LIMIT match_count;
    END IF;
END;
$$;

-- Drop views before recreating (column changes cause mismatches on upgrades)
DROP VIEW IF EXISTS knowledge_with_projects CASCADE;
DROP VIEW IF EXISTS memories_with_projects CASCADE;
DROP VIEW IF EXISTS recent_activity CASCADE;
DROP VIEW IF EXISTS tag_stats CASCADE;

-- View: knowledge items with their associated project names
CREATE OR REPLACE VIEW knowledge_with_projects AS
SELECT k.*,
       COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS projects
FROM knowledge k
LEFT JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
LEFT JOIN projects p ON p.id = pl.project_id
GROUP BY k.id;

-- View: memory items with their associated project names
CREATE OR REPLACE VIEW memories_with_projects AS
SELECT m.*,
       COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS projects
FROM memories m
LEFT JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
LEFT JOIN projects p ON p.id = pl.project_id
GROUP BY m.id;

-- View: recent activity across knowledge and memories
CREATE OR REPLACE VIEW recent_activity AS
SELECT id, 'knowledge' AS type, title AS name, category AS subtype, updated_at
FROM knowledge WHERE status = 'active'
UNION ALL
SELECT id, 'memory' AS type, name, memory_type AS subtype, updated_at
FROM memories WHERE status = 'active';

-- View: distinct tags with usage counts and last-used timestamps
CREATE OR REPLACE VIEW tag_stats AS
SELECT
    tag,
    COUNT(*) AS entry_count,
    MAX(k.updated_at) AS last_used
FROM knowledge k,
     LATERAL UNNEST(k.tags) AS tag
WHERE k.status = 'active'
GROUP BY tag
ORDER BY entry_count DESC, tag ASC;

-- Notify PostgREST to reload schema so the new view is exposed
NOTIFY pgrst, 'reload schema';

-- Function: return active knowledge and memories not linked to any active project
CREATE OR REPLACE FUNCTION orphaned_items()
RETURNS TABLE(id INT, type TEXT, name TEXT, updated_at TIMESTAMPTZ) AS $$
  SELECT k.id, 'knowledge', k.title, k.updated_at
  FROM knowledge k
  LEFT JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
  WHERE k.status = 'active' AND pl.id IS NULL
  UNION ALL
  SELECT m.id, 'memory', m.name, m.updated_at
  FROM memories m
  LEFT JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
  WHERE m.status = 'active' AND pl.id IS NULL
  ORDER BY updated_at DESC;
$$ LANGUAGE sql STABLE;
