"""Ingest EIA API v2 price series into the star schema.

    python -m ingestion.eia           # incremental: re-pull a 30-day overlap window, upsert
    python -m ingestion.eia --full    # backfill everything since 2010-01-01

Idempotent: running it twice changes nothing the second time. The overlap window picks up
EIA revisions, which are counted as `rows_updated` rather than silently overwritten.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import psycopg
import requests

from ingestion.config import get_settings, setup_logging
from ingestion.db import connect
from ingestion.http import get_json

log = logging.getLogger("eia")

EIA_BASE = "https://api.eia.gov/v2"
PAGE_SIZE = 5000  # EIA's documented maximum rows per JSON response
HISTORY_START = date(2010, 1, 1)  # first day in core.dim_date
OVERLAP_DAYS = 30


@dataclass(frozen=True)
class Series:
    series_id: str
    route: str
    frequency: str  # 'daily' or 'weekly'
    target: str  # 'gas' or 'diesel', selects the SQL below
    key: str  # hub_code (gas) or region_code (diesel)

    @property
    def source(self) -> str:
        return f"EIA:{self.series_id}"


SERIES = (
    Series("RNGWHHD", "natural-gas/pri/fut", "daily", "gas", "HENRY"),
    Series("EMD_EPD2D_PTE_R30_DPG", "petroleum/pri/gnd", "weekly", "diesel", "PADD3"),
    Series("EMD_EPD2D_PTE_NUS_DPG", "petroleum/pri/gnd", "weekly", "diesel", "US"),
)

LATEST_SQL = {
    "gas": "SELECT max(price_date) FROM core.fact_gas_price_daily WHERE source_series = %s",
    "diesel": "SELECT max(week_start) FROM core.fact_diesel_weekly WHERE source_series = %s",
}

UPSERT_TABLE = {"gas": "core.fact_gas_price_daily", "diesel": "core.fact_diesel_weekly"}

# Rows land in a temp staging table first, then one set-based upsert moves them in.
# `WHERE ... IS DISTINCT FROM` skips rows whose value hasn't changed, so RETURNING yields only
# real inserts and updates. `xmax = 0` is true for a freshly inserted row in Postgres.
UPSERT_SQL = {
    "gas": """
        INSERT INTO core.fact_gas_price_daily AS f
               (price_date, hub_id, price_usd_mmbtu, source_series)
        SELECT s.period, h.hub_id, s.value, %(source)s
        FROM stage s JOIN core.dim_hub h ON h.hub_code = %(key)s
        ON CONFLICT (price_date, hub_id) DO UPDATE
           SET price_usd_mmbtu = EXCLUDED.price_usd_mmbtu,
               source_series   = EXCLUDED.source_series,
               ingested_at     = now()
           WHERE f.price_usd_mmbtu IS DISTINCT FROM EXCLUDED.price_usd_mmbtu
        RETURNING (xmax = 0) AS inserted""",
    "diesel": """
        INSERT INTO core.fact_diesel_weekly AS f
               (week_start, region_code, price_usd_gal, source_series)
        SELECT s.period, %(key)s, s.value, %(source)s
        FROM stage s
        ON CONFLICT (week_start, region_code) DO UPDATE
           SET price_usd_gal = EXCLUDED.price_usd_gal,
               source_series = EXCLUDED.source_series,
               ingested_at   = now()
           WHERE f.price_usd_gal IS DISTINCT FROM EXCLUDED.price_usd_gal
        RETURNING (xmax = 0) AS inserted""",
}


@dataclass(frozen=True)
class Parsed:
    values: dict[date, Decimal]
    skipped: int  # null or non-numeric value
    duplicates: int  # period seen more than once in the payload (last one wins)


def parse(records: list[dict], series_id: str) -> Parsed:
    """Turn EIA rows ({'period': '2026-10-20', 'value': '3.1', ...}) into date -> Decimal."""
    values: dict[date, Decimal] = {}
    skipped = duplicates = 0
    for rec in records:
        if rec.get("series") != series_id:
            raise ValueError(f"expected series {series_id}, got {rec.get('series')!r}")
        try:
            value = Decimal(rec["value"])  # EIA sends numbers as strings; None on gaps
        except (TypeError, InvalidOperation):
            skipped += 1
            continue
        if not value.is_finite():
            skipped += 1
            continue
        period = date.fromisoformat(rec["period"])
        if period in values:
            duplicates += 1
        values[period] = value
    return Parsed(values, skipped, duplicates)


def fetch(session: requests.Session, api_key: str, series: Series, start: date) -> list[dict]:
    """All rows for one series from `start` onward, following EIA's offset pagination."""
    params = {
        "api_key": api_key,
        "frequency": series.frequency,
        "data[]": "value",
        "facets[series][]": series.series_id,
        "start": start.isoformat(),
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": PAGE_SIZE,
    }
    records: list[dict] = []
    while True:
        body = get_json(
            session, f"{EIA_BASE}/{series.route}/data/", {**params, "offset": len(records)}
        )
        page = body["response"]["data"]
        records.extend(page)
        if not page or len(records) >= int(body["response"]["total"]):
            return records


