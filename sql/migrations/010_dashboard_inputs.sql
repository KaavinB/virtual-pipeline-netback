-- Dashboard inputs in the units a person thinks in, translated into model overrides.
--
--   henry_hub      $/MMBtu, absolute     -> henry_hub_delta (vs the latest observed price)
--   diesel         $/gal, absolute       -> diesel_delta    (vs the latest observed price)
--   discount_pct   % off diesel parity   -> fuel_discount_to_diesel (fraction)
--   waha_discount  $/MMBtu below HH (+)  -> basis_waha (negative number)
--   customer_mi    one-way miles         -> fuel_customer_one_way_mi
--
-- A missing input means "base case", so a blank dashboard filter shows today's numbers.
CREATE FUNCTION mart.dashboard_overrides(inputs jsonb DEFAULT '{}')
RETURNS jsonb
LANGUAGE sql STABLE
RETURN jsonb_strip_nulls(jsonb_build_object(
    'henry_hub_delta',
        (inputs ->> 'henry_hub')::numeric
        - (SELECT g.price_usd_mmbtu FROM core.fact_gas_price_daily g
           JOIN core.dim_hub h USING (hub_id) WHERE h.hub_code = 'HENRY'
           ORDER BY g.price_date DESC LIMIT 1),
    'diesel_delta',
        (inputs ->> 'diesel')::numeric
        - (SELECT w.price_usd_gal FROM core.fact_diesel_weekly w
           WHERE w.region_code = (SELECT value_text FROM core.assumption WHERE key = 'diesel_region')
           ORDER BY w.week_start DESC LIMIT 1),
    'fuel_discount_to_diesel', (inputs ->> 'discount_pct')::numeric / 100,
    'basis_waha',              -(inputs ->> 'waha_discount')::numeric,
    'fuel_customer_one_way_mi', (inputs ->> 'customer_mi')::numeric
));

-- The value of every dashboard input actually in use, and where it came from, so a viewer
-- always knows whether they are looking at the base case or their own what-if.
CREATE FUNCTION mart.dashboard_inputs_in_use(inputs jsonb DEFAULT '{}')
RETURNS TABLE (input text, value text, source text)
LANGUAGE sql STABLE AS $$
    WITH base AS (
        SELECT g.price_usd_mmbtu AS henry_hub, g.price_date AS henry_hub_date
        FROM core.fact_gas_price_daily g JOIN core.dim_hub h USING (hub_id)
        WHERE h.hub_code = 'HENRY' ORDER BY g.price_date DESC LIMIT 1
    ), diesel AS (
        SELECT w.price_usd_gal AS diesel, w.week_start AS diesel_week
        FROM core.fact_diesel_weekly w
        WHERE w.region_code = (SELECT value_text FROM core.assumption WHERE key = 'diesel_region')
        ORDER BY w.week_start DESC LIMIT 1
    ), a AS (
        SELECT max(value) FILTER (WHERE key = 'fuel_discount_to_diesel')  AS discount,
               max(value) FILTER (WHERE key = 'basis_waha')               AS basis_waha,
               max(value) FILTER (WHERE key = 'fuel_customer_one_way_mi') AS customer_mi
        FROM core.assumption
    )
    SELECT v.input, v.value,
           CASE WHEN inputs ? v.slug THEN 'Your what-if' ELSE v.base_source END
    FROM base, diesel, a, LATERAL (VALUES
        (1, 'henry_hub', 'Henry Hub price',
            to_char(coalesce((inputs ->> 'henry_hub')::numeric, base.henry_hub), 'FM$990.00') || ' /MMBtu',
            'EIA, ' || base.henry_hub_date),
        (2, 'diesel', 'Diesel price (Gulf Coast)',
            to_char(coalesce((inputs ->> 'diesel')::numeric, diesel.diesel), 'FM$990.000') || ' /gal',
            'EIA, week of ' || diesel.diesel_week),
        (3, 'discount_pct', 'Customer discount off diesel',
            to_char(coalesce((inputs ->> 'discount_pct')::numeric, 100 * a.discount), 'FM990') || '%',
            'assumptions.yaml'),
        (4, 'waha_discount', 'Waha discount to Henry Hub',
            to_char(coalesce((inputs ->> 'waha_discount')::numeric, -a.basis_waha), 'FM$990.00'),
            'assumptions.yaml (EIA 2024)'),
        (5, 'customer_mi', 'Miles to fuel customer',
            to_char(coalesce((inputs ->> 'customer_mi')::numeric, a.customer_mi), 'FM9990') || ' mi',
            'assumptions.yaml')
    ) AS v(ord, slug, input, value, base_source)
    ORDER BY v.ord
$$;
