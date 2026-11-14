"""Watchlist and the daily collection the estimates need, through the radar's polite fetcher.

- `pulse radar watch add|remove|list <apple id | play package | store URL>`: watched games
  are looked up every day in every configured storefront (one batched Lookup per country)
  and their Play page is read, even when they are not charted.
- Calibration panel: Play details pages of up to `panel_size` charted games per day,
  stratified over rank bands of the top-free lists (1-10, 11-25, 26-50, 51-100), games
  already in the panel first, so the install counter of the same games is read daily and
  the download curves get windows across the whole rank range. It only uses Play budget
  left after the radar's own candidates and keeps `play_reserve` requests for a re-run.
Everything goes through Fetcher.get: robots.txt, 3 s spacing, the 200-a-day Play budget.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import duckdb
import yaml

from .. import apple, play, store
from ..http import BudgetExhausted, Fetcher, HostStopped, RobotsDisallowed
from ..settings import RadarSettings
from .settings import EstimateSettings

_PKG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
# An Android application id: letters, digits and _ only, every segment starts with a letter.
# iOS bundle ids such as "com.studio.game-ios" can never be one: they are not tried on Play.
_ANDROID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_IOS_SEGMENTS = {"ios", "iphone", "ipad"}


def plausible_android_id(bundle: str | None) -> bool:
    if not bundle or not _ANDROID.match(bundle):
        return False
    return not any(seg.lower() in _IOS_SEGMENTS for seg in bundle.split("."))


class WatchError(ValueError):
    pass


def parse_target(text: str) -> tuple[str, str]:
    """('apple', '123') or ('play', 'com.x.y') from an id, a package or a store URL."""
    t = text.strip()
    if t.startswith("http"):
        u = urlsplit(t)
        if u.hostname and u.hostname.endswith("apple.com"):
            m = re.search(r"/id(\d+)", u.path)
            if m:
                return "apple", m.group(1)
        if u.hostname == "play.google.com":
            pkg = (parse_qs(u.query).get("id") or [""])[0]
            if _PKG.match(pkg):
                return "play", pkg
        raise WatchError(f"not an App Store or Google Play app URL: {text}")
    if t.lower().startswith("id") and t[2:].isdigit():
        t = t[2:]
    if t.isdigit():
        return "apple", t
    if _PKG.match(t):
        return "play", t
    raise WatchError(f"not an Apple app id or a Play package name: {text}")


def watched(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in con.execute(
            "SELECT store, app_id FROM raw_mr_watch WHERE removed_at IS NULL ORDER BY 1, 2"
        ).fetchall()
    ]


def watch_add(con: duckdb.DuckDBPyConnection, target: str, note: str | None = None) -> bool:
    st, app_id = parse_target(target)
    if (st, app_id) in watched(con):
        return False
    store.insert(
        con,
        "raw_mr_watch",
        [{"store": st, "app_id": app_id, "added_at": store.utcnow(), "note": note}],
    )
    return True


def watch_remove(con: duckdb.DuckDBPyConnection, target: str) -> bool:
    st, app_id = parse_target(target)
    if (st, app_id) not in watched(con):
        return False
    con.execute(
        "UPDATE raw_mr_watch SET removed_at = ? WHERE store = ? AND app_id = ? "
        "AND removed_at IS NULL",
        [store.utcnow(), st, app_id],
    )
    return True


def watch_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    cur = con.execute(
        """
        SELECT w.store, w.app_id, w.added_at, w.note,
          (SELECT any_value(name) FROM raw_mr_app a WHERE a.app_id = w.app_id) AS name,
          (SELECT MAX(snapshot_date) FROM raw_mr_app a WHERE a.app_id = w.app_id) AS last_seen
        FROM raw_mr_watch AS w WHERE w.removed_at IS NULL ORDER BY w.added_at
        """
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


# -- daily stages -------------------------------------------------------------------------------


def _note(res: Any, msg: str, log: Callable[[str], None], error: bool = False) -> None:
    (res.errors if error else res.notes).append(msg)
    log(f"    radar: {msg}")


def lookup_watched(
    con: duckdb.DuckDBPyConnection, f: Fetcher, s: RadarSettings, day: dt.date, res: Any, log
) -> int:
    """Lookup of watched Apple ids that today's charts did not already bring, per storefront."""
    ids = [a for st, a in watched(con) if st == "apple"]
    if not ids:
        return 0
    n = 0
    for cc in s.countries:
        have = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT app_id FROM raw_mr_app WHERE snapshot_date = ? AND store = 'apple' "
                "AND country = ? AND source = 'lookup'",
                [day, cc],
            ).fetchall()
        }
        todo = [a for a in ids if a not in have]
        if not todo:
            continue
        try:
            found, _missing = apple.lookup_ids(
                f, todo, cc, batch=s.lookup_batch, purpose=f"lookup_watch:{cc}"
            )
        except (BudgetExhausted, HostStopped) as exc:
            _note(res, f"watchlist lookup {cc}: {exc}", log)
            break
        except Exception as exc:
            _note(res, f"watchlist lookup {cc}: {exc}", log, error=True)
            continue
        rows = [apple.app_row(r, day, cc, "watch") for r in found]
        store.replace(
            con,
            "raw_mr_app",
            "snapshot_date = ? AND store = 'apple' AND country = ? AND source = 'watch'",
            [day, cc],
            rows,
        )
        n += len(rows)
    res.counts["watch_lookup_rows"] = n
    return n


