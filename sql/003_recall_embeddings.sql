-- Create recall_embeddings: per-(recall, field) text vectors + FTS for Path 2 hybrid retrieval.
-- One row per (recall_number, field) — 'reason_for_recall' and 'product_description' — so a
-- single HNSW index covers both fields and adding a new field needs no schema change.
-- Embeddings are an external/derived artifact (OpenAI text-embedding-3-small, 1536-d), populated
-- by src/embed.py; kept OUT of drug_enforcement (which auto-recomputes from raw on re-ingest).
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS recall_embeddings (
    recall_number text NOT NULL,         -- links to drug_enforcement.recall_number
    field         text NOT NULL,         -- 'reason_for_recall' | 'product_description'
    content       text NOT NULL,         -- the exact text that was embedded
    content_hash  text NOT NULL,         -- md5(content); re-embed only when it changes
    embedding     vector(1536),          -- text-embedding-3-small
    -- FTS half of hybrid retrieval; STORED generated so it never drifts from content.
    content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    PRIMARY KEY (recall_number, field)   -- also indexes recall_number for the join back
);

-- Vector ANN: cosine distance (text-embedding-3 vectors are meant for cosine).
CREATE INDEX IF NOT EXISTS recall_embeddings_hnsw
    ON recall_embeddings USING hnsw (embedding vector_cosine_ops);

-- Keyword (FTS) half of hybrid retrieval.
CREATE INDEX IF NOT EXISTS recall_embeddings_tsv_gin
    ON recall_embeddings USING gin (content_tsv);
