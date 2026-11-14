"""One radar run: fetch -> raw tables -> models -> Play -> classification -> checks -> digest.

Used by `pulse radar run` (standalone) and by `pulse run` when `radar.enabled` is true.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
import requests
import yaml

from .. import checks as checks_mod
from .. import runner, warehouse
from ..config import Config, ConfigError
from ..llm import ImagePart, LLMClient, client_from_settings
from ..llm import settings as llm_settings
from . import apple, classify, digest, feed, play, store
from .estimate import collect as est_collect
from .estimate import settings as est_settings
from .estimate import stage as est_stage
from .http import BudgetExhausted, Fetcher, HostStopped, RobotsDisallowed, prune_cache
from .settings import RadarSettings, from_config
from .taxonomy import load as load_taxonomy


@dataclass
class RadarResult:
    day: dt.date
    run_id: str
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checks: list[Any] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    facts: dict[str, Any] | None = None


def models_dir() -> Path:
    return Path(str(resources.files("studio_pulse.radar").joinpath("models")))


def run_models(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    out = {}
    for m in runner.resolve_order(runner.discover(models_dir())):
        body = m.sql.strip().rstrip(";")
        try:
            con.execute(f"CREATE OR REPLACE TABLE {m.name} AS\n{body}")
        except duckdb.Error as exc:
            raise runner.ModelError(f"radar model {m.name} failed: {exc}") from exc
        out[m.name] = con.execute(f"SELECT COUNT(*) FROM {m.name}").fetchone()[0]
    return out


def checks_path() -> Path:
    return Path(str(resources.files("studio_pulse.radar").joinpath("checks.yaml")))


def run_checks(con: duckdb.DuckDBPyConnection, day: dt.date) -> list[checks_mod.CheckResult]:
    checks = checks_mod.load_checks(checks_path()) + checks_mod.load_checks(est_stage.checks_path())
    return checks_mod.run_checks(con, checks, None, day)


def _guard_out_dir(path: Path) -> None:
    """Radar output holds store data: never write it into the public demo site."""
    parts = [p.lower() for p in path.resolve().parts]
    for i in range(len(parts) - 1):
        if parts[i] == "docs" and parts[i + 1] == "demo":
            raise ConfigError(f"radar.out_dir {path} is inside docs/demo (public); keep it local")


def _play_map(path: Path | None) -> dict[str, str | None]:
    if not path or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): (str(v) if v else None) for k, v in (data.get("apple_to_play") or {}).items()}


def run_radar(
    cfg: Config,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    session: requests.Session | None = None,
    day: dt.date | None = None,
    llm_client: LLMClient | None = None,
    use_llm: bool = True,
    max_requests: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
    s: RadarSettings | None = None,
) -> RadarResult:
    s = s or from_config(cfg)
    es = est_settings.from_config(cfg)
    day = day or s.as_of or cfg.as_of or dt.date.today()
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    res = RadarResult(day=day, run_id=run_id)
    _guard_out_dir(s.out_dir)
    own = con is None
    con = con or warehouse.connect(cfg.warehouse)
    started = store.utcnow()
    tax = load_taxonomy()
    try:
        store.init_schema(con)
        est_stage.init_schema(con)
        store.set_params(con, s.params)
        store.set_config(
            con, {"as_of": day.isoformat(), "country": s.countries, "trend_list": s.trend_list}
        )
        if session is None and s.replay is not None:
            from .replay import RadarReplaySession

            session = RadarReplaySession(s.replay)
            sleep = lambda _s: None  # noqa: E731 - nothing is sent in a replay
        cache_dir = Path(cfg.state_dir) / "radar-cache"
        prune_cache(cache_dir, s.cache_keep_days, day)
        f = Fetcher(
            con,
            day,
            session=session,
            run_id=run_id,
            budgets={"itunes.apple.com": s.apple_budget, "play.google.com": s.play_budget},
            spacing={"itunes.apple.com": s.apple_spacing_s, "play.google.com": s.play_spacing_s},
            max_total=max_requests,
            cache_dir=cache_dir,
            sleep=sleep,
            clock=clock,
            log=log,
        )
        _charts(con, f, s, day, res, log)
        _lookups(con, f, s, day, res, log)
        _publishers(con, f, s, day, res, log)
        est_collect.lookup_watched(con, f, s, day, res, log)
        _settle_ratings(con, f, s, day, res, log)
        res.counts["models_1"] = run_models(con)
        if s.play_enabled:
            _play(con, f, s, day, res, log)
            # watched games and the calibration panel (estimates), within the Play budget
            panel = es if es.enabled else dataclasses.replace(es, panel_size=0)
            est_collect.collect_play(con, f, s, panel, day, res, log)
        run_models(con)
        charted = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT app_id FROM stg_mr_chart WHERE snapshot_date = ? ORDER BY 1", [day]
            ).fetchall()
        ]
        classify.store_profiles(con, charted, day)
        res.counts["keyword_profiles"] = classify.keyword_profiles(
            con, charted, tax, s.primary_country
        )
        client = llm_client
        if client is None and use_llm:
            ls = llm_settings(cfg.raw.get("llm"))
            client = client_from_settings(ls, state_dir=Path(cfg.state_dir), run_id=run_id)
        candidates = [
            r[0]
            for r in con.execute(
                "SELECT app_id FROM fct_mr_breakout_daily WHERE snapshot_date = ? AND "
                "(radar_class IS NOT NULL OR age_days <= ?) "
                "ORDER BY radar_class IS NULL, momentum DESC, app_id LIMIT ?",
                [day, s.params["age_max"], s.llm_max_apps],
            ).fetchall()
        ]
        images_for = _screenshots(con, f, s.llm_screenshots) if s.llm_screenshots else None
        res.counts["llm"] = classify.classify_apps(
            con,
            candidates,
            client,
            tax,
            primary_country=s.primary_country,
            images_for=images_for,
            log=log,
        )
        res.counts["models"] = run_models(con)
        res.counts["estimate_models"] = est_stage.run_models(con)
        est_summary = None
        if es.enabled:
            try:
                est_summary = est_stage.compute(con, s.countries, es, day, log)
            except Exception as exc:  # the day's data is stored; say so instead of hiding it
                res.errors.append(f"estimates: {type(exc).__name__}: {exc}")
                log(f"    radar: estimates failed: {exc}")
        res.checks = run_checks(con, day)
        store.insert(
            con,
            "raw_mr_check",
            [
                {
                    "run_id": run_id,
                    "checked_at": store.utcnow(),
                    "name": c.name,
                    "severity": c.severity,
                    "status": c.status,
                    "failing_rows": c.failing_rows,
                    "message": c.message,
                }
                for c in res.checks
            ],
        )
        lists = con.execute(
            "SELECT COUNT(*), list(DISTINCT country ORDER BY country) FROM stg_mr_list_days "
            "WHERE snapshot_date = ?",
            [day],
        ).fetchone()
        meta = {
            "run_id": run_id,
            "requests": f.summary(),
            "lists_observed": int(lists[0] or 0),
            "countries_observed": list(lists[1] or []),
            "llm": client.summary() if client else None,
            "errors": res.errors,
            "notes": res.notes,
        }
        facts = digest.build_facts(con, s, day, tax, meta=meta, checks=res.checks)
        if est_summary is not None:
            est_stage.add_facts(con, facts, day, est_summary)
        if s.narrate and client is not None:
            facts["meta"]["narration"] = digest.narrate(facts, client, log)
            facts["meta"]["llm"] = client.summary()
        res.facts = facts
        res.files = write_outputs(facts, s, tax)
        res.counts["requests"] = f.summary()
        res.ok = not res.errors and not any(c.is_blocking for c in res.checks)
    except Exception as exc:
        res.ok = False
        res.errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            store.insert(
                con,
                "raw_mr_run",
                [
                    {
                        "run_id": run_id,
                        "snapshot_date": day,
                        "started_at": started,
                        "finished_at": store.utcnow(),
                        "status": "success" if res.ok else "failed",
                        "requests": (res.counts.get("requests") or {}).get("sent"),
                        "llm_cost_usd": ((res.facts or {}).get("meta", {}).get("llm") or {}).get(
                            "cost_usd"
                        ),
                        "summary": json.dumps(
                            {"errors": res.errors, "notes": res.notes}, default=str
                        ),
                    }
                ],
            )
            con.execute("CHECKPOINT")
        finally:
            if own:
                con.close()
    return res


def write_outputs(facts: dict[str, Any], s: RadarSettings, tax: Any) -> dict[str, str]:
    out = s.out_dir
    out.mkdir(parents=True, exist_ok=True)
    day = facts["as_of"]
    narr = (facts.get("meta") or {}).get("narration") or {}
    files = {}
    for lang in ("tr", "en"):
        text = digest.render(facts, lang, narr.get(lang) if narr.get("status") == "ok" else None)
        text += est_stage.render_md(facts, lang)
        p = out / f"{day}.{lang}.md"
        p.write_text(text, encoding="utf-8")
        files[lang] = str(p)
    fp = out / f"{day}.facts.json"
    fp.write_text(json.dumps(facts, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    files["facts"] = str(fp)
    doc = feed.build(facts, tax)
    files["market_trends"] = str(feed.write(doc, out / "market_trends.yaml"))
    if s.creative_forge_library:
        files["creative_forge"] = str(
            feed.write(doc, s.creative_forge_library / "market_trends.yaml")
        )
    return files


# -- stages -------------------------------------------------------------------------------


def _stop(exc: Exception, res: RadarResult, what: str, log: Callable[[str], None]) -> None:
    msg = f"{what}: {exc}"
    (res.notes if isinstance(exc, (BudgetExhausted, HostStopped)) else res.errors).append(msg)
    log(f"    radar: {msg}")


def _charts(con, f: Fetcher, s: RadarSettings, day, res: RadarResult, log) -> None:
    n = 0
    for cc in s.countries:
        for spec in s.charts_for(cc):
            try:
                rows = apple.fetch_chart(f, day, cc, spec.chart, spec.genre, spec.limit)
            except (BudgetExhausted, HostStopped) as exc:
                _stop(exc, res, f"rss {cc} {spec.key}", log)
                return
            except Exception as exc:
                _stop(exc, res, f"rss {cc} {spec.key}", log)
                continue
            store.replace(
                con,
                "raw_mr_chart",
                "snapshot_date = ? AND store = 'apple' AND country = ? AND chart = ? "
                "AND genre_id = ?",
                [day, cc, spec.chart, spec.genre],
                rows,
            )
            n += len(rows)
    res.counts["chart_rows"] = n
    log(f"    radar: {n} chart rows")


def _lookups(con, f: Fetcher, s: RadarSettings, day, res: RadarResult, log) -> None:
    total = 0
    for cc in s.countries:
        ids = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT app_id FROM raw_mr_chart WHERE snapshot_date = ? AND country = ? "
                "AND rank <= requested_limit AND app_id IS NOT NULL ORDER BY 1",
                [day, cc],
            ).fetchall()
        ]
        if not ids:
            continue
        try:
            found, missing = apple.lookup_ids(f, ids, cc, batch=s.lookup_batch)
        except (BudgetExhausted, HostStopped) as exc:
            _stop(exc, res, f"lookup {cc}", log)
            return
        except Exception as exc:
            _stop(exc, res, f"lookup {cc}", log)
            continue
        rows = [apple.app_row(r, day, cc, "lookup") for r in found]
        store.replace(
            con,
            "raw_mr_app",
            "snapshot_date = ? AND store = 'apple' AND country = ? AND source = 'lookup'",
            [day, cc],
            rows,
        )
        _record_reads(con, day, f.run_id, cc, "batch", found)
        total += len(rows)
        if missing:
            res.notes.append(f"lookup {cc}: {len(missing)} charted app(s) not returned")
    res.counts["lookup_rows"] = total


def _record_reads(con, day, run_id: str, cc: str, source: str, results: list[dict]) -> None:
    now = store.utcnow()
    store.insert(
        con,
        "raw_mr_lookup_read",
        [
            {
                "snapshot_date": day,
                "run_id": run_id,
                "ts": now,
                "country": cc,
                "app_id": str(x["trackId"]),
                "source": source,
                "rating_count": int(x["userRatingCount"])
                if x.get("userRatingCount") is not None
                else None,
            }
            for x in results
        ],
    )


def _suspects(con, day, cc: str) -> dict[str, str]:
    """Apps whose rating count today is not trustworthy yet, with the reason: zero or missing,
    lower than the previous snapshot, or different between today's reads."""
    rows = con.execute(
        """
        WITH reads AS (
          SELECT app_id, MAX(rating_count) AS hi, MIN(rating_count) AS lo,
                 COUNT(*) FILTER (WHERE rating_count IS NULL) AS nulls, COUNT(*) AS n
          FROM raw_mr_lookup_read WHERE snapshot_date = ? AND country = ? GROUP BY 1
        ), prev AS (
          SELECT app_id, rating_count AS prev FROM raw_mr_app
          WHERE store = 'apple' AND source = 'lookup' AND country = ? AND snapshot_date < ?
            AND rating_count IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC) = 1
        )
        SELECT r.app_id,
          CASE WHEN r.hi IS NULL OR r.hi = 0 THEN 'zero_or_missing'
               WHEN p.prev IS NOT NULL AND r.hi < p.prev THEN 'below_previous'
               WHEN r.hi <> r.lo OR (r.nulls > 0 AND r.nulls < r.n) THEN 'reads_differ'
          END AS why
        FROM reads AS r LEFT JOIN prev AS p USING (app_id)
        """,
        [day, cc, cc, day],
    ).fetchall()
    return {a: w for a, w in rows if w}


