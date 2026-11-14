"""Semantic layer: what the analyst, and a model behind it, may ask the warehouse.

One description of every metric, built from
  * the metric catalog (metrics.yaml, the machine-readable docs/METRICS.md):
    name, synonyms, unit, how rows combine (pooled / sum / mean / latest), the
    model and column, its own dimensions and the finer sources that carry more
    (UA spend by country and platform in stg_ua_spend);
  * the formulas of docs/METRICS.md (metric_docs.yaml, generated from the
    Markdown by `python -m studio_pulse.brain.semantic --write`; a test fails
    when the two differ);
  * the grain, time column and window of every model (MODELS below; the tests
    check each grain is unique on the demo warehouse);
  * the warehouse itself: the columns present and the values of every filter
    dimension (networks, platforms, countries, versions, tiers, stores ...).

`context()` is what the model sees instead of a raw dump of tables; the
router and consistency.py use the same objects to apply and check filters.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .catalog import Catalog, Metric, load

DOCS_FILE = "metric_docs.yaml"


@dataclass(frozen=True)
class ModelSpec:
    grain: tuple[str, ...]
    time: str | None = None
    window: str | None = None  # for snapshot tables: what period one row covers
    window_days: int | None = None
    implied: tuple[tuple[str, str], ...] = ()  # filters every row satisfies (vitals: Android)

    def window_text(self, lang: str) -> str | None:
        if not self.window:
            return None
        return WINDOWS_TR.get(self.window, self.window) if lang == "tr" else self.window


MODELS: dict[str, ModelSpec] = {
    "fct_app_daily": ModelSpec(("app_id", "date"), "date"),
    "fct_app_summary": ModelSpec(("app_id",), None, "the 7 days ending at the app's last day with DAU (and the 7 days before)", 7),
    "fct_retention": ModelSpec(("app_id", "cohort_date", "day_n"), "cohort_date"),
    "fct_retention_summary": ModelSpec(("app_id", "day_n"), None, "the 28 most recent cohorts complete for that day N"),
    "fct_cohort_ltv": ModelSpec(("app_id", "cohort_date", "day_n"), "cohort_date"),
    "fct_ltv_curve": ModelSpec(("app_id", "day_n"), None, "every cohort of the loaded window eligible for day N"),
    "fct_level_funnel": ModelSpec(("app_id", "level"), None, "the whole loaded window"),
    "fct_perf_daily": ModelSpec(("app_id", "date", "device_tier"), "date"),
    "fct_perf_by_tier": ModelSpec(("app_id", "device_tier"), None, "the last 28 days of perf data", 28),
    "fct_perf_by_version": ModelSpec(("app_id", "app_version", "device_tier"), None, "the whole loaded window"),
    "fct_load_daily": ModelSpec(("app_id", "date", "kind"), "date"),
    "fct_load_times": ModelSpec(("app_id", "kind", "scene"), None, "the last 28 days of load data", 28),
    "fct_ua_daily": ModelSpec(("app_id", "date", "network", "campaign_key"), "date"),
    "fct_ua_summary": ModelSpec(("app_id", "network", "campaign_key"), None, "the last 30 days of spend", 30),
    "fct_ua_cohort_roas": ModelSpec(("app_id", "network", "campaign_key", "cohort_date"), "cohort_date"),
    "fct_ua_roas_curve": ModelSpec(("app_id", "network", "campaign_key", "day_n"), None, "every spend cohort that reached day N"),
    "fct_creative_scorecard": ModelSpec(("app_id", "network", "creative_key"), None, "every day of the creative"),
    "fct_review_daily": ModelSpec(("app_id", "store", "date"), "date"),
    "fct_review_topics": ModelSpec(("app_id", "topic"), None, "the last 30 days of reviews (and the 30 before)", 30),
    "fct_rating_by_version": ModelSpec(("app_id", "store", "app_version"), None, "every review of the version"),
    "fct_android_vitals_daily": ModelSpec(("app_id", "date"), "date", implied=(("platform", "ANDROID"), ("store", "google_play"))),
    "fct_source_freshness": ModelSpec(("app_id", "source"), None, "the last load"),
    "fct_experiment_arms": ModelSpec(("app_id", "experiment_id", "variant"), None, "the experiment so far"),
    "fct_experiment_tests": ModelSpec(("app_id", "experiment_id", "variant", "test"), None, "the experiment so far"),
    "fct_pltv_summary": ModelSpec(("app_id", "model", "horizon_day"), None, "the latest 28 install cohorts (a model prediction)"),
    "fct_pltv_curve": ModelSpec(("app_id", "day_n", "model"), None, "the latest 28 install cohorts (a model prediction)"),
    "stg_cohort_days": ModelSpec(("app_id", "platform", "cohort_date", "day_n"), "cohort_date"),
    "stg_cohort_dims": ModelSpec(("app_id", "cohort_date", "grouping_set", "platform", "country", "app_version", "network", "device_tier"), "cohort_date"),
    "stg_segment_retention": ModelSpec(("app_id", "cohort_date", "property", "value", "day0_sessions"), "cohort_date"),
    "stg_active_users": ModelSpec(("app_id", "platform", "date"), "date"),
    "stg_ua_spend": ModelSpec(("app_id", "date", "network", "campaign_key", "adset_id", "ad_id", "country", "platform"), "date"),
    "stg_store_daily": ModelSpec(("app_id", "store", "date"), "date"),
    "meta_apps": ModelSpec(("app_id",)),
    "fct_sdk_apps": ModelSpec(("app_id", "platform")),
    "fct_sdk_level_funnel": ModelSpec(("app_id", "level"), None, "the whole loaded window"),
    "fct_sdk_fail_heatmap": ModelSpec(("app_id", "level", "pos_space", "bin_x", "bin_y")),
    "fct_sdk_settings": ModelSpec(("app_id", "setting"), None, "the whole loaded window"),
    "fct_sdk_settings_when": ModelSpec(("app_id", "setting", "session_bucket")),
    "fct_sdk_purchases": ModelSpec(("app_id", "product_id"), None, "the whole loaded window"),
    "fct_sdk_ad_revenue_daily": ModelSpec(("app_id", "date", "ad_format"), "date"),
    "fct_sdk_experiments": ModelSpec(("app_id", "experiment_key", "variation")),
    "fct_sdk_errors_daily": ModelSpec(("app_id", "date"), "date"),
    "fct_sdk_errors": ModelSpec(("app_id", "fingerprint"), None, "the whole loaded window"),
    "fct_sdk_data_quality": ModelSpec(("app_id",), None, "the whole loaded window"),
}  # fmt: skip

WINDOWS_TR = {
    "the 7 days ending at the app's last day with DAU (and the 7 days before)": "uygulamanın DAU verisi olan son gününde biten 7 gün (ve önceki 7 gün)",
    "the 28 most recent cohorts complete for that day N": "o N. gün için tamamlanmış en yeni 28 kohort",
    "every cohort of the loaded window eligible for day N": "yüklenen penceredeki N. güne uygun her kohort",
    "the whole loaded window": "yüklenen pencerenin tamamı",
    "the last 28 days of perf data": "performans verisinin son 28 günü",
    "the last 28 days of load data": "yükleme verisinin son 28 günü",
    "the last 30 days of spend": "harcamanın son 30 günü",
    "every spend cohort that reached day N": "N. güne ulaşan her harcama kohortu",
    "every day of the creative": "kreatifin her günü",
    "the last 30 days of reviews (and the 30 before)": "yorumların son 30 günü (ve önceki 30 gün)",
    "every review of the version": "sürümün her yorumu",
    "the last load": "son yükleme",
    "the experiment so far": "deneyin şu ana kadarki kısmı",
    "the latest 28 install cohorts (a model prediction)": "en yeni 28 kurulum kohortu (bir model tahmini)",
}  # fmt: skip

# filter dimension -> column name (the same in every model that carries it)
DIM_COLUMNS: dict[str, str] = {
    "app": "app_id",
    "platform": "platform",
    "country": "country",
    "app_version": "app_version",
    "network": "network",
    "device_tier": "device_tier",
    "store": "store",
    "level": "level",
    "ad_format": "ad_format",
    "setting": "setting",
    "product_id": "product_id",
    "topic": "topic",
    "campaign_name": "campaign_name",
    "creative_key": "creative_key",
    "experiment_id": "experiment_id",
    "day_n": "day_n",
    "horizon_day": "horizon_day",
    "source": "source",
    "type": "type",
}
# where a dimension's values live, for "which values exist" (a helpful refusal)
VALUE_HOMES: dict[str, tuple[str, ...]] = {
    "network": ("stg_ua_spend", "fct_ua_daily", "stg_cohort_dims"),
    "platform": ("stg_active_users", "stg_cohort_days"),
    "country": ("stg_ua_spend", "stg_cohort_dims"),
    "app_version": ("stg_cohort_dims", "fct_perf_by_version", "fct_rating_by_version"),
    "device_tier": ("fct_perf_by_tier", "stg_cohort_dims"),
    "store": ("stg_store_daily", "fct_review_daily", "fct_rating_by_version"),
    "level": ("fct_level_funnel",),
    "ad_format": ("fct_sdk_ad_revenue_daily",),
    "setting": ("fct_sdk_settings",),
    "product_id": ("fct_sdk_purchases",),
}
JOINS = (
    "Every table has app_id (the ids under `apps`); join tables on app_id and, for daily "
    "tables, on date. Cohort tables are keyed by cohort_date (the install day) and day_n "
    "(days since install). meta_apps has each app's display name and genre. Snapshot tables "
    "(no date column) cover the window given under `window`."
)
RULES = (
    "Apply every filter the question names (app, network, platform, country, version, tier, "
    "store, level, period). If a filter has no column in the tables you use, or its value is "
    'not among `values`, answer {"sql": null}: never drop a filter.',
    "Periods are relative to `newest_data_day`: 'yesterday' is that day, 'last 7 days' the 7 "
    "days ending on it. Never use CURRENT_DATE or now().",
    "Combine rows the way `combine` says (pooled ratios are SUM(numerator) / SUM(denominator), "
    "not the mean of daily ratios).",
    "Country values are ISO codes in stg_ua_spend and English names in stg_cohort_dims.",
)


@cache
def metric_docs() -> dict[str, dict[str, str]]:
    """`model.column` -> {metric, formula} from docs/METRICS.md (generated file)."""
    text = resources.files("studio_pulse.brain").joinpath(DOCS_FILE).read_text(encoding="utf-8")
    return yaml.safe_load(text)["columns"]


_REF = re.compile(r"`((?:fct|stg)_[a-z0-9_]+)?(\.[a-z0-9_]+(?:/[a-z0-9_]+)*)?`")


def docs_from_markdown(text: str) -> dict[str, dict[str, str]]:
    """Every metric table row of METRICS.md with a `model.column` (or `.column` after one)."""
    out: dict[str, dict[str, str]] = {}
    model = None
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Metric", "Day", "Test"):
            continue
        name, formula, refs = cells[0], cells[1], cells[-1]
        for m in _REF.finditer(refs):
            if m.group(1):
                model = m.group(1)
            if not model:
                continue
            cols = (m.group(2) or "").lstrip(".")
            if not cols:
                continue
            base = cols.split("/")[0]
            prefix = re.sub(r"[a-z0-9]+$", "", base)  # roas_d0/d7/d30 -> roas_
            for i, c in enumerate(cols.split("/")):
                col = c if i == 0 else prefix + c
                out[f"{model}.{col}"] = {"metric": name, "formula": formula}
    return dict(sorted(out.items()))


def render_docs(text: str) -> str:
    docs = docs_from_markdown(text)
    head = (
        "# Generated from docs/METRICS.md by `python -m studio_pulse.brain.semantic --write`.\n"
        "# Do not edit: tests/brain/test_brain_semantic.py fails when it differs.\n"
    )
    return head + yaml.safe_dump({"columns": docs}, allow_unicode=True, sort_keys=True, width=200)


@dataclass
class Layer:
    catalog: Catalog
    schema: dict[str, dict[str, str]]
    values: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # model -> dim -> values

    def dims_of(self, model: str) -> list[str]:
        cols = self.schema.get(model, {})
        out = [d for d, c in DIM_COLUMNS.items() if c in cols and d != "app"]
        out += [d for d, _ in MODELS.get(model, ModelSpec(())).implied if d not in out]
        return out

    def implied(self, model: str) -> dict[str, str]:
        return dict(MODELS.get(model, ModelSpec(())).implied)

    def metric_entry(self, m: Metric) -> dict[str, Any]:
        """Everything the layer knows about one metric (tests, docs, the UI)."""
        spec = MODELS.get(m.model, ModelSpec(()))
        doc = metric_docs().get(f"{m.model}.{m.value}")
        entry: dict[str, Any] = {
            "name": m.name["en"],
            "definition": m.definition,
            "unit": m.unit,
            "table": m.model,
            "column": m.value,
            "grain": list(spec.grain),
            "combine": self.combine(m),
        }
        if doc:
            entry["formula"] = doc["formula"]
        if m.filter:
            entry["always_where"] = m.filter
        if m.time:
            entry["time"] = m.time
        elif spec.window:
            entry["window"] = spec.window
        filters = {d: m.model for d in m.dims}
        for src in m.sources:
            for d in src.dims:
                filters.setdefault(d, src.model)
        if filters:
            entry["filters"] = filters
        return entry

    @staticmethod
    def combine(m: Metric) -> str:
        if m.aggregation == "latest":
            return "a stored snapshot row per key: filter it, do not aggregate"
        return f"{m.aggregate_expr()} ({m.aggregation})"

    def metric_line(self, m: Metric) -> str:
        """One compact line per metric for the model's prompt."""
        spec = MODELS.get(m.model, ModelSpec(()))
        bits = [f"{m.name['en']} [{m.unit}]: {m.definition.rstrip('.')}", f"{m.model}.{m.value}"]
        if m.filter:
            bits.append(f"where {m.filter}")
        bits.append(f"combine {self.combine(m)}")
        if m.time:
            bits.append(f"time {m.time}")
        elif spec.window:
            bits.append(f"window: {spec.window}")
        if m.dims:
            bits.append("split/filter by " + ", ".join(m.dims))
        for src in m.sources:
            bits.append(f"{src.model} adds " + ", ".join(d for d in src.dims if d not in m.dims))
        return "; ".join(bits)


