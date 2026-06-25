-- Parse drug_enforcement.raw (JSONB) into typed, indexed columns.
-- Strategy: STORED generated columns. They are physically stored ("存放在数据库中"),
-- queryable/indexable, and auto-recompute whenever raw changes (e.g. on incremental
-- re-ingest via ON CONFLICT DO UPDATE) — so the parsed view never drifts from raw.
-- Idempotent: safe to re-run.

-- openFDA dates are 'YYYYMMDD' strings. Generated columns require an IMMUTABLE
-- expression, so wrap the conversion (to_date is only STABLE) in an immutable,
-- input-validating helper that returns NULL for missing/blank/malformed values.
CREATE OR REPLACE FUNCTION fda_yyyymmdd(s text) RETURNS date
    LANGUAGE sql IMMUTABLE RETURNS NULL ON NULL INPUT
    AS $$ SELECT CASE WHEN s ~ '^[0-9]{8}$' THEN to_date(s, 'YYYYMMDD') END $$;

-- Text fields (one column per scalar key in raw). report_date already exists as a
-- real column (populated by the ingester), so it is intentionally not duplicated here.
ALTER TABLE drug_enforcement
    ADD COLUMN IF NOT EXISTS recall_number             text GENERATED ALWAYS AS (raw->>'recall_number') STORED,
    ADD COLUMN IF NOT EXISTS event_id                  text GENERATED ALWAYS AS (raw->>'event_id') STORED,
    ADD COLUMN IF NOT EXISTS status                    text GENERATED ALWAYS AS (raw->>'status') STORED,
    ADD COLUMN IF NOT EXISTS classification            text GENERATED ALWAYS AS (raw->>'classification') STORED,
    ADD COLUMN IF NOT EXISTS product_type              text GENERATED ALWAYS AS (raw->>'product_type') STORED,
    ADD COLUMN IF NOT EXISTS voluntary_mandated        text GENERATED ALWAYS AS (raw->>'voluntary_mandated') STORED,
    ADD COLUMN IF NOT EXISTS initial_firm_notification text GENERATED ALWAYS AS (raw->>'initial_firm_notification') STORED,
    ADD COLUMN IF NOT EXISTS recalling_firm            text GENERATED ALWAYS AS (raw->>'recalling_firm') STORED,
    ADD COLUMN IF NOT EXISTS address_1                 text GENERATED ALWAYS AS (raw->>'address_1') STORED,
    ADD COLUMN IF NOT EXISTS address_2                 text GENERATED ALWAYS AS (raw->>'address_2') STORED,
    ADD COLUMN IF NOT EXISTS city                      text GENERATED ALWAYS AS (raw->>'city') STORED,
    ADD COLUMN IF NOT EXISTS state                     text GENERATED ALWAYS AS (raw->>'state') STORED,
    ADD COLUMN IF NOT EXISTS country                   text GENERATED ALWAYS AS (raw->>'country') STORED,
    ADD COLUMN IF NOT EXISTS postal_code               text GENERATED ALWAYS AS (raw->>'postal_code') STORED,
    ADD COLUMN IF NOT EXISTS distribution_pattern      text GENERATED ALWAYS AS (raw->>'distribution_pattern') STORED,
    ADD COLUMN IF NOT EXISTS product_description       text GENERATED ALWAYS AS (raw->>'product_description') STORED,
    ADD COLUMN IF NOT EXISTS product_quantity          text GENERATED ALWAYS AS (raw->>'product_quantity') STORED,
    ADD COLUMN IF NOT EXISTS reason_for_recall         text GENERATED ALWAYS AS (raw->>'reason_for_recall') STORED,
    ADD COLUMN IF NOT EXISTS code_info                 text GENERATED ALWAYS AS (raw->>'code_info') STORED,
    ADD COLUMN IF NOT EXISTS more_code_info            text GENERATED ALWAYS AS (raw->>'more_code_info') STORED;

-- Date fields (parsed YYYYMMDD -> date).
ALTER TABLE drug_enforcement
    ADD COLUMN IF NOT EXISTS recall_initiation_date     date GENERATED ALWAYS AS (fda_yyyymmdd(raw->>'recall_initiation_date')) STORED,
    ADD COLUMN IF NOT EXISTS center_classification_date date GENERATED ALWAYS AS (fda_yyyymmdd(raw->>'center_classification_date')) STORED,
    ADD COLUMN IF NOT EXISTS termination_date           date GENERATED ALWAYS AS (fda_yyyymmdd(raw->>'termination_date')) STORED;

-- Indexes on the high-value filter/aggregation dimensions (Tier-A in the
-- frequency-query design). Skipped product_type (single value) and free-text fields.
CREATE INDEX IF NOT EXISTS drug_enforcement_classification_idx         ON drug_enforcement (classification);
CREATE INDEX IF NOT EXISTS drug_enforcement_status_idx                 ON drug_enforcement (status);
CREATE INDEX IF NOT EXISTS drug_enforcement_state_idx                  ON drug_enforcement (state);
CREATE INDEX IF NOT EXISTS drug_enforcement_recalling_firm_idx         ON drug_enforcement (recalling_firm);
CREATE INDEX IF NOT EXISTS drug_enforcement_voluntary_mandated_idx     ON drug_enforcement (voluntary_mandated);
CREATE INDEX IF NOT EXISTS drug_enforcement_recall_initiation_date_idx ON drug_enforcement (recall_initiation_date);
