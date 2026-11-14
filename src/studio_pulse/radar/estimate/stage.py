"""The estimates' part of a radar run: schema, models, collection hooks, computation, facts.

run.py calls, in order: `init_schema`, `lookup_watched` (after the charts' Lookups),
`collect_play` (after the radar's own Play pages), `compute` (after the models), and
`add_facts` / `render_md` for the digest. Results go to raw_mr_est_* (one slice per day).
"""

from __future__ import annotations

import datetime as dt
import random
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb

from ... import runner
from .. import digest, store
from . import downloads, revenue
from .settings import EstimateSettings

KINDS = {
    "android_installs_ww": {
        "tr": "Android kurulum/gün (dünya)",
        "en": "Android installs/day (worldwide)",
    },
    "ios_downloads_ww": {"tr": "iOS indirme/gün (dünya)", "en": "iOS downloads/day (worldwide)"},
    "ios_revenue": {"tr": "iOS hasılat/gün (ülke, USD)", "en": "iOS revenue/day (country, USD)"},
}


def _pkg_file(name: str) -> Path:
    return Path(str(resources.files("studio_pulse.radar.estimate").joinpath(name)))


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    sql = _pkg_file("schema.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    for stmt in body.split(";"):
        if stmt.strip():
            con.execute(stmt)


def checks_path() -> Path:
    return _pkg_file("checks.yaml")


def run_models(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    out = {}
    for m in runner.resolve_order(runner.discover(_pkg_file("models"))):
        body = m.sql.strip().rstrip(";")
        try:
            con.execute(f"CREATE OR REPLACE TABLE {m.name} AS\n{body}")
        except duckdb.Error as exc:
            raise runner.ModelError(f"radar estimate model {m.name} failed: {exc}") from exc
        out[m.name] = con.execute(f"SELECT COUNT(*) FROM {m.name}").fetchone()[0]
    return out


def _replace_day(con, table: str, col: str, day: dt.date, rows: list[dict[str, Any]]) -> None:
    store.replace(con, table, f"{col} = ?", [day], rows)


def compute(
    con: duckdb.DuckDBPyConnection,
    countries: list[str],
    es: EstimateSettings,
    day: dt.date,
    log=print,
) -> dict[str, Any]:
    """Calibrate, backtest and band every charted game; store everything; return a summary."""
    rng = random.Random(es.seed)
    run_models(con)
    # -- downloads
    series = downloads.play_series(con)
    stale = downloads.counter_staleness(series)
    windows = downloads.refresh_windows(series, es)
    pts, exposure = downloads.points_by_list(con, windows, es)
    lists = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT list_key FROM stg_mr_list_days WHERE chart = ? AND snapshot_date = ?",
            [downloads.TOPFREE, day],
        ).fetchall()
    ]
    curves = downloads.calibrate(pts, lists, es, rng)
    _replace_day(
        con,
        "raw_mr_est_window",
        "computed_on",
        day,
        [
            {
                "computed_on": day,
                "play_id": w.play_id,
                "apple_ids": ",".join(w.apple_ids),
                "t0": w.t0,
                "t1": w.t1,
                "days": round(w.days, 3),
                "installs0": w.installs0,
                "installs1": w.installs1,
                "ratings0": w.ratings0,
                "ratings1": w.ratings1,
                "rate": w.rate,
                "status": w.status,
            }
            for w in windows
        ],
    )
    _replace_day(
        con,
        "raw_mr_est_exposure",
        "computed_on",
        day,
        [
            {
                "computed_on": day,
                "play_id": w.play_id,
                "t0": w.t0,
                "country": e.country,
                "list_key": e.list_key,
                "days_observed": e.days_observed,
                "days_listed": e.days_listed,
                "mean_rank": e.mean_rank,
            }
            for w, e in exposure
        ],
    )
    fits: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    for lk, c in sorted(curves.items()):
        f = c.fit
        fits.append(
            {
                "computed_on": day,
                "kind": "downloads",
                "country": c.country,
                "list_key": lk,
                "status": c.status,
                "n_games": c.n_games,
                "n_obs": c.n_obs,
                "min_games": es.min_games,
                "a": f.a if f else None,
                "b": f.b if f else None,
                "b_low": f.b_band[0] if f else None,
                "b_high": f.b_band[1] if f else None,
                "sigma": f.sigma if f else None,
                "rank_min": f.rank_min if f else None,
                "rank_max": f.rank_max if f else None,
                "method": "log(Play installs/day) = a - b log(mean rank); cluster bootstrap",
                "note": c.note,
            }
        )
        if c.status == "ok":
            for scheme, row in downloads.backtest(pts[lk], es, rng).items():
                tests.append(
                    {
                        "computed_on": day,
                        "kind": "downloads",
                        "country": c.country,
                        "list_key": lk,
                        "scheme": scheme,
                        "data": "live",
                        "nominal": es.band_hi - es.band_lo,
                        "note": None,
                        **row,
                    }
                )
    split = downloads.load_platform_split()
    games = downloads.game_bands(con, day, curves, split, es, rng)
    # -- revenue
    doc = revenue.load_anchors()
    anchors = doc["anchors"]
    ranks = {a["id"]: revenue.anchor_ranks(con, a) for a in anchors}
    model = revenue.build(doc, anchors, ranks, countries, es, rng)
    for cc in countries:
        ok = model is not None and cc in model.country_markets
        fits.append(
            {
                "computed_on": day,
                "kind": "revenue",
                "country": cc,
                "list_key": f"{cc}:{revenue.LIST}",
                "status": "ok" if ok else "no_source",
                "n_games": len(model.anchors) if model else 0,
                "n_obs": sum(len(ranks[a]) for a in model.anchors) if model else 0,
                "min_games": 2,
                "a": None,
                "b": None,
                "b_low": model.slope[0] if model else None,
                "b_high": model.slope[1] if model else None,
                "sigma": None,
                "rank_min": None,
                "rank_max": None,
                "method": "public anchors x rank^-b x country market size (revenue_anchors.yaml)",
                "note": None if ok else "no public market size for this country",
            }
        )
    if model is not None:
        games += revenue.game_bands(con, day, model, es, rng)
        for row in revenue.validate(doc, anchors, ranks, countries, es, rng):
            tests.append(
                {
                    "computed_on": day,
                    "kind": "revenue",
                    "list_key": f"{row['country']}:{revenue.LIST}",
                    "data": "live",
                    "nominal": es.band_hi - es.band_lo,
                    **row,
                }
            )
    _replace_day(con, "raw_mr_est_fit", "computed_on", day, fits)
    _replace_day(con, "raw_mr_est_backtest", "computed_on", day, tests)
    _replace_day(con, "raw_mr_est_game", "snapshot_date", day, games)
    by_status: dict[str, int] = {}
    for w in windows:
        by_status[w.status] = by_status.get(w.status, 0) + 1
    summary = {
        "band_pct": es.band_pct,
        "play_packages": len(series),
        "counter_pairs": stale,
        "windows": by_status,
        "download_lists": {
            lk: {"status": c.status, "n_games": c.n_games, "note": c.note}
            for lk, c in curves.items()
        },
        "revenue_countries": sorted(model.country_markets) if model else [],
        "revenue_anchors": model.anchors if model else [],
        "game_rows": len(games),
        "backtests": [
            {k: v for k, v in t.items() if k != "computed_on" and v is not None} for t in tests
        ],
    }
    log(
        f"    radar: estimates {len(games)} game bands; download lists ok "
        f"{sum(c.status == 'ok' for c in curves.values())}/{len(curves)}; windows {by_status}"
    )
    return summary


