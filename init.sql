-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Project knowledge base table
CREATE TABLE IF NOT EXISTS knowledge (
    id SERIAL PRIMARY KEY,
    project TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Shared resources table (cross-project)
CREATE TABLE IF NOT EXISTS shared_resources (
    id SERIAL PRIMARY KEY,
    resource_type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    url TEXT,
    projects TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
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
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes for vector similarity search
CREATE INDEX IF NOT EXISTS knowledge_embedding_idx ON knowledge USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS shared_resources_embedding_idx ON shared_resources USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS knowledge_project_idx ON knowledge (project);
CREATE INDEX IF NOT EXISTS knowledge_category_idx ON knowledge (category);

-- Cosine similarity search function
CREATE OR REPLACE FUNCTION search_knowledge(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_project TEXT DEFAULT NULL
)
RETURNS TABLE (
    id INT,
    project TEXT,
    category TEXT,
    title TEXT,
    content TEXT,
    tags TEXT[],
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        k.id,
        k.project,
        k.category,
        k.title,
        k.content,
        k.tags,
        1 - (k.embedding <=> query_embedding) AS similarity
    FROM knowledge k
    WHERE (filter_project IS NULL OR k.project = filter_project)
      AND k.embedding IS NOT NULL
    ORDER BY k.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

