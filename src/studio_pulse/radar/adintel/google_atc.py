"""Google Ads Transparency Center: a WEEKLY extract of the public BigQuery dataset
`bigquery-public-data.google_ads_transparency_center` into the local warehouse; dossiers read
only the local extract.

The dataset (checked on 14 Nov 2026 with the BigQuery API, INFORMATION_SCHEMA and free
`tabledata.list` samples):
- `creative_stats`: ~164 million rows, ~141 GB, neither partitioned nor clustered. One row per
  creative: advertiser_id, advertiser_disclosed_name, advertiser_legal_name, ad_format_type
  (TEXT / IMAGE / VIDEO), advertiser_location, topic, region_stats (REPEATED: region_code,
  first_shown, last_shown, times_shown_* bounds, surface_serving_stats), audience fields.
- There is no app, store or bundle field. The narrowest game filter the schema allows is the
  creative's `topic` ("Hobbies, Games & Leisure" holds King.com's creatives, and also toy shops
  and betting). `surface_serving_stats` exists only for EU regions and lists all five surfaces
  (SEARCH, YOUTUBE, PLAY, MAPS, SHOPPING) on every row, so a PLAY filter is not possible.
- Bytes billed are the full columns a query names: advertiser_id 3.93 GB,
  advertiser_disclosed_name 3.81 GB, topic 3.05 GB, ad_format_type 1.04 GB,
  region_stats.region_code 2.25 GB, first_shown and last_shown 6.27 GB each. `TABLESAMPLE`
  does not help: BigQuery checks `maximum_bytes_billed` against the unsampled size.

So one weekly job (dry-run first, `maximum_bytes_billed` = radar.adintel.max_gb, default 30)
scans those seven columns once (26.6 GB; about 115 GB a month, inside BigQuery's free 1 TiB)
for creatives whose topic is games OR whose advertiser name matches a developer the radar has
seen in the charts, and stores one row per advertiser (creatives, formats, games-topic share,
active in the last 30 days, first/last shown) plus its top regions. Bytes estimated and billed
are logged in raw_mr_adintel_run. A dossier then looks its developer up locally, for free.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

import duckdb

from ...config import Config, ConfigError
from ...connectors.executors import BigQueryExecutor, BytesCapExceeded
from ...llm import redact
from .. import store

DATASET = "bigquery-public-data.google_ads_transparency_center"
TABLE = f"`{DATASET}.creative_stats`"
GAMES_TOPIC = "Hobbies, Games & Leisure"
EXPECTED = {
    "advertiser_id",
    "advertiser_disclosed_name",
    "ad_format_type",
    "topic",
    "region_stats",
    "region_stats.region_code",
    "region_stats.first_shown",
    "region_stats.last_shown",
}
SCHEMA_SQL = (
    f"SELECT field_path FROM `{DATASET}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
    "WHERE table_name = 'creative_stats'"
)
HARD_MAX_GB = 50.0  # a typo in the config must not scan the whole 141 GB table
_STOP = {
    "ltd",
    "limited",
    "inc",
    "llc",
    "gmbh",
    "co",
    "corp",
    "corporation",
    "company",
    "pte",
    "sa",
    "as",
    "ab",
    "oy",
    "bv",
    "srl",
    "fz",
    "fze",
    "the",
    "games",
    "game",
    "studio",
    "studios",
    "technology",
    "technologies",
    "mobile",
    "global",
    "international",
    "sirketi",
    "anonim",
    "teknoloji",
}


def fold(name: str | None) -> str:
    """Lower-case ASCII words separated by single spaces (the SQL does the same)."""
    folded = unicodedata.normalize("NFKD", (name or "").replace("ı", "i").replace("İ", "I"))
    ascii_ = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_).split())


def name_pattern(name: str | None) -> str | None:
    """'Oakever Games Ltd' -> 'oakever': the longest run of distinctive words, lower-case,
    [a-z0-9 ] only. Generic words (games, ltd, studio ...) and words under three letters end a
    run; nothing else reaches the SQL text."""
    runs: list[list[str]] = [[]]
    for w in fold(name).split():
        if w in _STOP or len(w) < 3:
            runs.append([])
        else:
            runs[-1].append(w)
    best = max(runs, key=lambda r: (len(" ".join(r)), -runs.index(r)))
    return " ".join(best) if best else None


def _specific(p: str) -> bool:
    """A name pattern specific enough for the scan: two words, or one word of 7+ letters
    ("dream" or "grand" alone match thousands of unrelated advertisers)."""
    return " " in p or len(p) >= 7


_LEGAL = {
    "ltd", "limited", "inc", "llc", "gmbh", "co", "corp", "corporation", "company", "pte",
    "plc", "sa", "sas", "sl", "as", "ab", "oy", "bv", "srl", "fz", "fze", "kk", "the",
    "anonim", "sirketi", "ticaret",
}  # fmt: skip


def full_name(name: str | None) -> str:
    """The folded name without legal-form words: 'Grand Games A.Ş.' -> 'grand games'."""
    return " ".join(w for w in fold(name).split() if w not in _LEGAL and len(w) > 1)


def developer_patterns(con: duckdb.DuckDBPyConnection, extra: list[str] | None = None) -> list[str]:
    """Patterns for every developer the radar has seen (charts, Lookup, Play pages)."""
    names: set[str] = set(extra or [])
    for sql in (
        "SELECT DISTINCT developer FROM raw_mr_chart WHERE developer IS NOT NULL",
        "SELECT DISTINCT developer FROM raw_mr_app WHERE developer IS NOT NULL",
    ):
        try:
            names |= {r[0] for r in con.execute(sql).fetchall()}
        except duckdb.CatalogException:
            pass
    pats = {p for p in (name_pattern(n) for n in names) if p and _specific(p)}
    return sorted(pats)


def build_extract_sql(
    patterns: list[str], today: dt.date, topic: str = GAMES_TOPIC, min_games: int = 10
) -> str:
    for p in patterns:
        if not re.fullmatch(r"[a-z0-9]+(?: [a-z0-9]+)*", p):
            raise ValueError(f"unsafe advertiser pattern {p!r}")
    if not re.fullmatch(r"[A-Za-z ,&]+", topic):
        raise ValueError(f"unsafe topic {topic!r}")
    norm = (
        "CONCAT(' ', REGEXP_REPLACE(LOWER(REGEXP_REPLACE(NORMALIZE(advertiser_disclosed_name, "
        "NFKD), r'\\pM', '')), r'[^a-z0-9]+', ' '), ' ')"
    )
    alt = "|".join(patterns)
    names = f"REGEXP_CONTAINS({norm}, r' (?:{alt}) ')" if patterns else "FALSE"
    adv_names = (
        f"REGEXP_CONTAINS({norm.replace('advertiser_disclosed_name', 'name')}, "  # select alias
        f"r' (?:{alt}) ')"
        if patterns
        else "FALSE"
    )
    cutoff = (today - dt.timedelta(days=30)).isoformat()
    min_games = int(min_games)
    return f"""
