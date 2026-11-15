"""The ad centre's "Yapay zekâ özeti": a short, grounded paragraph per game and period.

Code writes it; a model may only restyle it. Each sentence is a template filled with numbers computed here (brain/
fmt.py formats them, so the verifier reads them back), and every number is checked against the evidence before the
text is shown (brain/verify.py); a sentence whose numbers do not verify is dropped. With an LLM configured
(`brain:` / `reklam: {ai: ...}`, the same providers and fallback as the analyst and the narrator) the paragraph may
be rewritten by brain.narrate.restyle, which keeps the rewrite only if all its numbers still verify, it adds no URL or
instruction, and it keeps its Turkish letters; otherwise the template stands. The model call is capped
(`reklam.ai.budget_usd`, default 0.05 USD per server run) and both the paragraph and the model's answer are cached
by content hash, so opening the page again costs nothing.

What it says, and how honest it is about noise:

* Spend, installs and CPI, and IPM, against the previous period of the same length. "Beyond normal noise" means a
  Welch test on the daily values (the day-to-day swing of ad delivery is much larger than a binomial count
  suggests, so pooled counts alone would call almost every wobble significant): p < 0.05 with at least 3 days on
  each side. Otherwise it says "henüz anlamlı değil", with p.
* The best and the weakest creative of the period on the same network (at least 5,000 impressions each), with the
  IPM's 95% Wilson interval, and why the weak one is weak: IPM = CTR x installs per click, so a low CTR points at
  the first seconds and a low install rate per click at the promise vs the store page.
* Fatigue: a creative live 14+ days whose IPM fell 20% or more from its first week (two-proportion test).
* What changed: the newest changes in or just before the period (creative-forge's audit log or the change file),
  each with the campaign's IPM or CPI over up to 7 days before and after it and the same Welch test.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from ..brain import fmt as F
from ..brain.evidence import Evidence, Fact
from . import reklam_data as D
from . import reklam_events as E

MIN_IMPS = 5000
DEFAULT_BUDGET = 0.05

T: dict[str, dict[str, str]] = {
    "tr": {
        "spend": "Bu dönemde {spend} harcandı; ağlar {installs} kurulum bildirdi, CPI {cpi}.",
        "spend_cmp": "Bu dönemde {spend} harcandı; ağlar {installs} kurulum bildirdi. CPI {cpi}, önceki döneme göre {change} ({sig}).",
        "ipm_cmp": "IPM {ipm}, önceki döneme göre {change} ({sig}).",
        "roas": "D7 ROAS {d7} ({n} kohort günü, ağ kurulumları üzerinden modellenmiş gelir).",
        "sig_yes": "günlük dalgalanmanın ötesinde, {p}",
        "sig_no": "henüz anlamlı değil, {p}",
        "sig_short": "henüz anlamlı değil: karşılaştırma için her iki tarafta en az 3 gün gerekir",
        "best": "En iyi kreatif {name} ({net}): IPM {ipm}, %95 aralık {lo}–{hi}.",
        "weak": "En zayıf kreatif {name} ({net}): IPM {ipm}, ağın geri kalanı {rest}; {why}. {sig}.",
        "why_ctr": "tıklama oranı düşük ({ctr}, geri kalan {ctr_rest}), yani ilk saniyeler kaydırmayı durdurmuyor",
        "why_cvr": "tıklama oranı yerinde ama tıklayanların kurulum oranı düşük ({cvr}, geri kalan {cvr_rest}): reklamın vaadi mağaza sayfasıyla uyuşmuyor olabilir",
        "why_both": "hem tıklama oranı ({ctr}) hem tıklayanların kurulum oranı ({cvr}) geri kalanın altında",
        "diff_yes": "Fark anlamlı: aralıklar örtüşmüyor",
        "diff_no": "Fark henüz anlamlı değil: aralıklar örtüşüyor",
        "tired": "{name} {days} gündür yayında; IPM ilk haftasına göre {change} ({p}): yorulma belirtisi, yeni bir kanca deneme zamanı.",
        "change": "{when}: {what}. {metric} {before} → {after} ({change}; {sig}).",
        "change_young": "{when}: {what}. Sonrası için {n} günlük veri var; henüz anlamlı değil.",
        "change_plain": "{when}: {what}.",
        "nothing": "Bu dönemde reklam harcaması yok.",
        "metric_ipm": "Kampanyanın IPM'i",
        "metric_cpi": "Kampanyanın CPI'ı",
    },
    "en": {
        "spend": "This period spent {spend}; the networks reported {installs} installs, CPI {cpi}.",
        "spend_cmp": "This period spent {spend}; the networks reported {installs} installs. CPI {cpi}, {change} on the previous period ({sig}).",
        "ipm_cmp": "IPM {ipm}, {change} on the previous period ({sig}).",
        "roas": "D7 ROAS {d7} ({n} cohort days, revenue modelled from network installs).",
        "sig_yes": "beyond normal day-to-day noise, {p}",
        "sig_no": "not significant yet, {p}",
        "sig_short": "not significant yet: a comparison needs at least 3 days on each side",
        "best": "Best creative {name} ({net}): IPM {ipm}, 95% interval {lo}–{hi}.",
        "weak": "Weakest creative {name} ({net}): IPM {ipm} against {rest} for the rest of the network; {why}. {sig}.",
        "why_ctr": "its click-through rate is low ({ctr} against {ctr_rest}): the first seconds do not stop the scroll",
        "why_cvr": "clicks are fine but few clickers install ({cvr} against {cvr_rest}): the ad's promise may not match the store page",
        "why_both": "both its click-through rate ({ctr}) and its installs per click ({cvr}) are below the rest",
        "diff_yes": "The difference is significant: the intervals do not overlap",
        "diff_no": "The difference is not significant yet: the intervals overlap",
        "tired": "{name} has run for {days} days; its IPM is {change} on its first week ({p}): signs of fatigue, time to try a new hook.",
        "change": "{when}: {what}. {metric} {before} → {after} ({change}; {sig}).",
        "change_young": "{when}: {what}. {n} day(s) of data since; not significant yet.",
        "change_plain": "{when}: {what}.",
        "nothing": "No ad spend in this period.",
        "metric_ipm": "The campaign's IPM",
        "metric_cpi": "The campaign's CPI",
    },
}


class Facts:
    """Collects every number a sentence uses, as evidence facts the verifier checks the text against."""

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.facts: dict[str, Fact] = {}

    def put(self, name: str, value: Any, unit: str) -> str:
        key = f"{name}_{len(self.facts)}"
        self.facts[key] = Fact(value, unit)
        if unit == "ipm":
            return F.plain(float(value), 1, self.lang)
        if unit == "change":
            self.facts[key] = Fact(value, "pct")
            s = F.pct(abs(float(value)), self.lang, 1)
            return ("+" if value > 0 else "−" if value < 0 else "") + s
        if unit == "share":
            self.facts[key] = Fact(value, "pct")
            return F.pct(float(value), self.lang)
        if unit == "roas":
            self.facts[key] = Fact(value, "pct")
            return F.pct(float(value), self.lang, 0)
        return F.number(value, unit, self.lang)

    def data(self, value: Any) -> str:
        return F.data(value)

    def evidence(self, app_id: str) -> Evidence:
        return Evidence(
            id="reklam", kind="reklam_summary", app_id=app_id, method="reklam_ai", facts=self.facts
        )


def welch_days(a: list[float], b: list[float]) -> float | None:
    """p of a Welch test on daily values (None below 3 days a side)."""
    from ..brain.stats import welch_mean_diff

    a = [x for x in a if x is not None and math.isfinite(x)]
    b = [x for x in b if x is not None and math.isfinite(x)]
    if len(a) < 3 or len(b) < 3:
        return None
    r = welch_mean_diff(a, b)
    return None if r is None else r[2]


def _sig(f: Facts, p: float | None) -> str:
    t = T[f.lang]
    if p is None:
        return t["sig_short"]
    return (t["sig_yes"] if p < 0.05 else t["sig_no"]).format(p=f.put("p", p, "p"))


def _series(daily: dict[dt.date, dict[str, Any]], dates: list[dt.date], key: str) -> list[float]:
    out = []
    for d in dates:
        v = (daily.get(d) or {}).get(key)
        if v is not None:
            out.append(float(v))
    return out


def build(ctx: dict[str, Any], lang: str) -> tuple[list[str], Facts]:
    """The template sentences (unverified) and their facts. `ctx` is the page's data (reklam.py)."""
    t = T[lang]
    f = Facts(lang)
    out: list[str] = []
    cur, prev, p = ctx["totals"], ctx["prev_totals"], ctx["period"]
    if not cur.get("spend"):
        return [t["nothing"]], f
    days_cur, days_prev = ctx["daily_cur"], ctx["daily_prev"]
    cd, pd = p.dates(), [p.prev_start + dt.timedelta(days=i) for i in range(p.days)]
    if prev.get("cpi") and cur.get("cpi"):
        pval = welch_days(_series(days_prev, pd, "cpi"), _series(days_cur, cd, "cpi"))
        out.append(t["spend_cmp"].format(spend=f.put("spend", cur["spend"], "usd"),
                                         installs=f.put("installs", cur["installs"], "count"),
                                         cpi=f.put("cpi", cur["cpi"], "usd"),
                                         change=f.put("cpi_change", cur["cpi"] / prev["cpi"] - 1, "change"),
                                         sig=_sig(f, pval)))  # fmt: skip
    else:
        out.append(t["spend"].format(spend=f.put("spend", cur["spend"], "usd"),
                                     installs=f.put("installs", cur.get("installs") or 0, "count"),
                                     cpi=f.put("cpi", cur.get("cpi"), "usd")))  # fmt: skip
    if prev.get("ipm") and cur.get("ipm"):
        pval = welch_days(_series(days_prev, pd, "ipm"), _series(days_cur, cd, "ipm"))
        out.append(t["ipm_cmp"].format(ipm=f.put("ipm", cur["ipm"], "ipm"),
                                       change=f.put("ipm_change", cur["ipm"] / prev["ipm"] - 1, "change"),
                                       sig=_sig(f, pval)))  # fmt: skip
    r = ctx.get("roas") or {}
    if r.get("d7") is not None and r.get("d7_days"):
        out.append(
            t["roas"].format(d7=f.put("d7", r["d7"], "roas"), n=f.put("n7", r["d7_days"], "count"))
        )
    out += _creatives(f, ctx)
    for c in ctx.get("tired") or []:
        out.append(t["tired"].format(name=f.data(E.short(c["name"])), days=f.put("days", c["days"], "count"),
                                     change=f.put("fatigue", c["change"], "change"), p=f.put("p", c["p"], "p")))  # fmt: skip
    out += _changes(f, ctx)
    return out, f


