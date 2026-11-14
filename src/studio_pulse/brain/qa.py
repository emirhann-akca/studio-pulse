"""Question router (B8): verified query -> catalog tool -> guarded SQL -> reasoned refusal.

1. Verified query library (verified_queries.yaml): owner-checked SQL with
   TR/EN phrasings; parameters (app, level) are recognised and bound.
2. Catalog tool: a metric named in the question (catalog names and synonyms,
   or the model's catalog JSON when an LLM is configured and validated
   against the catalog) is computed with its declared aggregation over a
   period; breakdowns only by the metric's own dimensions, or by the
   cohort_dims pull for retention.
3. Guarded SQL: only with a configured model; its SELECT goes through the
   AST guard and the locked read-only session (never to BigQuery).
4. Otherwise a refusal that names what is missing (metric, dimension, app,
   fresh data) instead of a guess.

Personal data and future dates are refused before routing. Every number in
an answer's text is a formatted evidence value and passes the verifier.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import duckdb
import yaml

from . import consistency, drivers, fmt, guard, intent, llm, narrate, semantic, verify
from .answer import (  # noqa: F401  (Answer is part of this module's API)
    _BREAKDOWN,
    _COLUMN,
    _LIST_WORD,
    _OVERALL,
    _PEOPLE,
    _PER_UNIT,
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
    _guess_unit,
)  # fmt: skip
from .catalog import Catalog, load, normalize
from .catalog_answers import CatalogAnswers
from .evidence import Evidence, Fact
from .executor import LockedSession, QueryTimeout, Result

ROUTES = ("verified_query", "catalog", "sql", "refused")

_PII = re.compile(
    r"\b(user ?ids?|user pseudo id|pseudo ?ids?|e ?-?mails?|e ?-?posta\w*|phone numbers?|telefon "
    r"numara\w*|ip address\w*|ip adres\w*|device ids?|cihaz kimli\w*|kullanici kimli\w*|"
    r"kullanici id\w*|names? of (the )?(users|players)|oyuncu(larin)? isim\w*|kullanici(larin)? "
    r"isim\w*|advertising id\w*|idfa|gaid|kisisel veri\w*|personal data)\b"
)
_FUTURE = re.compile(
    r"\b(next (week|month|year|quarter)|tomorrow|forecast\w*|predict\w*|will .* be|"
    r"gelecek (hafta|ay|yil|ceyrek)\w*|yarin\w*|onumuzdeki \w+|tahmin\w*|olacak\w*)\b"
)
_YEAR = re.compile(r"\b(20\d\d)\b")
# Filters, counts and "which day" need a query, not one metric over a period.
# "how many levels / campaigns / days": a count of rows, not a metric (B10 handles "how many
# users" as the metric itself)
_LONG_TAIL = re.compile(
    r"\b(how many (?:levels|campaigns|days|apps|versions|countries|creatives)|count of (?:levels|"
    r"campaigns|days)|number of (?:levels|campaigns|days)|below|above|less than|more than|"
    r"at least|at most|which day|what day|when did|kac (?:seviye|tane|kampanya|gun|uygulama|"
    r"surum)\w*|altinda|ustunde|fazla olan|az olan|hangi gun\w*)\b"
)
_LEVEL = re.compile(
    r"\b(?:level|seviye|bolum)\s*(\d{1,4})\b|\b(\d{1,4})\s*\.?\s*(?:seviye|bolum)\w*"
)
_DAYS = re.compile(r"\b(?:last|past|son|gecen)\s+(\d{1,3})\s*(?:days?|gun\w*)\b")
_WEEK = re.compile(r"\b(last week|past week|this week|son hafta\w*|gecen hafta\w*|bu hafta\w*)\b")
_MONTH = re.compile(r"\b(last month|past month|son ay\w*|gecen ay\w*|son bir ay\w*)\b")
_YESTERDAY = re.compile(r"\b(yesterday|dun|dunku)\b")
_DAY_N = re.compile(r"\bd(\d{1,3})\b")
_ISO_DAY = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
    "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}  # fmt: skip
_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\w*\s+(20\d\d)\b|\b(20\d\d)\s+(" + "|".join(_MONTHS) + r")\b"
)
_MONTH_ALONE = re.compile(r"\b(" + "|".join(m for m in _MONTHS if m != "may") + r")\w*\b")
_STOP = set(
    "the a an of in for is are was were what which how did do does to by and or on at with "
    "per each from our we me show give tell bir ve ile icin mi mu mi nedir ne nasil hangi "
    "hangisi kac kadar en ya da".split()
)


_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "iki": 2, "uc": 3, "dort": 4, "bes": 5}
_N_BEFORE = re.compile(r"\b(?:top|first|ilk|en \w+) (\d{1,2}|" + "|".join(_COUNT_WORDS) + r")\b")
_N_AFTER = re.compile(
    r"\b(\d{1,2}|"
    + "|".join(_COUNT_WORDS)
    + r") (?:en\b|hardest|easiest|most|best|worst|levels?\b|seviye|bolum)"
)
_PLURAL = re.compile(r"\b(?:levels|seviyeler\w*|bolumler\w*|which levels|hangi seviyeler)\b")


def _wanted_count(norm: str) -> int:
    """How many rows a list question asks for: "top 3", "ilk 3", "3 en zor" -> 3; a plural
    ("hardest levels", "en zor seviyeler") -> 3; otherwise 1."""
    for rx in (_N_BEFORE, _N_AFTER):
        m = rx.search(norm)
        if m:
            w = m.group(1)
            n = int(w) if w.isdigit() else _COUNT_WORDS[w]
            return max(1, min(n, 10))
    return 3 if _PLURAL.search(norm) else 1


@dataclass
class VQ:
    id: str
    phrasings: dict[str, list[str]]
    must: list[list[str]]
    params: list[str]
    sql: str
    units: dict[str, str]
    answer: dict[str, str]
    consts: dict[str, Fact]
    more: dict[str, str] = field(default_factory=dict)  # one row of a "top N" list: {row.<column>}


@lru_cache(maxsize=1)
def verified_queries() -> tuple[VQ, ...]:
    text = (
        resources.files("studio_pulse.brain")
        .joinpath("verified_queries.yaml")
        .read_text(encoding="utf-8")
    )
    out = []
    for q in yaml.safe_load(text)["queries"]:
        out.append(
            VQ(
                id=q["id"],
                phrasings={k: list(v) for k, v in q["phrasings"].items()},
                must=[[normalize(w) for w in g] for g in q.get("must") or []],
                params=list(q.get("params") or []),
                sql=q["sql"],
                units=dict(q.get("units") or {}),
                answer=dict(q["answer"]),
                consts={k: Fact(v[0], v[1]) for k, v in (q.get("consts") or {}).items()},
                more=dict(q.get("more") or {}),
            )
        )
    return tuple(out)


def _bind(sql: str, values: dict[str, Any]) -> tuple[str, list[Any]]:
    params: list[Any] = []

    def sub(m: re.Match[str]) -> str:
        params.append(values[m.group(1)])
        return "?"

    return re.sub(r"\$(\w+)", sub, sql), params


def _tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t not in _STOP]


def _hit(word: str, tokens: list[str]) -> bool:
    if len(word) < 4:
        return word in tokens
    return any(t.startswith(word) or (len(t) >= 4 and word.startswith(t)) for t in tokens)


class Router(CatalogAnswers):
    def __init__(
        self,
        session: LockedSession,
        *,
        catalog: Catalog | None = None,
        client: llm.LLMClient | None = None,
        as_of: dt.date | None = None,
    ):
        self.s = session
        self.catalog = catalog or load()
        self.client = client
        self.as_of = as_of or dt.date.today()
        self.schema = session.schema()
        self.apps: dict[str, str] = {}
        if "meta_apps" in self.schema:
            for app_id, name in session.run_trusted("SELECT app_id, name FROM meta_apps").rows:
                self.apps[app_id] = name or app_id
        elif "fct_app_daily" in self.schema:
            for (app_id,) in session.run_trusted(
                "SELECT DISTINCT app_id FROM fct_app_daily ORDER BY 1"
            ).rows:
                self.apps[app_id] = app_id
        self.genres: dict[str, str] = {}
        if "meta_apps" in self.schema and "genre" in self.schema["meta_apps"]:
            self.genres = {
                a: g
                for a, g in session.run_trusted("SELECT app_id, genre FROM meta_apps").rows
                if g
            }
        self._known: tuple[set[str], set[str]] | None = None
        self._layer: semantic.Layer | None = None

    # -- slots ---------------------------------------------------------------
    def find_apps(self, norm: str) -> list[str]:
        """Every app the question names, longest names first (for comparisons)."""
        padded = f" {norm} "
        hits: dict[str, int] = {}
        for app_id, name in self.apps.items():
            for form in {normalize(app_id), normalize(name)}:
                if form and f" {form} " in padded:
                    hits[app_id] = max(hits.get(app_id, 0), len(form))
        return sorted(hits, key=lambda a: -hits[a])

    def find_app(self, norm: str) -> str | None:
        padded = f" {norm} "
        hits = []
        for app_id, name in self.apps.items():
            for form in {normalize(app_id), normalize(name)}:
                if form and f" {form} " in padded:
                    hits.append((len(form), app_id))
        return max(hits)[1] if hits else None

    @staticmethod
    def find_level(norm: str) -> int | None:
        m = _LEVEL.search(norm)
        if not m:
            return None
        return int(m.group(1) or m.group(2))

    @staticmethod
    def find_days(norm: str) -> int | None:
        m = _DAYS.search(norm)
        if m:
            return max(1, min(int(m.group(1)), 365))
        if _WEEK.search(norm):
            return 7
        if _MONTH.search(norm):
            return 30
        if _YESTERDAY.search(norm):
            return 1
        return None

    @staticmethod
    def find_range(norm: str, last: dt.date | None) -> tuple[dt.date, dt.date] | None:
        """An explicit period in the question: ISO day(s) ("2026-10-31", "between X and Y")
        or a month ("October 2026", "ekim 2026'da", "ekimde" = that month of the data's
        latest year). None when the question names no period, so the default window holds."""
        days = []
        for m in _ISO_DAY.finditer(norm):
            try:
                days.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                continue
        if days:
            return min(days), max(days)
        m = _MONTH_YEAR.search(norm)
        if m:
            name = m.group(1) or m.group(4)
            year = int(m.group(2) or m.group(3))
        else:
            m = _MONTH_ALONE.search(norm)
            if not m or last is None:
                return None
            name, year = m.group(1), last.year
            if _MONTHS[name] > last.month:
                year -= 1
        month = _MONTHS[name]
        first = dt.date(year, month, 1)
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        return first, nxt - dt.timedelta(days=1)

    def _last_day(self) -> dt.date | None:
        if "fct_app_daily" not in self.schema:
            return None
        rows = self.s.run_trusted("SELECT MAX(date) FROM fct_app_daily WHERE dau IS NOT NULL").rows
        return rows[0][0] if rows else None

    # -- entry -----------------------------------------------------------------
    def intent(self, question: str) -> intent.Intent:
        terms, ids = self._known_words(question)
        return intent.parse(
            question,
            apps=self.apps,
            as_of=self.as_of,
            genres=self.genres,
            known_terms=terms,
            known_ids=ids,
        )

    def _known_words(self, question: str) -> tuple[set[str], set[str]]:
        """Words that are not an app name: catalog phrases, dimension values, identifiers."""
        if self._known is None:
            words: set[str] = set(intent.COMMON_WORDS)
            for phrase, _kind, _id in self.catalog.terms():
                words.update(phrase.split())
            for phrases in intent.NETWORKS.values():
                words.update(" ".join(phrases).split())
            for en, others, trs in intent.COUNTRIES.values():
                words.update(normalize(" ".join([en, *others, *trs])).split())
            for name in self.apps.values():
                words.update(normalize(name).split())
            ids: set[str] = set(self.catalog.metrics) | set(self.schema)
            for cols in self.schema.values():
                ids.update(cols)
            if "_" in question:
                for table, col in (
                    ("fct_experiment_tests", "experiment_id"),
                    ("fct_sdk_purchases", "product_id"),
                    ("fct_sdk_settings", "setting"),
                    ("fct_review_topics", "topic"),
                    ("fct_source_freshness", "source"),
                    ("fct_ua_daily", "campaign_key"),
                    ("fct_creative_scorecard", "creative_key"),
                ):
                    if table in self.schema:
                        ids.update(
                            str(r[0]).lower()
                            for r in self.s.run_trusted(f"SELECT DISTINCT {col} FROM {table}").rows
                            if r[0] is not None
                        )
            self._known = (words, ids)
        return self._known

    def ask(self, question: str, lang: str = "en") -> Answer:
        lang = lang if lang in fmt.LANGS else "en"
        q = (question or "").strip()[:500]
        norm = normalize(q)
        if not norm:
            return self._refuse(q, lang, "no_metric", {})
        if _PII.search(norm):
            return self._refuse(q, lang, "pii", {}, missing="personal data")
        last = self._last_day()
        it = self.intent(q)
        found = self.catalog.find(q)["metric"]
        predicted = "pltv_usd" in found
        future_year = any(int(y) > (last or self.as_of).year for y in _YEAR.findall(norm))
        if (_FUTURE.search(norm) and not predicted) or future_year or it.future:
            facts = {"last_day": Fact(last, "date")} if last else {}
            return self._refuse(q, lang, "future", facts, missing="future data")
        early = self._early(q, norm, lang, it, found)
        if early is not None:
            return early
        ans = self._route(q, norm, lang, it, last)
        ans = self._gate(ans, it)
        if it.typo_apps and ans.route != "refused":
            written, app = next(iter(it.typo_apps.items()))
            note = narrate.render(
                TEXTS["typo"][lang],
                {"written": Fact(written, "data"), "app": Fact(app, "text")},
                lang,
            )
            ans.text = f"{ans.text} {note}"
            ans.notes.append("typo_app")
        return ans

    def _early(
        self, q: str, norm: str, lang: str, it: intent.Intent, found: list[str]
    ) -> Answer | None:
        """Refusals that need no query: an app that does not exist, an ambiguous genre, a
        metric nobody collects, a filter value with no rows for the app."""
        if it.unknown_apps and not it.apps:
            facts = {
                "name": Fact(it.unknown_apps[0], "data"),
                "apps": Fact(", ".join(self.apps), "text"),
            }
            return self._refuse(q, lang, "unknown_app", facts, missing="app")
        if it.genre_apps and len(it.genre_apps) > 1 and not it.total and not it.compare:
            facts = {"apps": Fact(", ".join(it.genre_apps), "text")}
            return self._refuse(q, lang, "ambiguous_app", facts, missing="app")
        if it.genre_apps and len(it.genre_apps) == 1:
            it.apps = list(it.genre_apps)
        if it.unknown_metrics and not found:
            sug = self._suggest(norm, lang)
            facts = {"metric": Fact(it.unknown_metrics[0], "text")}
            if sug:
                facts["suggestions"] = Fact(sug, "text")
            # the refusal key stays no_metric: the refusal set asks "names what is missing"
            r = self._refuse(q, lang, "unknown_metric", facts, missing=it.unknown_metrics[0])
            r.refusal = "no_metric"
            return r
        app = it.apps[0] if len(it.apps) == 1 else None
        for dim, vals in it.filters().items():
            have = semantic.values_for(self.s, dim, app)
            if not have:
                continue  # no table carries the dimension for this app: the gate decides
            have_l = {h.lower() for h in have}
            missing = [v for v in vals if not (consistency.aliases(dim, v) & have_l)]
            if missing:
                shown = have if len(have) <= 12 else [*have[:3], "…", *have[-3:]]
                facts = {
                    "app_part": Fact(
                        f"{app} " if app else {"en": "This warehouse ", "tr": "Ambarın "}[lang],
                        "text",
                    ),
                    "filter": Fact(
                        f"{dim} = {', '.join(consistency.display(dim, m) for m in missing)}", "text"
                    ),
                    "values": Fact(", ".join(shown), "text"),
                }
                return self._refuse(q, lang, "no_such_value", facts, missing=dim)
        return None

    def _route(
        self, q: str, norm: str, lang: str, it: intent.Intent, last: dt.date | None
    ) -> Answer:
        app = it.apps[0] if len(it.apps) == 1 else self.find_app(norm)
        named = it.apps if it.apps else self.find_apps(norm)
        compare = named if len(named) >= 2 else None
        if compare:
            app = None  # "compare A and B": one row per named app
        rng = self.find_range(norm, last)
        n_days = self.find_days(norm)
        if it.period is not None and it.period.latest and not n_days and not rng:
            n_days = 1  # "latest", "en son": the newest day of a daily metric
        if it.period is not None and it.period.start and it.period.end and not it.period.latest:
            rng = (it.period.start, it.period.end)
        elif it.period is not None and it.period.days:
            n_days = it.period.days
        if rng and n_days and (rng[1] - rng[0]).days + 1 > n_days:
            rng = (rng[1] - dt.timedelta(days=n_days - 1), rng[1])  # "last 7 days of October"
        if it.why and app is not None:
            why = self._why(q, lang, it, app)
            if why is not None:
                return why
        # verified queries have fixed windows: an explicit period goes to the catalog / SQL
        vq = None if (rng or compare) else self._verified(q, norm, lang, app, it)
        if vq is not None and (vq.route == "refused" or not self._gaps(vq, it)):
            return vq  # a library query that does not cover the filters leaves it to the catalog
        long_tail = bool(_LONG_TAIL.search(norm)) and not any(
            self.catalog.get(m).unit == "date" for m in self.catalog.find(q)["metric"]
        )
        cat = None if long_tail else self._catalog(q, norm, lang, app, rng, compare, it)
        if cat is not None:
            return cat
        if self.client is not None:
            return self._sql(q, lang)
        if long_tail:
            return self._refuse(q, lang, "long_tail", {}, missing="query")
        return self._no_metric(q, norm, lang)

    def _gaps(self, ans: Answer, it: intent.Intent) -> list[consistency.Gap]:
        skip: set[str] = set()
        if ans.matched in ("hardest_levels",):
            skip.add("level")
        newest, first = self._span_of(ans, it)
        gaps = consistency.check(
            it,
            sql=ans.sql,
            params=ans.params,
            columns=ans.columns,
            rows=ans.rows,
            n_apps=len(self.apps),
            newest=newest,
            first=first,
            skip=skip,
        )
        cols = [c.lower() for c in ans.columns]
        m = self.catalog.metrics.get((ans.matched or "").split(":")[0])
        if m is not None and _PER_UNIT.search(it.norm) and m.aggregation == "sum":
            gaps.append(consistency.Gap("metric", ["a rate per user"], "not_applied"))
        if it.total and not it.apps and ans.route == "catalog" and "app_id" in cols:
            if len({str(r[cols.index("app_id")]) for r in ans.rows}) > 1:
                gaps.append(consistency.Gap("total", ["all apps"], "not_applied"))
        for dim in it.by:
            if dim == "app_id" and it.apps:
                continue
            col = semantic.DIM_COLUMNS.get(dim, dim)
            if col not in cols and not (dim == "level" and any("level" in c for c in cols)):
                gaps.append(consistency.Gap("breakdown", [dim], "not_applied"))
        return gaps

    def _gate(self, ans: Answer, it: intent.Intent) -> Answer:
        """Refuse an answer whose evidence does not cover the question's filters and period."""
        if ans.route == "refused" or "drivers" in ans.notes:
            return ans
        gaps = self._gaps(ans, it)
        if not gaps:
            return ans
        metric = self._metric_label(ans)
        g = gaps[0]
        if g.dim == "period":
            spec = next(
                (semantic.MODELS[t] for t in self._tables_of(ans) if t in semantic.MODELS), None
            )
            fixed = {"en": "a fixed window", "tr": "sabit bir dönem"}[ans.lang]
            facts = {
                "metric": Fact(metric, "text"),
                "window": Fact((spec.window_text(ans.lang) if spec else None) or fixed, "text"),
                "period": Fact(", ".join(g.asked), "text"),
            }
            key = "period_unchecked" if spec is not None and spec.time else "period_not_applied"
            r = self._refuse(ans.question, ans.lang, key, facts, missing="period")
        else:
            facts = {
                "metric": Fact(metric, "text"),
                "filters": Fact("; ".join(x.label() for x in gaps if x.dim != "period"), "text"),
            }
            r = self._refuse(ans.question, ans.lang, "filter_not_applied", facts, missing=g.dim)
        r.sql, r.params, r.matched = ans.sql, ans.params, ans.matched
        r.notes.append("consistency:" + ",".join(f"{x.dim}:{x.reason}" for x in gaps))
        return r

    def _tables_of(self, ans: Answer) -> list[str]:
        if not ans.sql:
            return []
        return sorted(consistency.coverage_of(ans.sql, ans.params, [], []).tables)

    def _span_of(self, ans: Answer, it: intent.Intent) -> tuple[dt.date | None, dt.date | None]:
        """Newest and oldest day of the answer's main table for the asked app."""
        for t in self._tables_of(ans):
            spec = semantic.MODELS.get(t)
            if not spec or not spec.time:
                continue
            app = it.apps[0] if len(it.apps) == 1 else None
            got = self._trusted(
                f"SELECT MAX({spec.time}), MIN({spec.time}) FROM {t}"
                + (" WHERE app_id = ?" if app else ""),
                [app] if app else [],
            )
            if got and got.rows:
                return got.rows[0][0], got.rows[0][1]
        return None, None

    def _metric_label(self, ans: Answer) -> str:
        m = self.catalog.metrics.get((ans.matched or "").split(":")[0])
        if m:
            return m.label(ans.lang)
        return ans.matched or ("this question" if ans.lang == "en" else "bu soru")

    def _suggest(self, norm: str, lang: str) -> str:
        toks = set(_tokens(norm))
        scored = []
        for m in self.catalog.metrics.values():
            words = set(_tokens(" ".join([m.name["en"], m.name["tr"]])))
            overlap = len(toks & words)
            if overlap:
                scored.append((-overlap, m.id))
        return ", ".join(self.catalog.get(i).label(lang) for _, i in sorted(scored)[:3])

    # -- why: driver analysis -----------------------------------------------------
    def _why(self, q: str, lang: str, it: intent.Intent, app: str) -> Answer | None:
        found = self.catalog.find(q)["metric"]
        if not found:
            return None
        metric = self._pick(found, it, normalize(q))[0]
        stale = self._stale(q, lang, app)
        if stale is not None:
            return stale
        tier = it.tiers[0] if it.tiers else None
        try:
            res = drivers.explain(self.s, self.catalog, metric, app, as_of=self.as_of, tier=tier)
        except (duckdb.Error, KeyError, IndexError, ValueError):
            res = None
        if res is None or res.value0 is None or res.value1 is None:
            return None
        unit = res.unit
        change = (res.value1 - res.value0) / abs(res.value0) if res.value0 else None
        facts: dict[str, Fact] = {
            "metric": Fact(metric.label(lang), "text"),
            "app": Fact(app, "text"),
            "value0": Fact(res.value0, unit),
            "value1": Fact(res.value1, unit),
            "cur_start": Fact(res.current[0], "date"),
            "cur_end": Fact(res.current[1], "date"),
            "base_start": Fact(res.baseline[0], "date"),
            "base_end": Fact(res.baseline[1], "date"),
        }
        word_i = 2
        if change is not None:
            facts["change"] = Fact(abs(change), "pct")
            word_i = 0 if change > 0.005 else (1 if change < -0.005 else 2)
        direction = drivers.DIRECTION[lang][word_i]
        if "{change}" in direction and "change" in facts:
            direction = direction.replace(
                "{change}", fmt.number(facts["change"].value, "pct", lang)
            )
        facts["direction"] = Fact(direction, "text")
        text = narrate.render(drivers.TEXTS["head"][lang], facts, lang)
        asked = drivers.asked_direction(normalize(q), metric.better)
        if asked and word_i in (0, 1) and asked != ("up" if word_i == 0 else "down"):
            text += drivers.TEXTS["not_moved"][lang]
        if res.dimension and res.segment is not None:
            dim_name = drivers.SEGMENT_TEXT[lang].get(res.dimension.split(",")[0], res.dimension)
            seg = res.segment
            if res.dimension == "component":
                seg = drivers.COMPONENT_NAMES.get(seg, {}).get(lang, seg)
            sunit = "pp" if unit == "pct" else unit
            seg_facts = {
                "dim": Fact(dim_name, "text"),
                "segment": Fact(seg, "version"),
            }
            if res.seg0_label:
                label = (
                    drivers.REST_TEXT[lang] if res.seg0_label == drivers.REST else res.seg0_label
                )
                seg_facts["seg0_label"] = Fact(label, "version")
            if res.seg0 is not None:
                seg_facts["seg0"] = Fact(res.seg0, unit)
            if res.seg1 is not None:
                seg_facts["seg1"] = Fact(res.seg1, unit)
            if res.contribution is not None:
                seg_facts["contribution"] = Fact(abs(res.contribution), sunit)
            if res.ep is not None and 0 < res.ep <= 1:
                seg_facts["ep"] = Fact(res.ep, "pct")
            facts.update(seg_facts)
            key = "segment_vs" if res.seg0_label else "segment"
            text += narrate.render(drivers.TEXTS[key][lang], seg_facts, lang)
        if res.release and not (
            res.dimension == "app_version" and str(res.segment).split(" ")[0] == res.release[0]
        ):
            rel = {"version": Fact(res.release[0], "version"), "first_seen": Fact(res.release[1], "date"),
                   "metric": Fact(metric.label(lang), "text")}  # fmt: skip
            facts.update({"version": rel["version"], "first_seen": rel["first_seen"]})
            key = "release" if res.dimension else "release_only"
            text += narrate.render(drivers.TEXTS[key][lang], rel, lang)
        text += drivers.TEXTS["caveat"][lang]
        main = res.evidence[0]
        ev = Evidence(
            id=f"why:{metric.id}:{app}",
            kind="answer",
            app_id=app,
            method=res.method,
            sql=main.sql,
            params=main.params,
            columns=main.columns,
            rows=main.rows,
            facts=facts,
            caveats=["association_in_time"],
        )
        ans = Answer(q, lang, "catalog", matched=f"{metric.id}:why", units={})
        ans.sql, ans.params, ans.columns, ans.rows = main.sql, main.params, main.columns, main.rows
        ans.evidence = ev
        evs = [ev, *res.evidence]
        ans.text, _src = narrate.restyle(text, evs, facts, lang, self.client)
        ans.verified = verify.verified(ans.text, evs, lang)
        ans.notes += ["drivers", "association_in_time"]
        return self._small(ans, app)

    # -- 1. verified queries ---------------------------------------------------
    def _verified(
        self, q: str, norm: str, lang: str, app: str | None, it: intent.Intent | None = None
    ) -> Answer | None:
        toks = _tokens(norm)
        wanted_cols: list[str] = []
        every = norm.split()
        asked_dims = [
            d
            for d in self.catalog.find(q)["dimension"]
            if d not in ("app_id", "date", "cohort_date")
        ]
        level = self.find_level(norm)
        days = self.find_days(norm)
        day_ns = {m.group(1) for m in _DAY_N.finditer(norm)}
        metric_cols = set()
        sdk_models = set()
        for mid in self.catalog.find(q)["metric"]:
            m = self.catalog.get(mid)
            col = m.value
            if re.fullmatch(r"[a-z_][a-z0-9_]*", col) and col != "retention":
                metric_cols.add(re.sub(r"_(usd|ms|mb)$", "", col))  # spend_usd ~ spend_30d_usd
            if mid.startswith("sdk_"):
                sdk_models.add(m.model)  # "SDK level fail rate": the SDK funnel, not the GA4 one
        best: tuple[float, VQ] | None = None
        for vq in verified_queries():
            if not all(any(_hit(w, every) for w in group) for group in vq.must):
                continue
            if any(c not in vq.sql for c in wanted_cols):
                continue  # the question filters by a column this query does not have
            if it is not None and it.day_ns and vq.id == "ltv_d30" and it.day_ns[0] != 30:
                continue  # D7 LTV is not the day-30 query
            if any(_COLUMN.get(d, d) not in vq.sql for d in asked_dims):  # a breakdown it lacks
                continue
            if "level" in vq.params and level is None:
                continue  # a level question without a level number is not this one
            if app is not None and "app" not in vq.params:
                continue  # a portfolio ranking does not answer a question about one app
            said = (vq.sql + " " + " ".join(vq.answer.values())).lower()
            if (
                vq.id.startswith("retention")
                and len(day_ns) == 1
                and not any(f"d{n}" in said or f"day_n = {n}" in said for n in day_ns)
            ):
                continue  # D30 asked, the retention query reports D1
            if metric_cols and not any(c in vq.sql for c in metric_cols):
                continue  # crash-free asked, the query is about retention
            if sdk_models and not any(t in vq.sql for t in sdk_models):
                continue
            window = vq.consts.get("window")
            if days is not None and window is not None and window.value != days:
                continue  # a fixed-window query cannot answer another period
            score = 0.0
            for phr in vq.phrasings.get("en", []) + vq.phrasings.get("tr", []):
                pt = [t for t in _tokens(re.sub(r"\{\w+\}", " ", phr))]
                if pt:
                    score = max(score, sum(_hit(t, toks) for t in pt) / len(pt))
            if level is not None:  # a level number favours the per-level query
                score += 0.5 if "level" in vq.params else -0.3
            if score >= 0.34 and (best is None or score > best[0]):
                best = (score, vq)
        if best is None:
            return None
        vq = best[1]
        values: dict[str, Any] = {}
        if "app" in vq.params:
            if app is None and len(self.apps) == 1:
                app = next(iter(self.apps))
            if app is None:
                return self._refuse(
                    q, lang, "need_app", {"apps": Fact(", ".join(self.apps), "text")}, missing="app"
                )
            values["app"] = app
        if "level" in vq.params:
            values["level"] = level
        stale = self._stale(q, lang, app)
        if stale is not None:
            return stale
        sql, params = _bind(vq.sql, values)
        res = self._trusted(sql, params)
        if res is None:
            return self._refuse(q, lang, "schema", {}, missing=vq.id)
        ans = Answer(q, lang, "verified_query", matched=vq.id, units=dict(vq.units))
        self._fill(ans, res, params)
        facts = (
            {
                f"first.{c}": Fact(v, vq.units.get(c, _guess_unit(v)))
                for c, v in zip(res.columns, res.rows[0], strict=False)
            }
            if res.rows
            else {}
        )
        facts.update(vq.consts)
        facts["rows"] = Fact(len(res.rows), "count")
        ev = self._evidence(f"vq:{vq.id}", "verified query " + vq.id, res, params, facts)
        ans.evidence = ev
        if not res.rows:
            return self._no_data(ans, vq.id, app)
        ans.text = self._say(vq.answer[lang], facts, ev, lang, {"app": app or "portfolio"})
        want = _wanted_count(norm)
        if vq.more.get(lang) and want > 1 and len(res.rows) > 1:  # "top 3": the next rows too
            items = []
            for i, row in enumerate(res.rows[1:want], start=2):
                row_facts = {
                    f"row.{c}": Fact(v, vq.units.get(c, _guess_unit(v)))
                    for c, v in zip(res.columns, row, strict=False)
                }
                ev.facts.update({f"row{i}.{k[4:]}": f for k, f in row_facts.items()})
                try:
                    item = narrate.render(vq.more[lang], row_facts, lang, {})
                except narrate.TemplateError:
                    break
                items.append(item[:1].lower() + item[1:])
            if items:
                lead = {"en": "Next:", "tr": "Sonra:"}[lang]
                ans.text = f"{ans.text} {lead} {'; '.join(items)}."
        ans.verified = verify.verified(ans.text, [ev], lang)
        return self._small(ans, app)

    def _trusted(self, sql: str, params: list[Any]) -> Result | None:
        """Catalog / library SQL; None when this warehouse predates a column it needs."""
        try:
            return self.s.run_trusted(sql, params)
        except QueryTimeout:
            raise
        except duckdb.Error:
            return None

    def _small(self, ans: Answer, app: str | None, named: list[str] | None = None) -> Answer:
        """Append the small-sample caveat (no numbers) when an app the answer is about has a median
        DAU under 50: the one app asked about, else the apps the text names, else all of them."""
        apps = [app] if app else (named if named else list(self.apps))
        if not apps or "fct_app_daily" not in self.schema:
            return ans
        res = self._trusted(
            "SELECT app_id, median(dau) FROM fct_app_daily WHERE dau IS NOT NULL "
            "AND date > (SELECT MAX(date) FROM fct_app_daily) - 14 GROUP BY 1",
            [],
        )
        small = {r[0] for r in (res.rows if res else []) if r[1] is not None and r[1] < 50}
        hit = [a for a in apps if a in small]
        if hit:
            some = len(hit) < len(apps) and not any(ch.isdigit() for ch in "".join(hit))
            if some:  # a ranking that also names a big app: say which numbers are fragile
                ans.text = f"{ans.text} {TEXTS['small_some'][ans.lang].format(apps=', '.join(hit))}"
            else:
                ans.text = f"{ans.text} {TEXTS['small'][ans.lang]}"
            ans.notes.append("small_sample")
        return ans

    def _llm_route(self, q: str) -> dict[str, Any] | None:
        assert self.client is not None
        payload = {
            "question": q,
            "metrics": {
                m.id: f"{m.name['en']}: {m.definition}" for m in self.catalog.metrics.values()
            },
            "dimensions": list(self.catalog.dimensions),
            "apps": list(self.apps),
        }
        try:
            reply = self.client.generate(llm.request("route", llm.SYSTEM_ROUTE, payload))
        except Exception:
            return None
        d = reply.data or {}
        metric = d.get("metric")
        if not isinstance(metric, str) or metric not in self.catalog.metrics:
            return None
        m = self.catalog.get(metric)
        words = set(
            _tokens(
                " ".join(
                    [m.id.replace("_", " "), m.name["en"], m.name["tr"]]
                    + [x for v in m.synonyms.values() for x in v]
                )
            )
        )
        if not any(_hit(w, _tokens(q)) for w in words if len(w) >= 3):
            return None  # "most frequent error" routed to crashed users: no shared word
        out: dict[str, Any] = {"metric": metric}
        if isinstance(d.get("app"), str) and d["app"] in self.apps:
            out["app"] = d["app"]
        if isinstance(d.get("by"), str) and d["by"] in self.catalog.dimensions:
            out["by"] = d["by"]
        if isinstance(d.get("days"), int) and 1 <= d["days"] <= 365:
            out["days"] = d["days"]
        return out

    def semantic_context(self) -> dict[str, Any]:
        """The semantic layer as the model sees it (semantic.py): metrics with grain,
        filters and their values, tables with windows, join paths, rules, today."""
        if self._layer is None:
            self._layer = semantic.build(self.s, self.catalog)
        return semantic.context(
            self._layer,
            apps=self.apps,
            genres=self.genres,
            as_of=self.as_of,
            newest_day=self._last_day(),
            allowed=self.s.allowed,
        )

    # -- 3. guarded SQL ------------------------------------------------------------
    def _sql(self, q: str, lang: str) -> Answer:
        assert self.client is not None
        payload = {"question": q, **self.semantic_context()}
        try:
            reply = self.client.generate(
                llm.request("sql", llm.SYSTEM_SQL, payload, max_tokens=900)
            )
        except Exception:
            return self._no_metric(q, normalize(q), lang)
        sql = (reply.data or {}).get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return self._no_metric(q, normalize(q), lang)
        try:
            res = self.s.run_untrusted(sql)
        except guard.Refused as exc:
            a = self._refuse(
                q, lang, "sql_refused", {"reason": Fact(exc.reason, "data")}, missing=None
            )
            a.sql = sql
            return a
        except QueryTimeout:
            a = self._refuse(q, lang, "timeout", {})
            a.sql = sql
            return a
        except Exception as exc:  # engine-level refusal or a bad query
            msg = (str(exc).splitlines() or [type(exc).__name__])[0][:160]
            a = self._refuse(q, lang, "sql_refused", {"reason": Fact(msg, "data")})
            a.sql = sql
            return a
        it = self.intent(q)
        listing = it.rank or it.by or it.compare or bool(_LIST_WORD.search(normalize(q)))
        if not res.rows or (len(res.rows) > 5 and not listing):
            key = "sql_empty" if not res.rows else "sql_table"
            a = self._refuse(q, lang, key, {"rows": Fact(len(res.rows), "count")}, missing="answer")
            a.sql = res.sql
            return a
        ans = Answer(q, lang, "sql", matched="generated")
        self._fill(ans, res, [])
        facts = {"rows": Fact(len(res.rows), "count")}
        ev = self._evidence(
            "sql:generated",
            "model-written SELECT, checked by the AST guard and run read-only",
            res,
            [],
            facts,
        )
        ans.evidence = ev
        ans.text = self._say(TEXTS["sql_rows"][lang], facts, ev, lang, {})
        value = self._single_row_text(res, lang)
        if value:  # one row: say the answer itself, not only "1 row" (the cells are the evidence)
            ans.text = f"{ans.text} {value}"
        ans.verified = verify.verified(ans.text, [ev], lang)
        ans.notes.append("generated_sql")
        return ans

    @staticmethod
    def _single_row_text(res: Any, lang: str) -> str:
        """`Result: date 2026-09-19; dau 1,489.` for a one-row result of up to four columns,
        formatted by the same templates the verifier reads; empty when it does not apply."""
        if len(res.rows) != 1 or not 1 <= len(res.columns) <= 4:
            return ""
        cells: dict[str, Fact] = {}
        parts: list[str] = []
        for i, (col, v) in enumerate(zip(res.columns, res.rows[0], strict=False)):
            if v is None or isinstance(v, (bytes, list, dict)):
                continue
            key = f"v{i}"
            cells[key] = Fact(v, _guess_unit(v))
            label = "" if col.startswith("_col") else f"{col} "
            parts.append(f"{label}{{{key}}}")
        if not parts:
            return ""
        head = {"en": "Result: ", "tr": "Sonuç: "}[lang]
        try:
            return narrate.render(head + "; ".join(parts) + ".", cells, lang, {})
        except narrate.TemplateError:
            return ""

    # -- helpers ----------------------------------------------------------------------
    def _stale(self, q: str, lang: str, app: str | None) -> Answer | None:
        if app is None or "fct_source_freshness" not in self.schema:
            return None
        from .insights.trust import PRIMARY  # GA4 or the studio SDK: the freshest one counts

        marks = ", ".join("?" * len(PRIMARY))
        rows = self.s.run_trusted(
            "SELECT data_through, source FROM fct_source_freshness WHERE app_id = ? "
            f"AND source IN ({marks}) AND data_through IS NOT NULL "
            "ORDER BY data_through DESC, source LIMIT 1",
            [app, *PRIMARY],
        ).rows
        if not rows:
            return None
        through, source = rows[0]
        lag = (self.as_of - through).days
        if lag <= 7:
            return None
        facts = {
            "app": Fact(app, "text"),
            "primary_source": Fact(PRIMARY[source], "text"),
            "data_through": Fact(through, "date"),
            "lag_days": Fact(lag, "days"),
            "as_of": Fact(self.as_of, "date"),
        }
        return self._refuse(q, lang, "stale", facts, missing="fresh data")

    def _no_metric(self, q: str, norm: str, lang: str) -> Answer:
        toks = set(_tokens(norm))
        scored = []
        for m in self.catalog.metrics.values():
            words = set(
                _tokens(
                    " ".join(
                        [
                            m.name["en"],
                            m.name["tr"],
                            *m.synonyms.get("en", ()),
                            *m.synonyms.get("tr", ()),
                        ]
                    )
                )
            )
            overlap = len(toks & words)
            if overlap:
                scored.append((-overlap, m.id))
        sug = [self.catalog.get(i).label(lang) for _, i in sorted(scored)[:3]]
        facts = {"suggestions": Fact(", ".join(sug), "text")} if sug else {}
        return self._refuse(q, lang, "no_metric", facts, missing="metric")

    def _no_data(self, ans: Answer, metric_id: str, app: str | None) -> Answer:
        m = self.catalog.metrics.get(metric_id)
        facts = {
            "metric": Fact(m.label(ans.lang) if m else metric_id, "text"),
            "app_part": Fact(f" ({app})" if app else "", "text"),
        }
        r = self._refuse(ans.question, ans.lang, "no_data", facts, missing=metric_id)
        r.sql, r.params, r.matched = ans.sql, ans.params, ans.matched
        return r

    def _refuse(
        self, q: str, lang: str, key: str, facts: dict[str, Fact], *, missing: str | None = None
    ) -> Answer:
        ev = Evidence(
            id=f"refusal:{key}", kind="refusal", app_id=None, method="router refusal", facts=facts
        )
        text = narrate.render(TEXTS[key][lang], facts, lang)
        return Answer(
            q,
            lang,
            "refused",
            text=text,
            evidence=ev,
            refusal=key,
            missing=missing,
            verified=verify.verified(text, [ev], lang),
        )

    def _fill(self, ans: Answer, res: Result, params: list[Any]) -> None:
        ans.sql, ans.params = res.sql, list(params)
        ans.columns, ans.rows, ans.truncated = (
            res.columns,
            [list(r) for r in res.rows],
            res.truncated,
        )

    @staticmethod
    def _evidence(
        eid: str, method: str, res: Result, params: list[Any], facts: dict[str, Fact]
    ) -> Evidence:
        return Evidence(
            id=eid,
            kind="answer",
            app_id=None,
            method=method,
            sql=res.sql,
            params=list(params),
            columns=res.columns,
            rows=[list(r) for r in res.rows[:200]],
            facts=facts,
            n=float(len(res.rows)),
        )

    def _say(
        self, template: str, facts: dict[str, Fact], ev: Evidence, lang: str, extra: dict[str, str]
    ) -> str:
        try:
            text = narrate.render(template, facts, lang, extra)
        except narrate.TemplateError:  # an empty cell the template needs: say only what is certain
            text = narrate.render(
                TEXTS["sql_rows"][lang], {"rows": Fact(len(ev.rows), "count")}, lang
            )
        new, _src = narrate.restyle(text, [ev], facts, lang, self.client)
        return new


def answer(
    path: str,
    question: str,
    *,
    lang: str = "en",
    client: llm.LLMClient | None = None,
    as_of: dt.date | None = None,
) -> Answer:
    """One question against a warehouse file (opens and closes the locked session)."""
    with LockedSession.open(path) as s:
        return Router(s, client=client, as_of=as_of).ask(question, lang)
