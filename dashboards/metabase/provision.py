"""Provision the Metabase dashboard from code (idempotent: re-running rebuilds it).

    python -m dashboards.metabase.provision     # or: make dashboard

Creates the admin user on first run, connects Metabase to the Postgres container, and builds a
four-tab dashboard whose cards are native SQL over the mart views. The scenario filters are
plain-language inputs (prices, discount %, miles); mart.dashboard_overrides() translates them into
model overrides, so the dashboard recomputes netback with the project's SQL, never its own.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass, field

import requests

from ingestion.config import setup_logging
from ingestion.db import connect

log = logging.getLogger("metabase")
BASE = os.getenv("MB_URL", "http://localhost:3000")
COLLECTION = "VPV Netback"
DASHBOARD = "Truck it or flare it? Virtual-pipeline netback, Permian & Eagle Ford"
DEFAULT_SITE = "EF-02"

GREEN, RED, BLUE, GREY, AMBER = "#3E9B6F", "#D9534F", "#2F6DB5", "#9AA5B1", "#E8A33D"

# ── filters ──────────────────────────────────────────────────────────────────
# Scenario inputs are optional: blank = base case (latest prices, assumptions.yaml).
SCENARIO_INPUTS = [  # (slug, label shown on the dashboard)
    ("henry_hub", "Henry Hub $/MMBtu"),
    ("diesel", "Diesel $/gal"),
    ("discount_pct", "Discount % off diesel"),
    ("waha_discount", "Waha discount $"),
    ("customer_mi", "Miles to customer"),
]
SCENARIO = "'{}'::jsonb" + "".join(
    f" [[|| jsonb_build_object('{s}', {{{{{s}}}}})]]" for s, _ in SCENARIO_INPUTS
)  # e.g. '{}'::jsonb [[|| jsonb_build_object('henry_hub', {{henry_hub}})]] ...  (Metabase drops
#        a [[...]] block when its filter is empty)
OVERRIDES = f"mart.dashboard_overrides({SCENARIO})"
LATEST = "(SELECT max(price_date) FROM core.fact_gas_price_daily)"


@dataclass
class Card:
    name: str
    display: str
    sql: str
    viz: dict = field(default_factory=dict)
    description: str = ""

    @property
    def tags(self) -> list[str]:
        used = [s for s, _ in SCENARIO_INPUTS if "{{" + s + "}}" in self.sql]
        return used + (["site"] if "{{site}}" in self.sql else [])


def money(column: str, decimals: int = 2) -> dict:
    return {f'["name","{column}"]': {"number_style": "currency", "currency": "USD",
                                     "decimals": decimals}}  # fmt: skip


def sign_colours(*columns: str) -> list[dict]:
    """Green above $0 (beats flaring), red below."""
    return [
        {"id": 0, "columns": list(columns), "type": "single", "operator": ">", "value": 0,
         "color": GREEN, "highlight_row": False},
        {"id": 1, "columns": list(columns), "type": "single", "operator": "<", "value": 0,
         "color": RED, "highlight_row": False},
    ]  # fmt: skip


FLARE_LINE = {"graph.show_goal": True, "graph.goal_value": 0, "graph.goal_label": "Flaring = $0"}

CARDS: dict[str, Card] = {
    # ── Overview ─────────────────────────────────────────────────────────────
    "scenario": Card(
        "Inputs in use",
        "table",
        f"""SELECT input AS "Input", value AS "Value", source AS "Source"
            FROM mart.dashboard_inputs_in_use({SCENARIO})""",
        {
            "table.column_formatting": [
                {
                    "id": 0,
                    "columns": ["Source"],
                    "type": "single",
                    "operator": "=",
                    "value": "Your what-if",
                    "color": AMBER,
                    "highlight_row": True,
                },
            ],
        },  # fmt: skip
        "Blank filters = base case. Rows you change light up amber.",
    ),
    "kpi_sites": Card(
        "Sites where trucking beats flaring",
        "scalar",
        f"""SELECT count(*) FILTER (WHERE best_netback_usd_mmbtu > 0) || ' of ' || count(*) AS sites
            FROM mart.site_latest_scenario({OVERRIDES})""",
        description="A site beats flaring when its best trucking option nets more than $0/MMBtu.",
    ),
    "kpi_fuel": Card(
        "Netback selling as fuel (median)",
        "scalar",
        f"""SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY best_fuel_netback_usd_mmbtu)::numeric, 2)
                   AS netback FROM mart.site_latest_scenario({OVERRIDES})""",
        {"column_settings": money("netback"), "scalar.suffix": " /MMBtu"},
        "Gas delivered to an in-basin customer (rig, frac fleet) as a diesel substitute.",
    ),
    "kpi_hub": Card(
        "Netback selling into a pipeline (median)",
        "scalar",
        f"""SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY best_hub_netback_usd_mmbtu)::numeric, 2)
                   AS netback FROM mart.site_latest_scenario({OVERRIDES})""",
        {"column_settings": money("netback"), "scalar.suffix": " /MMBtu"},
        "Gas delivered to Waha, Agua Dulce or Katy and sold at Henry Hub + basis.",
    ),
    "kpi_value": Card(
        "Value vs flaring, per year",
        "scalar",
        f"""SELECT round(sum(greatest(best_annual_value_vs_flaring_usd, 0)) / 1e6, 1) AS usd_m
            FROM mart.site_latest_scenario({OVERRIDES})""",
        {"column_settings": money("usd_m", 1), "scalar.suffix": "M"},
        "If each site's whole flared volume went to its best option. Fuel demand is not capped.",
    ),
    "by_site": Card(
        "Every site: sell as fuel vs sell into a pipeline ($/MMBtu at the wellsite)",
        "bar",
        f"""SELECT site_name AS "Site",
                   best_fuel_netback_usd_mmbtu AS "Sell as fuel (in-basin customer)",
                   best_hub_netback_usd_mmbtu  AS "Sell into a pipeline hub"
            FROM mart.site_latest_scenario({OVERRIDES}) ORDER BY site_id""",
        {
            "graph.dimensions": ["Site"],
            "graph.metrics": ["Sell as fuel (in-basin customer)", "Sell into a pipeline hub"],
            "series_settings": {
                "Sell as fuel (in-basin customer)": {"color": GREEN},
                "Sell into a pipeline hub": {"color": GREY},
            },
            "graph.y_axis.title_text": "$/MMBtu",
            "graph.y_axis.auto_split": False,  # one shared axis: negative bars must look negative
            "graph.x_axis.title_text": "",
            **FLARE_LINE,
        },  # fmt: skip
        "Bars above the line beat flaring. PB = Permian Basin, EF = Eagle Ford.",
    ),
    "map": Card(
        "Where the gas is: flaring sites and delivery hubs",
        "map",
        f"""SELECT site_name AS site, lat, lon,
                   CASE WHEN best_netback_usd_mmbtu > 0 THEN 'Truck: ' || best_option ELSE 'Flare' END
                       AS recommendation,
                   best_netback_usd_mmbtu AS netback_usd_mmbtu
            FROM mart.site_latest_scenario({OVERRIDES})
            UNION ALL
            SELECT hub_name, lat, lon, 'Delivery hub', NULL FROM core.dim_hub WHERE is_delivery_point""",
        {
            "map.type": "pin",
            "map.latitude_column": "lat",
            "map.longitude_column": "lon",
            "map.center_latitude": 30.2,
            "map.center_longitude": -100.4,
            "map.zoom": 6,
        },
        "Satellite-detected flares (World Bank / NOAA VIIRS, 2025) that flared every year 2023-25.",
    ),
    "scorecard": Card(
        "Site scorecard",
        "table",
        f"""SELECT site_name AS "Site", basin AS "Basin",
                   round(flared_mmscfd, 2) AS "Flared (MMcf/d)",
                   CASE WHEN best_netback_usd_mmbtu > 0 THEN 'Truck: ' || best_option
                        ELSE 'Flare: nothing pays' END AS "Recommendation",
                   best_netback_usd_mmbtu AS "Netback $/MMBtu",
                   round(best_annual_value_vs_flaring_usd / 1e6, 1) AS "$M/yr vs flaring"
            FROM mart.site_latest_scenario({OVERRIDES}) ORDER BY site_id""",
        {
            "table.column_formatting": sign_colours("Netback $/MMBtu"),
            "column_settings": {**money("Netback $/MMBtu")},
        },  # fmt: skip
    ),
    # ── Economics ────────────────────────────────────────────────────────────
    "walk_fuel": Card(
        "Selling as fuel: where each $/MMBtu goes (chosen site, best mode)",
        "waterfall",
        f"""WITH best AS (
                SELECT * FROM mart.netback_daily_scenario({OVERRIDES}, {LATEST})
                WHERE site_name = {{{{site}}}} AND market = 'fuel'
                ORDER BY netback_usd_mmbtu DESC LIMIT 1)
            SELECT step AS "Step", amount AS "$/MMBtu" FROM best CROSS JOIN LATERAL (VALUES
                (1, 'Price', delivered_price_usd_mmbtu),
                (2, CASE mode WHEN 'CNG' THEN 'Compress' ELSE 'Liquefy' END, -conditioning_usd_mmbtu),
                (3, 'Trucking', -trucking_usd_mmbtu),
                (4, 'Delivery', -delivery_usd_mmbtu),
                (5, 'Fees', -fee_usd_mmbtu)) AS v(ord, step, amount)
            ORDER BY ord""",
        {
            "graph.dimensions": ["Step"],
            "graph.metrics": ["$/MMBtu"],
            "waterfall.show_total": True,
            "waterfall.increase_color": GREEN,
            "waterfall.decrease_color": RED,
            "waterfall.total_color": BLUE,
            "graph.y_axis.title_text": "$/MMBtu",
            "graph.x_axis.title_text": "",
        },  # fmt: skip
        "Price minus every cost = netback (blue). Pick the site with the Site filter.",
    ),
    "walk_hub": Card(
        "Selling into a pipeline: where each $/MMBtu goes (chosen site, best hub)",
        "waterfall",
        f"""WITH best AS (
                SELECT * FROM mart.netback_daily_scenario({OVERRIDES}, {LATEST})
                WHERE site_name = {{{{site}}}} AND market = 'hub'
                ORDER BY netback_usd_mmbtu DESC LIMIT 1)
            SELECT step AS "Step", amount AS "$/MMBtu" FROM best CROSS JOIN LATERAL (VALUES
                (1, 'Price', delivered_price_usd_mmbtu),
                (2, CASE mode WHEN 'CNG' THEN 'Compress' ELSE 'Liquefy' END, -conditioning_usd_mmbtu),
                (3, 'Trucking', -trucking_usd_mmbtu),
                (4, 'Delivery', -delivery_usd_mmbtu),
                (5, 'Fees', -fee_usd_mmbtu)) AS v(ord, step, amount)
            ORDER BY ord""",
        {
            "graph.dimensions": ["Step"],
            "graph.metrics": ["$/MMBtu"],
            "waterfall.show_total": True,
            "waterfall.increase_color": GREEN,
            "waterfall.decrease_color": RED,
            "waterfall.total_color": BLUE,
            "graph.y_axis.title_text": "$/MMBtu",
            "graph.x_axis.title_text": "",
        },  # fmt: skip
        "Henry Hub + basis is far below the cost of moving gas by truck.",
    ),
    "distance": Card(
        "How far can you haul? Netback vs one-way distance",
        "line",
        f"""SELECT one_way_mi AS "One-way miles", option_label AS "Option",
                   netback_usd_mmbtu AS "Netback $/MMBtu"
            FROM mart.distance_curve_scenario({OVERRIDES})
            WHERE option_label IN ('CNG → fuel customer', 'LNG → fuel customer',
                                   'CNG → Agua Dulce', 'CNG → Waha')
            ORDER BY 2, 1""",
        {
            "graph.dimensions": ["One-way miles", "Option"],
            "graph.metrics": ["Netback $/MMBtu"],
            "series_settings": {
                "CNG → fuel customer": {"color": GREEN},
                "LNG → fuel customer": {"color": "#86C7A5"},
                "CNG → Agua Dulce": {"color": GREY},
                "CNG → Waha": {"color": AMBER},
            },
            "graph.y_axis.title_text": "$/MMBtu",
            **FLARE_LINE,
        },  # fmt: skip
        "Where a line crosses the flaring line is that option's break-even distance.",
    ),
    "prices": Card(
        "Why fuel sales win: what an MMBtu is worth (weekly, EIA)",
        "line",
        """SELECT date_trunc('week', price_date)::date AS "Week",
                  round(avg(diesel_parity_usd_mmbtu), 2) AS "As a diesel substitute",
                  round(avg(henry_hub_usd_mmbtu), 2) AS "As pipeline gas (Henry Hub)"
           FROM mart.v_prices_daily WHERE price_date >= '2021-01-01'
           GROUP BY 1 ORDER BY 1""",
        {
            "graph.dimensions": ["Week"],
            "graph.metrics": ["As a diesel substitute", "As pipeline gas (Henry Hub)"],
            "series_settings": {
                "As a diesel substitute": {"color": GREEN},
                "As pipeline gas (Henry Hub)": {"color": GREY},
            },
            "graph.y_axis.title_text": "$/MMBtu",
            "graph.x_axis.title_text": "",
        },  # fmt: skip
        "Diesel parity is LHV-adjusted: engines burn on lower heating value, gas sells on higher.",
    ),
    "history": Card(
        "Has it ever paid? Best netback by market since 2021 (site average)",
        "line",
        f"""WITH best AS (
                SELECT price_date, site_id, market, max(netback_usd_mmbtu) AS netback
                FROM mart.netback_daily_scenario({OVERRIDES}, '2021-01-01')
                GROUP BY 1, 2, 3)
            SELECT date_trunc('week', price_date)::date AS "Week",
                   CASE market WHEN 'hub' THEN 'Sell into a pipeline hub'
                               ELSE 'Sell as fuel (in-basin customer)' END AS "Market",
                   round(avg(netback), 2) AS "Netback $/MMBtu"
            FROM best GROUP BY 1, 2 ORDER BY 1, 2""",
        {
            "graph.dimensions": ["Week", "Market"],
            "graph.metrics": ["Netback $/MMBtu"],
            "series_settings": {
                "Sell as fuel (in-basin customer)": {"color": GREEN},
                "Sell into a pipeline hub": {"color": GREY},
            },
            "graph.y_axis.title_text": "$/MMBtu",
            "graph.x_axis.title_text": "",
            **FLARE_LINE,
        },  # fmt: skip
        "Pipeline sales came close to paying only in 2022, when Henry Hub averaged $6.45.",
    ),
    "crossover": Card(
        "CNG or LNG? LNG is cheaper only beyond",
        "table",
        """SELECT CASE market WHEN 'hub' THEN 'Selling into a pipeline' ELSE 'Selling as fuel' END
                      AS "Market",
                  crossover_one_way_mi AS "One-way miles"
           FROM mart.v_mode_crossover ORDER BY 2""",
        description="LNG carries more per truck, but liquefying it costs about $7/MMBtu at this scale, so it only wins on very long hauls. Every route here is under 600 miles.",
    ),
    # ── Sensitivity ──────────────────────────────────────────────────────────
    "tornado_fuel": Card(
        "What moves the fuel-sale netback? (chosen site)",
        "row",
        """SELECT driver_label AS "Driver",
                  round(netback_at_low - base_netback, 2) AS "Driver at its low end",
                  round(netback_at_high - base_netback, 2) AS "Driver at its high end"
           FROM mart.sensitivity_tornado WHERE site_name = {{site}} AND market = 'fuel'
           ORDER BY swing DESC LIMIT 8""",
        {
            "graph.dimensions": ["Driver"],
            "graph.metrics": ["Driver at its low end", "Driver at its high end"],
            "stackable.stack_type": "stacked",
            "series_settings": {
                "Driver at its low end": {"color": AMBER},
                "Driver at its high end": {"color": BLUE},
            },
            "graph.y_axis.title_text": "Change in netback vs base case ($/MMBtu)",
        },  # fmt: skip
        "Each bar moves one input across its range with everything else at base case.",
    ),
    "tornado_hub": Card(
        "What moves the pipeline netback? (chosen site)",
        "row",
        """SELECT driver_label AS "Driver",
                  round(netback_at_low - base_netback, 2) AS "Driver at its low end",
                  round(netback_at_high - base_netback, 2) AS "Driver at its high end"
           FROM mart.sensitivity_tornado WHERE site_name = {{site}} AND market = 'hub'
           ORDER BY swing DESC LIMIT 8""",
        {
            "graph.dimensions": ["Driver"],
            "graph.metrics": ["Driver at its low end", "Driver at its high end"],
            "stackable.stack_type": "stacked",
            "series_settings": {
                "Driver at its low end": {"color": AMBER},
                "Driver at its high end": {"color": BLUE},
            },
            "graph.y_axis.title_text": "Change in netback vs base case ($/MMBtu)",
        },  # fmt: skip
        "Henry Hub and Waha basis use their 3-year P10-P90 and sourced ranges.",
    ),
    "break_evens": Card(
        "Break-even points by site (how much would have to change?)",
        "table",
        f"""SELECT site_name AS "Site", basin AS "Basin",
                   best_fuel_netback_usd_mmbtu AS "Fuel netback $/MMBtu",
                   round(100 * best_fuel_break_even_discount) AS "Fuel pays until discount of (%)",
                   best_hub_option AS "Best pipeline option",
                   best_hub_netback_usd_mmbtu AS "Pipeline netback $/MMBtu",
                   best_hub_break_even_henry_hub AS "Pipeline pays above Henry Hub of"
            FROM mart.site_latest_scenario({OVERRIDES}) ORDER BY site_id""",
        {
            "table.column_formatting": sign_colours(
                "Fuel netback $/MMBtu", "Pipeline netback $/MMBtu"
            ),
            "column_settings": {
                **money("Fuel netback $/MMBtu"),
                **money("Pipeline netback $/MMBtu"),
                **money("Pipeline pays above Henry Hub of"),
            },
        },  # fmt: skip
    ),
    # ── Data & assumptions ───────────────────────────────────────────────────
    "assumptions": Card(
        "Assumptions (every non-market input, 2025 dollars)",
        "table",
        """SELECT key AS "Assumption", round(value, 4) AS "Value", unit AS "Unit",
                  CASE basis WHEN 'rough_estimate' THEN 'Rough estimate'
                             ELSE initcap(basis) END AS "Basis",
                  source AS "Source"
           FROM mart.v_assumption ORDER BY (basis = 'rough_estimate') DESC, key""",
        {
            "table.column_formatting": [
                {
                    "id": 0,
                    "columns": ["Basis"],
                    "type": "single",
                    "operator": "=",
                    "value": "Rough estimate",
                    "color": AMBER,
                    "highlight_row": False,
                }
            ]
        },  # fmt: skip
        "Edit assumptions.yaml and run `make assumptions sensitivity` to change these.",
    ),
    "dq": Card(
        "Data-quality checks (latest run)",
        "table",
        """SELECT CASE WHEN passed THEN 'Pass' WHEN severity = 'error' THEN 'FAIL' ELSE 'Warn' END
                      AS "Status",
                  check_name AS "Check", observed AS "Observed", threshold AS "Threshold"
           FROM ops.v_dq_latest ORDER BY passed, check_name""",
        {
            "table.column_formatting": [
                {
                    "id": 0,
                    "columns": ["Status"],
                    "type": "single",
                    "operator": "=",
                    "value": "Pass",
                    "color": GREEN,
                    "highlight_row": False,
                },
                {
                    "id": 1,
                    "columns": ["Status"],
                    "type": "single",
                    "operator": "=",
                    "value": "Warn",
                    "color": AMBER,
                    "highlight_row": False,
                },
                {
                    "id": 2,
                    "columns": ["Status"],
                    "type": "single",
                    "operator": "=",
                    "value": "FAIL",
                    "color": RED,
                    "highlight_row": False,
                },
            ]
        },  # fmt: skip
    ),
}


def base_case_facts() -> dict:
    """Numbers quoted in the guide text, read from the database so the words never drift."""
    with connect() as conn:
        row = conn.execute("""
            SELECT l.price_date,
                   (SELECT henry_hub_usd_mmbtu FROM mart.v_prices_daily
                    WHERE price_date = l.price_date),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY best_fuel_netback_usd_mmbtu),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY best_hub_netback_usd_mmbtu),
                   count(*) FILTER (WHERE best_fuel_netback_usd_mmbtu > 0),
                   count(*) FILTER (WHERE best_hub_netback_usd_mmbtu > 0),
                   count(*),
                   min(best_hub_break_even_henry_hub), max(best_hub_break_even_henry_hub),
                   bool_and(best_option LIKE 'CNG%' AND best_hub_option LIKE 'CNG%')
            FROM mart.v_site_latest l GROUP BY l.price_date""").fetchone()
    keys = ("date", "henry_hub", "fuel", "hub", "fuel_wins", "hub_wins", "sites", "be_lo",
            "be_hi", "cng_everywhere")  # fmt: skip
    return dict(zip(keys, row, strict=True))


def usd(x) -> str:
    """-3.87 -> '−$3.87' (a minus sign before the dollar sign reads naturally)."""
    return f"{'−' if x < 0 else ''}${abs(float(x)):.2f}"


def guide_text(f: dict, link) -> dict[str, str]:
    """Every text card. `link(query, tab)` builds a relative link that applies a what-if."""
    hh, doubled = float(f["henry_hub"]), round(2 * float(f["henry_hub"]), 2)
    hub_verdict = (
        f"a loss at **all {f['sites']}** sites" if f["hub_wins"] == 0
        else f"better than flaring at only {f['hub_wins']} of {f['sites']} sites"
    )  # fmt: skip

    def nav(tab: str) -> str:
        return (f"\n\n**Lost?** [Reset to today's prices]({link('site=EF-02', tab)}) · "
                f"[Back to Start here]({link('site=EF-02', 'Start here')})")  # fmt: skip

    return {
        # ── Start here ───────────────────────────────────────────────────────
        "welcome": (
            "# Should this gas be trucked, or burned?\n"
            "Oil wells in West and South Texas also produce natural gas. Where no pipeline can "
            "take it, producers often **flare** it (burn it on site) and earn nothing. A "
            "**virtual pipeline** puts that gas on trucks instead and sells it somewhere it is "
            f"wanted. This dashboard works out, for **{f['sites']} real flares spotted by "
            "satellite**, whether that trip pays, using only public data."
        ),
        "answer": (
            f"### The short answer (prices of {f['date']:%d %b %Y})\n"
            "- **Sell the gas as fuel** to drilling rigs and frac crews nearby, in place of "
            f"diesel: the producer keeps about **{usd(f['fuel'])} per MMBtu**. "
            f"{f['fuel_wins']} of {f['sites']} sites beat flaring.\n"
            "- **Sell it into a pipeline** at a trading hub: about "
            f"**{usd(f['hub'])} per MMBtu**, {hub_verdict}. Pipeline gas is cheap "
            f"({usd(hh)}) and trucking it is not; it would take a gas price of "
            f"{usd(f['be_lo'])}–{usd(f['be_hi'])} to break even.\n"
            + (
                "- **CNG** (compressed gas) beats **LNG** (liquefied) on every route here.\n"
                if f["cng_everywhere"]
                else ""
            )
        ),
        "howto": (
            "### How to use it, in 3 steps\n"
            "1. **Open Overview** (tab above). It shows every site's answer at today's prices.\n"
            '2. **Ask "what if?"** Type a number into any box at the top of the page, e.g. '
            "*Henry Hub $/MMBtu* = 6. Every chart updates, and the *Inputs in use* table lists "
            "what you changed. Click **×** in a box to go back to today's prices.\n"
            "3. **Dig deeper.** *Economics* shows where each dollar goes for one site (choose it "
            "in the *Site* box). *Sensitivity* shows which assumptions could change the answer."
        ),
        "try": (
            "### Try a question (one click)\n"
            f"- [What if gas prices doubled, to ${doubled:.2f}?]({link(f'henry_hub={doubled}', 'Overview')})\n"
            f"- [What if customers want 70% off the diesel price?]({link('discount_pct=70', 'Overview')})\n"
            f"- [What if diesel fell to $3.50 a gallon?]({link('diesel=3.5', 'Overview')})\n"
            f"- [What if the nearest customer is 150 miles away?]({link('customer_mi=150', 'Overview')})\n"
            f"- [Where does the money go at a Permian site?]({link('site=PB-07', 'Economics')})\n"
            f"- [What could change the answer for that site?]({link('site=PB-07', 'Sensitivity')})"
        ),
        "legend": (
            "### Reading the charts\n"
            "- **Green** = earns more than flaring. **Red**, or grey below the dotted line = "
            "earns less.\n"
            "- The dotted **Flaring = $0** line is the alternative: burning gas earns nothing.\n"
            "- In a *money walk*, the **blue** bar is what the producer keeps.\n"
            "- **Amber** = something you changed, or an assumption that had to be estimated.\n"
            "- Site names: **PB-** = Permian Basin (West Texas / New Mexico), **EF-** = Eagle "
            "Ford (South Texas)."
        ),
        "glossary": (
            "### Words you will see\n"
            "- **MMBtu**: the unit gas is priced in (one million British thermal units, about "
            "960 cubic feet of gas). Every $ figure here is per MMBtu.\n"
            "- **Netback**: what the producer keeps per MMBtu after paying for every step: "
            "processing, trucking, delivery and fees. Flaring's netback is $0.\n"
            f"- **Henry Hub**: the US benchmark gas price, set in Louisiana. Latest: {usd(hh)}.\n"
            "- **Hub, basis**: pipeline trading points near the sites: *Waha* (Permian), *Agua "
            "Dulce* (South Texas), *Katy* (Houston). *Basis* is how far a hub trades below Henry "
            "Hub; Waha is about $2 below because Permian pipelines are full."
        ),
        "glossary2": (
            "### &nbsp;\n"
            "- **CNG / LNG**: gas squeezed to high pressure (CNG) or chilled to a liquid (LNG) so "
            "it fits on a truck. CNG is cheaper to make; LNG carries more per load.\n"
            "- **Diesel parity**: what the gas is worth as a diesel replacement, i.e. the cost of "
            "buying the same useful energy as diesel.\n"
            "- **Customer discount**: how far below diesel parity a fuel customer pays, so that "
            "switching from diesel is worth it to them.\n"
            "- **Break-even**: the gas price, distance or discount at which trucking earns exactly "
            "what flaring does ($0)."
        ),
        # ── per-tab reading guides ───────────────────────────────────────────
        "intro": (
            "### How to read this page\n"
            "1. **Inputs in use** (right) says which prices you are looking at: today's, or "
            "your what-if (amber).\n"
            "2. **The four numbers** are the headline answer.\n"
            "3. **The bar chart** has one pair of bars per site: **green** = sell as fuel "
            "nearby, **grey** = sell into a pipeline. Above the dotted line beats flaring.\n"
            "4. **The map and table** show where each site is and what it should do."
            + nav("Overview")
        ),
        "econ": (
            "### How to read this page\n"
            "Choose a site in the **Site** box at the top. Each *money walk* reads left to "
            "right: the green bar is the selling price, each red bar is a cost, and the **blue** "
            "bar is what the producer keeps. Below that: how distance changes the answer, why "
            "fuel is worth more than pipeline gas, and how the answer has moved since 2021."
            + nav("Economics")
        ),
        "sens": (
            "### How to read this page\n"
            "Each row is an assumption we cannot know exactly. Its bars show how much the "
            "producer's take would move if that assumption sat at the **low (amber)** or "
            "**high (blue)** end of its plausible range. Long bars matter; short ones do not. "
            "**None of them is big enough to change any site's answer.** The table shows how "
            "much would have to change to tip each site." + nav("Sensitivity")
        ),
        "data": (
            "### Where the numbers come from\n"
            "Prices: **EIA** (US Energy Information Administration): Henry Hub daily, Gulf Coast "
            "diesel weekly. Sites: **World Bank / NOAA** satellite flare estimates, inside EIA "
            "basin outlines. Driving routes: **OSRM** (OpenStreetMap). Costs: World Bank, ATRI, "
            "DOE, equipment makers. **Amber rows are estimates** where no public figure exists."
            + nav("Data & assumptions")
        ),
    }


TABS = ["Start here", "Overview", "Economics", "Sensitivity", "Data & assumptions"]
LAYOUT = {  # tab -> [(card or text key, col, row, width, height)]; the grid is 24 columns wide
    "Start here": [
        ("welcome", 0, 0, 24, 3),
        ("answer", 0, 3, 12, 4), ("howto", 12, 3, 12, 4),
        ("try", 0, 7, 12, 4), ("legend", 12, 7, 12, 4),
        ("glossary", 0, 11, 12, 5), ("glossary2", 12, 11, 12, 5),
    ],
    "Overview": [
        ("intro", 0, 0, 13, 5), ("scenario", 13, 0, 11, 5),
        ("kpi_sites", 0, 5, 6, 3), ("kpi_fuel", 6, 5, 6, 3), ("kpi_hub", 12, 5, 6, 3),
        ("kpi_value", 18, 5, 6, 3),
        ("by_site", 0, 8, 24, 7),
        ("map", 0, 15, 10, 11), ("scorecard", 10, 15, 14, 11),
    ],
    "Economics": [
        ("econ", 0, 0, 24, 3),
        ("walk_fuel", 0, 3, 12, 8), ("walk_hub", 12, 3, 12, 8),
        ("distance", 0, 11, 12, 8), ("prices", 12, 11, 12, 8),
        ("history", 0, 19, 16, 8), ("crossover", 16, 19, 8, 8),
    ],
    "Sensitivity": [
        ("sens", 0, 0, 24, 3),
        ("tornado_fuel", 0, 3, 12, 9), ("tornado_hub", 12, 3, 12, 9),
        ("break_evens", 0, 12, 24, 9),
    ],
    "Data & assumptions": [
        ("data", 0, 0, 24, 2),
        ("assumptions", 0, 2, 16, 13), ("dq", 16, 2, 8, 13),
    ],
}  # fmt: skip


class Metabase:
    def __init__(self, base: str):
        self.base, self.http = base, requests.Session()

    def call(self, method: str, path: str, **kw):
        resp = self.http.request(method, f"{self.base}/api/{path}", timeout=60, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} /api/{path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else None


def wait_healthy(mb: Metabase, seconds: int = 300) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if mb.call("GET", "health").get("status") == "ok":
                return
        except (requests.ConnectionError, RuntimeError):
            pass
        time.sleep(3)
    raise RuntimeError(
        "Metabase did not become healthy (is `docker compose --profile bi up` running?)"
    )


def login(mb: Metabase, email: str, password: str) -> None:
    props = mb.call("GET", "session/properties")
    if not props.get("has-user-setup"):
        log.info("first run: creating admin user %s", email)
        mb.call("POST", "setup", json={
            "token": props["setup-token"],
            "user": {"first_name": "VPV", "last_name": "Admin", "email": email,
                     "password": password, "site_name": "VPV Netback"},
            "prefs": {"site_name": "VPV Netback", "site_locale": "en", "allow_tracking": False},
        })  # fmt: skip
    session = mb.call("POST", "session", json={"username": email, "password": password})
    mb.http.headers["X-Metabase-Session"] = session["id"]


def ensure_database(mb: Metabase) -> int:
    for db in mb.call("GET", "database")["data"]:
        if db["name"] == "VPV netback (Postgres)":
            return db["id"]
    created = mb.call("POST", "database", json={
        "engine": "postgres", "name": "VPV netback (Postgres)",
        "details": {"host": "db", "port": 5432,  # compose service name, inside the docker network
                    "dbname": os.getenv("PGDATABASE", "vpv"), "user": os.getenv("PGUSER", "vpv"),
                    "password": os.getenv("PGPASSWORD", "vpv"), "ssl": False},
    })  # fmt: skip
    return created["id"]


def fresh_collection(mb: Metabase) -> int:
    """Archive the previous build so a re-run never leaves duplicate cards behind."""
    for c in mb.call("GET", "collection"):
        if c.get("name") == COLLECTION and not c.get("archived"):
            mb.call("PUT", f"collection/{c['id']}", json={"archived": True})
    return mb.call("POST", "collection", json={"name": COLLECTION, "color": "#1F6F5C"})["id"]


def site_names() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT site_name FROM core.dim_site ORDER BY site_id")]


def template_tag(slug: str) -> dict:
    if slug == "site":
        return {"id": "site", "name": "site", "display-name": "Site", "type": "text",
                "required": True, "default": DEFAULT_SITE}  # fmt: skip
    label = dict(SCENARIO_INPUTS)[slug]
    return {"id": slug, "name": slug, "display-name": label, "type": "number", "required": False}


def create_card(mb, db_id, collection_id, card: Card) -> int:
    native = {"query": card.sql, "template-tags": {t: template_tag(t) for t in card.tags}}
    created = mb.call("POST", "card", json={
        "name": card.name, "display": card.display, "collection_id": collection_id,
        "description": card.description or None,
        "dataset_query": {"type": "native", "native": native, "database": db_id},
        "visualization_settings": card.viz,
    })  # fmt: skip
    return created["id"]


def dashboard_parameters(sites: list[str]) -> list[dict]:
    params = [{"id": s, "name": label, "slug": s, "type": "number/=", "sectionId": "number"}
              for s, label in SCENARIO_INPUTS]  # fmt: skip
    params.append({
        "id": "site", "name": "Site", "slug": "site", "type": "string/=", "sectionId": "string",
        "default": [DEFAULT_SITE], "required": True, "values_query_type": "list",
        "values_source_type": "static-list", "values_source_config": {"values": sites},
    })  # fmt: skip
    return params


def text_card(dashcard_id: int, tab_id: int, text: str, col, row, w, h) -> dict:
    return {
        "id": dashcard_id, "card_id": None, "dashboard_tab_id": tab_id,
        "col": col, "row": row, "size_x": w, "size_y": h, "parameter_mappings": [],
        "visualization_settings": {"virtual_card": {"name": None, "display": "text",
                                                    "visualization_settings": {},
                                                    "dataset_query": {}, "archived": False},
                                   "text": text},
    }  # fmt: skip


def build_dashboard(mb, collection_id, card_ids, sites) -> dict:
    params = dashboard_parameters(sites)
    dash = mb.call("POST", "dashboard", json={
        "name": DASHBOARD, "collection_id": collection_id, "parameters": params,
        "description": "Public data only: EIA prices, World Bank/NOAA VIIRS flares, OSRM routes.",
    })  # fmt: skip
    # Pass 1: create the tabs, so their ids exist for the one-click links in the guide text.
    created = mb.call("PUT", f"dashboard/{dash['id']}", json={
        "tabs": [{"id": -(i + 1), "name": name} for i, name in enumerate(TABS)], "dashcards": [],
    })  # fmt: skip
    tab_id = {t["name"]: t["id"] for t in created["tabs"]}
    public_uuid = mb.call("POST", f"dashboard/{dash['id']}/public_link")["uuid"]

    def link(query: str, tab: str) -> str:
        # Metabase resolves a bare "?query" link against the site root (-> login page), so
        # point at the public dashboard path: works on any host, no login needed.
        slug = tab.lower().replace(" & ", "-").replace(" ", "-")
        return f"/public/dashboard/{public_uuid}?{query}&tab={tab_id[tab]}-{slug}"

    text = guide_text(base_case_facts(), link)
    # Pass 2: place every card on its tab.
    dashcards, next_id = [], -1
    for name in TABS:
        for key, col, row, w, h in LAYOUT[name]:
            if key in text:
                dashcards.append(text_card(next_id, tab_id[name], text[key], col, row, w, h))
            else:
                mappings = [{"parameter_id": t, "card_id": card_ids[key],
                             "target": ["variable", ["template-tag", t]]}
                            for t in CARDS[key].tags]  # fmt: skip
                dashcards.append({"id": next_id, "card_id": card_ids[key],
                                  "dashboard_tab_id": tab_id[name], "col": col, "row": row,
                                  "size_x": w, "size_y": h, "parameter_mappings": mappings})  # fmt: skip
            next_id -= 1
    tabs = [{"id": tab_id[name], "name": name} for name in TABS]
    mb.call("PUT", f"dashboard/{dash['id']}", json={
        "tabs": tabs, "dashcards": dashcards, "parameters": params, "width": "full",
    })  # fmt: skip
    return {**dash, "public_uuid": public_uuid}


def verify(mb: Metabase, card_ids: dict[str, int]) -> None:
    """Run every card (base case), plus one what-if; fail on any SQL error."""
    for key, card_id in card_ids.items():
        result = mb.call("POST", f"card/{card_id}/query", json={})
        if result.get("status") != "completed":
            raise RuntimeError(f"card {key!r} failed: {result.get('error', result)}")
        log.info("card %-13s %5d rows", key, result["row_count"])
    what_if = mb.call("POST", f"card/{card_ids['scenario']}/query", json={"parameters": [
        {"id": "henry_hub", "type": "number/=", "target": ["variable", ["template-tag", "henry_hub"]],
         "value": [6]}
    ]})  # fmt: skip
    row = what_if["data"]["rows"][0]  # Henry Hub row of "Inputs in use"
    if row[2] != "Your what-if":
        raise RuntimeError(f"what-if input was not applied: {row}")
    log.info("what-if check: %s = %s (%s)", *row)


def main() -> int:
    setup_logging()
    email, password = os.getenv("MB_ADMIN_EMAIL"), os.getenv("MB_ADMIN_PASSWORD")
    if not (email and password):
        log.error("set MB_ADMIN_EMAIL and MB_ADMIN_PASSWORD in .env")
        return 2
    mb = Metabase(BASE)
    wait_healthy(mb)
    login(mb, email, password)
    db_id = ensure_database(mb)
    mb.call("POST", f"database/{db_id}/sync_schema")
    mb.call("PUT", "setting/enable-public-sharing", json={"value": True})
    collection_id = fresh_collection(mb)
    card_ids = {key: create_card(mb, db_id, collection_id, card) for key, card in CARDS.items()}
    dash = build_dashboard(mb, collection_id, card_ids, site_names())
    verify(mb, card_ids)
    log.info("dashboard:        %s/dashboard/%s", BASE, dash["id"])
    log.info("public (no login): %s/public/dashboard/%s", BASE, dash["public_uuid"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
