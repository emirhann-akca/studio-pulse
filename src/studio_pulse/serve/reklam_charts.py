"""Charts of the ad centre: HTML + inline SVG, in the page's language, with change markers.

The rules of dashboard/charts.py, which these follow and whose CSS (`.dash` scope) they reuse:

* one y-axis per chart, never two scales on one plot: spend and installs are two charts side by side;
* colour follows the entity (each network keeps its slot --s1..--s8 everywhere), a legend for two or more
  series and direct end labels when they fit; a single series has no legend, its title names it;
* thin 2 px lines, hairline grid, a hover target per day that lists every series (ui.js shows `data-tip`);
* every chart has a table view ("Veri tablosu"), so no value needs the tooltip;
* change markers: a numbered hairline on the day a change was recorded (creative-forge's audit log or the demo's
  change file); the same numbers head the change list under the charts, so a marker is never only a tooltip.

Labels that come from data (campaign and ad names) are escaped here; templates insert the result with |safe.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from typing import Any

MONTHS = {
    "tr": ("Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"),
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}
TABLE = {"tr": "Veri tablosu", "en": "Data table"}
DATE_COL = {"tr": "Tarih", "en": "Date"}
EMPTY = {"tr": "Bu dönem için veri yok.", "en": "No data for this period."}


def group(int_part: str, sep: str) -> str:
    out = ""
    while len(int_part) > 3:
        out = sep + int_part[-3:] + out
        int_part = int_part[:-3]
    return int_part + out


def num(v: float | None, lang: str, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    s = f"{abs(float(v)):.{digits}f}"
    ip, _, frac = s.partition(".")
    th, dec = (".", ",") if lang == "tr" else (",", ".")
    out = group(ip, th) + (dec + frac if frac else "")
    return ("-" if float(v) < 0 and out.strip("0.,") else "") + out


def fmt(v: float | None, kind: str, lang: str) -> str:
    """usd ($1.234,56), usd0, count, compact (12,3 B / 12.3K), pct (%12,3 / 12.3%), ipm (one decimal),
    roas (a ratio shown as a percentage), days, ratio."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    x = float(v)
    if kind == "usd":
        return ("-" if x < 0 else "") + "$" + num(abs(x), lang, 2)
    if kind == "usd0":
        return (
            ("-" if x < 0 else "")
            + "$"
            + num(abs(x), lang, 0 if abs(x) >= 100 else 2 if abs(x) < 10 else 1)
        )
    if kind == "count":
        return num(round(x), lang)
    if kind == "compact":
        a = abs(x)
        if a >= 1e6:
            return num(x / 1e6, lang, 1) + (" Mn" if lang == "tr" else "M")
        if a >= 1e4:
            return num(x / 1e3, lang, 1) + (" B" if lang == "tr" else "K")
        return num(round(x), lang)
    if kind in ("pct", "roas"):
        digits = 0 if kind == "roas" and abs(x) >= 1 else 1
        s = num(x * 100, lang, digits)
        return f"%{s}" if lang == "tr" else f"{s}%"
    if kind == "pct0":
        s = num(x * 100, lang, 0)
        return f"%{s}" if lang == "tr" else f"{s}%"
    if kind == "pct2":
        s = num(x * 100, lang, 2)
        return f"%{s}" if lang == "tr" else f"{s}%"
    if kind == "ipm":
        return num(x, lang, 1)
    if kind == "days":
        return num(round(x), lang)
    return num(x, lang, 2)


def signed_pct(change: float | None, lang: str) -> str:
    if change is None:
        return "—"
    s = num(abs(change) * 100, lang, 1)
    sign = "+" if change > 0 else ("−" if change < 0 else "±")
    return f"{sign}%{s}" if lang == "tr" else f"{sign}{s}%"


def day(d: dt.date | str, lang: str, year: bool = False) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    m = MONTHS.get(lang, MONTHS["en"])[d.month - 1]
    base = f"{d.day} {m}" if lang == "tr" else f"{m} {d.day}"
    return f"{base} {d.year}" if year else base


@dataclass
class Series:
    name: str
    values: Sequence[float | None]
    slot: int = 1
    dashed: bool = False


@dataclass
class Marker:
    date: dt.date
    n: int
    text: str


@dataclass
class Chart:
    title: str
    sub: str = ""
    kind: str = "count"
    height: int = 170
    markers: list[Marker] = field(default_factory=list)
    end_labels: bool = True  # off when series end on different x (creatives by week live)


def nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v < hi + step * 0.999:
        ticks.append(round(v, 10))
        v += step
    if ticks[-1] < hi:
        ticks.append(ticks[-1] + step)
    return ticks


def _uid(title: str) -> str:
    return "rc-" + "".join(ch for ch in title.lower() if ch.isalnum())[:32]


