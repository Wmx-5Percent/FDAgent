-- Create recall taxonomy sidecar tables for offline classification.
-- Project-owned derived tables: taxonomy definitions, per-record labels, and
-- discovery candidates. Idempotent: safe to re-run after drug_enforcement exists.

CREATE TABLE IF NOT EXISTS taxonomy (
    version     text NOT NULL,
    node_id     text NOT NULL,
    parent_id   text,
    label       text NOT NULL,
    definition  text NOT NULL,
    examples    text[] NOT NULL DEFAULT '{}',
    level       integer NOT NULL CHECK (level >= 0),
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'active', 'deprecated')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version, node_id),
    UNIQUE (version, label),
    FOREIGN KEY (version, parent_id)
        REFERENCES taxonomy (version, node_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS taxonomy_version_status_idx
    ON taxonomy (version, status);

CREATE INDEX IF NOT EXISTS taxonomy_parent_idx
    ON taxonomy (version, parent_id);

CREATE INDEX IF NOT EXISTS taxonomy_label_idx
    ON taxonomy (lower(label));

CREATE TABLE IF NOT EXISTS recall_label (
    record_id        text NOT NULL,
    version          text NOT NULL,
    node_id          text NOT NULL,
    level            integer NOT NULL CHECK (level >= 0),
    confidence       numeric(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence         text NOT NULL,
    source_text_hash text NOT NULL,
    labeler          text NOT NULL,
    model            text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, version, node_id, labeler),
    FOREIGN KEY (record_id)
        REFERENCES drug_enforcement (id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (version, node_id)
        REFERENCES taxonomy (version, node_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS recall_label_node_idx
    ON recall_label (version, node_id, confidence DESC);

CREATE INDEX IF NOT EXISTS recall_label_record_idx
    ON recall_label (record_id, version);

CREATE INDEX IF NOT EXISTS recall_label_low_confidence_idx
    ON recall_label (version, confidence)
    WHERE confidence < 0.7000;

CREATE INDEX IF NOT EXISTS recall_label_source_text_hash_idx
    ON recall_label (version, source_text_hash);

CREATE TABLE IF NOT EXISTS taxonomy_candidate (
    taxonomy_version text NOT NULL,
    cluster_key      text NOT NULL,
    candidate_node_id text NOT NULL,
    parent_id        text,
    proposed_label   text NOT NULL,
    definition       text NOT NULL,
    examples         text[] NOT NULL DEFAULT '{}',
    size             integer NOT NULL CHECK (size >= 0),
    growth_count     integer NOT NULL DEFAULT 0 CHECK (growth_count >= 0),
    coherence        numeric(5,4) CHECK (coherence IS NULL OR (coherence >= 0 AND coherence <= 1)),
    confidence       numeric(5,4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    evidence         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'accepted', 'rejected', 'deferred')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (taxonomy_version, cluster_key),
    FOREIGN KEY (taxonomy_version, parent_id)
        REFERENCES taxonomy (version, node_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS taxonomy_candidate_review_idx
    ON taxonomy_candidate (taxonomy_version, status, size DESC, coherence DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS taxonomy_candidate_parent_idx
    ON taxonomy_candidate (taxonomy_version, parent_id);

COMMENT ON TABLE taxonomy IS
    'Versioned recall-reason taxonomy induced from openFDA drug_enforcement reason_for_recall text. Project sidecar table; not an openFDA source table.';
COMMENT ON COLUMN taxonomy.version IS
    'Taxonomy version identifier, for example v1. Allows frozen label sets to coexist with future revisions.';
COMMENT ON COLUMN taxonomy.node_id IS
    'Stable machine identifier for a taxonomy node within a version.';
COMMENT ON COLUMN taxonomy.parent_id IS
    'Optional parent node_id within the same taxonomy version. NULL marks a top-level node.';
COMMENT ON COLUMN taxonomy.label IS
    'Human-readable taxonomy label.';
COMMENT ON COLUMN taxonomy.definition IS
    'Operational definition used by closed-set labelers.';
COMMENT ON COLUMN taxonomy.examples IS
    'Representative reason_for_recall snippets for the node.';
COMMENT ON COLUMN taxonomy.level IS
    'Depth in the taxonomy tree, where 0 is top-level.';
COMMENT ON COLUMN taxonomy.status IS
    'Lifecycle state: draft, active, or deprecated.';
COMMENT ON COLUMN taxonomy.created_at IS
    'Timestamp when this taxonomy node row was created.';
COMMENT ON COLUMN taxonomy.updated_at IS
    'Timestamp when this taxonomy node row was last upserted by the offline pipeline.';

COMMENT ON TABLE recall_label IS
    'Closed-set taxonomy labels assigned to drug_enforcement records by the offline classification pipeline. Project sidecar table; not an openFDA source table.';
COMMENT ON COLUMN recall_label.record_id IS
    'Source record id from drug_enforcement.id. For drug/enforcement this is the recall_number ingested by fetch_openfda.py.';
COMMENT ON COLUMN recall_label.version IS
    'Taxonomy version used for this label.';
COMMENT ON COLUMN recall_label.node_id IS
    'Assigned taxonomy node_id within the taxonomy version.';
COMMENT ON COLUMN recall_label.level IS
    'Level of the assigned taxonomy node, copied from taxonomy for faster filtering.';
COMMENT ON COLUMN recall_label.confidence IS
    'Classifier confidence for this assignment, from 0 to 1.';
COMMENT ON COLUMN recall_label.evidence IS
    'Supporting snippet from reason_for_recall used to justify the label.';
COMMENT ON COLUMN recall_label.source_text_hash IS
    'SHA-256 hash of the normalized source reason text used for cache/backfill grouping.';
COMMENT ON COLUMN recall_label.labeler IS
    'Labeler identifier, such as llm:gpt-4o-mini or a future distilled classifier.';
COMMENT ON COLUMN recall_label.model IS
    'Exact model or classifier name used to produce the label.';
COMMENT ON COLUMN recall_label.created_at IS
    'Timestamp when this label row was first inserted.';
COMMENT ON COLUMN recall_label.updated_at IS
    'Timestamp when this label row was last upserted by the offline pipeline.';

COMMENT ON TABLE taxonomy_candidate IS
    'Open-set discovery candidates from other or low-confidence residual recall reasons, awaiting human review before promotion to a taxonomy version.';
COMMENT ON COLUMN taxonomy_candidate.taxonomy_version IS
    'Existing taxonomy version the candidate was discovered against.';
COMMENT ON COLUMN taxonomy_candidate.cluster_key IS
    'Stable key for the residual cluster, derived from its member text hashes.';
COMMENT ON COLUMN taxonomy_candidate.candidate_node_id IS
    'Proposed machine identifier if the candidate is accepted into a future taxonomy version.';
COMMENT ON COLUMN taxonomy_candidate.parent_id IS
    'Optional proposed parent node_id in the existing taxonomy version.';
COMMENT ON COLUMN taxonomy_candidate.proposed_label IS
    'Human-readable proposed category label.';
COMMENT ON COLUMN taxonomy_candidate.definition IS
    'Draft operational definition for the candidate category.';
COMMENT ON COLUMN taxonomy_candidate.examples IS
    'Representative residual reason_for_recall snippets.';
COMMENT ON COLUMN taxonomy_candidate.size IS
    'Number of source records represented by the residual cluster.';
COMMENT ON COLUMN taxonomy_candidate.growth_count IS
    'Number of represented records with recent report_date values, used as a growth signal.';
COMMENT ON COLUMN taxonomy_candidate.coherence IS
    'Cluster coherence score from 0 to 1; higher means tighter residual grouping.';
COMMENT ON COLUMN taxonomy_candidate.confidence IS
    'LLM confidence that the candidate is a coherent new category, from 0 to 1.';
COMMENT ON COLUMN taxonomy_candidate.evidence IS
    'Structured evidence JSON: member hashes, prefix summaries, counts, and representative texts.';
COMMENT ON COLUMN taxonomy_candidate.status IS
    'Review state: proposed, accepted, rejected, or deferred.';
COMMENT ON COLUMN taxonomy_candidate.created_at IS
    'Timestamp when this candidate row was first inserted.';
COMMENT ON COLUMN taxonomy_candidate.updated_at IS
    'Timestamp when this candidate row was last upserted by the offline pipeline.';
