"""`market_trends.yaml` for creative-forge's `forge ideate` retrieval library.

Schema (studio-pulse/market_trends v1; docs/RADAR.md):

    version: 1
    schema: studio-pulse/market_trends/v1
    generated_at: 2026-11-12T09:30:00Z
    as_of: 2026-11-12
    source: studio-pulse market radar (Apple charts, iTunes Lookup, Google Play details)
    countries: [us, tr, gb, de]
    lessons:                       # creative-forge reads id/title/text (library._entries)
      - id: MT-2026-11-12-goods-sort
        title: "Market radar 2026-11-12: goods-sort is rising (2 games)"
        text: "<evidence-backed summary; every number carries its label>"
        kind: market_trend
        mechanic: goods-sort       # radar taxonomy value
        mechanic_shown: goods-sort # creative-forge tag_taxonomy value
        gamerefinery: Other puzzle
        themes: [food-cooking]
        twists: [order-fulfil]
        hooks: [satisfying-clean, before-after, near-miss]   # suggestion (inference)
        apps: [{app_key: apple:6811006356, name: Foodie Pop!, class: breakout}]
        evidence: [rss:us:topfreeapplications:6014#90@2026-11-12, ...]
        signal: daily_breakout     # or weekly_trend
        basis: {ranks: observed, ratings: observed, installs: store_reported, hooks: inference}

Entries are written in English because forge ideate prompts its model in English.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from .taxonomy import Taxonomy

HEADER = (
    "# Written by studio-pulse's market radar (pulse radar run). Read by creative-forge\n"
    "# `forge ideate` (search_creative_library) as kind market_trend. Local file: it holds\n"
    "# store-derived data, do not publish it.\n"
)


def _first(prof: dict[str, Any], dim: str) -> dict[str, Any] | None:
    return next((c for c in prof["claims"] if c["dimension"] == dim), None)


def _src(item: dict[str, Any]) -> str:
    """ "llm" when a model's quote-checked profile assigned the mechanic, else "keyword"."""
    return "llm" if item["profile"].get("source") == "llm" else "keyword"


def build(facts: dict[str, Any], tax: Taxonomy) -> dict[str, Any]:
    day = facts["as_of"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in facts["items"]:
        prim = _first(it["profile"], "mechanic_primary")
        if prim:
            groups.setdefault(prim["value"], []).append(it)
    lessons: list[dict[str, Any]] = []
    for mech, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        themes = sorted(
            {c["value"] for i in items for c in i["profile"]["claims"] if c["dimension"] == "theme"}
        )
        twists = sorted(
            {c["value"] for i in items for c in i["profile"]["claims"] if c["dimension"] == "twist"}
        )
        parts = []
        for i in items:
            ranks = ", ".join(
                f"{r['country'].upper()} {r['list']} #{r['rank']}" for r in i["ranks"][:3]
            )
            q = _first(i["profile"], "mechanic_primary")
            bit = (
                f"{i['name']} ({i['developer']}; radar class {i['class']}; charts {ranks} "
                f"[observed]; age {i['age']['value']} days [observed]"
            )
            if i.get("play") and i["play"].get("bucket"):
                bit += f"; Google Play {i['play']['bucket']} installs [store-reported]"
            how = "LLM, quote checked" if _src(i) == "llm" else "keyword guess"
            if q and q.get("quote"):
                bit += f'; mechanic: {how}, store text says "{q["quote"]}"'
            else:
                bit += f"; mechanic: {how}"
            parts.append(bit + ")")
        n = len(items)
        srcs = {_src(i) for i in items}
        tags = "llm" if srcs == {"llm"} else "keyword" if "llm" not in srcs else "mixed"
        caveat = {
            "llm": "",
            "mixed": "Some mechanic tags are keyword guesses, not checked by a model. ",
            "keyword": "Every mechanic tag here is a keyword guess from store text, not checked by "
            "a model: treat the grouping as a lead to verify. ",
        }[tags]
        lessons.append(
            {
                "id": f"MT-{day}-{mech}",
                "title": f"Market radar {day}: {mech} is rising ({n} new game{'s' if n != 1 else ''})"
                + (" (keyword tags only)" if tags == "keyword" else ""),
                "text": (
                    f"Market signal from public store charts on {day}: new games with {mech} as the "
                    f"primary mechanic are rising: "
                    + "; ".join(parts)
                    + ". "
                    + caveat
                    + (f"Twists seen in their store text: {', '.join(twists)}. " if twists else "")
                    + (f"Themes: {', '.join(themes)}. " if themes else "")
                    + f"Suggested hooks (inference, not data): {', '.join(tax.hooks(mech))}. "
                    "Use this as a direction for a creative test, not as proof of performance."
                ),
                "kind": "market_trend",
                "mechanic": mech,
                "mechanic_shown": tax.forge(mech),
                "gamerefinery": tax.gamerefinery(mech),
                "themes": themes,
                "twists": twists,
                "hooks": tax.hooks(mech),
                "apps": [
                    {"app_key": i["app_key"], "name": i["name"], "class": i["class"]} for i in items
                ],
                "evidence": [e for i in items for e in i["evidence"][:3]],
                "signal": "daily_breakout",
                "tags": tags,  # llm | keyword | mixed: how the mechanic was assigned
                "basis": {
                    "mechanic": {"llm": "llm_quote", "keyword": "keyword_guess", "mixed": "mixed"}[
                        tags
                    ],
                    "ranks": "observed",
                    "ratings": "observed",
                    "installs": "store_reported",
                    "hooks": "inference",
                },
            }
        )
    for r in facts["trends"]["rows"]:
        if not r["is_trend"]:
            continue
        mech = r["mechanic"]
        lessons.append(
            {
                "id": f"MT-{facts['trends']['week']}-{r['country']}-{mech}-weekly",
                "title": f"Market radar week {facts['trends']['week']}: {mech} share up in "
                f"{r['country'].upper()}",
                "text": (
                    f"Weekly chart share of {mech} games in {r['country'].upper()} top free games "
                    f"rose to {r['share'] * 100:.1f}% from a 4-week mean of "
                    f"{r['prior_mean'] * 100:.1f}% (z={r['z']}, {r['n_apps']} games) [observed]. "
                    f"Suggested hooks (inference): {', '.join(tax.hooks(mech))}."
                ),
                "kind": "market_trend",
                "mechanic": mech,
                "mechanic_shown": tax.forge(mech),
                "gamerefinery": tax.gamerefinery(mech),
                "themes": [],
                "twists": [],
                "hooks": tax.hooks(mech),
                "apps": [],
                "evidence": [f"trend:{r['country']}:{mech}@{facts['trends']['week']}"],
                "signal": "weekly_trend",
                "basis": {"share": "observed", "hooks": "inference"},
            }
        )
    return {
        "version": 1,
        "schema": "studio-pulse/market_trends/v1",
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "as_of": day,
        "source": "studio-pulse market radar (Apple charts, iTunes Lookup, Google Play details)",
        "countries": facts["countries"],
        "lessons": lessons,
    }


def write(doc: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110)
    path.write_text(text, encoding="utf-8")
    return path
