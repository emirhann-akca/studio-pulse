"""The dossier pages of `pulse serve` (local only): /radar/dosya lists the dossiers written by
`pulse radar dossier`, /radar/dosya/<key> shows one (Turkish first, ?lang=en), and
/radar/dosya/<key>/shot/<sN> serves a screenshot from the local cache. Store texts,
screenshots and reviews stay on this machine; nothing is written to the dashboard or the demo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from ...config import Config, ConfigError
from ..digest import COUNTRY, LIST, fmt
from .labels import LABELS
from .render import T
from .rubric import FACTOR_NAMES, VERDICTS

_KEY = re.compile(r"^(?:apple-\d{5,}|play-[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)$")


def _out_dir(cfg: Config) -> Path:
    from .settings import from_config

    return from_config(cfg).out_dir


def load(out_dir: Path, key: str) -> dict[str, Any] | None:
    from .run import latest

    files = latest(out_dir, key)
    return json.loads(files[0].read_text(encoding="utf-8")) if files else None


def index(out_dir: Path) -> list[dict[str, Any]]:
    from .run import latest

    rows = []
    for p in latest(out_dir):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rows.append(
            {
                "key": d["key"],
                "name": d["app"]["name"],
                "developer": d["app"].get("developer"),
                "as_of": d["as_of"],
                "verdict": d["decision"]["verdict"],
                "score": d["decision"]["score"]["value"],
                "mechanic": (d.get("mechanic") or {}).get("value"),
            }
        )
    return sorted(rows, key=lambda r: (r["as_of"], r["name"]), reverse=True)


def register(app: FastAPI, cfg: Config, templates: Any, context: Any) -> None:
    def ctx(request: Request, lang: str | None, **extra: Any) -> dict[str, Any]:
        from ...serve.ui import request_lang

        lang = request_lang(request, lang)
        return context(
            request,
            "radar",
            chosen="@market",
            source="files",
            lang=lang,
            labels=LABELS[lang],
            t=T[lang],
            countries=COUNTRY[lang],
            lists=LIST[lang],
            factor_names=FACTOR_NAMES[lang],
            verdicts=VERDICTS[lang],
            fmt=lambda x, n=0: fmt(x, lang, n),
            **extra,
        )

    @app.get("/radar/dosya", response_class=HTMLResponse)
    def dossier_index(request: Request, lang: str | None = None):
        try:
            rows, error = index(_out_dir(cfg)), None
        except (ConfigError, ValueError, OSError) as exc:
            rows, error = [], str(exc)
        return templates.TemplateResponse(
            request, "radar_dossier_index.html", ctx(request, lang, rows=rows, error=error)
        )

    @app.get("/radar/dosya/{key}", response_class=HTMLResponse)
    def dossier_page(request: Request, key: str, lang: str | None = None):
        if not _KEY.match(key):
            raise HTTPException(404)
        try:
            d, error = load(_out_dir(cfg), key), None
        except (ConfigError, ValueError, OSError) as exc:
            d, error = None, str(exc)
        if d is None and error is None:
            raise HTTPException(404)
        return templates.TemplateResponse(
            request, "radar_dossier.html", ctx(request, lang, d=d, error=error)
        )

    @app.get("/radar/dosya/{key}/shot/{sid}")
    def dossier_shot(key: str, sid: str):
        if not _KEY.match(key) or not re.fullmatch(r"s\d{1,2}", sid):
            raise HTTPException(404)
        d = load(_out_dir(cfg), key)
        shot = next((x for x in (d or {}).get("screenshots") or [] if x["id"] == sid), None)
        if not shot:
            raise HTTPException(404)
        root = (Path(cfg.state_dir) / "radar-dossier").resolve()
        p = Path(shot["file"]).resolve()
        if root not in p.parents or not p.is_file():  # only the dossier's own cache
            raise HTTPException(404)
        return FileResponse(p, media_type=shot.get("mime") or "application/octet-stream")
