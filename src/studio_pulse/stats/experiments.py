"""A/B test analysis from per-variant sufficient statistics.

The warehouse never sees a user: for each variant the extraction query (run
inside BigQuery for real data, see connectors/ga4.py `experiment_arms`)
returns n, conversions, sum(y), sum(y^2) and, for the pre-period covariate x,
sum(x), sum(x^2), sum(x*y). Everything below is computed from those numbers.

Tests, for every treatment variant against the control:

  srm             sample-ratio mismatch: chi-square goodness of fit of the user
                  counts against the configured split; flagged at p < 0.001
                  (GrowthBook's default threshold). A flagged experiment's
                  results should not be trusted.
  conversion      two-proportion z-test (pooled standard error for the test,
                  unpooled Wald interval for the difference)
  mean            Welch's t-test on the metric mean (unequal variances,
                  Welch-Satterthwaite degrees of freedom)
  cuped           the same test on CUPED-adjusted means (Deng et al. 2013):
                  y_adj = y - theta (x - mean x), theta = cov(x, y) / var(x)
                  pooled over both arms; variance per arm
                  var(y) + theta^2 var(x) - 2 theta cov(x, y)
  sequential      an always-valid confidence interval and p-value for the
                  CUPED (or plain) difference, valid however often the results
                  are looked at (asymptotic confidence sequences, Waudby-Smith
                  et al. 2023, arXiv:2103.06476, eq. 9 and 155)

The CUPED, Welch and sequential formulas are ported from GrowthBook's MIT
licensed statistics engine (growthbook/growthbook, packages/stats/gbstats:
models/statistics.py RegressionAdjustedStatistic and compute_theta,
frequentist/tests.py TwoSidedTTest, sequential_rho,
sequential_interval_halfwidth and SequentialTwoSidedTTest.p_value; commit
df3f4e2 of 11 Nov 2026; Copyright (c) 2025 GrowthBook, Inc., MIT). The gbstats
package from PyPI is not used: its last release pins pandas < 2 and numpy < 2.
Numerics are checked against scipy and statsmodels in tests/test_experiments.py
and against the worked Welch example of the NIST/SEMATECH e-Handbook of
Statistical Methods, section 7.3.1.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dist

SRM_THRESHOLD = 0.001
DEFAULT_TUNING = 5000.0  # GrowthBook's default sequential_tuning_parameter


@dataclass
class Arm:
    variant: str
    users: int
    conversions: int | None = None
    y_sum: float | None = None
    y_sumsq: float | None = None
    x_sum: float | None = None
    x_sumsq: float | None = None
    xy_sum: float | None = None

    @property
    def mean_y(self) -> float:
        return (self.y_sum or 0.0) / self.users if self.users else 0.0

    @property
    def mean_x(self) -> float:
        return (self.x_sum or 0.0) / self.users if self.users else 0.0

    @property
    def var_y(self) -> float:
        return _var(self.users, self.y_sum, self.y_sumsq)

    @property
    def var_x(self) -> float:
        return _var(self.users, self.x_sum, self.x_sumsq)

    @property
    def cov_xy(self) -> float:
        return _cov(self.users, self.y_sum, self.x_sum, self.xy_sum)

    @property
    def has_metric(self) -> bool:
        return self.y_sum is not None and self.y_sumsq is not None

    @property
    def has_covariate(self) -> bool:
        return None not in (self.x_sum, self.x_sumsq, self.xy_sum)


def _var(n: int, s: float | None, ss: float | None) -> float:
    if n <= 1 or s is None or ss is None:
        return 0.0
    return max(0.0, (ss - s * s / n) / (n - 1))


def _cov(n: int, sy: float | None, sx: float | None, sxy: float | None) -> float:
    if n <= 1 or None in (sy, sx, sxy):
        return 0.0
    return (sxy - sy * sx / n) / (n - 1)


# -- tests --------------------------------------------------------------------------


def srm(users: list[int], weights: list[float]) -> dict[str, float]:
    """Chi-square goodness of fit of the observed users against the expected split."""
    total, wsum = sum(users), sum(w for w in weights if w > 0)
    if not total or not wsum:
        return {"chi2": 0.0, "df": 0, "p_value": 1.0}
    chi2, k = 0.0, 0
    for o, w in zip(users, weights, strict=True):
        if w <= 0:
            continue
        e = w / wsum * total
        chi2 += (o - e) ** 2 / e
        k += 1
    df = max(1, k - 1)
    return {"chi2": chi2, "df": df, "p_value": dist.chi2_sf(chi2, df)}


def two_proportions(c1: int, n1: int, c2: int, n2: int, alpha: float = 0.05) -> dict[str, float]:
    """Control (c1 of n1) vs treatment (c2 of n2)."""
    p1, p2 = c1 / n1, c2 / n2
    diff = p2 - p1
    pooled = (c1 + c2) / (n1 + n2)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    z = diff / se_pooled if se_pooled > 0 else 0.0
    zq = dist.norm_ppf(1 - alpha / 2)
    return {
        "control": p1,
        "treatment": p2,
        "estimate": diff,
        "se": se,
        "z": z,
        "p_value": dist.norm_sf_two_sided(z) if se_pooled > 0 else 1.0,
        "ci_low": diff - zq * se,
        "ci_high": diff + zq * se,
        "rel_estimate": diff / p1 if p1 else None,
    }


def welch(
    m1: float, v1: float, n1: int, m2: float, v2: float, n2: int, alpha: float = 0.05
) -> dict[str, float]:
    """Welch's t-test of mean2 - mean1 from means, sample variances and sizes."""
    a, b = v1 / n1, v2 / n2
    se = math.sqrt(a + b)
    diff = m2 - m1
    if se == 0:
        return {
            "estimate": diff,
            "se": 0.0,
            "df": float("nan"),
            "t": 0.0,
            "p_value": 1.0,
            "ci_low": diff,
            "ci_high": diff,
            "rel_estimate": None,
            "rel_ci_low": None,
            "rel_ci_high": None,
        }
    df = (a + b) ** 2 / (a * a / (n1 - 1) + b * b / (n2 - 1))
    t = diff / se
    half = dist.t_ppf(1 - alpha / 2, df) * se
    out = {
        "estimate": diff,
        "se": se,
        "df": df,
        "t": t,
        "p_value": dist.t_sf_two_sided(t, df),
        "ci_low": diff - half,
        "ci_high": diff + half,
        "rel_estimate": None,
        "rel_ci_low": None,
        "rel_ci_high": None,
    }
    if m1:
        # Delta method for (m2 - m1) / m1, as GrowthBook's variance_of_ratios (no covariance
        # between independent arms).
        rel = diff / abs(m1)
        rel_var = b / m1**2 + a * m2**2 / m1**4
        zq = dist.t_ppf(1 - alpha / 2, df)
        out.update(
            rel_estimate=rel,
            rel_ci_low=rel - zq * math.sqrt(rel_var),
            rel_ci_high=rel + zq * math.sqrt(rel_var),
        )
    return out


