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

from . import fmt, guard, llm, narrate, verify
from .catalog import Catalog, CatalogError, Metric, load, normalize
from .evidence import Evidence, Fact
from .executor import LockedSession, QueryTimeout, Result

ROUTES = ("verified_query", "catalog", "sql", "refused")

_PII = re.compile(
    r"\b(user ?ids?|user pseudo id|pseudo ?ids?|e ?-?mails?|e ?-?posta\w*|phone\w*|telefon "
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
_LONG_TAIL = re.compile(
    r"\b(how many|count of|number of|below|above|less than|more than|at least|at most|"
    r"which day|what day|when did|kac (?:seviye|tane|kampanya|gun|uygulama|surum)\w*|"
    r"altinda|ustunde|fazla olan|az olan|hangi gun\w*)\b"
)
_LEVEL = re.compile(
    r"\b(?:level|seviye|bolum)\s*(\d{1,4})\b|\b(\d{1,4})\s*\.?\s*(?:seviye|bolum)\w*"
)
_DAYS = re.compile(r"\b(?:last|past|son|gecen)\s+(\d{1,3})\s*(?:days?|gun\w*)\b")
_WEEK = re.compile(r"\b(last week|past week|this week|son hafta\w*|gecen hafta\w*|bu hafta\w*)\b")
_MONTH = re.compile(r"\b(last month|past month|son ay\w*|gecen ay\w*|son bir ay\w*)\b")
_YESTERDAY = re.compile(r"\b(yesterday|dun|dunku)\b")
# "average rating", "overall D7", "ortalama puan": with no period named, the whole history
_OVERALL = re.compile(
    r"\b(?:average|overall|all time|ever|lifetime|ortalama\w*|genel|tum zaman\w*|butun)\b"
)
_WHY = re.compile(r"\b(?:why|neden|niye|nicin)\b")
_TREND = re.compile(
    r"\b(trend\w*|going up|going down|up or down|increas\w*|decreas\w*|grow\w*|declin\w*"
    r"|artiyor\w*|azaliyor\w*|dusuyor\w*|yukseliyor\w*|egilim\w*|gidisat\w*)\b"
)
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
# Dimensions people ask for that the warehouse does not collect at all.
_UNCOLLECTED = {
    "age": ("age", "yaş"),
    "yas": ("age", "yaş"),
    "yasa": ("age", "yaş"),
    "gender": ("gender", "cinsiyet"),
    "cinsiyet": ("gender", "cinsiyet"),
    "cinsiyete": ("gender", "cinsiyet"),
    "language": ("device language", "cihaz dili"),
    "dil": ("device language", "cihaz dili"),
    "dile": ("device language", "cihaz dili"),
    "city": ("city", "şehir"),
    "sehir": ("city", "şehir"),
    "sehre": ("city", "şehir"),
    "income": ("income", "gelir düzeyi"),
}
_COLUMN = {"network_campaign": "campaign_name"}
_STOP = set(
    "the a an of in for is are was were what which how did do does to by and or on at with "
    "per each from our we me show give tell bir ve ile icin mi mu mi nedir ne nasil hangi "
    "hangisi kac kadar en ya da".split()
)

TEXTS: dict[str, dict[str, str]] = {
    "pii": {
        "en": "This cannot be answered: the warehouse holds aggregates only; user ids, e-mails and other personal data never leave BigQuery.",
        "tr": "Bu soru cevaplanamaz: ambar yalnız toplulaştırılmış veri tutar; kullanıcı kimlikleri, e-postalar ve diğer kişisel veriler BigQuery'den hiç çıkmaz.",
    },
    "future": {
        "en": "There is no data for the future: the newest data day is {last_day}. A forecast is not an answer from data.",
        "tr": "Gelecek için veri yok: en yeni veri günü {last_day}. Bir tahmin, veriden gelen bir cevap değildir.",
    },
    "no_metric": {
        "en": "This question does not map to a metric in the catalog.[[ Close metrics: {suggestions}.]]",
        "tr": "Bu soru katalogdaki bir metriğe eşlenemedi.[[ Yakın metrikler: {suggestions}.]]",
    },
    "no_dimension": {
        "en": "{metric} has no breakdown by {dimension} in this warehouse.[[ Available: {available}.]]",
        "tr": "Bu ambarda {metric} için {dimension} kırılımı yok.[[ Mevcut: {available}.]]",
    },
    "dim_needs_pull": {
        "en": "{dimension} comes from the cohort_dims pull, which has no rows for {app} yet; enable it on the GA4 source and refresh.",
        "tr": "{dimension} kırılımı cohort_dims çekmesinden gelir ve {app} için henüz satırı yok; GA4 kaynağında açıp yenileyin.",
    },
    "dim_small_sdk": {
        "en": "{app} sends SDK data, but no {dimension} cell has reached {min_n} installs on one day yet, so there is no breakdown to show.",
        "tr": "{app} SDK verisi gönderiyor ama hiçbir {dimension} hücresi bir günde {min_n} kuruluma ulaşmadı; gösterilecek kırılım yok.",
    },
    "uncollected": {
        "en": "{dimension} is not collected: no table in the warehouse carries it.",
        "tr": "{dimension} toplanmıyor: ambardaki hiçbir tablo bu boyutu taşımıyor.",
    },
    "need_app": {
        "en": "Which app? This question needs one of: {apps}.",
        "tr": "Hangi uygulama? Bu soru şunlardan birini gerektirir: {apps}.",
    },
    "stale": {
        "en": "{primary_source} data for {app} ends {data_through}, {lag_days} days before {as_of}: refresh the data before reading answers from it.",
        "tr": "{app} için {primary_source} verisi {data_through} tarihinde bitiyor, {as_of} tarihinden {lag_days} gün önce: cevap okumadan önce veriyi yenileyin.",
    },
    "no_data": {
        "en": "{metric} has no rows{app_part} in this warehouse.",
        "tr": "Bu ambarda {metric} için{app_part} satır yok.",
    },
    "long_tail": {
        "en": "This needs a filtered or counted query; the catalog tool answers one metric over a period. Ask for the metric itself, or configure a model for checked SQL.",
        "tr": "Bu soru filtreli ya da sayımlı bir sorgu gerektiriyor; katalog aracı bir dönem için tek bir metrik cevaplar. Metriğin kendisini sorun ya da denetimli SQL için bir model yapılandırın.",
    },
    "schema": {
        "en": "This warehouse lacks a table or column the query needs; refresh it with the current version of studio-pulse.",
        "tr": "Bu ambarda sorgunun gerektirdiği bir tablo ya da sütun yok; ambarı studio-pulse'ın güncel sürümüyle yenileyin.",
    },
    "small": {
        "en": "Small sample: treat these numbers as directional.",
        "tr": "Küçük örneklem: bu sayılar yalnız yön gösterir.",
    },
    "why": {
        "en": "The cause is not something one metric can show; compare it with the release dates and the campaigns of that week.",
        "tr": "Nedeni tek bir metrikten çıkmaz; o haftanın sürüm tarihleri ve kampanyalarıyla karşılaştırın.",
    },
    "why_split": {
        "en": "The cause is not something one metric can show; to see where it moved, ask for {metric} of {app} by {dim}.",
        "tr": "Nedeni tek bir metrikten çıkmaz; nerede değiştiğini görmek için {app} {metric} değerini {dim} kırılımında sorun.",
    },
    "small_some": {
        "en": "Small sample for {apps}: treat that app's numbers as directional.",
        "tr": "{apps} için örneklem küçük: o uygulamanın sayıları yalnız yön gösterir.",
    },
    "sql_refused": {
        "en": "The generated SQL was refused by the guard: {reason}.",
        "tr": "Üretilen SQL koruma tarafından reddedildi: {reason}.",
    },
    "timeout": {
        "en": "The query was stopped by the time limit.",
        "tr": "Sorgu zaman sınırında durduruldu.",
    },
    "sql_rows": {
        "en": "The checked query returned {rows} row(s).",
        "tr": "Denetlenen sorgu {rows} satır döndürdü.",
    },
    "catalog_one": {
        "en": "{metric}{label_part}[[, {first_day} to {last_day}]]: {value}[[ (n = {n})]], {aggregation_text}.",
        "tr": "{metric}{label_part}[[, {first_day} ile {last_day} arası]]: {value}[[ (n = {n})]], {aggregation_text}.",
    },
    "catalog_many": {
        "en": "{metric}[[, {first_day} to {last_day}]] ({aggregation_text}): highest {top_label} {top_value}, lowest {bottom_label} {bottom_value}; {rows} rows.",
        "tr": "{metric}[[, {first_day} ile {last_day} arası]] ({aggregation_text}): en yüksek {top_label} {top_value}, en düşük {bottom_label} {bottom_value}; {rows} satır.",
    },
    "catalog_snapshot": {
        "en": "{metric}{label_part}: {value}[[ (n = {n})]] in the latest snapshot; {rows} rows in the table.",
        "tr": "{metric}{label_part}: son anlık görüntüde {value}[[ (n = {n})]]; tabloda {rows} satır.",
    },
}
AGG_TEXT = {
    "pooled": {"en": "pooled over the period", "tr": "dönem boyunca havuzlanmış"},
    "sum": {"en": "summed over the period", "tr": "dönem toplamı"},
    "mean_of_daily": {"en": "mean of the daily values", "tr": "günlük değerlerin ortalaması"},
    "latest": {"en": "latest value", "tr": "son değer"},
}


@dataclass
class Answer:
    question: str
    lang: str
    route: str
    text: str = ""
    sql: str = ""
    params: list[Any] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    evidence: Evidence | None = None
    matched: str | None = None
    refusal: str | None = None
    missing: str | None = None
    truncated: bool = False
    verified: bool = True
    source: str = "template"
    notes: list[str] = field(default_factory=list)

    def cells(self) -> list[list[str]]:
        """Rows formatted by code for display (units from the query or the metric)."""
        out = []
        for r in self.rows:
            out.append(
                [
                    fmt.number(v, self.units.get(c, _guess_unit(v)), self.lang)
                    if v is not None
                    else "—"
                    for c, v in zip(self.columns, r, strict=False)
                ]
            )
        return out


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


def _guess_unit(v: Any) -> str:
    if isinstance(v, (dt.date, str)):
        return "text" if isinstance(v, str) else "date"
    if isinstance(v, int):
        return "count"
    return "ratio"


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


class Router:
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
    def ask(self, question: str, lang: str = "en") -> Answer:
        lang = lang if lang in fmt.LANGS else "en"
        q = (question or "").strip()[:500]
        norm = normalize(q)
        if not norm:
            return self._refuse(q, lang, "no_metric", {})
        if _PII.search(norm):
            return self._refuse(q, lang, "pii", {}, missing="personal data")
        last = self._last_day()
        future_year = any(int(y) > (last or self.as_of).year for y in _YEAR.findall(norm))
        if _FUTURE.search(norm) or future_year:
            facts = {"last_day": Fact(last, "date")} if last else {}
            return self._refuse(q, lang, "future", facts, missing="future data")
        app = self.find_app(norm)
        named = self.find_apps(norm)
        compare = named if len(named) >= 2 else None
        if compare:
            app = None  # "compare A and B": one row per named app
        rng = self.find_range(norm, last)
        n_days = self.find_days(norm)
        if rng and n_days and (rng[1] - rng[0]).days + 1 > n_days:
            rng = (rng[1] - dt.timedelta(days=n_days - 1), rng[1])  # "last 7 days of October"
        # verified queries have fixed windows: an explicit period goes to the catalog / SQL
        vq = None if (rng or compare) else self._verified(q, norm, lang, app)
        if vq is not None:
            return vq
        long_tail = bool(_LONG_TAIL.search(norm))
        cat = None if long_tail else self._catalog(q, norm, lang, app, rng, compare)
        if cat is not None:
            return cat
        if self.client is not None:
            return self._sql(q, lang)
        if long_tail:
            return self._refuse(q, lang, "long_tail", {}, missing="query")
        return self._no_metric(q, norm, lang)

    # -- 1. verified queries ---------------------------------------------------
    def _verified(self, q: str, norm: str, lang: str, app: str | None) -> Answer | None:
        toks = _tokens(norm)
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
        for mid in self.catalog.find(q)["metric"]:
            col = self.catalog.get(mid).value
            if re.fullmatch(r"[a-z_][a-z0-9_]*", col) and col != "retention":
                metric_cols.add(col)
        best: tuple[float, VQ] | None = None
        for vq in verified_queries():
            if not all(any(_hit(w, every) for w in group) for group in vq.must):
                continue
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
            if metric_cols and not any(c.split("_")[0] in vq.sql for c in metric_cols):
                continue  # crash-free asked, the query is about retention
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

    # -- 2. catalog tool ---------------------------------------------------------
    def _catalog(
        self,
        q: str,
        norm: str,
        lang: str,
        app: str | None,
        rng: tuple[dt.date, dt.date] | None = None,
        compare: list[str] | None = None,
    ) -> Answer | None:
        found = self.catalog.find(q)
        metric_ids = found["metric"]
        dims = [d for d in found["dimension"] if d not in ("app_id", "date", "cohort_date")]
        days = self.find_days(norm)
        if not metric_ids and self.client is not None:
            routed = self._llm_route(q)
            if routed:
                metric_ids = [routed["metric"]]
                app = app or routed.get("app")
                if routed.get("by") and routed["by"] != "app_id":  # "by app" = the per-app rows
                    dims = [routed["by"]]
                days = days or routed.get("days")
        for word, (en, tr) in _UNCOLLECTED.items():
            if re.search(rf"\b{word}\b", norm) and metric_ids:
                return self._refuse(
                    q,
                    lang,
                    "uncollected",
                    {"dimension": Fact(en if lang == "en" else tr, "text")},
                    missing=en,
                )
        if not metric_ids:
            return None
        metric = self.catalog.get(metric_ids[0])
        stale = self._stale(q, lang, app)
        if stale is not None:
            return stale
        by: list[str] = []
        for d in dims[:1]:
            d = _COLUMN.get(d, d)  # a dimension's id may differ from its column (campaign)
            if d in metric.dims:
                by = [d]
            elif d in metric.pull_dims:
                return self._pull_dim(q, lang, metric, d, app, days)
            else:
                avail = [
                    self.catalog.dimensions[x].name[lang]
                    for x in metric.dims
                    if x in self.catalog.dimensions
                ]
                facts = {
                    "metric": Fact(metric.label(lang), "text"),
                    "dimension": Fact(self.catalog.dimensions[d].name[lang], "text"),
                }
                if avail:
                    facts["available"] = Fact(", ".join(avail), "text")
                return self._refuse(q, lang, "no_dimension", facts, missing=d)
        why = bool(_WHY.search(norm)) and not by and app is not None
        trend = (bool(_TREND.search(norm)) or why) and not by and app is not None
        overall = not days and rng is None and not trend and bool(_OVERALL.search(norm))
        ans = self._metric_answer(q, lang, metric, app, by, days, rng, compare, trend, overall)
        if why and ans.route == "catalog" and ans.evidence is not None:
            # one metric over time says whether it moved, not why: say so and point at the split
            splits = (*metric.dims, *metric.pull_dims)
            dim = next((d for d in ("platform", "country") if d in splits), None)
            hint = TEXTS["why_split" if dim else "why"][lang]
            dname = self.catalog.dimensions[dim].name[lang] if dim else ""
            ans.text = f"{ans.text} {hint.format(metric=metric.label(lang), app=app, dim=dname)}"
            ans.notes.append("why_not_inferred")
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
    ) -> Answer:
        start = end = None
        if metric.time and metric.aggregation != "latest":
            cond = f"WHERE {metric.where(['app_id = ?'] if app else [])}"
            got = self._trusted(
                f"SELECT MAX({metric.time}) FROM {metric.model} {cond}", [app] if app else []
            )
            if got is None:
                return self._refuse(q, lang, "schema", {}, missing=metric.id)
            last = got.rows[0][0]
            if last is None:
                return self._no_data(Answer(q, lang, "catalog", matched=metric.id), metric.id, app)
            span = days or (14 if metric.time == "cohort_date" else 7)
            end, start = last, last - dt.timedelta(days=span - 1)
            if overall:  # "average rating": every day the warehouse has, not the last week
                first = self._trusted(
                    f"SELECT MIN({metric.time}) FROM {metric.model} {cond}", [app] if app else []
                )
                if first is not None and first.rows[0][0] is not None:
                    start = first.rows[0][0]
            if rng is not None:  # the period the question names, cut at the latest data
                start, end = rng[0], min(rng[1], last)
                if start > end:
                    return self._no_data(
                        Answer(q, lang, "catalog", matched=metric.id), metric.id, app
                    )
        try:
            sql, params = metric.period_sql(app_id=app, start=start, end=end, by=by)
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
            return self._no_data(ans, metric.id, app)
        cols = res.columns
        rows = res.rows
        vi = cols.index("value")
        ni = cols.index("n") if "n" in cols else None
        label_cols = [c for c in cols if c in ("app_id", *metric.dims)]

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
        if metric.aggregation == "latest":
            r0 = valued[0]
            facts.update(
                value=Fact(r0[vi], metric.unit), label_part=Fact(f" ({label(r0)})", "text")
            )
            if ni is not None and r0[ni] is not None:
                facts["n"] = Fact(r0[ni], "count")
            key = "catalog_snapshot"
        elif len(valued) == 1:
            r0 = valued[0]
            facts.update(value=Fact(r0[vi], vunit), label_part=Fact(f" ({label(r0)})", "text"))
            if dated:
                facts.update(
                    first_day=Fact(r0[cols.index("first_day")], "date"),
                    last_day=Fact(r0[cols.index("last_day")], "date"),
                )
            if ni is not None and r0[ni] is not None:
                facts["n"] = Fact(r0[ni], "count")
            key = "catalog_one"
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
        ev = self._evidence(
            f"catalog:{metric.id}",
            f"catalog metric {metric.id} ({metric.aggregation})",
            res,
            params,
            facts,
        )
        ev.metric, ev.aggregation, ev.definition = metric.id, metric.aggregation, metric.definition
        ans.evidence = ev
        ans.text = self._say(TEXTS[key][lang], facts, ev, lang, {})
        if trend and key == "catalog_one" and start and end and (end - start).days >= 3:
            more = self._trend_text(metric, app, start, end, vunit, lang, ev)
            if more:
                ans.text = f"{ans.text} {more}"
        ans.verified = verify.verified(ans.text, [ev], lang)
        named: list[str] = []
        if app is None and "app_id" in cols:
            ai = cols.index("app_id")
            shown = [hi, lo] if key == "catalog_many" else [valued[0]]
            named = sorted({str(r[ai]) for r in shown})
        return self._small(ans, app, named)

    def _trend_text(
        self,
        metric: Metric,
        app: str | None,
        start: dt.date,
        end: dt.date,
        unit: str,
        lang: str,
        ev: Evidence,
    ) -> str:
        """Up or down: the first half of the period against the second, same definition."""
        mid = start + dt.timedelta(days=(end - start).days // 2)
        vals = []
        for a, b in ((start, mid), (mid + dt.timedelta(days=1), end)):
            sql, params = metric.period_sql(app_id=app, start=a, end=b)
            r = self._trusted(sql, params)
            if r is None or not r.rows or r.rows[0][r.columns.index("value")] is None:
                return ""
            vals.append(float(r.rows[0][r.columns.index("value")]))
        a, b = vals
        if a == 0:
            return ""
        change = (b - a) / abs(a)
        facts = {
            "first": Fact(a, unit), "second": Fact(b, unit), "change": Fact(abs(change), "pct"),
            "h1_end": Fact(mid, "date"), "h2_start": Fact(mid + dt.timedelta(days=1), "date"),
        }  # fmt: skip
        ev.facts.update({f"trend.{k}": v for k, v in facts.items()})
        word = {
            "en": ("up", "down", "flat"), "tr": ("arttı", "düştü", "yatay kaldı"),
        }[lang][0 if change > 0.02 else (1 if change < -0.02 else 2)]  # fmt: skip
        tmpl = {
            "en": "Trend: {first} until {h1_end}, {second} from {h2_start}: " + word + " {change}.",
            "tr": "Eğilim: {h1_end} tarihine kadar {first}, {h2_start} tarihinden sonra {second}: {change} "
            + word
            + ".",
        }[lang]
        try:
            return narrate.render(tmpl, facts, lang, {})
        except narrate.TemplateError:
            return ""

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

    def _pull_dim(
        self, q: str, lang: str, metric: Metric, dim: str, app: str | None, days: int | None
    ) -> Answer:
        dname = self.catalog.dimensions[dim].name[lang]
        n = {"d1_retention": 1, "d7_retention": 7, "d30_retention": 30}.get(metric.id)
        if app is None and len(self.apps) == 1:
            app = next(iter(self.apps))
        if app is None:
            return self._refuse(
                q, lang, "need_app", {"apps": Fact(", ".join(self.apps), "text")}, missing="app"
            )
        has = (
            "stg_cohort_dims" in self.schema
            and n is not None
            and self.s.run_trusted(
                "SELECT COUNT(*) FROM stg_cohort_dims WHERE app_id = ? AND grouping_set = ?",
                [app, dim],
            ).rows[0][0]
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
        span = days or 28
        sql = (
            f"SELECT {dim}, SUM(users) AS n, CAST(SUM(d{n}_users) AS DOUBLE) / NULLIF(SUM(users), 0) AS value, "
            "MIN(cohort_date) AS first_day, MAX(cohort_date) AS last_day "
            f"FROM stg_cohort_dims WHERE app_id = ? AND grouping_set = ? AND d{n}_users IS NOT NULL "
            f"AND cohort_date > (SELECT MAX(cohort_date) FROM stg_cohort_dims WHERE app_id = ? AND d{n}_users IS NOT NULL) - ? "
            "GROUP BY 1 ORDER BY n DESC"
        )
        params = [app, dim, app, span]
        res = self.s.run_trusted(sql, params)
        ans = Answer(q, lang, "catalog", matched=f"{metric.id}:{dim}")
        ans.units = {
            "value": "pct",
            "n": "count",
            "first_day": "date",
            "last_day": "date",
            dim: "version",
        }
        self._fill(ans, res, params)
        valued = [r for r in res.rows if r[2] is not None]
        if not valued:
            return self._no_data(ans, metric.id, app)
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
        ev = self._evidence(
            f"catalog:{metric.id}:{dim}",
            f"cohort_dims pull, {metric.id} by {dim} (pooled)",
            res,
            params,
            facts,
        )
        ans.evidence = ev
        ans.text = self._say(TEXTS["catalog_many"][lang], facts, ev, lang, {})
        ans.verified = verify.verified(ans.text, [ev], lang)
        return ans

    def _llm_route(self, q: str) -> dict[str, Any] | None:
        assert self.client is not None
        payload = {
            "question": q,
            "metrics": {m.id: m.name["en"] for m in self.catalog.metrics.values()},
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
        out: dict[str, Any] = {"metric": metric}
        if isinstance(d.get("app"), str) and d["app"] in self.apps:
            out["app"] = d["app"]
        if isinstance(d.get("by"), str) and d["by"] in self.catalog.dimensions:
            out["by"] = d["by"]
        if isinstance(d.get("days"), int) and 1 <= d["days"] <= 365:
            out["days"] = d["days"]
        return out

    # -- 3. guarded SQL ------------------------------------------------------------
    def _sql(self, q: str, lang: str) -> Answer:
        assert self.client is not None
        payload = {
            "question": q,
            "apps": list(self.apps),  # the values of app_id, in every table
            "tables": {t: list(c) for t, c in self.schema.items()},
            "metrics": {
                m.id: f"{m.model}.{m.value}: {m.definition}" for m in self.catalog.metrics.values()
            },
        }
        try:
            reply = self.client.generate(llm.request("sql", llm.SYSTEM_SQL, payload))
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
