"""The static site's JavaScript must reproduce the SQL netback for every what-if it offers."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dashboards.site.export import build

pytestmark = [pytest.mark.db, pytest.mark.data]
RUNNER = Path(__file__).parent / "site_parity.mjs"
SCENARIOS = [
    {},
    {"henry_hub": 6.2},
    {"discount_pct": 70},
    {"diesel": 3.5, "customer_mi": 150},
    {"waha_discount": 0.5, "henry_hub": 4},
    {"henry_hub": 8, "diesel": 7, "discount_pct": 90, "waha_discount": 3, "customer_mi": 10},
]


@pytest.fixture(scope="module")
def js_results(database_has_data):
    # Module-scoped fixtures run before the function-level skip for `data` tests, so check here.
    if not database_has_data:
        pytest.skip("needs a populated database (make all)")
    if not shutil.which("node"):
        pytest.skip("node not installed")
    from ingestion.db import connect

    with connect() as conn:
        payload = json.dumps({"data": build(conn), "scenarios": SCENARIOS})
    done = subprocess.run(["node", str(RUNNER)], input=payload, capture_output=True, text=True,
                          check=True)  # fmt: skip
    return json.loads(done.stdout)


@pytest.mark.parametrize("i", range(len(SCENARIOS)))
def test_js_matches_sql_scenario(db, js_results, i):
    sql = db.execute(
        """SELECT site_name, best_hub_netback_usd_mmbtu, best_fuel_netback_usd_mmbtu,
                  best_option, best_hub_break_even_henry_hub, best_fuel_break_even_discount
           FROM mart.site_latest_scenario(mart.dashboard_overrides(%s::jsonb))""",
        (json.dumps(SCENARIOS[i]),),
    ).fetchall()
    js = js_results[i]["sites"]
    assert len(sql) == len(js) == 18
    for site, hub, fuel, best, be_hh, be_disc in sql:
        assert js[site]["hub"] == pytest.approx(float(hub), abs=2e-3), site
        assert js[site]["fuel"] == pytest.approx(float(fuel), abs=2e-3), site
        assert js[site]["best"] == best, site
        assert js[site]["be_hh"] == pytest.approx(float(be_hh), abs=2e-3), site
        assert js[site]["be_discount"] == pytest.approx(float(be_disc), abs=2e-3), site


def test_js_crossover_matches_sql(db, js_results):
    sql = dict(db.execute("SELECT market, crossover_one_way_mi FROM mart.v_mode_crossover"))
    for row in js_results[0]["crossover"]:
        assert row["miles"] == pytest.approx(float(sql[row["market"]]), abs=1)
