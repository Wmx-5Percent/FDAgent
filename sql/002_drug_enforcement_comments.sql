-- Column documentation for drug_enforcement.
-- Definitions sourced from the official openFDA drug/enforcement field reference:
--   https://open.fda.gov/fields/drugenforcement.yaml
-- Stored as Postgres COMMENTs so the schema is self-describing for get_object_details,
-- psql \d+, and the (future) Text-to-SQL layer (readable via pg_description / col_description).
-- Idempotent: COMMENT ON overwrites the previous comment; safe to re-run.

COMMENT ON TABLE drug_enforcement IS
    'openFDA drug recall enforcement reports (endpoint drug/enforcement). One row per recall_number; the full original API record is kept in raw (JSONB) and key fields are parsed into typed columns.';

-- Ingester-owned columns (not openFDA fields) -------------------------------
COMMENT ON COLUMN drug_enforcement.id         IS 'Primary key = recall_number, extracted by the ingester to support idempotent upserts.';
COMMENT ON COLUMN drug_enforcement.source     IS 'openFDA endpoint this row was ingested from (e.g. drug/enforcement).';
COMMENT ON COLUMN drug_enforcement.report_date IS 'Date the FDA issued the enforcement report for the recall (parsed from raw.report_date).';
COMMENT ON COLUMN drug_enforcement.raw        IS 'Full original openFDA record exactly as returned by the API (JSONB); source of truth for all parsed columns.';
COMMENT ON COLUMN drug_enforcement.fetched_at IS 'Timestamp when the ingester last inserted/updated this row.';

-- openFDA fields, parsed into generated columns -----------------------------
COMMENT ON COLUMN drug_enforcement.recall_number IS 'Unique designation assigned by FDA to this specific recall, used for tracking (e.g. D-321-2016).';
COMMENT ON COLUMN drug_enforcement.event_id IS 'Numerical designation assigned by FDA to a recall event used for tracking; several recall_numbers may share one event_id.';
COMMENT ON COLUMN drug_enforcement.status IS 'Recall status: On-Going (currently in progress), Completed (all recoverable product retrieved or corrected), Terminated (FDA determined all reasonable efforts were made), or Pending (determined to be a recall but not yet classified).';
COMMENT ON COLUMN drug_enforcement.classification IS 'FDA hazard class for the relative degree of health hazard: Class I (could cause serious injury or death), Class II (temporary or medically reversible harm), Class III (unlikely to cause harm; a labeling or manufacturing violation).';
COMMENT ON COLUMN drug_enforcement.product_type IS 'Type of product recalled; for the drug endpoint this is always Drugs.';
COMMENT ON COLUMN drug_enforcement.voluntary_mandated IS 'Who initiated the recall: voluntary (firm-initiated, or at FDA request) versus FDA-mandated (the firm was ordered by FDA under the FD&C Act and related statutes).';
COMMENT ON COLUMN drug_enforcement.initial_firm_notification IS 'Method(s) by which the firm initially notified the public or its consignees of the recall (e.g. Letter, Press Release, Telephone, E-Mail).';
COMMENT ON COLUMN drug_enforcement.recalling_firm IS 'The firm that initiated the recall, or (for FDA-requested/mandated recalls) the firm with primary responsibility for manufacturing and/or marketing the recalled product.';
COMMENT ON COLUMN drug_enforcement.address_1 IS 'Street address (line 1) of the recalling firm.';
COMMENT ON COLUMN drug_enforcement.address_2 IS 'Street address (line 2) of the recalling firm.';
COMMENT ON COLUMN drug_enforcement.city IS 'City in which the recalling firm is located.';
COMMENT ON COLUMN drug_enforcement.state IS 'U.S. state in which the recalling firm is located.';
COMMENT ON COLUMN drug_enforcement.country IS 'Country in which the recalling firm is located.';
COMMENT ON COLUMN drug_enforcement.postal_code IS 'Postal / ZIP code of the recalling firm.';
COMMENT ON COLUMN drug_enforcement.distribution_pattern IS 'General area of initial distribution (e.g. specific states, or "nationwide" = the fifty states or a significant portion). Later redistribution by consignees may not be reflected.';
COMMENT ON COLUMN drug_enforcement.product_description IS 'Brief description of the product being recalled.';
COMMENT ON COLUMN drug_enforcement.product_quantity IS 'The amount of defective product subject to recall.';
COMMENT ON COLUMN drug_enforcement.reason_for_recall IS 'Information describing how the product is defective and violates the FD&C Act or related statutes.';
COMMENT ON COLUMN drug_enforcement.code_info IS 'List of all lot and/or serial numbers, product numbers, packer or manufacturer numbers, and sell/use-by dates that appear on the product or its labeling.';
COMMENT ON COLUMN drug_enforcement.more_code_info IS 'Additional lot/code information continued when it exceeds the code_info field.';
COMMENT ON COLUMN drug_enforcement.recall_initiation_date IS 'Date the firm first began notifying the public or its consignees of the recall (parsed to date).';
COMMENT ON COLUMN drug_enforcement.center_classification_date IS 'Date the FDA center assigned the recall classification (parsed to date).';
COMMENT ON COLUMN drug_enforcement.termination_date IS 'Date the recall was terminated, i.e. FDA determined all reasonable efforts to remove or correct the product were made (parsed to date; null while the recall is ongoing).';
