"""Data-quality checks, written to ops.dq_results.

    python -m ingestion.dq        # run all checks; exit 1 if any 'error' check fails

Each check is one SQL query returning (observed, details). The check passes when
observed <= threshold. Thresholds were calibrated on 2010-2026 history
(docs/04-ingestion-and-dq.md), so a failure means "unlike anything seen before".
"""

import logging
import sys
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from ingestion.config import setup_logging
from ingestion.db import connect

log = logging.getLogger("dq")


@dataclass(frozen=True)
class Check:
    name: str
    target: str
    severity: str  # 'error' blocks the pipeline, 'warn' only records
    threshold: float
    sql: str  # must return one row: (observed numeric, details jsonb)


HENRY = "(SELECT hub_id FROM core.dim_hub WHERE hub_code = 'HENRY')"

CHECKS = (
    # ── stale feeds ──────────────────────────────────────────────────────────
    Check(
        "gas_feed_stale_days",
        "fact_gas_price_daily",
        "error",
        10,
        # EIA releases the daily series weekly (Wednesday, through Tuesday): lag peaks at 8 days.
        f"""SELECT current_date - max(price_date),
                   jsonb_build_object('latest', max(price_date))
            FROM core.fact_gas_price_daily WHERE hub_id = {HENRY}""",
    ),
    Check(
        "diesel_feed_stale_days",
        "fact_diesel_weekly",
        "error",
        10,
        # Monday survey, published Monday evening (Tuesday after a holiday).
        """SELECT max(current_date - latest),
                  jsonb_object_agg(region_code, latest)
           FROM (SELECT region_code, max(week_start) AS latest
                 FROM core.fact_diesel_weekly GROUP BY region_code) r""",
    ),
    # ── missing periods ──────────────────────────────────────────────────────
    Check(
        "gas_missing_weekdays_90d",
        "fact_gas_price_daily",
        "warn",
        8,
        # Market holidays leave 5-8 weekday gaps per 90 days; 8 is the 2010-2026 maximum.
        f"""SELECT count(*), coalesce(jsonb_agg(d.date_day ORDER BY d.date_day), '[]')
            FROM core.dim_date d
            LEFT JOIN core.fact_gas_price_daily p
                   ON p.price_date = d.date_day AND p.hub_id = {HENRY}
            WHERE NOT d.is_weekend AND p.price_date IS NULL
              AND d.date_day BETWEEN (SELECT max(price_date) - 90 FROM core.fact_gas_price_daily)
                                 AND (SELECT max(price_date) FROM core.fact_gas_price_daily)""",
    ),
    Check(
        "diesel_missing_weeks",
        "fact_diesel_weekly",
        "error",
        0,
        # Every Monday between a region's first and last week must be present.
        """SELECT count(*), coalesce(jsonb_agg(jsonb_build_object('region', r.region_code,
                                                                  'week', w.week_start)), '[]')
           FROM (SELECT region_code, min(week_start) lo, max(week_start) hi
                 FROM core.fact_diesel_weekly GROUP BY region_code) r
           JOIN (SELECT DISTINCT week_start FROM core.dim_date) w
                ON w.week_start BETWEEN r.lo AND r.hi
           LEFT JOIN core.fact_diesel_weekly f
                ON f.region_code = r.region_code AND f.week_start = w.week_start
           WHERE f.week_start IS NULL""",
    ),
    # ── out of range ─────────────────────────────────────────────────────────
    Check(
        "gas_price_out_of_range_30d",
        "fact_gas_price_daily",
        "warn",
        0,
        # Henry Hub has never printed below $0 and peaked at $30.72 (2026-01-23).
        f"""SELECT count(*), coalesce(jsonb_agg(jsonb_build_object('date', price_date,
                                                                   'price', price_usd_mmbtu)), '[]')
            FROM core.fact_gas_price_daily
            WHERE hub_id = {HENRY} AND price_date > current_date - 30
              AND price_usd_mmbtu NOT BETWEEN 0 AND 40""",
    ),
    Check(
        "gas_daily_jump_30d",
        "fact_gas_price_daily",
        "warn",
        0,
        # >50% day-over-day moves happen only in extreme cold snaps; worth a human look.
        f"""SELECT count(*) FILTER (WHERE abs(chg) > 0.5),
                   jsonb_build_object('max_abs_change', round(max(abs(chg)), 3))
            FROM (SELECT price_date,
                         price_usd_mmbtu / nullif(lag(price_usd_mmbtu) OVER w, 0) - 1 AS chg
                  FROM core.fact_gas_price_daily WHERE hub_id = {HENRY}
                  WINDOW w AS (ORDER BY price_date)) x
            WHERE price_date > current_date - 30""",
    ),
    Check(
        "diesel_price_out_of_range",
        "fact_diesel_weekly",
        "error",
        0,
        # 2010-2026 range is $1.86-$6.53/gal; outside $1-$10 is a unit or parsing error.
        """SELECT count(*), coalesce(jsonb_agg(jsonb_build_object('week', week_start,
                                                                  'region', region_code)), '[]')
           FROM core.fact_diesel_weekly WHERE price_usd_gal NOT BETWEEN 1 AND 10""",
    ),
    Check(
        "diesel_weekly_jump_8w",
        "fact_diesel_weekly",
        "warn",
        0,
        # 99.9th percentile of week-over-week moves is ~22%.
        """SELECT count(*) FILTER (WHERE abs(chg) > 0.2),
                  jsonb_build_object('max_abs_change', round(max(abs(chg)), 3))
           FROM (SELECT week_start,
                        price_usd_gal / lag(price_usd_gal) OVER w - 1 AS chg
                 FROM core.fact_diesel_weekly
                 WINDOW w AS (PARTITION BY region_code ORDER BY week_start)) x
           WHERE week_start > current_date - 56""",
    ),
    # ── ingestion health ─────────────────────────────────────────────────────
    Check(
        "ingest_duplicate_periods",
        "ops.ingest_run",
        "warn",
        0,
        # The PK makes duplicates impossible in the table; this catches them in the payload.
        """SELECT coalesce(sum(rows_duplicate), 0), coalesce(jsonb_object_agg(source,
                                                                           rows_duplicate), '{}')
           FROM (SELECT DISTINCT ON (source) source, rows_duplicate FROM ops.ingest_run
                 WHERE status = 'success' ORDER BY source, run_id DESC) latest""",
    ),
    Check(
        "ingest_latest_run_failed",
        "ops.ingest_run",
        "error",
        0,
        """SELECT count(*) FILTER (WHERE status = 'failed'),
                  coalesce(jsonb_object_agg(source, status), '{}')
           FROM (SELECT DISTINCT ON (source) source, status FROM ops.ingest_run
                 WHERE status <> 'running' ORDER BY source, run_id DESC) latest""",
    ),
    # ── routes ───────────────────────────────────────────────────────────────
    Check(
        "route_missing_pairs",
        "fact_route",
        "error",
        0,
        # Every site needs a route to every delivery hub, or netback silently drops options.
        """SELECT count(*), coalesce(jsonb_agg(s.site_name || ' -> ' || h.hub_code), '[]')
           FROM core.dim_site s CROSS JOIN core.dim_hub h
           LEFT JOIN core.fact_route r ON r.site_id = s.site_id AND r.hub_id = h.hub_id
           WHERE h.is_delivery_point AND r.site_id IS NULL""",
    ),
    Check(
        "route_site_far_from_road",
        "fact_route",
        "warn",
        0,
        # OSRM snaps each flare to the nearest mapped road. Lease roads are often unmapped, so
        # > 2 km means the routed distance understates the trip (cost model adds the gap back).
        """SELECT count(*), coalesce(jsonb_object_agg(site_name, snap_m), '{}')
           FROM (SELECT DISTINCT s.site_name, round(r.site_snap_m) AS snap_m
                 FROM core.fact_route r JOIN core.dim_site s USING (site_id)
                 WHERE r.site_snap_m > 2000) far""",
    ),
    Check(
        "route_detour_ratio_out_of_range",
        "fact_route",
        "error",
        0,
        # Road km / straight-line km. Below 1 means snapping moved a point a long way; above 2.5
        # suggests swapped lat/lon or a snap onto the wrong road. Observed range: 1.08-1.78.
        """SELECT count(*), coalesce(jsonb_agg(jsonb_build_object(
                      'pair', site_name || ' -> ' || hub_code, 'ratio', round(ratio, 2))), '[]')
           FROM (SELECT s.site_name, h.hub_code,
                        (r.distance_m / 1000 / core.haversine_km(s.lat, s.lon, h.lat, h.lon))
                            ::numeric AS ratio
                 FROM core.fact_route r JOIN core.dim_site s USING (site_id)
                 JOIN core.dim_hub h USING (hub_id)) x
           WHERE ratio NOT BETWEEN 1.0 AND 2.5""",
    ),
    # ── assumptions ──────────────────────────────────────────────────────────
    Check(
        "assumptions_incomplete",
        "mart.v_mode_economics",
        "error",
        0,
        # A key missing from assumptions.yaml makes the pivot return NULL, and every cost
        # downstream would silently become NULL too. For a row, `m IS NOT NULL` is true only
        # when every column is non-null (and the LEFT JOIN yields a NULL row if the view is empty).
        """SELECT count(*) FILTER (WHERE NOT (m IS NOT NULL)),
                  coalesce(jsonb_agg(m.mode) FILTER (WHERE NOT (m IS NOT NULL)), '[]')
           FROM (SELECT 1) one LEFT JOIN mart.v_mode_economics m ON true""",
    ),
)


