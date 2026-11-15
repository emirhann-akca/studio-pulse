"""`pulse radar creatives refresh|list|show|add|tag` (registered by radar/cli.py)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from ... import warehouse
from ...config import load_config


def _day(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def _print(obj: object) -> None:
    """The commands' JSON: counts, statuses, redacted messages and the model's usage (requests and
    cost, see run.llm_usage); never a credential or the LLM client's own summary."""
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def cmd_refresh(args: argparse.Namespace) -> int:
    from . import run, settings

    cfg = load_config(args.config)
    s = settings.from_config(cfg)
    if args.max_gb is not None:
        if not 0 < args.max_gb <= settings.HARD_MAX_GB:
            raise SystemExit(f"--max-gb must be in (0, {settings.HARD_MAX_GB:g}]")
        s.cap_bytes = int(args.max_gb * 1e9)
    sources = tuple(x.strip() for x in args.source.split(",")) if args.source else run.SOURCES
    if any(x not in run.SOURCES for x in sources):
        raise SystemExit(f"--source: some of {', '.join(run.SOURCES)}")
    project = None
    if "google" in sources:
        from ..dossier.run import default_project

        project = default_project()
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 - stdout carries the JSON
    llm = None if (args.no_llm or args.no_tag or args.dry_run) else run.llm_client(cfg, s, log=log)
    con = warehouse.connect(cfg.warehouse)
    try:
        res = run.refresh(
            con, cfg, s=s, today=_day(args.date), project=project, llm=llm, sources=sources,
            force=args.force, dry_run_only=args.dry_run, tag=not args.no_tag, log=log,
        )  # fmt: skip
    finally:
        con.close()
    _print(res)
    g = res.get("google") or {}
    return 1 if g.get("status") in ("refused", "error") else 0


def _load(cfg_path: str | None):
    from . import library, settings

    cfg = load_config(cfg_path)
    s = settings.from_config(cfg)
    con = warehouse.connect(cfg.warehouse, read_only=True)
    try:
        rows = library.load(con, s, cfg.as_of or dt.date.today())
    finally:
        con.close()
    return cfg, s, rows