def cuped_theta(arms: list[Arm]) -> float:
    """theta = cov(x, y) / var(x) over all users of the compared arms (GrowthBook compute_theta)."""
    n = sum(a.users for a in arms)
    sy = sum(a.y_sum or 0.0 for a in arms)
    syy = sum(a.y_sumsq or 0.0 for a in arms)
    sx = sum(a.x_sum or 0.0 for a in arms)
    sxx = sum(a.x_sumsq or 0.0 for a in arms)
    sxy = sum(a.xy_sum or 0.0 for a in arms)
    var_x, var_y = _var(n, sx, sxx), _var(n, sy, syy)
    # near-zero pre-period variance: no adjustment (GrowthBook RELATIVE_VARIANCE_ZERO_TOL idea)
    if n <= 1 or var_y == 0 or var_x <= 1e-10 * (sxx / n if n else 0):
        return 0.0
    return _cov(n, sy, sx, sxy) / var_x


def cuped_arm(a: Arm, theta: float) -> tuple[float, float]:
    """(adjusted mean, adjusted variance) of one arm."""
    mean = a.mean_y - theta * a.mean_x
    var = a.var_y + theta * theta * a.var_x - 2 * theta * a.cov_xy
    return mean, max(0.0, var)


def sequential_rho(alpha: float, tuning: float = DEFAULT_TUNING) -> float:
    """Eq. 161 of Waudby-Smith et al. (two-sided), as GrowthBook sequential_rho."""
    return math.sqrt((-2 * math.log(alpha) + math.log(-2 * math.log(alpha) + 1)) / tuning)


