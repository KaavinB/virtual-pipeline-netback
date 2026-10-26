-- Assumptions live in assumptions.yaml (reviewed in git) and are loaded verbatim here.
-- Everything computed from them is a view, so the arithmetic is readable SQL, not Python.

CREATE TABLE core.assumption (
    key         text PRIMARY KEY,
    value       numeric,
    value_text  text,                      -- for non-numeric settings, e.g. diesel_region
    low         numeric,
    high        numeric,
    unit        text NOT NULL,
    basis       text NOT NULL CHECK (basis IN ('sourced', 'derived', 'rough_estimate')),
    usd_year    smallint,                  -- dollar year of a cost; NULL for non-money values
    source      text NOT NULL,
    url         text,
    note        text,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    CHECK ((value IS NULL) <> (value_text IS NULL)),
    CHECK (low IS NULL OR low <= value),
    CHECK (high IS NULL OR value <= high)
);

-- What-if overrides: a scenario sets the session setting `vpv.overrides` to a JSON object such
-- as {"fuel_discount_to_diesel": 0.5}, and every view below recomputes with those values.
-- The business logic stays in these views; scenarios and sensitivity only change inputs.
CREATE FUNCTION mart.overrides() RETURNS jsonb
LANGUAGE sql STABLE
RETURN coalesce(nullif(current_setting('vpv.overrides', true), '')::jsonb, '{}'::jsonb);

-- Assumptions in 2025 dollars. A 2015-dollar cost is scaled by CPI-U(2025) / CPI-U(2015).
-- An override is taken as already being in 2025 dollars (that is how a user thinks about it).
CREATE VIEW mart.v_assumption AS
SELECT a.key,
       CASE WHEN a.value IS NOT NULL   -- text settings (diesel_region) skip the numeric cast
            THEN coalesce((mart.overrides() ->> a.key)::numeric, a.value * f.factor) END AS value,
       a.low   * f.factor AS low,
       a.high  * f.factor AS high,
       CASE WHEN a.value_text IS NOT NULL
            THEN coalesce(mart.overrides() ->> a.key, a.value_text) END AS value_text,
       a.unit, a.basis, a.usd_year,
       a.value AS value_as_published,
       round(f.factor, 4) AS escalation_factor,
       a.source, a.url, a.note,
       mart.overrides() ? a.key AS is_overridden
FROM core.assumption a
CROSS JOIN LATERAL (
    SELECT CASE WHEN a.usd_year IS NULL OR a.usd_year = 2025 THEN 1
                ELSE (SELECT value FROM core.assumption WHERE key = 'cpi_u_2025')
                   / (SELECT value FROM core.assumption WHERE key = 'cpi_u_' || a.usd_year)
           END AS factor
) f;