def cmd_list(args: argparse.Namespace) -> int:
    from . import labels as L
    from . import library

    _, s, rows = _load(args.config)
    f = library.clean_filters(
        {"genre": args.genre, "hook": args.hook, "advertiser": args.advertiser,
         "source": args.source, "min_score": args.min_score, "active": "1" if args.active else ""},
        rows,
    )  # fmt: skip
    rows = library.apply(rows, f)[: args.limit]
    if args.json:
        _print([library.to_json(r) for r in rows])
        return 0
    lang = args.lang
    for r in rows:
        hook = L.name(L.HOOKS, r.get("hook") or "unknown", lang)
        ai = L.name(L.AI_LEVELS, r.get("ai_likelihood") or "unknown", lang)
        print(f"{r['score']:5.1f}  {r['creative_key']}  {r.get('advertiser_name')}  "
              f"[{r.get('format')}] {hook} · AI {ai} · {r.get('first_shown')}..{r.get('last_shown')}")  # fmt: skip
        if r["links"]["source"]:
            print(f"       {r['links']['source']}")
    print(f"{len(rows)} creative(s)", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from . import library

    _, _, rows = _load(args.config)
    r = library.find(rows, args.key)
    if not r:
        print("not found", file=sys.stderr)
        return 2
    out = library.to_json(r)
    out["similar"] = [x["creative_key"] for x in library.similar(rows, r)]
    out["brief_link"] = library.brief_link(r, args.lang)
    _print(out)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    from . import run, settings

    cfg = load_config(args.config)
    s = settings.from_config(cfg)
    today = cfg.as_of or dt.date.today()
    con = warehouse.connect(cfg.warehouse)
    try:
        try:
            if args.attach_to:
                row = run.attach_youtube(con, args.attach_to, args.url)
            elif not args.advertiser:
                raise ValueError("--advertiser is required (or --for an existing creative)")
            else:
                row = run.add_youtube(
                    con, s, args.url, advertiser=args.advertiser, genre=args.genre,
                    note=args.note, first=_day(args.first), last=_day(args.last), today=today,
                )  # fmt: skip
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        res: dict[str, object] = {"added": row["creative_key"]}
        if args.tag:
            llm = run.llm_client(cfg, s, log=lambda m: print(m, file=sys.stderr))
            res["tags"] = run.tag_pending(con, s, llm, keys=[row["creative_key"]],
                                          log=lambda m: print(m, file=sys.stderr))  # fmt: skip
            if llm is not None:
                res["llm"] = run.llm_usage(llm)
    finally:
        con.close()
    _print(res)
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    from . import run, settings

    cfg = load_config(args.config)
    s = settings.from_config(cfg)
    llm = None if args.no_llm else run.llm_client(cfg, s, log=lambda m: print(m, file=sys.stderr))
    con = warehouse.connect(cfg.warehouse)
    try:
        run.init_schema(con)
        res = run.tag_pending(con, s, llm, limit=args.limit, keys=args.key or None,
                              log=lambda m: print(m, file=sys.stderr))  # fmt: skip
    finally:
        con.close()
    if llm is not None:
        res["llm"] = run.llm_usage(llm)
    _print(res)
    return 0


def add_parser(rs: argparse._SubParsersAction, with_config) -> None:
    c = rs.add_parser(
        "creatives",
        help="competitor creative library: ads by hook type, AI likelihood, winner score",
    )
    sub = c.add_subparsers(dest="creatives_cmd", required=True)
    r = with_config(
        sub.add_parser("refresh", help="scan the sources (BigQuery dry-run first) and tag")
    )
    r.add_argument("--force", action="store_true", help="refresh even if a source is fresh")
    r.add_argument("--dry-run", action="store_true", help="only BigQuery dry runs: bytes, no scan")
    r.add_argument(
        "--max-gb", type=float, help="cap for the creatives query (radar.creatives.max_gb)"
    )
    r.add_argument("--source", help="comma list of google, meta, tiktok (default: all)")
    r.add_argument("--no-llm", action="store_true", help="keyword and metadata tags only")
    r.add_argument("--no-tag", action="store_true", help="do not tag")
    r.add_argument("--date", help="as-of date (default: today)")
    r.set_defaults(func=cmd_refresh)
    ls = with_config(sub.add_parser("list", help="list creatives, best winner score first"))
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--genre")
    ls.add_argument("--hook")
    ls.add_argument("--advertiser", help="advertiser key (as in the page's filter)")
    ls.add_argument("--source", help="google_atc, meta, tiktok or youtube")
    ls.add_argument("--min-score", type=int, choices=[20, 35, 50, 60, 75])
    ls.add_argument("--active", action="store_true", help="still running only")
    ls.add_argument("--limit", type=int, default=30)
    ls.add_argument("--lang", choices=["tr", "en"], default="tr")
    ls.set_defaults(func=cmd_list)
    sh = with_config(sub.add_parser("show", help="one creative as JSON (tags, score parts)"))
    sh.add_argument("key", help="creative key, e.g. g-CR0123..., m-123..., y-<YouTube id>")
    sh.add_argument("--lang", choices=["tr", "en"], default="tr")
    sh.set_defaults(func=cmd_show)
    ad = with_config(
        sub.add_parser("add", help="add a public YouTube ad by hand, or attach one (--for)")
    )
    ad.add_argument("url", help="YouTube watch / shorts / youtu.be URL or the video id")
    ad.add_argument("--advertiser", help="the advertiser's name (a new YouTube row)")
    ad.add_argument(
        "--for",
        dest="attach_to",
        help="attach the video to this creative instead (e.g. g-CR0123... from the Ads "
        "Transparency page that plays it): it keeps its dates and regions and gets tagged",
    )
    ad.add_argument("--genre", help="puzzle, sort, casual, word, board, other")
    ad.add_argument("--note", help="what you know about it (title, where you saw it)")
    ad.add_argument("--first", help="first seen running (YYYY-MM-DD; default today)")
    ad.add_argument("--last", help="last seen running (YYYY-MM-DD; default today)")
    ad.add_argument("--tag", action="store_true", help="tag it now (LLM within the budget)")
    ad.set_defaults(func=cmd_add)
    tg = with_config(sub.add_parser("tag", help="tag new or changed creatives"))
    tg.add_argument("--limit", type=int, help="LLM requests at most (default tagging.max_per_run)")
    tg.add_argument("--key", action="append", help="only this creative (repeatable)")
    tg.add_argument("--no-llm", action="store_true", help="keyword and metadata tags only")
    tg.set_defaults(func=cmd_tag)
