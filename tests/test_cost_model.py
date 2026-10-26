"""Re-derive the SQL views' numbers by hand from assumptions.yaml."""

import pytest

from ingestion.assumptions import load

pytestmark = [pytest.mark.db, pytest.mark.data]
A = {e["key"]: e["value"] for e in load()}
CPI = A["cpi_u_2025"] / A["cpi_u_2015"]


def mode_row(db, mode):
    cur = db.execute("SELECT * FROM mart.v_mode_economics WHERE mode = %s", (mode,))
    return dict(zip([c.name for c in cur.description], cur.fetchone(), strict=True))


def test_cng_payload_is_volume_limited(db):
    row = mode_row(db, "CNG")
    volume = A["cng_capacity_scf"] / 1000 * A["gas_mmbtu_per_mcf"]
    assert float(row["volume_limit_mmbtu"]) == pytest.approx(volume, abs=0.05)
    assert row["binding_limit"] == "volume"
    assert float(row["net_mmbtu_per_load"]) == pytest.approx(volume * 0.90, abs=0.05)


def test_lng_payload_is_weight_limited_at_80000_lb(db):
    row = mode_row(db, "LNG")
    payload_lb = 80000 - A["tractor_and_chassis_lb"] - A["lng_container_tare_lb"]
    assert float(row["weight_limit_mmbtu"]) == pytest.approx(payload_lb * 23726 / 1e6, abs=0.05)
    assert row["binding_limit"] == "weight"


def test_2015_dollar_costs_are_escalated_by_cpi(db):
    row = mode_row(db, "LNG")
    expected = (A["lng_gas_treatment"] + A["lng_liquefaction"]) * CPI
    assert float(row["conditioning_usd_mmbtu"]) == pytest.approx(expected, abs=0.001)


def test_trucking_cost_formula(db):
    (rt_mi, diesel, cost, net, fixed) = db.execute("""
        SELECT t.round_trip_mi, t.diesel_usd_gal, t.trucking_usd_mmbtu,
               m.net_mmbtu_per_load, m.fixed_transport_usd_mmbtu
        FROM mart.v_trucking_cost_weekly t JOIN mart.v_mode_economics m USING (mode)
        WHERE t.mode = 'CNG' ORDER BY t.week_start DESC, t.site_id, t.hub_id LIMIT 1""").fetchone()
    per_mile = float(diesel) / A["truck_mpg"] + A["nonfuel_usd_per_mile"]
    expected = float(fixed) + float(rt_mi) * per_mile / float(net)
    assert float(cost) == pytest.approx(expected, abs=0.001)
