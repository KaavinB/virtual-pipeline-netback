import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ingestion.eia import SERIES, fetch, parse
from tests.fakes import FakeResponse, FakeSession

# A real EIA v2 response (Henry Hub, 2026-10-01..20), captured 2026-10-26.
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "eia_rngwhhd_2026-10.json").read_text())
HENRY = SERIES[0]


def test_parses_real_payload_strings_to_decimals():
    parsed = parse(FIXTURE["response"]["data"], "RNGWHHD")
    assert len(parsed.values) == 14
    assert parsed.values[date(2026, 10, 20)] == Decimal("3.1")
    assert parsed.skipped == parsed.duplicates == 0


def test_skips_null_values_like_2018_01_05():
    rows = [{"series": "RNGWHHD", "period": "2018-01-05", "value": None},
            {"series": "RNGWHHD", "period": "2018-01-08", "value": "NA"},
            {"series": "RNGWHHD", "period": "2018-01-09", "value": "NaN"},
            {"series": "RNGWHHD", "period": "2018-01-10", "value": "2.81"}]  # fmt: skip
    parsed = parse(rows, "RNGWHHD")
    assert parsed.skipped == 3
    assert list(parsed.values) == [date(2018, 1, 10)]


def test_counts_duplicate_periods_and_keeps_last():
    rows = [{"series": "RNGWHHD", "period": "2026-10-20", "value": "3.0"},
            {"series": "RNGWHHD", "period": "2026-10-20", "value": "3.1"}]  # fmt: skip
    parsed = parse(rows, "RNGWHHD")
    assert parsed.duplicates == 1
    assert parsed.values[date(2026, 10, 20)] == Decimal("3.1")


def test_rejects_wrong_series():
    with pytest.raises(ValueError, match="expected series RNGWHHD"):
        parse([{"series": "RNGC1", "period": "2026-10-20", "value": "3"}], "RNGWHHD")


def test_fetch_follows_offset_pagination():
    def page(n, total):
        return FakeResponse(200, {"response": {"total": str(total), "data": [{}] * n}})

    session = FakeSession(page(5000, 7488), page(2488, 7488))
    records = fetch(session, "k", HENRY, date(2010, 1, 1))
    assert len(records) == 7488
    assert [c["offset"] for c in session.calls] == [0, 5000]
    assert session.calls[0]["facets[series][]"] == "RNGWHHD"
