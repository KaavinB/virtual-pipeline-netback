# 02 — Data sources (verified 2026-10-26)

Every endpoint below was called live before any code was written against it. The response
shapes quoted here are what the ingestion code parses.

## EIA API v2 — prices

Base: `https://api.eia.gov/v2/` · Docs: <https://www.eia.gov/opendata/documentation.php> ·
API version reported by server: `2.1.14`. Responses are capped at **5,000 rows per request**;
page through with `offset` + `length`. The key goes in the `api_key` query parameter.

| Series | Route | Facet | Frequency | Units | Latest seen |
|---|---|---|---|---|---|
| Henry Hub spot | `natural-gas/pri/fut` | `facets[series][]=RNGWHHD` | daily | $/MMBtu | 2026-10-20 = 3.10 |
| US No. 2 diesel retail | `petroleum/pri/gnd` | `facets[series][]=EMD_EPD2D_PTE_NUS_DPG` | weekly (Mon) | $/gal | 2026-10-19 = 6.276 |
| **Gulf Coast (PADD 3)** No. 2 diesel retail | `petroleum/pri/gnd` | `facets[series][]=EMD_EPD2D_PTE_R30_DPG` | weekly (Mon) | $/gal | 2026-10-19 = 5.903 |

Each row looks like this: `{"period": "2026-10-20", "series": "RNGWHHD", "value": "3.1", "units": "$/MMBTU", ...}`.
Note that **`value` arrives as a string** and has to be parsed.

**Which diesel series to use:** Texas and New Mexico are both in PADD 3 (Gulf Coast), and EIA
does not publish a Texas-only weekly diesel series. PADD 3 is the primary input. The US average
is ingested too, for comparison and as a fallback.

## Regional hub prices (Waha, Katy, Houston Ship Channel, Agua Dulce): **not publicly available**

- EIA API v2's spot-price route contains only `RNGWHHD` (Henry Hub) plus NYMEX contracts 1–4.
  This was checked by listing the facet: `GET /v2/natural-gas/pri/fut/facet/series/`.
- EIA's [Today in Energy prices page](https://www.eia.gov/todayinenergy/prices.php) shows the
  *current day* for Houston Ship Channel, credited as "republished by EIA with permission" from
  SNL Energy. It has no history, no API, and no Waha, Katy or Agua Dulce.
