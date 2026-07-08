-- Create firm-resolution sidecar tables for offline FDA recalling-firm resolution.
-- These tables do not rewrite drug_enforcement. They materialize conservative
-- entity/alias mappings, keep inferred relationships separate from FDA facts,
-- and log unknown or ambiguous inputs instead of inventing identities.
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;

CREATE TABLE IF NOT EXISTS parent_group (
    id              bigserial PRIMARY KEY,
    canonical_name  text NOT NULL CHECK (btrim(canonical_name) <> ''),
    normalized_name text NOT NULL CHECK (btrim(normalized_name) <> ''),
    source          text NOT NULL DEFAULT 'unknown'
                    CHECK (source IN ('fda', 'external', 'llm', 'manual', 'unknown')),
    confidence      numeric(5,4) NOT NULL DEFAULT 0
                    CHECK (confidence >= 0 AND confidence <= 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS parent_group_normalized_name_key
    ON parent_group (normalized_name);

CREATE INDEX IF NOT EXISTS parent_group_name_trgm
    ON parent_group USING gin (canonical_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS parent_group_normalized_name_trgm
    ON parent_group USING gin (normalized_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS parent_group_source_confidence_idx
    ON parent_group (source, confidence DESC);

CREATE TABLE IF NOT EXISTS firm (
    id              bigserial PRIMARY KEY,
    canonical_name  text NOT NULL CHECK (btrim(canonical_name) <> ''),
    normalized_name text NOT NULL CHECK (btrim(normalized_name) <> ''),
    parent_group_id bigint REFERENCES parent_group (id) ON DELETE SET NULL,
    fda_present     boolean NOT NULL DEFAULT true,
    source          text NOT NULL DEFAULT 'fda'
                    CHECK (source IN ('fda', 'external', 'llm', 'manual', 'unknown')),
    confidence      numeric(5,4) NOT NULL DEFAULT 1
                    CHECK (confidence >= 0 AND confidence <= 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS firm_normalized_name_key
    ON firm (normalized_name);

CREATE INDEX IF NOT EXISTS firm_parent_group_id_idx
    ON firm (parent_group_id);

CREATE INDEX IF NOT EXISTS firm_name_trgm
    ON firm USING gin (canonical_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS firm_normalized_name_trgm
    ON firm USING gin (normalized_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS firm_fda_present_source_idx
    ON firm (fda_present, source);

CREATE TABLE IF NOT EXISTS firm_alias (
    id                  bigserial PRIMARY KEY,
    raw_firm            text NOT NULL CHECK (btrim(raw_firm) <> ''),
    normalized_raw_firm text NOT NULL CHECK (btrim(normalized_raw_firm) <> ''),
    firm_id             bigint NOT NULL REFERENCES firm (id) ON DELETE CASCADE,
    alias_kind          text NOT NULL DEFAULT 'recalling_firm'
                        CHECK (alias_kind IN (
                            'recalling_firm',
                            'legal_name',
                            'trade_name',
                            'former_name',
                            'external_alias'
                        )),
    source_table        text NOT NULL DEFAULT 'drug_enforcement',
    source_field        text NOT NULL DEFAULT 'recalling_firm',
    record_count        integer NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    source              text NOT NULL DEFAULT 'fda'
                        CHECK (source IN ('fda', 'external', 'llm', 'manual', 'unknown')),
    confidence          numeric(5,4) NOT NULL DEFAULT 1
                        CHECK (confidence >= 0 AND confidence <= 1),
    evidence            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS firm_alias_source_raw_key
    ON firm_alias (source_table, source_field, raw_firm);

CREATE INDEX IF NOT EXISTS firm_alias_firm_id_idx
    ON firm_alias (firm_id);

CREATE INDEX IF NOT EXISTS firm_alias_raw_firm_trgm
    ON firm_alias USING gin (raw_firm gin_trgm_ops);

CREATE INDEX IF NOT EXISTS firm_alias_normalized_raw_firm_trgm
    ON firm_alias USING gin (normalized_raw_firm gin_trgm_ops);

CREATE INDEX IF NOT EXISTS firm_alias_source_confidence_idx
    ON firm_alias (source, confidence DESC);

CREATE TABLE IF NOT EXISTS brand_alias (
    id                    bigserial PRIMARY KEY,
    brand_name            text NOT NULL CHECK (btrim(brand_name) <> ''),
    normalized_brand_name text NOT NULL CHECK (btrim(normalized_brand_name) <> ''),
    firm_id               bigint REFERENCES firm (id) ON DELETE SET NULL,
    parent_group_id       bigint REFERENCES parent_group (id) ON DELETE SET NULL,
    provenance_tier       text NOT NULL
                          CHECK (provenance_tier IN (
                              'fda_fact',
                              'inferred_external_or_llm',
                              'unknown'
                          )),
    source                text NOT NULL DEFAULT 'unknown'
                          CHECK (source IN ('fda', 'external', 'llm', 'manual', 'unknown')),
    confidence            numeric(5,4) NOT NULL DEFAULT 0
                          CHECK (confidence >= 0 AND confidence <= 1),
    evidence              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (provenance_tier = 'unknown' AND firm_id IS NULL AND parent_group_id IS NULL)
        OR
        (provenance_tier <> 'unknown' AND (firm_id IS NOT NULL OR parent_group_id IS NOT NULL))
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS brand_alias_identity_key
    ON brand_alias (
        normalized_brand_name,
        provenance_tier,
        COALESCE(firm_id, 0::bigint),
        COALESCE(parent_group_id, 0::bigint),
        source
    );

CREATE INDEX IF NOT EXISTS brand_alias_firm_id_idx
    ON brand_alias (firm_id);

CREATE INDEX IF NOT EXISTS brand_alias_parent_group_id_idx
    ON brand_alias (parent_group_id);

CREATE INDEX IF NOT EXISTS brand_alias_brand_name_trgm
    ON brand_alias USING gin (brand_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS brand_alias_normalized_brand_name_trgm
    ON brand_alias USING gin (normalized_brand_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS brand_alias_provenance_confidence_idx
    ON brand_alias (provenance_tier, confidence DESC);

CREATE TABLE IF NOT EXISTS resolution_log (
    id                         bigserial PRIMARY KEY,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    resolved_at                timestamptz,
    entity_type                text NOT NULL
                               CHECK (entity_type IN ('firm', 'brand', 'parent_group')),
    input_value                text NOT NULL CHECK (btrim(input_value) <> ''),
    normalized_input           text,
    status                     text NOT NULL
                               CHECK (status IN (
                                   'unknown',
                                   'ambiguous',
                                   'needs_review',
                                   'skipped',
                                   'error'
                               )),
    reason                     text NOT NULL CHECK (btrim(reason) <> ''),
    candidate_firm_ids         bigint[] NOT NULL DEFAULT ARRAY[]::bigint[],
    candidate_parent_group_ids bigint[] NOT NULL DEFAULT ARRAY[]::bigint[],
    provenance_tier            text
                               CHECK (
                                   provenance_tier IS NULL
                                   OR provenance_tier IN (
                                       'fda_fact',
                                       'inferred_external_or_llm',
                                       'unknown'
                                   )
                               ),
    source                     text NOT NULL DEFAULT 'resolver',
    evidence                   jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS resolution_log_status_created_at_idx
    ON resolution_log (status, created_at DESC);

CREATE INDEX IF NOT EXISTS resolution_log_entity_status_idx
    ON resolution_log (entity_type, status);

CREATE INDEX IF NOT EXISTS resolution_log_input_value_trgm
    ON resolution_log USING gin (input_value gin_trgm_ops);

COMMENT ON TABLE parent_group IS
    'Optional parent-company/group sidecar for firm resolution. Rows may be FDA-derived, external, LLM-inferred, manual, or unknown; non-FDA sources are not FDA facts. [project table, inferred where source is not fda]';
COMMENT ON COLUMN parent_group.id IS
    'Surrogate primary key for a parent group candidate. [project field, not from openFDA]';
COMMENT ON COLUMN parent_group.canonical_name IS
    'Display name for the parent group. If source is not fda, this is an inferred/external label, not an FDA fact. [project field]';
COMMENT ON COLUMN parent_group.normalized_name IS
    'Token-normalized parent group name used for deterministic and fuzzy lookup. [project field]';
COMMENT ON COLUMN parent_group.source IS
    'Provenance source for this parent group: fda, external, llm, manual, or unknown. [project field]';
COMMENT ON COLUMN parent_group.confidence IS
    'Confidence score in [0,1] for the parent group identity/provenance. [project field]';
COMMENT ON COLUMN parent_group.evidence IS
    'Structured evidence, citations, or resolver metadata supporting the row. [project field]';
COMMENT ON COLUMN parent_group.created_at IS
    'Timestamp when the sidecar row was created. [project field]';
COMMENT ON COLUMN parent_group.updated_at IS
    'Timestamp when the sidecar row was last updated. [project field]';

COMMENT ON TABLE firm IS
    'Canonical firm sidecar. FDA-present firms come from recalling_firm strings; external/LLM rows must remain marked as inferred and separate from FDA facts. [project table]';
COMMENT ON COLUMN firm.id IS
    'Surrogate primary key for a canonical firm candidate. [project field, not from openFDA]';
COMMENT ON COLUMN firm.canonical_name IS
    'Display name chosen for the resolved firm cluster, usually the most common FDA recalling_firm spelling. [project field]';
COMMENT ON COLUMN firm.normalized_name IS
    'Token-normalized firm name used for lookup and de-duplication. [project field]';
COMMENT ON COLUMN firm.parent_group_id IS
    'Optional link to parent_group; absent when no conservative parent mapping exists. [project field, inferred unless source is fda]';
COMMENT ON COLUMN firm.fda_present IS
    'True when the firm is represented by at least one FDA recalling_firm alias; false only for explicitly separated external/inferred entities. [project field]';
COMMENT ON COLUMN firm.source IS
    'Provenance source for the firm identity: fda, external, llm, manual, or unknown. [project field]';
COMMENT ON COLUMN firm.confidence IS
    'Confidence score in [0,1] for the firm identity/cluster. [project field]';
COMMENT ON COLUMN firm.evidence IS
    'Structured evidence such as member aliases, record counts, thresholds, or verifier output. [project field]';
COMMENT ON COLUMN firm.created_at IS
    'Timestamp when the sidecar row was created. [project field]';
COMMENT ON COLUMN firm.updated_at IS
    'Timestamp when the sidecar row was last updated. [project field]';

COMMENT ON TABLE firm_alias IS
    'Alias mapping from raw source firm strings, especially drug_enforcement.recalling_firm, to canonical firm rows. [project table]';
COMMENT ON COLUMN firm_alias.id IS
    'Surrogate primary key for a firm alias mapping. [project field, not from openFDA]';
COMMENT ON COLUMN firm_alias.raw_firm IS
    'Raw firm string as it appeared in the source field, e.g. drug_enforcement.recalling_firm. [FDA fact when source=fda]';
COMMENT ON COLUMN firm_alias.normalized_raw_firm IS
    'Token-normalized raw firm string used for matching. [project field]';
COMMENT ON COLUMN firm_alias.firm_id IS
    'Canonical firm that this raw alias resolves to. [project field]';
COMMENT ON COLUMN firm_alias.alias_kind IS
    'Type of alias represented by raw_firm, such as recalling_firm or trade_name. [project field]';
COMMENT ON COLUMN firm_alias.source_table IS
    'Source table for the alias string, e.g. drug_enforcement. [project field]';
COMMENT ON COLUMN firm_alias.source_field IS
    'Source column for the alias string, e.g. recalling_firm. [project field]';
COMMENT ON COLUMN firm_alias.record_count IS
    'Number of source records observed for this exact alias at write time. [project field]';
COMMENT ON COLUMN firm_alias.source IS
    'Provenance source for the alias mapping: fda, external, llm, manual, or unknown. [project field]';
COMMENT ON COLUMN firm_alias.confidence IS
    'Confidence score in [0,1] for assigning this alias to firm_id. [project field]';
COMMENT ON COLUMN firm_alias.evidence IS
    'Structured evidence supporting the alias mapping. [project field]';
COMMENT ON COLUMN firm_alias.created_at IS
    'Timestamp when the sidecar row was created. [project field]';
COMMENT ON COLUMN firm_alias.updated_at IS
    'Timestamp when the sidecar row was last updated. [project field]';

COMMENT ON TABLE brand_alias IS
    'Brand or product-name sidecar mapping to firm/parent candidates with explicit provenance tiers. Unknowns should be logged in resolution_log rather than asserted here. [project table]';
COMMENT ON COLUMN brand_alias.id IS
    'Surrogate primary key for a brand alias mapping. [project field, not from openFDA]';
COMMENT ON COLUMN brand_alias.brand_name IS
    'User-facing brand or product name being resolved. [project field]';
COMMENT ON COLUMN brand_alias.normalized_brand_name IS
    'Token-normalized brand/product name used for lookup. [project field]';
COMMENT ON COLUMN brand_alias.firm_id IS
    'Candidate canonical firm for this brand when known. [project field]';
COMMENT ON COLUMN brand_alias.parent_group_id IS
    'Candidate parent group for this brand when known. [project field, inferred unless provenance_tier=fda_fact]';
COMMENT ON COLUMN brand_alias.provenance_tier IS
    'Tier for the mapping: fda_fact, inferred_external_or_llm, or unknown. [project field]';
COMMENT ON COLUMN brand_alias.source IS
    'Specific source class for the mapping: fda, external, llm, manual, or unknown. [project field]';
COMMENT ON COLUMN brand_alias.confidence IS
    'Confidence score in [0,1] for this brand mapping. [project field]';
COMMENT ON COLUMN brand_alias.evidence IS
    'Structured evidence such as recall numbers, citations, or verifier output. [project field]';
COMMENT ON COLUMN brand_alias.created_at IS
    'Timestamp when the sidecar row was created. [project field]';
COMMENT ON COLUMN brand_alias.updated_at IS
    'Timestamp when the sidecar row was last updated. [project field]';

COMMENT ON TABLE resolution_log IS
    'Audit log for unresolved, ambiguous, skipped, or error cases in firm/brand/parent resolution. Unknowns are recorded here rather than fabricated as facts. [project table]';
COMMENT ON COLUMN resolution_log.id IS
    'Surrogate primary key for a resolution event. [project field, not from openFDA]';
COMMENT ON COLUMN resolution_log.created_at IS
    'Timestamp when the resolver logged this event. [project field]';
COMMENT ON COLUMN resolution_log.resolved_at IS
    'Optional timestamp when a later process resolved this event. [project field]';
COMMENT ON COLUMN resolution_log.entity_type IS
    'Entity type being resolved: firm, brand, or parent_group. [project field]';
COMMENT ON COLUMN resolution_log.input_value IS
    'Original input value that could not be resolved conservatively or needs review. [project field]';
COMMENT ON COLUMN resolution_log.normalized_input IS
    'Token-normalized input value used by the resolver. [project field]';
COMMENT ON COLUMN resolution_log.status IS
    'Outcome status: unknown, ambiguous, needs_review, skipped, or error. [project field]';
COMMENT ON COLUMN resolution_log.reason IS
    'Human-readable reason the input was logged instead of asserted as a resolved identity. [project field]';
COMMENT ON COLUMN resolution_log.candidate_firm_ids IS
    'Candidate firm ids considered for this event, if any. [project field]';
COMMENT ON COLUMN resolution_log.candidate_parent_group_ids IS
    'Candidate parent group ids considered for this event, if any. [project field]';
COMMENT ON COLUMN resolution_log.provenance_tier IS
    'Provenance tier involved in the attempted resolution, when applicable. [project field]';
COMMENT ON COLUMN resolution_log.source IS
    'Resolver/source that produced this log event. [project field]';
COMMENT ON COLUMN resolution_log.evidence IS
    'Structured evidence or diagnostics for the log event. [project field]';
