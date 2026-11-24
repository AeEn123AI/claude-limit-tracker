#!/usr/bin/env python3
"""Write the current estimates into docs/index.html's OpenGraph / Twitter meta tags,
so link previews (Discord, etc.) show the latest numbers. Run after collect.py.

Only the block between <!-- og:start --> and <!-- og:end --> is touched.
"""
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data" / "estimates.json"
INDEX = ROOT / "docs" / "index.html"
DAYS_PER_MONTH = 30.44
START, END = "<!-- og:start -->", "<!-- og:end -->"


def pretty_model(m: str) -> str:
    # claude-opus-5-5 -> Opus 5.5
    parts = m.replace("claude-", "").split("-")
    if len(parts) >= 2 and all(p.isdigit() for p in parts[1:]):
        return parts[0].capitalize() + " " + ".".join(parts[1:])
    return m


def usd(x: float) -> str:
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def tok(x: float) -> str:
    return f"{x / 1e6:,.0f}M" if x >= 1e7 else f"{x / 1e6:,.1f}M"


def build(d: dict) -> tuple[str, str]:
    bars = d.get("bars", {})
    plan, price = d.get("plan", "Claude"), d.get("plan_price") or 20
    bits, ref = [], None
    month = None
    for key, name in (("five_hour", "5-hour"), ("seven_day", "Weekly")):
        cur = (bars.get(key) or {}).get("current")
        if not cur:
            continue
        e = cur.get("adj") or cur.get("est")
        ref = ref or (bars[key].get("ref_model") if cur.get("adj") else None)
        bits.append(f"{name} 100% ≈ {usd(e['usd']['median'])} ({tok(e['wtok']['median'])} weighted tokens)")
        m = e["usd"]["median"] * (DAYS_PER_MONTH / 7 if key == "seven_day" else 24 / 5 * DAYS_PER_MONTH)
        month = m if month is None else min(month, m)
    gen = d.get("generated_at", "")
    try:
        day = datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%d %b %Y")
    except ValueError:
        day = datetime.now(timezone.utc).strftime("%d %b %Y")
    unit = f" in {pretty_model(ref)} API-equivalent" if ref else " API-equivalent"
    desc = f"{plan} limits{unit}: " + "; ".join(bits) + "."
    if month:
        desc += f" Maxed every week ≈ {usd(month)}/month on the API vs ${price:g} for the plan."
    desc += f" Updated {day}."
    title = "Claude limit tracker: how big is 100% of the bar?"
    return title, desc


def main() -> int:
    d = json.loads(DATA.read_text())
    title, desc = build(d)
    t, de = html.escape(title, quote=True), html.escape(desc, quote=True)
    block = "\n".join([
        START,
        f'<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="Claude limit tracker">',
        f'<meta property="og:title" content="{t}">',
        f'<meta property="og:description" content="{de}">',
        f'<meta name="twitter:card" content="summary">',
        f'<meta name="twitter:title" content="{t}">',
        f'<meta name="twitter:description" content="{de}">',
        f'<meta name="theme-color" content="#2a78d6">',
        END,
    ])
    src = INDEX.read_text()
    if START in src and END in src:
        new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, src, flags=re.S)
    else:
        new = src.replace("<link rel=\"stylesheet\"", block + "\n<link rel=\"stylesheet\"", 1)
    if new != src:
        INDEX.write_text(new)
    print(desc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
