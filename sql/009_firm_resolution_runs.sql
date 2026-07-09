-- Create firm-resolution run/pair audit tables for incremental company normalization.
-- These tables make `src/firm/resolve.py --mode incremental --apply` auditable:
-- every run records its source, thresholds, mode, status, summary stats, and every
-- candidate pair decision. Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS firm_resolution_run (
    id                       bigserial PRIMARY KEY,
    created_at               timestamptz NOT NULL DEFAULT now(),
    started_at               timestamptz NOT NULL DEFAULT now(),
    completed_at             timestamptz,
    status                   text NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'succeeded', 'failed')),
    mode                     text NOT NULL
                             CHECK (mode IN ('full', 'incremental', 'calibration')),
    apply_writes             boolean NOT NULL DEFAULT false,
    source_table             text NOT NULL CHECK (btrim(source_table) <> ''),
    source_field             text NOT NULL CHECK (btrim(source_field) <> ''),
    limit_rows               integer CHECK (limit_rows IS NULL OR limit_rows >= 0),
    candidate_threshold      numeric(6,5) NOT NULL CHECK (candidate_threshold >= 0 AND candidate_threshold <= 1),
    auto_merge_threshold     numeric(6,5) NOT NULL CHECK (auto_merge_threshold >= 0 AND auto_merge_threshold <= 1),
    review_threshold         numeric(6,5) NOT NULL CHECK (review_threshold >= 0 AND review_threshold <= 1),
    token_threshold          numeric(6,5) NOT NULL CHECK (token_threshold >= 0 AND token_threshold <= 1),
    verify_llm               boolean NOT NULL DEFAULT false,
    llm_model                text,
    llm_confidence_threshold numeric(6,5) NOT NULL CHECK (llm_confidence_threshold >= 0 AND llm_confidence_threshold <= 1),
    source_value_count       integer NOT NULL DEFAULT 0 CHECK (source_value_count >= 0),
    selected_value_count     integer NOT NULL DEFAULT 0 CHECK (selected_value_count >= 0),
    skipped_value_count      integer NOT NULL DEFAULT 0 CHECK (skipped_value_count >= 0),
    candidate_pair_count     integer NOT NULL DEFAULT 0 CHECK (candidate_pair_count >= 0),
    accepted_pair_count      integer NOT NULL DEFAULT 0 CHECK (accepted_pair_count >= 0),
    review_pair_count        integer NOT NULL DEFAULT 0 CHECK (review_pair_count >= 0),
    rejected_pair_count      integer NOT NULL DEFAULT 0 CHECK (rejected_pair_count >= 0),
    firm_rows_touched        integer NOT NULL DEFAULT 0 CHECK (firm_rows_touched >= 0),
    alias_rows_touched       integer NOT NULL DEFAULT 0 CHECK (alias_rows_touched >= 0),
    log_rows_written         integer NOT NULL DEFAULT 0 CHECK (log_rows_written >= 0),
    stats                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message            text
);

CREATE INDEX IF NOT EXISTS firm_resolution_run_created_at_idx
    ON firm_resolution_run (created_at DESC);

CREATE INDEX IF NOT EXISTS firm_resolution_run_source_mode_idx
    ON firm_resolution_run (source_table, source_field, mode, created_at DESC);

CREATE INDEX IF NOT EXISTS firm_resolution_run_status_idx
    ON firm_resolution_run (status, created_at DESC);

