"""A synthetic multi-day radar warehouse for the market-estimate tests (no store data).

Games, ids and numbers are invented. Each game has a latent strength per country that
drifts daily; its daily ranks come from sorting strength plus noise, and its true worldwide
Android installs per day are sum_c scale_c * rank_c^-b * exp(game effect). The Play counter
(`realInstalls`) is refreshed every k days per game like the real one (some never refresh,
some move by a few dozen installs while their ratings keep growing), and read once a day.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass

from studio_pulse.radar import store

START = dt.date(2026, 10, 1)


@dataclass
class World:
    days: list[dt.date]
    b_true: float
    scale: dict[str, float]
    n_games: int


def build(
    con,
    *,
    days: int = 35,
    n_games: int = 140,
    seed: int = 11,
    b_true: float = 0.8,
    scale: dict[str, float] | None = None,
    game_sd: float = 0.35,
) -> World:
    rng = random.Random(seed)
    scale = scale or {"us": 60000.0, "tr": 9000.0}
    countries = sorted(scale)
    strength = {(g, c): rng.gauss(0, 1) for g in range(n_games) for c in countries}
    gross = {g: rng.gauss(0, 1) for g in range(n_games)}
    effect = {g: rng.gauss(0, game_sd) for g in range(n_games)}
    period = {g: rng.choice([1, 2, 3, 4, 6]) for g in range(n_games)}
    phase = {g: rng.randrange(period[g]) for g in range(n_games)}
    never = {g for g in range(n_games) if rng.random() < 0.12}  # counter frozen
    partial = {g for g in range(n_games) if g not in never and rng.random() < 0.06}
    cum = {g: float(rng.randint(50_000, 5_000_000)) for g in range(n_games)}
    counter = dict(cum)
    day_list = [START + dt.timedelta(days=i) for i in range(days)]
    for i, day in enumerate(day_list):
        chart_rows, play_rows = [], []
        ranks: dict[tuple[int, str], int] = {}
        for c in countries:
            for g in range(n_games):
                strength[(g, c)] += rng.gauss(0, 0.12)
            order = sorted(
                range(n_games), key=lambda g, c=c: -(strength[(g, c)] + rng.gauss(0, 0.1))
            )
            for r, g in enumerate(order, start=1):
                ranks[(g, c)] = r
        fetched = dt.datetime.combine(day, dt.time(9, 0))
        lists = [(c, "topfreeapplications", ranks) for c in countries]
        gorder = sorted(range(n_games), key=lambda g: -(gross[g] + rng.gauss(0, 0.1)))
        lists.append(
            ("us", "topgrossingapplications", {(g, "us"): r for r, g in enumerate(gorder, 1)})
        )
        for c, chart, rk in lists:
            for g in range(n_games):
                r = rk[(g, c)]
                if r > 100:
                    continue
                chart_rows.append(
                    {
                        "snapshot_date": day,
                        "fetched_at": fetched,
                        "store": "apple",
                        "country": c,
                        "chart": chart,
                        "genre_id": "6014",
                        "requested_limit": 100,
                        "n_rows": 100,
                        "rank": r,
                        "app_id": str(990000000 + g),
                        "bundle_id": f"com.synth.game{g}",
                        "name": f"Synth Game {g}",
                        "developer": "Synthetic Studio",
                        "developer_id": "1",
                        "category_id": "6014",
                        "feed_updated": fetched,
                        "source_url": "synthetic",
                    }
                )
        for g in range(n_games):
            true = sum(scale[c] * ranks[(g, c)] ** (-b_true) for c in countries)
            cum[g] += true * math.exp(effect[g])
            if g not in never and (i - phase[g]) % period[g] == 0:
                # a lagging counter moves by a few dozen installs while the ratings keep
                # growing (seen live on 13-14 Nov 2026: +26 to +87 installs, +1,700 ratings)
                counter[g] = counter[g] + rng.randint(20, 90) if g in partial else cum[g]
            play_rows.append(
                {
                    "snapshot_date": day,
                    "fetched_at": fetched + dt.timedelta(minutes=rng.randint(0, 50)),
                    "store": "play",
                    "country": "us",
                    "app_id": f"com.synth.game{g}",
                    "bundle_id": f"com.synth.game{g}",
                    "name": f"Synth Game {g}",
                    "real_installs": int(counter[g]),
                    "min_installs": 10_000,
                    "installs_bucket": "10,000+",
                    "rating_count": int(0.012 * cum[g]),
                    "version": f"1.{i // 10}",
                    "current_version_date": START + dt.timedelta(days=(i // 10) * 10),
                    "source": "play_details",
                }
            )
        store.insert(con, "raw_mr_chart", chart_rows)
        store.insert(con, "raw_mr_app", play_rows)
    return World(day_list, b_true, scale, n_games)
