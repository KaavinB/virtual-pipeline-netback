-- Scenarios, distance curves and the sensitivity (tornado) table.

-- ── scenarios ────────────────────────────────────────────────────────────────
-- Run the standard views with what-if inputs, e.g.
--   SELECT * FROM mart.site_latest_scenario('{"fuel_discount_to_diesel": 0.6, "henry_hub_delta": 1}');
-- The override lives only for this call: it is set, the view is evaluated, then it is cleared.
CREATE FUNCTION mart.site_latest_scenario(overrides jsonb DEFAULT '{}')
RETURNS SETOF mart.v_site_latest
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    PERFORM set_config('vpv.overrides', overrides::text, true);
    RETURN QUERY SELECT * FROM mart.v_site_latest;   -- executed (and buffered) right here
    PERFORM set_config('vpv.overrides', '', true);
END $$;

CREATE FUNCTION mart.netback_daily_scenario(overrides jsonb DEFAULT '{}',
                                            from_date date DEFAULT '2010-01-01')
RETURNS SETOF mart.v_netback_daily
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    PERFORM set_config('vpv.overrides', overrides::text, true);
    RETURN QUERY SELECT * FROM mart.v_netback_daily WHERE price_date >= from_date;
    PERFORM set_config('vpv.overrides', '', true);
END $$;

-- ── netback vs distance ──────────────────────────────────────────────────────
-- For the latest prices, netback of each market x mode at one-way distances 0-1,000 mi (no
-- last-mile leg). Where a line crosses 0 is that option's break-even distance.
CREATE VIEW mart.v_distance_curve AS
WITH p AS (
    SELECT
        max(value) FILTER (WHERE key = 'truck_mpg')               AS mpg,
        max(value) FILTER (WHERE key = 'nonfuel_usd_per_mile')    AS nonfuel_usd_per_mile,
        max(value) FILTER (WHERE key = 'fuel_discount_to_diesel') AS fuel_discount,
        max(value) FILTER (WHERE key = 'diesel_lhv_btu_per_gal')  AS diesel_lhv,
        max(value) FILTER (WHERE key = 'gas_lhv_btu_per_lb')      AS gas_lhv,
        max(value) FILTER (WHERE key = 'gas_hhv_btu_per_lb')      AS gas_hhv
    FROM mart.v_assumption
),
latest AS (  -- the same prices v_site_latest uses (scenario deltas included)
    SELECT price_date, henry_hub_usd_mmbtu, diesel_usd_gal FROM mart.v_prices_daily
    WHERE price_date = (SELECT max(price_date) FROM core.fact_gas_price_daily)
),
markets AS (
    SELECT 'hub' AS market, h.hub_code,
           l.henry_hub_usd_mmbtu + b.value AS delivered_price_usd_mmbtu
    FROM core.dim_hub h
    JOIN mart.v_assumption b ON b.key = 'basis_' || lower(h.hub_code)
    CROSS JOIN latest l
    UNION ALL
    SELECT 'fuel', NULL,
           mart.diesel_parity_usd_mmbtu(l.diesel_usd_gal, p.diesel_lhv, p.gas_lhv, p.gas_hhv)
             * (1 - p.fuel_discount)
    FROM latest l CROSS JOIN p
)
SELECT mk.market, mk.hub_code, m.mode,
       m.mode || ' → ' || coalesce(initcap(replace(mk.hub_code, '_', ' ')), 'fuel customer')
                                                                   AS option_label,
       d.one_way_mi,
       round(mk.delivered_price_usd_mmbtu
             - m.conditioning_usd_mmbtu
             - CASE mk.market WHEN 'hub' THEN m.delivery_usd_mmbtu
                              ELSE m.delivery_as_fuel_usd_mmbtu END
             - m.fee_usd_mmbtu
             - mart.trucking_usd_mmbtu(m.fixed_transport_usd_mmbtu, 2 * d.one_way_mi,
                                       l.diesel_usd_gal, p.mpg, p.nonfuel_usd_per_mile,
                                       m.net_mmbtu_per_load), 3)   AS netback_usd_mmbtu
FROM markets mk
CROSS JOIN mart.v_mode_economics m
CROSS JOIN generate_series(0, 1000, 10) AS d(one_way_mi)
CROSS JOIN latest l
CROSS JOIN p;

-- ── CNG vs LNG crossover ─────────────────────────────────────────────────────
-- Cost per MMBtu is  a + b x one-way miles  for each mode, where a = every distance-independent
-- cost and b = 2 x $/mile / MMBtu per load. LNG has the larger a and the smaller b, so the lines
-- cross at  (a_LNG - a_CNG) / (b_CNG - b_LNG)  miles. Price cancels out: it is a cost question.
CREATE VIEW mart.v_mode_crossover AS
WITH p AS (
    SELECT
        max(value) FILTER (WHERE key = 'truck_mpg')            AS mpg,
        max(value) FILTER (WHERE key = 'nonfuel_usd_per_mile') AS nonfuel_usd_per_mile
    FROM mart.v_assumption
),
diesel AS (
    SELECT diesel_usd_gal FROM mart.v_prices_daily
    WHERE price_date = (SELECT max(price_date) FROM core.fact_gas_price_daily)
),
lines AS (
    SELECT mk.market, m.mode,
           m.conditioning_usd_mmbtu + m.fee_usd_mmbtu + m.fixed_transport_usd_mmbtu
             + CASE mk.market WHEN 'hub' THEN m.delivery_usd_mmbtu
                              ELSE m.delivery_as_fuel_usd_mmbtu END              AS a,
           2 * (d.diesel_usd_gal / p.mpg + p.nonfuel_usd_per_mile) / m.net_mmbtu_per_load AS b
    FROM (VALUES ('hub'), ('fuel')) AS mk(market)
    CROSS JOIN mart.v_mode_economics m
    CROSS JOIN diesel d CROSS JOIN p
)
SELECT c.market,
       round(c.a, 3) AS cng_fixed_usd_mmbtu, round(c.b * 1000, 3) AS cng_usd_mmbtu_per_1000mi,
       round(l.a, 3) AS lng_fixed_usd_mmbtu, round(l.b * 1000, 3) AS lng_usd_mmbtu_per_1000mi,
       round((l.a - c.a) / (c.b - l.b))   AS crossover_one_way_mi
FROM lines c JOIN lines l ON l.market = c.market AND l.mode = 'LNG'
WHERE c.mode = 'CNG';

-- ── sensitivity (tornado) ────────────────────────────────────────────────────
-- Filled by `make sensitivity` (ingestion/sensitivity.py): each driver is moved to its low and
-- high value with everything else at base, via mart.site_latest_scenario().
CREATE TABLE mart.sensitivity_tornado (
    site_name        text    NOT NULL,
    basin            text    NOT NULL,
    market           text    NOT NULL CHECK (market IN ('hub', 'fuel')),
    driver           text    NOT NULL,     -- assumption key, or henry_hub / diesel
    driver_label     text    NOT NULL,
    unit             text    NOT NULL,
    base_input       numeric,
    low_input        numeric NOT NULL,
    high_input       numeric NOT NULL,
    base_netback     numeric NOT NULL,
    netback_at_low   numeric NOT NULL,
    netback_at_high  numeric NOT NULL,
    swing            numeric NOT NULL,     -- |netback_at_high - netback_at_low|
    flips_decision   boolean NOT NULL,     -- does either end change "beats flaring"?
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_name, market, driver)
);
