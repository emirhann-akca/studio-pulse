"""The Studio's numbers for a chosen period, with the previous period and their evidence.

Every KPI tile is one entry of METRICS: the SQL that returns its daily rows (date, value,
n), how the days aggregate, and the sample size below which the number is not shown.
The tile and its drill-down page (/studio/<app>/metric/<key>) run the same SQL, and the
drill-down prints it, so a number on screen can always be traced to its query.

Honest at small scale: retention from 3 installs or crash-free users from 5 players is
noise, so a metric whose n is under its floor shows "not enough data (n=…)" instead of a
percentage, and no change against the previous period is computed for it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

PERIODS = (7, 30, 90)


@dataclass(frozen=True)
class Metric:
    key: str
    tr: str
    en: str
    sql: str  # params: app_id, start, end -> date, value, n
    agg: str  # avg | sum | weighted | last
    fmt: str  # num | pct | usd | rating
    min_n: int = 0  # below this total n the value is withheld
    higher_is_better: bool = True
    note_tr: str = ""
    note_en: str = ""
    lag: int = 0  # cohort metrics: the window moves back this many days (day N must have happened)


_DAILY = "SELECT date, {expr} AS value, {n} AS n FROM fct_app_daily WHERE app_id = ? AND date BETWEEN ? AND ? AND {expr} IS NOT NULL ORDER BY date"
_RET = (
    "SELECT cohort_date AS date, retention AS value, cohort_size AS n FROM fct_retention "
    "WHERE app_id = ? AND day_n = {d} AND is_complete AND cohort_date BETWEEN ? AND ? ORDER BY cohort_date"
)

METRICS: dict[str, Metric] = {
    m.key: m
    for m in (
        Metric("dau", "Günlük aktif (ort.)", "Daily active (avg)", _DAILY.format(expr="dau", n="dau"), "avg", "num",
               note_tr="Gün başına tekil aktif kullanıcı, dönem ortalaması.",
               note_en="Distinct active users per day, averaged over the period."),
        Metric("new_users", "Yeni kullanıcı", "New users", _DAILY.format(expr="new_users", n="new_users"), "sum", "num",
               note_tr="İlk kez açan kullanıcılar (GA4 first_open ya da SDK ilk oturum).",
               note_en="Users opening the app for the first time (GA4 first_open or the SDK's first session)."),
        Metric("installs", "Mağaza kurulumu", "Store installs", _DAILY.format(expr="store_installs", n="store_installs"), "sum", "num",
               note_tr="Play Console / App Store Connect raporlarındaki kurulumlar.",
               note_en="Installs from the Play Console / App Store Connect reports."),
        Metric("d1", "D1 elde tutma", "D1 retention", _RET.format(d=1), "weighted", "pct", min_n=50, lag=1,
               note_tr="Ertesi gün dönenlerin oranı, kohort büyüklüğüyle ağırlıklı; kohortlar dönemden 1 gün önce biten pencereden. 50 kurulumun altında gösterilmez.",
               note_en="Share returning the next day, weighted by cohort size; cohorts from the window ending 1 day before the period. Hidden under 50 installs."),
        Metric("d7", "D7 elde tutma", "D7 retention", _RET.format(d=7), "weighted", "pct", min_n=50, lag=7,
               note_tr="7. gün dönenlerin oranı; kohortlar dönemden 7 gün önce biten pencereden (7. günleri yaşanmış olsun diye). 50 kurulumun altında gösterilmez.",
               note_en="Share returning on day 7; cohorts from the window ending 7 days before the period (so their day 7 has happened). Hidden under 50 installs."),
        Metric("revenue", "Gelir", "Revenue", _DAILY.format(expr="revenue_usd", n="dau"), "sum", "usd",
               note_tr="Uygulama içi satın alma + reklam geliri (USD, sabit kurla).",
               note_en="In-app purchases plus ad revenue (USD at fixed rates)."),
        Metric("arpdau", "ARPDAU", "ARPDAU", _DAILY.format(expr="arpdau", n="dau"), "weighted", "usd", min_n=100,
               note_tr="Günlük aktif kullanıcı başına gelir; toplam gelir / toplam DAU. 100 kullanıcı-günün altında gösterilmez.",
               note_en="Revenue per daily active user: total revenue / total DAU. Hidden under 100 user-days."),
        Metric("crash_free", "Çökmesiz kullanıcı", "Crash-free users", _DAILY.format(expr="crash_free_users", n="dau"), "weighted", "pct", min_n=100,
               note_tr="Çökme yaşamayan aktif kullanıcıların oranı. 100 kullanıcı-günün altında gösterilmez.",
               note_en="Share of active users without a crash. Hidden under 100 user-days."),
        Metric("rating", "Mağaza puanı", "Store rating", _DAILY.format(expr="rating", n="1"), "last", "rating",
               note_tr="Mağazanın bildirdiği son ortalama puan.",
               note_en="The latest average rating the store reports."),
    )
}  # fmt: skip


def fmt(v: float | None, kind: str, lang: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        s = f"{v * 100:.1f}"
        return f"%{s.replace('.', ',')}" if lang == "tr" else f"{s}%"
    if kind == "rating":
        s = f"{v:.2f}"
        return s.replace(".", ",") if lang == "tr" else s
    digits = 2 if kind == "usd" else 0
    s = f"{v:,.{digits}f}"
    if lang == "tr":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${s}" if kind == "usd" else s


def aggregate(rows: list[dict[str, Any]], how: str) -> tuple[float | None, float]:
    """(value, n) for the period."""
    vals = [(float(r["value"]), float(r["n"] or 0)) for r in rows if r["value"] is not None]
    if not vals:
        return None, 0.0
    n = sum(w for _, w in vals)
    if how == "sum":
        return sum(v for v, _ in vals), n
    if how == "avg":
        return sum(v for v, _ in vals) / len(vals), n
    if how == "last":
        return vals[-1][0], n
    if n <= 0:
        return None, 0.0
    return sum(v * w for v, w in vals) / n, n


def window(end: dt.date, days: int) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    cur = (end - dt.timedelta(days=days - 1), end)
    prev = (cur[0] - dt.timedelta(days=days), cur[0] - dt.timedelta(days=1))
    return cur, prev


def spark(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = [(i * 64 / (len(values) - 1), 20 - (v - lo) / span * 18) for i, v in enumerate(values)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return f'<svg class="spark" viewBox="0 0 64 22" aria-hidden="true"><path d="{d}"/></svg>'


def area_chart(
    cur: list[tuple[Any, float]], prev: list[float], label: str, kind: str, lang: str
) -> str:
    """The period as an area line, the previous period as a dashed line on the same axis."""
    if len(cur) < 2:
        return ""
    w, h, pl, pr, pt, pb = 640.0, 160.0, 44.0, 8.0, 8.0, 20.0
    vals = [v for _, v in cur]
    top = max(vals + prev) or 1.0
    x = lambda i, n: pl + i * (w - pl - pr) / max(n - 1, 1)  # noqa: E731
    y = lambda v: pt + (1 - v / top) * (h - pt - pb)  # noqa: E731
    line = " ".join(f"{x(i, len(vals)):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"{x(0, len(vals)):.1f},{y(0):.1f} {line} {x(len(vals) - 1, len(vals)):.1f},{y(0):.1f}"
    grid = "".join(
        f'<line class="gridline" x1="{pl}" x2="{w - pr}" y1="{y(top * f):.1f}" y2="{y(top * f):.1f}"/>'
        f'<text x="{pl - 6}" y="{y(top * f) + 3:.1f}" text-anchor="end">{fmt(top * f, kind, lang)}</text>'
        for f in (0, 0.5, 1)
    )
    prev_line = ""
    if len(prev) >= 2:
        pts = " ".join(f"{x(i, len(prev)):.1f},{y(v):.1f}" for i, v in enumerate(prev))
        prev_line = f'<polyline class="prev" points="{pts}"/>'
    return (
        f'<svg class="area-chart" viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" role="img" aria-label="{label}">'
        f'{grid}<polygon class="area" points="{area}"/>{prev_line}<polyline class="line" points="{line}"/></svg>'
        f'<div class="axis"><span>{cur[0][0]}</span><span>{cur[-1][0]}</span></div>'
    )


def last_date(rows_fn: Any, con: Any, app_id: str) -> dt.date | None:
    r = rows_fn(con, "SELECT MAX(date) AS d FROM fct_app_daily WHERE app_id = ?", [app_id])
    return r[0]["d"] if r and r[0]["d"] else None


def compute(
    rows_fn: Any, con: Any, app_id: str, key: str, end: dt.date, days: int, lang: str
) -> dict[str, Any]:
    """One metric for the period and the one before: value, change, n, daily rows, SQL."""
    m = METRICS[key]
    (cs, ce), (ps, pe) = window(end - dt.timedelta(days=m.lag), days)
    cur_rows = rows_fn(con, m.sql, [app_id, cs, ce])
    prev_rows = rows_fn(con, m.sql, [app_id, ps, pe])
    v, n = aggregate(cur_rows, m.agg)
    pv, pn = aggregate(prev_rows, m.agg)
    enough = n >= m.min_n
    prev_enough = pn >= m.min_n
    change = None
    if v is not None and pv and enough and prev_enough and m.agg != "last":
        change = (v - pv) / abs(pv)
    return {
        "key": key,
        "label": m.tr if lang == "tr" else m.en,
        "note": m.note_tr if lang == "tr" else m.note_en,
        "value": fmt(v if enough else None, m.fmt, lang),
        "raw": v,
        "enough": enough or v is None,
        "has_data": v is not None,
        "n": int(n),
        "min_n": m.min_n,
        "change": change,
        "change_text": None
        if change is None
        else (("+" if change >= 0 else "−") + fmt(abs(change), "pct", lang)),
        "good": None if change is None else ((change >= 0) == m.higher_is_better),
        "prev_value": fmt(pv if prev_enough else None, m.fmt, lang),
        "spark": spark([float(r["value"]) for r in cur_rows if r["value"] is not None]),
        "rows": cur_rows,
        "prev_rows": prev_rows,
        "sql": m.sql,
        "params": [app_id, cs.isoformat(), ce.isoformat()],
        "period": (cs, ce),
        "prev_period": (ps, pe),
        "fmt": m.fmt,
    }
