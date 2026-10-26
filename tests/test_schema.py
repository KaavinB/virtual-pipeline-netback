import psycopg
import pytest

pytestmark = pytest.mark.db

HENRY = "(SELECT hub_id FROM core.dim_hub WHERE hub_code = 'HENRY')"
WAHA = "(SELECT hub_id FROM core.dim_hub WHERE hub_code = 'WAHA')"


def test_negative_gas_price_is_allowed(db):
    # Waha has printed below $0; the schema must store it and leave judgment to DQ checks.
    db.execute(f"""
        INSERT INTO core.fact_gas_price_daily (price_date, hub_id, price_usd_mmbtu, source_series)
        VALUES ('2030-05-01', {HENRY}, -2.50, 'test')""")


def test_price_needs_a_known_date(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(f"""
            INSERT INTO core.fact_gas_price_daily
                   (price_date, hub_id, price_usd_mmbtu, source_series)
            VALUES ('1999-01-01', {HENRY}, 2.0, 'test')""")


def test_diesel_price_must_be_positive(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("""
            INSERT INTO core.fact_diesel_weekly
                   (week_start, region_code, price_usd_gal, source_series)
            VALUES ('2030-05-06', 'PADD3', 0, 'test')""")


def test_week_start_is_monday(db):
    (bad,) = db.execute(
        "SELECT count(*) FROM core.dim_date WHERE extract(isodow FROM week_start) <> 1"
    ).fetchone()
    assert bad == 0


def test_route_miles_are_derived_from_metres(db):
    db.execute("""
        INSERT INTO core.dim_site (flare_id, site_name, basin, state, lat, lon, flared_mmscfd,
                                   flare_year, years_flared_of_3, selection_rank)
        VALUES ('test', 'T-1', 'Permian', 'TX', 31.9, -103.4, 0.5, 2025, 3, 1)""")
    (miles,) = db.execute(f"""
        INSERT INTO core.fact_route (site_id, hub_id, distance_m, duration_s,
                                     site_snap_m, hub_snap_m, router)
        SELECT site_id, {WAHA}, 160934.4, 7200, 10, 10, 'test'
        FROM core.dim_site WHERE flare_id = 'test'
        RETURNING distance_mi""").fetchone()
    assert float(miles) == 100.0