def _settle_ratings(
    con, f: Fetcher, s: RadarSettings, day, res: RadarResult, log, rechecks: int = 2
) -> None:
    """Re-query suspicious Lookup rating counts (up to `rechecks` times, through the polite
    fetcher) and keep each app's maximum read of the day: counts only grow within a version,
    and a transient 0 must not reach the velocity score or the reset flag."""
    summary: dict[str, dict[str, int]] = {}
    for cc in s.countries:
        first = _suspects(con, day, cc)
        if not first:
            continue
        st = summary.setdefault(cc, {"suspects": len(first), "requests": 0, "settled": 0})
        todo = dict(first)
        rechecked: set[str] = set()
        for attempt in range(1, rechecks + 1):
            if not todo:
                break
            before = f.sent
            try:
                found, _ = apple.lookup_ids(
                    f,
                    sorted(todo),
                    cc,
                    batch=s.lookup_batch,
                    purpose=f"lookup_recheck:{cc}",
                    cacheable=False,
                )
            except (BudgetExhausted, HostStopped) as exc:
                _stop(exc, res, f"lookup recheck {cc}", log)
                break
            except Exception as exc:
                _stop(exc, res, f"lookup recheck {cc}", log)
                break
            st["requests"] += f.sent - before
            rechecked |= set(todo)
            _record_reads(con, day, f.run_id, cc, f"recheck{attempt}", found)
            reads = {
                a: [
                    x
                    for x in con.execute(
                        "SELECT rating_count FROM raw_mr_lookup_read WHERE snapshot_date = ? "
                        "AND country = ? AND app_id = ? ORDER BY ts, source",
                        [day, cc, a],
                    ).fetchall()
                ]
                for a in todo
            }
            changed = {a: [r[0] for r in v] for a, v in reads.items() if len({r[0] for r in v}) > 1}
            if changed:
                f.annotate_last(
                    "rating counts differ between reads: "
                    + json.dumps(changed, separators=(",", ":"))
                )
            still = _suspects(con, day, cc)
            # a disagreement is settled by one more read (the maximum wins); a zero or a drop
            # gets the second recheck too
            todo = {a: w for a, w in still.items() if a in todo and w != "reads_differ"}
        con.execute(
            """
            UPDATE raw_mr_app AS a SET rating_count = m.hi
            FROM (SELECT app_id, MAX(rating_count) AS hi FROM raw_mr_lookup_read
                  WHERE snapshot_date = ? AND country = ? GROUP BY 1) AS m
            WHERE a.snapshot_date = ? AND a.store = 'apple' AND a.country = ?
              AND a.source = 'lookup' AND a.app_id = m.app_id AND m.hi IS NOT NULL
              AND (a.rating_count IS NULL OR a.rating_count < m.hi)
            """,
            [day, cc, day, cc],
        )
        final = _suspects(con, day, cc)
        # settled: re-read, and now either clean or only a disagreement resolved by the maximum
        st["settled"] = sum(1 for a in rechecked if final.get(a) in (None, "reads_differ"))
    if summary:
        res.counts["lookup_recheck"] = summary
        log(f"    radar: rating rechecks {summary}")