-- One row per mode: how much energy a truckload delivers, and the per-MMBtu costs that do
-- not depend on distance. Payload is the smaller of what the tank holds (volume) and what the
-- truck may legally carry (80,000 lb gross minus tractor, chassis and empty container).
CREATE VIEW mart.v_mode_economics AS
WITH p AS (
    SELECT
        max(value) FILTER (WHERE key = 'gas_mmbtu_per_mcf')             AS mmbtu_per_mcf,
        max(value) FILTER (WHERE key = 'gross_vehicle_weight_limit_lb') AS gvw_lb,
        max(value) FILTER (WHERE key = 'tractor_and_chassis_lb')        AS tractor_lb,
        max(value) FILTER (WHERE key = 'cng_capacity_scf')              AS cng_scf,
        max(value) FILTER (WHERE key = 'cng_container_tare_lb')         AS cng_tare_lb,
        max(value) FILTER (WHERE key = 'cng_lb_per_scf')                AS cng_lb_per_scf,
        max(value) FILTER (WHERE key = 'cng_deliverable_fraction')      AS cng_deliverable,
        max(value) FILTER (WHERE key = 'lng_capacity_gal')              AS lng_gal,
        max(value) FILTER (WHERE key = 'lng_max_fill_fraction')         AS lng_fill,
        max(value) FILTER (WHERE key = 'lng_lb_per_gal')                AS lng_lb_per_gal,
        max(value) FILTER (WHERE key = 'lng_btu_per_lb')                AS lng_btu_per_lb,
        max(value) FILTER (WHERE key = 'lng_container_tare_lb')         AS lng_tare_lb,
        max(value) FILTER (WHERE key = 'lng_deliverable_fraction')      AS lng_deliverable,
        max(value) FILTER (WHERE key = 'cng_gas_treatment')             AS cng_treatment,
        max(value) FILTER (WHERE key = 'cng_compression')               AS cng_compression,
        max(value) FILTER (WHERE key = 'cng_delivery')                  AS cng_delivery,
        max(value) FILTER (WHERE key = 'cng_fixed_transport')           AS cng_fixed_transport,
        max(value) FILTER (WHERE key = 'lng_gas_treatment')             AS lng_treatment,
        max(value) FILTER (WHERE key = 'lng_liquefaction')              AS lng_liquefaction,
        max(value) FILTER (WHERE key = 'lng_delivery')                  AS lng_delivery,
        max(value) FILTER (WHERE key = 'lng_fixed_transport')           AS lng_fixed_transport,
        max(value) FILTER (WHERE key = 'marketing_fee')                 AS marketing_fee
    FROM mart.v_assumption
),
modes AS (
    SELECT 'CNG' AS mode,
           cng_scf / 1000 * mmbtu_per_mcf                                    AS volume_limit_mmbtu,
           (gvw_lb - tractor_lb - cng_tare_lb) / cng_lb_per_scf / 1000 * mmbtu_per_mcf
                                                                             AS weight_limit_mmbtu,
           cng_deliverable                                                   AS deliverable_fraction,
           cng_treatment + cng_compression                                   AS conditioning_usd_mmbtu,
           cng_delivery                                                      AS delivery_usd_mmbtu,
           cng_fixed_transport                                               AS fixed_transport_usd_mmbtu,
           marketing_fee
    FROM p
    UNION ALL
    SELECT 'LNG',
           lng_gal * lng_fill * lng_lb_per_gal * lng_btu_per_lb / 1e6,
           (gvw_lb - tractor_lb - lng_tare_lb) * lng_btu_per_lb / 1e6,
           lng_deliverable,
           lng_treatment + lng_liquefaction,
           lng_delivery,
           lng_fixed_transport,
           marketing_fee
    FROM p
)
SELECT mode,
       round(volume_limit_mmbtu, 1)                                          AS volume_limit_mmbtu,
       round(weight_limit_mmbtu, 1)                                          AS weight_limit_mmbtu,
       CASE WHEN weight_limit_mmbtu < volume_limit_mmbtu THEN 'weight' ELSE 'volume' END
                                                                             AS binding_limit,
       round(least(volume_limit_mmbtu, weight_limit_mmbtu) * deliverable_fraction, 1)
                                                                             AS net_mmbtu_per_load,
       round(conditioning_usd_mmbtu, 3)                                      AS conditioning_usd_mmbtu,
       round(delivery_usd_mmbtu, 3)                                          AS delivery_usd_mmbtu,
       round(fixed_transport_usd_mmbtu, 3)                                   AS fixed_transport_usd_mmbtu,
       round(marketing_fee, 3)                                               AS fee_usd_mmbtu
FROM modes;

-- Trucking cost for every route x mode x diesel week ($/MMBtu delivered).
--   round trip miles = 2 x (road miles + last-mile gap from flare to nearest mapped road)
--   $/mile           = diesel $/gal / mpg + non-fuel $/mile (ATRI)
--   $/MMBtu          = fixed part + round trip miles x $/mile / net MMBtu per load
CREATE VIEW mart.v_trucking_cost_weekly AS
WITH p AS (
    SELECT
        max(value)      FILTER (WHERE key = 'truck_mpg')            AS mpg,
        max(value)      FILTER (WHERE key = 'nonfuel_usd_per_mile') AS nonfuel_per_mile,
        max(value)      FILTER (WHERE key = 'last_mile_factor')     AS last_mile_factor,
        max(value_text) FILTER (WHERE key = 'diesel_region')        AS diesel_region
    FROM mart.v_assumption
),
legs AS (
    SELECT r.site_id, r.hub_id, r.distance_mi,
           r.site_snap_m / 1609.344 * p.last_mile_factor              AS last_mile_mi,
           2 * (r.distance_mi + r.site_snap_m / 1609.344 * p.last_mile_factor)
                                                                      AS round_trip_mi
    FROM core.fact_route r CROSS JOIN p
)
SELECT l.site_id, l.hub_id, m.mode, d.week_start,
       d.price_usd_gal                                                AS diesel_usd_gal,
       l.distance_mi,
       round(l.last_mile_mi, 2)                                       AS last_mile_mi,
       round(l.round_trip_mi, 1)                                      AS round_trip_mi,
       round(d.price_usd_gal / p.mpg + p.nonfuel_per_mile, 3)         AS usd_per_mile,
       round(l.round_trip_mi * (d.price_usd_gal / p.mpg + p.nonfuel_per_mile), 2)
                                                                      AS usd_per_load,
       round(m.fixed_transport_usd_mmbtu
             + l.round_trip_mi * (d.price_usd_gal / p.mpg + p.nonfuel_per_mile)
               / m.net_mmbtu_per_load, 4)                             AS trucking_usd_mmbtu
FROM legs l
CROSS JOIN p
CROSS JOIN mart.v_mode_economics m
JOIN core.fact_diesel_weekly d ON d.region_code = p.diesel_region;