def sequential(
    estimate: float, se: float, n: int, alpha: float = 0.05, tuning: float = DEFAULT_TUNING
) -> dict[str, float]:
    """Always-valid interval and p-value (GrowthBook SequentialTwoSidedTTest).

    n is the total number of users in both arms; se the standard error of the
    difference, so s2 = se^2 n is the variance on the per-user scale.
    """
    rho = sequential_rho(alpha, tuning)
    s2 = se * se * n
    nr2 = n * rho * rho
    half = math.sqrt(s2) * math.sqrt(
        2 * (nr2 + 1) * math.log(math.sqrt(nr2 + 1) / alpha) / (n * rho) ** 2
    )
    if se > 0:
        st2 = estimate * estimate * n / (se * se)
        log_e = rho * rho * st2 / (2 * (nr2 + 1)) - 0.5 * math.log(nr2 + 1)
        p = min(1.0, math.exp(-log_e)) if log_e > -700 else 1.0
    else:
        p = 1.0
    return {
        "estimate": estimate,
        "ci_low": estimate - half,
        "ci_high": estimate + half,
        "p_value": p,
        "rho": rho,
        "halfwidth": half,
    }


# -- one experiment -----------------------------------------------------------------


@dataclass
class Experiment:
    id: str
    arms: list[Arm]
    control: str | None = None
    split: dict[str, float] = field(default_factory=dict)
    alpha: float = 0.05
    tuning: float = DEFAULT_TUNING
    name: str = ""
    metric: str = "metric"
    app_id: str = ""


def analyze(exp: Experiment) -> dict[str, Any]:
    arms = sorted(exp.arms, key=lambda a: a.variant)
    if len(arms) < 2:
        raise ValueError(f"experiment {exp.id!r}: needs at least two variants, got {len(arms)}")
    control = next((a for a in arms if a.variant == exp.control), arms[0])
    weights = [float(exp.split.get(a.variant, 1.0)) for a in arms]
    s = srm([a.users for a in arms], weights)
    wsum = sum(weights)
    out: dict[str, Any] = {
        "experiment": exp.id,
        "name": exp.name or exp.id,
        "app_id": exp.app_id,
        "metric": exp.metric,
        "control": control.variant,
        "alpha": exp.alpha,
        "srm": {**s, "flagged": s["p_value"] < SRM_THRESHOLD},
        "arms": [
            {
                "variant": a.variant,
                "users": a.users,
                "share": a.users / sum(x.users for x in arms),
                "expected_share": w / wsum,
                "conversions": a.conversions,
                "conversion_rate": (a.conversions / a.users) if a.conversions is not None else None,
                "mean": a.mean_y if a.has_metric else None,
                "sd": math.sqrt(a.var_y) if a.has_metric else None,
                "mean_x": a.mean_x if a.has_covariate else None,
            }
            for a, w in zip(arms, weights, strict=True)
        ],
        "comparisons": [],
    }
    for t in arms:
        if t is control:
            continue
        cmp: dict[str, Any] = {"variant": t.variant}
        if control.conversions is not None and t.conversions is not None:
            cmp["conversion"] = two_proportions(
                control.conversions, control.users, t.conversions, t.users, exp.alpha
            )
        if control.has_metric and t.has_metric:
            cmp["mean"] = welch(
                control.mean_y, control.var_y, control.users, t.mean_y, t.var_y, t.users, exp.alpha
            )
            best = cmp["mean"]
            if control.has_covariate and t.has_covariate:
                theta = cuped_theta([control, t])
                mc, vc = cuped_arm(control, theta)
                mt, vt = cuped_arm(t, theta)
                cu = welch(mc, vc, control.users, mt, vt, t.users, exp.alpha)
                se2 = cmp["mean"]["se"] ** 2
                # Relative lift is against the unadjusted control mean, as in GrowthBook.
                if control.mean_y:
                    scale = cu["estimate"] / abs(control.mean_y)
                    ratio = cu["se"] / abs(control.mean_y)
                    q = dist.t_ppf(1 - exp.alpha / 2, cu["df"]) if cu["se"] else 0.0
                    cu.update(
                        rel_estimate=scale,
                        rel_ci_low=scale - q * ratio,
                        rel_ci_high=scale + q * ratio,
                    )
                cu.update(
                    theta=theta,
                    variance_reduction=(1 - cu["se"] ** 2 / se2) if se2 else 0.0,
                    control_mean=mc,
                    treatment_mean=mt,
                )
                cmp["cuped"] = cu
                best = cu
            cmp["sequential"] = sequential(
                best["estimate"], best["se"], control.users + t.users, exp.alpha, exp.tuning
            )
            cmp["sequential"]["on"] = "cuped" if best is cmp.get("cuped") else "mean"
        out["comparisons"].append(cmp)
    out["verdict"] = verdict(out)
    return out


