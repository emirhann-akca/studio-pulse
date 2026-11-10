"""End-to-end pipeline: ingest every configured source, run models, alerts."""

from __future__ import annotations

import datetime as dt
import glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from . import alerts as alerts_mod
from . import checks as checks_mod
from . import runner, runstate, warehouse
from .config import AppConfig, Config, ConfigError
from .connectors import admob, crashlytics, ga4, revenuecat, stores, unity_jsonl
from .connectors.dialect import BigQueryDialect, DuckDBDialect
from .connectors.executors import BigQueryExecutor, DuckDBExecutor, QueryStat

SOURCE_TYPES = (
    "ga4_bigquery",
    "ga4_files",
    "crashlytics_bigquery",
    "crashlytics_files",
    "revenuecat",
    "admob",
    "play_console",
    "app_store",
    "unity_jsonl",
)


@dataclass
class IngestReport:
    loaded: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    bigquery: list[QueryStat] = field(default_factory=list)


def _paths(cfg: Config, spec: str | list[str]) -> list[Path]:
    specs = spec if isinstance(spec, list) else [spec]
    out: list[Path] = []
    for s in specs:
        p = cfg.resolve(s)
        if any(ch in str(p) for ch in "*?["):
            out += [Path(x) for x in sorted(glob.glob(str(p), recursive=True))]
        elif p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.suffix in (".parquet", ".json", ".jsonl"))
        elif p.exists():
            out.append(p)
    if not out:
        raise FileNotFoundError(f"no files match {spec}")
    return out


def _ingest_source(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    app: AppConfig,
    kind: str,
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    dry_run: bool,
    bq_stats: list[QueryStat],
    log: Any,
    bq_client: Any = None,
) -> Any:
    name = src.get("name", kind)
    if kind in ("ga4_bigquery", "ga4_files"):
        opts = ga4.GA4Options.from_source(src)
        opts.timezone = app.timezone
        if kind == "ga4_bigquery":
            dialect = BigQueryDialect(src["project"], src["dataset"])
            ex = BigQueryExecutor(
                billing_project=src.get("billing_project", src["project"]),
                max_bytes_billed=min(
                    int(src.get("max_bytes_billed", cfg.max_bytes_billed)), cfg.max_bytes_billed
                ),
                location=src.get("location"),
                dry_run_only=dry_run,
                client=bq_client,
                log=log,
            )
        else:
            dialect = DuckDBDialect()
            ex = DuckDBExecutor({"ga4_events": _paths(cfg, src["path"])})
            if dry_run:
                return "skipped (dry run)"
        try:
            return ga4.ingest(
                con,
                app_id=app.id,
                source=name,
                dialect=dialect,
                executor=ex,
                start=start,
                end=end,
                opts=opts,
                log=log,
            )
        finally:
            bq_stats.extend(ex.stats if kind == "ga4_bigquery" else [])
    if kind in ("crashlytics_bigquery", "crashlytics_files"):
        if kind == "crashlytics_bigquery":
            dialect = BigQueryDialect(src["project"], src["dataset"])
            ex = BigQueryExecutor(
                billing_project=src.get("billing_project", src["project"]),
                max_bytes_billed=min(
                    int(src.get("max_bytes_billed", cfg.max_bytes_billed)), cfg.max_bytes_billed
                ),
                location=src.get("location"),
                dry_run_only=dry_run,
                client=bq_client,
                log=log,
            )
            tables = list(src["tables"])
        else:
            dialect = DuckDBDialect()
            ex = DuckDBExecutor({"crashlytics_events": _paths(cfg, src["path"])})
            tables = ["crashlytics_events"]
            if dry_run:
                return "skipped (dry run)"
        try:
            return crashlytics.ingest(
                con,
                app_id=app.id,
                source=name,
                dialect=dialect,
                executor=ex,
                tables=tables,
                start=start,
                end=end,
                timezone=app.timezone,
                log=log,
            )
        finally:
            bq_stats.extend(ex.stats if kind == "crashlytics_bigquery" else [])
    if dry_run:
        return "skipped (dry run)"
    if kind == "revenuecat":
        path = src["path"]
        if not str(path).startswith("gs://"):
            path = str(cfg.resolve(path))
        cache = cfg.resolve(src.get("cache_dir", f".pulse-cache/revenuecat/{app.id}"))
        return revenuecat.ingest(
            con,
            app_id=app.id,
            source=name,
            path=path,
            cache_dir=cache,
            product_prefixes=src.get("product_prefixes"),
            log=log,
        )
    if kind == "admob":
        rf = src.get("response_file")
        return admob.ingest(
            con,
            app_id=app.id,
            source=name,
            start=start,
            end=end,
            publisher_id=src.get("publisher_id"),
            app_ids=src.get("app_ids"),
            response_file=cfg.resolve(rf) if rf else None,
            log=log,
        )
    if kind == "play_console":
        return stores.ingest_play(
            con,
            app_id=app.id,
            source=name,
            path=cfg.resolve(src["path"]),
            package=src.get("package"),
            log=log,
        )
    if kind == "app_store":
        return stores.ingest_appstore(
            con,
            app_id=app.id,
            source=name,
            path=cfg.resolve(src["path"]),
            apple_id=src.get("apple_id"),
            sku=src.get("sku"),
            log=log,
        )
    if kind == "unity_jsonl":
        return unity_jsonl.ingest(
            con,
            app_id=app.id,
            source=name,
            path=cfg.resolve(src["path"]),
            app=src.get("app"),
            timezone=app.timezone,
            log=log,
        )
    raise ConfigError(f"unknown source type {kind!r} (known: {', '.join(SOURCE_TYPES)})")


