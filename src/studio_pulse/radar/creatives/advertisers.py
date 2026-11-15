"""Which advertisers the creative library follows, and their Google Ads Transparency ids.

The list is the seed of big casual / puzzle publishers (settings.SEED), the config's
`radar.creatives.advertisers` and, with `discover`, the publishers with the most chart rows in
the radar's own snapshots of the last `discover_days` days (no request: the warehouse only).

Google ids: `google_ids` from the config are used as they are. Otherwise names are resolved
(a) locally, from the weekly advertiser extract (`raw_mr_adv`, radar/adintel) when it exists,
or (b) with one small BigQuery query (advertiser_id, advertiser_disclosed_name and topic:
10.8 GB on 15 Nov 2026, dry-run first, refused above `resolve_max_gb`), cached for
`resolve_every_days` (30). A name matches when the advertiser's name without legal-form words
equals it or starts with it ("Dream Games" matches "Dream Games Teknoloji A.S."), and the
advertiser has at least 3 games-topic creatives making up at least a fifth of its creatives
(a namesake toy shop or bakery is dropped).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

import duckdb

from ..adintel.google_atc import DATASET, GAMES_TOPIC, TABLE, full_name
from .settings import Advertiser, CreativeSettings, slug

MAX_IDS_PER_ADVERTISER = 5
_SAFE = re.compile(r"[a-z0-9]+(?: [a-z0-9]+)*")
SORT_MECHANICS = {"color-sort", "goods-sort", "hexa-sort"}
APPLE_GENRES = {"7012": "puzzle", "7003": "casual", "7019": "word", "7004": "board"}


def name_keys(names: tuple[str, ...] | list[str]) -> list[str]:
    """Folded names without legal words, [a-z0-9 ] only (the only text that reaches SQL)."""
    out = sorted({k for k in (full_name(n) for n in names) if k and _SAFE.fullmatch(k)})
    return out


def matches(adv_name: str, keys: list[str]) -> bool:
    a = full_name(adv_name)
    return any(a == k or a.startswith(k + " ") for k in keys)


def discover(
    con: duckdb.DuckDBPyConnection, s: CreativeSettings, today: dt.date
) -> list[Advertiser]:
    """The radar charts' top publishers not already tracked, with a genre from their games."""
    if not s.discover or s.discover_top <= 0:
        return []
    since = today - dt.timedelta(days=s.discover_days)
    try:
        rows = con.execute(
            "SELECT developer, COUNT(*) AS n, list(DISTINCT genre_id) AS genres, "
            "list(DISTINCT app_id) AS apps FROM raw_mr_chart WHERE developer IS NOT NULL "
            "AND snapshot_date >= ? GROUP BY developer ORDER BY n DESC, developer LIMIT 200",
            [since],
        ).fetchall()
    except duckdb.CatalogException:
        return []
    sort_apps: set[str] = set()
    try:
        sort_apps = {
            str(r[0]).split(":", 1)[-1]
            for r in con.execute(
                "SELECT DISTINCT app_key FROM fct_mr_app_tags WHERE dimension = "
                "'mechanic_primary' AND list_contains(?::VARCHAR[], value)",
                [sorted(SORT_MECHANICS)],
            ).fetchall()
        }
    except duckdb.Error:
        pass
    tracked = [k for a in s.advertisers for k in name_keys(a.google_names or (a.name,))]
    found: list[Advertiser] = []
    keys: list[str] = []
    for dev, _n, genres, apps in rows:
        k = full_name(dev)
        if not k or not _SAFE.fullmatch(k) or len(k) < 4:
            continue
        if any(k == t or k.startswith(t + " ") or t.startswith(k + " ") for t in tracked + keys):
            continue
        if any(str(a) in sort_apps for a in apps or []):
            genre = "sort"
        else:
            named = [APPLE_GENRES[g] for g in genres or [] if g in APPLE_GENRES]
            genre = "puzzle" if "puzzle" in named else (named[0] if named else "casual")
        keys.append(k)
        found.append(
            Advertiser(
                key=slug(dev), name=str(dev)[:80], genre=genre, origin="charts",
                google_names=(str(dev)[:80],),
            )
        )  # fmt: skip
        if len(found) >= s.discover_top:
            break
    return found


def build_resolve_sql(keys: list[str], topic: str = GAMES_TOPIC) -> str:
    for k in keys:
        if not _SAFE.fullmatch(k):
            raise ValueError(f"unsafe advertiser name {k!r}")
    if not keys:
        raise ValueError("no advertiser names to resolve")
    norm = (
        "CONCAT(' ', REGEXP_REPLACE(LOWER(REGEXP_REPLACE(NORMALIZE(advertiser_disclosed_name, "
        "NFKD), r'\\pM', '')), r'[^a-z0-9]+', ' '), ' ')"
    )
    alt = "|".join(keys)
    return f"""
SELECT advertiser_id, ANY_VALUE(advertiser_disclosed_name) AS name, COUNT(*) AS creatives,
       COUNTIF(topic = '{topic}') AS games_creatives
FROM {TABLE}
WHERE REGEXP_CONTAINS({norm}, r' (?:{alt}) ')
GROUP BY advertiser_id
""".strip()


