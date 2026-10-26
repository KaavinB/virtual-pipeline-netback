import json

import pytest

pytestmark = [pytest.mark.db, pytest.mark.data]


def site_latest(db, overrides=None):
    source = "mart.v_site_latest" if overrides is None else "mart.site_latest_scenario(%s::jsonb)"
    sql = f"SELECT site_name, best_hub_netback_usd_mmbtu, best_fuel_netback_usd_mmbtu FROM {source}"
    params = () if overrides is None else (json.dumps(overrides),)
    return {s: (float(h), float(f)) for s, h, f in db.execute(sql, params).fetchall()}


def test_empty_scenario_equals_the_base_views(db):
    assert site_latest(db, {}) == site_latest(db)


def test_henry_hub_shift_moves_hub_netback_one_for_one_and_not_fuel(db):
    base, shifted = site_latest(db), site_latest(db, {"henry_hub_delta": 1.0})
    for site, (hub, fuel) in base.items():
        assert shifted[site][0] == pytest.approx(hub + 1.0, abs=2e-3)
        assert shifted[site][1] == pytest.approx(fuel, abs=2e-3)


def test_override_does_not_leak_into_later_queries(db):
    base = site_latest(db)
    site_latest(db, {"fuel_discount_to_diesel": 0.9})
    assert site_latest(db) == base  # same transaction, override already cleared


def test_fuel_discount_override_matches_parity_arithmetic(db):
    (parity,) = db.execute(
        "SELECT DISTINCT diesel_parity_usd_mmbtu FROM mart.v_netback_daily"
        " WHERE price_date = (SELECT max(price_date) FROM core.fact_gas_price_daily)"
    ).fetchone()
    base, deeper = site_latest(db), site_latest(db, {"fuel_discount_to_diesel": 0.5})
    for site in base:  # 10 more points of discount = 10% of parity less revenue
        assert base[site][1] - deeper[site][1] == pytest.approx(0.1 * float(parity), abs=2e-3)


def test_distance_curve_crosses_zero_at_crossover_consistent_costs(db):
    (cross,) = db.execute(
        "SELECT crossover_one_way_mi FROM mart.v_mode_crossover WHERE market = 'hub'"
    ).fetchone()
    # At the crossover distance CNG and LNG cost the same, so their netbacks match.
    rows = dict(db.execute(
        "SELECT mode, netback_usd_mmbtu FROM mart.v_distance_curve"
        " WHERE market = 'hub' AND hub_code = 'WAHA' AND one_way_mi = %s",
        (round(float(cross), -1),),
    ).fetchall())  # fmt: skip
    assert float(rows["CNG"]) == pytest.approx(float(rows["LNG"]), abs=0.1)


def test_tornado_swing_is_the_spread_between_ends(db):
    bad = db.execute(
        "SELECT count(*) FROM mart.sensitivity_tornado"
        " WHERE abs(swing - abs(netback_at_high - netback_at_low)) > 1e-6"
    ).fetchone()[0]
    total = db.execute("SELECT count(*) FROM mart.sensitivity_tornado").fetchone()[0]
    assert total > 0 and bad == 0
