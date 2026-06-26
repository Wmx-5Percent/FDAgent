-- Generalize recall_embeddings -> embeddings for MULTIPLE data sources (Path 2).
-- The original table keyed on drug_enforcement's recall_number; future FDA datasets
-- (device/food enforcement, adverse-event reports, ...) have different id fields, so we
-- add a `source` dimension and rename the id column to the generic `source_id`. The key
-- becomes (source, source_id, field) — globally unique even if ids collide across sources.
-- In-place: preserves the existing vectors (no re-embedding). Idempotent: safe to re-run.

ALTER TABLE IF EXISTS recall_embeddings RENAME TO embeddings;

-- recall_number -> source_id (guarded so re-runs don't error)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'embeddings' AND column_name = 'recall_number') THEN
        ALTER TABLE embeddings RENAME COLUMN recall_number TO source_id;
    END IF;
END $$;

-- add the source discriminator; backfill existing rows, then require it explicitly going forward
ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'drug_enforcement';
ALTER TABLE embeddings ALTER COLUMN source DROP DEFAULT;

-- repoint the primary key to (source, source_id, field)
ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS recall_embeddings_pkey;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'embeddings_pkey') THEN
        ALTER TABLE embeddings ADD CONSTRAINT embeddings_pkey PRIMARY KEY (source, source_id, field);
    END IF;
END $$;

-- keep index names in step with the table
ALTER INDEX IF EXISTS recall_embeddings_hnsw RENAME TO embeddings_hnsw;
ALTER INDEX IF EXISTS recall_embeddings_tsv_gin RENAME TO embeddings_tsv_gin;
