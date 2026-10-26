"""Driving distance, time and road geometry from every site to every delivery hub (OSRM).

    python -m ingestion.routes            # route only pairs missing from core.fact_route
    python -m ingestion.routes --refresh  # re-route every pair

Results are cached in core.fact_route: road networks don't change week to week, and the public
OSRM demo server asks for at most 1 request per second. Distances are one way; the cost model
doubles them for the round trip.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass

import requests
from psycopg.types.json import Jsonb

from ingestion.config import get_settings, setup_logging
from ingestion.db import connect
from ingestion.http import get_json

log = logging.getLogger("routes")
MIN_SECONDS_BETWEEN_REQUESTS = 1.1  # OSRM demo-server policy: <= 1 request/second


@dataclass(frozen=True)
class Route:
    distance_m: float
    duration_s: float
    site_snap_m: float  # how far OSRM moved each endpoint to reach the road network
    hub_snap_m: float
    geojson: dict


def parse_route(body: dict) -> Route:
    if body.get("code") != "Ok" or not body.get("routes"):
        raise ValueError(f"OSRM returned no route: {body.get('code')} {body.get('message', '')}")
    best = body["routes"][0]
    origin, destination = body["waypoints"]
    return Route(best["distance"], best["duration"], origin["distance"], destination["distance"],
                 best["geometry"])  # fmt: skip


def fetch_route(session, base_url: str, origin: tuple, destination: tuple) -> Route:
    """origin / destination are (lat, lon); OSRM wants lon,lat."""
    coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
    body = get_json(session, f"{base_url}/route/v1/driving/{coords}",
                    {"overview": "simplified", "geometries": "geojson"})  # fmt: skip
    return parse_route(body)


PAIRS_SQL = """
    SELECT s.site_id, s.site_name, s.lat, s.lon, h.hub_id, h.hub_code, h.lat, h.lon
    FROM core.dim_site s
    CROSS JOIN core.dim_hub h
    WHERE h.is_delivery_point
      AND (%(refresh)s OR NOT EXISTS (SELECT 1 FROM core.fact_route r
                                      WHERE r.site_id = s.site_id AND r.hub_id = h.hub_id))
    ORDER BY s.site_id, h.hub_id"""

UPSERT_SQL = """
    INSERT INTO core.fact_route (site_id, hub_id, distance_m, duration_s, site_snap_m,
                                 hub_snap_m, router, route_geojson)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (site_id, hub_id) DO UPDATE SET
        distance_m = EXCLUDED.distance_m, duration_s = EXCLUDED.duration_s,
        site_snap_m = EXCLUDED.site_snap_m, hub_snap_m = EXCLUDED.hub_snap_m,
        router = EXCLUDED.router, route_geojson = EXCLUDED.route_geojson, routed_at = now()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="re-route pairs already cached")
    args = parser.parse_args(argv)
    setup_logging()

    base_url = get_settings().osrm_url
    router = "osrm-demo" if "project-osrm.org" in base_url else "osrm-local"
    with connect() as conn, requests.Session() as session:
        session.headers["User-Agent"] = "vpv-netback/0.1 (portfolio project)"
        pairs = conn.execute(PAIRS_SQL, {"refresh": args.refresh}).fetchall()
        log.info("%d site-hub pairs to route via %s", len(pairs), base_url)
        failures = 0
        for site_id, site_name, s_lat, s_lon, hub_id, hub_code, h_lat, h_lon in pairs:
            started = time.monotonic()
            try:
                r = fetch_route(session, base_url, (s_lat, s_lon), (h_lat, h_lon))
            except Exception as exc:  # one bad pair shouldn't lose the other 53
                failures += 1
                log.error("%s -> %s failed: %s", site_name, hub_code, exc)
            else:
                row = (site_id, hub_id, r.distance_m, r.duration_s, r.site_snap_m, r.hub_snap_m)
                conn.execute(UPSERT_SQL, (*row, router, Jsonb(r.geojson)))
                conn.commit()
                log.info("%s -> %-10s %6.1f mi  %5.2f h  snap %4.0f m", site_name, hub_code,
                         r.distance_m / 1609.344, r.duration_s / 3600, r.site_snap_m)  # fmt: skip
            if router == "osrm-demo":
                time.sleep(max(0.0, MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - started)))
        conn.execute("ANALYZE core.fact_route")  # keep planner estimates current
        conn.commit()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
