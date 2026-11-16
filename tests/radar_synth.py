"""A synthetic multi-day radar warehouse for the market-estimate tests (no store data).

Games, ids and numbers are invented. Each game has a latent strength per country that
drifts daily; its daily ranks come from sorting strength plus noise, and its true worldwide
Android installs per day are sum_c scale_c * rank_c^-b * exp(game effect). The Play counter
(`realInstalls`) is refreshed every k days per game like the real one (some never refresh,
some move by a few dozen installs while their ratings keep growing), and read once a day.
"""

from __future__ import annotations

import datetime as dt
import json
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


# -- new launches (fct_mr_launches, /radar/yeni, the shareable deck) ----------------------------

LAUNCH_DAY = dt.date(2026, 11, 15)
SYNTH_PUBLISHER = "1999000001"  # an invented artistId (never requested: nothing is fetched)
OTHER_DEVELOPER = "1999000002"  # the developer of the chart-only games
CDN = "https://is1-ssl.mzstatic.com/image/thumb/Synthetic/v4"


def _icon(app: str) -> str:
    return f"{CDN}/{app}/AppIcon/512x512bb.jpg"


def _shots(app: str, n: int = 4) -> list[str]:
    return [f"{CDN}/{app}/shot{i}/392x696bb.jpg" for i in range(1, n + 1)]


# app id -> (name, genre, genres, release offset in days from LAUNCH_DAY, icon, screenshots)
LAUNCH_APPS = {
    "990100001": ("Synth Pop Sort", "Games", ["Games", "Puzzle"], -2),
    "990100002": ("Synth Block Jam", "Games", ["Games", "Puzzle"], -5),
    "990100003": ("Synth Tower Run", "Games", ["Games", "Action"], -3),
    "990100004": ("Synth Feed Hero", None, None, None),  # no Lookup row: the feed's date only
    "990100005": ("Synth Old Classic", "Games", ["Games", "Casual"], -400),
    "990100006": ("Synth Soft Launch", "Games", ["Games", "Casual"], -3),  # us: -100
    "990100007": ("Synth Coming Soon", "Games", ["Games", "Puzzle"], 10),  # a pre-order
    "990100008": ("Synth Photo Tool", "Photo & Video", ["Photo & Video"], -1),  # not a game
    "990100009": ("Synth Month Old", "Games", ["Games", "Casual"], -22),
    "990100010": ("Synth Bad Links", "Games", ["Games", "Casual"], -4),
    "990100099": ("Synth Evergreen", "Games", ["Games"], -900),  # charted every day
}


