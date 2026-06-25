-- Column documentation for drug_enforcement.
-- Descriptions are taken VERBATIM from the official openFDA drug/enforcement field
-- reference (https://open.fda.gov/fields/drugenforcement.yaml — the same source behind
-- https://open.fda.gov/data/datadictionary), fetched 2026-06-25.
--
-- Honesty markers:
--   * No marker            -> exact verbatim openFDA text.
--   * "[inferred]"         -> our wording; openFDA does NOT document this field.
--   * "[project field...]" -> our own ingester column, not an openFDA field.
-- Stored as Postgres COMMENTs so the schema is self-describing (psql \d+,
-- pg_description / col_description, and the future Text-to-SQL layer).
-- Idempotent: COMMENT ON overwrites the previous comment; safe to re-run.

COMMENT ON TABLE drug_enforcement IS
    'openFDA drug recall enforcement reports (endpoint drug/enforcement). One row per recall_number; the full original API record is kept in raw (JSONB) and key fields are parsed into typed columns. [project description, not from openFDA]';

-- Ingester-owned columns (not openFDA fields) -------------------------------
COMMENT ON COLUMN drug_enforcement.id         IS 'Ingester-assigned primary key (equals recall_number); enables idempotent upserts. [project field, not from openFDA]';
COMMENT ON COLUMN drug_enforcement.source     IS 'openFDA endpoint this row was ingested from, e.g. drug/enforcement. [project field, not from openFDA]';
COMMENT ON COLUMN drug_enforcement.raw        IS 'Full original openFDA record exactly as returned by the API (JSONB); source of truth for all parsed columns. [project field, not from openFDA]';
COMMENT ON COLUMN drug_enforcement.fetched_at IS 'Timestamp when the ingester last inserted/updated this row. [project field, not from openFDA]';

-- openFDA fields — VERBATIM openFDA definitions -----------------------------
COMMENT ON COLUMN drug_enforcement.report_date IS 'Date that the FDA issued the enforcement report for the product recall.';
COMMENT ON COLUMN drug_enforcement.recall_number IS 'A numerical designation assigned by FDA to a specific recall event used for tracking purposes.';
COMMENT ON COLUMN drug_enforcement.event_id IS 'A numerical designation assigned by FDA to a specific recall event used for tracking purposes.';
COMMENT ON COLUMN drug_enforcement.classification IS 'Numerical designation (I, II, or III) that is assigned by FDA to a particular product recall that indicates the relative degree of health hazard. Class I: Dangerous or defective products that predictably could cause serious health problems or death. Examples include: food found to contain botulinum toxin, food with undeclared allergens, a label mix-up on a lifesaving drug, or a defective artificial heart valve. Class II: Products that might cause a temporary health problem, or pose only a slight threat of a serious nature. Example: a drug that is under-strength but that is not used to treat life-threatening situations. Class III: Products that are unlikely to cause any adverse health reaction, but that violate FDA labeling or manufacturing laws. Examples include: a minor container defect and lack of English labeling in a retail food.';
COMMENT ON COLUMN drug_enforcement.status IS 'Recall status (openFDA leaves the field description blank; the allowed values and their verbatim meanings are): On-Going: A recall which is currently in progress. Completed: The recall action reaches the point at which the firm has actually retrieved and impounded all outstanding product that could reasonably be expected to be recovered, or has completed all product corrections. Terminated: FDA has determined that all reasonable efforts have been made to remove or correct the violative product in accordance with the recall strategy, and proper disposition has been made according to the degree of hazard. Pending: Actions that have been determined to be recalls, but that remain in the process of being classified.';
COMMENT ON COLUMN drug_enforcement.product_type IS 'The type of product being recalled. For drug queries, this will always be `Drugs`.';
COMMENT ON COLUMN drug_enforcement.voluntary_mandated IS 'Describes who initiated the recall. Recalls are almost always voluntary, meaning initiated by a firm. A recall is deemed voluntary when the firm voluntarily removes or corrects marketed products or the FDA requests the marketed products be removed or corrected. A recall is mandated when the firm was ordered by the FDA to remove or correct the marketed products, under section 518(e) of the FD&C Act, National Childhood Vaccine Injury Act of 1986, 21 CFR 1271.440, Infant Formula Act of 1980 and its 1986 amendments, or the Food Safety Modernization Act (FSMA).';
COMMENT ON COLUMN drug_enforcement.initial_firm_notification IS 'The method(s) by which the firm initially notified the public or their consignees of a recall. A consignee is a person or firm named in a bill of lading to whom or to whose order the product has or will be delivered.';
COMMENT ON COLUMN drug_enforcement.recalling_firm IS 'The firm that initiates a recall or, in the case of an FDA requested recall or FDA mandated recall, the firm that has primary responsibility for the manufacture and (or) marketing of the product to be recalled.';
COMMENT ON COLUMN drug_enforcement.city IS 'The city in which the recalling firm is located.';
COMMENT ON COLUMN drug_enforcement.state IS 'The U.S. state in which the recalling firm is located.';
COMMENT ON COLUMN drug_enforcement.country IS 'The country in which the recalling firm is located.';
COMMENT ON COLUMN drug_enforcement.distribution_pattern IS 'General area of initial distribution such as, “Distributors in 6 states: NY, VA, TX, GA, FL and MA; the Virgin Islands; Canada and Japan”. The term “nationwide” is defined to mean the fifty states or a significant portion.  Note that subsequent distribution by the consignees to other parties may not be included.';
COMMENT ON COLUMN drug_enforcement.product_description IS 'Brief description of the product being recalled.';
COMMENT ON COLUMN drug_enforcement.product_quantity IS 'The amount of defective product subject to recall.';
COMMENT ON COLUMN drug_enforcement.reason_for_recall IS 'Information describing how the product is defective and violates the FD&C Act or related statutes.';
COMMENT ON COLUMN drug_enforcement.code_info IS 'A list of all lot and/or serial numbers, product numbers, packer or manufacturer numbers, sell or use by dates, etc., which appear on the product or its labeling.';
COMMENT ON COLUMN drug_enforcement.recall_initiation_date IS 'Date that the firm first began notifying the public or their consignees of the recall.';

-- Fields present in the data but NOT documented in the openFDA reference ------
-- (wording below is ours, clearly marked [inferred]) ------------------------
COMMENT ON COLUMN drug_enforcement.address_1 IS '[inferred] Street address line 1 of the recalling firm. (Not documented in the openFDA field reference.)';
COMMENT ON COLUMN drug_enforcement.address_2 IS '[inferred] Street address line 2 of the recalling firm. (Not documented in the openFDA field reference.)';
COMMENT ON COLUMN drug_enforcement.postal_code IS '[inferred] Postal/ZIP code of the recalling firm. (Not documented in the openFDA field reference.)';
COMMENT ON COLUMN drug_enforcement.more_code_info IS '[inferred] Overflow of code_info when the lot/code list exceeds the field length. (Not documented in the openFDA field reference.)';
COMMENT ON COLUMN drug_enforcement.center_classification_date IS '[inferred] Date the FDA center assigned the recall classification. (Not documented in the openFDA field reference.)';
COMMENT ON COLUMN drug_enforcement.termination_date IS '[inferred] Date the recall was terminated. (Not documented in the openFDA field reference.)';
