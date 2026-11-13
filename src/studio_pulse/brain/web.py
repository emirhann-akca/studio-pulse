"""`pulse serve` pages for the brain: /insights and /ask (B9).

/insights shows the newest stored run of the insight jobs (meta_insights);
before the first run it computes them live, read-only. Every card's text is
template + verifier output, and its evidence (method, SQL, rows, n, interval,
p/q) sits under it. /ask answers through the router; POST /ask needs the
page's token and a same-origin request, like "run now". Turkish / English via
?lang=tr|en. No JavaScript build: htmx is already vendored.
"""

from __future__ import annotations

import datetime as dt
import secrets
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import runstate
from ..config import Config
from ..llm.base import LLMError
from ..serve.ui import request_lang
from . import evidence as evidence_mod
from . import fmt, llm, narrate
from .executor import LockedSession

UI: dict[str, dict[str, str]] = {
    "en": {
        "insights": "Insights", "ask": "Ask", "evidence": "Evidence", "method": "Method",
        "interval": "Interval", "recommend": "Recommended", "none": "No insights yet. Run a refresh (or `pulse insights`) to compute them.",
        "live": "Computed live from the warehouse (read-only); the next refresh stores them.",
        "stored": "Stored run", "tested": "hypotheses tested", "blocked": "Data first: some apps have data problems, so their numbers are not explained below.",
        "critical": "Critical", "warning": "Warning", "info": "Info", "rows": "rows",
        "placeholder": "e.g. Which levels are the hardest in my game?", "ask_btn": "Ask",
        "route_verified_query": "Verified query", "route_catalog": "Catalog metric",
        "route_sql": "Generated SQL (checked)", "route_refused": "Cannot answer",
        "examples": "Examples", "sql": "SQL", "unverified": "Number check failed: text withheld.",
        "truncated": "Result cut at the row limit.", "lang_other": "Türkçe", "q": "q",
        "params": "parameters", "definition": "Definition", "as_of": "as of",
        "how": "Numbers are formatted by code and checked against the evidence; recommendations cite evidence ids.",
    },
    "tr": {
        "insights": "İçgörüler", "ask": "Sor", "evidence": "Kanıt", "method": "Yöntem",
        "interval": "Aralık", "recommend": "Öneri", "none": "Henüz içgörü yok. Hesaplamak için bir yenileme (ya da `pulse insights`) çalıştırın.",
        "live": "Ambardan canlı hesaplandı (salt okunur); bir sonraki yenileme kaydeder.",
        "stored": "Kayıtlı koşum", "tested": "test edilen hipotez", "blocked": "Önce veri: bazı uygulamalarda veri sorunu var, bu yüzden sayıları aşağıda açıklanmıyor.",
        "critical": "Kritik", "warning": "Uyarı", "info": "Bilgi", "rows": "satır",
        "placeholder": "ör. Oyunumda en zor seviyeler hangileri?", "ask_btn": "Sor",
        "route_verified_query": "Doğrulanmış sorgu", "route_catalog": "Katalog metriği",
        "route_sql": "Üretilmiş SQL (denetlendi)", "route_refused": "Cevaplanamaz",
        "examples": "Örnekler", "sql": "SQL", "unverified": "Sayı kontrolü geçmedi: metin gösterilmiyor.",
        "truncated": "Sonuç satır sınırında kesildi.", "lang_other": "English", "q": "q",
        "params": "parametreler", "definition": "Tanım", "as_of": "tarih",
        "how": "Sayıları kod biçimlendirir ve kanıtla karşılaştırır; her öneri kanıt kimliği gösterir.",
    },
}  # fmt: skip

EXAMPLES = {
    "en": [
        "Which levels are the hardest in {app}?",
        "D1 retention by app version for {app}",
        "What was the payer conversion of {app} in the last 7 days?",
        "Which campaign has the best D7 ROAS?",
    ],
    "tr": [
        "{app} en zor seviyeler hangileri?",
        "{app} sürüme göre D1 elde tutma",
        "{app} son 7 günde ödeyen dönüşümü neydi?",
        "Veri güncel mi?",
    ],
}


def _lang(value: str | None) -> str:
    return value if value in fmt.LANGS else "en"


def db_path(cfg: Config) -> Path | None:
    """The file pages may read: the live warehouse, or the last good snapshot during a refresh."""
    hb = runstate.read_heartbeat(cfg.state_dir) or {}
    if hb.get("status") != "running" and cfg.warehouse.exists():
        return cfg.warehouse
    snap = runstate.snapshot_path(cfg.state_dir)
    return snap if snap.exists() else None


def _client(cfg: Config) -> llm.LLMClient | None:
    try:
        return llm.client_for(cfg)
    except (ValueError, LLMError):  # a bad provider setting: the page falls back to templates
        return None


def _ev_view(e: evidence_mod.Evidence, lang: str) -> dict[str, Any]:
    unit = e.effect_unit or "ratio"
    out: dict[str, Any] = {
        "id": e.id,
        "method": e.method,
        "sql": e.sql.strip(),
        "params": [str(p) for p in e.params],
        "definition": e.definition,
        "aggregation": e.aggregation,
        "n_text": fmt.number(e.n, "count", lang) if e.n is not None else None,
        "ci_text": fmt.interval(e.ci[0], e.ci[1], unit, lang) if e.ci else None,
        "p_text": " · ".join(
            x for x in (
                fmt.number(e.p, "p", lang) if e.p is not None else "",
                fmt.number(e.q, "q", lang) if e.q is not None else "",
            ) if x
        ) or None,
        "columns": e.columns,
        "rows": [[_cell(v, lang, c) for c, v in zip(e.columns, r, strict=False)] for r in e.rows[:12]],
        "more": max(0, len(e.rows) - 12),
    }  # fmt: skip
    return out


