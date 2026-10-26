"""Validate assumptions.yaml and load it into core.assumption (the file is the source of truth).

python -m ingestion.assumptions              # validate + load
python -m ingestion.assumptions --markdown   # print the assumptions table for the docs
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from ingestion.config import ROOT, setup_logging
from ingestion.db import connect

log = logging.getLogger("assumptions")
ASSUMPTIONS_PATH = ROOT / "assumptions.yaml"
BASES = {"sourced", "derived", "rough_estimate"}
FIELDS = ("key", "value", "value_text", "low", "high", "unit", "basis", "usd_year", "source",
          "url", "note")  # fmt: skip


def validate(entries: list[dict]) -> list[dict]:
    """Return entries with every field present; raise ValueError listing every problem."""
    problems, keys = [], set()
    for i, e in enumerate(entries):
        key = e.get("key", f"entry #{i}")
        unknown = set(e) - set(FIELDS)
        if unknown:
            problems.append(f"{key}: unknown fields {sorted(unknown)}")
        for required in ("key", "unit", "basis", "source"):
            if not e.get(required):
                problems.append(f"{key}: missing {required}")
        if key in keys:
            problems.append(f"{key}: duplicate key")
        keys.add(key)
        if e.get("basis") not in BASES:
            problems.append(f"{key}: basis must be one of {sorted(BASES)}")
        if ("value" in e) == ("value_text" in e):
            problems.append(f"{key}: give exactly one of value / value_text")
        value = e.get("value")
        if value is not None and not isinstance(value, int | float):
            problems.append(f"{key}: value must be a number")
        elif value is not None and not e.get("low", value) <= value <= e.get("high", value):
            problems.append(f"{key}: value {value} outside [low, high]")
        if e.get("basis") == "sourced" and e.get("value") is not None and not e.get("url"):
            problems.append(f"{key}: a sourced number needs a url")
    usd_years = {e["usd_year"] for e in entries if e.get("usd_year")}
    for year in usd_years - {2025}:
        if f"cpi_u_{year}" not in keys:
            problems.append(f"usd_year {year} is used but cpi_u_{year} is not defined")
    if problems:
        raise ValueError("assumptions.yaml is invalid:\n  " + "\n  ".join(problems))
    return [{field: e.get(field) for field in FIELDS} for e in entries]


def load(path: Path = ASSUMPTIONS_PATH) -> list[dict]:
    return validate(yaml.safe_load(path.read_text()))


def save(entries: list[dict]) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM core.assumption WHERE key <> ALL(%s)", ([e["key"] for e in entries],)
        )
        conn.cursor().executemany(
            """INSERT INTO core.assumption
                   (key, value, value_text, low, high, unit, basis, usd_year, source, url, note)
               VALUES (%(key)s, %(value)s, %(value_text)s, %(low)s, %(high)s, %(unit)s,
                       %(basis)s, %(usd_year)s, %(source)s, %(url)s, %(note)s)
               ON CONFLICT (key) DO UPDATE SET
                   value = EXCLUDED.value, value_text = EXCLUDED.value_text,
                   low = EXCLUDED.low, high = EXCLUDED.high, unit = EXCLUDED.unit,
                   basis = EXCLUDED.basis, usd_year = EXCLUDED.usd_year,
                   source = EXCLUDED.source, url = EXCLUDED.url, note = EXCLUDED.note,
                   loaded_at = now()""",
            entries,
        )
        conn.execute("ANALYZE core.assumption")  # keep planner estimates current


def to_markdown(entries: list[dict]) -> str:
    """The assumptions table used in the README, generated so it can never drift from the file."""
    label = {"sourced": "sourced", "derived": "derived", "rough_estimate": "**rough estimate**"}
    lines = ["| Assumption | Value | Range (sensitivity) | Unit | Basis | Source |",
             "|---|---|---|---|---|---|"]  # fmt: skip
    for e in entries:
        value = e["value_text"] if e["value"] is None else f"{e['value']:g}"
        rng = f"{e['low']:g} – {e['high']:g}" if e["low"] is not None else ""
        unit = e["unit"] + (f" ({e['usd_year']}$)" if e["usd_year"] else "")
        source = f"[{e['source']}]({e['url']})" if e["url"] else e["source"]
        lines.append(
            f"| `{e['key']}` | {value} | {rng} | {unit} | {label[e['basis']]} | {source} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markdown", action="store_true", help="print the table, don't load")
    args = parser.parse_args(argv)
    setup_logging()
    entries = load()
    if args.markdown:
        print(to_markdown(entries))
        return 0
    save(entries)
    counts = {b: sum(e["basis"] == b for e in entries) for b in sorted(BASES)}
    log.info("core.assumption: %d loaded %s", len(entries), counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
