"""Inline CSS and JS for the static dashboard (no external requests).

The colour, type and theme tokens are the studio-ui design system's (serve/static/ui.css,
shared with the Studio and Creative Forge), so the dashboard reads as the same product;
the rules below them are the dashboard's own charts and tables.
"""

from importlib import resources

_UI = resources.files("studio_pulse.serve").joinpath("static", "ui.css").read_text(encoding="utf-8")
_TOKENS = (
    _UI[_UI.index(":root {") : _UI.index("/* ---- base ---- */")]
    + ":root { --banner-bg: var(--warn-soft); --banner-ink: var(--text); --banner-border: var(--warn); }\n"
)

CSS = (
    _TOKENS
    + r"""* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 var(--font);
}
a { color: var(--link); }
.wrap { max-width: 1240px; margin: 0 auto; padding: 0 16px 48px; }
header.top { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 16px 0 8px; }
header.top h1 { font-size: 20px; margin: 0; font-weight: 650; }
header.top .meta { color: var(--ink-2); font-size: 13px; }
header.top .spacer { flex: 1; }
nav.apps { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 12px; }
nav.apps a { padding: 4px 10px; border-radius: 999px; border: 1px solid var(--ring); text-decoration: none; color: var(--ink); background: var(--surface); font-size: 13px; }
nav.apps a[aria-current="page"] { border-color: var(--s1); box-shadow: inset 0 0 0 1px var(--s1); }
button.theme { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--ring); background: var(--surface); color: var(--ink); cursor: pointer; }
.banner { margin: 8px 0 16px; padding: 10px 14px; border-radius: 8px; background: var(--banner-bg); color: var(--banner-ink); border: 1px solid var(--banner-border); font-weight: 600; }
.banner span { font-weight: 400; }
h2 { font-size: 16px; margin: 28px 0 10px; font-weight: 650; }
.note { color: var(--ink-2); font-size: 13px; margin: -4px 0 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 10px 12px; min-width: 0; }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: 22px; font-weight: 650; margin-top: 2px; }
.tile .delta { font-size: 12px; color: var(--ink-2); }
.tile .delta.good { color: var(--good-ink); }
.tile .delta.bad { color: var(--critical); }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 460px), 1fr)); gap: 12px; }
figure.chart { margin: 0; background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 12px 12px 8px; min-width: 0; }
figure.chart figcaption h3 { font-size: 14px; margin: 0; font-weight: 600; }
figure.chart figcaption p { margin: 2px 0 0; color: var(--ink-2); font-size: 12px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 8px 0 4px; font-size: 12px; color: var(--ink-2); }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .sw { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.legend .sw.box { height: 10px; width: 10px; border-radius: 3px; }
.legend .ramp { display: inline-block; width: 72px; height: 10px; border-radius: 3px; background: linear-gradient(90deg, color-mix(in oklab, var(--s1) 8%, var(--surface)), var(--s1)); }
.plot { display: grid; grid-template-columns: 52px minmax(0, 1fr) var(--endw, 0px); grid-template-rows: var(--h, 200px) 22px; margin-top: 6px; }
.yaxis { position: relative; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.yaxis span { position: absolute; right: 6px; transform: translateY(50%); white-space: nowrap; }
.area { position: relative; border-bottom: 1px solid var(--axis); }
.area svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.xaxis { grid-column: 2; position: relative; font-size: 11px; color: var(--muted); }
.xaxis span { position: absolute; top: 4px; transform: translateX(-50%); white-space: nowrap; }
.xaxis span.first { transform: none; }
.xaxis span.last { transform: translateX(-100%); }
.endlabels { position: relative; min-width: 0; font-size: 11px; color: var(--ink-2); }
.endlabels span { position: absolute; left: 6px; transform: translateY(-50%); white-space: nowrap; }
.grid line { stroke: var(--grid); stroke-width: 1px; }
.ln { fill: none; stroke-width: 2px; stroke-linejoin: round; stroke-linecap: round; }
.ln.dashed { stroke-dasharray: 5 4; }
.dot { stroke-width: 8px; stroke-linecap: round; }
.dotring { stroke: var(--surface); stroke-width: 12px; stroke-linecap: round; }
.hits rect { fill: transparent; }
.hits rect:hover, .hits rect:focus { fill: var(--hover); outline: none; }
.c1 { stroke: var(--s1); } .c2 { stroke: var(--s2); } .c3 { stroke: var(--s3); } .c4 { stroke: var(--s4); }
.c5 { stroke: var(--s5); } .c6 { stroke: var(--s6); } .c7 { stroke: var(--s7); } .c8 { stroke: var(--s8); }
.b1 { background: var(--s1); } .b2 { background: var(--s2); } .b3 { background: var(--s3); } .b4 { background: var(--s4); }
.b5 { background: var(--s5); } .b6 { background: var(--s6); } .b7 { background: var(--s7); } .b8 { background: var(--s8); }
.bars { position: absolute; inset: 0; display: flex; align-items: flex-end; gap: 2px; padding: 0 2px; }
.bars .cat { flex: 1 1 0; min-width: 0; height: 100%; display: flex; align-items: flex-end; justify-content: center; gap: 2px; }
.bars .cat:hover, .bars .cat:focus { background: var(--hover); outline: none; }
.bars .bar { width: min(24px, 100%); border-radius: 4px 4px 0 0; min-height: 1px; flex: 0 1 24px; }
.xaxis.cats span { transform: translateX(-50%); }
.bench { display: grid; grid-template-columns: 34px minmax(0, 1fr) 104px; align-items: center; gap: 10px; margin: 16px 0 24px; }
.bench .bl { font-weight: 600; font-size: 13px; }
.bench .track { position: relative; height: 14px; border-radius: 4px; }
.bench .zone { position: absolute; top: 0; bottom: 0; background: color-mix(in oklab, var(--s1) 6%, var(--surface)); }
.bench .zone.z1 { background: color-mix(in oklab, var(--s1) 16%, var(--surface)); }
.bench .zone.z2 { background: color-mix(in oklab, var(--s1) 28%, var(--surface)); }
.bench .zone.z3 { background: color-mix(in oklab, var(--s1) 42%, var(--surface)); }
.bench .zone:first-child { border-radius: 4px 0 0 4px; }
.bench .zone:last-of-type { border-radius: 0 4px 4px 0; }
.bench .cut { position: absolute; top: -3px; bottom: -3px; }
.bench .cut i { position: absolute; left: -1px; top: 0; bottom: 0; width: 2px; background: var(--axis); }
.bench .cut b { position: absolute; top: 21px; transform: translateX(-50%); font-size: 10px; font-weight: 400; color: var(--muted); white-space: nowrap; }
.bench .mark { position: absolute; top: -5px; width: 4px; height: 24px; margin-left: -2px; border-radius: 2px; background: var(--ink); box-shadow: 0 0 0 2px var(--surface); }
.bench .bv { font-variant-numeric: tabular-nums; font-weight: 600; }
.bench .bv em { display: block; font-style: normal; font-weight: 400; font-size: 12px; color: var(--ink-2); }
details.data { margin-top: 6px; }
details.data summary { cursor: pointer; color: var(--ink-2); font-size: 12px; }
.tbl-wrap { overflow-x: auto; max-height: 320px; overflow-y: auto; margin-top: 6px; }
table { border-collapse: collapse; font-size: 12px; width: 100%; }
th, td { padding: 4px 8px; text-align: right; border-bottom: 1px solid var(--grid); white-space: nowrap; font-variant-numeric: tabular-nums; }
th.l, td.l { text-align: left; }
th { color: var(--ink-2); font-weight: 600; position: sticky; top: 0; background: var(--surface); }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 8px 12px; min-width: 0; }
.heat td { text-align: center; min-width: 44px; padding: 4px 6px; }
.heat td.na { color: var(--muted); }
.alerts { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
.alerts li { background: var(--surface); border: 1px solid var(--ring); border-left: 4px solid var(--warning); border-radius: 8px; padding: 8px 12px; }
.alerts li.critical { border-left-color: var(--critical); }
.alerts .sev { font-weight: 650; margin-right: 6px; }
.alerts .ok { border-left-color: var(--good); }
.status { display: inline-flex; align-items: center; gap: 4px; }
.status i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.status.fresh i { background: var(--good); } .status.stale i { background: var(--warning); } .status.old i { background: var(--critical); }
#tip { position: fixed; z-index: 10; pointer-events: none; background: var(--ink); color: var(--surface); font-size: 12px; padding: 6px 8px; border-radius: 6px; max-width: 280px; white-space: pre-line; display: none; box-shadow: 0 2px 8px rgba(0,0,0,.2); }
footer { margin-top: 40px; color: var(--muted); font-size: 12px; }
@media (max-width: 520px) {
  .plot { grid-template-columns: 44px minmax(0, 1fr) 0px; }
  .tile .value { font-size: 19px; }
  .endlabels { display: none; }
}
@media print { button.theme, #tip { display: none; } }
"""
)