def _manual_map(s: RadarSettings) -> dict[str, str | None]:
    p = s.play_map_file
    if not p or not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(k): (str(v) if v else None) for k, v in (data.get("apple_to_play") or {}).items()}


def _band(rank: int) -> int:
    return 0 if rank <= 10 else 1 if rank <= 25 else 2 if rank <= 50 else 3


def panel_jobs(
    con: duckdb.DuckDBPyConnection, s: RadarSettings, es: EstimateSettings, day: dt.date
) -> list[tuple[str | None, str, str]]:
    """(apple_id, package, why) to read today: watched games first, then the panel."""
    from .stage import run_models  # the panel reads today's rank input

    run_models(con)
    manual = _manual_map(s)
    xref = {
        r[0]: (r[1], r[2], r[3])
        for r in con.execute(
            "SELECT apple_id, play_id, status, verified_on FROM raw_mr_xref "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY apple_id ORDER BY verified_on DESC) = 1"
        ).fetchall()
    }
    bundles = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT app_id, any_value(bundle_id) FROM raw_mr_app WHERE store = 'apple' "
            "AND bundle_id IS NOT NULL GROUP BY 1"
        ).fetchall()
    }

    def package(apple_id: str, bundle: str | None) -> tuple[str | None, str]:
        if apple_id in manual:
            return manual[apple_id], "manual"
        known = xref.get(apple_id)
        if known and known[1] == "mapped":
            return known[0], "known"
        if known and known[1] == "manual_none":
            return None, "none"
        if known and known[1] == "not_found" and (day - known[2]).days < s.play_retry_days:
            return None, "none"
        if not plausible_android_id(bundle):
            return None, "none"
        return bundle, "bundle_id"

    jobs: list[tuple[str | None, str, str]] = []
    for st, app_id in watched(con):
        if st == "play":
            jobs.append((None, app_id, "watch"))
        else:
            pkg, how = package(app_id, bundles.get(app_id))
            if pkg:
                jobs.append((app_id, pkg, f"watch:{how}"))
    if es.panel_size <= 0:
        return jobs
    cands = con.execute(
        """
        SELECT c.app_id, any_value(c.bundle_id) AS bundle, MIN(c.rank) AS best
        FROM stg_mr_rank_input AS c
        WHERE c.snapshot_date = ? AND c.list_kind = 'free' AND c.store = 'apple'
        GROUP BY 1
        """,
        [day],
    ).fetchall()
    with_history = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT app_id FROM raw_mr_app WHERE store = 'play' AND snapshot_date < ? "
            "AND real_installs IS NOT NULL",
            [day],
        ).fetchall()
    }
    read_today = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT app_id FROM raw_mr_app WHERE store = 'play' AND snapshot_date = ?",
            [day],
        ).fetchall()
    }
    pool: list[tuple[int, int, int, str, str, str]] = []
    for apple_id, bundle, best in cands:
        pkg, how = package(apple_id, bundle)
        if not pkg or pkg in read_today:
            continue  # a page already read today (the radar's own candidates) needs no slot
        pool.append((0 if pkg in with_history else 1, _band(best), best, apple_id, pkg, how))
    # sticky members first, then round-robin over rank bands (best rank first within a band)
    pool.sort(key=lambda p: (p[0], p[2]))
    seen_band: dict[tuple[int, int], int] = {}
    keyed = []
    for p in pool:
        k = (p[0], p[1])
        seen_band[k] = seen_band.get(k, 0) + 1
        keyed.append(((p[0], seen_band[k], p[1]), p))
    keyed.sort(key=lambda kp: kp[0])
    taken = 0
    for _, (_s, _b, _r, apple_id, pkg, how) in keyed:
        if taken >= es.panel_size:
            break
        jobs.append((apple_id, pkg, f"panel:{how}"))
        taken += 1
    return jobs


