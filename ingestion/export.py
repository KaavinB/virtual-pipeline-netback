"""Write the dashboard extracts (Tableau Public reads files, not databases).

    python -m ingestion.export        # -> dashboards/exports/*.csv

Every extract is a plain SELECT from the mart views, so the files carry exactly the numbers the
SQL produces; nothing is recomputed here.
"""

import logging
import sys

from ingestion.config import ROOT, setup_logging
from ingestion.db import connect

log = logging.getLogger("export")
EXPORT_DIR = ROOT / "dashboards" / "exports"
HISTORY_FROM = "2021-01-01"  # five years keeps the netback extract ~200k rows

EXTRACTS = {
    "sites": "SELECT * FROM mart.v_site_latest ORDER BY site_id",
    "hubs": """SELECT hub_code, hub_name, region, lat, lon, location_note
               FROM core.dim_hub WHERE is_delivery_point ORDER BY hub_id""",
    "route_path": "SELECT * FROM mart.v_route_path ORDER BY route, point_order",
    "netback_daily": f"""
        SELECT price_date, site_name, basin, state, market, hub_code, mode, option_label,
               henry_hub_usd_mmbtu, basis_usd_mmbtu, diesel_usd_gal, diesel_parity_usd_mmbtu,
               delivered_price_usd_mmbtu, conditioning_usd_mmbtu, trucking_usd_mmbtu,
               delivery_usd_mmbtu, fee_usd_mmbtu, total_cost_usd_mmbtu, netback_usd_mmbtu,
               beats_flaring, is_best_option, one_way_mi, break_even_one_way_mi,
               break_even_henry_hub, break_even_fuel_discount, annual_value_vs_flaring_usd
        FROM mart.v_netback_daily WHERE price_date >= '{HISTORY_FROM}'
        ORDER BY price_date, site_name, option_label""",
    "distance_curve": "SELECT * FROM mart.v_distance_curve ORDER BY option_label, one_way_mi",
    "mode_crossover": "SELECT * FROM mart.v_mode_crossover",
    "tornado": """SELECT * FROM mart.sensitivity_tornado
                  ORDER BY site_name, market, swing DESC""",
    "assumptions": """SELECT key, value, low, high, value_text, unit, basis, usd_year,
                             value_as_published, escalation_factor, source, url, note
                      FROM mart.v_assumption ORDER BY key""",
    "dq_latest": """SELECT check_name, target, severity, passed, observed, threshold, details,
                           checked_at FROM ops.v_dq_latest ORDER BY check_name""",
}


def main() -> int:
    setup_logging()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        for name, sql in EXTRACTS.items():
            path = EXPORT_DIR / f"{name}.csv"
            with (
                path.open("wb") as fh,
                conn.cursor().copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)") as copy,
            ):
                for chunk in copy:
                    fh.write(chunk)
            rows = sum(1 for _ in path.open()) - 1
            log.info("%-15s %7d rows  %6.1f KB", name, rows, path.stat().st_size / 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