def ingest(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    *,
    apps: list[str] | None = None,
    sources: list[str] | None = None,
    dry_run: bool = False,
    log: Any = print,
    bq_client: Any = None,
    fail_fast: bool = False,
) -> IngestReport:
    start, end = cfg.window()
    warehouse.upsert_apps(con, cfg.apps, cfg.is_demo)
    warehouse.load_fx_rates(con, cfg.fx_rates, dt.date(2000, 1, 1))
    report = IngestReport()
    for app in cfg.apps:
        if apps and app.id not in apps:
            continue
        log(f"[{app.id}] window {start} .. {end}")
        for kind, src in app.sources.items():
            if sources and kind not in sources:
                continue
            if src.get("enabled", True) is False:
                continue
            key = f"{app.id}.{kind}"
            try:
                report.loaded[key] = _ingest_source(
                    con,
                    cfg,
                    app,
                    kind,
                    src or {},
                    start,
                    end,
                    dry_run,
                    report.bigquery,
                    log,
                    bq_client,
                )
            except Exception as exc:  # report and continue with the other sources
                report.errors[key] = f"{type(exc).__name__}: {exc}"
                log(f"    ERROR {key}: {report.errors[key]}")
                if fail_fast:
                    raise
    warehouse.set_meta(con, "window_start", start.isoformat())
    warehouse.set_meta(con, "window_end", end.isoformat())
    return report


def default_file(
    cfg: Config | None, explicit: str | Path | None, attr: str, name: str
) -> Path | None:
    """Rules/checks file: explicit path, the config's entry, ./<name>, then the packaged default."""
    if explicit:
        return Path(explicit)
    configured = getattr(cfg, attr, None) if cfg else None
    if configured:
        return Path(configured)
    here = Path(__file__).resolve()
    for p in (Path(name), here.parents[2] / name, here.parent / "_defaults" / name):
        if p.exists():
            return p
    return None


def record_alerts(con: duckdb.DuckDBPyConnection, fired: list[Any], run_id: str) -> None:
    now = warehouse.utcnow()
    if fired:
        con.executemany(
            "INSERT INTO meta_alert_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    run_id,
                    now,
                    a.rule,
                    a.app_id,
                    a.severity,
                    a.metric,
                    a.date,
                    a.value,
                    a.baseline,
                    a.threshold,
                    a.message,
                ]
                for a in fired
            ],
        )
    con.execute("DELETE FROM meta_alert_events WHERE fired_at < ?", [now - dt.timedelta(days=90)])


def run_all(
    cfg: Config,
    *,
    rules_path: Path | None = None,
    checks_path: Path | None = None,
    dry_run_notify: bool = True,
    send_notifications: bool = True,
    build_site: bool = True,
    site_dir: Path | None = None,
    command: str = "run",
    log: Any = print,
    bq_client: Any = None,
    verbose_models: bool = False,
) -> dict[str, Any]:
    """One refresh: ingest, models, data-quality checks, alerts, dashboard.

    Holds the run lock (raises runstate.LockedError when another refresh holds
    it), records a heartbeat, and snapshots the warehouse for the UI after a
    successful run. A run fails (result["ok"] is False) on ingest errors or
    on any failed check with severity error; the dashboard is still built so
    the failure is visible.
    """
    from .dashboard.site import build_site as _build

    with runstate.RunRecorder(cfg.state_dir, command) as rec:
        con = warehouse.connect(cfg.warehouse)
        try:
            log("Ingesting through the connectors ...")
            report = ingest(con, cfg, log=log, bq_client=bq_client)
            log("Running SQL models ...")
            runner.run_models(con, verbose=verbose_models)
            as_of = cfg.as_of or dt.date.today()
            check_results: list[checks_mod.CheckResult] = []
            if checks_path and checks_path.exists():
                check_results = checks_mod.run_checks(
                    con, checks_mod.load_checks(checks_path), cfg, as_of
                )
                run_id = checks_mod.store_results(con, check_results)
                log(checks_mod.format_console(check_results))
            else:
                run_id = runstate.now_iso()
                log("(no checks.yaml found: data-quality checks skipped)")
            rules = alerts_mod.load_rules(rules_path) if rules_path and rules_path.exists() else []
            fired = alerts_mod.evaluate(con, rules)
            record_alerts(con, fired, run_id)
            log(alerts_mod.format_console(fired))
            if send_notifications:
                to_send = fired + checks_mod.as_alerts(check_results, as_of)
                alerts_mod.notify(to_send, title=cfg.title, dry_run=dry_run_notify, log=log)
            out = site_dir or cfg.site_dir
            pages = (
                _build(con, cfg, out, alerts=fired, rules=rules, checks=check_results)
                if build_site
                else []
            )
            con.execute("CHECKPOINT")
        finally:
            con.close()
        summary = checks_mod.summarize(check_results)
        rec.summary.update(
            alerts=len(fired),
            critical_alerts=sum(a.severity == "critical" for a in fired),
            checks=summary["checks"],
            check_errors=summary["errors"],
            check_warnings=summary["warnings"],
            ingest_errors=len(report.errors),
            pages=len(pages),
        )
        problems = []
        if report.errors:
            problems.append(
                f"{len(report.errors)} source(s) failed to load: "
                + ", ".join(sorted(report.errors))
            )
        if summary["errors"]:
            failed = [r.name for r in check_results if r.is_blocking]
            problems.append(
                f"{summary['errors']} data-quality check(s) failed: " + ", ".join(failed)
            )
        if problems:
            rec.fail("; ".join(problems))
        else:
            runstate.make_snapshot(cfg.warehouse, cfg.state_dir)
    return {
        "ingest": report,
        "alerts": fired,
        "checks": check_results,
        "pages": pages,
        "ok": not problems,
        "error": "; ".join(problems) or None,
    }
