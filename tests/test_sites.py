import pytest

from ingestion.sites import (
    BASINS,
    Flare,
    haversine_km,
    load_ring,
    point_in_polygon,
    select,
    state_of,
)

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
# A "C" shape: the notch (4..10, 4..6) is outside although it is inside the bounding box.
C_SHAPE = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 6), (10, 6), (10, 10), (0, 10)]


@pytest.mark.parametrize(
    "lon, lat, inside",
    [(5, 5, True), (11, 5, False), (-1, 5, False), (5, 11, False)],
)
def test_point_in_square(lon, lat, inside):
    assert point_in_polygon(lon, lat, SQUARE) is inside


def test_point_in_concave_polygon_respects_the_notch():
    assert point_in_polygon(2, 5, C_SHAPE)
    assert not point_in_polygon(7, 5, C_SHAPE)  # bounding box would wrongly say inside


def test_real_basin_boundaries():
    permian, eagle_ford = (load_ring(BASINS[b][0]) for b in ("Permian", "Eagle Ford"))
    midland = (-102.08, 31.99)  # Midland, TX: heart of the Permian
    houston = (-95.37, 29.76)
    assert point_in_polygon(*midland, permian) and not point_in_polygon(*houston, permian)
    assert point_in_polygon(-98.0, 28.8, eagle_ford) and not point_in_polygon(*midland, eagle_ford)


def test_haversine_one_degree_of_latitude():
    assert haversine_km(30, -100, 31, -100) == pytest.approx(111.19, abs=0.01)


def test_state_split_at_the_tx_nm_border():
    assert state_of(32.69, -103.29) == "NM"  # north of 32°N, west of 103.064°W
    assert state_of(31.90, -103.49) == "TX"  # Delaware Basin south of 32°N is Texas
    assert state_of(32.40, -101.28) == "TX"


def flare(fid, lon, lat, mmscfd_2025, years=(2023, 2024, 2025), field_type="OIL"):
    return Flare(fid, lat, lon, field_type, None, {y: mmscfd_2025 for y in years})


def test_select_applies_rule_spacing_and_order():
    flares = {f.flare_id: f for f in [
        flare("big", 5.0, 5.0, 1.5),
        flare("near_big", 5.01, 5.0, 1.4),               # ~1 km away: dropped by spacing
        flare("far", 8.0, 8.0, 0.5),
        flare("too_small", 2.0, 2.0, 0.05),
        flare("too_large", 2.0, 8.0, 3.0),
        flare("one_year_only", 3.0, 3.0, 1.0, years=(2025,)),
        flare("gas_field", 7.0, 2.0, 1.0, field_type="GAS"),
        flare("outside", 20.0, 20.0, 1.0),
    ]}  # fmt: skip
    sites = select(flares, "Permian", SQUARE, count=5, prefix="PB")
    assert [s.flare_id for s in sites] == ["big", "far"]
    assert [s.site_name for s in sites] == ["PB-01", "PB-02"]
