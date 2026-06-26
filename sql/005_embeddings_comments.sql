-- Column documentation for `embeddings` (Path 2 vector + full-text store).
-- This is a PROJECT-OWNED, derived/rebuildable table — none of its columns are openFDA
-- fields, so every comment below describes our own schema (no verbatim dictionary source).
-- Stored as Postgres COMMENTs so the schema is self-describing (psql \d+, col_description,
-- and the Text-to-SQL / retrieval layers). Idempotent: COMMENT ON overwrites; safe to re-run.

COMMENT ON TABLE embeddings IS
    'Vector + full-text index over FDA source text, for Path 2 semantic / hybrid retrieval. One row per (source, source_id, field): each row embeds one text field of one source record. Derived & rebuildable from the source tables via src/embed.py — NOT a source of truth. [project table, not from openFDA]';

COMMENT ON COLUMN embeddings.source IS
    'Which dataset this row came from (= the source table name), e.g. ''drug_enforcement''. Part of the primary key; lets one index serve many FDA datasets and disambiguates ids that may collide across sources. [project field, not from openFDA]';
COMMENT ON COLUMN embeddings.source_id IS
    'The source record''s natural key within its source (e.g. drug_enforcement.recall_number). Join back to the source table on this. Part of the primary key. [project field, not from openFDA]';
COMMENT ON COLUMN embeddings.field IS
    'Which text field of the source record was embedded, e.g. ''reason_for_recall'' or ''product_description''. Part of the primary key; one row per (source, source_id, field). [project field, not from openFDA]';
COMMENT ON COLUMN embeddings.content IS
    'The exact source text that was embedded, kept verbatim for full-text search, reranking, and display. [project field, not from openFDA]';
COMMENT ON COLUMN embeddings.content_hash IS
    'md5(content). Drives incremental re-embedding: a row is re-embedded only when its source text changes (hash differs), mirroring fetch_openfda --since auto. [project field, not from openFDA]';
COMMENT ON COLUMN embeddings.embedding IS
    'OpenAI text-embedding-3-small vector (1536-d) of content; cosine distance (<=>) via the HNSW index powers semantic nearest-neighbour search. [project field, not from openFDA]';
COMMENT ON COLUMN embeddings.content_tsv IS
    'English full-text-search vector, GENERATED ALWAYS from content (so it never drifts) and GIN-indexed. The keyword half of the planned hybrid (vector + FTS) retrieval. [project field, not from openFDA]';
