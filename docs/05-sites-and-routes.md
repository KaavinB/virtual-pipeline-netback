# 05 — Sites and routes

## Where the sites come from

The 18 example sites are **real flares detected by satellite** (NOAA VIIRS), with annual volumes
estimated by the World Bank ([data page](https://www.worldbank.org/en/programs/gasflaringreduction/global-flaring-data)).
Basin membership uses **EIA's published boundaries**, not a bounding box:

| Boundary | EIA source | Saved as |
|---|---|---|
| Permian Basin (sedimentary basin, 135-point polygon) | `SedimentaryBasins_US_EIA` FeatureServer, layer 109, `Name = 'Permian'` | `data/reference/permian_basin_eia.geojson` |
| Eagle Ford (shale play, 214-point polygon) | `ShalePlay_EagleFord_Boundary_EIA_Aug2015_v2` FeatureServer | `data/reference/eagle_ford_play_eia.geojson` |

Both come from EIA's ArcGIS service directory,
`https://services7.arcgis.com/FGr1D95XCGALKXqM/ArcGIS/rest/services`, queried with `outSR=4326`
(lon/lat). A ray-casting point-in-polygon test (`ingestion/sites.py`, about 10 lines) assigns
each flare to a basin.

### Selection rule

| Rule | Value | Why |
|---|---|---|
| Field type | `OIL` | Associated gas: gas that comes out with the oil. This is the stranded-gas case a virtual pipeline serves. |
| Persistence | flared in **each** of 2023, 2024, 2025 | A one-off upset flares once. A site with no takeaway flares every year. This is the evidence behind "probably off-pipeline". |
| 2025 volume | 0.10–2.00 MMSCFD | *Rough industry estimate.* Below about 0.1 MMSCFD a trailer takes days to fill. Above about 2 MMSCFD a gathering line usually pays for itself, so the site isn't a trucking customer. |
| Spacing | ≥ 20 km apart | Largest flares are picked first, skipping any within 20 km of an earlier pick. This spreads sites out so routes cover a range of distances. |
| Count | 12 Permian + 6 Eagle Ford | Roughly proportional to flared volume: Permian 273 MMSCFD vs Eagle Ford 41 MMSCFD in 2025. |

Result: 378 Permian and 54 Eagle Ford flares pass the rule, and the 18 below are picked. Sorting is
by volume with ties broken by id, so the selection is identical on every run.
**Operator names are deliberately not loaded.** Field names come from the World Bank data.

## Routes (OSRM, one way, miles)

| Site | State | Field | MMSCFD (2025) | Lat, Lon | → Waha | → Agua Dulce | → Katy | Snap (m) |
|---|---|---|---|---|---|---|---|---|
| PB-01 | TX | SPRABERRY | 1.93 | 31.64168, -102.19216 | 84.4 | 460.4 | 489.6 | 463 |
| PB-02 | TX | SPRABERRY | 1.92 | 32.40822, -101.28336 | 143.1 | 451.1 | 449.8 | 156 |
| PB-03 | TX | BREEDLOVE, SOUTH | 1.87 | 32.42849, -102.09239 | 117.1 | 482.8 | 472.6 | 234 |
| PB-04 | TX | PHANTOM | 1.84 | 31.28756, -103.67452 | 47.0 | 516.6 | 545.8 | **2157** |
| PB-05 | TX | SPRABERRY | 1.60 | 31.73783, -101.79919 | 110.3 | 440.0 | 469.2 | 104 |
| PB-06 | TX | SPRABERRY | 1.37 | 32.14447, -101.97563 | 102.3 | 463.6 | 453.4 | 310 |
| PB-07 | TX | KEYSTONE | 1.32 | 31.94882, -103.04573 | 58.0 | 529.5 | 558.7 | 2 |
| PB-08 | TX | SPRABERRY | 1.26 | 32.38865, -101.60909 | 136.5 | 452.2 | 442.0 | 138 |
| PB-09 | TX | SPRABERRY | 1.23 | 31.40448, -101.99288 | 114.4 | 431.7 | 460.9 | 1112 |
| PB-10 | TX | PHANTOM | 1.06 | 31.79501, -103.25614 | 51.0 | 545.0 | 574.2 | 679 |
| PB-11 | TX | PHANTOM | 1.01 | 31.91342, -103.49282 | 66.2 | 549.9 | 579.1 | **7926** |
| PB-12 | NM | EUMONT | 0.94 | 32.69226, -103.28513 | 117.3 | 561.2 | 551.0 | 502 |
| EF-01 | TX | EAGLEVILLE | 0.93 | 28.99464, -97.84434 | 399.0 | 98.3 | 165.6 | 72 |
| EF-02 | TX | SAN MIGUEL CREEK | 0.71 | 28.52763, -98.54406 | 410.7 | 89.0 | 227.5 | 297 |
| EF-03 | TX | EAGLEVILLE | 0.60 | 28.73229, -98.73878 | 405.2 | 128.4 | 222.1 | 76 |
| EF-04 | TX | EAGLEVILLE | 0.38 | 28.84283, -97.98892 | 401.5 | 94.9 | 177.0 | 106 |
| EF-05 | TX | EAGLEVILLE | 0.33 | 28.59865, -99.36457 | 353.3 | 128.0 | 250.5 | 1735 |
| EF-06 | TX | BRISCOE RANCH | 0.24 | 28.33974, -99.55102 | 339.3 | 127.2 | 283.1 | 87 |

What the table already says about the business:
- **Permian gas has one realistic truck destination: Waha** (47–143 mi). Katy is 440–580 mi away,
  a full day's drive each way. Waha is also the most discounted hub, because Permian takeaway
  pipelines are full. That tension, a near but cheap hub against a far but well-priced one, is
  exactly what the netback model prices.
- **Eagle Ford sites have two live options**: Agua Dulce at 89–128 mi, or Katy at 166–283 mi.

## Route quality

| Check | Result |
|---|---|
| `route_missing_pairs` (error) | 0. All 54 site × hub pairs are routed. |
| `route_detour_ratio_out_of_range` (error) | 0. Road ÷ straight-line distance ranges 1.08–1.78. |
| `route_site_far_from_road` (warn) | **2**: PB-11 (7.9 km) and PB-04 (2.2 km). OSRM snaps each flare to the nearest *mapped* road, and private lease roads are often missing from OpenStreetMap. The routed miles therefore understate these trips. The cost model adds the snap distance back as a last-mile leg, a conservative straight-line lower bound. |

## Caveats

- OSRM's `car` profile gives car speeds. Loaded trucks are slower, so durations are shown for
  context only. Trucking cost is per mile, not per hour.
- Hub coordinates are town proxies for clusters of interconnects (see `dim_hub.location_note`).
  A real virtual-pipeline operator would deliver to the nearest pipeline interconnect with
  capacity, which is usually closer than a named hub. Using hubs is conservative.
- **Local OSRM fallback** (`make osrm-local`) uses Geofabrik's Texas extract, which **excludes
  New Mexico**. PB-12 (Eumont, NM) would fail to route there, and `route_missing_pairs` would
  report it. The public demo server covers all of North America.