# -- digest -----------------------------------------------------------------------------------


def _est(row: dict[str, Any]) -> dict[str, Any]:
    return digest.estimate(
        row["low"],
        row["high"],
        row["method"],
        int(row["n"]),
        mid=row["mid"],
        unit=row["unit"],
        kind=row["kind"],
        country=row["country"],
        list=row["list_key"].split(":", 1)[1],
        rank=row["rank"],
    )


def add_facts(
    con: duckdb.DuckDBPyConnection, facts: dict[str, Any], day: dt.date, summary: dict[str, Any]
) -> None:
    """Attach bands to the digest items and a per-country grossing top 5; summary to meta."""
    rows = digest._rows(
        con,
        "SELECT g.*, c.name FROM raw_mr_est_game AS g LEFT JOIN (SELECT DISTINCT ON (app_id) "
        "app_id, name FROM stg_mr_chart WHERE snapshot_date = ?) AS c USING (app_id) "
        "WHERE g.snapshot_date = ? ORDER BY g.kind, g.country, g.rank",
        [day, day],
    )
    by_app: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_app.setdefault(r["app_id"], []).append(r)
    for it in facts.get("items") or []:
        it["market_estimates"] = [_est(r) for r in by_app.get(it["app_id"], [])]
    top = [
        {"app_id": r["app_id"], "name": r["name"], "band": _est(r)}
        for r in rows
        if r["kind"] == "ios_revenue" and r["rank"] <= 3
    ]
    facts["estimates"] = {"grossing_top": top}
    facts.setdefault("meta", {})["estimates"] = summary
    problems = digest.check_labels(facts)
    if problems:
        raise digest.LabelError("; ".join(problems[:5]))


