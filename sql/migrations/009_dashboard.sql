-- Serving objects for dashboards (Metabase reads these live; `make export` writes them to CSV).

-- Netback-vs-distance curves under a what-if scenario (same mechanism as site_latest_scenario).
CREATE FUNCTION mart.distance_curve_scenario(overrides jsonb DEFAULT '{}')
RETURNS SETOF mart.v_distance_curve
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    PERFORM set_config('vpv.overrides', overrides::text, true);
    RETURN QUERY SELECT * FROM mart.v_distance_curve;
    PERFORM set_config('vpv.overrides', '', true);
END $$;

-- Road geometry as ordered points: one row per vertex of each site -> hub route line.
-- BI tools draw a path by connecting points in `point_order` within each route.
CREATE VIEW mart.v_route_path AS
SELECT s.site_name, h.hub_code, s.site_name || ' → ' || h.hub_code AS route,
       pt.ord::int AS point_order,
       (pt.xy ->> 1)::numeric AS lat,
       (pt.xy ->> 0)::numeric AS lon
FROM core.fact_route r
JOIN core.dim_site s USING (site_id)
JOIN core.dim_hub h USING (hub_id)
CROSS JOIN LATERAL jsonb_array_elements(r.route_geojson -> 'coordinates') WITH ORDINALITY AS pt(xy, ord);
