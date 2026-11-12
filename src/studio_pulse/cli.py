"""`pulse` command-line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

from . import __version__, alerts, pipeline, runner, runstate, warehouse
from .config import Config, ConfigError, load_config


def _rules(cfg: Config | None, explicit: str | None) -> Path | None:
    return pipeline.default_file(cfg, explicit, "alerts_path", "alerts.yaml")


def _checks(cfg: Config | None, explicit: str | None) -> Path | None:
    return pipeline.default_file(cfg, explicit, "checks_path", "checks.yaml")


def _locked(exc: Exception) -> int:
    print(str(exc), file=sys.stderr)
    return runstate.EXIT_LOCKED


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo.generate import generate

    t0 = time.perf_counter()
    work, out = Path(args.workdir), Path(args.out)
    if work.exists() and args.clean:
        shutil.rmtree(work)
    print(f"Generating synthetic demo data in {work}/ (scale {args.scale}) ...")
    cfg = load_config(generate(work, scale=args.scale, site_dir=out))
    if cfg.warehouse.exists():
        cfg.warehouse.unlink()
    try:
        res = pipeline.run_all(
            cfg,
            rules_path=_rules(cfg, args.rules),
            checks_path=_checks(cfg, args.checks),
            send_notifications=False,
            command="demo",
            verbose_models=args.verbose,
        )
    except runstate.LockedError as exc:
        return _locked(exc)
    if res["ingest"].errors:
        print("Ingest errors:", res["ingest"].errors, file=sys.stderr)
    index = out / "index.html"
    print(
        f"Dashboard: {len(res['pages'])} page(s) written to {out}/ in {time.perf_counter() - t0:.1f}s"
    )
    print(f"Open {index} in a browser, or run: pulse serve --config {cfg.path}")
    if args.open and index.exists():
        import webbrowser

        webbrowser.open(index.resolve().as_uri())
    return 0 if res["ok"] else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, apps=args.app, sources=args.source, dry_run=args.dry_run)
    finally:
        con.close()
    if rep.bigquery:
        print("\nBigQuery queries:")
        for s in rep.bigquery:
            est = f"{(s.estimated_bytes or 0) / 1e6:,.1f} MB est"
            billed = (
                "dry-run only"
                if s.dry_run_only
                else (
                    f"{(s.billed_bytes or 0) / 1e6:,.1f} MB billed"
                    if s.billed_bytes is not None
                    else "not run"
                )
            )
            print(f"  {s.name:<40} {est:>16}  {billed}")
    if rep.errors:
        print("\nErrors:")
        for k, v in rep.errors.items():
            print(f"  {k}: {v}")
        return 1
    return 0


def cmd_run_models(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    con = warehouse.connect(cfg.warehouse)
    try:
        runner.run_models(con, verbose=True)
    finally:
        con.close()
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    for m in runner.resolve_order(runner.discover()):
        deps = ", ".join(sorted(m.depends_on)) or "-"
        print(f"{m.name:<26} <- {deps}")
        if args.verbose and m.description:
            print(f"    {m.description}")
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    rules_path = _rules(cfg, args.rules)
    if not rules_path:
        print("no alerts.yaml found", file=sys.stderr)
        return 2
    rules = alerts.load_rules(rules_path)
    con = warehouse.connect(cfg.warehouse)
    try:
        fired = alerts.evaluate(con, rules)
    finally:
        con.close()
    print(alerts.format_console(fired))
    alerts.notify(fired, title=cfg.title, dry_run=args.dry_run)
    return 3 if (fired and args.fail_on_alert) else 0


def cmd_check(args: argparse.Namespace) -> int:
    from . import checks

    cfg = load_config(args.config)
    path = _checks(cfg, args.checks)
    if not path:
        print("no checks.yaml found", file=sys.stderr)
        return 2
    defs = checks.load_checks(path)
    try:
        con = warehouse.connect(cfg.warehouse, wait_s=args.wait)
    except warehouse.WarehouseLocked as exc:
        return _locked(exc)
    try:
        results = checks.run_checks(con, defs, cfg)
        if not args.no_store:
            checks.store_results(con, results)
    finally:
        con.close()
    if args.json:
        print(
            json.dumps(
                {
                    "summary": checks.summarize(results),
                    "results": [r.__dict__ for r in results],
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(checks.format_console(results))
    s = checks.summarize(results)
    return 1 if s["errors"] or (args.strict and s["warnings"]) else 0


def cmd_build_dashboard(args: argparse.Namespace) -> int:
    from . import checks
    from .dashboard.site import build_site

    cfg = load_config(args.config)
    rules_path = _rules(cfg, args.rules)
    rules = alerts.load_rules(rules_path) if rules_path else []
    con = warehouse.connect(cfg.warehouse)
    try:
        fired = alerts.evaluate(con, rules)
        out = Path(args.out) if args.out else cfg.site_dir
        pages = build_site(
            con, cfg, out, alerts=fired, rules=rules, checks=checks.latest_results(con)
        )
    finally:
        con.close()
    print(f"{len(pages)} page(s) written to {out}/")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    try:
        res = pipeline.run_all(
            cfg,
            rules_path=_rules(cfg, args.rules),
            checks_path=_checks(cfg, args.checks),
            dry_run_notify=not args.notify,
        )
    except runstate.LockedError as exc:
        return _locked(exc)
    print(f"{len(res['pages'])} page(s) written to {cfg.site_dir}/")
    if not res["ok"]:
        print(f"refresh failed: {res['error']}", file=sys.stderr)
        return 1
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    from .fingerprint import fingerprint

    cfg = load_config(args.config)
    con = warehouse.connect(cfg.warehouse, read_only=True)
    try:
        res = fingerprint(con, Path(args.out) if args.out else None)
    finally:
        con.close()
    print(res["sha256"])
    for t, v in res["tables"].items():
        print(f"  {t:<26} {v['rows']:>9} rows  {v['sha256'][:16]}")
    return 0


def _split(text: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (text or "").split(","):
        if part.strip():
            k, _, v = part.partition("=")
            out[k.strip()] = float(v)
    return out


def cmd_experiment(args: argparse.Namespace) -> int:
    from .stats import experiments as X

    if args.input:
        exp = X.Experiment(
            id=Path(args.input).stem,
            name=args.name or Path(args.input).stem,
            arms=X.read_arms(args.input),
            control=args.control,
            split=_split(args.split),
            alpha=args.alpha,
            metric=args.metric,
        )
        results = [X.analyze(exp)]
    else:
        cfg = load_config(args.config)
        if args.sql:
            from .connectors import ga4
            from .connectors.dialect import BigQueryDialect

            for app in cfg.apps:
                src = app.sources.get("ga4_bigquery") or {}
                for raw in app.experiments:
                    e = ga4.ExperimentSpec.from_dict(raw)
                    if args.id and e.id not in args.id:
                        continue
                    start, end = cfg.window()
                    d = BigQueryDialect(
                        src.get("project", "PROJECT"), src.get("dataset", "DATASET")
                    )
                    opts = ga4.GA4Options.from_source(src)
                    opts.timezone = app.timezone
                    print(f"-- {app.id} / {e.id}: per-variant sufficient statistics (no user ids)")
                    print(ga4.GA4Queries(d, start, end, opts).experiment_arms(e).strip() + ";\n")
            return 0
        con = warehouse.connect(cfg.warehouse, read_only=True)
        try:
            exps = X.load_experiments(con)
        finally:
            con.close()
        exps = [e for e in exps if not args.id or e.id in args.id]
        if not exps:
            print(
                "no experiment results in the warehouse (configure experiments: and run pulse run)",
                file=sys.stderr,
            )
            return 2
        results = [X.analyze(e) for e in exps]
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print("\n\n".join(X.format_console(r) for r in results))
    return 3 if any(r["srm"]["flagged"] for r in results) else 0


# -- schedule ----------------------------------------------------------------------


def _schedule_state_dir(args: argparse.Namespace, rec: dict | None) -> Path | None:
    if rec and rec.get("state_dir"):
        return Path(rec["state_dir"])
    try:
        return load_config(args.config).state_dir
    except ConfigError:
        return None


def cmd_schedule_install(args: argparse.Namespace) -> int:
    from . import schedule

    cfg = load_config(args.config)
    hour, minute = schedule.parse_time(args.at)
    pulse_args = ["run", "--config", str(cfg.path)] + (["--notify"] if args.notify else [])
    job = schedule.Job(
        name=args.name,
        hour=hour,
        minute=minute,
        args=pulse_args,
        workdir=cfg.base_dir,
        log_dir=Path(cfg.state_dir) / "logs",
        config=cfg.path,
        state_dir=cfg.state_dir,
    )
    b = schedule.backend(args.backend)
    if args.dry_run:
        data = b.render(job)
        text = data[2:].decode("utf-16-le") if data[:2] == b"\xff\xfe" else data.decode("utf-8")
        print(text.replace("\r\n", "\n"))
        print(f"(dry run: nothing registered; {b.kind} would run {job.argv()})")
        return 0
    b.install(job)
    schedule.save_record(job, b.kind)
    st = b.status(job.name)
    print(f"Registered {st.get('id')} ({b.kind}): daily at {hour:02d}:{minute:02d}")
    print("  " + " ".join(f'"{a}"' if " " in a else a for a in job.argv()))
    print(f"Log: {job.log_file}")
    print(f"Try it now: pulse schedule run-now --name {job.name} --wait 300")
    return 0 if st.get("registered") else 1


def cmd_schedule_status(args: argparse.Namespace) -> int:
    from . import schedule

    b = schedule.backend(args.backend)
    rec = schedule.load_record(args.name)
    os_state = b.status(args.name)
    state_dir = _schedule_state_dir(args, rec)
    hb = runstate.read_heartbeat(state_dir) if state_dir else None
    out = {
        "name": args.name,
        "registered": bool(os_state.get("registered")),
        "scheduler": os_state,
        "record": rec,
        "heartbeat": hb,
    }
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        reg = "registered" if out["registered"] else "NOT registered"
        print(f"{args.name}: {reg} ({b.kind} {os_state.get('id')})")
        for k in ("state", "last_exit_code", "runs", "next_run", "last_run", "last_result"):
            if k in os_state:
                print(f"  {k.replace('_', ' ')}: {os_state[k]}")
        if rec:
            print(f"  daily at {rec.get('at')}; config {rec.get('config')}")
            print(f"  log {rec.get('log_file')}")
        if hb:
            print(
                f"  last refresh: {hb.get('status')} (started {hb.get('last_start')}, "
                f"{hb.get('duration_s', '?')} s); last success {hb.get('last_success') or 'never'}"
            )
            if hb.get("error"):
                print(f"  error: {hb['error']}")
        else:
            print("  no refresh recorded yet")
    return 0 if out["registered"] else 3


def cmd_schedule_run_now(args: argparse.Namespace) -> int:
    from . import schedule

    b = schedule.backend(args.backend)
    rec = schedule.load_record(args.name)
    state_dir = _schedule_state_dir(args, rec)
    # The run we start must be told apart from the last one, even if that
    # finished a moment ago: remember its identity; the new run's start time
    # (second resolution) can only be at or after the time of the kick.
    before = (runstate.read_heartbeat(state_dir) or {}) if state_dir else {}
    before_id = (before.get("last_start"), before.get("pid"))
    kicked = dt.datetime.now(dt.UTC).replace(microsecond=0)
    b.run_now(args.name)
    print(f"Started {args.name} through {b.kind}.")
    if not args.wait:
        return 0
    if state_dir is None:
        print("cannot wait: unknown state directory (pass --config)", file=sys.stderr)
        return 2
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        hb = runstate.read_heartbeat(state_dir) or {}
        started = runstate.parse_iso(hb.get("last_start"))
        if (
            (hb.get("last_start"), hb.get("pid")) != before_id
            and started
            and started >= kicked
            and hb.get("status") in ("success", "failed", "interrupted")
        ):
            print(
                f"Finished: {hb['status']} in {hb.get('duration_s')} s"
                + (f" ({hb['error']})" if hb.get("error") else "")
            )
            return 0 if hb["status"] == "success" else 1
        time.sleep(1)
    print(f"No finished run within {args.wait:.0f} s.", file=sys.stderr)
    print(json.dumps(b.status(args.name), indent=2, default=str), file=sys.stderr)
    if rec and rec.get("log_file"):
        log = Path(rec["log_file"])
        for p in (log, log.with_name("launchd.log")):
            if p.exists():
                tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                print(f"--- {p}", *tail, sep="\n", file=sys.stderr)
    return 1


def cmd_schedule_remove(args: argparse.Namespace) -> int:
    from . import schedule

    b = schedule.backend(args.backend)
    removed = b.remove(args.name)
    schedule.delete_record(args.name)
    if b.status(args.name).get("registered"):
        print(f"{args.name} is still registered after removal", file=sys.stderr)
        return 1
    if not removed:
        print(f"no schedule named {args.name}", file=sys.stderr)
        return 0 if args.missing_ok else 3
    print(f"Removed {args.name}.")
    return 0


# -- serve, doctor -------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from .serve.server import serve
    except ImportError as exc:
        print(
            f"pulse serve needs fastapi, uvicorn and jinja2 ({exc}); reinstall studio-pulse",
            file=sys.stderr,
        )
        return 2
    cfg = load_config(args.config)
    return serve(cfg, host=args.host, port=args.port, open_browser=args.open, check=args.check)


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    results = doctor.run(args.config)
    print(doctor.as_json(results) if args.json else doctor.format_console(results))
    return 1 if any(r.status == "fail" for r in results) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulse", description="Studio Pulse: app portfolio data and monitoring."
    )
    p.add_argument("--version", action="version", version=f"studio-pulse {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_config(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument(
            "-c",
            "--config",
            help="config file (default: $PULSE_CONFIG, pulse.local.yaml, pulse.yaml)",
        )
        return sp

    d = sub.add_parser(
        "demo",
        help="generate synthetic data for 3 fictional apps, run everything, build the dashboard",
    )
    d.add_argument(
        "--out", default="demo-site", help="dashboard output directory (default: demo-site)"
    )
    d.add_argument(
        "--workdir", default=".pulse-demo", help="where generated source files and the warehouse go"
    )
    d.add_argument(
        "--scale", type=float, default=1.0, help="multiply synthetic install volume (default 1.0)"
    )
    d.add_argument("--rules", help="alert rules file (default: alerts.yaml)")
    d.add_argument("--checks", help="data-quality checks file (default: checks.yaml)")
    d.add_argument(
        "--no-clean", dest="clean", action="store_false", help="keep an existing workdir"
    )
    d.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_demo)

    i = with_config(sub.add_parser("ingest", help="load configured sources into the warehouse"))
    i.add_argument("--app", action="append", help="only this app id (repeatable)")
    i.add_argument("--source", action="append", help="only this source type (repeatable)")
    i.add_argument(
        "--dry-run",
        action="store_true",
        help="BigQuery: dry-run every query and print bytes; load nothing",
    )
    i.set_defaults(func=cmd_ingest)

    m = with_config(sub.add_parser("run-models", help="(re)build all SQL models"))
    m.set_defaults(func=cmd_run_models)

    ml = sub.add_parser("models", help="list models in execution order with their dependencies")
    ml.add_argument("-v", "--verbose", action="store_true")
    ml.set_defaults(func=cmd_models)

    a = with_config(sub.add_parser("alerts", help="evaluate alert rules and notify"))
    a.add_argument("--rules", help="alert rules file (default: alerts.yaml)")
    a.add_argument(
        "--dry-run", action="store_true", help="print notifications instead of sending them"
    )
    a.add_argument(
        "--fail-on-alert", action="store_true", help="exit with status 3 when any alert fires"
    )
    a.set_defaults(func=cmd_alerts)

    c = with_config(
        sub.add_parser(
            "check",
            help="run the data-quality checks (checks.yaml); exit 1 when an error-level check fails",
        )
    )
    c.add_argument("--checks", help="checks file (default: checks.yaml)")
    c.add_argument("--json", action="store_true", help="print results as JSON")
    c.add_argument("--strict", action="store_true", help="also fail on warn-level checks")
    c.add_argument(
        "--no-store", action="store_true", help="do not record the results in the warehouse"
    )
    c.add_argument(
        "--wait",
        type=float,
        default=60.0,
        help="seconds to wait for a running refresh to release the warehouse (default 60)",
    )
    c.set_defaults(func=cmd_check)

    b = with_config(sub.add_parser("build-dashboard", help="write the static HTML dashboard"))
    b.add_argument("--out", help="output directory (default: site_dir from the config)")
    b.add_argument("--rules", help="alert rules file shown on the dashboard")
    b.set_defaults(func=cmd_build_dashboard)

    r = with_config(
        sub.add_parser(
            "run",
            help="refresh: ingest + models + checks + alerts + dashboard (takes the run lock)",
        )
    )
    r.add_argument("--rules", help="alert rules file")
    r.add_argument("--checks", help="data-quality checks file")
    r.add_argument(
        "--notify", action="store_true", help="actually send notifications (default: dry run)"
    )
    r.set_defaults(func=cmd_run)

    f = with_config(
        sub.add_parser(
            "fingerprint",
            help="export every stg_/fct_ model as sorted CSV and print SHA-256 hashes",
        )
    )
    f.add_argument("--out", help="directory for the CSV files and fingerprint.json")
    f.set_defaults(func=cmd_fingerprint)

    ex = with_config(
        sub.add_parser(
            "experiment",
            help="A/B test analysis: SRM, z / Welch tests, CUPED and an always-valid interval",
        )
    )
    ex.add_argument("--id", action="append", help="only this experiment id (repeatable)")
    ex.add_argument(
        "--input",
        help="analyse a CSV/JSON of per-variant aggregates instead of the warehouse "
        "(columns: variant, users, conversions, y_sum, y_sumsq, x_sum, x_sumsq, xy_sum)",
    )
    ex.add_argument("--control", help="control variant (with --input; default: first)")
    ex.add_argument("--split", help="expected split, e.g. A=0.5,B=0.5 (with --input)")
    ex.add_argument("--alpha", type=float, default=0.05, help="significance level (default 0.05)")
    ex.add_argument("--name", help="experiment name (with --input)")
    ex.add_argument("--metric", default="metric", help="metric label (with --input)")
    ex.add_argument(
        "--sql",
        action="store_true",
        help="print the BigQuery SQL that computes the per-variant aggregates; run nothing",
    )
    ex.add_argument("--json", action="store_true")
    ex.set_defaults(func=cmd_experiment)

    s = sub.add_parser(
        "schedule", help="daily refresh through launchd (macOS) or Task Scheduler (Windows)"
    )
    ssub = s.add_subparsers(dest="schedule_cmd", required=True)

    def sched(name: str, help_: str, func) -> argparse.ArgumentParser:
        sp = with_config(ssub.add_parser(name, help=help_))
        sp.add_argument("--name", default="refresh", help="job name (default: refresh)")
        sp.add_argument("--backend", choices=["launchd", "schtasks"], help=argparse.SUPPRESS)
        sp.set_defaults(func=func)
        return sp

    si = sched("install", "register (or replace) the daily refresh", cmd_schedule_install)
    si.add_argument("--at", default="09:30", help="local time HH:MM (default 09:30)")
    si.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="do not send Slack / e-mail notifications from scheduled runs",
    )
    si.add_argument("--dry-run", action="store_true", help="print the plist / task XML only")
    ss = sched("status", "show registration, last result and last refresh", cmd_schedule_status)
    ss.add_argument("--json", action="store_true")
    sr = sched("run-now", "start the registered job immediately", cmd_schedule_run_now)
    sr.add_argument(
        "--wait", type=float, default=0, help="wait up to N seconds for the run to finish"
    )
    sx = sched("remove", "unregister the job and delete its definition", cmd_schedule_remove)
    sx.add_argument("--missing-ok", action="store_true", help="exit 0 when nothing is registered")

    sv = with_config(
        sub.add_parser("serve", help="local web UI: dashboard, refresh health, alerts, run now")
    )
    sv.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    sv.add_argument(
        "--port", type=int, default=8765, help="port (default 8765; 0 picks a free one)"
    )
    sv.add_argument("--open", action="store_true", help="open the UI in a browser")
    sv.add_argument(
        "--check",
        action="store_true",
        help="start on a free port, request every page, stop; exit 1 if any page fails",
    )
    sv.set_defaults(func=cmd_serve)

    from .radar.cli import add_parser as add_radar_parser

    add_radar_parser(sub, with_config)

    dr = with_config(sub.add_parser("doctor", help="check this machine and the config"))
    dr.add_argument("--json", action="store_true", help="machine-readable output")
    dr.set_defaults(func=cmd_doctor)
    return p


def _safe_console() -> None:
    # A legacy console code page (cp1254, cp857 ...) cannot encode every
    # character in names or messages: escape those instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    from .checks import CheckError
    from .schedule import ScheduleError

    _safe_console()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (alerts.RuleError, CheckError) as exc:
        print(f"rules error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        if args.func is not cmd_experiment:
            raise
        print(f"experiment error: {exc}", file=sys.stderr)
        return 2
    except ScheduleError as exc:
        print(f"schedule error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