def _creatives(f: Facts, ctx: dict[str, Any]) -> list[str]:
    from ..brain.stats import wilson

    t = T[f.lang]
    rows = [r for r in ctx.get("creatives") or [] if (r.get("impressions") or 0) >= MIN_IMPS]
    by_net: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_net.setdefault(r["network"], []).append(r)
    cands = [(n, rs) for n, rs in by_net.items() if len(rs) >= 2]
    if not cands:
        return []
    net, rs = max(cands, key=lambda x: sum(r.get("spend") or 0 for r in x[1]))
    rs = sorted(rs, key=lambda r: -(r.get("ipm") or 0))
    best, weak = rs[0], rs[-1]
    out = []
    lo, hi = wilson(best["installs"] or 0, best["impressions"])
    f.facts["ci_level"] = Fact(95, "id")  # "%95 aralık" / "95% interval"
    out.append(t["best"].format(name=f.data(E.short(best["ad_name"] or best["ckey"])), net=D.label(net),
                                ipm=f.put("ipm_best", best["ipm"], "ipm"), lo=f.put("lo", lo * 1000, "ipm"),
                                hi=f.put("hi", hi * 1000, "ipm")))  # fmt: skip
    rest = [r for r in rs if r is not weak]
    ri = sum(r.get("installs") or 0 for r in rest)
    rn = sum(r.get("impressions") or 0 for r in rest)
    rc = sum(r.get("clicks") or 0 for r in rest)
    if not rn or not rc or not weak.get("clicks"):
        return out
    rest_ipm, rest_ctr, rest_cvr = ri / rn * 1000, rc / rn, ri / rc
    w_ctr = (weak["clicks"] or 0) / weak["impressions"]
    w_cvr = (weak["installs"] or 0) / weak["clicks"]
    low_ctr, low_cvr = w_ctr < rest_ctr * 0.85, w_cvr < rest_cvr * 0.85
    if low_ctr and low_cvr:
        why = t["why_both"].format(ctr=f.put("ctr", w_ctr, "pct"), cvr=f.put("cvr", w_cvr, "pct"))
    elif low_ctr:
        why = t["why_ctr"].format(
            ctr=f.put("ctr", w_ctr, "pct"), ctr_rest=f.put("ctr_rest", rest_ctr, "pct")
        )
    else:
        why = t["why_cvr"].format(
            cvr=f.put("cvr", w_cvr, "pct"), cvr_rest=f.put("cvr_rest", rest_cvr, "pct")
        )
    a_lo, a_hi = wilson(weak["installs"] or 0, weak["impressions"])
    b_lo, b_hi = wilson(ri, rn)
    sig = t["diff_yes"] if (a_hi < b_lo or b_hi < a_lo) else t["diff_no"]
    out.append(t["weak"].format(name=f.data(E.short(weak["ad_name"] or weak["ckey"])), net=D.label(net),
                                ipm=f.put("ipm_weak", weak["ipm"], "ipm"), rest=f.put("ipm_rest", rest_ipm, "ipm"),
                                why=why, sig=sig))  # fmt: skip
    return out


