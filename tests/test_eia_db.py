from datetime import date
from decimal import Decimal

import pytest

from ingestion.eia import SERIES, upsert

pytestmark = pytest.mark.db
HENRY, DIESEL_PADD3 = SERIES[0], SERIES[1]
# Far-future dates so tests never collide with real ingested history.
D1, D2 = date(2030, 6, 3), date(2030, 6, 4)


def test_upsert_is_idempotent_and_tracks_revisions(db):
    assert upsert(db, HENRY, {D1: Decimal("3.10"), D2: Decimal("3.20")}) == (2, 0)
    db.execute("DROP TABLE stage")  # ON COMMIT DROP; we roll back instead of committing
    assert upsert(db, HENRY, {D1: Decimal("3.10"), D2: Decimal("3.20")}) == (0, 0)
    db.execute("DROP TABLE stage")
    assert upsert(db, HENRY, {D1: Decimal("3.10"), D2: Decimal("3.25")}) == (0, 1)
    (price,) = db.execute(
        "SELECT price_usd_mmbtu FROM core.fact_gas_price_daily WHERE price_date = %s", (D2,)
    ).fetchone()
    assert price == Decimal("3.250")


def test_diesel_upsert_writes_region(db):
    assert upsert(db, DIESEL_PADD3, {date(2030, 6, 3): Decimal("5.903")}) == (1, 0)
    (region, source) = db.execute(
        "SELECT region_code, source_series FROM core.fact_diesel_weekly WHERE week_start = %s",
        (date(2030, 6, 3),),
    ).fetchone()
    assert (region, source) == ("PADD3", "EIA:EMD_EPD2D_PTE_R30_DPG")
