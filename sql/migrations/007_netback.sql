-- Netback: what the producer nets per MMBtu at the wellsite, for every site, day and option.
--
-- Options per site (8):
--   hub  x {CNG, LNG} x {Waha, Agua Dulce, Katy}: decompress or regasify into a pipeline, sold at
--        Henry Hub + basis
--   fuel x {CNG, LNG}: delivered to an in-basin customer as a diesel substitute, at diesel parity
--        (LHV basis) less a discount
-- The alternative is flaring, which earns $0, so `netback > 0` means "trucking beats flaring".

-- v_mode_economics gains the delivery cost when gas is sold as fuel: CNG is let down into the
-- customer's fuel train (same as pipeline delivery); LNG is handed over as liquid, with no regas.
-- Views are dropped and recreated (not altered) so each definition reads top to bottom.
-- v_trucking_cost_weekly is rebuilt on top of v_legs + one shared trucking function.
DROP VIEW mart.v_trucking_cost_weekly;
DROP VIEW mart.v_mode_economics;

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
        max(value) FILTER (WHERE key = 'lng_delivery_as_fuel')          AS lng_delivery_as_fuel,
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
           cng_delivery                                                      AS delivery_as_fuel_usd_mmbtu,
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
           lng_delivery_as_fuel,
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
       round(delivery_as_fuel_usd_mmbtu, 3)                                  AS delivery_as_fuel_usd_mmbtu,
       round(fixed_transport_usd_mmbtu, 3)                                   AS fixed_transport_usd_mmbtu,
       round(marketing_fee, 3)                                               AS fee_usd_mmbtu
FROM modes;

-- The trucking formula, defined once and used by both views below (see docs/01):
--   $/MMBtu = fixed part + round-trip miles x (diesel $/gal / mpg + non-fuel $/mile) / MMBtu per load
CREATE FUNCTION mart.trucking_usd_mmbtu(fixed_usd_mmbtu numeric, round_trip_mi numeric,
                                        diesel_usd_gal numeric, mpg numeric,
                                        nonfuel_usd_per_mile numeric, net_mmbtu_per_load numeric)
RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN fixed_usd_mmbtu
       + round_trip_mi * (diesel_usd_gal / mpg + nonfuel_usd_per_mile) / net_mmbtu_per_load;

-- Diesel parity per HHV MMBtu of gas. Engines burn on LHV but gas is sold on HHV: a gallon of
-- diesel does the work of diesel_lhv / (gas LHV/HHV) Btu of gas as sold.
CREATE FUNCTION mart.diesel_parity_usd_mmbtu(diesel_usd_gal numeric, diesel_lhv_btu_gal numeric,
                                             gas_lhv_btu_lb numeric, gas_hhv_btu_lb numeric)
RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN diesel_usd_gal / (diesel_lhv_btu_gal / (gas_lhv_btu_lb / gas_hhv_btu_lb) / 1e6);

-- Every way a site's gas can leave: (site, market, hub, mode) with its distance and load economics.
-- Hub legs use routed OSRM miles; the fuel leg uses the assumed in-basin customer distance.
-- Both add the flare-to-road last mile. 18 sites x (3 hubs + 1 fuel customer) x 2 modes = 144 rows.
CREATE VIEW mart.v_legs AS
WITH p AS (
    SELECT
        max(value) FILTER (WHERE key = 'truck_mpg')                AS mpg,
        max(value) FILTER (WHERE key = 'nonfuel_usd_per_mile')     AS nonfuel_usd_per_mile,
        max(value) FILTER (WHERE key = 'last_mile_factor')         AS last_mile_factor,
        max(value) FILTER (WHERE key = 'fuel_customer_one_way_mi') AS fuel_customer_mi,
        max(value) FILTER (WHERE key = 'route_distance_factor')    AS route_distance_factor
    FROM mart.v_assumption
),
site_snap AS (  -- the flare's distance to the road network is the same whatever the destination
    SELECT site_id, max(site_snap_m) AS site_snap_m FROM core.fact_route GROUP BY site_id
),
destinations AS (
    SELECT r.site_id, 'hub' AS market, r.hub_id, r.distance_mi * p.route_distance_factor AS one_way_mi
    FROM core.fact_route r CROSS JOIN p
    UNION ALL
    SELECT s.site_id, 'fuel', NULL, p.fuel_customer_mi
    FROM site_snap s CROSS JOIN p
)
SELECT d.site_id, d.market, d.hub_id, h.hub_code, m.mode,
       d.one_way_mi,
       s.site_snap_m / 1609.344 * p.last_mile_factor                  AS last_mile_mi,
       2 * (d.one_way_mi + s.site_snap_m / 1609.344 * p.last_mile_factor) AS round_trip_mi,
       m.net_mmbtu_per_load, m.fixed_transport_usd_mmbtu, m.conditioning_usd_mmbtu,
       CASE d.market WHEN 'hub' THEN m.delivery_usd_mmbtu
                     ELSE m.delivery_as_fuel_usd_mmbtu END            AS delivery_usd_mmbtu,
       m.fee_usd_mmbtu, p.mpg, p.nonfuel_usd_per_mile
