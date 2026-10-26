-- Great-circle distance in km. Used by DQ to compare road distance with straight-line distance
-- (a road route shorter than the straight line, or wildly longer, means bad coordinates).
CREATE FUNCTION core.haversine_km(lat1 float8, lon1 float8, lat2 float8, lon2 float8)
RETURNS float8
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN 2 * 6371.0 * asin(sqrt(
           sin(radians(lat2 - lat1) / 2) ^ 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ^ 2));