def _changes(f: Facts, ctx: dict[str, Any]) -> list[str]:
    t = T[f.lang]
    p = ctx["period"]
    end = ctx["data_end"]
    events = [
        e for e in ctx.get("events") or [] if e["kind"] in E.MOVERS and e.get("kind") != "activated"
    ]
    events = [e for e in events if p.start - dt.timedelta(days=3) <= e["day"] <= p.end][-3:]
    out = []
    by_camp = ctx.get("campaign_daily") or {}
    for e in reversed(events):
        when = _when(f, e["day"])
        what = E.text(
            e,
            f.lang,
            lambda v: F.usd(float(v), f.lang) if isinstance(v, (int, float)) else "—",
            quote=F.data,
        )
        what = _num_safe(f, what, e)
        series = by_camp.get(str(e.get("campaign_name") or "")) or {}
        metric = "cpi" if e["kind"] == "budget_changed" else "ipm"
        before = [series.get(e["day"] - dt.timedelta(days=i)) for i in range(1, 8)]
        after = [
            series.get(e["day"] + dt.timedelta(days=i))
            for i in range(0, 7)
            if e["day"] + dt.timedelta(days=i) <= end
        ]
        b = [x[metric] for x in before if x and x.get(metric) is not None]
        a = [x[metric] for x in after if x and x.get(metric) is not None]
        if not series:
            out.append(t["change_plain"].format(when=when, what=what))
            continue
        if len(a) < 3 or len(b) < 3:
            out.append(
                t["change_young"].format(when=when, what=what, n=f.put("n_after", len(a), "count"))
            )
            continue
        mb, ma = sum(b) / len(b), sum(a) / len(a)
        unit = "usd" if metric == "cpi" else "ipm"
        out.append(t["change"].format(when=when, what=what, metric=t[f"metric_{metric}"],
                                      before=f.put("before", mb, unit), after=f.put("after", ma, unit),
                                      change=f.put("change", ma / mb - 1 if mb else 0, "change"),
                                      sig=_sig(f, welch_days(b, a))))  # fmt: skip
    return out