WITH c AS (
  SELECT advertiser_id, advertiser_disclosed_name AS name, topic, ad_format_type AS fmt,
         region_stats, (SELECT MAX(r.last_shown) FROM UNNEST(region_stats) AS r) AS creative_last
  FROM {TABLE}
  WHERE topic = '{topic}' OR {names}
), adv AS (
  SELECT advertiser_id, ANY_VALUE(name) AS name, COUNT(*) AS creatives,
         COUNTIF(topic = '{topic}') AS games_creatives,
         COUNTIF(fmt = 'VIDEO') AS video, COUNTIF(fmt = 'IMAGE') AS image,
         COUNTIF(fmt = 'TEXT') AS text, COUNTIF(creative_last >= '{cutoff}') AS active_30d
  FROM c GROUP BY advertiser_id
  -- keep game advertisers and the radar developers; drop toy shops with a handful of ads
  HAVING COUNTIF(topic = '{topic}') >= {min_games} OR {adv_names}
), reg AS (
  SELECT c.advertiser_id, r.region_code, COUNT(*) AS creatives,
         MIN(r.first_shown) AS first_shown, MAX(r.last_shown) AS last_shown
  FROM c, UNNEST(c.region_stats) AS r GROUP BY 1, 2
)
SELECT adv.advertiser_id, adv.name, adv.creatives, adv.games_creatives, adv.video, adv.image,
       adv.text, adv.active_30d, MIN(reg.first_shown) AS first_shown,
       MAX(reg.last_shown) AS last_shown, COUNT(reg.region_code) AS regions_n,
       ARRAY_AGG(STRUCT(reg.region_code AS region, reg.creatives AS creatives,
                        reg.first_shown AS first_shown, reg.last_shown AS last_shown)
                 ORDER BY reg.creatives DESC LIMIT 8) AS regions
