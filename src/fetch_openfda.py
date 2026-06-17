#!/usr/bin/env python3
"""
fetch_openfda.py — Pull public-domain MRI adverse-event reports from the
openFDA MAUDE API and save them as ticket-like records.

WHY: openFDA device-event data is U.S.-government PUBLIC DOMAIN, already
de-identified by the FDA (look for "(B)(4)" redaction markers). Each report
contains free-text narratives that are structurally identical to service
tickets — perfect, legal, zero-IP-risk training data for the ticket agent.

Output (data/raw/):
  - tickets.jsonl : one JSON object per report (full metadata)
  - tickets.csv   : flat table with a `ticket_text` column (analog of your
                    real Excel column Q) for the LLM structuring pipeline.

Usage:
  python3 src/fetch_openfda.py --count 2000
  python3 src/fetch_openfda.py --count 5000 --query 'magnetic resonance'

No third-party packages required (uses only the Python standard library).
Optional: set OPENFDA_API_KEY env var to raise rate limits.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://api.fda.gov/device/event.json"
PAGE_SIZE = 100  # openFDA returns up to 1000/req; 100 keeps each call light
REQUEST_PAUSE_S = 0.3  # be polite; well under the 240 req/min limit

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "raw"


def build_url(query: str, skip: int, limit: int) -> str:
    params = {
        "search": f'device.generic_name:"{query}"',
        "limit": limit,
        "skip": skip,
    }
    api_key = os.environ.get("OPENFDA_API_KEY")
    if api_key:
        params["api_key"] = api_key
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def fetch_page(query: str, skip: int, limit: int) -> list[dict]:
    url = build_url(query, skip, limit)
    req = urllib.request.Request(url, headers={"User-Agent": "ticket-agent-portfolio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("results", [])
    except urllib.error.HTTPError as e:
        # openFDA returns 404 when skip exceeds available results — normal stop.
        if e.code == 404:
            return []
        print(f"  ! HTTP {e.code} at skip={skip}: {e.reason}", file=sys.stderr)
        return []
    except Exception as e:  # noqa: BLE001 - keep the puller resilient
        print(f"  ! error at skip={skip}: {e!r}", file=sys.stderr)
        return []


def to_ticket(record: dict) -> dict:
    """Flatten one MAUDE report into a ticket-like row."""
    device = (record.get("device") or [{}])[0]

    # Combine all narrative sections into one messy, unstructured blob —
    # this mirrors a raw service ticket (your real column Q). The LLM
    # structuring pipeline will later split it into the 3 columns.
    sections = []
    for t in record.get("mdr_text") or []:
        label = t.get("text_type_code") or "Narrative"
        body = (t.get("text") or "").strip()
        if body:
            sections.append(f"[{label}] {body}")
    ticket_text = "\n".join(sections)

    return {
        "report_id": record.get("report_number") or record.get("mdr_report_key"),
        "date_received": record.get("date_received"),
        "event_type": record.get("event_type"),
        "brand_name": device.get("brand_name"),
        "generic_name": device.get("generic_name"),
        "manufacturer": device.get("manufacturer_d_name"),
        "product_problems": "; ".join(record.get("product_problems") or []),
        "ticket_text": ticket_text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch MRI tickets from openFDA MAUDE.")
    parser.add_argument("--count", type=int, default=2000, help="target number of records")
    parser.add_argument("--query", default="magnetic resonance", help="device.generic_name search term")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = OUT_DIR / "tickets.jsonl"
    csv_path = OUT_DIR / "tickets.csv"

    print(f"Fetching up to {args.count} '{args.query}' reports from openFDA MAUDE...")

    seen: set[str] = set()
    rows: list[dict] = []
    skip = 0
    while len(rows) < args.count:
        limit = min(PAGE_SIZE, args.count - len(rows))
        results = fetch_page(args.query, skip, limit)
        if not results:
            print("  (no more results)")
            break
        for rec in results:
            row = to_ticket(rec)
            rid = str(row["report_id"])
            if rid in seen or not row["ticket_text"]:
                continue
            seen.add(rid)
            rows.append(row)
        print(f"  fetched {len(rows)} / {args.count}")
        skip += limit
        if skip >= 25000:  # openFDA skip ceiling without paging tokens
            print("  reached openFDA skip ceiling (25000)")
            break
        time.sleep(REQUEST_PAUSE_S)

    # Write JSONL
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Write CSV
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print(f"\nDone. {len(rows)} tickets saved:")
    print(f"  - {jsonl_path.relative_to(ROOT)}")
    print(f"  - {csv_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