def build(session: Any, catalog: Catalog | None = None, *, with_values: bool = True) -> Layer:
    cat = catalog or load()
    schema = session.schema() if hasattr(session, "schema") else {}
    full = _full_schema(session) or schema
    layer = Layer(cat, full)
    if with_values:
        for model in sorted(set(VALUE_HOMES_ALL) & set(full)):
            for d in layer.dims_of(model):
                col = DIM_COLUMNS.get(d)
                if not col or col not in full[model] or d in ("day_n", "level", "campaign_name"):
                    continue
                try:
                    rows = session.run_trusted(
                        f"SELECT DISTINCT CAST({col} AS VARCHAR) FROM {model} "
                        f"WHERE {col} IS NOT NULL ORDER BY 1 LIMIT 60"
                    ).rows
                except Exception:
                    continue
                layer.values.setdefault(model, {})[d] = [r[0] for r in rows]
    return layer


VALUE_HOMES_ALL = sorted({t for ts in VALUE_HOMES.values() for t in ts} | {"fct_ua_summary"})


def _full_schema(session: Any) -> dict[str, dict[str, str]]:
    """Every table of the file (the router reads staging tables through trusted SQL).
    Read on the session's connection: the result has more rows than the answer row cap."""
    cached = getattr(session, "_full_schema_cache", None)
    if cached is not None:
        return cached
    sql = (
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
    )
    try:
        con = getattr(session, "con", None)
        rows = con.execute(sql).fetchall() if con is not None else session.run_trusted(sql).rows
    except Exception:
        return {}
    out: dict[str, dict[str, str]] = {}
    for t, c, ty in rows:
        out.setdefault(t, {})[c] = ty
    try:
        session._full_schema_cache = out
    except AttributeError:
        pass
    return out