def pick_ids(adv: Advertiser, candidates: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(ids, names) of the candidates that are this advertiser (name rule + games share)."""
    keys = name_keys(adv.google_names or (adv.name,))
    ok = [
        c
        for c in candidates
        if matches(str(c.get("name") or ""), keys)
        and int(c.get("games_creatives") or 0) >= 3
        and int(c.get("games_creatives") or 0) >= 0.2 * int(c.get("creatives") or 0)
    ]
    ok.sort(key=lambda c: (-int(c.get("games_creatives") or 0), str(c["advertiser_id"])))
    ok = ok[:MAX_IDS_PER_ADVERTISER]
    return [str(c["advertiser_id"]) for c in ok], [str(c.get("name") or "") for c in ok]


def _local_candidates(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]] | None:
    try:
        rows = con.execute(
            "SELECT advertiser_id, name, creatives, games_creatives FROM raw_mr_adv"
        ).fetchall()
    except duckdb.CatalogException:
        return None
    if not rows:
        return None
    return [
        {"advertiser_id": r[0], "name": r[1], "creatives": r[2], "games_creatives": r[3]}
        for r in rows
    ]


def cached(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    rows = con.execute(
        "SELECT advertiser_key, google_ids, google_names, resolved_at FROM mr_creative_advertiser"
    ).fetchall()
    return {r[0]: {"ids": list(r[1] or []), "names": list(r[2] or []), "at": r[3]} for r in rows}


def resolve(
    con: duckdb.DuckDBPyConnection,
    advertisers: list[Advertiser],
    *,
    s: CreativeSettings,
    executor_factory: Callable[[int], Any] | None,
    now: dt.datetime,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Fill each advertiser's google ids (config ids, cache, local extract or one query).
    Returns {"status", "source", "estimated_bytes", "billed_bytes", "advertisers": [...]}."""
    cache = cached(con)
    fresh_after = now - dt.timedelta(days=s.resolve_every_days)
    out: dict[str, Any] = {"status": "ok", "source": "cache", "estimated_bytes": 0,
                           "billed_bytes": 0}  # fmt: skip
    todo: list[Advertiser] = []
    resolved: dict[str, tuple[list[str], list[str], dt.datetime | None]] = {}
    for a in advertisers:
        if a.google_ids:
            resolved[a.key] = (list(a.google_ids), [], now)
            continue
        c = cache.get(a.key)
        if c and c["at"] and c["at"] >= fresh_after and not force:
            resolved[a.key] = (c["ids"], c["names"], c["at"])
        else:
            todo.append(a)
    if todo:
        cands = _local_candidates(con)
        if cands is not None:
            out["source"] = "local_extract"
        elif executor_factory is None:
            out.update(status="not_configured", source="none",
                       message="no Google Cloud project and no local advertiser extract")  # fmt: skip
            cands = []
        else:
            keys = sorted({k for a in todo for k in name_keys(a.google_names or (a.name,))})
            ex = executor_factory(s.resolve_cap_bytes)
            from ...connectors.executors import BytesCapExceeded

            try:
                table = ex.run("creatives_resolve", build_resolve_sql(keys))
                cands = table.to_pylist() if table is not None else []
                out["source"] = "bigquery"
                if table is None:  # dry run only
                    out["status"] = "dry_run"
            except BytesCapExceeded as exc:
                out.update(status="refused", message=str(exc))
                cands = []
            except Exception as exc:  # auth, quota: the run goes on with what it has
                out.update(status="error", message=f"{type(exc).__name__}: {exc}"[:400])
                cands = []
            out["estimated_bytes"] = sum(x.estimated_bytes or 0 for x in ex.stats)
            out["billed_bytes"] = sum(x.billed_bytes or 0 for x in ex.stats)
            log(
                f"    creatives: resolve {len(keys)} names, {out['status']}, estimated "
                f"{out['estimated_bytes'] / 1e9:.2f} GB, billed {out['billed_bytes'] / 1e9:.2f} GB"
            )
        for a in todo:
            if out["status"] == "ok":
                ids, names = pick_ids(a, cands)
                resolved[a.key] = (ids, names, now)
            elif a.key in cache:  # refused or failed: keep the older ids rather than none
                c = cache[a.key]
                resolved[a.key] = (c["ids"], c["names"], c["at"])
    rows = []
    for a in advertisers:
        ids, names, at = resolved.get(a.key, ([], [], None))
        rows.append(
            {
                "advertiser_key": a.key,
                "name": a.name,
                "genre": a.genre,
                "origin": a.origin,
                "google_ids": ids,
                "google_names": names,
                "meta_page_ids": list(a.meta_page_ids),
                "resolved_at": at,
            }
        )
    from .. import store

    store.replace(con, "mr_creative_advertiser", "TRUE", [], rows)
    out["advertisers"] = rows
    out["dataset"] = DATASET
    return out
