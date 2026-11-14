"""The catalog tool of the question router: one metric, every filter of the question.

A mixin of qa.Router (split out of qa.py): metric choice, filters resolved to the
values each model stores, finer source models, portfolio totals, rankings,
trends against the previous period, the dashboard headline next to a period
answer, and retention by platform / country / version / source / tier from the
cohort_dims pull (stg_cohort_days for the platform split of SDK apps).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import replace
from typing import Any

from . import consistency, fmt, intent, narrate, semantic, verify
from .answer import (
    _BREAKDOWN,
    _COLUMN,
    _OVERALL,
    _PEOPLE,
    _QUIT_WORD,
    _RETENTION_IDS,
    _RETENTION_WORD,
    _TREND,
    _UNCOLLECTED,
    _WHY,
    AGG_TEXT,
    MIN_RANK_N,
    TEXTS,
    Answer,
)  # fmt: skip
from .catalog import CatalogError, Metric
from .evidence import Evidence, Fact
from .executor import Result


class CatalogAnswers:
    """Methods of Router that answer from the metric catalog (self is a qa.Router)."""

    # -- 2. catalog tool ---------------------------------------------------------
    def _pick(self, ids: list[str], it: intent.Intent | None, norm: str) -> list[Metric]:
        """The catalog metrics a question names, adjusted by what else it asks: the p95
        sibling of a p50 metric, day-N retention for any N, network installs when an ad
        network is named, the per-format SDK metric when an ad format is named."""
        out: list[Metric] = []
        p95 = bool(re.search(r"\b(p95|95th|yuzde 95)\b", norm))
        for mid in ids:
            m = self.catalog.get(mid)
            if p95 and "p50" in m.id and m.id.replace("p50", "p95") in self.catalog.metrics:
                m = self.catalog.get(m.id.replace("p50", "p95"))
            if all(x.id != m.id for x in out):
                out.append(m)
        if it is None:
            return out
        ret_word = _RETENTION_WORD.search(norm)
        if it.day_ns and ret_word and (not out or out[0].id in _RETENTION_IDS):
            n = it.day_ns[0]
            mid = f"d{n}_retention"
            if mid in self.catalog.metrics:
                m = self.catalog.get(mid)
            else:
                base = self.catalog.get("d1_retention")
                m = replace(
                    base,
                    id=mid,
                    name={"en": f"D{n} retention", "tr": f"D{n} elde tutma"},
                    synonyms={},
                    filter=f"day_n = {int(n)} AND is_complete",
                    definition=f"users of the install cohort active exactly on day {int(n)} / cohort size",
                    alert_dims=None,
                    alert_baseline=None,
                    pull_dims=(),
                    headline=None,
                )
            out = [m, *[x for x in out if x.id not in _RETENTION_IDS]]
        if it.day_ns and len(out) > 1 and "day_n" not in out[0].dims:
            dayed = [m for m in out if "day_n" in m.dims or "horizon_day" in m.dims]
            if dayed:  # "revenue per user at day 14": the curve, not ARPDAU
                out = [dayed[0], *[m for m in out if m.id != dayed[0].id]]
        filters = it.filters()
        swap = {}
        if any(v != "organic" for v in filters.get("network", [])):
            swap.update({"new_users": "ua_installs", "store_installs": "ua_installs"})
        if "ad_format" in filters:
            swap.update({"ad_revenue_usd": "sdk_ad_revenue", "revenue_usd": "sdk_ad_revenue",
                         "ecpm_usd": "sdk_ad_ecpm"})  # fmt: skip
        if "creative_key" in it.by:
            swap.update({"ua_ipm": "creative_ipm"})
        if not out and _PEOPLE.search(norm):
            out = [self.catalog.get("dau")]  # "how many iOS users", "kaç oyuncuya ulaştı"
        if _QUIT_WORD.search(norm) and ("level" in it.by or "level" in filters):
            target = "level_churned_users" if "level" in it.by else "level_churn_rate"
            if not out or "level" not in out[0].dims:
                out = [self.catalog.get(target), *out]
            elif out[0].id in ("level_churn_rate", "users_reached") and "level" in it.by:
                out = [self.catalog.get(target), *out]
        out = [self.catalog.get(swap[m.id]) if m.id in swap else m for m in out]
        uniq: dict[str, Metric] = {}
        for m in out:
            uniq.setdefault(m.id, m)
        return list(uniq.values())

    def _resolve(self, model: str, dim: str, vals: list[str], app: str | None) -> list[str]:
        """Each asked value as the model stores it (country codes vs names, network aliases)."""
        col = semantic.DIM_COLUMNS.get(dim, dim)
        cols = self.s.run_trusted(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [model]
        ).rows
        if col not in {c for (c,) in cols}:
            return list(vals)
        sql = f"SELECT DISTINCT CAST({col} AS VARCHAR) FROM {model} WHERE {col} IS NOT NULL"
        params: list[Any] = []
        if app:
            sql += " AND app_id = ?"
            params.append(app)
        have = [r[0] for r in self.s.run_trusted(sql, params).rows]
        out = []
        for v in vals:
            al = consistency.aliases(dim, v)
            hit = next((h for h in have if h.lower() in al), None)
            out.append(hit if hit is not None else v)
        return out

    def _catalog(
        self,
        q: str,
        norm: str,
        lang: str,
        app: str | None,
        rng: tuple[dt.date, dt.date] | None = None,
        compare: list[str] | None = None,
        it: intent.Intent | None = None,
    ) -> Answer | None:
        found = self.catalog.find(q)
        metrics = self._pick(found["metric"], it, norm)
        dims = [d for d in found["dimension"] if d not in ("app_id", "date", "cohort_date")]
        if dims and it is not None and not _BREAKDOWN.search(norm):
            dims = []  # "mağazadan indirildi": the word names the metric, not a split
        days = self.find_days(norm)
        if it is not None and it.period is not None and it.period.days:
            days = it.period.days
        if (
            it is not None
            and it.period is not None
            and it.period.latest
            and not days
            and rng is None
        ):
            days = 1  # "latest", "en son": the newest day
        if not metrics and self.client is not None:
            routed = self._llm_route(q)
            if routed:
                metrics = [self.catalog.get(routed["metric"])]
                app = app or routed.get("app")
                if routed.get("by") and routed["by"] != "app_id":  # "by app" = the per-app rows
                    dims = [routed["by"]]
                days = days or routed.get("days")
        for word, (en, tr) in _UNCOLLECTED.items():
            if re.search(rf"\b{word}\b", norm) and metrics:
                return self._refuse(
                    q,
                    lang,
                    "uncollected",
                    {"dimension": Fact(en if lang == "en" else tr, "text")},
                    missing=en,
                )
        if not metrics:
            return None
        metric = metrics[0]
        stale = self._stale(q, lang, app)
        if stale is not None:
            return stale
        filters: dict[str, list[str]] = dict(it.filters()) if it else {}
        if it is not None and it.day_ns:
            for d in ("day_n", "horizon_day"):
                if d in metric.dims:
                    filters[d] = [str(it.day_ns[0])]
        if (
            it is not None
            and not it.day_ns
            and "day_n" in metric.dims
            and metric.model == "fct_ltv_curve"
        ):
            filters["day_n"] = [str(self._ltv_day(app))]  # the curve at the latest day <= 30
        for d, v in semantic.MODELS.get(metric.model, semantic.ModelSpec(())).implied:
            if d in filters and all(v.lower() in consistency.aliases(d, x) for x in filters[d]):
                filters.pop(d)  # Android vitals are Android: nothing to filter
        if not dims and it is not None:
            dims = [d for d in it.by if d != "app_id"]
        dims = [d for d in dims if d not in filters]
        by: list[str] = []
        for d in dims[:1]:
            d = _COLUMN.get(d, d)  # a dimension's id may differ from its column (campaign)
            if d in metric.filter_dims():
                by = [d]
            elif d in metric.pull_dims:
                pv = {k: v for k, v in filters.items() if k in metric.pull_dims}
                return self._pull_dim(q, lang, metric, d, app, days, pv)
            else:
                avail = [
                    self.catalog.dimensions[x].name[lang]
                    for x in metric.dims
                    if x in self.catalog.dimensions
                ]
                dname = self.catalog.dimensions[d].name[lang] if d in self.catalog.dimensions else d
                facts = {
                    "metric": Fact(metric.label(lang), "text"),
                    "dimension": Fact(dname, "text"),
                }
                if avail:
                    facts["available"] = Fact(", ".join(avail), "text")
                return self._refuse(q, lang, "no_dimension", facts, missing=d)
        pull = {
            k: v
            for k, v in filters.items()
            if k not in metric.filter_dims() and k in metric.pull_dims
        }
        if pull:
            return self._pull_dim(q, lang, metric, next(iter(pull)), app, days, pull)
        need = set(filters) | set(by)
        chosen = metric.for_dims(need) if need else metric
        if chosen is None:
            bad = [d for d in need if d not in metric.filter_dims()]
            facts = {
                "metric": Fact(metric.label(lang), "text"),
                "filters": Fact(
                    "; ".join(
                        f"{d} = {', '.join(consistency.display(d, v) for v in filters.get(d, []))}"
                        if d in filters
                        else d
                        for d in bad
                    ),
                    "text",
                ),
            }
            return self._refuse(
                q, lang, "filter_not_applied", facts, missing=bad[0] if bad else None
            )
        metric = chosen
        dim_filter = {d: self._resolve(metric.model, d, v, app) for d, v in filters.items()}
        rank = None
        if (
            it is not None
            and it.rank
            and not by
            and app is not None
            and metric.aggregation == "latest"
            and [d for d in metric.dims if d not in dim_filter]
        ):
            by = [next(d for d in metric.dims if d not in dim_filter)]  # "most frequent error"
        if it is not None and it.rank and (by or (not app and not compare)):
            rank = self._rank_order(norm, metric)
        conj = re.search(r"\b(and|ve|ile)\b", norm)
        if len(metrics) >= 2 and conj and not compare and not by and app is not None:
            return self._multi(q, lang, metrics[:3], app, days, rng, dim_filter)
        if (
            it is not None
            and (it.total or it.all_apps)
            and not app
            and not compare
            and not by
            and not rank
        ):
            total = self._total(q, lang, metric, days, rng, dim_filter)
            if total is not None:
                return total
        why = bool(_WHY.search(norm)) and not by and app is not None
        trend_words = bool(_TREND.search(norm)) or bool(it and it.trend)
        trend = (trend_words or why) and not by and app is not None
        overall = not days and rng is None and not trend and bool(_OVERALL.search(norm))
        ans = self._metric_answer(
            q, lang, metric, app, by, days, rng, compare, trend, overall,
            dim_filter=dim_filter, rank=rank, headline=not days and rng is None and not overall,
        )  # fmt: skip
        if (
            ans.route == "refused"
            and ans.refusal == "no_data"
            and metric.headline
            and not days
            and rng is None
            and not by
        ):  # no complete cohort in the window: the dashboard's pooled figure answers
            h = self.catalog.metrics.get(metric.headline[0])
            hf = {k: [str(v)] for k, v in metric.headline[1]}
            hf.update(dim_filter)
            if h is not None and all(d in h.dims for d in hf):
                alt = self._metric_answer(q, lang, h, app, [], None, dim_filter=hf)
                if alt.route != "refused":
                    ans = alt
        if why and ans.route == "catalog" and ans.evidence is not None:
            # one metric over time says whether it moved, not why: say so and point at the split
            splits = (*metric.dims, *metric.pull_dims)
            dim = next((d for d in ("platform", "country") if d in splits), None)
            hint = TEXTS["why_split" if dim else "why"][lang]
            dname = self.catalog.dimensions[dim].name[lang] if dim else ""
            ans.text = f"{ans.text} {hint.format(metric=metric.label(lang), app=app, dim=dname)}"
            ans.notes.append("why_not_inferred")
        return ans

    def _ltv_day(self, app: str | None) -> int:
        got = self._trusted(
            "SELECT MAX(day_n) FROM fct_ltv_curve WHERE day_n <= 30"
            + (" AND app_id = ?" if app else ""),
            [app] if app else [],
        )
        return int(got.rows[0][0]) if got and got.rows and got.rows[0][0] is not None else 30

    @staticmethod
    def _rank_order(norm: str, metric: Metric) -> str:
        """'asc' or 'desc' for "which ... lowest / cheapest / best / most"."""
        low = re.search(
            r"\b(lowest|least|cheapest|smallest|fewest|minimum|en (?:dusuk|az|ucuz|kucuk))\b", norm
        )
        best = re.search(r"\b(best|en iyi)\b", norm)
        worst = re.search(r"\b(worst|en kotu)\b", norm)
        if low:
            return "asc"
        if best:
            return "asc" if metric.better == "down" else "desc"
        if worst:
            return "desc" if metric.better == "down" else "asc"
        return "desc"

    def _multi(
        self,
        q: str,
        lang: str,
        metrics: list[Metric],
        app: str,
        days: int | None,
        rng: tuple[dt.date, dt.date] | None,
        dim_filter: dict[str, list[str]],
    ) -> Answer:
        """ "DAU and revenue yesterday": one answer per metric, one table."""
        parts: list[Answer] = []
        for m in metrics:
            usable = {d: v for d, v in dim_filter.items() if d in m.filter_dims()}
            mm = m.for_dims(set(usable)) if usable else m
            if mm is None:
                continue
            a = self._metric_answer(q, lang, mm, app, [], days, rng, dim_filter=usable)
            if a.route == "catalog" and a.evidence is not None:
                parts.append(a)
        if not parts:
            return self._refuse(q, lang, "no_metric", {}, missing="metric")
        if len(parts) == 1:
            return parts[0]
        ans = Answer(q, lang, "catalog", matched="+".join(p.matched or "" for p in parts))
        ans.text = " ".join(p.text for p in parts)
        ans.columns = ["metric", "value"]
        ans.rows = [[p.matched, p.rows[0][p.columns.index("value")]] for p in parts if p.rows]
        ans.sql = parts[0].sql
        ans.params = parts[0].params
        ans.units = {"value": "ratio"}
        facts: dict[str, Fact] = {}
        rows: list[list[Any]] = []
        for i, p in enumerate(parts):
            assert p.evidence is not None
            facts.update({f"m{i}.{k}": v for k, v in p.evidence.facts.items()})
            rows += p.evidence.rows
        ans.evidence = Evidence(
            id="catalog:multi", kind="answer", app_id=app, method="catalog metrics, one per name",
            sql=parts[0].sql, params=parts[0].params, columns=parts[0].columns, rows=rows, facts=facts,
        )  # fmt: skip
        ans.verified = verify.verified(ans.text, [p.evidence for p in parts if p.evidence], lang)
        return ans

    def _total(
        self,
        q: str,
        lang: str,
        metric: Metric,
        days: int | None,
        rng: tuple[dt.date, dt.date] | None,
        dim_filter: dict[str, list[str]],
    ) -> Answer | None:
        """ "Total DAU across all apps": one number for the portfolio (sum, daily sum, pool)."""
        if metric.aggregation == "latest" or not metric.time:
            return None
        got = self._trusted(
            f"SELECT MAX({metric.time}) FROM {metric.model} WHERE {metric.where()}", []
        )
        if got is None or not got.rows or got.rows[0][0] is None:
            return None
        last = got.rows[0][0]
        span = days or metric.default_days or (14 if metric.time == "cohort_date" else 7)
        start, end = last - dt.timedelta(days=span - 1), last
        if rng is not None:
            start, end = rng[0], min(rng[1], last)
        sql, params = metric.total_sql(start=start, end=end, dim_filter=dim_filter)
        res = self._trusted(sql, params)
        if res is None or not res.rows or res.rows[0][0] is None:
            return None
        ans = Answer(q, lang, "catalog", matched=f"{metric.id}:total")
        ans.units = {"value": metric.unit, "first_day": "date", "last_day": "date"}
        self._fill(ans, res, params)
        r0 = res.rows[0]
        cols = res.columns
        vunit = (
            "ratio"
            if metric.aggregation == "mean_of_daily" and metric.unit == "count"
            else metric.unit
        )
        agg = "sum" if metric.aggregation == "sum" else metric.aggregation
        facts = {
            "metric": Fact(metric.label(lang), "text"),
            "value": Fact(r0[cols.index("value")], vunit),
            "first_day": Fact(r0[cols.index("first_day")], "date"),
            "last_day": Fact(r0[cols.index("last_day")], "date"),
            "aggregation_text": Fact(
                AGG_TEXT[agg][lang]
                if metric.aggregation != "mean_of_daily"
                else {"en": "mean of the daily totals", "tr": "günlük toplamların ortalaması"}[
                    lang
                ],
                "text",
            ),
        }
        ev = self._evidence(
            f"catalog:{metric.id}:total",
            f"catalog metric {metric.id}, all apps",
            res,
            params,
            facts,
        )
        ev.metric, ev.aggregation, ev.definition = metric.id, metric.aggregation, metric.definition
        ans.evidence = ev
        ans.text = self._say(TEXTS["total"][lang], facts, ev, lang, {})
        ans.verified = verify.verified(ans.text, [ev], lang)
        return ans

    def _metric_answer(
        self,
        q: str,
        lang: str,
        metric: Metric,
        app: str | None,
        by: list[str],
        days: int | None,
        rng: tuple[dt.date, dt.date] | None = None,
        compare: list[str] | None = None,
        trend: bool = False,
        overall: bool = False,
        *,
        dim_filter: dict[str, list[str]] | None = None,
        rank: str | None = None,
        headline: bool = False,
    ) -> Answer:
        start = end = None
        dim_filter = dict(dim_filter or {})
        if metric.time and metric.aggregation != "latest":
            cond = f"WHERE {metric.where(['app_id = ?'] if app else [])}"
            if metric.default_days and not rng and not days:
                # the UA page's window: the 30 days ending at the newest spend day, even when
                # a cohort metric (ROAS) has no value yet for its newest cohorts
                cond = "WHERE app_id = ?" if app else ""
            got = self._trusted(
                f"SELECT MAX({metric.time}), MIN({metric.time}) FROM {metric.model} {cond}",
                [app] if app else [],
            )
            if got is None:
                return self._refuse(q, lang, "schema", {}, missing=metric.id)
            last, first_data = got.rows[0][0], got.rows[0][1]
            if last is None:
                return self._no_data(Answer(q, lang, "catalog", matched=metric.id), metric.id, app)
            span = days or metric.default_days or (14 if metric.time == "cohort_date" else 7)
            end, start = last, last - dt.timedelta(days=span - 1)
            if overall:  # "average rating": every day the warehouse has, not the last week
                start = first_data or start
            if rng is not None:  # the period the question names, cut at the latest data
                start, end = rng[0], min(rng[1], last)
                if start > end or rng[1] < (first_data or rng[1]):
                    return self._no_period(q, lang, metric, app, rng, first_data, last)
        elif metric.time and metric.aggregation == "latest" and rng is not None:
            end = rng[1]  # the stored row of that day ("WAU on 15 October")
        try:
            sql, params = metric.period_sql(
                app_id=app, start=start, end=end, by=by, dim_filter=dim_filter or None
            )
        except CatalogError as exc:
            return self._refuse(
                q,
                lang,
                "no_dimension",
                {"metric": Fact(metric.label(lang), "text"), "dimension": Fact(str(exc), "text")},
            )
        res = self._trusted(sql, params)
        if res is None:
            return self._refuse(q, lang, "schema", {}, missing=metric.id)
        if compare and "app_id" in res.columns:  # keep the apps the question compares
            ai = res.columns.index("app_id")
            res.rows = [r for r in res.rows if r[ai] in compare]
        ans = Answer(q, lang, "catalog", matched=metric.id)
        ans.units = {
            "value": metric.unit,
            "n": "count",
            "first_day": "date",
            "last_day": "date",
            "days": "count",
        }
        if metric.unit == "pct" or metric.aggregation == "latest":
            ans.units.update({c: metric.unit for c in ("value",)})
        self._fill(ans, res, params)
        if not res.rows:
            if dim_filter:
                return self._no_filter_rows(ans, metric, app, dim_filter)
            return self._no_data(ans, metric.id, app)
        cols = res.columns
        rows = res.rows
        vi = cols.index("value")
        ni = cols.index("n") if "n" in cols else None
        label_cols = [c for c in cols if c in ("app_id", *metric.dims)]
        if dim_filter and metric.aggregation == "latest":  # "Meta / (all)": the asked row only
            label_cols = [c for c in label_cols if c not in dim_filter or c == "app_id"]

        def label(r: tuple[Any, ...]) -> str:
            return " / ".join(str(r[cols.index(c)]) for c in label_cols)

        facts: dict[str, Fact] = {
            "metric": Fact(metric.label(lang), "text"),
            "rows": Fact(len(rows), "count"),
            "aggregation_text": Fact(AGG_TEXT[metric.aggregation][lang], "text"),
        }
        valued = [r for r in rows if r[vi] is not None]
        if not valued:
            return self._no_data(ans, metric.id, app)
        # tables without a date column (the level funnel covers the loaded window) give no span
        dated = "first_day" in cols and "last_day" in cols
        vunit = metric.unit
        if metric.aggregation == "mean_of_daily" and vunit == "count":
            vunit = "ratio"  # a mean of daily counts keeps its decimals (1.60, not 2)
            ans.units["value"] = vunit
        if rank and len(valued) > 1:
            pool = valued
            if ni is not None and metric.aggregation == "pooled":
                big = [r for r in valued if r[ni] is not None and r[ni] >= MIN_RANK_N]
                pool = big or valued
            pool = sorted(pool, key=lambda r: r[vi], reverse=rank == "desc")
            items = []
            untrusted = any(c in label_cols for c in ("campaign_name", "creative_key", "type"))
            for i, r in enumerate(pool[:3], start=1):
                facts[f"rank{i}_n"] = Fact(i, "id")
                facts[f"rank{i}_label"] = Fact(label(r), "data" if untrusted else "text")
                facts[f"rank{i}_value"] = Fact(r[vi], vunit)
                items.append(f"{{rank{i}_n}}. {{rank{i}_label}} {{rank{i}_value}}")
            if dated:
                facts.update(
                    first_day=Fact(min(r[cols.index("first_day")] for r in valued), "date"),
                    last_day=Fact(max(r[cols.index("last_day")] for r in valued), "date"),
                )
            order = {
                "asc": {"en": "lowest first", "tr": "en düşükten başlayarak"},
                "desc": {"en": "highest first", "tr": "en yüksekten başlayarak"},
            }[rank][lang]
            facts["order"] = Fact(order, "text")
            tmpl = TEXTS["ranked"][lang].replace("{items}", "; ".join(items))
            key = "ranked"
        elif metric.aggregation == "latest" and len(valued) > 1 and not dim_filter:
            r0 = valued[0]
            facts.update(
                value=Fact(r0[vi], metric.unit), label_part=Fact(f" ({label(r0)})", "text")
            )
            if ni is not None and r0[ni] is not None:
                facts["n"] = Fact(r0[ni], "count")
            key = "catalog_snapshot"
            tmpl = TEXTS[key][lang]
        elif len(valued) == 1 or metric.aggregation == "latest":
            r0 = valued[0]
            facts.update(value=Fact(r0[vi], vunit), label_part=Fact(f" ({label(r0)})", "text"))
            if dated:
                facts.update(
                    first_day=Fact(r0[cols.index("first_day")], "date"),
                    last_day=Fact(r0[cols.index("last_day")], "date"),
                )
            elif metric.time and metric.time in cols and r0[cols.index(metric.time)] is not None:
                facts.update(
                    first_day=Fact(r0[cols.index(metric.time)], "date"),
                    last_day=Fact(r0[cols.index(metric.time)], "date"),
                )
            if ni is not None and r0[ni] is not None:
                facts["n"] = Fact(r0[ni], "count")
            key = "catalog_one"
            tmpl = TEXTS[key][lang]
        else:
            hi = max(valued, key=lambda r: r[vi])
            lo = min(valued, key=lambda r: r[vi])
            facts.update(
                top_label=Fact(label(hi), "text"),
                top_value=Fact(hi[vi], vunit),
                bottom_label=Fact(label(lo), "text"),
                bottom_value=Fact(lo[vi], vunit),
            )
            if dated:
                facts.update(
                    first_day=Fact(min(r[cols.index("first_day")] for r in valued), "date"),
                    last_day=Fact(max(r[cols.index("last_day")] for r in valued), "date"),
                )
            key = "catalog_many"
            tmpl = TEXTS[key][lang]
        ev = self._evidence(
            f"catalog:{metric.id}",
            f"catalog metric {metric.id} ({metric.aggregation})",
            res,
            params,
            facts,
        )
        ev.metric, ev.aggregation, ev.definition = metric.id, metric.aggregation, metric.definition
        ans.evidence = ev
        ans.text = self._say(tmpl, facts, ev, lang, {})
        evs = [ev]
        if trend and key == "catalog_one" and start and end:
            more = self._trend_text(metric, app, start, end, vunit, lang, ev, dim_filter)
            if more:
                ans.text = f"{ans.text} {more}"
        if headline and metric.headline and not by and (key == "catalog_one" or compare):
            more = self._headline_text(
                metric, [app] if app else list(compare or []), lang, ev, dim_filter
            )
            if more:
                ans.text = f"{ans.text} {more}"
        ans.verified = verify.verified(ans.text, evs, lang)
        named: list[str] = []
        if app is None and "app_id" in cols:
            ai = cols.index("app_id")
            if key == "catalog_many":
                shown = [hi, lo]
            elif key == "ranked":
                shown = pool[:3]
            else:
                shown = [valued[0]]
            named = sorted({str(r[ai]) for r in shown})
        return self._small(ans, app, named)

    def _no_period(
        self,
        q: str,
        lang: str,
        metric: Metric,
        app: str | None,
        rng: tuple[dt.date, dt.date],
        first: dt.date | None,
        last: dt.date,
    ) -> Answer:
        facts = {
            "period": Fact(
                rng[0].isoformat()
                if rng[0] == rng[1]
                else f"{rng[0].isoformat()} – {rng[1].isoformat()}",
                "text",
            ),
            "metric": Fact(metric.label(lang), "text"),
            "app_part": Fact(f" ({app})" if app else "", "text"),
            "first_day": Fact(first or last, "date"),
            "last_day": Fact(last, "date"),
        }
        r = self._refuse(q, lang, "no_period_data", facts, missing="period")
        r.matched = metric.id
        return r

    def _no_filter_rows(
        self, ans: Answer, metric: Metric, app: str | None, dim_filter: dict[str, list[str]]
    ) -> Answer:
        d, vals = next(iter(dim_filter.items()))
        have = semantic.values_for(self.s, d, app) if d in semantic.VALUE_HOMES else []
        facts = {
            "metric": Fact(metric.label(ans.lang), "text"),
            "app_part": Fact(f" ({app})" if app else "", "text"),
            "filter": Fact(f"{d} = {', '.join(consistency.display(d, v) for v in vals)}", "text"),
        }
        if have:
            facts["values"] = Fact(", ".join(have[:12]), "text")
        r = self._refuse(ans.question, ans.lang, "no_filter_rows", facts, missing=d)
        r.sql, r.params, r.matched = ans.sql, ans.params, ans.matched
        return r

    def _headline_text(
        self,
        metric: Metric,
        apps: list[str],
        lang: str,
        ev: Evidence,
        dim_filter: dict[str, list[str]],
    ) -> str:
        """The dashboard's headline figure next to a period answer (D7 over 28 cohorts)."""
        assert metric.headline is not None
        hid, where = metric.headline
        h = self.catalog.metrics.get(hid)
        if h is None or not apps or any(d not in h.dims for d in dim_filter):
            return ""
        flt = {k: [str(v)] for k, v in where}
        flt.update(dim_filter)
        parts: list[str] = []
        facts: dict[str, Fact] = {}
        for i, app in enumerate(apps):
            try:
                sql, params = h.period_sql(app_id=app, dim_filter=flt)
            except CatalogError:
                return ""
            res = self._trusted(sql, params)
            if res is None or len(res.rows) != 1:
                return ""
            v = res.rows[0][res.columns.index("value")]
            if v is None:
                return ""
            facts[f"h{i}"] = Fact(v, h.unit)
            parts.append(f"{app} {{h{i}}}" if len(apps) > 1 else f"{{h{i}}}")
        spec = semantic.MODELS.get(h.model)
        window = spec.window_text(lang) if spec else None
        facts["window"] = Fact(window or h.definition, "text")
        ev.facts.update({f"headline.{k}": f for k, f in facts.items()})
        tmpl = TEXTS["headline"][lang].replace("{headline}", ", ".join(parts))
        try:
            return narrate.render(tmpl, facts, lang, {})
        except narrate.TemplateError:
            return ""

    def _trend_text(
        self,
        metric: Metric,
        app: str | None,
        start: dt.date,
        end: dt.date,
        unit: str,
        lang: str,
        ev: Evidence,
        dim_filter: dict[str, list[str]] | None = None,
    ) -> str:
        """Up or down: the period against the period of the same length just before it."""
        length = (end - start).days + 1
        p_end = start - dt.timedelta(days=1)
        p_start = p_end - dt.timedelta(days=length - 1)
        try:
            sql, params = metric.period_sql(
                app_id=app, start=p_start, end=p_end, dim_filter=dim_filter or None
            )
        except CatalogError:
            return ""
        r = self._trusted(sql, params)
        if r is None or not r.rows or r.rows[0][r.columns.index("value")] is None:
            return ""
        cur_r = self._trusted(
            *metric.period_sql(app_id=app, start=start, end=end, dim_filter=dim_filter or None)
        )
        if cur_r is None or not cur_r.rows or cur_r.rows[0][cur_r.columns.index("value")] is None:
            return ""
        a = float(r.rows[0][r.columns.index("value")])
        b = float(cur_r.rows[0][cur_r.columns.index("value")])
        if a == 0:
            return ""
        change = (b - a) / abs(a)
        facts = {
            "prev": Fact(a, unit), "value": Fact(b, unit), "change": Fact(abs(change), "pct"),
            "p_start": Fact(p_start, "date"), "p_end": Fact(p_end, "date"),
        }  # fmt: skip
        ev.facts.update({f"trend.{k}": v for k, v in facts.items()})
        i = 0 if change > 0.005 else (1 if change < -0.005 else 2)
        word = {
            "en": ("up {change}", "down {change}", "flat"),
            "tr": ("{change} arttı", "{change} düştü", "yatay"),
        }[lang][i]  # fmt: skip
        try:
            word_text = word.replace("{change}", fmt.number(abs(change), "pct", lang))
            facts["word"] = Fact(word_text, "text")
            return narrate.render(TEXTS["trend_prev"][lang], facts, lang, {})
        except narrate.TemplateError:
            return ""

    def _pull_dim(
        self,
        q: str,
        lang: str,
        metric: Metric,
        dim: str,
        app: str | None,
        days: int | None,
        values: dict[str, list[str]] | None = None,
    ) -> Answer:
        """Retention by (or for one value of) platform, country, version, source or tier,
        from the cohort_dims pull; the platform split of stg_cohort_days when the pull has no
        cells for the app (SDK apps)."""
        dname = self.catalog.dimensions[dim].name[lang]
        n = {"d1_retention": 1, "d7_retention": 7, "d30_retention": 30}.get(metric.id)
        if app is None and len(self.apps) == 1:
            app = next(iter(self.apps))
        if app is None:
            return self._refuse(
                q, lang, "need_app", {"apps": Fact(", ".join(self.apps), "text")}, missing="app"
            )
        wanted = list((values or {}).get(dim, []))
        has = (
            "stg_cohort_dims" in self.schema
            and n is not None
            and self.s.run_trusted(
                "SELECT COUNT(*) FROM stg_cohort_dims WHERE app_id = ? AND grouping_set = ?",
                [app, dim],
            ).rows[0][0]
        )
        span = days or 28
        if not has and dim == "platform" and n is not None and "stg_cohort_days" in self.schema:
            sql = (
                "SELECT platform, SUM(CASE WHEN day_n = 0 THEN users END) AS n, "
                f"CAST(SUM(CASE WHEN day_n = {n} THEN users END) AS DOUBLE) / "
                "NULLIF(SUM(CASE WHEN day_n = 0 THEN users END), 0) AS value, "
                "MIN(cohort_date) AS first_day, MAX(cohort_date) AS last_day "
                "FROM stg_cohort_days WHERE app_id = ? AND cohort_date <= (SELECT MAX(cohort_date) "
                f"FROM stg_cohort_days WHERE app_id = ? AND day_n = {n}) AND cohort_date > (SELECT "
                f"MAX(cohort_date) FROM stg_cohort_days WHERE app_id = ? AND day_n = {n}) - ? "
                "GROUP BY 1 ORDER BY n DESC"
            )
            params: list[Any] = [app, app, app, span]
            res = self.s.run_trusted(sql, params)
            if res.rows:
                return self._pull_answer(
                    q, lang, metric, dim, app, res, params, wanted, "stg_cohort_days"
                )
        sdk_app = (
            not has
            and "fct_sdk_apps" in self.schema
            and self.s.run_trusted(
                "SELECT COUNT(*) FROM fct_sdk_apps WHERE app_id = ?", [app]
            ).rows[0][0]
        )
        if sdk_app:  # the SDK carries every dimension; the cells are just too small
            return self._refuse(
                q,
                lang,
                "dim_small_sdk",
                {
                    "dimension": Fact(dname, "text"),
                    "app": Fact(app, "text"),
                    "min_n": Fact(50, "count"),
                },
                missing=dim,
            )
        if not has:
            return self._refuse(
                q,
                lang,
                "dim_needs_pull",
                {"dimension": Fact(dname, "text"), "app": Fact(app, "text")},
                missing=dim,
            )
        sql = (
            f"SELECT {dim}, SUM(users) AS n, CAST(SUM(d{n}_users) AS DOUBLE) / NULLIF(SUM(users), 0) AS value, "
            "MIN(cohort_date) AS first_day, MAX(cohort_date) AS last_day "
            f"FROM stg_cohort_dims WHERE app_id = ? AND grouping_set = ? AND d{n}_users IS NOT NULL "
            f"AND cohort_date > (SELECT MAX(cohort_date) FROM stg_cohort_dims WHERE app_id = ? AND d{n}_users IS NOT NULL) - ? "
            "GROUP BY 1 ORDER BY n DESC"
        )
        params = [app, dim, app, span]
        res = self.s.run_trusted(sql, params)
        return self._pull_answer(q, lang, metric, dim, app, res, params, wanted, "stg_cohort_dims")

    def _pull_answer(
        self,
        q: str,
        lang: str,
        metric: Metric,
        dim: str,
        app: str,
        res: Result,
        params: list[Any],
        wanted: list[str],
        source: str,
    ) -> Answer:
        ans = Answer(q, lang, "catalog", matched=f"{metric.id}:{dim}")
        ans.units = {
            "value": "pct",
            "n": "count",
            "first_day": "date",
            "last_day": "date",
            dim: "version",
        }
        if wanted:  # one value (or the values compared): keep only their rows
            want = set().union(*(consistency.aliases(dim, w) for w in wanted))
            have = [str(r[0]) for r in res.rows if r[0] is not None]
            res.rows = [r for r in res.rows if r[0] is not None and str(r[0]).lower() in want]
            if not res.rows:
                facts = {
                    "metric": Fact(metric.label(lang), "text"),
                    "app_part": Fact(f" ({app})", "text"),
                    "filter": Fact(
                        f"{dim} = {', '.join(consistency.display(dim, w) for w in wanted)}", "text"
                    ),
                }
                if have:
                    facts["values"] = Fact(", ".join(have), "text")
                r = self._refuse(q, lang, "no_filter_rows", facts, missing=dim)
                r.sql, r.params, r.matched = res.sql, params, ans.matched
                return r
        self._fill(ans, res, params)
        valued = [r for r in res.rows if r[2] is not None]
        if not valued:
            return self._no_data(ans, metric.id, app)
        method = (
            f"cohort_dims pull, {metric.id} by {dim} (pooled)"
            if source == "stg_cohort_dims"
            else f"stg_cohort_days, {metric.id} by platform (pooled)"
        )
        if len(valued) == 1:
            r0 = valued[0]
            facts = {
                "metric": Fact(metric.label(lang), "text"),
                "label_part": Fact(f" ({app}, {consistency.display(dim, str(r0[0]))})", "text"),
                "value": Fact(r0[2], "pct"),
                "n": Fact(r0[1], "count"),
                "first_day": Fact(r0[3], "date"),
                "last_day": Fact(r0[4], "date"),
                "aggregation_text": Fact(AGG_TEXT["pooled"][lang], "text"),
            }
            ev = self._evidence(f"catalog:{metric.id}:{dim}", method, res, params, facts)
            ans.evidence = ev
            ans.text = self._say(TEXTS["catalog_one"][lang], facts, ev, lang, {})
            ans.verified = verify.verified(ans.text, [ev], lang)
            return self._small(ans, app)
        hi = max(valued, key=lambda r: r[2])
        lo = min(valued, key=lambda r: r[2])
        facts = {
            "metric": Fact(metric.label(lang), "text"),
            "rows": Fact(len(valued), "count"),
            "aggregation_text": Fact(AGG_TEXT["pooled"][lang], "text"),
            "top_label": Fact(str(hi[0]), "version"),
            "top_value": Fact(hi[2], "pct"),
            "bottom_label": Fact(str(lo[0]), "version"),
            "bottom_value": Fact(lo[2], "pct"),
            "first_day": Fact(min(r[3] for r in valued), "date"),
            "last_day": Fact(max(r[4] for r in valued), "date"),
        }
        ev = self._evidence(f"catalog:{metric.id}:{dim}", method, res, params, facts)
        ans.evidence = ev
        ans.text = self._say(TEXTS["catalog_many"][lang], facts, ev, lang, {})
        ans.verified = verify.verified(ans.text, [ev], lang)
        return self._small(ans, app) if source != "stg_cohort_dims" else ans
