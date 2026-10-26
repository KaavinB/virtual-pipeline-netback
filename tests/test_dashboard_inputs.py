"""The dashboard's plain-language inputs must translate exactly into model overrides."""

import json

import pytest

pytestmark = [pytest.mark.db, pytest.mark.data]


def overrides(db, inputs: dict) -> dict:
    sql = "SELECT mart.dashboard_overrides(%s::jsonb)"
    (result,) = db.execute(sql, (json.dumps(inputs),)).fetchone()
    return result


def test_blank_inputs_mean_base_case(db):
    assert overrides(db, {}) == {}


def test_absolute_prices_become_shifts_from_the_latest_observation(db):
    (latest_hh,) = db.execute(
        "SELECT price_usd_mmbtu FROM core.fact_gas_price_daily ORDER BY price_date DESC LIMIT 1"
    ).fetchone()
    o = overrides(db, {"henry_hub": 6, "discount_pct": 70, "waha_discount": 1.5})
    assert o["henry_hub_delta"] == pytest.approx(6 - float(latest_hh))
    assert o["fuel_discount_to_diesel"] == pytest.approx(0.70)
    assert o["basis_waha"] == pytest.approx(-1.5)  # a discount below Henry Hub is negative basis


def test_inputs_in_use_flags_what_the_user_changed(db):
    rows = db.execute(
        "SELECT input, value, source FROM mart.dashboard_inputs_in_use(%s::jsonb)",
        (json.dumps({"henry_hub": 6}),),
    ).fetchall()
    assert len(rows) == 5
    assert rows[0] == ("Henry Hub price", "$6.00 /MMBtu", "Your what-if")
    assert all(source != "Your what-if" for _, _, source in rows[1:])
