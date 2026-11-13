"""`pulse demo`: one synthetic market-radar day, so the Studio's market view has data.

Every game, developer, rank and install bucket here is invented; the facts file has
the radar's shape (radar/digest.py) so the Studio and /radar read it like a real run,
and its narration is a fixed text that only states numbers from these facts. Store
links are left out on purpose: nothing here points at a real listing.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

PUZZLE, CASUAL, SIM = (
    "topfreeapplications:7012",
    "topfreeapplications:7003",
    "topfreeapplications:7015",
)

# name, developer, class, (country, list, rank), age days, Play bucket, mechanic, source, quote
GAMES: list[tuple[str, str, str, tuple[str, str, int], int, str | None, str, str, str]] = [
    ("Teacup Tumble", "Puddlelight Studio", "breakout", ("us", PUZZLE, 6), 21, "500,000+",
     "goods-sort", "llm", "sort the teacups onto the right shelves"),
    ("Bolt & Bloom", "Brassbell Games", "breakout", ("gb", PUZZLE, 9), 34, "1,000,000+",
     "screw-pin", "llm", "unscrew the pins in the right order"),
    ("Inkwell Words", "Kitefield Labs", "breakout", ("tr", PUZZLE, 4), 15, "50,000+",
     "word", "llm", "find the hidden words on every page"),
    ("Pocket Orchard Merge", "Saltpine Studio", "breakout", ("de", CASUAL, 12), 44, "100,000+",
     "merge", "llm", "merge fruit to grow your orchard"),
    ("Jamboree Bus", "Northgale Interactive", "breakout", ("us", CASUAL, 18), 27, "1,000,000+",
     "jam", "llm", "clear the parking jam one bus at a time"),
    ("Glassjar Colors", "Mintmoth Games", "breakout", ("tr", PUZZLE, 11), 9, "10,000+",
     "color-sort", "keyword", "Sort"),
    ("Tiny Tram Tycoon", "Copperleaf Games", "breakout", ("de", SIM, 5), 52, "500,000+",
     "idle-tycoon", "keyword", "Tycoon"),
    ("Lumen Blocks", "Bramblewick", "breakout", ("gb", CASUAL, 22), 40, "100,000+",
     "block-puzzle", "llm", "fill rows and columns to clear the board"),
    ("Marble Mahjong Garden", "Dovetail Play", "breakout", ("us", PUZZLE, 31), 58, "1,000,000+",
     "solitaire-mahjong", "llm", "match pairs of marble tiles"),
    ("Sprout Dash", "Puddlelight Studio", "breakout", ("tr", CASUAL, 27), 12, None,
     "runner", "keyword", "run"),
    ("Velvet Kitchen Rush", "Saltpine Studio", "late_riser", ("us", CASUAL, 14), 96, "5,000,000+",
     "cooking-sim", "llm", "serve every order before the timer runs out"),
    ("Paperboat Match", "Kitefield Labs", "late_riser", ("gb", PUZZLE, 16), 88, "1,000,000+",
     "match-3", "llm", "match three paper boats to sail on"),
    ("Quiet Lighthouse", "Bramblewick", "late_riser", ("de", PUZZLE, 19), 104, "500,000+",
     "hidden-object", "llm", "find the hidden objects in each room"),
    ("Copper Kettle Inn", "Copperleaf Games", "watch", ("tr", SIM, 33), 65, "100,000+",
     "idle-tycoon", "keyword", "Tycoon"),
    ("Wobble Tower Draw", "Mintmoth Games", "watch", ("us", PUZZLE, 44), 30, "50,000+",
     "physics-draw", "llm", "draw a line to keep the tower standing"),
]  # fmt: skip

# games per mechanic on today's charts, by country (the "mechanics" panel)
TREND_ROWS: dict[str, dict[str, int]] = {
    "merge": {"us": 9, "tr": 7, "gb": 8, "de": 7},
    "match-3": {"us": 7, "tr": 5, "gb": 6, "de": 6},
    "goods-sort": {"us": 6, "tr": 5, "gb": 4, "de": 4},
    "word": {"us": 4, "tr": 6, "gb": 4, "de": 3},
    "color-sort": {"us": 4, "tr": 5, "gb": 3, "de": 3},
    "solitaire-mahjong": {"us": 5, "tr": 2, "gb": 4, "de": 3},
    "block-puzzle": {"us": 3, "tr": 3, "gb": 3, "de": 3},
    "idle-tycoon": {"us": 2, "tr": 3, "gb": 2, "de": 3},
}

NARRATION = {
    "en": (
        "Teacup Tumble (goods sort) is today's strongest new riser: #6 in US Puzzle at 21 days "
        "old. Inkwell Words reached #4 in Turkey Puzzle at 15 days. Merge is the most common "
        "mechanic on today's charts (31 games across the four countries), ahead of match-3 (24)."
    ),
    "tr": (
        "Teacup Tumble (eşya sıralama) bugünün en güçlü yeni yükseleni: 21 günlükken ABD "
        "Bulmaca listesinde 6. sırada. Inkwell Words 15 günde Türkiye Bulmaca listesinde 4. "
        "sıraya çıktı. Bugünkü listelerde en yaygın mekanik birleştirme (dört ülkede 31 oyun); "
        "onu üçlü eşleştirme izliyor (24)."
    ),
}


def facts(as_of: dt.date) -> dict[str, Any]:
    from ..radar.taxonomy import load

    tax = load()
    items: list[dict[str, Any]] = []
    ideas: list[dict[str, Any]] = []
    for n, (name, dev, cls, (country, lst, rank), age, play, mech, src, quote) in enumerate(GAMES):
        key = f"demo:{n + 1}"
        items.append(
            {
                "app_key": key,
                "app_id": str(n + 1),
                "name": name,
                "developer": dev,
                "class": cls,
                "store_url": None,
                "play_url": None,
                "ranks": [{"country": country, "list": lst, "rank": rank}],
                "momentum": {"value": round(1.0 - n * 0.05, 2), "basis": "estimate"},
                "age": {"value": age, "basis": "observed"},
                "play": {"bucket": play, "basis": "store_reported"} if play else None,
                "profile": {
                    "source": src,
                    "claims": [
                        {
                            "dimension": "mechanic_primary",
                            "value": mech,
                            "quote": quote,
                            "field": "description",
                            "source": src,
                        }
                    ],  # fmt: skip
                },
            }
        )
        ideas.append(
            {
                "app_key": key,
                "name": name,
                "class": cls,
                "mechanic": mech,
                "quote": quote,
                "source": src,
                "forge_mechanic": tax.forge(mech),
                "hooks": tax.hooks(mech),
            }
        )
    rows = [
        {"country": c, "mechanic": m, "n_apps": k, "is_trend": False}
        for m, by in TREND_ROWS.items()
        for c, k in by.items()
    ]
    top = max(TREND_ROWS, key=lambda m: sum(TREND_ROWS[m].values()))
    ideas.insert(0, {"kind": "top_mechanic", "mechanic": top,
                     "count": {"value": sum(TREND_ROWS[top].values()), "basis": "observed"}})  # fmt: skip
    return {
        "as_of": as_of.isoformat(),
        "synthetic": True,
        "countries": ["us", "tr", "gb", "de"],
        "items": items,
        "trends": {"history_days": {"value": 9}, "week": as_of.isoformat(), "rows": rows},
        "ideas": ideas,
        "meta": {"narration": {"status": "ok", **NARRATION}},
    }


def write(out_dir: Path, as_of: dt.date) -> Path:
    """<out_dir>/radar/<as_of>.facts.json, where the radar's default out_dir looks."""
    d = out_dir / "radar"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.facts.json"):
        old.unlink()
    path = d / f"{as_of.isoformat()}.facts.json"
    path.write_text(json.dumps(facts(as_of), ensure_ascii=False, indent=1), encoding="utf-8")
    return path
