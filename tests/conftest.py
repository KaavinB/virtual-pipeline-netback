import psycopg
import pytest

from ingestion.db import connect


@pytest.fixture
def db():
    """A connection inside a transaction that is always rolled back: tests never leave data."""
    try:
        conn = connect()
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (make up)")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(scope="session")
def database_has_data() -> bool:
    """True once `make all` has loaded prices, sites, routes and assumptions."""
    try:
        with connect() as conn:
            (ready,) = conn.execute(
                """SELECT EXISTS (SELECT 1 FROM core.fact_gas_price_daily)
                      AND EXISTS (SELECT 1 FROM core.fact_route)
                      AND EXISTS (SELECT 1 FROM core.assumption)"""
            ).fetchone()
            return ready
    except (psycopg.OperationalError, psycopg.errors.UndefinedTable):
        return False


@pytest.fixture(autouse=True)
def _skip_data_tests_on_empty_database(request, database_has_data):
    # CI's push job runs against a freshly migrated, empty database; the scheduled job loads
    # real data and runs everything.
    if request.node.get_closest_marker("data") and not database_has_data:
        pytest.skip("needs a populated database (make all)")