def verdict(res: dict[str, Any]) -> str:
    if res["srm"]["flagged"]:
        return (
            f"Sample ratio mismatch (p = {res['srm']['p_value']:.2g}): assignment or "
            "logging is broken; do not use these results."
        )
    lines = []
    for c in res["comparisons"]:
        seq = c.get("sequential")
        main = c.get("cuped") or c.get("mean")
        if not main:
            conv = c.get("conversion")
            if conv:
                sig = conv["p_value"] < res["alpha"]
                lines.append(
                    f"{c['variant']}: conversion {conv['estimate'] * 100:+.2f} pts "
                    f"({'significant' if sig else 'not significant'}, p = {conv['p_value']:.3g})"
                )
            continue
        label = "CUPED" if "cuped" in c else "Welch"
        if seq and (seq["ci_low"] > 0 or seq["ci_high"] < 0):
            direction = "better" if seq["estimate"] > 0 else "worse"
            lines.append(
                f"{c['variant']} is {direction} than {res['control']} on {res['metric']} "
                f"(always-valid interval excludes 0; {label} p = {main['p_value']:.3g}): "
                "safe to stop and decide."
            )
        elif main["p_value"] < res["alpha"]:
            lines.append(
                f"{c['variant']}: {label} p = {main['p_value']:.3g} at the planned end, "
                "but the always-valid interval still includes 0: if you have been "
                "peeking, keep running."
            )
        else:
            lines.append(
                f"{c['variant']}: no detectable difference yet ({label} p = {main['p_value']:.3g})."
            )
    return " ".join(lines) or "Not enough data."


def format_console(res: dict[str, Any]) -> str:
    s = res["srm"]
    lines = [
        f"Experiment {res['name']} ({res['experiment']}), metric {res['metric']}, "
        f"control {res['control']}",
        f"  SRM: chi2 {s['chi2']:.2f}, df {s['df']}, p {s['p_value']:.3g}"
        + ("  << SAMPLE RATIO MISMATCH" if s["flagged"] else "  (ok)"),
    ]
    for a in res["arms"]:
        conv = f"{a['conversion_rate'] * 100:.2f}%" if a["conversion_rate"] is not None else "-"
        mean = f"{a['mean']:.4f}" if a["mean"] is not None else "-"
        lines.append(
            f"  {a['variant']:<10} users {a['users']:>9,} ({a['share'] * 100:5.2f}% vs "
            f"{a['expected_share'] * 100:5.2f}%)  conversion {conv:>7}  mean {mean}"
        )
    for c in res["comparisons"]:
        for key, label in (
            ("conversion", "conversion (z)"),
            ("mean", "mean (Welch)"),
            ("cuped", "mean (CUPED)"),
            ("sequential", "always-valid"),
        ):
            r = c.get(key)
            if not r:
                continue
            extra = ""
            if key == "cuped":
                extra = f"  theta {r['theta']:.3f}, variance -{r['variance_reduction'] * 100:.1f}%"
            lines.append(
                f"  {c['variant']} vs {res['control']} {label:<15} {r['estimate']:+.5f} "
                f"[{r['ci_low']:+.5f}, {r['ci_high']:+.5f}]  p {r['p_value']:.4g}{extra}"
            )
    lines.append("  Verdict: " + res["verdict"])
    return "\n".join(lines)


# -- files --------------------------------------------------------------------------

ARM_FIELDS = ("users", "conversions", "y_sum", "y_sumsq", "x_sum", "x_sumsq", "xy_sum")


