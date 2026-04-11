-- ============================================================
-- OpenBrain Migration: Lifecycle Management & Schema Refactor
-- Run once against an existing database after deploying the
-- updated init.sql. Safe to re-run (idempotent).
-- ============================================================

BEGIN;

-- ----------------------------------------------------------
-- Step 1: Add new columns to existing tables (idempotent)
-- ----------------------------------------------------------

ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE memories ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE projects ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS orphan_policy TEXT;

CREATE INDEX IF NOT EXISTS knowledge_status_idx ON knowledge (status);
CREATE INDEX IF NOT EXISTS knowledge_url_idx ON knowledge (url) WHERE url IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_status_idx ON memories (status);

-- ----------------------------------------------------------
-- Step 2: Create project_links table
-- ----------------------------------------------------------

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

-- ----------------------------------------------------------
-- Step 3: Rename 'general' to 'General' and seed system project
-- ----------------------------------------------------------

UPDATE projects SET name = 'General' WHERE name = 'general';
UPDATE knowledge SET project = 'General' WHERE project = 'general';
UPDATE memories SET project = 'General' WHERE project = 'general';

INSERT INTO projects (name, description, status)
VALUES ('General', 'Default project for non-project-specific knowledge and memories', 'system')
ON CONFLICT (name) DO UPDATE SET status = 'system';

-- ----------------------------------------------------------
-- Step 4: Backfill project_links from knowledge.project
-- ----------------------------------------------------------

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

INSERT INTO project_links (project_id, knowledge_id, status)
SELECT p.id, k.id, 'active'
FROM knowledge k
CROSS JOIN projects p
WHERE p.name = 'General'
  AND (k.project IS NULL OR k.project = '')
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.knowledge_id = k.id
  );

UPDATE knowledge SET project = 'General' WHERE project IS NULL OR project = '';

-- ----------------------------------------------------------
-- Step 5: Backfill project_links from memories.project
-- ----------------------------------------------------------

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

INSERT INTO project_links (project_id, memory_id, status)
SELECT p.id, m.id, 'active'
FROM memories m
CROSS JOIN projects p
WHERE p.name = 'General'
  AND (m.project IS NULL OR m.project = '')
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.memory_id = m.id
  );

UPDATE memories SET project = 'General' WHERE project IS NULL OR project = '';

-- ----------------------------------------------------------
-- Step 6: Migrate shared_resources → knowledge + project_links
-- ----------------------------------------------------------

DO $$
DECLARE
    sr RECORD;
    new_knowledge_id INT;
    proj_id INT;
    content_text TEXT;
    proj_name TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'shared_resources') THEN
        RAISE NOTICE 'shared_resources table does not exist, skipping migration';
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

-- ----------------------------------------------------------
-- Step 7: Drop shared_resources table and related objects
-- ----------------------------------------------------------

DROP FUNCTION IF EXISTS search_shared_resources(vector(1536), INT, TEXT, TEXT);
DROP INDEX IF EXISTS shared_resources_embedding_idx;
DROP TABLE IF EXISTS shared_resources;

COMMIT;
