"""Alert rules (alerts.yaml) evaluated against the warehouse models.

Each rule names a metric from METRICS (a daily series per app), a condition and
its parameters:

  above / below          value of the latest point vs a fixed threshold
  drop_vs_baseline       latest value at least threshold_pct % below the mean of
                         the previous baseline_days points
  rise_vs_baseline       latest value at least threshold_pct % above that mean
  forecast_band          latest value outside the `level` % prediction interval of a
                         forecast with weekly seasonality, fitted on the previous
                         history_days (default 56) points; direction drop | rise |
                         both; engine auto | seasonal | statsforecast (see
                         stats/forecast.py); never fires on, nor learns from,
                         the listed holidays

Optional per rule: where (dimension filter, e.g. {device_tier: low}),
min_sample (skip points backed by fewer users/windows/samples), apps (limit to
some app ids), severity (warning | critical), evaluate_days (average the last N
points instead of using only the latest one), min_z (proportion metrics only:
also require the move to be at least min_z binomial standard errors, see
_z_score).

Baselines and multi-day values combine points the way the metric catalog
(brain/metrics.yaml, `alert.baseline`) says: `pooled` weights each day by its
sample (sum(value x sample) / sum(sample), e.g. retained users / cohort users),
used where the sample is the ratio's denominator; `mean_of_daily` is the plain
mean. The brain reads the same field, so both quote the same baseline.

Notifications: always printed; also sent to Slack when
$PULSE_SLACK_WEBHOOK_URL is set and by e-mail when $PULSE_SMTP_HOST and
$PULSE_ALERT_EMAIL_TO are set. --dry-run prints what would be sent.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import duckdb
import yaml


@dataclass(frozen=True)
class MetricSpec:
    sql: str
    unit: str  # count | pct | usd | ms | ratio | mb
    dims: tuple[str, ...] = ()
    description: str = ""


METRICS: dict[str, MetricSpec] = {
    "dau": MetricSpec(
        "SELECT app_id, date, dau AS value, dau AS sample FROM fct_app_daily WHERE dau IS NOT NULL",
        "count",
    ),
    "new_users": MetricSpec(
        "SELECT app_id, date, new_users AS value, dau AS sample FROM fct_app_daily WHERE new_users IS NOT NULL",
        "count",
    ),
    "sessions_per_dau": MetricSpec(
        "SELECT app_id, date, sessions_per_dau AS value, dau AS sample FROM fct_app_daily", "ratio"
    ),
    "revenue_usd": MetricSpec(
        "SELECT app_id, date, revenue_usd AS value, dau AS sample FROM fct_app_daily WHERE dau IS NOT NULL",
        "usd",
    ),
    "arpdau": MetricSpec(
        "SELECT app_id, date, arpdau AS value, dau AS sample FROM fct_app_daily", "usd"
    ),
    "payer_conversion": MetricSpec(
        "SELECT app_id, date, payer_conversion AS value, dau AS sample FROM fct_app_daily", "pct"
    ),
    "ecpm_usd": MetricSpec(
        "SELECT app_id, date, ecpm_usd AS value, ad_impressions AS sample FROM fct_app_daily", "usd"
    ),
    "crash_free_users": MetricSpec(
        "SELECT app_id, date, crash_free_users AS value, dau AS sample FROM fct_app_daily", "pct"
    ),
    "crash_free_sessions": MetricSpec(
        "SELECT app_id, date, crash_free_sessions AS value, sessions AS sample FROM fct_app_daily",
        "pct",
    ),
    "d1_retention": MetricSpec(
        "SELECT app_id, cohort_date AS date, retention AS value, cohort_size AS sample FROM fct_retention WHERE day_n = 1 AND is_complete",
        "pct",
        description="D1 retention by install cohort date",
    ),
    "d7_retention": MetricSpec(
        "SELECT app_id, cohort_date AS date, retention AS value, cohort_size AS sample FROM fct_retention WHERE day_n = 7 AND is_complete",
        "pct",
        description="D7 retention by install cohort date",
    ),
    "frame_time_p50_ms": MetricSpec(
        "SELECT app_id, date, ft_p50_ms AS value, windows AS sample, device_tier FROM fct_perf_daily",
        "ms",
        ("device_tier",),
    ),
    "frame_time_p95_ms": MetricSpec(
        "SELECT app_id, date, ft_p95_ms AS value, windows AS sample, device_tier FROM fct_perf_daily",
        "ms",
        ("device_tier",),
    ),
    "slow_frame_share": MetricSpec(
        "SELECT app_id, date, slow_frame_share AS value, windows AS sample, device_tier FROM fct_perf_daily",
        "pct",
        ("device_tier",),
    ),
    "janky_frame_share": MetricSpec(
        "SELECT app_id, date, janky_frame_share AS value, windows AS sample, device_tier "
        "FROM fct_perf_daily WHERE janky_frame_share IS NOT NULL",
        "pct",
        ("device_tier",),
        "Frames over the display's frame budget / frames (Flutter package)",
    ),
    "memory_p95_mb": MetricSpec(
        "SELECT app_id, date, mem_alloc_p95_mb AS value, windows AS sample, device_tier FROM fct_perf_daily",
        "mb",
        ("device_tier",),
    ),
    "process_memory_p95_mb": MetricSpec(
        "SELECT app_id, date, sys_used_p95_mb AS value, windows AS sample, device_tier FROM fct_perf_daily",
        "mb",
        ("device_tier",),
        "System Used Memory (what the OS counts against the app), p95 over windows",
    ),
    "cold_start_p50_ms": MetricSpec(
        "SELECT app_id, date, p50_ms AS value, samples AS sample FROM fct_load_daily WHERE kind = 'cold_start'",
        "ms",
    ),
    "scene_load_p50_ms": MetricSpec(
        "SELECT app_id, date, p50_ms AS value, samples AS sample FROM fct_load_daily WHERE kind = 'scene_load'",
        "ms",
    ),
    "ua_spend_usd": MetricSpec(
        "SELECT app_id, date, SUM(spend_usd) AS value, SUM(network_installs) AS sample "
        "FROM fct_ua_daily GROUP BY 1, 2",
        "usd",
        description="Ad-network spend per day, all networks",
    ),
    "ua_cpi_usd": MetricSpec(
        "SELECT app_id, date, SUM(spend_usd) / NULLIF(SUM(network_installs), 0) AS value, "
        "SUM(network_installs) AS sample FROM fct_ua_daily GROUP BY 1, 2",
        "usd",
        description="Spend / network-reported installs per day, all networks",
    ),
    "review_rating": MetricSpec(
        "SELECT app_id, date, avg_rating AS value, reviews AS sample FROM fct_review_daily "
        "WHERE store = 'all'",
        "ratio",
        description="Mean star rating of the reviews written that day (both stores)",
    ),
    "low_star_share": MetricSpec(
        "SELECT app_id, date, low_star_share AS value, reviews AS sample FROM fct_review_daily "
        "WHERE store = 'all'",
        "pct",
        description="Share of that day's reviews with 1 or 2 stars",
    ),
    "anr_rate": MetricSpec(
        "SELECT app_id, date, user_perceived_anr_rate AS value, distinct_users AS sample "
        "FROM fct_android_vitals_daily",
        "pct",
        description="Play vitals: user-perceived ANR rate (daily)",
    ),
    "vitals_crash_rate": MetricSpec(
        "SELECT app_id, date, user_perceived_crash_rate AS value, distinct_users AS sample "
        "FROM fct_android_vitals_daily",
        "pct",
        description="Play vitals: user-perceived crash rate (daily)",
    ),
}

CONDITIONS = ("above", "below", "drop_vs_baseline", "rise_vs_baseline", "forecast_band")


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    name: str
    metric: str
    condition: str
    threshold: float | None = None
    threshold_pct: float | None = None
    baseline_days: int = 14
    min_baseline_points: int | None = None
    min_sample: float = 0
    evaluate_days: int = 1
    min_z: float | None = None
    where: dict[str, Any] = field(default_factory=dict)
    apps: list[str] = field(default_factory=list)
    severity: str = "warning"
    description: str = ""
    # forecast_band (level None: the engine's default, stats/forecast.py DEFAULT_LEVEL)
    level: float | None = None
    history_days: int = 56
    min_history: int = 28
    direction: str = "both"
    engine: str = "auto"
    holidays: list[Any] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any], defaults: dict[str, Any]) -> Rule:
        merged = {**defaults, **d}
        known = set(cls.__dataclass_fields__)
        unknown = set(merged) - known
        if unknown:
            raise RuleError(f"rule {d.get('name')!r}: unknown keys {sorted(unknown)}")
        r = cls(**merged)
        if r.metric not in METRICS:
            raise RuleError(
                f"rule {r.name!r}: unknown metric {r.metric!r} (known: {', '.join(sorted(METRICS))})"
            )
        if r.condition not in CONDITIONS:
            raise RuleError(f"rule {r.name!r}: condition must be one of {CONDITIONS}")
        if r.condition in ("above", "below") and r.threshold is None:
            raise RuleError(f"rule {r.name!r}: needs threshold")
        if r.condition.endswith("_vs_baseline") and r.threshold_pct is None:
            raise RuleError(f"rule {r.name!r}: needs threshold_pct")
        if r.condition == "forecast_band":
            from .stats.forecast import ENGINES

            if r.evaluate_days != 1:
                raise RuleError(
                    f"rule {r.name!r}: forecast_band evaluates one day (evaluate_days: 1)"
                )
            if r.direction not in ("both", "drop", "rise"):
                raise RuleError(f"rule {r.name!r}: direction must be both, drop or rise")
            if r.engine not in ENGINES:
                raise RuleError(f"rule {r.name!r}: engine must be one of {', '.join(ENGINES)}")
            if r.level is not None and not 50 <= float(r.level) < 100:
                raise RuleError(f"rule {r.name!r}: level is a percentage in [50, 100)")
        try:
            r.holidays = sorted(
                h if isinstance(h, dt.date) else dt.date.fromisoformat(str(h)) for h in r.holidays
            )
        except ValueError as exc:
            raise RuleError(f"rule {r.name!r}: holidays must be dates (YYYY-MM-DD)") from exc
        if r.severity not in ("warning", "critical"):
            raise RuleError(f"rule {r.name!r}: severity must be warning or critical")
        if r.min_z is not None and METRICS[r.metric].unit != "pct":
            raise RuleError(f"rule {r.name!r}: min_z applies to proportion metrics only")
        bad_dims = set(r.where) - set(METRICS[r.metric].dims)
        if bad_dims:
            raise RuleError(
                f"rule {r.name!r}: metric {r.metric} has no dimension(s) {sorted(bad_dims)}"
            )
        return r


@dataclass
class Alert:
    rule: str
    app_id: str
    severity: str
    metric: str
    date: dt.date
    value: float
    baseline: float | None
    threshold: float
    message: str


def load_rules(path: str | Path) -> list[Rule]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    rules = [Rule.from_dict(r, defaults) for r in data.get("rules") or []]
    names = [r.name for r in rules]
    if len(names) != len(set(names)):
        raise RuleError("rule names must be unique")
    return rules


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "pct":
        return f"{v * 100:.2f}%"
    if unit == "usd":
        return f"${v:,.3f}" if abs(v) < 10 else f"${v:,.0f}"
    if unit == "ms":
        return f"{v:.1f} ms"
    if unit == "mb":
        return f"{v:.0f} MB"
    if unit == "count":
        return f"{v:,.0f}"
    return f"{v:.2f}"


def _series(
    con: duckdb.DuckDBPyConnection, rule: Rule
) -> dict[str, list[tuple[dt.date, float, float | None]]]:
    spec = METRICS[rule.metric]
    sql = f"SELECT * FROM ({spec.sql}) WHERE value IS NOT NULL"
    params: list[Any] = []
    for k, v in rule.where.items():
        sql += f" AND {k} = ?"
        params.append(v)
    if rule.apps:
        sql += " AND app_id IN (" + ",".join("?" * len(rule.apps)) + ")"
        params += rule.apps
    sql += " ORDER BY app_id, date"
    try:
        rows = con.execute(sql, params).fetchall()
    except duckdb.CatalogException:
        return {}
    out: dict[str, list[tuple[dt.date, float, float | None]]] = {}
    for r in rows:
        out.setdefault(r[0], []).append((r[1], float(r[2]), r[3]))
    return out


def _combine(points: list[tuple[dt.date, float, float | None]], pooled: bool) -> float:
    """Pooled (sample-weighted) or plain mean of the points' values."""
    if pooled:
        w = sum(p[2] or 0 for p in points)
        if w > 0 and all(p[2] is not None for p in points):
            return sum(p[1] * p[2] for p in points) / w
    return sum(p[1] for p in points) / len(points)