FROM destinations d
JOIN site_snap s USING (site_id)
LEFT JOIN core.dim_hub h ON h.hub_id = d.hub_id
CROSS JOIN p
CROSS JOIN mart.v_mode_economics m;

-- Trucking cost per leg and diesel week, for charts of cost over time.
CREATE VIEW mart.v_trucking_cost_weekly AS
SELECT l.site_id, l.market, l.hub_id, l.hub_code, l.mode, w.week_start,
       w.diesel_usd_gal,
       l.one_way_mi,
       round(l.last_mile_mi, 2)                                       AS last_mile_mi,
       round(l.round_trip_mi, 1)                                      AS round_trip_mi,
       round(w.diesel_usd_gal / l.mpg + l.nonfuel_usd_per_mile, 3)    AS usd_per_mile,
       round(l.round_trip_mi * (w.diesel_usd_gal / l.mpg + l.nonfuel_usd_per_mile), 2)
                                                                      AS usd_per_load,
       round(mart.trucking_usd_mmbtu(l.fixed_transport_usd_mmbtu, l.round_trip_mi,
                                     w.diesel_usd_gal, l.mpg, l.nonfuel_usd_per_mile,
                                     l.net_mmbtu_per_load), 4)        AS trucking_usd_mmbtu
FROM mart.v_legs l
JOIN (  -- a scenario may shift diesel by `diesel_delta` $/gal
    SELECT week_start, price_usd_gal + coalesce((mart.overrides() ->> 'diesel_delta')::numeric, 0)
                           AS diesel_usd_gal
    FROM core.fact_diesel_weekly
    WHERE region_code = (SELECT value_text FROM mart.v_assumption WHERE key = 'diesel_region')
) w ON true;

-- ── prices per trading day ───────────────────────────────────────────────────
-- Gas trades daily, diesel is surveyed weekly: each trading day takes the latest diesel week on
-- or before it (an as-of join), so a late diesel release never drops a day. A scenario may shift
-- observed prices by `henry_hub_delta` ($/MMBtu) and `diesel_delta` ($/gal).
CREATE VIEW mart.v_prices_daily AS
WITH p AS (
    SELECT
        max(value)      FILTER (WHERE key = 'diesel_lhv_btu_per_gal')  AS diesel_lhv,
        max(value)      FILTER (WHERE key = 'gas_lhv_btu_per_lb')      AS gas_lhv,
        max(value)      FILTER (WHERE key = 'gas_hhv_btu_per_lb')      AS gas_hhv,
        max(value_text) FILTER (WHERE key = 'diesel_region')           AS diesel_region,
        coalesce((mart.overrides() ->> 'henry_hub_delta')::numeric, 0) AS henry_hub_delta,
        coalesce((mart.overrides() ->> 'diesel_delta')::numeric, 0)    AS diesel_delta
    FROM mart.v_assumption
)
SELECT g.price_date,
       g.price_usd_mmbtu + p.henry_hub_delta                           AS henry_hub_usd_mmbtu,
       w.week_start                                                    AS diesel_week,
       w.price_usd_gal + p.diesel_delta                                AS diesel_usd_gal,
       mart.diesel_parity_usd_mmbtu(w.price_usd_gal + p.diesel_delta,
                                    p.diesel_lhv, p.gas_lhv, p.gas_hhv)
                                                                       AS diesel_parity_usd_mmbtu
