-- Star schema. Grains:
--   fact_gas_price_daily : one row per hub per trading day
--   fact_diesel_weekly   : one row per region per EIA survey week (Monday)
--   fact_route           : one row per (site, hub) pair, the latest routed result
-- Constraints reject what is physically impossible (NULLs, negative distances).
-- Things that are merely implausible (a $40 gas print, a stale feed) are flagged by data-quality
-- checks instead, because a negative Waha price is real and must not be rejected.

-- ── dimensions ────────────────────────────────────────────────────────────────

CREATE TABLE core.dim_date (
    date_day     date PRIMARY KEY,
    year         smallint NOT NULL,
    quarter      smallint NOT NULL,
    month        smallint NOT NULL,
    month_name   text     NOT NULL,
    iso_week     smallint NOT NULL,
    week_start   date     NOT NULL,          -- Monday; matches EIA's weekly diesel period
    day_of_week  smallint NOT NULL,          -- ISO: 1 = Monday ... 7 = Sunday
    is_weekend   boolean  NOT NULL
);

INSERT INTO core.dim_date
SELECT d::date,
       extract(year    FROM d),
       extract(quarter FROM d),
       extract(month   FROM d),
       to_char(d, 'Mon'),
       extract(week    FROM d),
       date_trunc('week', d)::date,
       extract(isodow  FROM d),
       extract(isodow  FROM d) IN (6, 7)
FROM generate_series('2010-01-01'::date, '2030-12-31'::date, interval '1 day') AS d;

CREATE TABLE core.dim_hub (
    hub_id             smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hub_code           text    NOT NULL UNIQUE,
    hub_name           text    NOT NULL,
    region             text    NOT NULL,
    lat                numeric(8,5),
    lon                numeric(8,5),
    is_delivery_point  boolean NOT NULL,     -- false = price reference only (Henry Hub)
    location_note      text    NOT NULL
);

-- Hubs are clusters of interconnects, not points. Each coordinate is a documented proxy
-- (a few miles of error is noise against 100+ mile truck routes).
INSERT INTO core.dim_hub (hub_code, hub_name, region, lat, lon, is_delivery_point, location_note) VALUES
  ('HENRY',      'Henry Hub',        'Louisiana',   NULL,     NULL,      false,
   'Benchmark price only (Erath, LA). Not a trucking destination.'),
  ('WAHA',       'Waha Hub',         'Permian',     31.24053, -103.06612, true,
   'Proxy: Coyanosa, Pecos County, TX; hub is ~3 mi NW (Wikipedia/GNIS).'),
  ('AGUA_DULCE', 'Agua Dulce Hub',   'South Texas', 27.78250, -97.91083,  true,
   'Proxy: Agua Dulce, Nueces County, TX (Wikipedia/GNIS).'),
  ('KATY',       'Katy Hub',         'Gulf Coast',  29.78579, -95.82439,  true,
   'Proxy: Katy, TX (Wikipedia/GNIS).');

CREATE TABLE core.dim_site (
    site_id            int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    flare_id           text NOT NULL UNIQUE,  -- World Bank / VIIRS id, e.g. 's_15915'
    site_name          text NOT NULL,         -- display label, e.g. 'PB-03'
    basin              text NOT NULL CHECK (basin IN ('Permian', 'Eagle Ford')),
    state              text NOT NULL CHECK (state IN ('TX', 'NM')),
    lat                numeric(8,5) NOT NULL,
    lon                numeric(8,5) NOT NULL,
    field_type         text,                  -- OIL / GAS as classified by the World Bank
    field_name         text,
    flared_mmscfd      numeric(10,4) NOT NULL CHECK (flared_mmscfd >= 0),  -- latest year
    flare_year         smallint NOT NULL,
    years_flared_of_3  smallint NOT NULL CHECK (years_flared_of_3 BETWEEN 0 AND 3),
    selection_rank     smallint NOT NULL,     -- rank within basin by the selection rule
    loaded_at          timestamptz NOT NULL DEFAULT now()
);

-- ── facts ────────────────────────────────────────────────────────────────────

CREATE TABLE core.fact_gas_price_daily (
    price_date       date     NOT NULL REFERENCES core.dim_date,
    hub_id           smallint NOT NULL REFERENCES core.dim_hub,
    price_usd_mmbtu  numeric(8,3) NOT NULL,
    source_series    text     NOT NULL,      -- e.g. 'EIA:RNGWHHD'
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (price_date, hub_id)
);

CREATE TABLE core.fact_diesel_weekly (
    week_start       date NOT NULL REFERENCES core.dim_date,
    region_code      text NOT NULL CHECK (region_code IN ('US', 'PADD3')),
    price_usd_gal    numeric(6,3) NOT NULL CHECK (price_usd_gal > 0),
    source_series    text NOT NULL,          -- e.g. 'EIA:EMD_EPD2D_PTE_R30_DPG'
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, region_code)
);

CREATE TABLE core.fact_route (
    site_id        int      NOT NULL REFERENCES core.dim_site ON DELETE CASCADE,
    hub_id         smallint NOT NULL REFERENCES core.dim_hub,
    distance_m     numeric(10,1) NOT NULL CHECK (distance_m >= 0),   -- one way, as OSRM returns
    duration_s     numeric(10,1) NOT NULL CHECK (duration_s >= 0),
    distance_mi    numeric(8,1) GENERATED ALWAYS AS (round(distance_m / 1609.344, 1)) STORED,
    duration_hr    numeric(6,2) GENERATED ALWAYS AS (round(duration_s / 3600, 2)) STORED,
    site_snap_m    numeric(8,1) NOT NULL,     -- how far OSRM moved the site to reach a road
    hub_snap_m     numeric(8,1) NOT NULL,
    router         text NOT NULL,             -- 'osrm-demo' or 'osrm-local'
    routed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, hub_id)
);

-- Price lookups are always "this hub over a date range"; the PK (date, hub) can't serve that.
CREATE INDEX ON core.fact_gas_price_daily (hub_id, price_date);
