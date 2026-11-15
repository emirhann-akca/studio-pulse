"""/radar/kreatifler ("Rakip Kreatif Kütüphanesi") and /radar/kreatifler/<key> in `pulse serve`.

Local only, like the rest of the radar. Everything that came from an ad repository (names,
ad text, on-screen text, model sentences) is rendered through Jinja autoescape; links and
media URLs are rebuilt from validated ids (library.links), never taken from stored text.
Filter values are matched against the options the page offers, so nothing a visitor types is
echoed. These two routes send their own Content-Security-Policy: the shell's, plus
`img-src https://i.ytimg.com` (YouTube thumbnails) and, on the detail page only,
`frame-src https://www.youtube-nocookie.com` (the official embedded player). Scripts stay as
strict as everywhere else.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...config import Config, ConfigError
from ...serve.dataflow import num
from . import labels as L
from . import library, score
from .settings import from_config

KEY = re.compile(r"^(?:g-CR\d{10,30}|m-\d{5,25}|t-\d{5,25}|y-[A-Za-z0-9_-]{11})$")
YT_IMG = "https://i.ytimg.com"
YT_FRAME = "https://www.youtube-nocookie.com"
VIEWS = ("all", "ai", "hooks")


def page_csp(frames: bool) -> str:
    """The shell's policy (same script hashes as serve/app.py) plus the YouTube origins."""
    from ...dashboard.assets import JS
    from ...serve import security, ui

    hashes = [
        security.csp_hash(ui.UI_JS),
        security.csp_hash(JS),
        security.csp_hash(JS.replace("\r\n", "\n").replace("\n", "\r\n")),
    ]
    return security.content_security_policy(
        hashes, img_src=[YT_IMG], frame_src=[YT_FRAME] if frames else []
    )


def _view(r: dict[str, Any], as_of: dt.date, lang: str) -> dict[str, Any]:
    """Display values for one creative (labels in the page language, no raw codes)."""
    p = r["parts"]
    days = p["longevity"]["signal"]
    ago = p["active"]["signal"]
    first, last = r.get("first_shown"), r.get("last_shown")
    start = as_of - dt.timedelta(days=365)
    tl = None
    if first and last:
        left = max(0, (first - start).days) / 365
        right = min(365, max(0, (last - start).days + 1)) / 365
        tl = {"left": round(100 * left, 1), "width": round(max(1.0, 100 * (right - left)), 1),
              "start": start.isoformat()}  # fmt: skip
    adv = str(r.get("advertiser_name") or r.get("advertiser_key") or "?")
    return {
        "key": r["creative_key"],
        "hook": L.name(L.HOOKS, r.get("hook") or "unknown", lang),
        "hook_key": r.get("hook") or "unknown",
        "ai": L.name(L.AI_LEVELS, r.get("ai_likelihood") or "unknown", lang),
        "ai_key": r.get("ai_likelihood") or "unknown",
        "format": L.name(L.FORMATS, r.get("format") or "UNKNOWN", lang),
        "genre": L.name(L.GENRE_NAMES, r.get("genre"), lang),
        "source": L.name(L.SOURCES, r.get("source"), lang),
        "tag_source": L.name(L.TAG_SOURCES, r.get("tag_source") or "metadata", lang),
        "adv": adv,
        "initials": "".join(w[0] for w in adv.split()[:2]).upper() or "?",
        "days": days,
        "ago": ago,
        "running": r["is_active"],
        "band": score.band(r["score"]),
        "timeline": tl,
    }