T = {
    "tr": {
        "title": "Tahminler (TAHMİN)",
        "intro": (
            "Her bant %{pct} aralıktır (alt-üst), ortası ayrıca yazılır; dayanağı yanındadır. "
            "İndirme eğrileri stüdyonun kendi Play sayaç pencerelerinden, hasılat eğrisi kamuya "
            "açık şirket açıklamalarından kalibre edilir (docs/RADAR.md, 'Ne kadar doğru?')."
        ),
        "dl_status": "İndirme eğrileri: {ok}/{total} liste hazır.",
        "dl_none": "Hiçbir liste için yeterli Play sayaç penceresi yok (kullanılabilir 7+ günlük pencere: {w}); indirme tahmini gösterilmiyor.",
        "insufficient": "yetersiz veri",
        "stale": "Play sayacı: {pairs} ardışık okumanın {stale} tanesinde kurulum sayısı değişmedi, puan sayısı arttı (sayaç yenilenmemiş).",
        "rev": "Hasılat (iOS, ülke içi, USD/gün), her ülkede en çok hasılat yapan ilk 3",
        "rev_none": "Hasılat tahmini yok ({why}).",
        "acc": "Doğruluk (örneklem dışı)",
        "items": "Bugünkü adaylar için bantlar",
        "no_source": "kamu kaynağı yok",
    },
    "en": {
        "title": "Estimates (ESTIMATE)",
        "intro": (
            "Every band is a {pct}% interval (low-high) with its middle; its basis is next to "
            "it. Download curves are calibrated on the studio's own Play counter windows, the "
            "revenue curve on public company disclosures (docs/RADAR.md, 'How accurate is this')."
        ),
        "dl_status": "Download curves: {ok}/{total} lists ready.",
        "dl_none": "No list has enough Play counter windows (usable windows of 7+ days: {w}); no download estimate is shown.",
        "insufficient": "insufficient data",
        "stale": "Play counter: in {stale} of {pairs} consecutive reads the install count did not change while the rating count grew (counter not refreshed).",
        "rev": "Revenue (iOS, in-country, USD/day), top 3 grossing per country",
        "rev_none": "No revenue estimate ({why}).",
        "acc": "Accuracy (out of sample)",
        "items": "Bands for today's candidates",
        "no_source": "no public source",
    },
}


def render_md(facts: dict[str, Any], lang: str) -> str:
    meta = (facts.get("meta") or {}).get("estimates")
    if not meta:
        return ""
    t = T[lang]
    lab = digest.LABELS[lang]["estimate"]
    L = ["", f"## {t['title']}", "", t["intro"].format(pct=meta.get("band_pct", 80)), ""]
    dl = meta.get("download_lists") or {}
    ok = [k for k, v in dl.items() if v["status"] == "ok"]
    L.append("- " + t["dl_status"].format(ok=len(ok), total=len(dl)))
    if not ok:
        L.append("  " + t["dl_none"].format(w=(meta.get("windows") or {}).get("ok", 0)))
    short = [
        f"{digest._list_name(lang, k.split(':', 1)[1])} {k.split(':')[0].upper()} "
        f"(n={v['n_games']})"
        for k, v in sorted(dl.items())
        if v["status"] != "ok"
    ]
    if short:
        L.append(
            f"  {t['insufficient']}: " + ", ".join(short[:12]) + (" …" if len(short) > 12 else "")
        )
    cp = meta.get("counter_pairs") or {}
    if cp.get("pairs"):
        L.append("- " + t["stale"].format(pairs=cp["pairs"], stale=cp["stale"]))
    L += ["", f"### {t['rev']}", ""]
    top = (facts.get("estimates") or {}).get("grossing_top") or []
    if not top:
        L.append(t["rev_none"].format(why=t["no_source"]))
    for g in top:
        b = g["band"]
        L.append(
            f"- {b['country'].upper()} #{b['rank']} {g['name']}: "
            f"{digest.fmt_compact(b['low'], lang)}–{digest.fmt_compact(b['high'], lang)} "
            f"({lab}; {b['method']}; n={b['n']})"
        )
    with_est = [i for i in facts.get("items") or [] if i.get("market_estimates")]
    if with_est:
        L += ["", f"### {t['items']}", ""]
        for it in with_est[:10]:
            parts = []
            for e in it["market_estimates"][:4]:
                parts.append(
                    f"{KINDS[e['kind']][lang]} {e['country'].upper()} #{e['rank']}: "
                    f"{digest.fmt_compact(e['low'], lang)}–{digest.fmt_compact(e['high'], lang)}"
                )
            L.append(f"- {it['name']}: " + "; ".join(parts) + f" ({lab})")
    tests = meta.get("backtests") or []
    if tests:
        L += ["", f"### {t['acc']}", ""]
        for x in tests:
            cov = x.get("coverage")
            mape = x.get("mape")
            L.append(
                f"- {x['kind']} {x.get('list_key', '')} {x['scheme']} ({x['data']}): n={x['n']}"
                + (f", MAPE {mape * 100:.0f}%" if mape is not None else "")
                + (
                    f", {lab} bandı içinde {cov * 100:.0f}%"
                    if cov is not None and lang == "tr"
                    else ""
                )
                + (
                    f", inside the band {cov * 100:.0f}%"
                    if cov is not None and lang == "en"
                    else ""
                )
            )
    return "\n".join(L).rstrip() + "\n"