def values_for(session: Any, dim: str, app: str | None) -> list[str]:
    """Values of a dimension that have rows (for one app, or all apps)."""
    col = DIM_COLUMNS.get(dim)
    if not col:
        return []
    full = _full_schema(session)
    out: list[str] = []
    for model in VALUE_HOMES.get(dim, ()):
        if model not in full or col not in full[model]:
            continue
        sql = f"SELECT DISTINCT CAST({col} AS VARCHAR) FROM {model} WHERE {col} IS NOT NULL"
        params: list[Any] = []
        if app:
            sql += " AND app_id = ?"
            params.append(app)
        if model == "stg_cohort_dims" and dim != "platform":
            sql += " AND grouping_set = ?"
            params.append(dim)
        try:
            rows = session.run_trusted(sql + " ORDER BY 1 LIMIT 60", params).rows
        except Exception:
            continue
        for (v,) in rows:
            if v not in out and v not in ("(all)", "all", "unknown"):
                out.append(v)
    return out


def context(
    layer: Layer,
    *,
    apps: dict[str, str],
    genres: dict[str, str] | None = None,
    as_of: dt.date | None = None,
    newest_day: dt.date | None = None,
    allowed: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """The model's view of the warehouse: one line per metric (definition, table and column,
    how rows combine, time or window, filters), the tables with grain, window and the stored
    values of their filter columns, join paths, rules and today's date."""
    tables: dict[str, Any] = {}
    for t, cols in sorted(layer.schema.items()):
        if allowed is not None and t not in allowed:
            continue
        spec = MODELS.get(t)
        entry: dict[str, Any] = {"columns": ", ".join(cols)}
        if spec:
            entry["grain"] = ", ".join(spec.grain)
            if spec.window:
                entry["window"] = spec.window
        vals = layer.values.get(t)
        if vals:
            entry["values"] = {d: v[:15] for d, v in vals.items() if d not in ("source",)}
        tables[t] = entry
    metrics = {
        mid: layer.metric_line(m)
        for mid, m in layer.catalog.metrics.items()
        if allowed is None or m.model in allowed
    }
    out: dict[str, Any] = {
        "apps": {
            a: f"{n}{f' ({genres[a]})' if genres and a in genres else ''}" for a, n in apps.items()
        },
        "metrics": metrics,
        "tables": tables,
        "joins": JOINS,
        "rules": list(RULES),
    }
    if as_of:
        out["today"] = as_of.isoformat()
    if newest_day:
        out["newest_data_day"] = newest_day.isoformat()
    return out


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="regenerate brain/metric_docs.yaml from docs/METRICS.md"
    )
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root)
    text = (root / "docs" / "METRICS.md").read_text(encoding="utf-8")
    out = render_docs(text)
    target = root / "src" / "studio_pulse" / "brain" / DOCS_FILE
    if args.write:
        target.write_text(out, encoding="utf-8")
        print(f"wrote {target}")
        return 0
    same = target.exists() and target.read_text(encoding="utf-8") == out
    print("up to date" if same else f"{target} differs from docs/METRICS.md")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(_main())
