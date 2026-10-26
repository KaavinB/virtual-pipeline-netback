"""Check v_netback_daily against the formulas in docs/01, computed independently in Python."""

from datetime import date

import pytest

from ingestion.assumptions import load

pytestmark = [pytest.mark.db, pytest.mark.data]
A = {e["key"]: e["value"] for e in load()}
DAY = date(2026, 10, 20)  # a Tuesday; its diesel week starts Monday 2026-10-19


def rows(db, where="", params=()):
    cur = db.execute(f"SELECT * FROM mart.v_netback_daily WHERE price_date = %s {where}",
                     (DAY, *params))  # fmt: skip
    names = [c.name for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def test_eight_options_per_site_and_exactly_one_best(db):
    out = rows(db)
    assert len(out) == 18 * 8
    best = [r for r in out if r["is_best_option"]]
    assert len(best) == 18 and len({r["site_id"] for r in best}) == 18


def test_hub_price_is_henry_hub_plus_basis(db):
    (r,) = rows(db, "AND site_name = 'PB-07' AND option_label = 'CNG → Waha'")
    assert float(r["delivered_price_usd_mmbtu"]) == pytest.approx(
        float(r["henry_hub_usd_mmbtu"]) + A["basis_waha"], abs=1e-3
    )


def test_diesel_as_of_join_uses_that_weeks_survey(db):
    (week_price,) = db.execute(
        "SELECT price_usd_gal FROM core.fact_diesel_weekly"
        " WHERE region_code = 'PADD3' AND week_start = '2026-10-19'"
    ).fetchone()
    assert {r["diesel_usd_gal"] for r in rows(db)} == {week_price}


def test_fuel_price_is_lhv_diesel_parity_less_discount(db):
    (r,) = rows(db, "AND site_name = 'EF-02' AND option_label = 'CNG → fuel customer'")
    lhv_over_hhv = A["gas_lhv_btu_per_lb"] / A["gas_hhv_btu_per_lb"]
    parity = float(r["diesel_usd_gal"]) / (A["diesel_lhv_btu_per_gal"] / lhv_over_hhv / 1e6)
    assert float(r["diesel_parity_usd_mmbtu"]) == pytest.approx(parity, abs=1e-3)
    assert float(r["delivered_price_usd_mmbtu"]) == pytest.approx(
        parity * (1 - A["fuel_discount_to_diesel"]), abs=1e-3
    )


def test_netback_is_price_minus_every_cost(db):
    for r in rows(db):
        costs = sum(float(r[c]) for c in ("conditioning_usd_mmbtu", "trucking_usd_mmbtu",
                                          "delivery_usd_mmbtu", "fee_usd_mmbtu"))  # fmt: skip
        assert float(r["netback_usd_mmbtu"]) == pytest.approx(
            float(r["delivered_price_usd_mmbtu"]) - costs, abs=2e-3
        )
        assert r["beats_flaring"] == (r["netback_usd_mmbtu"] > 0)


def test_break_even_henry_hub_zeroes_the_hub_netback(db):
    for r in rows(db, "AND market = 'hub'"):
        shortfall = float(r["break_even_henry_hub"]) - float(r["henry_hub_usd_mmbtu"])
        assert float(r["netback_usd_mmbtu"]) == pytest.approx(-shortfall, abs=2e-3)


def test_break_even_distance_zeroes_the_netback(db):
    """Re-price trucking at the break-even distance: netback must come out ~0."""
    (r,) = rows(db, "AND site_name = 'PB-01' AND option_label = 'CNG → fuel customer'")
    (net_load, fixed) = db.execute(
        "SELECT net_mmbtu_per_load, fixed_transport_usd_mmbtu FROM mart.v_mode_economics"
        " WHERE mode = 'CNG'"
    ).fetchone()
    per_mile = float(r["diesel_usd_gal"]) / A["truck_mpg"] + A["nonfuel_usd_per_mile"]
    (last_mile,) = db.execute(
        "SELECT max(site_snap_m) / 1609.344 FROM core.fact_route WHERE site_id = %s",
        (r["site_id"],),
    ).fetchone()
    miles = float(r["break_even_one_way_mi"]) + float(last_mile) * A["last_mile_factor"]
    trucking = float(fixed) + 2 * miles * per_mile / float(net_load)
    other = float(r["total_cost_usd_mmbtu"]) - float(r["trucking_usd_mmbtu"])
    assert float(r["delivered_price_usd_mmbtu"]) - other - trucking == pytest.approx(0, abs=0.01)


def test_site_latest_has_one_row_per_site(db):
    (n, distinct) = db.execute(
        "SELECT count(*), count(DISTINCT site_id) FROM mart.v_site_latest"
    ).fetchone()
    assert n == distinct == 18
