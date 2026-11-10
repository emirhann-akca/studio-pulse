"""`pulse` command-line interface."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import __version__, alerts, pipeline, runner, warehouse
from .config import Config, ConfigError, load_config


def _default_rules(cfg: Config | None, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    if cfg and cfg.alerts_path:
        return cfg.alerts_path
    for p in (Path("alerts.yaml"), Path(__file__).resolve().parents[2] / "alerts.yaml"):
        if p.exists():
            return p
    return None


def cmd_demo(args: argparse.Namespace) -> int:
    from .dashboard.site import build_site
    from .demo.generate import generate

    t0 = time.perf_counter()
    work = Path(args.workdir)
    if work.exists() and args.clean:
        shutil.rmtree(work)
    print(f"Generating synthetic demo data in {work}/ (scale {args.scale}) ...")
    cfg_path = generate(work, scale=args.scale)
    cfg = load_config(cfg_path)
    if cfg.warehouse.exists():
        cfg.warehouse.unlink()
    con = warehouse.connect(cfg.warehouse)
    try:
        print("Ingesting through the connectors ...")
        report = pipeline.ingest(con, cfg)
        if report.errors:
            print("Ingest errors:", report.errors, file=sys.stderr)
            return 1
        print("Running SQL models ...")
        runner.run_models(con, verbose=args.verbose)
        rules_path = _default_rules(cfg, args.rules)
        rules = alerts.load_rules(rules_path) if rules_path else []
        fired = alerts.evaluate(con, rules)
        print(alerts.format_console(fired))
        out = Path(args.out)
        pages = build_site(con, cfg, out, alerts=fired, rules=rules)
    finally:
        con.close()
    print(f"Dashboard: {len(pages)} page(s) written to {out}/ in {time.perf_counter() - t0:.1f}s")
    print(f"Open {out / 'index.html'} in a browser.")
    return 0


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
            billed = "dry-run only" if s.dry_run_only else (f"{(s.billed_bytes or 0) / 1e6:,.1f} MB billed" if s.billed_bytes is not None else "not run")
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
    rules_path = _default_rules(cfg, args.rules)
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


def cmd_build_dashboard(args: argparse.Namespace) -> int:
    from .dashboard.site import build_site

    cfg = load_config(args.config)
    rules_path = _default_rules(cfg, args.rules)
    rules = alerts.load_rules(rules_path) if rules_path else []
    con = warehouse.connect(cfg.warehouse)
    try:
        fired = alerts.evaluate(con, rules)
        out = Path(args.out) if args.out else cfg.site_dir
        pages = build_site(con, cfg, out, alerts=fired, rules=rules)
    finally:
        con.close()
    print(f"{len(pages)} page(s) written to {out}/")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    res = pipeline.run_all(cfg, rules_path=_default_rules(cfg, args.rules), dry_run_notify=not args.notify)
    print(f"{len(res['pages'])} page(s) written to {cfg.site_dir}/")
    return 1 if res["ingest"].errors else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pulse", description="Studio Pulse: app portfolio data and monitoring.")
    p.add_argument("--version", action="version", version=f"studio-pulse {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="generate synthetic data for 3 fictional apps, run everything, build the dashboard")
    d.add_argument("--out", default="demo-site", help="dashboard output directory (default: demo-site)")
    d.add_argument("--workdir", default=".pulse-demo", help="where generated source files and the warehouse go")
    d.add_argument("--scale", type=float, default=1.0, help="multiply synthetic install volume (default 1.0)")
    d.add_argument("--rules", help="alert rules file (default: alerts.yaml)")
    d.add_argument("--no-clean", dest="clean", action="store_false", help="keep an existing workdir")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_demo)

    def with_config(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("-c", "--config", help="config file (default: $PULSE_CONFIG, pulse.local.yaml, pulse.yaml)")
        return sp

    i = with_config(sub.add_parser("ingest", help="load configured sources into the warehouse"))
    i.add_argument("--app", action="append", help="only this app id (repeatable)")
    i.add_argument("--source", action="append", help="only this source type (repeatable)")
    i.add_argument("--dry-run", action="store_true", help="BigQuery: dry-run every query and print bytes; load nothing")
    i.set_defaults(func=cmd_ingest)

    m = with_config(sub.add_parser("run-models", help="(re)build all SQL models"))
    m.set_defaults(func=cmd_run_models)

    ml = sub.add_parser("models", help="list models in execution order with their dependencies")
    ml.add_argument("-v", "--verbose", action="store_true")
    ml.set_defaults(func=cmd_models)

    a = with_config(sub.add_parser("alerts", help="evaluate alert rules and notify"))
    a.add_argument("--rules", help="alert rules file (default: alerts.yaml)")
    a.add_argument("--dry-run", action="store_true", help="print notifications instead of sending them")
    a.add_argument("--fail-on-alert", action="store_true", help="exit with status 3 when any alert fires")
    a.set_defaults(func=cmd_alerts)

    b = with_config(sub.add_parser("build-dashboard", help="write the static HTML dashboard"))
    b.add_argument("--out", help="output directory (default: site_dir from the config)")
    b.add_argument("--rules", help="alert rules file shown on the dashboard")
    b.set_defaults(func=cmd_build_dashboard)

    r = with_config(sub.add_parser("run", help="ingest + models + alerts + dashboard"))
    r.add_argument("--rules", help="alert rules file")
    r.add_argument("--notify", action="store_true", help="actually send notifications (default: dry run)")
    r.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
