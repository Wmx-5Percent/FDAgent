---
name: db-column-docs-from-dictionary
description: "Attach an upstream field dictionary's VERBATIM definitions to a database table as Postgres column COMMENTs, with explicit markers for anything inferred. Use when: documenting table columns from the openFDA data dictionary or a similar public field reference; backfilling COMMENT ON COLUMN from official field definitions; fixing schema descriptions that are missing or were paraphrased. Not for: inventing column meanings, or general schema/data-model design."
---

# Documenting DB columns from an upstream field dictionary

## Metadata
- **Type**: Workflow
- **Use when**: a table's columns need authoritative documentation sourced from a dataset's official field reference (worked example: openFDA `drug/enforcement` → table `drug_enforcement`).
- **Output**: a versioned, idempotent migration `sql/NNN_<table>_comments.sql`, applied to the DB.
- **Created**: 2026-06-25.

## Goal
Make a table self-documenting by attaching to every column the **verbatim** definition from the dataset's official field reference — with anything that is *not* from that source explicitly marked.

## Boundaries (hard rules — violating any of these makes the result wrong)
- **Verbatim only.** Copy the upstream description exactly, including punctuation and wording. Never paraphrase, shorten, or "improve" official text. Changing even one word means it no longer matches the dictionary.
- **Never pass off inference as official.** If the source leaves a field blank, or omits it, your wording must carry an `[inferred]` marker; columns you (not the dataset) created carry a `[project field, not from <source>]` marker. No unmarked invented text.
- **Trust the structured reference, not the rendered page.** The HTML data-dictionary UI is JS-rendered and category-filtered — do not transcribe definitions from it.

## Acceptance criteria (a fresh agent can self-check)
- Every *documented* column's comment is **byte-identical** to the upstream field reference. Spot-diff a few long ones (e.g. the recall `voluntary_mandated` legal text) against the source.
- Every column whose definition is **not** in the source is marked `[inferred]`; project/ingester columns are marked as such. Nothing invented is left unmarked.
- The migration is **idempotent**: re-running is a no-op, and `col_description('<table>'::regclass, attnum)` is non-null for every intended column.
- Non-ASCII punctuation (e.g. “ ” curly quotes) survives, and the SQL is valid (single quotes inside literals doubled).

## Resources & verification
- **Authoritative source (openFDA):** `https://open.fda.gov/fields/<endpoint>.yaml` — e.g. `https://open.fda.gov/fields/drugenforcement.yaml`. Each field has `description` and (for enums) `possible_values`. This is the same data behind the data-dictionary UI, in machine-readable form.
- **Pull + parse, never eyeball:** `curl -s <yaml-url> -o /tmp/f.yaml`, then parse with a real YAML library (`python -c "import yaml; d=yaml.safe_load(open('/tmp/f.yaml')); ..."`) to emit `field → description` and enum value meanings. Guard for non-dict / empty entries.
- **Apply:** `psql -d <db> -v ON_ERROR_STOP=1 -f sql/NNN_<table>_comments.sql`.
- **Verify:** `SELECT col_description('<table>'::regclass, attnum) FROM pg_attribute ...` via `psql` or a direct DB connection.

## Known pitfalls (each one actually happened in this codebase)
- **Paraphrasing the dictionary.** A first pass shortened `voluntary_mandated` and others; the reviewer immediately caught the mismatch with openFDA. → verbatim, always.
- **Inventing text for blank fields.** openFDA leaves several enforcement fields blank (`address_1`, `address_2`, `more_code_info`, `center_classification_date`, `termination_date`) and omits some entirely that still appear in the data (`postal_code`). Mark these `[inferred]` — do not present guesses as official.
- **`get_object_details` (Postgres MCP) does NOT render column comments**, and the MCP's restricted-mode `execute_sql` **rejects catalog queries** (`col_description`, `::regclass` get blocked by its safe-SQL validator). Verify via `psql \d+` or a direct `psycopg` connection instead. (Plain data `SELECT`s through the MCP work fine.)
- **`fetch_webpage` reformats and line-wraps text** → unusable for verbatim copying. Use raw `curl` + a parser.
- **Enum fields** (`classification`, `status`, `product_type`) carry their meaning in `possible_values`, sometimes with an *empty* top-level `description`. Include those value meanings verbatim; for a blank description, say so rather than fabricating one.

## Output spec (example)
```sql
-- Header: cite the exact source URL + fetch date and the marker convention.
COMMENT ON TABLE drug_enforcement IS
    'openFDA drug recall enforcement reports ... [project description, not from openFDA]';

-- Documented field: VERBATIM openFDA text, no marker.
COMMENT ON COLUMN drug_enforcement.voluntary_mandated IS
    'Describes who initiated the recall. Recalls are almost always voluntary ... or the Food Safety Modernization Act (FSMA).';

-- Not documented upstream: our wording, clearly marked.
COMMENT ON COLUMN drug_enforcement.postal_code IS
    '[inferred] Postal/ZIP code of the recalling firm. (Not documented in the openFDA field reference.)';
```
Store at `sql/NNN_<table>_comments.sql`; keep it idempotent (`COMMENT ON` overwrites) so it can be re-applied after each ingest or schema change.