def _when(f: Facts, d: dt.date) -> str:
    from .reklam_charts import day

    f.put("day", d.day, "count")  # the day of month is a number the verifier must find
    return day(d, f.lang)


def _num_safe(f: Facts, what: str, e: dict[str, Any]) -> str:
    """Event texts carry names (quoted as data) and budget amounts (registered as facts)."""
    d = e.get("detail") or {}
    for k in ("before", "after"):
        if isinstance(d.get(k), (int, float)):
            f.put(k, float(d[k]), "usd")
    return what


# -- verification, the optional model and the cache ----------------------------------------------------------
def verified_text(sentences: list[str], f: Facts, app_id: str) -> tuple[str, list[str]]:
    from ..brain import verify

    ev = f.evidence(app_id)
    keep, dropped = [], []
    for s in sentences:
        (dropped if verify.unsupported(s, [ev], f.lang) else keep).append(s)
    return " ".join(keep), dropped


class Summarizer:
    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        raw = getattr(cfg, "raw", None) or {}
        state = Path(
            getattr(cfg, "state_dir", None) or Path(getattr(cfg, "base_dir", ".")) / "pulse-state"
        )
        self.cache = state / "reklam" / "ai"
        ai = (raw.get("reklam") or {}).get("ai") or {}
        self.block = {
            **(raw.get("brain") or {}),
            **ai,
            "budget_usd": float(ai.get("budget_usd", DEFAULT_BUDGET)),
        }
        self._client: Any = "unset"

    def client(self) -> Any:
        if self._client == "unset":
            from ..brain.llm import client_from_config

            try:
                self._client = client_from_config(self.block, shared_block=(self.cfg.raw or {}).get("llm"),
                                                  state_dir=self.cache.parent)  # fmt: skip
            except Exception:  # a broken provider block: templates only
                self._client = None
        return self._client

    def summary(self, ctx: dict[str, Any], lang: str, app_id: str) -> dict[str, Any]:
        sentences, f = build(ctx, lang)
        text, dropped = verified_text(sentences, f, app_id)
        client = self.client()
        name = getattr(client, "name", "none") if client else "none"
        key = hashlib.sha256(
            json.dumps([text, lang, name], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32]
        path = self.cache / f"{key}.json"
        try:
            hit = json.loads(path.read_text(encoding="utf-8"))
            return {**hit, "cached": True, "dropped": dropped}
        except (OSError, ValueError):
            pass
        source = "template"
        if client is not None and text:
            from ..brain.narrate import restyle

            text, source = restyle(text, [f.evidence(app_id)], f.facts, lang, client)
        out = {"text": text, "source": source, "model": name if source == "narrator" else None}
        self.cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return {**out, "cached": False, "dropped": dropped}