FROM adv LEFT JOIN reg USING (advertiser_id)
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
""".strip()


@dataclass
class AdIntelSettings:
    enabled: bool = False
    every_days: int = 7
    cap_bytes: int = 30_000_000_000
    topic: str = GAMES_TOPIC
    extra_names: tuple[str, ...] = ()
    min_games_creatives: int = 10


def settings(cfg: Config) -> AdIntelSettings:
    raw = dict((cfg.raw.get("radar") or {}).get("adintel") or {})
    gb = float(raw.get("max_gb", 30))
    if not 0 < gb <= HARD_MAX_GB:
        raise ConfigError(f"radar.adintel.max_gb must be in (0, {HARD_MAX_GB:g}]")
    return AdIntelSettings(
        enabled=bool(raw.get("enabled", False)),
        every_days=int(raw.get("every_days", 7)),
        cap_bytes=int(gb * 1e9),
        topic=str(raw.get("topic", GAMES_TOPIC)),
        extra_names=tuple(str(x) for x in raw.get("extra_names") or []),
        min_games_creatives=int(raw.get("min_games_creatives", 10)),
    )


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    sql = (
        resources.files("studio_pulse.radar.adintel")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    for stmt in body.split(";"):
        if stmt.strip():
            con.execute(stmt)


def _date(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def last_refresh(con: duckdb.DuckDBPyConnection) -> dict[str, Any] | None:
    init_schema(con)
    row = con.execute(
        "SELECT run_at, extract_date, estimated_bytes, billed_bytes, advertisers FROM "
        "raw_mr_adintel_run WHERE status = 'ok' ORDER BY run_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {
        "run_at": row[0].isoformat() if row[0] else None,
        "extract_date": row[1].isoformat() if row[1] else None,
        "estimated_gb": round((row[2] or 0) / 1e9, 2),
        "billed_gb": round((row[3] or 0) / 1e9, 3),
        "advertisers": row[4],
    }


def _rows_for(result_rows: list[dict[str, Any]], today: dt.date) -> tuple[list[dict], list[dict]]:
    adv, reg = [], []
    for r in result_rows:
        adv.append(
            {
                "extract_date": today,
                "advertiser_id": r["advertiser_id"],
                "name": r["name"],
                "name_norm": fold(r["name"]),
                "creatives": r["creatives"],
                "games_creatives": r["games_creatives"],
                "video": r["video"],
                "image": r["image"],
                "text": r["text"],
                "active_30d": r["active_30d"],
                "first_shown": _date(r["first_shown"]),
                "last_shown": _date(r["last_shown"]),
                "regions_n": r["regions_n"],
            }
        )
        for g in r.get("regions") or []:
            reg.append(
                {
                    "extract_date": today,
                    "advertiser_id": r["advertiser_id"],
                    "region_code": g["region"],
                    "creatives": g["creatives"],
                    "first_shown": _date(g["first_shown"]),
                    "last_shown": _date(g["last_shown"]),
                }
            )
    return adv, reg


def refresh(
    con: duckdb.DuckDBPyConnection,
    *,
    project: str | None,
    s: AdIntelSettings,
    today: dt.date | None = None,
    client: Any = None,
    force: bool = False,
    dry_run_only: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """One weekly extract. Skipped when the last good one is younger than every_days."""
    today = today or dt.date.today()
    init_schema(con)
    last = last_refresh(con)
    if last and not force and not dry_run_only:
        age = (today - dt.date.fromisoformat(last["extract_date"])).days
        if age < s.every_days:
            return {"status": "fresh", "age_days": age, **last}
    secrets_ = (project,) if project else ()

    def say(m: str) -> None:
        log(redact(m, secrets_))

    if not project and client is None:
        return {"status": "not_configured", "message": "no Google Cloud project"}
    patterns = developer_patterns(con, list(s.extra_names))
    sql = build_extract_sql(patterns, today, s.topic, s.min_games_creatives)
    ex = BigQueryExecutor(
        billing_project=project or "",
        max_bytes_billed=s.cap_bytes,
        client=client,
        log=say,
        dry_run_only=dry_run_only,
    )
    out: dict[str, Any] = {"patterns": len(patterns), "cap_gb": round(s.cap_bytes / 1e9, 2)}
    t0 = time.perf_counter()
    status, msg, rows = "error", "", []
    try:
        table = ex.run("atc_schema", SCHEMA_SQL)
        if table is not None:
            missing = sorted(EXPECTED - set(table.column("field_path").to_pylist()))
            if missing:
                raise RuntimeError("columns missing from INFORMATION_SCHEMA: " + ", ".join(missing))
        result = ex.run("atc_weekly_extract", sql)
        if dry_run_only:
            status, msg = "dry_run", "dry run only; nothing stored"
        else:
            rows = result.to_pylist() if result is not None else []
            status = "ok"
    except BytesCapExceeded as exc:
        status, msg = "refused", str(exc)
    except Exception as exc:  # auth, quota, API errors: logged, the run continues
        status, msg = "error", redact(f"{type(exc).__name__}: {exc}", secrets_)[:400]
    est = sum(x.estimated_bytes or 0 for x in ex.stats)
    billed = sum(x.billed_bytes or 0 for x in ex.stats)
    if status == "ok":
        adv, reg = _rows_for(rows, today)
        store.replace(con, "raw_mr_adv", "TRUE", [], adv)
        store.replace(con, "raw_mr_adv_region", "TRUE", [], reg)
        out["advertisers"] = prune(con, patterns, s.min_games_creatives)
    store.insert(
        con,
        "raw_mr_adintel_run",
        [
            {
                "run_at": store.utcnow(),
                "extract_date": today,
                "status": status,
                "estimated_bytes": est,
                "billed_bytes": billed,
                "cap_bytes": s.cap_bytes,
                "advertisers": out.get("advertisers"),
                "patterns": len(patterns),
                "message": msg[:400],
            }
        ],
    )
    out.update(
        {
            "status": status,
            "message": msg,
            "estimated_gb": round(est / 1e9, 3),
            "billed_gb": round(billed / 1e9, 3),
            "seconds": round(time.perf_counter() - t0, 1),
        }
    )
    say(
        f"    adintel: {status}, estimated {out['estimated_gb']} GB, billed "
        f"{out['billed_gb']} GB, {out.get('advertisers', 0)} advertisers"
    )
    return out


def prune(con: duckdb.DuckDBPyConnection, patterns: list[str], min_games: int) -> int:
    """The same rule as the scan's HAVING, applied locally: keep advertisers with at least
    min_games games-topic creatives or a name matching a radar developer. Returns the rows kept."""
    rx = f" (?:{'|'.join(patterns)}) " if patterns else None
    keep = "games_creatives >= ?" + (" OR regexp_matches(' ' || name_norm || ' ', ?)" if rx else "")
    con.execute(f"DELETE FROM raw_mr_adv WHERE NOT ({keep})", [min_games, *([rx] if rx else [])])
    con.execute(
        "DELETE FROM raw_mr_adv_region WHERE advertiser_id NOT IN (SELECT advertiser_id FROM raw_mr_adv)"
    )
    return con.execute("SELECT COUNT(*) FROM raw_mr_adv").fetchone()[0]


def lookup_local(
    con: duckdb.DuckDBPyConnection, names: list[str], today: dt.date, limit: int = 5
) -> dict[str, Any]:
    """The dossier's view: advertisers in the latest local extract matching the names."""
    init_schema(con)
    fulls = sorted({f for f in (full_name(n) for n in names) if f})
    found = sorted({p for p in (name_pattern(n) for n in names) if p}, key=lambda p: (len(p), p))
    patterns: list[str] = []
    for p in found:  # '% pebbleworks %' already matches what '% example pebbleworks %' would
        if not any(q in p for q in patterns):
            patterns.append(p)
    patterns.sort()
    base: dict[str, Any] = {
        "source": "google_ads_transparency_center",
        "patterns": patterns,
        "basis": "platform_reported",
    }
    last = last_refresh(con)
    if not last:
        return {
            **base,
            "status": "no_extract",
            "message": "no local extract yet: pulse radar adintel refresh",
            "advertisers": [],
        }
    base["extract"] = last
    if not patterns:
        return {**base, "status": "ok", "advertisers": []}
    cond = " OR ".join(["(' ' || name_norm || ' ') LIKE ?"] * len(patterns))
    cands = con.execute(
        "SELECT advertiser_id, name, creatives, games_creatives, video, image, text, "
        f"active_30d, first_shown, last_shown, regions_n FROM raw_mr_adv WHERE {cond} "
        "ORDER BY games_creatives DESC, creatives DESC, advertiser_id LIMIT 50",
        [f"% {p} %" for p in patterns],
    ).fetchall()

    def strong(adv_name: str) -> bool:
        """The same company: equal names without legal words, or the developer's name of two
        or more words inside the advertiser's ('dream games' in 'dream games publishing')."""
        a = full_name(adv_name)
        return any(a == f or (" " in f and f" {f} " in f" {a} ") for f in fulls)

    ranked = sorted(cands, key=lambda r: (not strong(r[1]), -r[3], -r[2], r[0]))
    rows = [(*r, strong(r[1])) for r in ranked[:limit]]
    advertisers = []
    for r in rows:
        regs = con.execute(
            "SELECT region_code, creatives, first_shown, last_shown FROM raw_mr_adv_region "
            "WHERE advertiser_id = ? ORDER BY creatives DESC, region_code",
            [r[0]],
        ).fetchall()
        advertisers.append(
            {
                "advertiser_id": r[0],
                "name": r[1],
                "creatives": r[2],
                "games_creatives": r[3],
                "formats": {"video": r[4], "image": r[5], "text": r[6]},
                "active_30d": r[7],
                "first_shown": r[8].isoformat() if r[8] else None,
                "last_shown": r[9].isoformat() if r[9] else None,
                "days_since_last_shown": (today - r[9]).days if r[9] else None,
                "regions_n": r[10],
                # name: the developer's whole name is in the advertiser's; partial: only the
                # distinctive word is (a namesake is possible, so it is shown as unverified)
                "match": "name" if r[11] else "partial",
                "regions": [
                    {
                        "region": g[0],
                        "creatives": g[1],
                        "first_shown": g[2].isoformat() if g[2] else None,
                        "last_shown": g[3].isoformat() if g[3] else None,
                    }
                    for g in regs
                ],
                "url": f"https://adstransparency.google.com/advertiser/{r[0]}",
            }
        )
    return {**base, "status": "ok", "advertisers": advertisers}


def run_in_pipeline(
    con: duckdb.DuckDBPyConnection, cfg: Config, log: Callable[[str], None]
) -> dict[str, Any] | None:
    """`pulse run` hook: the weekly extract when radar.adintel.enabled (skipped while fresh)."""
    s = settings(cfg)
    if not s.enabled:
        return None
    today = cfg.as_of or dt.date.today()
    log("Ads Transparency weekly extract ...")
    last = last_refresh(con)
    if last and (today - dt.date.fromisoformat(last["extract_date"])).days < s.every_days:
        # fresh: no credential lookup, no request of any kind
        res = {
            "status": "fresh",
            "age_days": (today - dt.date.fromisoformat(last["extract_date"])).days,
            **last,
        }
    else:
        from ..dossier.run import default_project

        res = refresh(con, project=default_project(), s=s, today=today, log=log)
    if res["status"] == "fresh":
        log(f"    adintel: extract from {res['extract_date']} is fresh ({res['age_days']} d)")
    elif res["status"] != "ok":
        raise RuntimeError(f"adintel {res['status']}: {res.get('message')}")
    return res
