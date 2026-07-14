-- Create hybrid_search_log: versioned retrieval-lab traces for /hybrid-search.
-- This table is separate from query_log because it records retrieval internals for
-- run-to-run comparison, not user-facing /ask answers. Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS hybrid_search_log (
    id                  bigserial PRIMARY KEY,
    created_at          timestamptz NOT NULL DEFAULT now(),
    query               text NOT NULL,
    field               text NOT NULL CHECK (field IN ('reason_for_recall', 'product_description', 'both')),
    k                   integer NOT NULL CHECK (k > 0),
    filters             jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding_provider  text NOT NULL,
    embedding_model     text NOT NULL,
    retrieval_mode      text NOT NULL,
    fallback_reason     text,
    vector_hit_count    integer NOT NULL DEFAULT 0 CHECK (vector_hit_count >= 0),
    fts_hit_count       integer NOT NULL DEFAULT 0 CHECK (fts_hit_count >= 0),
    fused_hit_count     integer NOT NULL DEFAULT 0 CHECK (fused_hit_count >= 0),
    top_recall_numbers  jsonb NOT NULL DEFAULT '[]'::jsonb,
    timings_ms          jsonb NOT NULL DEFAULT '{}'::jsonb,
    request             jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_type          text,
    error_message       text
);

CREATE INDEX IF NOT EXISTS hybrid_search_log_created_at_idx
    ON hybrid_search_log (created_at DESC);

CREATE INDEX IF NOT EXISTS hybrid_search_log_mode_created_at_idx
    ON hybrid_search_log (retrieval_mode, created_at DESC);

CREATE INDEX IF NOT EXISTS hybrid_search_log_field_created_at_idx
    ON hybrid_search_log (field, created_at DESC);

CREATE INDEX IF NOT EXISTS hybrid_search_log_filters_gin
    ON hybrid_search_log USING gin (filters jsonb_path_ops);

CREATE INDEX IF NOT EXISTS hybrid_search_log_top_recall_numbers_gin
    ON hybrid_search_log USING gin (top_recall_numbers jsonb_path_ops);

COMMENT ON TABLE hybrid_search_log IS
    'Versioned retrieval-lab trace table for /hybrid-search. Stores safe query, filter, provider/model, retrieval-mode, fallback, hit-count, timing, and top-hit metadata for debugging hybrid retrieval. [project table, not from openFDA]';

COMMENT ON COLUMN hybrid_search_log.id IS
    'Surrogate primary key for one /hybrid-search lab run. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.created_at IS
    'Server timestamp when the log row was inserted. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.query IS
    'Natural-language retrieval query submitted to the lab endpoint. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.field IS
    'Embedded recall text field searched by the lab: reason_for_recall, product_description, or both. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.k IS
    'Requested number of fused retrieval rows to return. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.filters IS
    'Validated hard filters applied to drug_enforcement before vector/FTS retrieval, stored as JSON for run comparison. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.embedding_provider IS
    'Embedding provider name used for query embeddings, e.g. openai or openrouter; secrets and base URLs are never stored. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.embedding_model IS
    'Embedding model name used for query embeddings, e.g. openai/text-embedding-3-small. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.retrieval_mode IS
    'Actual retrieval mode, e.g. hybrid or fts_only. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.fallback_reason IS
    'Safe exception type or degradation reason when query embeddings were unavailable; NULL for normal hybrid runs. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.vector_hit_count IS
    'Number of raw vector candidates collected before RRF fusion. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.fts_hit_count IS
    'Number of raw FTS candidates collected before RRF fusion. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.fused_hit_count IS
    'Number of distinct candidates after RRF fusion before the final top-k cut. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.top_recall_numbers IS
    'Recall numbers returned in final fused order for quick run-to-run comparison. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.timings_ms IS
    'Operational timing metadata in milliseconds for embedding, vector search, FTS search, fusion, logging, and total endpoint handling. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.request IS
    'Safe request payload accepted by /hybrid-search. Does not include environment variables or credentials. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.response_metadata IS
    'Compact response metadata such as row counts, aliases, FTS queries, and top hit identifiers; does not duplicate full long-text rows. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.error_type IS
    'Exception class name for handled lab endpoint errors. NULL for successful runs. [project field, not from openFDA]';
COMMENT ON COLUMN hybrid_search_log.error_message IS
    'Short safe error message for handled lab endpoint errors. NULL for successful runs. [project field, not from openFDA]';