CREATE TABLE IF NOT EXISTS firm_match_pair (
    id                  bigserial PRIMARY KEY,
    run_id              bigint NOT NULL REFERENCES firm_resolution_run (id) ON DELETE CASCADE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    source_table        text NOT NULL CHECK (btrim(source_table) <> ''),
    source_field        text NOT NULL CHECK (btrim(source_field) <> ''),
    left_raw_firm       text NOT NULL CHECK (btrim(left_raw_firm) <> ''),
    right_raw_firm      text NOT NULL CHECK (btrim(right_raw_firm) <> ''),
    left_normalized     text NOT NULL CHECK (btrim(left_normalized) <> ''),
    right_normalized    text NOT NULL CHECK (btrim(right_normalized) <> ''),
    trigram_similarity  numeric(6,5) NOT NULL CHECK (trigram_similarity >= 0 AND trigram_similarity <= 1),
    word_similarity     numeric(6,5) NOT NULL CHECK (word_similarity >= 0 AND word_similarity <= 1),
    token_jaccard       numeric(6,5) NOT NULL CHECK (token_jaccard >= 0 AND token_jaccard <= 1),
    phonetic_match      boolean NOT NULL DEFAULT false,
    decision            text NOT NULL
                        CHECK (decision IN ('accepted', 'needs_review', 'rejected')),
    decision_reason     text NOT NULL CHECK (btrim(decision_reason) <> ''),
    confidence          numeric(6,5) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verified_by_llm     boolean NOT NULL DEFAULT false,
    evidence            jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS firm_match_pair_run_pair_key
    ON firm_match_pair (
        run_id,
        source_table,
        source_field,
        LEAST(left_raw_firm, right_raw_firm),
        GREATEST(left_raw_firm, right_raw_firm)
    );

CREATE INDEX IF NOT EXISTS firm_match_pair_decision_idx
    ON firm_match_pair (decision, confidence DESC);

CREATE INDEX IF NOT EXISTS firm_match_pair_left_trgm
    ON firm_match_pair USING gin (left_raw_firm gin_trgm_ops);

CREATE INDEX IF NOT EXISTS firm_match_pair_right_trgm
    ON firm_match_pair USING gin (right_raw_firm gin_trgm_ops);

COMMENT ON TABLE firm_resolution_run IS
    'Audit table for each firm-resolution run. Records mode, source field, thresholds, LLM settings, status, and summary stats so incremental company-name normalization is reproducible. [project table, not from openFDA]';
COMMENT ON COLUMN firm_resolution_run.id IS
    'Surrogate primary key for one resolver execution. [project field, not from openFDA]';
COMMENT ON COLUMN firm_resolution_run.created_at IS
    'Timestamp when the run audit row was created. [project field]';
COMMENT ON COLUMN firm_resolution_run.started_at IS
    'Timestamp when resolver work started. [project field]';
COMMENT ON COLUMN firm_resolution_run.completed_at IS
    'Timestamp when resolver work completed or failed. [project field]';
COMMENT ON COLUMN firm_resolution_run.status IS
    'Run status: running, succeeded, or failed. [project field]';
COMMENT ON COLUMN firm_resolution_run.mode IS
    'Resolver mode: full, incremental, or calibration. [project field]';
COMMENT ON COLUMN firm_resolution_run.apply_writes IS
    'True when this run wrote sidecar firm/alias/log rows; false for dry-run style audit uses. [project field]';
COMMENT ON COLUMN firm_resolution_run.source_table IS
    'Source table read by the resolver, e.g. drug_enforcement. [project field]';
COMMENT ON COLUMN firm_resolution_run.source_field IS
    'Source column read by the resolver, e.g. recalling_firm. [project field]';
COMMENT ON COLUMN firm_resolution_run.limit_rows IS
    'Optional CLI limit applied to selected source values for test runs. [project field]';
COMMENT ON COLUMN firm_resolution_run.candidate_threshold IS
    'pg_trgm candidate threshold used to generate candidate pairs. [project field]';
COMMENT ON COLUMN firm_resolution_run.auto_merge_threshold IS
    'Similarity threshold required for deterministic auto-merge decisions. [project field]';
COMMENT ON COLUMN firm_resolution_run.review_threshold IS
    'Minimum score for non-auto-merged candidate pairs to enter review instead of rejection. [project field]';
COMMENT ON COLUMN firm_resolution_run.token_threshold IS
    'Minimum token-overlap threshold used by deterministic auto-merge rules. [project field]';
COMMENT ON COLUMN firm_resolution_run.verify_llm IS
    'True when optional structured LLM verification was requested. [project field]';
COMMENT ON COLUMN firm_resolution_run.llm_model IS
    'LLM model used for optional pair verification, if any. [project field]';
COMMENT ON COLUMN firm_resolution_run.llm_confidence_threshold IS
    'Minimum optional LLM confidence required to accept a pair. [project field]';
COMMENT ON COLUMN firm_resolution_run.source_value_count IS
    'Total distinct non-empty source values considered in the comparison pool. [project field]';
COMMENT ON COLUMN firm_resolution_run.selected_value_count IS
    'Distinct source values selected for this run, e.g. new/changed/retry values in incremental mode. [project field]';
COMMENT ON COLUMN firm_resolution_run.skipped_value_count IS
    'Selected source values skipped because normalization produced no usable tokens. [project field]';
COMMENT ON COLUMN firm_resolution_run.candidate_pair_count IS
    'Number of generated candidate pairs. [project field]';
COMMENT ON COLUMN firm_resolution_run.accepted_pair_count IS
    'Number of candidate pairs accepted for auto-merge. [project field]';
COMMENT ON COLUMN firm_resolution_run.review_pair_count IS
    'Number of candidate pairs routed to review. [project field]';
COMMENT ON COLUMN firm_resolution_run.rejected_pair_count IS
    'Number of candidate pairs rejected below review threshold or by verifier. [project field]';
COMMENT ON COLUMN firm_resolution_run.firm_rows_touched IS
    'Number of firm rows inserted or updated by this run. [project field]';
COMMENT ON COLUMN firm_resolution_run.alias_rows_touched IS
    'Number of firm_alias rows inserted or updated by this run. [project field]';
COMMENT ON COLUMN firm_resolution_run.log_rows_written IS
    'Number of resolution_log rows written by this run. [project field]';
COMMENT ON COLUMN firm_resolution_run.stats IS
    'Additional structured run stats and diagnostics. [project field]';
COMMENT ON COLUMN firm_resolution_run.error_message IS
    'Failure message when status=failed. [project field]';

COMMENT ON TABLE firm_match_pair IS
    'Candidate-pair audit table for firm resolution. Stores matching signals and resolver decisions per run; accepted pairs may drive alias clustering while needs_review/rejected pairs remain auditable. [project table, not from openFDA]';
COMMENT ON COLUMN firm_match_pair.id IS
    'Surrogate primary key for one candidate-pair decision. [project field, not from openFDA]';
COMMENT ON COLUMN firm_match_pair.run_id IS
    'firm_resolution_run id that produced this candidate decision. [project field]';
COMMENT ON COLUMN firm_match_pair.created_at IS
    'Timestamp when the candidate-pair row was recorded. [project field]';
COMMENT ON COLUMN firm_match_pair.source_table IS
    'Source table for both raw firm strings. [project field]';
COMMENT ON COLUMN firm_match_pair.source_field IS
    'Source field for both raw firm strings. [project field]';
COMMENT ON COLUMN firm_match_pair.left_raw_firm IS
    'First raw firm string in canonical pair order. [FDA fact when source_table/field point to FDA data]';
COMMENT ON COLUMN firm_match_pair.right_raw_firm IS
    'Second raw firm string in canonical pair order. [FDA fact when source_table/field point to FDA data]';
COMMENT ON COLUMN firm_match_pair.left_normalized IS
    'Token-normalized left firm string used for matching. [project field]';
COMMENT ON COLUMN firm_match_pair.right_normalized IS
    'Token-normalized right firm string used for matching. [project field]';
COMMENT ON COLUMN firm_match_pair.trigram_similarity IS
    'pg_trgm similarity between normalized firm strings. [project field]';
COMMENT ON COLUMN firm_match_pair.word_similarity IS
    'pg_trgm word_similarity between normalized firm strings. [project field]';
COMMENT ON COLUMN firm_match_pair.token_jaccard IS
    'Jaccard overlap of normalized token sets. [project field]';
COMMENT ON COLUMN firm_match_pair.phonetic_match IS
    'True when primary tokens have matching metaphone encodings. [project field]';
COMMENT ON COLUMN firm_match_pair.decision IS
    'Resolver decision: accepted, needs_review, or rejected. [project field]';
COMMENT ON COLUMN firm_match_pair.decision_reason IS
    'Human-readable reason for the resolver decision. [project field]';
COMMENT ON COLUMN firm_match_pair.confidence IS
    'Confidence score in [0,1] assigned to this pair decision. [project field]';
COMMENT ON COLUMN firm_match_pair.verified_by_llm IS
    'True when optional structured LLM verification influenced this decision. [project field]';
COMMENT ON COLUMN firm_match_pair.evidence IS
    'Structured resolver evidence, including thresholds and optional verifier details. [project field]';