def upsert(
    conn: psycopg.Connection, series: Series, values: dict[date, Decimal]
) -> tuple[int, int]:
    """Load values into the fact table; returns (inserted, updated). Caller commits."""
    conn.execute("CREATE TEMP TABLE stage (period date PRIMARY KEY, value numeric) ON COMMIT DROP")
    with conn.cursor().copy("COPY stage (period, value) FROM STDIN") as copy:
        for row in values.items():
            copy.write_row(row)
    flags = conn.execute(UPSERT_SQL[series.target], {"source": series.source, "key": series.key})
    inserted_flags = [inserted for (inserted,) in flags.fetchall()]
    inserted = sum(inserted_flags)
    # Fresh planner statistics: without them Postgres misjudges row counts after a bulk load
    # and v_netback_daily falls into a nested-loop plan (29 s instead of 1 s).
    conn.execute(f"ANALYZE {UPSERT_TABLE[series.target]}")
    return inserted, len(inserted_flags) - inserted


def window_start(conn: psycopg.Connection, series: Series, full: bool) -> date:
    (latest,) = conn.execute(LATEST_SQL[series.target], (series.source,)).fetchone()
    if full or latest is None:
        return HISTORY_START
    return max(HISTORY_START, latest - timedelta(days=OVERLAP_DAYS))


def ingest_series(conn, run_log, session, api_key: str, series: Series, full: bool) -> bool:
    start = window_start(conn, series, full)
    (run_id,) = run_log.execute(
        "INSERT INTO ops.ingest_run (source, window_start) VALUES (%s, %s) RETURNING run_id",
        (series.source, start),
    ).fetchone()
    try:
        records = fetch(session, api_key, series, start)
        parsed = parse(records, series.series_id)
        inserted, updated = upsert(conn, series, parsed.values)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        run_log.execute(
            "UPDATE ops.ingest_run SET status = 'failed', finished_at = now(), error = %s"
            " WHERE run_id = %s",
            (f"{type(exc).__name__}: {exc}"[:2000], run_id),
        )
        log.exception("%s failed", series.series_id)
        return False

    run_log.execute(
        """UPDATE ops.ingest_run
           SET status = 'success', finished_at = now(), rows_fetched = %s, rows_skipped = %s,
               rows_duplicate = %s, rows_inserted = %s, rows_updated = %s
           WHERE run_id = %s""",
        (len(records), parsed.skipped, parsed.duplicates, inserted, updated, run_id),
    )
    log.info(
        "%-22s from %s  fetched=%d skipped=%d dup=%d inserted=%d updated=%d",
        series.series_id, start, len(records), parsed.skipped, parsed.duplicates, inserted, updated,
    )  # fmt: skip
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="backfill from 2010-01-01")
    args = parser.parse_args(argv)
    setup_logging()

    api_key = get_settings().eia_api_key
    if not api_key:
        log.error("EIA_API_KEY is not set (see .env.example)")
        return 2

    # Run bookkeeping uses its own autocommit connection, so a failed load is rolled back
    # while the record of that failure is kept.
    with connect() as conn, connect(autocommit=True) as run_log, requests.Session() as session:
        session.headers["User-Agent"] = "vpv-netback/0.1 (portfolio project)"
        ok = [ingest_series(conn, run_log, session, api_key, s, args.full) for s in SERIES]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
