"""The radar page of `pulse serve` ("Pazar Radarı / Market Radar"): local only.

It renders the newest facts file of the configured out_dir, and the industry news list
(news.json, written by `pulse radar news` and by every radar run). Store names, links and
quotes stay on this machine; nothing here is written to the static dashboard or the public
demo. Feed text is shown escaped (autoescape) and a news link is rendered only when it is
http(s).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..config import Config, ConfigError
from . import news as news_mod
from .digest import CLASS, COUNTRY, LABELS, LIST


def news_items(out_dir: Path, limit: int = 30) -> dict[str, Any] | None:
    doc = news_mod.read_file(out_dir)
    if not doc:
        return None
    items = []
    for it in (doc.get("items") or [])[:limit]:
        if not isinstance(it, dict):
            continue
        link = str(it.get("link") or "")
        items.append(
            {
                "title": str(it.get("title") or ""),
                "link": link if link.startswith(("https://", "http://")) else None,
                "source": str(it.get("source_name") or it.get("source") or ""),
                "date": str(it.get("published_at") or it.get("first_seen") or "")[:10],
                "summary": str(it.get("summary") or ""),
                "topics": [t for t in it.get("topics") or [] if t in news_mod.TOPICS],
                "relevance": it.get("relevance"),
                "why": str(it.get("why") or ""),
                "llm": it.get("tag_source") == "llm",
            }
        )
    feeds = [f for f in doc.get("feeds") or [] if isinstance(f, dict) and f.get("error")]
    return {"generated_at": doc.get("generated_at"), "days": doc.get("days"), "items": items,
            "failed": feeds}  # fmt: skip


def latest_facts(out_dir: Path) -> dict[str, Any] | None:
    files = sorted(out_dir.glob("*.facts.json")) if out_dir.is_dir() else []
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def known_games(open_db: Any, cfg: Config, ids: list[str]) -> set[str]:
    """The ids among `ids` that the warehouse has a game page for (/radar/oyun/<id> answers
    404 for the others, e.g. a facts file whose run never reached this warehouse)."""
    if open_db is None or not ids:
        return set()
    con, _ = open_db(cfg)
    if con is None:
        return set()
    found: set[str] = set()
    try:
        for table in ("raw_mr_app", "stg_mr_rank_input"):  # what game_view() reads
            try:
                found |= {
                    str(r[0])
                    for r in con.execute(
                        f"SELECT DISTINCT app_id FROM {table} "
                        "WHERE list_contains(?::VARCHAR[], app_id)",
                        [ids],
                    ).fetchall()
                }
            except Exception:  # the table does not exist in this warehouse
                continue
        return found
    finally:
        con.close()


def register(app: FastAPI, cfg: Config, templates: Any, context: Any, open_db: Any = None) -> None:
    @app.get("/radar", response_class=HTMLResponse)
    def radar_page(request: Request, lang: str | None = None):
        from ..serve.ui import request_lang
        from .settings import from_config

        lang = request_lang(request, lang)
        news = None
        try:
            out_dir = from_config(cfg).out_dir
            facts = latest_facts(out_dir)
            news = news_items(out_dir)
            error = None
            linked = known_games(
                open_db, cfg, [str(i.get("app_id")) for i in (facts or {}).get("items") or []]
            )
        except (ConfigError, ValueError, OSError) as exc:
            facts, error, linked = None, str(exc), set()
        return templates.TemplateResponse(
            request,
            "radar.html",
            context(
                request,
                "radar",
                chosen="@market",
                source="files",
                facts=facts,
                news=news,
                linked=linked,
                topic_names={k: v[0 if lang == "tr" else 1] for k, v in news_mod.TOPICS.items()},
                error=error,
                lang=lang,
                labels=LABELS[lang],
                classes=CLASS[lang],
                countries=COUNTRY[lang],
                lists=LIST[lang],
            ),
        )
