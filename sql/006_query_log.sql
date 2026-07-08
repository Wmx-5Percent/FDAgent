-- Create query_log: L1 Postgres observability for every handled /ask request.
-- This is a project-owned trace table, not an openFDA source table. It stores the
-- materialized QuerySpec/decision plus compact response metadata so /ask behavior is
-- auditable before adding Langfuse. Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS query_log (
    id                bigserial PRIMARY KEY,
    created_at        timestamptz NOT NULL DEFAULT now(),
    route             text NOT NULL,
    question          text NOT NULL,
    request           jsonb NOT NULL,
    status_code       integer NOT NULL,
    ok                boolean NOT NULL,
    latency_ms        integer NOT NULL CHECK (latency_ms >= 0),
    query_intent      text,
    data_kind         text,
    semantic_query    text,
    query_spec        jsonb,
    decision          jsonb,
    response_metadata jsonb,
    error_type        text,
    error_message     text,
    error_detail      jsonb
);

CREATE INDEX IF NOT EXISTS query_log_created_at_idx
    ON query_log (created_at DESC);

CREATE INDEX IF NOT EXISTS query_log_ok_created_at_idx
    ON query_log (ok, created_at DESC);

CREATE INDEX IF NOT EXISTS query_log_query_intent_idx
    ON query_log (query_intent);

CREATE INDEX IF NOT EXISTS query_log_data_kind_idx
    ON query_log (data_kind);

CREATE INDEX IF NOT EXISTS query_log_query_spec_gin
    ON query_log USING gin (query_spec jsonb_path_ops);

COMMENT ON TABLE query_log IS
    'L1 Postgres observability trace table for handled /ask requests. Stores request payloads, materialized QuerySpec/decision JSON, compact response metadata, latency, route/type, and error fields before the later Langfuse integration. [project table, not from openFDA]';

COMMENT ON COLUMN query_log.id IS
    'Surrogate primary key for one handled /ask request. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.created_at IS
    'Server timestamp when the log row was inserted. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.route IS
    'API route that produced this trace, currently /ask. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.question IS
    'Natural-language user question from the request. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.request IS
    'JSON request payload accepted by the /ask endpoint. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.status_code IS
    'HTTP status code returned or intended by the endpoint after handling the request. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.ok IS
    'True when /ask completed successfully and returned an answer; false for endpoint-level handled errors. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.latency_ms IS
    'Elapsed endpoint handling time in milliseconds, measured around NL routing, execution, serialization, and log-write preparation. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.query_intent IS
    'QuerySpec intent selected by the NL layer, e.g. count_total, count_by, trend, or sample. NULL when no QuerySpec was produced. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.data_kind IS
    'Serialized response data kind, e.g. scalar, distribution, series, rows, or retrieval. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.semantic_query IS
    'Fuzzy concept routed to semantic retrieval when QuerySpec.semantic_query is present. NULL for purely SQL-backed questions. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.query_spec IS
    'Full validated QuerySpec as JSON, excluding null fields. This is the materialized inspectable decision used to execute /ask. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.decision IS
    'Compact structured routing decision derived from QuerySpec, such as sql vs semantic route, intent, data kind, and filter count. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.response_metadata IS
    'Compact response metadata such as data kind, result count, summary length, and serving model; does not duplicate the full response body. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.error_type IS
    'Exception class name for endpoint-level handled errors. NULL for successful requests. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.error_message IS
    'Short error message for endpoint-level handled errors. NULL for successful requests. [project field, not from openFDA]';
COMMENT ON COLUMN query_log.error_detail IS
    'Structured error metadata safe for local debugging, such as the HTTP status used by the endpoint. [project field, not from openFDA]';