def read_arms(path: str | Path) -> list[Arm]:
    """Aggregates from a CSV (one row per variant) or JSON (a list of objects)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    rows = (
        json.loads(text) if p.suffix.lower() == ".json" else list(csv.DictReader(text.splitlines()))
    )
    arms = []
    for r in rows:
        if "variant" not in r or "users" not in r:
            raise ValueError(f"{p.name}: every row needs variant and users")
        vals: dict[str, Any] = {}
        for f in ARM_FIELDS:
            v = r.get(f)
            if v in (None, ""):
                continue
            vals[f] = int(float(v)) if f in ("users", "conversions") else float(v)
        arms.append(Arm(variant=str(r["variant"]), **vals))
    return arms


# -- warehouse ----------------------------------------------------------------------


def _round(v: Any) -> Any:
    """10 significant digits: stored results are identical on every OS."""
    if isinstance(v, float) and math.isfinite(v):
        return float(f"{v:.10g}")
    return v


def load_experiments(con: Any) -> list[Experiment]:
    try:
        arms = con.execute(
            "SELECT app_id, experiment_id, variant, users, conversions, y_sum, y_sumsq, x_sum, "
            "x_sumsq, xy_sum FROM raw_experiment_arms ORDER BY 1, 2, 3"
        ).fetchall()
        meta = {
            (r[0], r[1]): r
            for r in con.execute(
                "SELECT app_id, experiment_id, name, control, split, alpha, metric, tuning "
                "FROM meta_experiments"
            ).fetchall()
        }
    except Exception:  # tables missing in an old warehouse
        return []
    by: dict[tuple[str, str], list[Arm]] = {}
    for r in arms:
        by.setdefault((r[0], r[1]), []).append(
            Arm(r[2], int(r[3] or 0), *(None if x is None else x for x in r[4:]))
        )
    out = []
    for key, a in sorted(by.items()):
        m = meta.get(key)
        out.append(
            Experiment(
                id=key[1],
                app_id=key[0],
                arms=a,
                name=m[2] if m else key[1],
                control=m[3] if m else None,
                split=json.loads(m[4]) if m and m[4] else {},
                alpha=float(m[5]) if m and m[5] else 0.05,
                metric=m[6] if m and m[6] else "metric",
                tuning=float(m[7]) if m and m[7] else DEFAULT_TUNING,
            )
        )
    return out


def store(con: Any) -> int:
    """(Re)build fct_experiment_arms and fct_experiment_tests from raw_experiment_arms."""
    con.execute(
        "CREATE OR REPLACE TABLE fct_experiment_arms (app_id VARCHAR, experiment_id VARCHAR, "
        "name VARCHAR, metric VARCHAR, variant VARCHAR, is_control BOOLEAN, users BIGINT, "
        "share DOUBLE, expected_share DOUBLE, conversions BIGINT, conversion_rate DOUBLE, "
        "mean DOUBLE, sd DOUBLE, mean_pre DOUBLE, srm_chi2 DOUBLE, srm_p DOUBLE, "
        "srm_flagged BOOLEAN, verdict VARCHAR)"
    )
    con.execute(
        "CREATE OR REPLACE TABLE fct_experiment_tests (app_id VARCHAR, experiment_id VARCHAR, "
        "variant VARCHAR, control VARCHAR, test VARCHAR, estimate DOUBLE, ci_low DOUBLE, "
        "ci_high DOUBLE, p_value DOUBLE, rel_estimate DOUBLE, rel_ci_low DOUBLE, "
        "rel_ci_high DOUBLE, se DOUBLE, df DOUBLE, theta DOUBLE, variance_reduction DOUBLE)"
    )
    n = 0
    for exp in load_experiments(con):
        try:
            res = analyze(exp)
        except ValueError:
            continue
        s = res["srm"]
        for a in res["arms"]:
            con.execute(
                "INSERT INTO fct_experiment_arms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _round(v)
                    for v in (
                        exp.app_id,
                        exp.id,
                        res["name"],
                        res["metric"],
                        a["variant"],
                        a["variant"] == res["control"],
                        a["users"],
                        a["share"],
                        a["expected_share"],
                        a["conversions"],
                        a["conversion_rate"],
                        a["mean"],
                        a["sd"],
                        a["mean_x"],
                        s["chi2"],
                        s["p_value"],
                        s["flagged"],
                        res["verdict"],
                    )
                ],
            )
        for c in res["comparisons"]:
            for test in ("conversion", "mean", "cuped", "sequential"):
                r = c.get(test)
                if not r:
                    continue
                con.execute(
                    "INSERT INTO fct_experiment_tests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        _round(v)
                        for v in (
                            exp.app_id,
                            exp.id,
                            c["variant"],
                            res["control"],
                            test,
                            r["estimate"],
                            r["ci_low"],
                            r["ci_high"],
                            r["p_value"],
                            r.get("rel_estimate"),
                            r.get("rel_ci_low"),
                            r.get("rel_ci_high"),
                            r.get("se"),
                            r.get("df"),
                            r.get("theta"),
                            r.get("variance_reduction"),
                        )
                    ],
                )
        n += 1
    return n


def report_html(con: Any, apps: list[dict[str, Any]]) -> str:
    from html import escape

    from ..dashboard.charts import data_table, fmt_int, fmt_pct

    intro = (
        '<p class="note">Each experiment is analysed from per-variant totals computed inside '
        "the warehouse source (no user rows leave BigQuery): a sample-ratio-mismatch check, a "
        "two-proportion z-test on conversion, Welch's t-test on the metric mean, the same test "
        "on CUPED-adjusted means (pre-period covariate), and an always-valid interval that stays "
        "correct however often you look. Formulas ported from GrowthBook's MIT stats engine; "
        "see docs/METRICS.md.</p>"
    )
    try:
        arms = con.execute(
            "SELECT * FROM fct_experiment_arms ORDER BY app_id, experiment_id, variant"
        )
        cols = [c[0] for c in arms.description]
        arm_rows = [dict(zip(cols, r, strict=False)) for r in arms.fetchall()]
        tests = con.execute(
            "SELECT * FROM fct_experiment_tests ORDER BY app_id, experiment_id, variant"
        )
        tcols = [c[0] for c in tests.description]
        test_rows = [dict(zip(tcols, r, strict=False)) for r in tests.fetchall()]
    except Exception:
        arm_rows, test_rows = [], []
    if not arm_rows:
        return intro + (
            '<p class="note">No experiments configured. Add an <code>experiments:</code> list to an '
            "app (pulse.example.yaml); exposure must be logged as an event with the experiment id "
            "and the variant as parameters.</p>"
        )
    names = {a["app_id"]: a["name"] for a in apps}
    parts = [intro]
    seen = []
    for r in arm_rows:
        key = (r["app_id"], r["experiment_id"])
        if key not in seen:
            seen.append(key)
    for app_id, exp_id in seen:
        arms_e = [r for r in arm_rows if (r["app_id"], r["experiment_id"]) == (app_id, exp_id)]
        tests_e = [r for r in test_rows if (r["app_id"], r["experiment_id"]) == (app_id, exp_id)]
        first = arms_e[0]
        srm_cls = "critical" if first["srm_flagged"] else "ok"
        srm_txt = (
            f"Sample ratio check: chi-square {first['srm_chi2']:.2f}, p = {first['srm_p']:.3g}"
            + (" - MISMATCH, results not trustworthy" if first["srm_flagged"] else " - ok")
        )
        arm_table = data_table(
            [
                "Variant",
                "Users",
                "Share (expected)",
                "Conversion",
                f"Mean {first['metric']}",
                "SD",
                "Pre-period mean",
            ],
            [
                [
                    escape(a["variant"]) + (" (control)" if a["is_control"] else ""),
                    fmt_int(a["users"]),
                    f"{fmt_pct(a['share'], 2)} ({fmt_pct(a['expected_share'], 0)})",
                    fmt_pct(a["conversion_rate"], 2),
                    f"{a['mean']:.4f}" if a["mean"] is not None else "–",
                    f"{a['sd']:.4f}" if a["sd"] is not None else "–",
                    f"{a['mean_pre']:.4f}" if a["mean_pre"] is not None else "–",
                ]
                for a in arms_e
            ],
            left_cols=(0,),
        )
        labels = {
            "conversion": "Conversion (two-proportion z)",
            "mean": "Mean (Welch t)",
            "cuped": "Mean, CUPED-adjusted",
            "sequential": "Always-valid (sequential)",
        }
        test_table = data_table(
            [
                "Variant",
                "Test",
                "Difference",
                "95% interval",
                "Relative",
                "p-value",
                "Variance reduction",
            ],
            [
                [
                    escape(t["variant"]),
                    labels.get(t["test"], t["test"]),
                    f"{t['estimate']:+.4f}",
                    f"[{t['ci_low']:+.4f}, {t['ci_high']:+.4f}]",
                    fmt_pct(t["rel_estimate"], 1) if t["rel_estimate"] is not None else "–",
                    f"{t['p_value']:.3g}",
                    fmt_pct(t["variance_reduction"], 1)
                    if t["variance_reduction"] is not None
                    else "–",
                ]
                for t in tests_e
            ],
            left_cols=(0, 1),
        )
        parts.append(
            f"<h2>{escape(names.get(app_id, app_id))} · {escape(first['name'])}</h2>"
            f'<ul class="alerts"><li class="{srm_cls}"><span class="sev">SRM</span>{escape(srm_txt)}</li>'
            f'<li class="ok"><span class="sev">Verdict</span>{escape(first["verdict"])}</li></ul>'
            + arm_table
            + test_table
        )
    return "".join(parts)
