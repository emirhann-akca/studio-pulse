"""Metric catalog (metrics.yaml): load, validate, resolve names, build SQL.

Every SQL string built here comes from catalog fields (reviewed text in the
package) and validated identifiers; nothing a user or a model typed is pasted
into it. Values (app ids, dates) are bound as parameters.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

UNITS = ("count", "pct", "usd", "ms", "mb", "ratio", "days")
AGGREGATIONS = ("pooled", "sum", "mean_of_daily", "latest")
BASELINES = ("pooled", "mean_of_daily")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Metric:
    id: str
    name: dict[str, str]
    synonyms: dict[str, tuple[str, ...]]
    unit: str
    better: str
    model: str
    value: str
    aggregation: str
    definition: str
    time: str | None = None
    filter: str | None = None
    dims: tuple[str, ...] = ()
    numerator: str | None = None
    denominator: str | None = None
    sample: str | None = None
    alert_dims: tuple[str, ...] | None = None  # None: not an alert metric
    alert_baseline: str | None = None
    pull_dims: tuple[str, ...] = ()

    @property
    def is_alert_metric(self) -> bool:
        return self.alert_dims is not None

    def label(self, lang: str) -> str:
        return self.name.get(lang) or self.name["en"]

    def where(self, extra: list[str] | None = None) -> str:
        parts = [f"({self.filter})"] if self.filter else []
        parts += extra or []
        return " AND ".join(parts) if parts else "TRUE"

    # -- SQL ---------------------------------------------------------------
    def series_sql(self) -> str:
        """The per-(app, time[, alert dims]) series alerts.py evaluates.

        Rows of a model with more keys than the series keeps (UA spend per
        network and campaign) are rolled up with the metric's aggregation.
        """
        if self.time is None:
            raise CatalogError(f"{self.id} has no time column")
        keep = list(self.alert_dims or ())
        rollup = [d for d in self.dims if d not in keep]
        if not rollup:
            cols = ["app_id", f"{self.time} AS date", f"{self.value} AS value"]
            cols.append(f"{self.sample} AS sample" if self.sample else "NULL AS sample")
            cols += keep
            return f"SELECT {', '.join(cols)} FROM {self.model} WHERE {self.where()}"
        value = self.aggregate_expr()
        sample = f"SUM({self.sample})" if self.sample else "NULL"
        cols = ["app_id", f"{self.time} AS date", f"{value} AS value", f"{sample} AS sample"]
        cols += keep
        group = ", ".join(str(i + 1) for i in range(2)) + "".join(f", {d}" for d in keep)
        return f"SELECT {', '.join(cols)} FROM {self.model} WHERE {self.where()} GROUP BY {group}"

    def aggregate_expr(self) -> str:
        """SQL aggregate combining rows into one number (pooled / sum / mean)."""
        if self.aggregation == "pooled":
            return f"CAST(SUM({self.numerator}) AS DOUBLE) / NULLIF(SUM({self.denominator}), 0)"
        if self.aggregation == "sum":
            return f"SUM({self.value})"
        if self.aggregation == "mean_of_daily":
            return f"AVG({self.value})"
        raise CatalogError(f"{self.id}: aggregation {self.aggregation} has no aggregate")

    def n_expr(self) -> str | None:
        if self.aggregation == "pooled":
            return f"SUM({self.denominator})"
        if self.aggregation in ("mean_of_daily", "sum"):
            return None  # a sum is itself a count; a daily mean reports its days
        if self.sample:
            return f"SUM({self.sample})"
        return None

    def period_sql(
        self,
        *,
        app_id: str | None = None,
        start: dt.date | None = None,
        end: dt.date | None = None,
        by: list[str] | None = None,
        dim_filter: dict[str, Any] | None = None,
    ) -> tuple[str, list[Any]]:
        """One number per app (and per `by` dimension) over [start, end].

        Snapshot metrics (aggregation latest) return their rows as stored,
        filtered by app and dimension values.
        """
        by = list(by or [])
        for d in by + list(dim_filter or {}):
            if d not in self.dims:
                raise CatalogError(f"{self.id} has no dimension {d!r}")
        conds: list[str] = []
        params: list[Any] = []
        if app_id:
            conds.append("app_id = ?")
            params.append(app_id)
        for k, v in (dim_filter or {}).items():
            conds.append(f"CAST({k} AS VARCHAR) = ?")
            params.append(str(v))
        if self.time and start:
            conds.append(f"{self.time} >= ?")
            params.append(start)
        if self.time and end:
            conds.append(f"{self.time} <= ?")
            params.append(end)
        if self.aggregation == "latest":
            cols = ["app_id", *self.dims, f"{self.value} AS value"]
            if self.sample:
                cols.append(f"{self.sample} AS n")
            order = ", ".join(["app_id", *self.dims])
            if self.time:
                cols.insert(1, self.time)
                conds.append(
                    f"{self.time} = (SELECT MAX({self.time}) FROM {self.model} m2 "
                    f"WHERE m2.app_id = {self.model}.app_id AND {self.where()})"
                )
            sql = f"SELECT {', '.join(cols)} FROM {self.model} WHERE {self.where(conds)} ORDER BY {order}"
            return sql, params
        conds.append(f"({self.value}) IS NOT NULL")
        n = self.n_expr()
        cols = ["app_id", *by, f"{self.aggregate_expr()} AS value"]
        if n:
            cols.append(f"{n} AS n")
        if self.time:
            cols += [f"MIN({self.time}) AS first_day", f"MAX({self.time}) AS last_day"]
            if self.aggregation == "mean_of_daily":
                cols.append(f"COUNT(DISTINCT {self.time}) AS days")
        group = ", ".join(["app_id", *by])
        sql = (
            f"SELECT {', '.join(cols)} FROM {self.model} WHERE {self.where(conds)} "
            f"GROUP BY {group} ORDER BY {group}"
        )
        return sql, params


@dataclass(frozen=True)
class Dimension:
    id: str
    name: dict[str, str]
    synonyms: dict[str, tuple[str, ...]]
    tables: tuple[str, ...] = ()


@dataclass
class Catalog:
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    not_metrics: tuple[str, ...] = ()
    _terms: list[tuple[str, str, str]] = field(default_factory=list, repr=False)

    def get(self, metric_id: str) -> Metric:
        try:
            return self.metrics[metric_id]
        except KeyError:
            raise CatalogError(f"unknown metric {metric_id!r}") from None

    def models(self) -> set[str]:
        return {m.model for m in self.metrics.values()}

    def terms(self) -> list[tuple[str, str, str]]:
        """(normalized phrase, kind, id) for every name and synonym, longest first."""
        if not self._terms:
            out: list[tuple[str, str, str]] = []
            for m in self.metrics.values():
                for lang in ("en", "tr"):
                    for phrase in (m.name.get(lang, ""), *m.synonyms.get(lang, ())):
                        if phrase:
                            out.append((normalize(phrase), "metric", m.id))
                out.append((normalize(m.id.replace("_", " ")), "metric", m.id))
            for d in self.dimensions.values():
                for lang in ("en", "tr"):
                    for phrase in (d.name.get(lang, ""), *d.synonyms.get(lang, ())):
                        if phrase:
                            out.append((normalize(phrase), "dimension", d.id))
            out.sort(key=lambda t: -len(t[0]))
            self._terms = out
        return self._terms

    def find(self, text: str) -> dict[str, list[str]]:
        """Metric and dimension ids named in a question (longest phrase wins)."""
        norm = " " + normalize(text) + " "
        found: dict[str, list[str]] = {"metric": [], "dimension": []}
        taken: list[tuple[int, int]] = []
        for phrase, kind, ident in self.terms():
            if not phrase:
                continue
            for m in re.finditer(r"(?<=\s)" + re.escape(phrase) + r"(?=\s)", norm):
                span = (m.start(), m.end())
                if any(a < span[1] and span[0] < b for a, b in taken):
                    continue
                taken.append(span)
                if ident not in found[kind]:
                    found[kind].append(ident)
        return found

    def alert_baseline(self, metric_id: str) -> str:
        m = self.metrics.get(metric_id)
        return m.alert_baseline if m and m.alert_baseline else "mean_of_daily"


_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})


def normalize(text: str) -> str:
    """Case- and accent-folded words for matching (Turkish dotted/dotless i aware)."""
    t = text.translate(_TR_LOWER).lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("ı", "i")
    t = re.sub(r"[^a-z0-9%/.+-]+", " ", t)
    t = re.sub(r"(?<![0-9])[./+-]|[./+-](?![0-9])", " ", t)
    return " ".join(t.split())


def _ident(v: str, what: str) -> str:
    if not _IDENT.match(v or ""):
        raise CatalogError(f"invalid {what}: {v!r}")
    return v


def _lang_map(raw: Any, what: str) -> dict[str, str]:
    if not isinstance(raw, dict) or "en" not in raw or "tr" not in raw:
        raise CatalogError(f"{what}: needs en and tr")
    return {str(k): str(v) for k, v in raw.items()}


def _syn(raw: Any) -> dict[str, tuple[str, ...]]:
    raw = raw or {}
    return {str(k): tuple(str(x) for x in (v or [])) for k, v in raw.items()}


def parse(data: dict[str, Any]) -> Catalog:
    dims: dict[str, Dimension] = {}
    for did, d in (data.get("dimensions") or {}).items():
        _ident(did, "dimension id")
        dims[did] = Dimension(
            did,
            _lang_map(d.get("name"), f"dimension {did}"),
            _syn(d.get("synonyms")),
            tuple(d.get("tables") or ()),
        )
    metrics: dict[str, Metric] = {}
    known = {
        "name", "synonyms", "unit", "better", "model", "time", "value", "filter", "dims",
        "aggregation", "numerator", "denominator", "sample", "alert", "pull_dims", "definition",
    }  # fmt: skip
    for mid, m in (data.get("metrics") or {}).items():
        _ident(mid, "metric id")
        unknown = set(m) - known
        if unknown:
            raise CatalogError(f"metric {mid}: unknown keys {sorted(unknown)}")
        for req in ("name", "unit", "model", "value", "aggregation", "definition"):
            if req not in m:
                raise CatalogError(f"metric {mid}: needs {req}")
        if m["unit"] not in UNITS:
            raise CatalogError(f"metric {mid}: unit must be one of {UNITS}")
        if m["aggregation"] not in AGGREGATIONS:
            raise CatalogError(f"metric {mid}: aggregation must be one of {AGGREGATIONS}")
        if m["aggregation"] == "pooled" and not (m.get("numerator") and m.get("denominator")):
            raise CatalogError(f"metric {mid}: pooled needs numerator and denominator")
        alert = m.get("alert")
        if alert is not None:
            if alert.get("baseline", "mean_of_daily") not in BASELINES:
                raise CatalogError(f"metric {mid}: alert baseline must be one of {BASELINES}")
            if alert.get("baseline") == "pooled" and not m.get("sample"):
                raise CatalogError(f"metric {mid}: a pooled alert baseline needs a sample")
        for d in [*(m.get("dims") or []), *(m.get("pull_dims") or [])]:
            if d not in dims and d not in ("campaign_name", "day_n"):
                raise CatalogError(f"metric {mid}: unknown dimension {d!r}")
        metrics[mid] = Metric(
            id=mid,
            name=_lang_map(m["name"], f"metric {mid}"),
            synonyms=_syn(m.get("synonyms")),
            unit=m["unit"],
            better=m.get("better", "none"),
            model=_ident(m["model"], "model"),
            value=str(m["value"]),
            aggregation=m["aggregation"],
            definition=str(m["definition"]),
            time=_ident(m["time"], "time column") if m.get("time") else None,
            filter=m.get("filter"),
            dims=tuple(m.get("dims") or ()),
            numerator=m.get("numerator"),
            denominator=m.get("denominator"),
            sample=m.get("sample"),
            alert_dims=tuple(alert.get("dims") or ()) if alert is not None else None,
            alert_baseline=alert.get("baseline", "mean_of_daily") if alert is not None else None,
            pull_dims=tuple(m.get("pull_dims") or ()),
        )
    return Catalog(metrics, dims, tuple(data.get("not_metrics") or ()))


@lru_cache(maxsize=1)
def load() -> Catalog:
    text = (
        resources.files("studio_pulse.brain").joinpath("metrics.yaml").read_text(encoding="utf-8")
    )
    return parse(yaml.safe_load(text))


def alert_baseline(metric_id: str) -> str:
    """How alerts.py combines baseline / evaluated points for this metric."""
    return load().alert_baseline(metric_id)
