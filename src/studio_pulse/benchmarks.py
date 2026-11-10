"""Public retention benchmarks shown next to each game's own numbers.

Source: GameAnalytics, "Mobile and PC Game Benchmarks 2026" - 16,262 mobile
games with at least 1,000 MAU, iOS and Android combined, all genres. Summary
used: https://gamedevreports.substack.com/p/gameanalytics-mobile-and-pc-game

Values are kept as the summary words them ("just under 4%", "6-7%"); `cut` is
the number a game has to reach to be placed in that bracket (the middle of a
range). The summary gives no median D1 and no top-10% D30, so those are left
out rather than guessed. These are game benchmarks: they are not drawn for
apps whose genre is not a game genre.
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE = "GameAnalytics, Mobile and PC Game Benchmarks 2026 (16,262 mobile games with at least 1,000 MAU, all genres)"
SOURCE_URL = "https://gamedevreports.substack.com/p/gameanalytics-mobile-and-pc-game"

NON_GAME_GENRES = frozenset({"app", "fitness", "health", "education", "productivity", "utility", "lifestyle"})


@dataclass(frozen=True)
class Band:
    label: str  # "median" | "top 25%" | "top 10%"
    cut: float  # retention needed to be in this bracket
    text: str  # as worded in the source


RETENTION: dict[int, tuple[Band, ...]] = {
    1: (Band("top 25%", 0.30, "just above 30%"), Band("top 10%", 0.40, "40%")),
    7: (Band("median", 0.039, "just under 4%"), Band("top 25%", 0.065, "6-7%"), Band("top 10%", 0.115, "11-12%")),
    30: (Band("median", 0.0074, "0.69-0.79%"), Band("top 25%", 0.017, "about 1.6-1.8%")),
}


def applies_to(genre: str | None) -> bool:
    return (genre or "app").lower() not in NON_GAME_GENRES


def bracket(day_n: int, value: float | None) -> str | None:
    """Where `value` sits among the benchmark cuts for day N, e.g. "top 25%"."""
    bands = RETENTION.get(day_n)
    if value is None or not bands:
        return None
    reached = [b for b in bands if value >= b.cut]
    if reached:
        top = reached[-1]
        return "above median" if top.label == "median" else top.label
    return "below median" if bands[0].label == "median" else f"below {bands[0].label}"


def summary_text() -> str:
    parts = []
    for n, bands in RETENTION.items():
        parts.append(f"D{n} " + ", ".join(f"{b.label} {b.text}" for b in bands))
    return "; ".join(parts)
