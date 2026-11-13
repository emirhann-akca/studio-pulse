"""Alert messages in the page's language.

alerts.evaluate stores an English message with each alert (Slack, e-mail and the
console use it). The UI re-states an alert in Turkish from the stored numbers (value,
baseline, threshold) and its rule in alerts.yaml (condition, window), with the metric's
Turkish name from the brain's catalog; dimension filters and the z / n note are carried
over from the stored message. An alert whose rule is no longer in the rules file keeps
its stored text.
"""

from __future__ import annotations

import re
from typing import Any

from .. import alerts


def _fmt(v: float | None, unit: str, lang: str) -> str:
    if v is None:
        return "—"
    if unit == "pct":
        s = f"{v * 100:.2f}"
        return f"%{s.replace('.', ',')}" if lang == "tr" else f"{s}%"
    if unit == "usd":
        s = f"{v:,.3f}" if abs(v) < 10 else f"{v:,.0f}"
    elif unit == "ms":
        s = f"{v:.1f}"
    elif unit in ("mb", "count"):
        s = f"{v:,.0f}"
    else:
        s = f"{v:.2f}"
    if lang == "tr":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return {"usd": f"${s}", "ms": f"{s} ms", "mb": f"{s} MB"}.get(unit, s)


class AlertText:
    def __init__(self, rules_path: Any) -> None:
        self.rules: dict[str, Any] = {}
        try:
            if rules_path:
                self.rules = {r.name: r for r in alerts.load_rules(rules_path)}
        except (OSError, alerts.RuleError, ValueError):
            self.rules = {}

    def __call__(self, a: dict[str, Any], lang: str) -> str:
        msg = str(a.get("message") or "")
        rule = self.rules.get(a.get("rule"))
        spec = alerts.METRICS.get(a.get("metric") or "")
        if lang != "tr" or rule is None or spec is None or a.get("value") is None:
            return msg
        from ..brain.catalog import alert_baseline, load

        cat = load().metrics.get(a["metric"])
        name = cat.name.get("tr", a["metric"]) if cat else a["metric"]
        unit = spec.unit
        v, b, thr = a["value"], a.get("baseline"), a.get("threshold")
        f = lambda x: _fmt(x, unit, "tr")  # noqa: E731
        if rule.condition in ("above", "below"):
            side = "üstünde" if rule.condition == "above" else "altında"
            text = f"{name} {f(v)}: eşiğin ({f(thr)}) {side}"
        elif rule.condition == "forecast_band":
            text = (
                f"{name} {f(v)}: tahmin aralığının dışında (beklenen {f(b)}, haftalık mevsimsellik)"
            )
        elif b:
            change = (v - b) / abs(b)
            pooled = alert_baseline(a["metric"]) == "pooled"
            ref = "havuzlanmış oranına" if pooled else "ortalamasına"
            word = "düştü" if change < 0 else "arttı"
            pct = f"{abs(change) * 100:.1f}".replace(".", ",")
            text = (
                f"{name} {f(v)}: son {rule.baseline_days} günün {ref} ({f(b)}) göre %{pct} {word}"
                f" (sınır %{float(thr or 0):.0f})"
            )
        else:
            return msg
        z = re.search(r"\(z=(-?[\d.]+), n=([\d,]+)\)", msg)
        if z:
            text += f" (z={z.group(1).replace('.', ',')}, n={z.group(2).replace(',', '.')})"
        dims = re.findall(r"\[[a-z_]+=[^\]]*\]", msg)
        return text + "".join(f" {d}" for d in dims)