FROM core.fact_gas_price_daily g
JOIN core.dim_hub h ON h.hub_id = g.hub_id AND h.hub_code = 'HENRY'
CROSS JOIN p
CROSS JOIN LATERAL (
    SELECT week_start, price_usd_gal FROM core.fact_diesel_weekly
    WHERE region_code = p.diesel_region AND week_start <= g.price_date
    ORDER BY week_start DESC LIMIT 1
) w;

-- ── the netback ──────────────────────────────────────────────────────────────
CREATE VIEW mart.v_netback_daily AS
WITH p AS (
    SELECT
        max(value) FILTER (WHERE key = 'fuel_discount_to_diesel') AS fuel_discount,
        max(value) FILTER (WHERE key = 'gas_mmbtu_per_mcf')       AS mmbtu_per_mcf
    FROM mart.v_assumption
),
days AS (SELECT * FROM mart.v_prices_daily),
priced AS (
    SELECT d.price_date, l.site_id, l.market, l.hub_code, l.mode,
           d.henry_hub_usd_mmbtu, d.diesel_usd_gal, d.diesel_parity_usd_mmbtu,
           b.value                                                     AS basis_usd_mmbtu,
           CASE l.market
               WHEN 'hub'  THEN d.henry_hub_usd_mmbtu + b.value
               WHEN 'fuel' THEN d.diesel_parity_usd_mmbtu * (1 - p.fuel_discount)
           END                                                         AS delivered_price_usd_mmbtu,
           l.conditioning_usd_mmbtu,
           mart.trucking_usd_mmbtu(l.fixed_transport_usd_mmbtu, l.round_trip_mi,
                                   d.diesel_usd_gal, l.mpg, l.nonfuel_usd_per_mile,
                                   l.net_mmbtu_per_load)               AS trucking_usd_mmbtu,
           l.delivery_usd_mmbtu,
           l.fee_usd_mmbtu,
           -- pieces for the closed-form break-even distance below
           l.fixed_transport_usd_mmbtu, l.net_mmbtu_per_load,
           d.diesel_usd_gal / l.mpg + l.nonfuel_usd_per_mile           AS usd_per_mile,
           l.one_way_mi, l.last_mile_mi, p.mmbtu_per_mcf
    FROM mart.v_legs l
    CROSS JOIN days d          -- 144 legs x 4,242 trading days: every row is an output row
    CROSS JOIN p
    LEFT JOIN mart.v_assumption b ON b.key = 'basis_' || lower(l.hub_code)
),
netted AS (
    SELECT *,
           conditioning_usd_mmbtu + trucking_usd_mmbtu + delivery_usd_mmbtu + fee_usd_mmbtu
                                                                       AS total_cost_usd_mmbtu,
           delivered_price_usd_mmbtu
             - (conditioning_usd_mmbtu + trucking_usd_mmbtu + delivery_usd_mmbtu + fee_usd_mmbtu)
                                                                       AS netback_usd_mmbtu,
           -- Margin left for driving once every distance-independent cost is paid.
           delivered_price_usd_mmbtu - conditioning_usd_mmbtu - delivery_usd_mmbtu
             - fee_usd_mmbtu - fixed_transport_usd_mmbtu               AS margin_before_miles
    FROM priced
)
SELECT n.price_date,
       s.site_id, s.site_name, s.basin, s.state,
       n.market,
       n.hub_code,
       n.mode,
       n.mode || ' → ' || coalesce(initcap(replace(n.hub_code, '_', ' ')), 'fuel customer')
                                                                       AS option_label,
       n.henry_hub_usd_mmbtu,
       n.basis_usd_mmbtu,
       n.diesel_usd_gal,
       round(n.diesel_parity_usd_mmbtu, 3)                             AS diesel_parity_usd_mmbtu,
       round(n.delivered_price_usd_mmbtu, 3)                           AS delivered_price_usd_mmbtu,
       round(n.conditioning_usd_mmbtu, 3)                              AS conditioning_usd_mmbtu,
       round(n.trucking_usd_mmbtu, 3)                                  AS trucking_usd_mmbtu,
       round(n.delivery_usd_mmbtu, 3)                                  AS delivery_usd_mmbtu,
       round(n.fee_usd_mmbtu, 3)                                       AS fee_usd_mmbtu,
       round(n.total_cost_usd_mmbtu, 3)                                AS total_cost_usd_mmbtu,
       round(n.netback_usd_mmbtu, 3)                                   AS netback_usd_mmbtu,
       n.netback_usd_mmbtu > 0                                         AS beats_flaring,
       -- exactly one best option per site-day; ties broken by mode/hub so the pick is stable
       row_number() OVER (PARTITION BY n.price_date, n.site_id
                          ORDER BY n.netback_usd_mmbtu DESC, n.mode, n.hub_code NULLS LAST) = 1
                                                                       AS is_best_option,
       n.one_way_mi,
       -- Solve netback = 0 for one-way road miles: margin = 2 x (d + last_mile) x $/mi / load.
       -- 0 means the option loses money even next door.
       round(greatest(0, n.margin_before_miles * n.net_mmbtu_per_load / (2 * n.usd_per_mile)
                         - n.last_mile_mi), 1)                         AS break_even_one_way_mi,
       -- Henry Hub needed for netback = 0 (hub options only): costs minus basis.
       CASE WHEN n.market = 'hub'
            THEN round(n.total_cost_usd_mmbtu - n.basis_usd_mmbtu, 3) END
                                                                       AS break_even_henry_hub,
       -- Largest discount to diesel parity the customer could demand before flaring wins
       -- (fuel options only): parity x (1 - d) = cost  =>  d = 1 - cost / parity.
       CASE WHEN n.market = 'fuel'
            THEN round(1 - n.total_cost_usd_mmbtu / n.diesel_parity_usd_mmbtu, 3) END
                                                                       AS break_even_fuel_discount,
       -- Annual dollars vs flaring if the site's whole flared volume went this way.
       round(n.netback_usd_mmbtu * s.flared_mmscfd * 1000 * n.mmbtu_per_mcf * 365)
                                                                       AS annual_value_vs_flaring_usd
