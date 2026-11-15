"""One refresh of the creative library: advertisers, sources, tags. Weekly inside `pulse run`
(radar.creatives.enabled); `pulse radar creatives refresh` by hand.

- Google Ads Transparency (BigQuery): at most every `every_days` (7), dry-run first and refused
  above `max_gb`; resolving advertiser names costs one more small query at most monthly.
- Meta Ad Library API: daily when a token exists (free; incremental from the last run).
- TikTok Commercial Content API: off unless enabled with an approved app's keys.
- Tags: new or changed creatives only, LLM spending capped per run.
Every source's run is a row in mr_creative_run with what BigQuery estimated and billed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

from ...config import Config
from ...llm import redact
from .. import store
from ..taxonomy import load as load_taxonomy
from . import advertisers as adv_mod
from . import google, meta, tagging, tiktok
from .library import init_schema
from .settings import Advertiser, CreativeSettings, from_config

SOURCES = ("google", "meta", "tiktok")


def _last_ok(con: duckdb.DuckDBPyConnection, source: str) -> dt.datetime | None:
    r = con.execute(
        "SELECT MAX(run_at) FROM mr_creative_run WHERE source = ? AND status IN ('ok', 'partial')",
        [source],
    ).fetchone()
    return r[0] if r else None


def _log_run(
    con: duckdb.DuckDBPyConnection, source: str, res: dict[str, Any], cap: int = 0
) -> None:
    store.insert(
        con,
        "mr_creative_run",
        [
            {
                "run_at": store.utcnow(),
                "source": source,
                "status": res.get("status"),
                "estimated_bytes": res.get("estimated_bytes") or 0,
                "billed_bytes": res.get("billed_bytes") or 0,
                "cap_bytes": cap,
                "rows": res.get("rows_n"),
                "cost_usd": res.get("cost_usd"),
                "message": str(res.get("message") or "")[:400],
            }
        ],
    )


def _upsert(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    keys = sorted({r["creative_key"] for r in rows})
    return store.replace(con, "mr_creative", "list_contains(?::VARCHAR[], creative_key)", [keys],
                         rows)  # fmt: skip


def _adv_dicts(advs: list[Advertiser], resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {r["advertiser_key"]: r for r in resolved}
    out = []
    for a in advs:
        r = by_key.get(a.key) or {}
        out.append(
            {
                "advertiser_key": a.key,
                "name": a.name,
                "genre": a.genre,
                "google_ids": list(r.get("google_ids") or a.google_ids),
                "google_names": list(r.get("google_names") or []),
                "meta_page_ids": list(a.meta_page_ids),
            }
        )
    return out


def refresh_google(
    con: duckdb.DuckDBPyConnection,
    s: CreativeSettings,
    advs: list[Advertiser],
    *,
    project: str | None,
    client: Any,
    today: dt.date,
    force: bool,
    dry_run_only: bool,
    log: Callable[[str], None],
) -> dict[str, Any]:
    from ...connectors.executors import BigQueryExecutor, BytesCapExceeded

    last = _last_ok(con, "google_atc")
    if last and not force and not dry_run_only and (today - last.date()).days < s.every_days:
        return {"status": "fresh", "last_run": last.isoformat(), "advertisers": len(advs)}
    secrets_ = (project,) if project else ()

    def say(m: str) -> None:
        log(redact(m, secrets_))

    factory = None
    if project or client is not None:

        def factory(cap: int) -> BigQueryExecutor:
            return BigQueryExecutor(billing_project=project or "", max_bytes_billed=cap,
                                    client=client, log=say, dry_run_only=dry_run_only)  # fmt: skip

    res = adv_mod.resolve(
        con, advs, s=s, executor_factory=factory, now=store.utcnow(), force=force, log=say
    )
    resolve_info = {k: res.get(k) for k in ("status", "source", "estimated_bytes", "billed_bytes",
                                            "message")}  # fmt: skip
    if res["source"] == "bigquery":
        _log_run(con, "google_resolve", res, s.resolve_cap_bytes)
    advd = _adv_dicts(advs, res["advertisers"])
    ids = sorted({i for a in advd for i in a["google_ids"]})
    out: dict[str, Any] = {"resolve": resolve_info, "advertisers": len(advs),
                           "resolved": sum(1 for a in advd if a["google_ids"]),
                           "advertiser_ids": len(ids), "cap_gb": round(s.cap_bytes / 1e9, 2)}  # fmt: skip
    if factory is None:
        out.update(status="not_configured", message="no Google Cloud project")
        return out
    if not ids and not dry_run_only:
        out.update(status="no_advertisers", message="no tracked advertiser resolved to a Google id")
        _log_run(con, "google_atc", out, s.cap_bytes)
        return out
    sql = google.build_creatives_sql(
        ids or [google.PLACEHOLDER_ID], today, lookback_days=s.lookback_days,
        active_days=s.active_days, per_advertiser=s.per_advertiser,
    )  # fmt: skip
    ex = factory(s.cap_bytes)
    t0 = time.perf_counter()
    status, msg, rows = "error", "", []
    try:
        table = ex.run("creatives_weekly", sql)
        if dry_run_only:
            status, msg = "dry_run", "dry run only; nothing stored"
        else:
            rows = table.to_pylist() if table is not None else []
            status = "ok"
    except BytesCapExceeded as exc:
        status, msg = "refused", str(exc)
    except Exception as exc:
        status, msg = "error", redact(f"{type(exc).__name__}: {exc}", secrets_)[:400]
    est = sum(x.estimated_bytes or 0 for x in ex.stats)
    billed = sum(x.billed_bytes or 0 for x in ex.stats)
    n = 0
    if status == "ok":
        n = google.replace_rows(con, google.to_rows(rows, advd, today, store.utcnow()))
    out.update(status=status, message=msg, estimated_bytes=est, billed_bytes=billed, rows_n=n,
               estimated_gb=round(est / 1e9, 3), billed_gb=round(billed / 1e9, 3),
               seconds=round(time.perf_counter() - t0, 1))  # fmt: skip
    _log_run(con, "google_atc", out, s.cap_bytes)
    say(f"    creatives: google {status}, estimated {out['estimated_gb']} GB, billed "
        f"{out['billed_gb']} GB, {n} creatives")  # fmt: skip
    return out


def _terms(advs: list[Advertiser]) -> dict[str, tuple[str, ...]]:
    from .settings import SEED

    seed = {a["name"]: tuple(a.get("meta_terms") or ()) for a in SEED}
    return {a.key: a.meta_terms or seed.get(a.name, ()) for a in advs}


def _since(
    con: duckdb.DuckDBPyConnection, source: str, s: CreativeSettings, today: dt.date
) -> dt.date:
    last = _last_ok(con, source)
    if last:
        return last.date() - dt.timedelta(days=7)  # overlap: late stop times get updated
    return today - dt.timedelta(days=s.lookback_days)


def refresh_meta(
    con: duckdb.DuckDBPyConnection, s: CreativeSettings, advs: list[Advertiser], *,
    today: dt.date, force: bool, session: Any, token: str | None, log: Callable[[str], None],
) -> dict[str, Any]:  # fmt: skip
    if not s.meta:
        return {"status": "disabled"}
    last = _last_ok(con, "meta")
    if last and not force and (today - last.date()).days < s.meta_every_days:
        return {"status": "fresh", "last_run": last.isoformat()}
    advd = _adv_dicts(advs, [])
    res = meta.refresh(
        advd, _terms(advs), countries=s.meta_countries, version=s.meta_version,
        max_pages=s.meta_max_pages, since=_since(con, "meta", s, today), today=today,
        now=store.utcnow(), session=session, token=token, log=log,
    )  # fmt: skip
    if res["status"] == "not_configured":
        return {"status": "not_configured", "message": res["message"]}
    res["rows_n"] = _upsert(con, res.pop("rows"))
    _log_run(con, "meta", res)
    return res


def refresh_tiktok(
    con: duckdb.DuckDBPyConnection, s: CreativeSettings, advs: list[Advertiser], *,
    today: dt.date, session: Any, log: Callable[[str], None],
) -> dict[str, Any]:  # fmt: skip
    tt = tiktok.TikTokCommercialContent(
        enabled=s.tiktok, countries=s.tiktok_countries, session=session, log=log
    )
    st = tt.status()
    if st["status"] != "ready":
        return st
    res = tt.refresh(_adv_dicts(advs, []), _terms(advs), _since(con, "tiktok", s, today), today,
                     store.utcnow())  # fmt: skip
    res["rows_n"] = _upsert(con, res.pop("rows"))
    _log_run(con, "tiktok", res)
    return res


def tag_pending(
    con: duckdb.DuckDBPyConnection,
    s: CreativeSettings,
    client: Any,
    *,
    limit: int | None = None,
    keys: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Tag creatives whose tags are missing or stale. LLM paths: at most `limit` (default
    tagging.max_per_run) per run, YouTube videos first; keyword/metadata tags: all."""
    from .library import _rows

    tax = load_taxonomy()
    rows = [r for r in _rows(con) if not keys or r["creative_key"] in keys]
    provider = client.provider if client else None
    model = provider.model if provider else ""
    limit = s.tag_max_per_run if limit is None else limit
    todo_llm, todo_cheap = [], []
    for r in rows:
        path = tagging.plan(r, provider)
        h = tagging.input_hash(r, path, model, s.video_seconds)
        if r.get("input_hash") == h:
            continue
        if path == "keyword" and r.get("tag_source") in ("llm_video", "llm_text"):
            continue  # no LLM this run: keep the earlier LLM tags
        (todo_llm if path != "keyword" else todo_cheap).append(r)
    todo_llm.sort(
        key=lambda r: (r.get("source") != "youtube", -(r["last_shown"] or dt.date.min).toordinal())
    )
    picked = todo_llm[: max(0, limit)]
    skipped = len(todo_llm) - len(picked)
    out_rows = []
    spent = 0.0
    counts: dict[str, int] = {}
    for r in picked:
        tag = tagging.tag_one(r, tax, client, seconds=s.video_seconds, log=log)
        spent += float(tag.get("cost_usd") or 0)
        counts[tag["tag_source"]] = counts.get(tag["tag_source"], 0) + 1
        out_rows.append({**tag, "creative_key": r["creative_key"]})
    for r in todo_cheap:
        tag = tagging.tag_one(r, tax, None, seconds=s.video_seconds, log=log)
        counts[tag["tag_source"]] = counts.get(tag["tag_source"], 0) + 1
        out_rows.append({**tag, "creative_key": r["creative_key"]})
    now = store.utcnow()
    store.insert(
        con,
        "mr_creative_tag",
        [
            {
                **t,
                "evidence": json.dumps(t.get("evidence") or {}, ensure_ascii=False),
                "rejected": json.dumps(t.get("rejected") or [], ensure_ascii=False),
                "tagged_at": now,
            }
            for t in out_rows
        ],
    )
    con.execute(  # keep the newest three tag rows per creative
        "DELETE FROM mr_creative_tag WHERE (creative_key, tagged_at) IN (SELECT creative_key, "
        "tagged_at FROM (SELECT creative_key, tagged_at, ROW_NUMBER() OVER (PARTITION BY "
        "creative_key ORDER BY tagged_at DESC) AS n FROM mr_creative_tag) WHERE n > 3)"
    )
    return {"tagged": len(out_rows), "by_source": counts, "llm_skipped_over_limit": skipped,
            "cost_usd": round(spent, 6)}  # fmt: skip