def collect_play(
    con: duckdb.DuckDBPyConnection,
    f: Fetcher,
    s: RadarSettings,
    es: EstimateSettings,
    day: dt.date,
    res: Any,
    log,
) -> dict[str, int]:
    """Play pages of watched games and of the calibration panel, within the remaining budget."""
    stats = {"requested": 0, "stored": 0, "skipped_budget": 0}
    if not s.play_enabled:
        return stats
    host = "play.google.com"
    jobs = panel_jobs(con, s, es, day)
    done: set[str] = set()
    for apple_id, pkg, why in jobs:
        if pkg in done:
            continue
        done.add(pkg)
        if con.execute(
            "SELECT 1 FROM raw_mr_app WHERE snapshot_date = ? AND store = 'play' AND app_id = ? "
            "LIMIT 1",
            [day, pkg],
        ).fetchone():
            continue
        if not why.startswith("watch") and f.used_today(host) >= s.play_budget - es.play_reserve:
            stats["skipped_budget"] += 1
            continue
        try:
            d = play.fetch_details(f, pkg, s.play_country, s.play_lang)
        except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
            _note(res, f"estimates play: {exc}", log)
            break
        except Exception as exc:
            _note(res, f"estimates play {pkg}: {exc}", log, error=True)
            continue
        stats["requested"] += 1
        method = why.split(":")[-1]
        if d is None:
            if apple_id and method == "bundle_id":
                store.insert(
                    con,
                    "raw_mr_xref",
                    [
                        {
                            "apple_id": apple_id,
                            "bundle_id": pkg,
                            "play_id": None,
                            "method": method,
                            "status": "not_found",
                            "verified_on": day,
                        }
                    ],
                )
            continue
        store.replace(
            con,
            "raw_mr_app",
            "snapshot_date = ? AND store = 'play' AND app_id = ?",
            [day, pkg],
            [play.app_row(d, day, s.play_country)],
        )
        stats["stored"] += 1
        if apple_id and method in ("bundle_id", "manual"):
            store.insert(
                con,
                "raw_mr_xref",
                [
                    {
                        "apple_id": apple_id,
                        "bundle_id": pkg,
                        "play_id": pkg,
                        "method": method,
                        "status": "mapped",
                        "verified_on": day,
                    }
                ],
            )
    res.counts["estimates_play"] = stats
    log(f"    radar: estimates Play pages {stats['stored']} stored of {stats['requested']}")
    return stats