FROM netted n
JOIN core.dim_site s USING (site_id);

-- One row per site for the latest priced day: the best option overall, the best pipeline-hub
-- option, and flaring ($0) side by side. This is what the dashboard map colours by.
CREATE VIEW mart.v_site_latest AS
WITH latest AS (
    SELECT * FROM mart.v_netback_daily
    WHERE price_date = (SELECT max(price_date) FROM core.fact_gas_price_daily)
),
best_hub AS (
    SELECT DISTINCT ON (site_id) site_id, option_label, netback_usd_mmbtu, break_even_henry_hub,
                                 one_way_mi, break_even_one_way_mi
    FROM latest WHERE market = 'hub'
    ORDER BY site_id, netback_usd_mmbtu DESC, option_label
),
best_fuel AS (
    SELECT DISTINCT ON (site_id) site_id, option_label, netback_usd_mmbtu, break_even_fuel_discount
    FROM latest WHERE market = 'fuel'
    ORDER BY site_id, netback_usd_mmbtu DESC, option_label
)
SELECT s.site_id, s.site_name, s.basin, s.state, s.lat, s.lon, s.field_name, s.flared_mmscfd,
       l.price_date,
       l.option_label                    AS best_option,
       l.netback_usd_mmbtu               AS best_netback_usd_mmbtu,
       l.annual_value_vs_flaring_usd     AS best_annual_value_vs_flaring_usd,
       h.option_label                    AS best_hub_option,
       h.netback_usd_mmbtu               AS best_hub_netback_usd_mmbtu,
       h.one_way_mi                      AS best_hub_one_way_mi,
       h.break_even_henry_hub            AS best_hub_break_even_henry_hub,
       f.option_label                    AS best_fuel_option,
       f.netback_usd_mmbtu               AS best_fuel_netback_usd_mmbtu,
       f.break_even_fuel_discount        AS best_fuel_break_even_discount,
       0::numeric                        AS flare_netback_usd_mmbtu,
       CASE WHEN l.netback_usd_mmbtu > 0 THEN 'truck: ' || l.market ELSE 'flare' END
                                         AS decision
FROM core.dim_site s
JOIN latest l ON l.site_id = s.site_id AND l.is_best_option
JOIN best_hub h ON h.site_id = s.site_id
JOIN best_fuel f ON f.site_id = s.site_id;
