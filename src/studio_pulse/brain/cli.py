"""`pulse insights`, `pulse ask` and `pulse brain eval`."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _cfg(args: argparse.Namespace) -> Any:
    from ..config import load_config

    return load_config(args.config)


def _date(v: str | None) -> dt.date | None:
    return dt.date.fromisoformat(v) if v else None


def cmd_insights(args: argparse.Namespace) -> int:
    from .. import alerts, pipeline, warehouse
    from . import evidence, insights, llm, narrate

    cfg = _cfg(args)
    as_of = _date(args.as_of) or cfg.as_of
    rules_path = pipeline.default_file(cfg, None, "alerts_path", "alerts.yaml")
    rules = alerts.load_rules(rules_path) if rules_path else []
    con = warehouse.connect(cfg.warehouse, read_only=args.no_store)
    try:
        rep = insights.run(con, as_of=as_of, rules=rules)
        if not args.no_store:
            evidence.store(con, rep.insights, rep.as_of)
    finally:
        con.close()
    client = llm.client_from_config((cfg.raw or {}).get("brain"))
    if args.json:
        out = []
        for ins in rep.insights[: args.top or None]:
            n = narrate.narrate(ins, args.lang, client)
            out.append({**ins.to_json(), "title": n.title, "text": n.body, "caveats": n.caveats,
                        "recommendations_text": [a for a, _ in n.actions]})  # fmt: skip
        print(json.dumps({"as_of": rep.as_of, "tested": rep.tested, "errors": rep.errors,
                          "insights": out}, default=str, ensure_ascii=False, indent=2))  # fmt: skip
        return 0
    print(
        f"{len(rep.insights)} insight(s) from {rep.tested} tested hypotheses (as of {rep.as_of})."
    )
    for ins in rep.insights[: args.top or None]:
        n = narrate.narrate(ins, args.lang, client)
        print(f"\n#{ins.rank} [{ins.severity.upper()}] {n.title}")
        if n.body:
            print(f"   {n.body}")
        for a, ids in n.actions:
            print(f"   -> {a} [{', '.join(ids)}]")
    for k, v in rep.errors.items():
        if not k.endswith(":trace"):
            print(f"job error {k}: {v}", file=sys.stderr)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from . import llm
    from .qa import answer

    cfg = _cfg(args)
    path = Path(cfg.warehouse)
    if not path.exists():
        print("no warehouse yet: run `pulse run` (or `pulse demo`) first", file=sys.stderr)
        return 2
    client = llm.client_from_config((cfg.raw or {}).get("brain"))
    a = answer(str(path), " ".join(args.question), lang=args.lang, client=client, as_of=cfg.as_of)
    if args.json:
        print(json.dumps({k: v for k, v in a.__dict__.items() if k != "evidence"}
                         | {"evidence": a.evidence.to_json() if a.evidence else None},
                         default=str, ensure_ascii=False, indent=2))  # fmt: skip
        return 0 if a.route != "refused" else 3
    print(f"[{a.route}{' ' + a.matched if a.matched else ''}] {a.text}")
    if a.sql:
        print("\n" + a.sql.strip())
    if a.rows:
        print("\n" + " | ".join(a.columns))
        for r in a.cells()[:20]:
            print(" | ".join(r))
        if len(a.rows) > 20:
            print(f"... {len(a.rows) - 20} more row(s)")
    return 0 if a.route != "refused" else 3


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import run_all

    cfg = _cfg(args)
    rep = run_all(cfg.warehouse, as_of=cfg.as_of)
    s = rep.summary()
    if args.json:
        print(json.dumps({"summary": s, "failures": [c.__dict__ for c in rep.failures()]},
                         ensure_ascii=False, indent=2))  # fmt: skip
    else:
        for name in ("golden", "refuse", "inject"):
            x = s[name]
            print(f"{name:<7} {x['passed']}/{x['cases']} passed ({x['rate'] * 100:.1f}%)")
        print(f"route accuracy {s['route_accuracy'] * 100:.1f}%")
        for c in rep.failures():
            print(f"  FAIL {c.set}/{c.id} [{c.lang}] {c.question!r}: {c.detail}")
    return 0 if not rep.failures() else 1


def add_commands(
    sub: Any, with_config: Callable[[argparse.ArgumentParser], argparse.ArgumentParser]
) -> None:
    i = with_config(
        sub.add_parser("insights", help="run the deterministic insight jobs and print them")
    )
    i.add_argument("--lang", choices=("en", "tr"), default="en")
    i.add_argument("--json", action="store_true")
    i.add_argument("--top", type=int, default=0, help="print only the first N")
    i.add_argument("--as-of", help="judge freshness against this date (default: config / today)")
    i.add_argument("--no-store", action="store_true", help="read-only: do not write meta_insights")
    i.set_defaults(func=cmd_insights)

    a = with_config(
        sub.add_parser("ask", help="ask a question; answers carry their SQL and evidence")
    )
    a.add_argument("question", nargs="+")
    a.add_argument("--lang", choices=("en", "tr"), default="en")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_ask)

    b = sub.add_parser("brain", help="the insight engine's evaluation sets")
    bsub = b.add_subparsers(dest="brain_cmd", required=True)
    e = with_config(bsub.add_parser("eval", help="golden, refusal and injection sets (FakeLLM)"))
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval)
