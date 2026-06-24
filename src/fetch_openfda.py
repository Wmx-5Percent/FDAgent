#!/usr/bin/env python3
"""Generic openFDA -> PostgreSQL ingester.

Pulls records from *any* openFDA endpoint and upserts them into a PostgreSQL
table as JSONB. The same code works for every data source — you only change the
``--endpoint`` and ``--table`` flags.

Why JSONB instead of one column per field?
    Every openFDA endpoint has a different (and nested) schema. Storing the full
    record as JSONB keeps 100% fidelity and makes the loader source-agnostic. We
    additionally extract two scalars used by almost every downstream query:
      * ``id``          -- the endpoint's natural key (for idempotent upserts)
      * ``report_date`` -- a date column (for time filters + incremental pulls)
    You can still query any nested field directly, e.g. ``raw->>'classification'``,
    and add per-endpoint generated columns / views later.

Design properties:
    * Idempotent   -- ``ON CONFLICT (id) DO UPDATE`` means re-runs never duplicate.
    * Incremental  -- ``--since`` (or ``--since auto``) only fetches new records.
    * Resilient    -- exponential backoff on 429/5xx/timeouts.
    * Injection-safe -- table name goes through ``psycopg.sql.Identifier``.

Examples
--------
    # First full load of drug recall enforcement reports
    python src/fetch_openfda.py --endpoint drug/enforcement --table drug_enforcement

    # Another source into its own table
    python src/fetch_openfda.py --endpoint food/enforcement --table food_enforcement

    # Scheduled incremental top-up (only rows newer than what we already stored)
    python src/fetch_openfda.py --endpoint drug/enforcement --table drug_enforcement --since auto

    # Filtered pull
    python src/fetch_openfda.py --endpoint drug/enforcement --table drug_enforcement \
        --search 'classification:"Class I"'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from typing import Any, Iterable

import requests

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - friendly hint
    sys.exit("psycopg (v3) is required:  pip install 'psycopg[binary]'")

API_ROOT = "https://api.fda.gov"
PAGE_LIMIT_MAX = 1000      # openFDA hard cap on `limit`
SKIP_MAX = 25_000          # openFDA hard cap on `skip` (deep paging beyond needs date partitioning)

# Per-endpoint (natural_key_field, date_field). These let the loader extract the
# right id/date out of the box. Any endpoint not listed here still works — pass
# --id-field / --date-field, or rely on the content-hash fallback for the id.
ENDPOINT_DEFAULTS: dict[str, tuple[str | None, str | None]] = {
    "drug/enforcement":   ("recall_number", "report_date"),
    "food/enforcement":   ("recall_number", "report_date"),
    "device/enforcement": ("recall_number", "report_date"),
    "device/recall":      ("cfres_id", "event_date_posted"),
    "drug/event":         ("safetyreportid", "receivedate"),
    "device/event":       ("mdr_report_key", "date_received"),
    "drug/label":         ("id", "effective_time"),
    "drug/ndc":           ("product_ndc", None),
    "drug/drugsfda":      ("application_number", None),
    "device/510k":        ("k_number", "decision_date"),
    "device/pma":         ("pma_number", "decision_date"),
    "device/classification": ("product_code", None),
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_fda_date(value: Any) -> date | None:
    """openFDA dates are usually 'YYYYMMDD'; some are ISO 'YYYY-MM-DD'."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) >= 8 and v[:8].isdigit():
        try:
            return datetime.strptime(v[:8], "%Y%m%d").date()
        except ValueError:
            pass
    try:
        return datetime.strptime(v[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def record_id(rec: dict, id_field: str | None) -> str:
    """Return the natural key, or a deterministic content hash as fallback."""
    if id_field:
        val = rec.get(id_field)
        if val not in (None, ""):
            return str(val)
    # Fallback: stable hash so upserts stay idempotent even without a known key.
    blob = json.dumps(rec, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha1:" + hashlib.sha1(blob).hexdigest()


def fda_date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# openFDA HTTP
# --------------------------------------------------------------------------- #
def fetch_page(
    endpoint: str,
    *,
    search: str | None,
    skip: int,
    limit: int,
    api_key: str | None,
    session: requests.Session,
    max_retries: int = 5,
) -> tuple[list[dict], int]:
    """Fetch one page. Returns (results, total). 404 == no matches -> ([], 0)."""
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if search:
        params["search"] = search
    if api_key:
        params["api_key"] = api_key

    url = f"{API_ROOT}/{endpoint}.json"
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            print(f"  ! network error ({exc}); retry {attempt}/{max_retries} in {backoff:.0f}s")
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 404:
            return [], 0  # openFDA returns 404 when a search matches nothing
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                resp.raise_for_status()
            wait = float(resp.headers.get("Retry-After", backoff))
            print(f"  ! HTTP {resp.status_code}; retry {attempt}/{max_retries} in {wait:.0f}s")
            time.sleep(wait)
            backoff *= 2
            continue

        resp.raise_for_status()
        payload = resp.json()
        total = payload.get("meta", {}).get("results", {}).get("total", 0)
        return payload.get("results", []), total

    return [], 0


# --------------------------------------------------------------------------- #
# database
# --------------------------------------------------------------------------- #
def ensure_table(conn: "psycopg.Connection", table: str) -> None:
    tbl = sql.Identifier(table)
    with conn.cursor() as cur:
        cur.execute(sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {tbl} (
                id          text PRIMARY KEY,
                source      text NOT NULL,
                report_date date,
                raw         jsonb NOT NULL,
                fetched_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        ).format(tbl=tbl))
        cur.execute(sql.SQL(
            "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} (report_date)"
        ).format(idx=sql.Identifier(f"{table}_report_date_idx"), tbl=tbl))
        cur.execute(sql.SQL(
            "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} USING gin (raw jsonb_path_ops)"
        ).format(idx=sql.Identifier(f"{table}_raw_gin_idx"), tbl=tbl))
    conn.commit()


def upsert_rows(conn: "psycopg.Connection", table: str, rows: Iterable[tuple]) -> int:
    tbl = sql.Identifier(table)
    stmt = sql.SQL(
        """
        INSERT INTO {tbl} (id, source, report_date, raw, fetched_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (id) DO UPDATE
           SET source      = EXCLUDED.source,
               report_date = EXCLUDED.report_date,
               raw         = EXCLUDED.raw,
               fetched_at  = now()
        """
    ).format(tbl=tbl)
    rows = list(rows)
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(stmt, rows)
    conn.commit()
    return len(rows)


def max_report_date(conn: "psycopg.Connection", table: str) -> date | None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT max(report_date) FROM {tbl}").format(
                tbl=sql.Identifier(table)))
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None


# --------------------------------------------------------------------------- #
# main flow
# --------------------------------------------------------------------------- #
def build_search(base_search: str | None, date_field: str | None,
                 since: date | None) -> str | None:
    # NOTE: use literal spaces here, NOT '+'. requests percent-encodes the value;
    # a space becomes '+' (which openFDA reads as a space), but a literal '+'
    # becomes '%2B' (a literal plus) and breaks the Lucene range query.
    if since is None or not date_field:
        return base_search
    today = fda_date_str(date.today())
    date_clause = f"{date_field}:[{fda_date_str(since)} TO {today}]"
    return f"({base_search}) AND {date_clause}" if base_search else date_clause


def run(args: argparse.Namespace) -> int:
    id_field, date_field = ENDPOINT_DEFAULTS.get(args.endpoint, (None, None))
    if args.id_field:
        id_field = args.id_field
    if args.date_field:
        date_field = args.date_field

    dsn = args.db or os.environ.get("DATABASE_URL") or "postgresql://localhost:5432/fda"
    api_key = args.api_key or os.environ.get("OPENFDA_API_KEY")

    print(f"endpoint : {args.endpoint}")
    print(f"table    : {args.table}")
    print(f"id field : {id_field or '(content hash fallback)'}")
    print(f"date fld : {date_field or '(none)'}")
    print(f"database : {dsn}")

    conn = psycopg.connect(dsn)
    try:
        if args.create_table:
            ensure_table(conn, args.table)

        # Resolve incremental window.
        since: date | None = None
        if args.since == "auto":
            since = max_report_date(conn, args.table)
            print(f"since    : auto -> {since or '(table empty, full load)'}")
        elif args.since:
            since = parse_fda_date(args.since) or datetime.strptime(args.since, "%Y-%m-%d").date()
            print(f"since    : {since}")

        search = build_search(args.search, date_field, since)
        if search:
            print(f"search   : {search}")

        session = requests.Session()
        session.headers["User-Agent"] = "fdaAgent-ingester/1.0"

        limit = min(args.limit, PAGE_LIMIT_MAX)
        skip = 0
        written = 0
        total = None

        while True:
            results, page_total = fetch_page(
                args.endpoint, search=search, skip=skip, limit=limit,
                api_key=api_key, session=session)
            if total is None:
                total = page_total
                print(f"matched  : {total} records\n")
            if not results:
                break

            batch = []
            for rec in results:
                rid = record_id(rec, id_field)
                rdate = parse_fda_date(rec.get(date_field)) if date_field else None
                batch.append((rid, args.endpoint, rdate, Jsonb(rec)))

            if args.dry_run:
                print(f"  [dry-run] would upsert {len(batch)} rows (skip={skip})")
            else:
                written += upsert_rows(conn, args.table, batch)
                print(f"  upserted {written}/{total} (skip={skip})")

            skip += limit
            if args.max_records and skip >= args.max_records:
                print(f"\nreached --max-records={args.max_records}, stopping.")
                break
            if skip >= total:
                break
            if skip >= SKIP_MAX:
                print(f"\n! Hit openFDA skip cap ({SKIP_MAX}). {total - skip} records "
                      f"remain. Use --since to partition by date, or download the bulk "
                      f"JSON files for a full load.")
                break
            time.sleep(args.sleep)

        print(f"\nDone. {'(dry-run) ' if args.dry_run else ''}{written} rows upserted "
              f"into {args.table}.")
        return 0
    finally:
        conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generic openFDA -> PostgreSQL ingester (JSONB upsert).")
    p.add_argument("--endpoint", required=True,
                   help="openFDA endpoint, e.g. drug/enforcement, food/enforcement, device/event")
    p.add_argument("--table", required=True,
                   help="Target PostgreSQL table name (created if missing).")
    p.add_argument("--db", default=None,
                   help="Postgres DSN (default: $DATABASE_URL or postgresql://localhost:5432/fda)")
    p.add_argument("--search", default=None,
                   help="openFDA search filter, e.g. 'classification:\"Class I\"'")
    p.add_argument("--since", default=None,
                   help="Incremental pull: a date 'YYYY-MM-DD', or 'auto' to use MAX(report_date) in the table.")
    p.add_argument("--limit", type=int, default=PAGE_LIMIT_MAX,
                   help=f"Page size (max {PAGE_LIMIT_MAX}).")
    p.add_argument("--max-records", type=int, default=None,
                   help="Stop after roughly this many records (for testing).")
    p.add_argument("--id-field", default=None,
                   help="Override the natural-key field for this endpoint.")
    p.add_argument("--date-field", default=None,
                   help="Override the date field used for report_date + --since.")
    p.add_argument("--api-key", default=None,
                   help="openFDA API key (default: $OPENFDA_API_KEY). Raises rate limits.")
    p.add_argument("--sleep", type=float, default=0.2,
                   help="Seconds to sleep between pages (politeness).")
    p.add_argument("--no-create-table", dest="create_table", action="store_false",
                   help="Do not auto-create the table / indexes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and report counts but do not write to the database.")
    p.set_defaults(create_table=True)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
