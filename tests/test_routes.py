import json
from pathlib import Path

import pytest

from ingestion.routes import parse_route

# Real OSRM demo-server response for PB-01 -> Waha, captured 2026-10-26.
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "osrm_route_pb01_waha.json").read_text())


def test_parse_real_route():
    r = parse_route(FIXTURE)
    assert 130_000 < r.distance_m < 140_000  # ~84 mi
    assert r.duration_s > 0
    assert r.site_snap_m > r.hub_snap_m  # the flare is off-road; the hub proxy is a town
    assert r.geojson["type"] == "LineString" and len(r.geojson["coordinates"]) > 2


def test_no_route_raises():
    with pytest.raises(ValueError, match="NoRoute"):
        parse_route({"code": "NoRoute", "message": "Impossible route", "routes": []})
