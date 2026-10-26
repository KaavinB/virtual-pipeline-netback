"""One-at-a-time sensitivity (tornado) of netback, written to mart.sensitivity_tornado.

    python -m ingestion.sensitivity

Each driver is moved to its low and then its high value, everything else at base, and the best
hub netback and best fuel-customer netback of every site are recomputed by the SQL views via
mart.site_latest_scenario(). No netback arithmetic happens in Python.

Ranges: assumptions use their low/high from assumptions.yaml; Henry Hub and diesel use the
10th-90th percentile of the trailing three years of observed prices (widened to include today's
price), applied as a shift from the latest observation.
"""

import json
import logging
import sys
from dataclasses import dataclass

import psycopg

from ingestion.config import setup_logging
from ingestion.db import connect

log = logging.getLogger("sensitivity")

LABELS = {
    "henry_hub": "Henry Hub price",
    "diesel": "Diesel price (PADD 3)",
    "basis_waha": "Waha basis",
    "basis_agua_dulce": "Agua Dulce basis",
    "basis_katy": "Katy basis",
    "fuel_discount_to_diesel": "Fuel discount to diesel",
    "fuel_customer_one_way_mi": "Distance to fuel customer",
    "route_distance_factor": "Route distance to hub (x)",
    "nonfuel_usd_per_mile": "Non-fuel trucking $/mile",
    "truck_mpg": "Truck fuel economy",
    "cng_capacity_scf": "CNG trailer capacity",
    "cng_deliverable_fraction": "CNG deliverable fraction",
    "lng_deliverable_fraction": "LNG deliverable fraction",
    "tractor_and_chassis_lb": "Tractor + chassis weight",
    "cng_compression": "CNG compression cost",
    "cng_gas_treatment": "CNG gas treatment cost",
    "cng_delivery": "CNG decompression cost",
    "cng_fixed_transport": "CNG fixed transport cost",
    "lng_liquefaction": "LNG liquefaction cost",
    "lng_gas_treatment": "LNG gas treatment cost",
    "lng_delivery": "LNG regas cost",
    "lng_delivery_as_fuel": "LNG fuel hand-off cost",
    "lng_fixed_transport": "LNG fixed transport cost",
    "marketing_fee": "Marketing fee",
    "last_mile_factor": "Last-mile detour factor",
}

MARKET_COLUMN = {"hub": "best_hub_netback_usd_mmbtu", "fuel": "best_fuel_netback_usd_mmbtu"}


@dataclass(frozen=True)
class Driver:
    key: str  # override key passed to the scenario
    name: str  # name stored in the table
    unit: str
    base: float | None
    low: float  # override value at the low end
    high: float
    low_shown: float  # what the dashboard shows as the low / high input
    high_shown: float


def assumption_drivers(conn: psycopg.Connection) -> list[Driver]:
    rows = conn.execute(
        """SELECT key, unit, value, low, high FROM mart.v_assumption
           WHERE low IS NOT NULL AND high IS NOT NULL AND low <> high ORDER BY key"""
    ).fetchall()
    return [Driver(k, k, u, float(v), float(lo), float(hi), float(lo), float(hi))
            for k, u, v, lo, hi in rows]  # fmt: skip


def price_drivers(conn: psycopg.Connection) -> list[Driver]:
    """Henry Hub and diesel: P10-P90 of the last three years, as shifts from the latest price."""
    (hh_now, hh_p10, hh_p90) = conn.execute(
        """WITH h AS (SELECT price_date, price_usd_mmbtu FROM core.fact_gas_price_daily g
                      JOIN core.dim_hub USING (hub_id) WHERE hub_code = 'HENRY')
           SELECT (SELECT price_usd_mmbtu FROM h ORDER BY price_date DESC LIMIT 1),
                  percentile_cont(0.1) WITHIN GROUP (ORDER BY price_usd_mmbtu),
                  percentile_cont(0.9) WITHIN GROUP (ORDER BY price_usd_mmbtu)
           FROM h WHERE price_date > (SELECT max(price_date) FROM h) - interval '3 years'"""
    ).fetchone()
    (d_now, d_p10, d_p90) = conn.execute(
        """WITH d AS (SELECT week_start, price_usd_gal FROM core.fact_diesel_weekly
                      WHERE region_code = (SELECT value_text FROM core.assumption
                                           WHERE key = 'diesel_region'))
           SELECT (SELECT price_usd_gal FROM d ORDER BY week_start DESC LIMIT 1),
                  percentile_cont(0.1) WITHIN GROUP (ORDER BY price_usd_gal),
                  percentile_cont(0.9) WITHIN GROUP (ORDER BY price_usd_gal)
           FROM d WHERE week_start > (SELECT max(week_start) FROM d) - interval '3 years'"""
    ).fetchone()
    hh_now, d_now = float(hh_now), float(d_now)
    # Widen to include today's price: diesel in late 2026 sits above its own 3-year P90.
    hh_p10, hh_p90 = min(hh_p10, hh_now), max(hh_p90, hh_now)
    d_p10, d_p90 = min(d_p10, d_now), max(d_p90, d_now)
    return [
        Driver("henry_hub_delta", "henry_hub", "usd_per_mmbtu", hh_now,
               hh_p10 - hh_now, hh_p90 - hh_now, hh_p10, hh_p90),
        Driver("diesel_delta", "diesel", "usd_per_gal", d_now,
               d_p10 - d_now, d_p90 - d_now, d_p10, d_p90),
    ]  # fmt: skip


def scenario(conn: psycopg.Connection, overrides: dict) -> dict[str, tuple[str, dict]]:
    """site_name -> (basin, {market: netback}) under the given overrides."""
    rows = conn.execute(
        "SELECT site_name, basin, best_hub_netback_usd_mmbtu, best_fuel_netback_usd_mmbtu"
        " FROM mart.site_latest_scenario(%s::jsonb)",
        (json.dumps(overrides),),
    ).fetchall()
    return {s: (b, {"hub": float(h), "fuel": float(f)}) for s, b, h, f in rows}


def main() -> int:
    setup_logging()
    with connect() as conn:
        drivers = price_drivers(conn) + assumption_drivers(conn)
        base = scenario(conn, {})
        rows = []
        for d in drivers:
            low, high = scenario(conn, {d.key: d.low}), scenario(conn, {d.key: d.high})
            for site, (basin, base_nb) in base.items():
                for market in ("hub", "fuel"):
                    b, lo, hi = base_nb[market], low[site][1][market], high[site][1][market]
                    if abs(hi - lo) < 5e-4:
                        continue  # driver does not touch this market (e.g. basis on fuel sales)
                    flips = (lo > 0) != (b > 0) or (hi > 0) != (b > 0)
                    label = LABELS.get(d.name, d.name)
                    inputs = (d.base, d.low_shown, d.high_shown)
                    rows.append((site, basin, market, d.name, label, d.unit, *inputs,
                                 b, lo, hi, abs(hi - lo), flips))  # fmt: skip
            log.info("%-26s low=%-10.4g high=%-10.4g", d.name, d.low_shown, d.high_shown)
        conn.execute("TRUNCATE mart.sensitivity_tornado")
        conn.cursor().executemany(
            """INSERT INTO mart.sensitivity_tornado
                   (site_name, basin, market, driver, driver_label, unit, base_input, low_input,
                    high_input, base_netback, netback_at_low, netback_at_high, swing,
                    flips_decision)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            rows,
        )
    log.info("mart.sensitivity_tornado: %d rows (%d drivers)", len(rows), len(drivers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
