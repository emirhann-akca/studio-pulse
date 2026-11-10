"""Dependency-free chart components rendered to HTML + inline SVG.

Design rules (kept deliberately small):
  * text (axes, labels, legends) is HTML so it stays readable at any width;
    marks live in an SVG stretched to the plot area with non-scaling strokes;
  * a legend whenever there are two or more series, direct end labels for up
    to four series when they do not collide;
  * one y-axis per chart, categorical colours assigned in a fixed order;
  * every chart carries a "Data table" toggle with the exact numbers and a
    hover tooltip per x position / bar.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

Num = float | int | None


# -- number formatting ---------------------------------------------------------

def fmt_int(v: Num) -> str:
    return "–" if v is None else f"{v:,.0f}"


def fmt_compact(v: Num) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}K"
    if a >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.1f}"


def fmt_pct(v: Num, digits: int = 1) -> str:
    return "–" if v is None else f"{v * 100:.{digits}f}%"


def fmt_pct2(v: Num) -> str:
    return fmt_pct(v, 2)


def fmt_usd(v: Num) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e4:
        return f"${v / 1e3:.1f}K"
    if a >= 100:
        return f"${v:,.0f}"
    if a >= 1:
        return f"${v:,.2f}"
    if a == 0:
        return "$0"
    return f"${v:.3f}"


def fmt_ms(v: Num) -> str:
    return "–" if v is None else (f"{v:,.0f} ms" if abs(v) >= 100 else f"{v:.1f} ms")


def fmt_mb(v: Num) -> str:
    return "–" if v is None else f"{v:,.0f} MB"


def fmt_num(v: Num) -> str:
    if v is None:
        return "–"
    return f"{v:,.2f}" if abs(v) < 10 else f"{v:,.0f}"


def fmt_date(d: dt.date | str) -> str:
    if isinstance(d, str):
        return d
    return f"{d:%b} {d.day}" if hasattr(d, "strftime") else str(d)


# -- scales --------------------------------------------------------------------

def nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    if hi == lo:
        hi = lo + (abs(lo) or 1.0)
    span = hi - lo
    raw = span / target
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


@dataclass
class Series:
    name: str
    values: Sequence[Num]
    dashed: bool = False
    slot: int | None = None  # 1..8 categorical slot; defaults to position


def _uid(prefix: str, title: str) -> str:
    return prefix + "-" + "".join(ch for ch in title.lower() if ch.isalnum())[:40]


def _legend(series: Sequence[Series], box: bool = False) -> str:
    if len(series) < 2:
        return ""
    keys = []
    for i, s in enumerate(series):
        slot = s.slot or (i + 1)
        style = "sw box" if box else "sw"
        dash = ' style="background-image:linear-gradient(90deg,transparent 50%,var(--surface) 50%);background-size:6px 3px"' if s.dashed else ""
        keys.append(f'<span class="key"><i class="{style} b{slot}"{dash}></i>{escape(s.name)}</span>')
    return '<div class="legend" aria-hidden="true">' + "".join(keys) + "</div>"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    th = "".join(f"<th>{escape(h)}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<details class="data"><summary>Data table</summary><div class="tbl-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div></details>'


def _figure(title: str, subtitle: str, inner: str, fid: str) -> str:
    sub = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    return f'<figure class="chart" id="{fid}"><figcaption><h3>{escape(title)}</h3>{sub}</figcaption>{inner}</figure>'


def empty_chart(title: str, message: str = "No data for this chart yet.", subtitle: str = "") -> str:
    return _figure(title, subtitle, f'<p class="note" style="margin:12px 0">{escape(message)}</p>', _uid("c", title))


def _label_positions(ys: list[tuple[int, float]], min_gap: float = 11.0) -> bool:
    ordered = sorted(y for _, y in ys)
    return all(b - a >= min_gap for a, b in zip(ordered, ordered[1:], strict=False))


def line_chart(
    title: str,
    x: Sequence[dt.date | str],
    series: Sequence[Series],
    fmt: Callable[[Num], str] = fmt_num,
    subtitle: str = "",
    height: int = 200,
    zero: bool = True,
    x_fmt: Callable[[dt.date | str], str] = fmt_date,
    x_name: str = "Date",
) -> str:
    vals = [v for s in series for v in s.values if v is not None]
    if not x or not vals:
        return empty_chart(title, subtitle=subtitle)
    lo, hi = (min(0.0, min(vals)) if zero else min(vals)), max(vals)
    if not zero and hi > lo:
        pad = (hi - lo) * 0.08
        floor = 0.0 if min(vals) >= 0 else -math.inf
        lo, hi = max(floor, lo - pad), hi + pad
    ticks = nice_ticks(lo, hi)
    lo, hi = ticks[0], ticks[-1]
    n = len(x)

    def px(i: int) -> float:
        return 500.0 if n == 1 else i * 1000.0 / (n - 1)

    def py(v: float) -> float:
        return 100.0 - (v - lo) / (hi - lo) * 100.0

    grid = "".join(
        f'<line x1="0" x2="1000" y1="{py(t):.3f}" y2="{py(t):.3f}" vector-effect="non-scaling-stroke"/>'
        for t in ticks[1:]
    )
    paths, dots, ends = [], [], []
    for si, s in enumerate(series):
        slot = s.slot or (si + 1)
        d, pen = [], False
        last = None
        for i, v in enumerate(s.values):
            if v is None:
                pen = False
                continue
            d.append(f"{'L' if pen else 'M'}{px(i):.2f},{py(v):.3f}")
            pen = True
            last = (i, v)
        if d:
            cls = f"ln c{slot}" + (" dashed" if s.dashed else "")
            paths.append(f'<path class="{cls}" d="{" ".join(d)}" vector-effect="non-scaling-stroke"/>')
        if last:
            xi, yv = px(last[0]), py(last[1])
            seg = f'x1="{xi:.2f}" x2="{xi:.2f}" y1="{yv:.3f}" y2="{yv:.3f}" vector-effect="non-scaling-stroke"'
            dots.append(f'<line class="dotring" {seg}/><line class="dot c{slot}" {seg}/>')
            ends.append((si, yv, last[1]))
    step = 1000.0 / n
    hits = []
    for i in range(n):
        lines = [x_fmt(x[i]) if not isinstance(x[i], str) else x[i]]
        for s in series:
            v = s.values[i] if i < len(s.values) else None
            lines.append(f"{s.name}: {fmt(v)}")
        tip = escape("\n".join(lines), quote=True)
        left = max(0.0, px(i) - step / 2)
        hits.append(f'<rect x="{left:.2f}" y="0" width="{step:.2f}" height="100" data-tip="{tip}"/>')
    svg = (
        '<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">'
        f'<g class="grid">{grid}</g>{"".join(paths)}{"".join(dots)}<g class="hits">{"".join(hits)}</g></svg>'
    )
    yaxis = "".join(f'<span style="bottom:{100 - py(t):.2f}%">{escape(fmt(t))}</span>' for t in ticks)
    m = min(n, 5)
    idx = sorted({round(i * (n - 1) / (m - 1)) for i in range(m)}) if m > 1 else [0]
    xlabels = []
    for i in idx:
        cls = "first" if i == 0 and n > 1 else ("last" if i == n - 1 and n > 1 else "")
        lab = x_fmt(x[i]) if not isinstance(x[i], str) else x[i]
        xlabels.append(f'<span class="{cls}" style="left:{px(i) / 10:.2f}%">{escape(lab)}</span>')
    endlabels, end_texts = "", []
    if 2 <= len(series) <= 4 and _label_positions([(si, y) for si, y, _ in ends]):
        end_texts = [(y, f"{series[si].name} {fmt(v)}") for si, y, v in ends]
    elif len(series) == 1 and ends:
        end_texts = [(ends[0][1], fmt(ends[0][2]))]
    endlabels = "".join(f'<span style="top:{y:.2f}%">{escape(t)}</span>' for y, t in end_texts)
    endw = (max(len(t) for _, t in end_texts) * 6.4 + 10) if end_texts else 0
    plot = (
        f'<div class="plot" style="--h:{height}px;--endw:{endw:.0f}px"><div class="yaxis">{yaxis}</div>'
        f'<div class="area">{svg}</div><div class="endlabels">{endlabels}</div>'
        f'<div class="xaxis">{"".join(xlabels)}</div></div>'
    )
    rows = [
        [x_fmt(x[i]) if not isinstance(x[i], str) else x[i]] + [fmt(s.values[i] if i < len(s.values) else None) for s in series]
        for i in range(n)
    ]
    table = _table([x_name] + [s.name for s in series], rows)
    label = f"{title}: line chart, {len(series)} series, {n} points"
    return _figure(title, subtitle, _legend(series) + f'<div role="img" aria-label="{escape(label, quote=True)}">{plot}</div>' + table, _uid("c", title))


def bar_chart(
    title: str,
    categories: Sequence[str],
    series: Sequence[Series],
    fmt: Callable[[Num], str] = fmt_num,
    subtitle: str = "",
    height: int = 180,
    x_name: str = "Category",
    max_labels: int = 12,
) -> str:
    vals = [v for s in series for v in s.values if v is not None]
    if not categories or not vals:
        return empty_chart(title, subtitle=subtitle)
    ticks = nice_ticks(min(0.0, min(vals)), max(vals))
    lo, hi = ticks[0], ticks[-1]
    cats = []
    for ci, c in enumerate(categories):
        bars, tip = [], [c]
        for si, s in enumerate(series):
            v = s.values[ci] if ci < len(s.values) else None
            tip.append(f"{s.name}: {fmt(v)}")
            if v is None:
                continue
            h = max(0.0, (v - lo) / (hi - lo) * 100)
            bars.append(f'<div class="bar b{s.slot or si + 1}" style="height:{h:.2f}%"></div>')
        t = escape("\n".join(tip), quote=True)
        cats.append(f'<div class="cat" data-tip="{t}">{"".join(bars)}</div>')
    grid = "".join(
        f'<line x1="0" x2="1000" y1="{100 - (t - lo) / (hi - lo) * 100:.3f}" y2="{100 - (t - lo) / (hi - lo) * 100:.3f}" vector-effect="non-scaling-stroke"/>'
        for t in ticks[1:]
    )
    svg = f'<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true"><g class="grid">{grid}</g></svg>'
    yaxis = "".join(f'<span style="bottom:{(t - lo) / (hi - lo) * 100:.2f}%">{escape(fmt(t))}</span>' for t in ticks)
    n = len(categories)
    k = max(1, math.ceil(n / max_labels))
    xl = "".join(
        f'<span style="left:{(i + 0.5) * 100 / n:.2f}%">{escape(categories[i])}</span>' for i in range(0, n, k)
    )
    plot = (
        f'<div class="plot" style="--h:{height}px"><div class="yaxis">{yaxis}</div>'
        f'<div class="area">{svg}<div class="bars">{"".join(cats)}</div></div><div class="endlabels"></div>'
        f'<div class="xaxis cats">{xl}</div></div>'
    )
    rows = [[categories[i]] + [fmt(s.values[i] if i < len(s.values) else None) for s in series] for i in range(n)]
    label = f"{title}: bar chart, {len(series)} series, {n} categories"
    return _figure(
        title, subtitle,
        _legend(series, box=True) + f'<div role="img" aria-label="{escape(label, quote=True)}">{plot}</div>' + _table([x_name] + [s.name for s in series], rows),
        _uid("b", title),
    )


def heatmap(
    title: str,
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    values: Sequence[Sequence[Num]],
    fmt: Callable[[Num], str] = fmt_pct,
    subtitle: str = "",
    corner: str = "",
    scale_max: float | None = None,
) -> str:
    flat = [v for r in values for v in r if v is not None]
    if not flat:
        return empty_chart(title, subtitle=subtitle)
    top = scale_max or max(flat) or 1.0
    lab = escape(title, quote=True)
    head = f'<th class="l">{escape(corner)}</th>' + "".join(f"<th>{escape(c)}</th>" for c in col_labels)
    body = []
    for label, row in zip(row_labels, values, strict=False):
        cells = []
        for v in row:
            if v is None:
                cells.append('<td class="na">·</td>')
                continue
            share = max(0.0, min(1.0, v / top))
            pct = 8 + share * 82
            ink = "#ffffff" if pct > 52 else "var(--ink)"
            cells.append(
                f'<td style="background:color-mix(in oklab, var(--s1) {pct:.0f}%, var(--surface));color:{ink}">{escape(fmt(v))}</td>'
            )
        body.append(f'<tr><td class="l">{escape(label)}</td>{"".join(cells)}</tr>')
    table = (
        f'<div class="tbl-wrap" style="max-height:none"><table class="heat" aria-label="{lab}">'
        f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
    )
    return _figure(title, subtitle, table, _uid("h", title))


def tile(label: str, value: str, delta: str | None = None, tone: str | None = None, title: str | None = None) -> str:
    d = f'<div class="delta {tone or ""}">{escape(delta)}</div>' if delta else ""
    t = f' title="{escape(title, quote=True)}"' if title else ""
    return f'<div class="tile"{t}><div class="label">{escape(label)}</div><div class="value">{escape(value)}</div>{d}</div>'


def data_table(
    header: Sequence[str], rows: Sequence[Sequence[str]], raw_html_cols: Sequence[int] = (), left_cols: Sequence[int] = (0,)
) -> str:
    """A plain table; text columns (left_cols) are left-aligned, numbers right-aligned."""
    th = "".join(f'<th class="{"l" if i in left_cols else ""}">{escape(h)}</th>' for i, h in enumerate(header))
    out = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            cls = ' class="l"' if i in left_cols else ""
            cells.append(f"<td{cls}>{c if i in raw_html_cols else escape(str(c))}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="card"><div class="tbl-wrap" style="max-height:none"><table><thead><tr>{th}</tr></thead><tbody>{"".join(out)}</tbody></table></div></div>'
