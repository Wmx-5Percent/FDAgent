-- Create provenance-backed parent-group rollup prerequisites for firm exposure.
-- This migration keeps FDA firm facts separate from inferred parent edges: only
-- active, confirmed, non-unknown, non-LLM-only edges are eligible for parent
-- exposure rollups. Unknown/unconfirmed edges remain auditable but do not affect
-- exact parent-group counts. Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS firm_parent_group_edge (
    id                bigserial PRIMARY KEY,
    firm_id           bigint NOT NULL REFERENCES firm (id) ON DELETE CASCADE,
    parent_group_id   bigint NOT NULL REFERENCES parent_group (id) ON DELETE CASCADE,
    provenance_tier   text NOT NULL
                      CHECK (provenance_tier IN (
                          'fda_fact',
                          'inferred_external_or_llm',
                          'unknown'
                      )),
    source            text NOT NULL DEFAULT 'unknown'
                      CHECK (source IN ('fda', 'external', 'llm', 'manual', 'unknown')),
    source_name       text,
    source_url        text,
    source_id         text,
    as_of_date        date NOT NULL DEFAULT CURRENT_DATE,
    review_status     text NOT NULL DEFAULT 'needs_review'
                      CHECK (review_status IN (
                          'confirmed',
                          'needs_review',
                          'rejected',
                          'superseded'
                      )),
    active            boolean NOT NULL DEFAULT true,
    confidence        numeric(5,4) NOT NULL DEFAULT 0
                      CHECK (confidence >= 0 AND confidence <= 1),
    evidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (
        provenance_tier <> 'unknown'
        OR (source = 'unknown' AND confidence = 0)
    ),
    CHECK (
        review_status <> 'confirmed'
        OR provenance_tier = 'unknown'
        OR source IN ('fda', 'external', 'manual')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS firm_parent_group_edge_one_active_confirmed
    ON firm_parent_group_edge (firm_id)
    WHERE active AND review_status = 'confirmed';

CREATE UNIQUE INDEX IF NOT EXISTS firm_parent_group_edge_dedupe_key
    ON firm_parent_group_edge (
        firm_id,
        parent_group_id,
        provenance_tier,
        source,
        COALESCE(source_id, ''),
        as_of_date
    );

CREATE INDEX IF NOT EXISTS firm_parent_group_edge_parent_idx
    ON firm_parent_group_edge (parent_group_id, active, review_status);

CREATE INDEX IF NOT EXISTS firm_parent_group_edge_provenance_idx
    ON firm_parent_group_edge (provenance_tier, source, review_status, active);

CREATE OR REPLACE VIEW firm_parent_group_active AS
SELECT
    edge.id AS edge_id,
    edge.firm_id,
    f.canonical_name AS firm_name,
    edge.parent_group_id,
    pg.canonical_name AS parent_group_name,
    edge.provenance_tier,
    edge.source,
    edge.source_name,
    edge.source_url,
    edge.source_id,
    edge.as_of_date,
    edge.confidence,
    edge.evidence,
    (
        edge.active
        AND edge.review_status = 'confirmed'
        AND edge.provenance_tier <> 'unknown'
        AND edge.source <> 'llm'
    ) AS is_provenance_backed
FROM firm_parent_group_edge edge
JOIN firm f ON f.id = edge.firm_id
JOIN parent_group pg ON pg.id = edge.parent_group_id
WHERE edge.active AND edge.review_status = 'confirmed';

CREATE OR REPLACE VIEW parent_group_member_exposure_v1 AS
SELECT
    active.parent_group_id,
    active.parent_group_name,
    active.firm_id,
    active.firm_name,
    count(*)::int AS total_recalls,
    count(*) FILTER (WHERE de.classification = 'Class I')::int AS class_i_recalls,
    count(*) FILTER (WHERE de.classification = 'Class II')::int AS class_ii_recalls,
    count(*) FILTER (WHERE de.classification = 'Class III')::int AS class_iii_recalls,
    count(*) FILTER (
        WHERE de.classification IS NULL
           OR de.classification NOT IN ('Class I', 'Class II', 'Class III')
    )::int AS unclassified_recalls,
    (
        3 * count(*) FILTER (WHERE de.classification = 'Class I')
        + 2 * count(*) FILTER (WHERE de.classification = 'Class II')
        + count(*) FILTER (WHERE de.classification = 'Class III')
    )::int AS severity_weighted_exposure,
    active.provenance_tier,
    active.source,
    active.source_name,
    active.source_url,
    active.source_id,
    active.as_of_date,
    active.confidence,
    active.evidence AS edge_evidence,
    (
        array_agg(
            de.recall_number
            ORDER BY
                CASE de.classification
                    WHEN 'Class I' THEN 3
                    WHEN 'Class II' THEN 2
                    WHEN 'Class III' THEN 1
                    ELSE 0
                END DESC,
                de.recall_initiation_date DESC NULLS LAST,
                de.recall_number
        )
    )[1:10] AS evidence
FROM drug_enforcement de
JOIN firm_alias fa
  ON fa.source_table = 'drug_enforcement'
 AND fa.source_field = 'recalling_firm'
 AND fa.raw_firm = de.recalling_firm
JOIN firm_parent_group_active active
  ON active.firm_id = fa.firm_id
 AND active.is_provenance_backed
GROUP BY
    active.parent_group_id,
    active.parent_group_name,
    active.firm_id,
    active.firm_name,
    active.provenance_tier,
    active.source,
    active.source_name,
    active.source_url,
    active.source_id,
    active.as_of_date,
    active.confidence,
    active.evidence;

CREATE OR REPLACE VIEW parent_group_exposure_v1 AS
SELECT
    parent_group_id,
    parent_group_name,
    sum(total_recalls)::int AS total_recalls,
    sum(class_i_recalls)::int AS class_i_recalls,
    sum(class_ii_recalls)::int AS class_ii_recalls,
    sum(class_iii_recalls)::int AS class_iii_recalls,
    sum(unclassified_recalls)::int AS unclassified_recalls,
    sum(severity_weighted_exposure)::int AS severity_weighted_exposure,
    count(DISTINCT firm_id)::int AS member_firm_count,
    jsonb_agg(
        jsonb_build_object(
            'firm_id', firm_id,
            'firm_name', firm_name,
            'total_recalls', total_recalls,
            'severity_weighted_exposure', severity_weighted_exposure,
            'class_i_recalls', class_i_recalls,
            'class_ii_recalls', class_ii_recalls,
            'class_iii_recalls', class_iii_recalls,
            'unclassified_recalls', unclassified_recalls,
            'edge_provenance_tier', provenance_tier,
            'edge_source', source,
            'edge_source_name', source_name,
            'edge_source_url', source_url,
            'edge_source_id', source_id,
            'edge_as_of_date', as_of_date,
            'edge_confidence', confidence,
            'edge_evidence', edge_evidence,
            'evidence', evidence
        )
        ORDER BY severity_weighted_exposure DESC, total_recalls DESC, firm_name ASC
    ) AS member_breakdown
FROM parent_group_member_exposure_v1
GROUP BY parent_group_id, parent_group_name;

COMMENT ON TABLE firm_parent_group_edge IS
    'Auditable firm→parent_group edge table. Only active confirmed non-unknown, non-LLM-only edges are eligible for parent-group exposure rollups; unknown/unconfirmed edges remain visible but do not affect exact parent counts. [project table, inferred unless provenance_tier=fda_fact]';
COMMENT ON COLUMN firm_parent_group_edge.id IS
    'Surrogate primary key for one firm→parent_group edge assertion. [project field, not from openFDA]';
COMMENT ON COLUMN firm_parent_group_edge.firm_id IS
    'Canonical FDA-present firm being attached to a parent group. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.parent_group_id IS
    'Parent group candidate for the firm. [project field, inferred unless provenance_tier=fda_fact]';
COMMENT ON COLUMN firm_parent_group_edge.provenance_tier IS
    'Provenance tier for the edge: fda_fact, inferred_external_or_llm, or unknown. Parent edges are normally inferred_external_or_llm or unknown because FDA does not publish ownership. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.source IS
    'Source class for the edge: fda, external, llm, manual, or unknown. Confirmed rollup edges may not be LLM-only. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.source_name IS
    'Human-readable source name, e.g. Wikidata, SEC Exhibit 21, or human-confirmed seed. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.source_url IS
    'Citation URL for the edge when available. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.source_id IS
    'Structured source identifier such as a Wikidata QID or SEC accession when available. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.as_of_date IS
    'Date for which the parent relationship is asserted, because ownership changes over time. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.review_status IS
    'Review state for the edge: confirmed, needs_review, rejected, or superseded. Only confirmed active edges can drive rollups. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.active IS
    'True when this is the current edge record for the firm; old edges should be superseded instead of deleted. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.confidence IS
    'Confidence score in [0,1] for the edge assertion. Unknown self-parent placeholders must keep 0. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.evidence IS
    'Structured citation, reviewer, and resolver metadata supporting or explaining the edge. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.created_at IS
    'Timestamp when the edge row was created. [project field]';
COMMENT ON COLUMN firm_parent_group_edge.updated_at IS
    'Timestamp when the edge row was last updated. [project field]';

COMMENT ON VIEW firm_parent_group_active IS
    'Current confirmed firm→parent edges, with is_provenance_backed marking edges eligible for parent-group exposure rollups. [project view]';
COMMENT ON VIEW parent_group_member_exposure_v1 IS
    'Member-firm recall exposure under provenance-backed parent edges, preserving the FDA recalling_firm evidence behind each parent rollup. [project view]';
COMMENT ON VIEW parent_group_exposure_v1 IS
    'Parent-group recall exposure aggregate over provenance-backed firm→parent edges only. Unknown/unconfirmed/LLM-only edges are intentionally absent. [project view]';