def build_launches(con, day: dt.date = LAUNCH_DAY) -> None:
    """Charts, Lookup and publisher-catalogue rows for the launch tests (all invented).

    On `day` (window 14 days): Pop Sort (publisher only), Block Jam (publisher and charts in two
    storefronts), Tower Run (chart only, Lookup date), Feed Hero (chart only, feed date), Bad
    Links (publisher only; its icon and one screenshot are not Apple CDN URLs) are launches.
    Old Classic (a first chart entry 400 days after release), Soft Launch (released 100 days
    ago in the US), Coming Soon (pre-order) and Photo Tool (not a game) are not; Month Old was
    a launch 20 days earlier."""
    d = lambda n: day + dt.timedelta(days=n)  # noqa: E731

    def chart(snap, cc, ranks, feed=None):
        fetched = dt.datetime.combine(snap, dt.time(9, 0))
        store.insert(
            con,
            "raw_mr_chart",
            [
                {
                    "snapshot_date": snap, "fetched_at": fetched, "store": "apple",
                    "country": cc, "chart": "topfreeapplications", "genre_id": "6014",
                    "requested_limit": 100, "n_rows": 100, "rank": r, "app_id": a,
                    "bundle_id": f"com.synth.{a}", "name": LAUNCH_APPS[a][0],
                    "developer": "Synthetic Charts Ltd", "developer_id": OTHER_DEVELOPER,
                    "release_date_feed": (feed or {}).get(a), "category_id": "6014",
                    "feed_updated": fetched, "source_url": "synthetic",
                }
                for a, r in ranks.items()
            ],
        )  # fmt: skip

    def app(snap, cc, a, source, *, release=None, icon=None, shots=None, dev_id=None):
        name, genre, genres, offset = LAUNCH_APPS[a]
        rel = release if release is not None else (d(offset) if offset is not None else None)
        store.insert(
            con,
            "raw_mr_app",
            [
                {
                    "snapshot_date": snap, "fetched_at": dt.datetime.combine(snap, dt.time(9, 5)),
                    "store": "apple", "country": cc, "app_id": a, "bundle_id": f"com.synth.{a}",
                    "name": name, "developer": "Synthetic Publisher Ltd",
                    "developer_id": dev_id or SYNTH_PUBLISHER, "release_date": rel,
                    "primary_genre": genre, "genres": json.dumps(genres),
                    "rating_count": 10, "description": f"{name}: a synthetic puzzle game.",
                    "screenshot_urls": json.dumps(_shots(a) if shots is None else shots),
                    "icon_url": _icon(a) if icon is None else icon,
                    "store_url": f"https://apps.apple.com/us/app/id{a}", "source": source,
                }
            ],
        )  # fmt: skip

    # the radar observed the US list for a week before (so first chart entries are known)
    for n in range(-7, 0):
        chart(d(n), "us", {"990100099": 1})
    for n in range(-7, 1):
        app(d(n), "us", "990100099", "lookup", dev_id=OTHER_DEVELOPER)
    # publisher catalogue read 20 days ago, yesterday and today
    for snap in (d(-20), d(-1), d(0)):
        for a in ("990100001", "990100002", "990100007", "990100008", "990100009", "990100010"):
            if a == "990100001" and snap < d(-2):
                continue  # not released yet 20 days ago
            if a in ("990100002", "990100010") and snap == d(-20):
                continue
            if a == "990100010":
                app(snap, "us", a, "publisher", icon="http://evil.example/icon.png",
                    shots=["javascript:alert(1)", _shots(a)[0], "https://evil.example/s.jpg"])  # fmt: skip
            else:
                app(snap, "us", a, "publisher")
    # charts: yesterday and today
    chart(d(-1), "us", {"990100099": 1, "990100002": 40})
    chart(d(0), "us", {"990100099": 1, "990100002": 30, "990100003": 80, "990100004": 90,
                       "990100005": 95, "990100006": 70}, feed={"990100004": d(-6)})  # fmt: skip
    chart(d(0), "tr", {"990100002": 12, "990100006": 50})
    # Lookup rows of the charted apps (tr and us); Soft Launch's US release is 100 days ago
    for cc, ids in (("us", ["990100002", "990100003", "990100005", "990100006"]),
                    ("tr", ["990100002", "990100006"])):  # fmt: skip
        for a in ids:
            dev = None if a == "990100002" else OTHER_DEVELOPER  # Block Jam is the publisher's
            if a == "990100006" and cc == "us":
                app(d(0), cc, a, "lookup", release=d(-100), dev_id=dev)
            else:
                app(d(0), cc, a, "lookup", dev_id=dev)
    # a keyword profile for Block Jam (the mechanic tag the page filters on)
    store.insert(
        con,
        "raw_mr_profile",
        [
            {
                "app_key": "apple:990100002", "profile_id": "keyword:apple:990100002:x",
                "source": "keyword", "analyzed_at": dt.datetime.combine(day, dt.time(10)),
                "input_sha256": "x", "dimension": dim, "value": val,
                "field": "apple.description", "quote": q, "visual": False,
            }
            for dim, val, q in (("mechanic_primary", "jam", "block jam"),
                                ("mechanic", "block-puzzle", "block puzzle"))
        ],
    )  # fmt: skip
