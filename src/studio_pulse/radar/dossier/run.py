"""One game dossier: store data -> screenshots -> LLM deconstruction -> reviews -> ads ->
decision card -> files (TR/EN Markdown, facts JSON, market_trends.yaml for creative-forge).

`pulse radar dossier <apple id | play package>` runs it; the web page /radar/dosya shows the
result. Everything stays local: out_dir is the radar's out_dir/dossier (never docs/demo).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import requests
import yaml

from ... import warehouse
from ...config import Config
from ...llm import BudgetExceeded, ImagePart, LLMClient, LLMError, client_from_settings
from ...llm import settings as llm_settings
from .. import store
from ..feed import write as write_feed
from ..http import BudgetExhausted, Fetcher, HostStopped, RobotsDisallowed
from ..run import _guard_out_dir
from ..taxonomy import keyword_claims
from ..taxonomy import load as load_taxonomy
from . import collect, deconstruct, peers, render, rubric
from . import reviews as reviews_mod
from .labels import LabelError, check_labels
from .settings import DossierSettings, from_config

SCHEMA = "studio-pulse/game_dossier/v1"
FIELD_CHARS = 3000


@dataclass
class DossierResult:
    key: str
    day: dt.date
    ok: bool = True
    files: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def default_project() -> str | None:
    """The billing project for BigQuery: the environment, else ADC / gcloud's configured
    project (google.auth.default reads it). Never printed or written to a file."""
    for n in ("PULSE_ADS_PROJECT", "GOOGLE_CLOUD_PROJECT", "PULSE_LLM_PROJECT"):
        if os.environ.get(n):
            return os.environ[n]
    try:
        import google.auth

        _, project = google.auth.default()
        return project
    except Exception:
        return None


def _fields(apple: dict[str, Any], play: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    if apple.get("found"):
        out["apple.name"] = apple.get("name") or ""
        out["apple.description"] = apple.get("description") or ""
        out["apple.release_notes"] = apple.get("release_notes") or ""
        out["apple.genres"] = ", ".join(apple.get("genres") or [])
    if play.get("found"):
        row = play["row"]
        out["play.name"] = row.get("name") or ""
        out["play.description"] = row.get("description") or ""
        out["play.summary"] = row.get("summary") or ""
        out["play.categories"] = ", ".join(json.loads(row.get("store_tags") or "[]"))
    return {k: v[:FIELD_CHARS] for k, v in out.items() if v}


def _play_store(play: dict[str, Any]) -> dict[str, Any] | None:
    if not play.get("found"):
        return None
    r = play["row"]
    return {
        "package": r["app_id"],
        "name": r["name"],
        "developer": r["developer"],
        "bucket": r["installs_bucket"],
        "min_installs": r["min_installs"],
        "real_installs": r["real_installs"],
        "rating_count": r["rating_count"],
        "rating_avg": round(r["rating_avg"], 3) if r["rating_avg"] is not None else None,
        "histogram": json.loads(r["histogram"]) if r["histogram"] else None,
        "contains_ads": r["contains_ads"],
        "offers_iap": r["offers_iap"],
        "iap_range": r["iap_range"],
        "released": collect._iso(r["release_date"]),
        "updated": play.get("updated"),
        "version": r["version"],
        "genre": r["primary_genre"],
        "categories": json.loads(r["store_tags"] or "[]"),
        "video": bool(r.get("video_url")),
        "store_url": r["store_url"],
        "basis": "store_reported",
        "source": "play_details_page",
    }


def _cadence(
    apple: dict[str, Any], play: dict[str, Any], ctx: dict[str, Any], revs: list, day: dt.date
) -> dict[str, Any]:
    dates: dict[str, str] = {}  # version -> earliest date known
    for v in ctx.get("versions_seen") or []:
        if v.get("version") and v.get("date"):
            dates[v["version"]] = min(dates.get(v["version"], v["date"]), v["date"])
    for v in reviews_mod.versions_from_reviews(revs):
        dates[v["version"]] = min(dates.get(v["version"], v["first_review"]), v["first_review"])
    sf = (apple.get("storefronts") or [{}])[0] if apple.get("found") else {}
    cur = sf.get("current_version_date") or (play.get("updated") if play.get("found") else None)
    rel = sf.get("release_date") or (
        collect._iso(play["row"]["release_date"]) if play.get("found") else None
    )
    out: dict[str, Any] = {
        "apple_version": sf.get("version"),
        "apple_current_version_date": sf.get("current_version_date"),
        "apple_release_date": sf.get("release_date"),
        "play_version": play["row"]["version"] if play.get("found") else None,
        "play_updated": play.get("updated") if play.get("found") else None,
        "versions_known": [
            {"version": k, "date": v}
            for k, v in sorted(dates.items(), key=lambda kv: (kv[1], kv[0]))
        ],
        "basis": "observed",
        "note": "version dates: store fields, the radar's snapshots and the earliest Play review "
        "naming each version (a lower bound)",
    }
    try:
        if cur:
            out["days_since_update"] = (day - dt.date.fromisoformat(cur[:10])).days
        if rel:
            age = (day - dt.date.fromisoformat(rel[:10])).days
            out["days_since_release"] = age
        # the known versions are a sample (store fields, snapshots, versions named by reviews),
        # so only a lower bound is honest: at least this many versions in the last 90 days
        since = (day - dt.timedelta(days=90)).isoformat()
        out["versions_90d_min"] = sum(1 for v in dates.values() if v >= since)
    except ValueError:
        pass
    return out


def _mechanic(
    dec: dict[str, Any], ctx: dict[str, Any], fields: dict[str, str], tax: Any
) -> dict[str, Any]:
    for m in dec.get("mechanics") or []:
        return {
            "value": m["value"],
            "source": "llm_visual" if m.get("visual_only") else "llm",
            "evidence": m["evidence"],
        }
    for t in ctx.get("tags") or []:
        if t["dimension"] == "mechanic_primary":
            return {
                "value": t["value"],
                "source": f"radar_{t['source']}",
                "evidence": [{"kind": "text", "field": t["field"], "quote": t["quote"]}],
            }
    for c in keyword_claims(fields, tax):
        if c["dimension"] == "mechanic_primary":
            return {
                "value": c["value"],
                "source": "keyword",
                "evidence": [{"kind": "text", "field": c["field"], "quote": c["quote"]}],
            }
    return {"value": None, "source": None, "evidence": []}


def _peer_reviews(con, f, s, peer, rv, themes, client, parser, res) -> None:
    """Mine the Play reviews of up to s.peer_reviews peers (one LLM call for all of them) and
    add the theme comparison to the peer items."""
    ids = [p["app_id"] for p in peer["peers"]]
    mapped = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT apple_id, play_id FROM raw_mr_xref WHERE status = 'mapped' "
            "AND apple_id IN (SELECT UNNEST(?)) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY apple_id ORDER BY verified_on DESC) = 1",
            [ids],
        ).fetchall()
    }
    pool: list[dict[str, Any]] = []
    used: list[dict[str, Any]] = []
    for app_id in ids:
        if len(used) >= s.peer_reviews:
            break
        pkg = mapped.get(app_id)
        if not pkg:
            continue
        try:
            data = collect.play_data(f, pkg, s.radar.play_country, s.radar.play_lang, parser)
        except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
            res.notes.append(f"peer reviews: {exc}")
            break
        except Exception as exc:
            res.notes.append(f"peer reviews {pkg}: {type(exc).__name__}: {exc}")
            continue
        if data.get("found") and data.get("reviews"):
            pool += data["reviews"]
            used.append({"app_id": app_id, "package": pkg, "reviews": len(data["reviews"])})
    if not pool:
        return
    try:
        out = client.generate(reviews_mod.mining_request(pool, themes))
    except (BudgetExceeded, LLMError) as exc:
        res.notes.append(f"peer review themes: {type(exc).__name__}: {exc}"[:300])
        return
    kept, _ = reviews_mod.verify_labels(out.data or {}, pool, themes)
    pooled = reviews_mod.theme_counts(kept, themes, len(pool))
    peer["review_peers"] = used
    peer["items"] += peers.compare_themes(rv["themes"], rv["n"], pooled, len(pool), themes)


def build_dossier(
    cfg: Config,
    target_text: str,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    session: requests.Session | None = None,
    day: dt.date | None = None,
    llm_client: LLMClient | None = None,
    use_llm: bool = True,
    ads: bool = True,
    meta: Any = None,
    max_requests: int | None = 60,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
    s: DossierSettings | None = None,
    play_parser: Any = None,
    forge: bool = False,
) -> DossierResult:
    s = s or from_config(cfg)
    day = day or s.radar.as_of or cfg.as_of or dt.date.today()
    target = collect.parse_target(target_text)
    res = DossierResult(key=target.key, day=day)
    _guard_out_dir(s.out_dir)
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    own = con is None
    con = con or warehouse.connect(cfg.warehouse)
    tax = load_taxonomy()
    try:
        store.init_schema(con)
        f = Fetcher(
            con,
            day,
            session=session,
            run_id=run_id,
            budgets={
                "itunes.apple.com": s.radar.apple_budget,
                "play.google.com": s.radar.play_budget,
            },
            spacing={
                "itunes.apple.com": s.radar.apple_spacing_s,
                "play.google.com": s.radar.play_spacing_s,
            },
            max_total=max_requests,
            cache_dir=Path(cfg.state_dir) / "radar-cache",
            robots_hosts=("play.google.com", "itunes.apple.com"),
            sleep=sleep,
            clock=clock,
            log=log,
        )
        cc = s.primary_country
        try:
            target = collect.resolve(con, f, target, cc)
        except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
            res.notes.append(f"resolve: {exc}")
        apple: dict[str, Any] = {"found": False}
        if target.apple_id:
            try:
                apple = collect.apple_data(f, target.apple_id, s.countries)
            except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
                res.notes.append(f"lookup: {exc}")
        play: dict[str, Any] = {"found": False}
        pkg = target.play_package or (apple.get("bundle_id") if apple.get("found") else None)
        if pkg and s.radar.play_enabled:
            try:
                play = collect.play_data(
                    f, pkg, s.radar.play_country, s.radar.play_lang, play_parser
                )
                if play.get("found"):
                    target.play_package = pkg
            except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
                res.notes.append(f"play: {exc}")
            except Exception as exc:
                res.notes.append(f"play {pkg}: {type(exc).__name__}: {exc}")
        if not apple.get("found") and not play.get("found"):
            raise collect.TargetError(f"no store data for {target_text!r}")
        res.key = target.key
        fields = _fields(apple, play)
        shot_urls = (
            (apple.get("screenshot_urls") if apple.get("found") else None)
            or play.get("screenshot_urls")
            or []
        )
        shots = (
            collect.screenshots(
                f, shot_urls, Path(cfg.state_dir) / "radar-dossier" / target.key, s.screenshots, log
            )
            if s.screenshots
            else []
        )
        ctx = collect.warehouse_context(con, target, day)
        client = llm_client
        if client is None and use_llm:
            client = client_from_settings(
                llm_settings(cfg.raw.get("llm")), state_dir=Path(cfg.state_dir), run_id=run_id
            )
        dec: dict[str, Any] = {"status": "skipped", "reason": "LLM off"}
        if client is not None and fields:
            images = [ImagePart(Path(x["file"]).read_bytes(), x["mime"]) for x in shots]
            try:
                out = client.generate(deconstruct.build_request(tax, fields, images))
                dec = deconstruct.verify(out.data or {}, fields, len(images), tax)
                dec.update(
                    {
                        "basis": "llm",
                        "status": "ok",
                        "model": f"{client.provider.name}:{client.provider.model}",
                        "prompt_version": deconstruct.PROMPT_VERSION,
                        "cost_usd": round(out.cost_usd, 6),
                        "cached": out.cached,
                        "screenshots_sent": len(images),
                    }
                )
            except (BudgetExceeded, LLMError) as exc:
                dec = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:300]}
                res.notes.append(f"deconstruction: {dec['reason']}")
            items = deconstruct.support_items(dec, tax) if dec.get("status") == "ok" else []
            if items:
                # second pass: does each quote / screenshot actually support its claim?
                try:
                    sup = client.generate(deconstruct.support_request(items, images))
                    dec = deconstruct.apply_support(dec, items, sup.data or {})
                    dec["support"].update(
                        {"cost_usd": round(sup.cost_usd, 6), "cached": sup.cached}
                    )
                except (BudgetExceeded, LLMError) as exc:
                    dec["support"] = {
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {exc}"[:300],
                    }
                    res.notes.append(f"support check: {dec['support']['reason']}")
        mech = _mechanic(dec, ctx, fields, tax)
        market = collect.market_context(con, mech["value"], day, target.apple_id)
        revs = list(play.get("reviews") or [])
        apple_rev: dict[str, Any] = {"status": "skipped"}
        if s.reviews and target.apple_id:
            try:
                apple_rev = reviews_mod.fetch_apple_reviews(f, target.apple_id, cc)
            except (BudgetExhausted, HostStopped) as exc:
                apple_rev = {"status": "error", "reason": str(exc), "reviews": []}
            revs += apple_rev.get("reviews") or []
        rv: dict[str, Any] = {
            "basis": "store_reported",
            "n": len(revs),
            "sources": {
                "play": {
                    "status": "ok" if play.get("found") else "skipped",
                    "n": len(play.get("reviews") or []),
                    "order": "most_relevant (Play details page)",
                },
                "apple": {k: v for k, v in apple_rev.items() if k != "reviews"},
            },
            "stars": {
                "basis": "store_reported",
                **{str(k): sum(1 for r in revs if r["score"] == k) for k in range(1, 6)},
            },
            "replied": {
                "value": sum(1 for r in revs if r.get("replied")),
                "basis": "store_reported",
            },
            "mean_stars": round(sum(r["score"] for r in revs) / len(revs), 2) if revs else None,
            "status": "skipped",
        }
        themes = s.benchmarks.get("review_themes") or {}
        if s.reviews and revs and client is not None:
            try:
                out = client.generate(reviews_mod.mining_request(revs, themes))
                kept, dropped = reviews_mod.verify_labels(out.data or {}, revs, themes)
                rv.update(
                    {
                        "status": "ok",
                        "themes": reviews_mod.theme_counts(kept, themes, len(revs)),
                        "labelled": {"value": len(kept), "basis": "llm"},
                        "dropped": dropped,
                        "cost_usd": round(out.cost_usd, 6),
                        "model": f"{client.provider.name}:{client.provider.model}",
                    }
                )
            except (BudgetExceeded, LLMError) as exc:
                rv["status"] = "error"
                rv["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        peer = peers.compare(con, target.apple_id, mech["value"], day)
        if (
            peer["status"] == "ok"
            and s.peer_reviews
            and rv.get("status") == "ok"
            and s.radar.play_enabled
            and client is not None
        ):
            _peer_reviews(con, f, s, peer, rv, themes, client, play_parser, res)
        ads_out: dict[str, Any] = {
            "google_atc": {"status": "skipped"},
            "meta": {"status": "skipped"},
        }
        names = sorted(
            {
                x
                for x in (
                    apple.get("seller"),
                    apple.get("developer"),
                    (play.get("row") or {}).get("developer"),
                )
                if x
            }
        )
        if ads and s.google_atc:
            # the weekly extract (pulse radar adintel refresh) is read locally: no BigQuery here
            from ..adintel.google_atc import lookup_local

            ads_out["google_atc"] = lookup_local(con, names, day)
            ads_out["google_atc"]["names"] = names
        if ads:
            if meta is None:
                from ..adintel.meta_library import MetaAdLibrary

                meta = MetaAdLibrary(
                    version=s.meta_version, countries=s.meta_countries, log=log, today=day
                )
            app_name = apple.get("name") if apple.get("found") else play["row"]["name"]
            ads_out["meta"] = meta.search(search_terms=app_name)
        name = apple.get("name") if apple.get("found") else play["row"]["name"]
        d: dict[str, Any] = {
            "schema": SCHEMA,
            "as_of": day.isoformat(),
            "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "key": target.key,
            "app_key": target.app_key,
            "countries": s.countries,
            "app": {
                "name": name,
                "developer": apple.get("developer")
                if apple.get("found")
                else play["row"]["developer"],
                "seller": apple.get("seller"),
                "apple_id": target.apple_id if apple.get("found") else None,
                "play_package": target.play_package if play.get("found") else None,
                "bundle_id": apple.get("bundle_id"),
                "store_url": apple.get("store_url"),
                "play_url": (play.get("row") or {}).get("store_url"),
            },
            "store": {
                "apple": {
                    "primary_genre": apple.get("primary_genre"),
                    "genres": apple.get("genres"),
                    "content_rating": apple.get("content_rating"),
                    "file_size_mb": {"value": apple["file_size_mb"], "basis": "observed"}
                    if apple.get("file_size_mb") is not None
                    else None,
                    "storefronts": apple.get("storefronts") or [],
                    "missing_storefronts": apple.get("missing") or [],
                }
                if apple.get("found")
                else None,
                "play": _play_store(play),
            },
            "cadence": _cadence(apple, play, ctx, revs, day),
            "warehouse": ctx,
            "market": market,
            "mechanic": mech,
            "screenshots": shots,
            "texts": fields,
            "deconstruction": dec,
            "peers": peer,
            "reviews": rv,
            "ads": ads_out,
        }
        d["decision"] = rubric.decide(d, s)
        d["first_test"] = rubric.first_test(d, s, tax)
        d["meta"] = {
            "run_id": run_id,
            "requests": f.summary(),
            "llm": client.summary() if client else None,
            "bigquery_billed_gb": 0.0,  # dossiers never query BigQuery; see the weekly extract
            "notes": res.notes,
            "errors": res.errors,
            "llm_label": "LLM output: the deconstruction and review labels; every kept claim "
            "has a quote found in the store text/review or a screenshot id",
        }
        problems = check_labels(d)
        if problems:
            raise LabelError("; ".join(problems[:5]))
        res.facts = d
        res.files = write_outputs(d, s, tax, forge=forge)
    finally:
        try:
            con.execute("CHECKPOINT")
        except duckdb.Error:
            pass
        if own:
            con.close()
    return res


def dossier_dir(s: DossierSettings, key: str) -> Path:
    return s.out_dir / key


def write_outputs(
    d: dict[str, Any], s: DossierSettings, tax: Any, *, forge: bool = False
) -> dict[str, str]:
    out = dossier_dir(s, d["key"])
    out.mkdir(parents=True, exist_ok=True)
    day = d["as_of"]
    files: dict[str, str] = {}
    fp = out / f"{day}.facts.json"
    fp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    files["facts"] = str(fp)
    for lang in ("tr", "en"):
        p = out / f"{day}.{lang}.md"
        p.write_text(render.render(d, lang), encoding="utf-8")
        files[lang] = str(p)
    doc = rubric.forge_doc(d, tax)
    files["market_trends"] = str(write_feed(doc, out / f"{day}.market_trends.yaml"))
    if forge and s.radar.creative_forge_library:
        files["creative_forge"] = str(
            merge_into_forge(doc, s.radar.creative_forge_library / "market_trends.yaml")
        )
    return files


def merge_into_forge(doc: dict[str, Any], path: Path) -> Path:
    """Add (or replace) the dossier's lesson in creative-forge's market_trends.yaml, keeping the
    radar's lessons. The radar rewrites that file on its next run."""
    existing: dict[str, Any] = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    new_ids = {lesson["id"] for lesson in doc["lessons"]}
    lessons = [x for x in existing.get("lessons") or [] if x.get("id") not in new_ids]
    merged = dict(existing) if existing else dict(doc)
    merged["lessons"] = lessons + doc["lessons"]
    for k in ("version", "schema"):
        merged.setdefault(k, doc[k])
    return write_feed(merged, path)


def latest(out_dir: Path, key: str | None = None) -> list[Path]:
    """Facts files, newest day per game (all games when key is None)."""
    if not out_dir.is_dir():
        return []
    dirs = [out_dir / key] if key else sorted(p for p in out_dir.iterdir() if p.is_dir())
    out = []
    for d in dirs:
        files = sorted(d.glob("*.facts.json")) if d.is_dir() else []
        if files:
            out.append(files[-1])
    return out