def _cell(v: Any, lang: str, column: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, (str, dt.date, bool)):
        return str(v)
    if column in ("p", "q", "p_value", "q_value"):
        x = float(v)
        return "< " + fmt.plain(0.001, 3, lang) if x < 0.001 else fmt.plain(x, 3, lang)
    if isinstance(v, int) or float(v).is_integer():
        return fmt.count(float(v), lang)
    return fmt.plain(float(v), 4 if abs(float(v)) < 1 else 2, lang)


def cards(
    insights: list[evidence_mod.Insight], lang: str, client: llm.LLMClient | None
) -> list[dict[str, Any]]:
    out = []
    for ins in insights:
        n = narrate.narrate(ins, lang, client)
        out.append(
            {
                "id": ins.id,
                "rank": ins.rank,
                "severity": ins.severity,
                "sev_label": UI[lang][ins.severity],
                "kind": ins.kind,
                "app": ins.app_id,
                "blocked": ins.blocked,
                "title": n.title,
                "body": n.body if n.verified else UI[lang]["unverified"],
                "caveats": n.caveats,
                "actions": n.actions,
                "source": n.source,
                "q_text": fmt.number(ins.q, "q", lang) if ins.q is not None else None,
                "evidence": [_ev_view(e, lang) for e in ins.evidence],
            }
        )
    return out


def register(
    app: FastAPI, cfg: Config, base: Jinja2Templates, *, context: Any, hosts: set[str]
) -> None:
    here = Path(str(resources.files("studio_pulse.brain").joinpath("templates")))
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader([jinja2.FileSystemLoader(str(here)), base.env.loader]),
        autoescape=jinja2.select_autoescape(),
    )
    env.globals.update(base.env.globals)
    templates = Jinja2Templates(env=env)

    def load_insights(lang: str) -> dict[str, Any]:
        from .. import alerts, pipeline
        from . import insights as jobs

        path = db_path(cfg)
        if path is None:
            return {"cards": [], "run": None, "live": False, "source": "none"}
        source = "live" if path == cfg.warehouse else "snapshot"
        session = LockedSession.open(path)
        con = session.con
        try:
            run, stored = evidence_mod.latest(con)
            live = False
            tested = None
            if run is None:
                rules_path = pipeline.default_file(cfg, None, "alerts_path", "alerts.yaml")
                rules = alerts.load_rules(rules_path) if rules_path else []
                rep = jobs.run(con, as_of=cfg.as_of, rules=rules)
                stored, live, tested = rep.insights, True, rep.tested
                run = {"as_of": rep.as_of, "created_at": None, "run_id": None}
        finally:
            session.close()
        return {
            "cards": cards(stored, lang, _client(cfg)),
            "run": run,
            "live": live,
            "tested": tested,
            "source": source,
        }

    app.state.brain_insights = load_insights  # the Studio's per-app AI panel
    app.state.brain_templates = templates

    @app.get("/insights", response_class=HTMLResponse)
    def insights_page(request: Request, lang: str | None = None):
        lg = request_lang(request, lang)
        data = load_insights(lg)
        blocked = any(c["blocked"] for c in data["cards"])
        return templates.TemplateResponse(
            request,
            "insights.html",
            context(request, "insights", lang=lg, t=UI[lg], blocked=blocked, **data),
        )

    def examples(lang: str) -> list[str]:
        path = db_path(cfg)
        app_id = cfg.apps[0].id if cfg.apps else "app"
        if path is not None and cfg.apps:
            app_id = next((a.id for a in cfg.apps if a.sources), cfg.apps[0].id)
        return [e.format(app=app_id) for e in EXAMPLES[lang]]

    def answer(question: str, lang: str) -> Any:
        from .qa import Router

        path = db_path(cfg)
        if path is None:
            return None
        with LockedSession.open(path) as s:
            return Router(s, client=_client(cfg), as_of=cfg.as_of).ask(question, lang)

    app.state.brain_answer = answer

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request, lang: str | None = None, q: str | None = None):
        lg = request_lang(request, lang)
        ans = answer(q, lg) if q else None
        return templates.TemplateResponse(
            request,
            "ask.html",
            context(
                request, "ask", lang=lg, t=UI[lg], q=q or "", answer=ans,
                examples=examples(lg), source="live" if db_path(cfg) == cfg.warehouse else "snapshot",
            ),
        )  # fmt: skip

    @app.post("/ask", response_class=HTMLResponse)
    async def ask_post(request: Request):
        token = str(base.env.globals.get("token", ""))
        if not token or not secrets.compare_digest(request.headers.get("x-pulse-token", ""), token):
            return PlainTextResponse("missing or wrong token", 403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in hosts:
            return PlainTextResponse("cross-origin request refused", 403)
        # htmx posts application/x-www-form-urlencoded; parsed here so the UI needs no
        # python-multipart dependency
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        q = (form.get("q") or [""])[0]
        lg = request_lang(request, (form.get("lang") or [None])[0])
        ans = await run_in_threadpool(answer, q, lg) if q.strip() else None
        return templates.TemplateResponse(
            request, "_answer.html", {"request": request, "answer": ans, "t": UI[lg], "lang": lg}
        )
