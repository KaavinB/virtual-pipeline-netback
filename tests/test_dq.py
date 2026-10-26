import pytest

from ingestion.dq import CHECKS, run_checks

pytestmark = pytest.mark.db


def results_by_name(db):
    return {r.check.name: r for r in run_checks(db)}


@pytest.mark.data
def test_stale_gas_feed_fails(db):
    # Inside the rolled-back test transaction: pretend the last price is 30 days old.
    db.execute("DELETE FROM core.fact_gas_price_daily WHERE price_date > current_date - 30")
    stale = results_by_name(db)["gas_feed_stale_days"]
    assert not stale.passed and stale.observed >= 30


@pytest.mark.data
def test_missing_diesel_week_is_reported(db):
    db.execute("DELETE FROM core.fact_diesel_weekly WHERE week_start = '2020-03-02'")
    missing = results_by_name(db)["diesel_missing_weeks"]
    assert missing.observed == 2  # both regions
    assert {"region": "US", "week": "2020-03-02"} in missing.details


def test_empty_table_fails_rather_than_passes(db):
    db.execute("DELETE FROM core.fact_gas_price_daily")
    assert not results_by_name(db)["gas_feed_stale_days"].passed


def test_check_names_are_unique():
    names = [c.name for c in CHECKS]
    assert len(names) == len(set(names))


@pytest.mark.data
def test_missing_assumption_is_caught(db):
    db.execute("DELETE FROM core.assumption WHERE key = 'lng_liquefaction'")
    check = results_by_name(db)["assumptions_incomplete"]
    assert not check.passed and check.details == ["LNG"]


@pytest.mark.data
def test_far_from_road_warning_names_the_sites(db):
    far = results_by_name(db)["route_site_far_from_road"]
    assert far.check.severity == "warn"
    assert set(far.details) == {"PB-04", "PB-11"}
