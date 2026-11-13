"""The radar page of `pulse serve` ("Pazar Radarı / Market Radar"): local only.

It renders the newest facts file of the configured out_dir. Store names, links and quotes
stay on this machine; nothing here is written to the static dashboard or the public demo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..config import Config, ConfigError
from .digest import CLASS, COUNTRY, LABELS, LIST


def latest_facts(out_dir: Path) -> dict[str, Any] | None:
    files = sorted(out_dir.glob("*.facts.json")) if out_dir.is_dir() else []
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def register(app: FastAPI, cfg: Config, templates: Any, context: Any) -> None:
    @app.get("/radar", response_class=HTMLResponse)
    def radar_page(request: Request, lang: str | None = None):
        from ..serve.ui import request_lang
        from .settings import from_config

        lang = request_lang(request, lang)
        try:
            out_dir = from_config(cfg).out_dir
            facts = latest_facts(out_dir)
            error = None
        except (ConfigError, ValueError, OSError) as exc:
            facts, error = None, str(exc)
        return templates.TemplateResponse(
            request,
            "radar.html",
            context(
                request,
                "radar",
                chosen="@market",
                source="files",
                facts=facts,
                error=error,
                lang=lang,
                labels=LABELS[lang],
                classes=CLASS[lang],
                countries=COUNTRY[lang],
                lists=LIST[lang],
            ),
        )
