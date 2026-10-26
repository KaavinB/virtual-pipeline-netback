-- Simplified route line (GeoJSON LineString, lon/lat) so the dashboard map can draw the
-- actual road path instead of a straight line. Nullable: routing metrics don't depend on it.
ALTER TABLE core.fact_route ADD COLUMN route_geojson jsonb;