def _publishers(con, f: Fetcher, s: RadarSettings, day, res: RadarResult, log) -> None:
    cc = s.primary_country
    for p in s.publishers:
        last = con.execute(
            "SELECT MAX(snapshot_date) FROM raw_mr_app WHERE source = 'publisher' AND developer_id = ?",
            [p["id"]],
        ).fetchone()[0]
        if last is not None and (day - last).days < s.publisher_every_days:
            continue
        try:
            apps = apple.lookup_publisher(f, p["id"], cc)
        except (BudgetExhausted, HostStopped) as exc:
            _stop(exc, res, f"publisher {p['name']}", log)
            return
        except Exception as exc:
            _stop(exc, res, f"publisher {p['name']}", log)
            continue
        rows = [apple.app_row(a, day, cc, "publisher") for a in apps]
        for r in rows:
            r["developer_id"] = p["id"]
        _record_reads(con, day, f.run_id, cc, "publisher", apps)
        store.replace(
            con,
            "raw_mr_app",
            "snapshot_date = ? AND source = 'publisher' AND developer_id = ?",
            [day, p["id"]],
            rows,
        )
        res.counts[f"publisher:{p['id']}"] = len(rows)


def _play(con, f: Fetcher, s: RadarSettings, day, res: RadarResult, log) -> None:
    manual = _play_map(s.play_map_file)
    cands = con.execute(
        "SELECT app_id, bundle_id FROM fct_mr_breakout_daily WHERE snapshot_date = ? AND "
        "age_days <= ? ORDER BY momentum DESC, app_id",
        [day, s.params["late_age_max"]],
    ).fetchall()
    xref = {
        r[0]: (r[1], r[2], r[3])
        for r in con.execute(
            "SELECT apple_id, play_id, status, verified_on FROM raw_mr_xref "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY apple_id ORDER BY verified_on DESC) = 1"
        ).fetchall()
    }
    jobs: list[tuple[str | None, str, str]] = []  # (apple_id, package, method)
    for apple_id, bundle in cands:
        if apple_id in manual:
            if manual[apple_id]:
                jobs.append((apple_id, manual[apple_id], "manual"))
            elif xref.get(apple_id, (None, None, None))[1] != "manual_none":
                store.insert(
                    con,
                    "raw_mr_xref",
                    [
                        {
                            "apple_id": apple_id,
                            "bundle_id": bundle,
                            "play_id": None,
                            "method": "manual",
                            "status": "manual_none",
                            "verified_on": day,
                        }
                    ],
                )
            continue
        known = xref.get(apple_id)
        if known and known[1] == "mapped":
            jobs.append((apple_id, known[0], "known"))
        elif known and known[1] == "not_found" and (day - known[2]).days < s.play_retry_days:
            continue
        elif bundle:
            jobs.append((apple_id, bundle, "bundle_id"))
    jobs += [(None, pkg, "watch") for pkg in s.play_watch]
    done: set[str] = set()
    fetched = mapped = 0
    for apple_id, pkg, method in jobs:
        if pkg in done:
            continue
        done.add(pkg)
        have = con.execute(
            "SELECT 1 FROM raw_mr_app WHERE snapshot_date = ? AND store = 'play' "
            "AND app_id = ? LIMIT 1",
            [day, pkg],
        ).fetchone()
        if have:
            continue
        try:
            d = play.fetch_details(f, pkg, s.play_country, s.play_lang)
        except (BudgetExhausted, HostStopped, RobotsDisallowed) as exc:
            _stop(exc, res, "play", log)
            break
        except Exception as exc:
            _stop(exc, res, f"play {pkg}", log)
            continue
        fetched += 1
        if d is None:
            if apple_id:
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
        row = play.app_row(d, day, s.play_country)
        store.replace(
            con,
            "raw_mr_app",
            "snapshot_date = ? AND store = 'play' AND app_id = ?",
            [day, pkg],
            [row],
        )
        if apple_id and method != "known":
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
        mapped += 1
    res.counts["play"] = {"requested": fetched, "stored": mapped}
    log(f"    radar: Play details {mapped} stored of {fetched} requested")


