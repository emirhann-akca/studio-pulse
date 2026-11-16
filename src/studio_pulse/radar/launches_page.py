"""/radar/yeni ("Yeni Çıkanlar / New launches") in `pulse serve`: local only, like /radar.

A card per game released in the chosen window (7, 14 or 30 days up to the radar's newest day):
icon, name, publisher, store, release date, how the radar found it, mechanic tags, up to two
screenshots and a store link. Store text is rendered through Jinja autoescape; store and Play
links are rebuilt from validated ids; image URLs are kept only when they are https URLs on
Apple's image CDN. Filter values are matched against the options the page offers, so nothing
a visitor types is echoed. The route sends its own Content-Security-Policy: the shell's, plus
Apple's image hosts (is1..is5-ssl.mzstatic.com) in img-src. Scripts stay as strict as
everywhere else.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..config import Config, ConfigError
from .digest import LABELS
from .launches import (
    DAY_OPTIONS,
    FOUND_VIA,
    IMG_ORIGINS,
    T,
    latest_day,
    recent,
    view,
    window_start,
)
from .launches import watched_names as _watched


def page_csp() -> str:
    """The shell's policy (same script hashes as serve/app.py) plus Apple's image CDN."""
    from ..dashboard.assets import JS
    from ..serve import security, ui

    hashes = [
        security.csp_hash(ui.UI_JS),
        security.csp_hash(JS),
        security.csp_hash(JS.replace("\r\n", "\n").replace("\n", "\r\n")),
    ]
    return security.content_security_policy(hashes, img_src=list(IMG_ORIGINS))


def register(app: FastAPI, cfg: Config, templates: Any, context: Any, open_db: Any) -> None:
    @app.get("/radar/yeni", response_class=HTMLResponse)
    def launches_page(request: Request, lang: str | None = None):
        from ..serve.ui import request_lang
        from .settings import from_config

        lg = request_lang(request, lang)
        error = None
        try:
            s = from_config(cfg)
            launch_days, publishers = int(s.params["launch_days"]), s.publishers
        except (ConfigError, ValueError, OSError) as exc:
            error, launch_days, publishers = str(exc), 14, []
        default = launch_days if launch_days in DAY_OPTIONS else 14
        q = request.query_params
        days = next((d for d in DAY_OPTIONS if str(d) == q.get("days")), default)
        rows: list[dict[str, Any]] = []
        day = None
        if open_db is not None and error is None:
            con, _ = open_db(cfg)
            if con is not None:
                try:
                    day = latest_day(con)
                    rows = recent(con, day, days) if day else []
                finally:
                    con.close()
        watched = _watched(publishers)
        cards = [view(r, lg, watched) for r in rows]
        mech_opts = sorted({m for c in cards for m in c["mechanics"]})
        mechanic = next((m for m in mech_opts if m == q.get("mechanic")), None)
        shown = [c for c in cards if mechanic is None or mechanic in c["mechanics"]]
        counts = {k: sum(1 for c in shown if c["via"] == k) for k in FOUND_VIA}
        keep = {"lang": "en"} if lg == "en" else {}
        day_links = [
            (d, "/radar/yeni?" + urlencode({**keep, "days": d, **({"mechanic": mechanic} if mechanic else {})}))
            for d in DAY_OPTIONS
        ]  # fmt: skip
        resp = templates.TemplateResponse(
            request,
            "radar_launches.html",
            context(
                request, "radar", chosen="@launches", source="files", lang=lg, t=T[lg],
                cards=shown, total=len(cards), counts=counts, days=days, day=day,
                start=window_start(day, days) if day else None,
                day_options=DAY_OPTIONS, day_links=day_links, mech_opts=mech_opts,
                mechanic=mechanic, watched_n=len(publishers), error=error,
                observed=LABELS[lg]["observed"],
            ),
        )  # fmt: skip
        resp.headers["content-security-policy"] = page_csp()
        return resp