def _z_score(value: float, reference: float, n: float) -> float | None:
    """How many binomial standard errors `value` (a share of n) is from `reference`.

    D1 retention of a 50-user cohort has a standard error of about 6.7 points
    at 35%, so a 20% relative drop is well inside the noise; without this check
    the d1 rule fired on small cohorts that were simply unlucky.
    """
    if n <= 0 or not 0 < reference < 1:
        return None
    return (value - reference) / math.sqrt(reference * (1 - reference) / n)


def evaluate(con: duckdb.DuckDBPyConnection, rules: list[Rule]) -> list[Alert]:
    from .brain.catalog import alert_baseline

    alerts: list[Alert] = []
    for rule in rules:
        unit = METRICS[rule.metric].unit
        pooled = alert_baseline(rule.metric) == "pooled"
        for app_id, pts in _series(con, rule).items():
            pts = [p for p in pts if p[2] is None or p[2] >= rule.min_sample]
            if len(pts) < rule.evaluate_days:
                continue
            latest = pts[-rule.evaluate_days :]
            value = _combine(latest, pooled)
            day = latest[-1][0]
            first_eval_day = latest[0][0]
            baseline = None
            if rule.condition == "forecast_band":
                from .stats import forecast

                holidays = set(rule.holidays)
                if day in holidays:
                    continue
                lo_day = day - dt.timedelta(days=rule.history_days)
                hist = [(p[0], p[1]) for p in pts[:-1] if lo_day <= p[0] < day]
                b = forecast.band(
                    hist,
                    day,
                    level=rule.level,
                    engine=rule.engine,
                    holidays=holidays,
                    min_points=rule.min_history,
                )
                if b is None:
                    continue
                hit = forecast.outside(value, b, rule.direction)
                baseline = b.center
                thr = b.low if value < b.center else b.high
                lvl = rule.level or forecast.DEFAULT_LEVEL[b.engine]
                msg = (
                    f"{rule.metric} {_fmt(value, unit)} is outside the {float(lvl):g}% "
                    f"forecast band [{_fmt(b.low, unit)}, {_fmt(b.high, unit)}] (expected "
                    f"{_fmt(b.center, unit)}, weekly seasonality, {b.engine})"
                )
            elif rule.condition in ("above", "below"):
                hit = (
                    value > rule.threshold if rule.condition == "above" else value < rule.threshold
                )
                thr = float(rule.threshold)
                msg = f"{rule.metric} {_fmt(value, unit)} is {rule.condition} {_fmt(thr, unit)}"
            else:
                lo = first_eval_day - dt.timedelta(days=rule.baseline_days)
                base_pts = [p for p in pts if lo <= p[0] < first_eval_day]
                need = rule.min_baseline_points or max(3, rule.baseline_days // 2)
                if len(base_pts) < need:
                    continue
                baseline = _combine(base_pts, pooled)
                if baseline == 0:
                    continue
                change = (value - baseline) / abs(baseline)
                thr = float(rule.threshold_pct)
                if rule.condition == "drop_vs_baseline":
                    hit = change <= -thr / 100
                    word = "down"
                else:
                    hit = change >= thr / 100
                    word = "up"
                msg = (
                    f"{rule.metric} {_fmt(value, unit)} is {word} {abs(change) * 100:.1f}% vs the "
                    f"trailing {rule.baseline_days}-day {'pooled rate' if pooled else 'mean'} "
                    f"{_fmt(baseline, unit)} (limit {thr:.0f}%)"
                )
            if hit and rule.min_z is not None:
                n = sum(p[2] or 0 for p in latest)
                z = _z_score(value, baseline if baseline is not None else thr, n)
                if z is not None:
                    hit = abs(z) >= rule.min_z
                    msg += f" (z={z:.1f}, n={n:,.0f})"
            if hit:
                where = "".join(f" [{k}={v}]" for k, v in rule.where.items())
                alerts.append(
                    Alert(
                        rule.name,
                        app_id,
                        rule.severity,
                        rule.metric,
                        day,
                        value,
                        baseline,
                        thr,
                        msg + where,
                    )
                )
    alerts.sort(key=lambda a: (a.severity != "critical", a.app_id, a.rule))
    return alerts


def format_console(alerts: list[Alert]) -> str:
    if not alerts:
        return "No alerts."
    lines = [f"{len(alerts)} alert(s):"]
    for a in alerts:
        lines.append(
            f"  [{a.severity.upper():8}] {a.app_id:<16} {a.rule:<28} {a.date}  {a.message}"
        )
    return "\n".join(lines)


def slack_payload(alerts: list[Alert], title: str) -> dict[str, Any]:
    lines = [f"*{title}*: {len(alerts)} alert(s)"]
    for a in alerts:
        icon = ":red_circle:" if a.severity == "critical" else ":warning:"
        lines.append(f"{icon} *{a.app_id}* `{a.rule}` ({a.date}): {a.message}")
    return {"text": "\n".join(lines)}


def notify(
    alerts: list[Alert],
    *,
    title: str = "Studio Pulse",
    dry_run: bool = False,
    session: Any = None,
    smtp_factory: Any = None,
    log: Any = print,
) -> dict[str, str]:
    """Send alerts to the configured channels. Returns {channel: status}."""
    status: dict[str, str] = {}
    if not alerts:
        return status
    webhook = os.environ.get("PULSE_SLACK_WEBHOOK_URL")
    if webhook:
        payload = slack_payload(alerts, title)
        if dry_run:
            log("[dry-run] would POST to Slack webhook:\n" + json.dumps(payload, indent=2))
            status["slack"] = "dry-run"
        else:
            if session is None:
                import requests

                session = requests.Session()
            r = session.post(webhook, json=payload, timeout=20)
            status["slack"] = "sent" if r.status_code < 300 else f"error {r.status_code}"
    host, to = os.environ.get("PULSE_SMTP_HOST"), os.environ.get("PULSE_ALERT_EMAIL_TO")
    if host and to:
        msg = EmailMessage()
        msg["Subject"] = f"{title}: {len(alerts)} alert(s)"
        msg["From"] = os.environ.get("PULSE_SMTP_FROM", "studio-pulse@localhost")
        msg["To"] = to
        msg.set_content(format_console(alerts))
        if dry_run:
            log(f"[dry-run] would e-mail {len(alerts)} alert(s) via {host}")
            status["email"] = "dry-run"
        else:
            factory = smtp_factory or smtplib.SMTP
            with factory(host, int(os.environ.get("PULSE_SMTP_PORT", "587"))) as s:
                if os.environ.get("PULSE_SMTP_STARTTLS", "1") == "1" and hasattr(s, "starttls"):
                    s.starttls()
                user = os.environ.get("PULSE_SMTP_USER")
                if user:
                    s.login(user, os.environ.get("PULSE_SMTP_PASSWORD", ""))
                s.send_message(msg)
            status["email"] = "sent"
    if not status:
        log(
            "(no notification channel configured: set PULSE_SLACK_WEBHOOK_URL or PULSE_SMTP_HOST + PULSE_ALERT_EMAIL_TO)"
        )
    return status