def _screenshots(con, f: Fetcher, n: int) -> Callable[[str], list[ImagePart]]:
    def get(app_id: str) -> list[ImagePart]:
        row = con.execute(
            "SELECT screenshot_urls FROM raw_mr_app WHERE store = 'apple' AND "
            "app_id = ? ORDER BY snapshot_date DESC LIMIT 1",
            [app_id],
        ).fetchone()
        urls = json.loads(row[0] or "[]")[:n] if row else []
        out = []
        for u in urls:
            try:
                r = f.get(u, "screenshot", cacheable=False)
            except Exception:
                break
            if r.status == 200:
                out.append(ImagePart(r.content, "image/jpeg"))
        return out

    return get


def run_in_pipeline(
    con: duckdb.DuckDBPyConnection, cfg: Config, log: Callable[[str], None]
) -> list[checks_mod.CheckResult]:
    """`pulse run` hook: run the radar on the open warehouse; returns its check results."""
    log("Market radar ...")
    res = run_radar(cfg, con=con, log=log)
    for e in res.errors:
        log(f"    radar error: {e}")
    if res.files:
        log(f"    radar digest: {res.files.get('tr')}")
    if res.errors:
        raise RuntimeError("; ".join(res.errors[:3]))
    return res.checks


def enabled(cfg: Config) -> bool:
    return bool((cfg.raw.get("radar") or {}).get("enabled"))