JS = r"""
(function () {
  var root = document.documentElement;
  try { var saved = localStorage.getItem('ui-theme'); if (saved) root.setAttribute('data-theme', saved); } catch (e) {}
  var btn = document.querySelector('button.theme');
  function label() {
    var dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') && window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
    if (btn) btn.textContent = dark ? 'Light mode' : 'Dark mode';
    return dark;
  }
  if (btn) {
    label();
    btn.addEventListener('click', function () {
      var next = label() ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('ui-theme', next); } catch (e) {}
      label();
    });
  }
  var tip = document.getElementById('tip');
  function show(el, x, y) {
    tip.textContent = el.getAttribute('data-tip');
    tip.style.display = 'block';
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, x + 12), window.innerWidth - w - 8);
    var top = y - h - 12; if (top < 8) top = y + 16;
    tip.style.left = left + 'px'; tip.style.top = top + 'px';
  }
  document.addEventListener('pointermove', function (ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip]');
    if (el) show(el, ev.clientX, ev.clientY); else tip.style.display = 'none';
  });
  document.addEventListener('focusin', function (ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip]');
    if (el) { var r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); }
  });
  document.addEventListener('focusout', function () { tip.style.display = 'none'; });
})();
"""


def _scoped_chart_css() -> str:
    """The chart and table rules above, every selector prefixed with .dash, so server
    pages in the studio-ui shell can embed dashboard charts without these generic rules
    (table, th, .card, .dot) leaking into the shell."""
    start = CSS.index(".grid2 {")
    stop = CSS.index(".alerts {")
    out = []
    for line in CSS[start:stop].splitlines():
        if "{" not in line or line.startswith(".card {"):
            continue
        sel, rest = line.split("{", 1)
        sel = ", ".join(".dash " + part.strip() for part in sel.split(","))
        out.append(f"{sel} {{{rest}")
    return "\n".join(out)


CHART_CSS = _scoped_chart_css()
