# Building the Tableau Public version

Tableau Public reads files, not databases, so `make export` writes the extracts below to
`dashboards/exports/`. Every number in them comes straight from the mart views. **The Metabase
dashboard (`make dashboard`) is the live, fully parameterised version.** This guide rebuilds its
core views in Tableau Public (free; needs a Tableau Public account and the desktop app).

| File | Grain | Use |
|---|---|---|
| `sites.csv` | 1 row per site (latest day) | map, scorecard |
| `hubs.csv` | 1 row per delivery hub | map layer |
| `route_path.csv` | 1 row per route vertex | road lines on the map (`point_order`) |
| `netback_daily.csv` | day × site × option, 2021+ (~209k rows) | history, option comparison |
| `distance_curve.csv` | option × one-way mile (0–1,000) | break-even distance curve |
| `tornado.csv` | site × market × driver | sensitivity |
| `assumptions.csv` | 1 row per assumption | assumptions panel, with sources |
| `dq_latest.csv` | 1 row per DQ check | data-quality panel |

## Sheets

1. **Map.** Data: `sites.csv`. Lon → Columns, Lat → Rows, map mark.
   - Colour by `best_netback_usd_mmbtu` (diverging palette, centre 0). Size by `flared_mmscfd`.
     Tooltip: `site_name`, `best_option`, `best_hub_option`, `best_hub_break_even_henry_hub`.
   - Add a second map layer from `hubs.csv` (shape: square).
   - Optional road lines: `route_path.csv`, Line mark, Path = `point_order`, Detail = `route`.
2. **Scorecard.** `sites.csv` as a text table, sorted by netback.
3. **Netback vs distance.** `distance_curve.csv`: `one_way_mi` (continuous) × `netback_usd_mmbtu`,
   Colour = `option_label`. Add a constant reference line at 0 labelled "flare = $0".
4. **History.** `netback_daily.csv`, filter `is_best_option` per market: WEEK(`price_date`) ×
   AVG(`netback_usd_mmbtu`), Colour = `market`.
5. **Tornado.** `tornado.csv`, filter `market` and `site_name` (single-value dropdowns).
   - Rows = `driver_label` sorted by `swing`. Columns: dual axis of `netback_at_low` and
     `netback_at_high` as Gantt bars, with a reference line at `base_netback`.
6. **Assumptions** and **Data quality**: text tables from their CSVs. Show `basis` so rough
   estimates stand out.

## Exact what-if parameters

Netback is linear in Henry Hub and in the fuel discount, so these two parameters are exact
(the same identities are asserted in `tests/test_scenarios.py`):

```
[HH shift]  (float, default 0)       hub netback  = [netback_usd_mmbtu] + [HH shift]
[Discount]  (float 0-1, default 0.4) fuel netback = [netback_usd_mmbtu]
                                                    + [diesel_parity_usd_mmbtu] * (0.40 - [Discount])
```

```
// calculated field: Netback (what-if), for netback_daily.csv
IF [market] = "hub" THEN [netback_usd_mmbtu] + [HH shift]
ELSE [netback_usd_mmbtu] + [diesel_parity_usd_mmbtu] * (0.40 - [Discount])
END
```

Anything non-linear (distances, payloads, diesel's effect on trucking) is left to the SQL: change
`assumptions.yaml`, run `make assumptions sensitivity export`, and refresh the extracts.

## Publish

File → Save to Tableau Public. Name it *Virtual Pipeline Netback: Permian & Eagle Ford*, then put
the link at the top of the README.
