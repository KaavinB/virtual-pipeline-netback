"""Export everything the static (GitHub Pages) dashboard needs to site/data.json.

    python -m dashboards.site.export     # or: make site

GitHub Pages serves files, not databases, so the browser gets the SQL views' outputs plus the
per-route inputs to the netback formula (fixed $/MMBtu, MMBtu per load, miles). site/netback.js
only does the final arithmetic for what-if inputs, and tests/test_site_parity.py checks it
against mart.site_latest_scenario() for a set of scenarios.
"""

import json
import logging
import sys
from datetime import UTC, date, datetime
from decimal import Decimal

from ingestion.config import ROOT, setup_logging
from ingestion.db import connect

log = logging.getLogger("site")
OUT = ROOT / "site" / "data.json"
HISTORY_FROM = "2021-01-01"


def rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [c.name for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def plain(value):
    """JSON-friendly: Decimal -> float, date -> ISO string."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def build(conn) -> dict:
    a = {r["key"]: r for r in rows(conn, "SELECT key, value, value_text FROM mart.v_assumption")}
    prices = rows(
        conn,
        """SELECT price_date, henry_hub_usd_mmbtu, diesel_week, diesel_usd_gal
                           FROM mart.v_prices_daily ORDER BY price_date DESC LIMIT 1""",
    )[0]
    lhv_ratio = a["gas_lhv_btu_per_lb"]["value"] / a["gas_hhv_btu_per_lb"]["value"]

    legs = rows(
        conn,
        """SELECT s.site_name, l.market, l.hub_code, l.mode, l.one_way_mi,
                                l.last_mile_mi
                         FROM mart.v_legs l JOIN core.dim_site s USING (site_id)""",
    )
    sites = rows(
        conn,
        """SELECT site_id, site_name, basin, state, lat, lon, field_name,
                                 flared_mmscfd FROM core.dim_site ORDER BY site_id""",
    )
    for s in sites:
        mine = [leg for leg in legs if leg["site_name"] == s["site_name"]]
        s["last_mile_mi"] = mine[0]["last_mile_mi"]
        s["hub_miles"] = {leg["hub_code"]: leg["one_way_mi"] for leg in mine
                          if leg["market"] == "hub" and leg["mode"] == "CNG"}  # fmt: skip

    route_points = rows(conn, "SELECT site_name, hub_code, lat, lon FROM mart.v_route_path "
                              "ORDER BY site_name, hub_code, point_order")  # fmt: skip
    routes: dict[str, list] = {}
    for p in route_points:
        routes.setdefault(f"{p['site_name']}|{p['hub_code']}", []).append(
            [round(float(p["lat"]), 4), round(float(p["lon"]), 4)]
        )

    tornado = rows(
        conn,
        """SELECT site_name, market, driver_label, unit, low_input, high_input,
                                   base_input, netback_at_low - base_netback AS low_delta,
                                   netback_at_high - base_netback AS high_delta, swing
                            FROM mart.sensitivity_tornado ORDER BY site_name, market, swing DESC""",
    )

    return plain({
        "generated_at": datetime.now(UTC).replace(microsecond=0),
        "as_of": prices["price_date"],
        "latest": {"henry_hub": prices["henry_hub_usd_mmbtu"], "diesel": prices["diesel_usd_gal"],
                   "diesel_week": prices["diesel_week"]},
        "constants": {
            "mpg": a["truck_mpg"]["value"],
            "nonfuel_usd_per_mile": a["nonfuel_usd_per_mile"]["value"],
            "mmbtu_per_mcf": a["gas_mmbtu_per_mcf"]["value"],
            # diesel $/gal divided by this = diesel parity per HHV MMBtu of gas (LHV-adjusted)
            "parity_mmbtu_per_gal": a["diesel_lhv_btu_per_gal"]["value"] / lhv_ratio / 1_000_000,
        },
        "base": {"discount_pct": 100 * a["fuel_discount_to_diesel"]["value"],
                 "waha_discount": -a["basis_waha"]["value"],
                 "customer_mi": a["fuel_customer_one_way_mi"]["value"]},
        "basis": {"WAHA": a["basis_waha"]["value"], "AGUA_DULCE": a["basis_agua_dulce"]["value"],
                  "KATY": a["basis_katy"]["value"]},
        "modes": rows(conn, "SELECT * FROM mart.v_mode_economics ORDER BY mode"),
        "crossover": rows(conn, "SELECT * FROM mart.v_mode_crossover"),
        "sites": sites,
        "hubs": rows(conn, """SELECT hub_code, hub_name, lat, lon FROM core.dim_hub
                              WHERE is_delivery_point ORDER BY hub_id"""),
        "routes": routes,
        "prices_weekly": rows(conn, f"""
            SELECT date_trunc('week', price_date)::date AS week,
                   round(avg(henry_hub_usd_mmbtu), 2) AS henry_hub,
                   round(avg(diesel_parity_usd_mmbtu), 2) AS diesel_parity
            FROM mart.v_prices_daily WHERE price_date >= '{HISTORY_FROM}'
            GROUP BY 1 ORDER BY 1"""),
        "history_weekly": rows(conn, f"""
            WITH best AS (
                SELECT price_date, site_id, market, max(netback_usd_mmbtu) AS netback
                FROM mart.v_netback_daily WHERE price_date >= '{HISTORY_FROM}' GROUP BY 1, 2, 3)
            SELECT date_trunc('week', price_date)::date AS week,
                   round(avg(netback) FILTER (WHERE market = 'fuel'), 2) AS fuel,
                   round(avg(netback) FILTER (WHERE market = 'hub'), 2) AS hub
            FROM best GROUP BY 1 ORDER BY 1"""),
        "tornado": tornado,
        "assumptions": rows(conn, """SELECT key, value, value_text, unit, basis, source, url
                                     FROM mart.v_assumption
                                     ORDER BY (basis = 'rough_estimate') DESC, key"""),
        "dq": rows(conn, """SELECT check_name, severity, passed, observed, threshold, checked_at
                            FROM ops.v_dq_latest ORDER BY passed, check_name"""),
    })  # fmt: skip


def main() -> int:
    setup_logging()
    with connect() as conn:
        data = build(conn)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    log.info("wrote %s (%.0f KB, prices as of %s)", OUT.relative_to(ROOT),
             OUT.stat().st_size / 1024, data["as_of"])  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