- Daily regional indices are sold by NGI and Platts (NGI builds its indices on ICE trade data):
  [NGI price index data](https://www.naturalgasintel.com/services/ngi-price-index-data/).

**Decision:** delivered price = Henry Hub (real, daily) + **basis per hub**. Basis is an
assumption in `assumptions.yaml`, documented and adjustable in the dashboard. The project
does not scrape licensed price data.

## Site locations — World Bank / NOAA VIIRS flare detections

- File: [Flare Volume Estimates by Individual Flare Location 2012–2025](https://thedocs.worldbank.org/en/doc/b34e0c054bb3fe3695e70154c28eef3f-0400072026/related/Flare-Volume-Estimates-by-individual-Flare-Location-2012-2025.xlsx)
  (June 2026 Global Gas Flaring Tracker). Listed on the
  [GFMR global flaring data page](https://www.worldbank.org/en/programs/gasflaringreduction/global-flaring-data).
- Attribution required by the page: *flare volumes from NOAA, Payne Institute (Colorado School of
  Mines), World Bank/GFMR.*
- Sheet `Dashboard_file`, one row per flare per year:
  `id, lat, lon, Country, BCM, MMSCFD, Year, Field type, Operator, Field name, Location, Flare Level, Flaring Vol (million m3)`.
- 2025 US flares: 11,691. Inside a Permian bounding box: 5,087 (273 MMSCFD total).
  Inside an Eagle Ford box: 1,901 (41 MMSCFD).
- **How this is used:** example sites are *real satellite-detected flare locations with
  estimated flared volumes*. Whether a given site actually lacks a pipeline is **not** in the
  data. That is an assumption, stated wherever sites are shown. Operator names are deliberately
  not carried into the project.

## Routing — OSRM

- Public demo server `https://router.project-osrm.org`, `route` service with
  `overview=simplified&geometries=geojson`: one request per site × hub pair, which returns
  distance, duration, snap distances and the road geometry for the map. Policy
  ([OSRM wiki](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server)): at most
  1 request/second, reasonable non-commercial use, no uptime guarantee. The client waits 1.1 s
  between requests (54 pairs, about a minute) and caches results in `fact_route`, so later runs
  only route new pairs.
- The response includes a per-point `distance` field: how far (in metres) OSRM had to move the
  coordinate to snap it to the nearest road. Flares sit on pads off lease roads, so this
  **snap distance is recorded and checked in data quality.**
- Fallback: local OSRM in Docker on a Geofabrik Texas extract (`make osrm-local`).

## Basin boundaries — EIA

- Permian: EIA `SedimentaryBasins_US_EIA` FeatureServer, layer 109, `Name = 'Permian'`.
  Eagle Ford: EIA `ShalePlay_EagleFord_Boundary_EIA_Aug2015_v2`. Both are from
  <https://services7.arcgis.com/FGr1D95XCGALKXqM/ArcGIS/rest/services>, queried as GeoJSON with
  `outSR=4326`, and committed under `data/reference/`.

## Cost, physical and regulatory inputs (all in `assumptions.yaml`)

Each one was checked at its source on 2026-10-26. The URLs live next to the values in
`assumptions.yaml`.

| Input | Source |
|---|---|
| Waha basis −$2.07 (2024), −$0.42 (H2 2021) | [EIA Today in Energy, 2024-09-10](https://www.eia.gov/todayinenergy/detail.php?id=63044) |
| Heat content 1,037 Btu/cf | [EIA FAQ 45](https://www.eia.gov/tools/faqs/faq.php?id=45&t=8) |
| LNG/CNG/diesel heating values (HHV, LHV) | [DOE AFDC fuel properties](https://afdc.energy.gov/fuels/properties) |
| Class 8 truck 6.30 mpg | [DOE AFDC / FHWA Highway Statistics VM-1](https://afdc.energy.gov/data/10310) |
| Non-fuel trucking $1.854/mi (2025) | [ATRI Operational Costs of Trucking, 2026 edition](https://www.ttnews.com/articles/atri-truck-costs-2025) |
| 80,000 lb gross vehicle weight | [23 CFR 658.17(b)](https://www.law.cornell.edu/cfr/text/23/658.17) |
| CNG TITAN4 40 ft: 363,148 scf, 33,700 lb | [Hexagon Mobile Pipeline brochure (2021)](https://www.cngservices.co.uk/wp-content/uploads/MobilePipleline_ePub_2021_11x8.5_b.pdf) |
| LNG 40 ft T75 ISO: 12,020 gal, 26,898 lb tare | [OEG cryogenic ISO datasheet](https://www.oeg.group/hubfs/data-sheets/OEG-Datasheet-Cryogenic-ISO-tanks.pdf) |
| Treatment, compression, liquefaction, delivery costs (USD2015) | World Bank GGFR: [CNG](https://documents1.worldbank.org/curated/en/210571472125529218/pdf/104200-V2-WP-CNG-commercialization-PUBLIC-Main-report-REPLACEMENT.pdf), [mini-LNG](https://documents1.worldbank.org/curated/en/299861484716073109/pdf/112131-WP-MiniMicroLNGforcommercializationofsmallvolumesofassociatedgas-PUBLIC-v2.pdf), [comparison](https://documents1.worldbank.org/curated/en/795991484736761603/pdf/112132-WP-ComparisonofMiniLNGandCNGforcommercializingsmallvolumesofassociatedgas-PUBLIC-v2.pdf) |
| CPI-U 2015 = 237.017, 2025 = 321.943 | [BLS CUUR0000SA0](https://data.bls.gov/timeseries/CUUR0000SA0) (via BLS API v2) |

**Not verifiable, so labelled rough estimates:** Katy and Agua Dulce basis (NGI articles block
automated access), fuel-customer discount and distance, tractor + chassis weight, LNG fill and
heel, marketing fee, last-mile and route-distance factors.