def register(app: FastAPI, cfg: Config, templates: Any, context: Any, open_db: Any) -> None:
    def load(
        lang: str,
    ) -> tuple[list[dict[str, Any]], Any, dt.date, list[dict[str, Any]], str | None]:
        s = from_config(cfg)
        as_of = cfg.as_of or dt.date.today()
        if open_db is None:
            return [], s, as_of, [], None
        con, _ = open_db(cfg)
        if con is None:
            return [], s, as_of, [], None
        try:
            return library.load(con, s, as_of), s, as_of, library.runs(con), None
        finally:
            con.close()

    def ctx(request: Request, lang: str | None, **extra: Any) -> dict[str, Any]:
        from ...serve.ui import request_lang

        lang = request_lang(request, lang)
        return context(
            request, "radar", chosen="@market", source="files", lang=lang, t=L.T[lang],
            L=L, label=lambda table, k: L.name(table, k, lang), **extra,
        )  # fmt: skip

    @app.get("/radar/kreatifler", response_class=HTMLResponse)
    def creatives_page(request: Request, lang: str | None = None):
        from ...serve.ui import request_lang

        lg = request_lang(request, lang)
        try:
            rows, s, as_of, runs, error = load(lg)
        except (ConfigError, ValueError, OSError) as exc:
            rows, s, as_of, runs, error = [], from_config(cfg), dt.date.today(), [], str(exc)
        f = library.clean_filters(dict(request.query_params), rows)
        view = request.query_params.get("view")
        view = view if view in VIEWS else "all"
        shown = library.apply(rows, f)
        if view == "ai":  # the AI-made videos: likelihood medium or high, watched by the LLM
            shown = [r for r in shown if r.get("ai_likelihood") in ("medium", "high")]
        groups = []
        if view == "hooks":  # the other videos, separated by hook type
            for h in L.LLM_HOOKS:
                rs = [r for r in shown if r.get("hook") == h]
                if rs:
                    groups.append({"hook": h, "n": len(rs),
                                   "cards": [(r, _view(r, as_of, lg)) for r in rs[:8]]})  # fmt: skip
        untagged = sum(1 for r in shown if (r.get("hook") or "unknown") == "unknown")
        watched = sum(1 for r in rows if r.get("tag_source") == "llm_video")
        opts = library.options(rows)
        names = {r["advertiser_key"]: r.get("advertiser_name") for r in rows}
        keep = {k: ("1" if v is True else v) for k, v in f.items()}
        if lg == "en":
            keep["lang"] = "en"
        views = [(v, "/radar/kreatifler?" + urlencode({**keep, "view": v})) for v in VIEWS]
        resp = templates.TemplateResponse(
            request,
            "radar_creatives.html",
            ctx(
                request, lang,
                cards=[(r, _view(r, as_of, lg)) for r in shown[: library.MAX_CARDS]],
                total=len(shown), all_n=len(rows), f=f, opts=opts, adv_names=names,
                summary=library.summary(rows, s, as_of), runs=runs,
                genre_names=[L.name(L.GENRE_NAMES, g, lg).lower() for g in s.genres],
                error=error, as_of=as_of, min_scores=(20, 35, 50, 60, 75), view=view,
                views=views, groups=groups, untagged=untagged, watched=watched,
            ),
        )  # fmt: skip
        resp.headers["content-security-policy"] = page_csp(frames=False)
        return resp

    @app.get("/radar/kreatifler/{key}", response_class=HTMLResponse)
    def creative_page(request: Request, key: str, lang: str | None = None):
        from ...serve.ui import request_lang

        if not KEY.match(key):
            raise HTTPException(404)
        lg = request_lang(request, lang)
        try:
            rows, s, as_of, _runs, _ = load(lg)
        except (ConfigError, ValueError, OSError):
            rows, s, as_of = [], from_config(cfg), dt.date.today()
        r = library.find(rows, key)
        if r is None:
            raise HTTPException(404)
        sim = library.similar(rows, r)
        resp = templates.TemplateResponse(
            request,
            "radar_creative.html",
            ctx(
                request, lang, r=r, v=_view(r, as_of, lg),
                similar=[(x, _view(x, as_of, lg)) for x in sim],
                brief=library.brief_link(r, lg),
                signal_why=library.signal_reason(r, lg, lambda v: num(v, lg)), weights=s.weights, as_of=as_of,
                active_days=s.active_days,
            ),
        )  # fmt: skip
        resp.headers["content-security-policy"] = page_csp(frames=True)
        return resp