def llm_client(
    cfg: Config,
    s: CreativeSettings,
    run_id: str | None = None,
    log: Callable[[str], None] = print,
) -> Any:
    """The shared LLM settings with the budget lowered to the tagging cap (None: no LLM, also
    when the provider is not configured: tags then come from keywords and metadata)."""
    from ...llm import LLMConfigError, client_from_settings
    from ...llm import settings as llm_settings

    st = llm_settings(cfg.raw.get("llm"))
    if st["provider"] == "none" or not s.tagging or s.tag_budget_usd <= 0:
        return None
    shared = st.get("budget_usd")
    st["budget_usd"] = (
        min(float(shared), s.tag_budget_usd) if shared is not None else s.tag_budget_usd
    )
    try:
        return client_from_settings(st, state_dir=Path(cfg.state_dir), run_id=run_id)
    except LLMConfigError as exc:
        log(f"    creatives: no LLM tagging ({exc})")
        return None


def refresh(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    *,
    s: CreativeSettings | None = None,
    today: dt.date | None = None,
    project: str | None = None,
    bq_client: Any = None,
    llm: Any = None,
    sources: tuple[str, ...] = SOURCES,
    force: bool = False,
    dry_run_only: bool = False,
    tag: bool = True,
    meta_session: Any = None,
    meta_token: str | None = None,
    tiktok_session: Any = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    s = s or from_config(cfg)
    today = today or cfg.as_of or dt.date.today()
    init_schema(con)
    advs = list(s.advertisers) + adv_mod.discover(con, s, today)
    out: dict[str, Any] = {"advertisers": {"tracked": len(advs),
                           "charts": sum(1 for a in advs if a.origin == "charts")}}  # fmt: skip
    if "google" in sources:
        out["google"] = refresh_google(con, s, advs, project=project, client=bq_client,
                                       today=today, force=force, dry_run_only=dry_run_only,
                                       log=log)  # fmt: skip
    else:
        # the advertiser table still lists everyone tracked (the page's filters read it)
        adv_mod.resolve(con, advs, s=dataclasses.replace(s, resolve_every_days=10**6),
                        executor_factory=None, now=store.utcnow(), log=log)  # fmt: skip
    if dry_run_only:
        return out
    if "meta" in sources:
        out["meta"] = refresh_meta(con, s, advs, today=today, force=force, session=meta_session,
                                   token=meta_token, log=log)  # fmt: skip
    if "tiktok" in sources:
        out["tiktok"] = refresh_tiktok(con, s, advs, today=today, session=tiktok_session, log=log)
    if tag:
        out["tags"] = tag_pending(con, s, llm, log=log)
        if llm is not None:
            out["llm"] = llm.summary()
            _log_run(con, "tags", {"status": "ok", "rows_n": out["tags"]["tagged"],
                                   "cost_usd": out["tags"]["cost_usd"]})  # fmt: skip
    out["creatives"] = con.execute("SELECT COUNT(*) FROM mr_creative").fetchone()[0]
    return out


def run_in_pipeline(
    con: duckdb.DuckDBPyConnection, cfg: Config, log: Callable[[str], None]
) -> dict[str, Any] | None:
    """`pulse run` hook: Google weekly (skipped while fresh, no credential lookup then), Meta
    daily with a token, tags within the tagging budget."""
    s = from_config(cfg)
    if not s.enabled:
        return None
    today = cfg.as_of or dt.date.today()
    log("Creative library ...")
    init_schema(con)
    last = _last_ok(con, "google_atc")
    google_due = not last or (today - last.date()).days >= s.every_days
    project = None
    if google_due:
        from ..dossier.run import default_project

        project = default_project()
    res = refresh(
        con, cfg, s=s, today=today, project=project, llm=llm_client(cfg, s, log=log), log=log
    )
    g = res.get("google") or {}
    if g.get("status") in ("refused", "error"):
        raise RuntimeError(f"creatives google {g['status']}: {g.get('message')}")
    return res


def add_youtube(
    con: duckdb.DuckDBPyConnection,
    s: CreativeSettings,
    url: str,
    *,
    advertiser: str,
    genre: str | None = None,
    note: str | None = None,
    first: dt.date | None = None,
    last: dt.date | None = None,
    today: dt.date,
) -> dict[str, Any]:
    """A public YouTube ad the owner saw (for example on the Ads Transparency page): the id,
    the advertiser and what the owner typed. Nothing is fetched from YouTube."""
    from .settings import GENRES, slug
    from .youtube import video_id

    vid = video_id(url)
    if not vid:
        raise ValueError("not a YouTube video URL or id")
    init_schema(con)
    key = slug(advertiser)
    known = next((a for a in s.advertisers if a.key == key), None)
    g = (genre or (known.genre if known else "other")).lower()
    if g not in GENRES:
        raise ValueError(f"genre must be one of {', '.join(GENRES)}")
    first = first or today
    last = last or today
    if last < first:
        raise ValueError("--last is before --first")
    row = {
        "creative_key": f"y-{vid}",
        "source": "youtube",
        "creative_id": vid,
        "advertiser_key": key,
        "advertiser_name": known.name if known else " ".join(advertiser.split())[:80],
        "genre": g,
        "format": "VIDEO",
        "regions": [],
        "regions_n": 0,
        "first_shown": first,
        "last_shown": last,
        "youtube_id": vid,
        "note": " ".join((note or "").split())[:200] or None,
        "extract_date": today,
        "updated_at": store.utcnow(),
    }
    _upsert(con, [row])
    return row


def attach_youtube(con: duckdb.DuckDBPyConnection, key: str, url: str) -> dict[str, Any]:
    """Link a public YouTube video to a creative already in the library (for example the
    video a Google Ads Transparency creative page plays): the creative keeps its dates,
    regions and impressions for the score, and its video can now be tagged."""
    from .youtube import video_id

    vid = video_id(url)
    if not vid:
        raise ValueError("not a YouTube video URL or id")
    init_schema(con)
    row = con.execute(
        "SELECT creative_key, source FROM mr_creative WHERE creative_key = ?", [key]
    ).fetchone()
    if not row:
        raise ValueError(f"no creative {key!r} in the library")
    if row[1] == "youtube":
        raise ValueError("this creative is a YouTube row already")
    con.execute(
        "UPDATE mr_creative SET youtube_id = ?, updated_at = ? WHERE creative_key = ?",
        [vid, store.utcnow(), key],
    )
    return {"creative_key": key, "youtube_id": vid}