def _legend(series: Sequence[Series]) -> str:
    if len(series) < 2:
        return ""
    keys = "".join(
        f'<span class="key"><i class="sw b{s.slot}"></i>{escape(s.name)}</span>' for s in series
    )
    return f'<div class="legend" aria-hidden="true">{keys}</div>'


def _table(lang: str, header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    left = ' class="l"'
    th = "".join(f"<th{left if i == 0 else ''}>{escape(h)}</th>" for i, h in enumerate(header))
    body = "".join(
        "<tr>"
        + "".join(f"<td{left if i == 0 else ''}>{escape(c)}</td>" for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    return (f'<details class="data"><summary>{TABLE.get(lang, TABLE["en"])}</summary><div class="tbl-wrap"><table>'
            f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div></details>")  # fmt: skip


def _figure(c: Chart, inner: str) -> str:
    sub = f"<p>{escape(c.sub)}</p>" if c.sub else ""
    return f'<figure class="chart rk" id="{_uid(c.title)}"><figcaption><h3>{escape(c.title)}</h3>{sub}</figcaption>{inner}</figure>'


def empty(c: Chart, lang: str, message: str | None = None) -> str:
    return _figure(
        c, f'<p class="note rk-empty">{escape(message or EMPTY.get(lang, EMPTY["en"]))}</p>'
    )


def line(c: Chart, x: Sequence[dt.date], series: Sequence[Series], lang: str, *, zero: bool = True,
         x_label: str | None = None, x_fmt: Any = None) -> str:  # fmt: skip
    """A line chart on one axis; `x_fmt` formats an x value (default: a short date)."""
    series = [s for s in series if any(v is not None for v in s.values)]
    vals = [float(v) for s in series for v in s.values if v is not None]
    if not x or not vals:
        return empty(c, lang)
    xf = x_fmt or (lambda d: day(d, lang))
    lo = min(0.0, min(vals)) if zero else min(vals)
    hi = max(vals)
    if not zero:
        pad = (hi - lo) * 0.1 or abs(hi) * 0.1 or 1.0
        lo, hi = max(0.0, lo - pad) if min(vals) >= 0 else lo - pad, hi + pad
    ticks = nice_ticks(lo, hi)
    lo, hi = ticks[0], ticks[-1]
    n = len(x)

    def px(i: int) -> float:
        return 500.0 if n == 1 else i * 1000.0 / (n - 1)

    def py(v: float) -> float:
        return 100.0 - (v - lo) / (hi - lo) * 100.0

    grid = "".join(f'<line x1="0" x2="1000" y1="{py(t):.3f}" y2="{py(t):.3f}" vector-effect="non-scaling-stroke"/>'
                   for t in ticks[1:])  # fmt: skip
    paths, dots, ends = [], [], []
    for s in series:
        d, pen, last = [], False, None
        for i, v in enumerate(s.values):
            if v is None:
                pen = False
                continue
            d.append(f"{'L' if pen else 'M'}{px(i):.2f},{py(float(v)):.3f}")
            pen, last = True, (i, float(v))
        if d:
            paths.append(f'<path class="ln c{s.slot}{" dashed" if s.dashed else ""}" d="{" ".join(d)}" '
                         'vector-effect="non-scaling-stroke"/>')  # fmt: skip
        if last:
            xi, yv = px(last[0]), py(last[1])
            seg = f'x1="{xi:.2f}" x2="{xi:.2f}" y1="{yv:.3f}" y2="{yv:.3f}" vector-effect="non-scaling-stroke"'
            dots.append(f'<line class="dotring" {seg}/><line class="dot c{s.slot}" {seg}/>')
            ends.append((s, yv, last[1]))
    step = 1000.0 / n
    hits = []
    for i in range(n):
        lines = [xf(x[i])]
        lines += [
            f"{fmt(s.values[i] if i < len(s.values) else None, c.kind, lang)}  {s.name}"
            for s in series
        ]
        tip = escape("\n".join(lines), quote=True)
        left = max(0.0, px(i) - step / 2)
        hits.append(
            f'<rect x="{left:.2f}" y="0" width="{step:.2f}" height="100" data-tip="{tip}"/>'
        )
    svg = ('<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">'
           f'<g class="grid">{grid}</g>{"".join(paths)}{"".join(dots)}<g class="hits">{"".join(hits)}</g></svg>')  # fmt: skip
    marks = ""
    index = {d: i for i, d in enumerate(x)} if x and isinstance(x[0], dt.date) else {}
    by_day: dict[dt.date, list[Marker]] = {}
    for m in c.markers:  # changes on the same day share one badge ("1–3"), their texts one tooltip
        if m.date in index:
            by_day.setdefault(m.date, []).append(m)
    for d, ms in by_day.items():
        left = px(index[d]) / 10
        nums = [m.n for m in ms]
        label = str(nums[0]) if len(nums) == 1 else f"{min(nums)}–{max(nums)}"
        text = escape("\n".join(m.text for m in ms), quote=True)
        marks += (
            f'<span class="mk" style="left:{left:.2f}%"><button type="button" class="mk-n{" wide" if len(ms) > 1 else ""}" '
            f'data-tip="{text}" aria-label="{text}">{label}</button></span>'
        )
    yaxis = "".join(f'<span style="bottom:{100 - py(t):.2f}%">{escape(fmt(t, _axis_kind(c.kind), lang))}</span>'
                    for t in ticks)  # fmt: skip
    m_ = min(n, 5)
    idx = sorted({round(i * (n - 1) / (m_ - 1)) for i in range(m_)}) if m_ > 1 else [0]
    xl = []
    for i in idx:
        cls = "first" if i == 0 and n > 1 else ("last" if i == n - 1 and n > 1 else "")
        xl.append(f'<span class="{cls}" style="left:{px(i) / 10:.2f}%">{escape(xf(x[i]))}</span>')
    end_texts: list[tuple[float, str]] = []
    if not c.end_labels:
        pass
    elif 2 <= len(series) <= 4 and _apart([y for _, y, _ in ends]):
        end_texts = [(y, f"{s.name} {fmt(v, c.kind, lang)}") for s, y, v in ends]
    elif len(series) == 1 and ends:
        end_texts = [(ends[0][1], fmt(ends[0][2], c.kind, lang))]
    endlabels = "".join(f'<span style="top:{y:.2f}%">{escape(t)}</span>' for y, t in end_texts)
    endw = (max(len(t) for _, t in end_texts) * 6.4 + 10) if end_texts else 0
    plot = (f'<div class="plot" style="--h:{c.height}px;--endw:{min(endw, 150):.0f}px"><div class="yaxis">{yaxis}</div>'
            f'<div class="area">{svg}<div class="marks">{marks}</div></div><div class="endlabels">{endlabels}</div>'
            f'<div class="xaxis">{"".join(xl)}</div></div>')  # fmt: skip
    rows = [[xf(x[i])] + [fmt(s.values[i] if i < len(s.values) else None, c.kind, lang) for s in series]
            for i in range(n)]  # fmt: skip
    label = f"{c.title}: {len(series)} · {n}"
    return _figure(c, _legend(series) + f'<div role="img" aria-label="{escape(label, quote=True)}">{plot}</div>'
                   + _table(lang, [x_label or DATE_COL.get(lang, "Date")] + [s.name for s in series], rows))  # fmt: skip


def _axis_kind(kind: str) -> str:
    return {"usd": "usd0", "count": "compact", "roas": "pct0", "pct2": "pct"}.get(kind, kind)


def _apart(ys: list[float], gap: float = 11.0) -> bool:
    ys = sorted(ys)
    return all(b - a >= gap for a, b in zip(ys, ys[1:], strict=False))


def hbars(c: Chart, rows: Sequence[dict[str, Any]], lang: str, header: Sequence[str]) -> str:
    """Horizontal bars, one per row: {"name", "value", "slot", "cells": [extra column texts]}; the value is
    printed at the bar's end, the extra columns stay in text (no second scale)."""
    if not rows:
        return empty(c, lang)
    top = max(float(r["value"] or 0) for r in rows) or 1.0
    items = []
    for r in rows:
        w = max(0.0, float(r["value"] or 0)) / top * 100
        tip = escape(
            " · ".join([r["name"], fmt(r["value"], c.kind, lang), *r.get("cells", [])]), quote=True
        )
        cells = "".join(f'<span class="hb-c">{escape(t)}</span>' for t in r.get("cells", []))
        items.append(
            f'<div class="hb" tabindex="0" data-tip="{tip}"><span class="hb-n">{escape(r["name"])}</span>'
            f'<span class="hb-t"><i class="b{r.get("slot", 1)}" style="width:{w:.2f}%"></i>'
            f"<b>{escape(fmt(r['value'], c.kind, lang))}</b></span>{cells}</div>"
        )
    rows_t = [[r["name"], fmt(r["value"], c.kind, lang), *r.get("cells", [])] for r in rows]
    return _figure(
        c, '<div class="hbars">' + "".join(items) + "</div>" + _table(lang, header, rows_t)
    )


def spark(values: Sequence[float], cls: str = "spark") -> str:
    vals = [float(v or 0) for v in values]
    if len(vals) < 2 or not any(vals):
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = [(i * 64 / (len(vals) - 1), 20 - (v - lo) / span * 18) for i, v in enumerate(vals)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return f'<svg class="{cls}" viewBox="0 0 64 22" aria-hidden="true"><path d="{d}"/></svg>'
