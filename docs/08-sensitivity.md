# 08 — Sensitivity

**Method.** One driver at a time is moved to its low and then its high value, with every other
input at base. The netbacks are recomputed **by the same SQL views**, through
`mart.site_latest_scenario(overrides)`. Python (`ingestion/sensitivity.py`) only chooses the
inputs. It does no netback arithmetic.

Ranges:
- **Assumptions:** each one's `low`/`high` from `assumptions.yaml`.
- **Henry Hub and diesel:** the 10th–90th percentile of the trailing three years of observed
  prices, widened to include today's price. Late-2026 diesel ($5.90) sits *above* its own
  3-year P90 ($5.13).

Result: 25 drivers × 18 sites × 2 markets, stored in `mart.sensitivity_tornado`.
**Decision flips: 0 of 514 site × market × driver combinations.**
No single input, at either end of its range, changes whether a site should truck or flare.

## How the scenario layer works

```sql
-- every view reads assumptions through mart.v_assumption, which honours a session override:
SELECT * FROM mart.site_latest_scenario('{"fuel_discount_to_diesel": 0.6, "henry_hub_delta": 1.0}');
```
The function sets `vpv.overrides` (transaction-local), evaluates `v_site_latest`, and clears the
setting. A test asserts that an override never leaks into the next query. The same mechanism
drives the dashboard's assumption controls, so **the dashboard, the tornado and the base case all run
one implementation of the business logic.**

## Tornado: EF-02, Eagle Ford (0.71 MMSCFD)

**Fuel customer (CNG): base $20.09/MMBtu**

| Driver | Low → high input | Netback at low | Netback at high | Swing |
|---|---|---|---|---|
| Fuel discount to diesel | 0.20 → 0.60 | 28.34 | 11.84 | 16.50 |
| Diesel price (PADD 3) | 3.19 → 5.90 | 8.84 | 20.09 | 11.25 |
| Distance to fuel customer | 10 → 150 | 20.75 | 18.44 | 2.31 |
| CNG fixed transport cost | 1.09 → 1.94 | 20.50 | 19.64 | 0.86 |
| CNG gas treatment cost | 0.29 → 0.75 | 20.37 | 19.91 | 0.46 |
| CNG compression cost | 0.95 → 1.29 | 20.13 | 19.79 | 0.34 |
| Marketing fee | 0.00 → 0.25 | 20.19 | 19.94 | 0.25 |
| Non-fuel trucking $/mile | 1.48 → 2.23 | 20.20 | 19.98 | 0.22 |

**Best pipeline hub (CNG → Agua Dulce): base −$2.60/MMBtu**

| Driver | Low → high input | Netback at low | Netback at high | Swing |
|---|---|---|---|---|
| Henry Hub price | 1.83 → 3.90 | -3.87 | -1.80 | 2.07 |
| CNG fixed transport cost | 1.09 → 1.94 | -2.20 | -3.05 | 0.86 |
| Route distance to hub (x) | 0.75 → 1.25 | -2.24 | -2.97 | 0.73 |
| CNG gas treatment cost | 0.29 → 0.75 | -2.32 | -2.78 | 0.46 |
| Agua Dulce basis | -0.60 → -0.20 | -2.80 | -2.40 | 0.40 |
| Non-fuel trucking $/mile | 1.48 → 2.23 | -2.41 | -2.80 | 0.39 |
| CNG compression cost | 0.95 → 1.29 | -2.56 | -2.90 | 0.34 |
| Marketing fee | 0.00 → 0.25 | -2.50 | -2.75 | 0.25 |

## Tornado: PB-07, Permian (1.32 MMSCFD)

**Best pipeline hub (CNG → Waha): base −$3.76/MMBtu**

| Driver | Low → high input | Netback at low | Netback at high | Swing |
|---|---|---|---|---|
| Henry Hub price | 1.83 → 3.90 | -5.03 | -2.96 | 2.07 |
| Waha basis | -2.37 → -0.42 | -4.06 | -2.11 | 1.95 |
| CNG fixed transport cost | 1.09 → 1.94 | -3.35 | -4.21 | 0.86 |
| Route distance to hub (x) | 0.75 → 1.25 | -3.52 | -4.00 | 0.48 |
| CNG gas treatment cost | 0.29 → 0.75 | -3.48 | -3.94 | 0.46 |
| CNG compression cost | 0.95 → 1.29 | -3.72 | -4.06 | 0.34 |
| Non-fuel trucking $/mile | 1.48 → 2.23 | -3.63 | -3.89 | 0.25 |
| Marketing fee | 0.00 → 0.25 | -3.66 | -3.91 | 0.25 |

## What drives the answer

1. **Fuel market:** the **customer discount** and the **diesel price** dominate. Each cost-side
   input moves netback by less than $1 on its own. The decision is robust: even at a
   60% discount, or with diesel at its 3-year low of $3.19, netback stays above $8/MMBtu.
2. **Hub market:** **Henry Hub** and **Waha basis** dominate, then the fixed transport cost and
   route distance. Even Henry Hub at its 3-year P90, or Waha basis back at its 2021 level, leaves
   every hub netback negative.
3. **LNG never becomes the best option** in any single-driver scenario, so LNG-specific costs
   such as liquefaction don't appear in these tornados.

## CNG vs LNG crossover (`mart.v_mode_crossover`)

| Market | CNG cost | LNG cost | LNG beats CNG beyond |
|---|---|---|---|
| hub | $3.835 + $16.471/1,000 mi | $10.342 + $9.198/1,000 mi | **895 mi** |
| fuel | $3.835 + $16.471/1,000 mi | $8.495 + $9.198/1,000 mi | **641 mi** |

LNG's per-mile cost is about 44% lower (more MMBtu per legal load), but it starts $4.66–6.51/MMBtu
behind on liquefaction. Every route in this project is under 600 mi, so **CNG wins at every site.**
`mart.v_distance_curve` gives the full netback-vs-distance curves used on the dashboard.