@dataclass(frozen=True)
class Result:
    check: Check
    observed: float | None
    passed: bool
    details: dict


def run_checks(conn: psycopg.Connection, checks=CHECKS) -> list[Result]:
    results = []
    for check in checks:
        observed, details = conn.execute(check.sql).fetchone()
        # no data at all (observed NULL) is a failure, not a pass
        passed = observed is not None and float(observed) <= check.threshold
        results.append(Result(check, observed, passed, details or {}))
    return results


def record(conn: psycopg.Connection, results: list[Result]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO ops.dq_results
                   (check_name, target, severity, passed, observed, threshold, details)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [
                (r.check.name, r.check.target, r.check.severity, r.passed, r.observed,
                 r.check.threshold, Jsonb(r.details))
                for r in results
            ],
        )  # fmt: skip


def main() -> int:
    setup_logging()
    with connect() as conn:
        results = run_checks(conn)
        record(conn, results)
    for r in results:
        status = "PASS" if r.passed else ("FAIL" if r.check.severity == "error" else "WARN")
        log.info("%-4s %-32s observed=%-6s threshold=%s", status, r.check.name, r.observed,
                 r.check.threshold)  # fmt: skip
    failed = [r.check.name for r in results if not r.passed and r.check.severity == "error"]
    if failed:
        log.error("blocking DQ failures: %s", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
