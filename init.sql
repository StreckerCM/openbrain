-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Project knowledge base table
CREATE TABLE IF NOT EXISTS knowledge (
    id SERIAL PRIMARY KEY,
    project TEXT NOT NULL DEFAULT 'general',
    category TEXT NOT NULL DEFAULT 'general',
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
    project TEXT DEFAULT 'general',
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

-- Seed the default 'general' project
INSERT INTO projects (name, description, status)
VALUES ('general', 'Default project for non-project-specific knowledge and memories', 'system')
ON CONFLICT (name) DO NOTHING;

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
        SELECT
            k.id, k.project, k.category, k.title, k.content, k.url, k.tags, k.status,
            1 - (k.embedding <=> query_embedding) AS similarity
        FROM knowledge k
        JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
        JOIN projects p ON p.id = pl.project_id AND p.name = filter_project
        WHERE k.status = filter_status
          AND k.embedding IS NOT NULL
        ORDER BY k.embedding <=> query_embedding
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
        SELECT
            m.id, m.memory_type, m.name, m.description, m.content, m.project, m.status,
            1 - (m.embedding <=> query_embedding) AS similarity
        FROM memories m
        JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
        JOIN projects p ON p.id = pl.project_id AND p.name = filter_project
        WHERE (filter_type IS NULL OR m.memory_type = filter_type)
          AND m.status = filter_status
          AND m.embedding IS NOT NULL
        ORDER BY m.embedding <=> query_embedding
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
