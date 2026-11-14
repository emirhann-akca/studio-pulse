"""Answers of the question router and the texts they are written from.

Split out of qa.py so the router (qa.py) and the catalog tool (catalog_answers.py)
share one Answer type, one table of templates and the question patterns.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from . import fmt
from .evidence import Evidence

# "average rating", "overall D7", "ortalama puan": with no period named, the whole history
_OVERALL = re.compile(
    r"\b(?:average|overall|all time|ever|lifetime|ortalama\w*|genel|tum zaman\w*|butun)\b"
)

_WHY = re.compile(r"\b(?:why|neden|niye|nicin)\b")

_TREND = re.compile(
    r"\b(trend\w*|going up|going down|up or down|increas\w*|decreas\w*|grow\w*|declin\w*"
    r"|artiyor\w*|azaliyor\w*|dusuyor\w*|yukseliyor\w*|egilim\w*|gidisat\w*)\b"
)

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

_RETENTION_IDS = (
    "d1_retention",
    "d7_retention",
    "d14_retention",
    "d30_retention",
    "headline_retention",
)

_RETENTION_WORD = re.compile(
    r"\b(retention|retain\w*|elde tutma\w*|tutma\w*|tutunma\w*|geri don\w*|retention\w*)\b"
)

_QUIT_WORD = re.compile(
    r"\b(quit\w*|give up|gives up|giving up|churn\w*|drop off|birak\w*|terk\w*)\b"
)

_BREAKDOWN = re.compile(
    r"\b(by|per|each|split|breakdown|broken down|vs|versus|across|gore|bazinda|her|which|hangi\w*|"
    r"kirilim\w*|karsilastir\w*|compare\w*|and|ve|ile)\b"
)

_PEOPLE = re.compile(
    r"\b(how many (?:\w+ ){0,2}(?:users|players|people)|kac (?:\w+ ){0,2}(?:kullanici|oyuncu)\w*)\b"
)

_LIST_WORD = re.compile(
    r"\b(list|show|each|every|all|table|daily|per day|by day|listele|goster|her|tum|gunluk)\b"
)

# "sessions per daily user", "kullanıcı başına": a rate, never a raw count or a sum
_PER_UNIT = re.compile(
    r"\b(per (?:daily |active )?(?:user|dau|player|install|session)s?|(?:kullanici|oyuncu|kurulum|"
    r"oturum) basina)\b"
)

MIN_RANK_N = 50  # smallest sample a ranked proportion may rest on (insights.common.MIN_N)

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
    "sql_empty": {
        "en": "The model's checked query found no rows: there is no data for this question.",
        "tr": "Modelin denetlenen sorgusu hiç satır bulmadı: bu soru için veri yok.",
    },
    "sql_table": {
        "en": "The model's checked query returned a table of {rows} rows, not one answer; ask for a specific value or a breakdown.",
        "tr": "Modelin denetlenen sorgusu tek bir cevap değil {rows} satırlık bir tablo döndürdü; belirli bir değer ya da kırılım sorun.",
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
    "unknown_app": {
        "en": "There is no app called {name} in this warehouse. Apps: {apps}.",
        "tr": "Bu ambarda {name} adında bir uygulama yok. Uygulamalar: {apps}.",
    },
    "ambiguous_app": {
        "en": "More than one app fits: {apps}. Name the one you mean.",
        "tr": "Birden fazla uygulama uyuyor: {apps}. Hangisini kastettiğinizi yazın.",
    },
    "unknown_metric": {
        "en": "{metric} is not collected: no table in this warehouse carries it.[[ Close metrics: {suggestions}.]]",
        "tr": "{metric} toplanmıyor: bu ambardaki hiçbir tablo bunu taşımıyor.[[ Yakın metrikler: {suggestions}.]]",
    },
    "no_such_value": {
        "en": "{app_part}has no data for {filter}.[[ Values with data: {values}.]]",
        "tr": "{app_part}için {filter} verisi yok.[[ Verisi olanlar: {values}.]]",
    },
    "filter_not_applied": {
        "en": "I can't apply {filters} to {metric}: the tables it comes from do not carry that split, and the unfiltered number would answer another question.",
        "tr": "{metric} için {filters} filtresini uygulayamıyorum: geldiği tablolarda bu kırılım yok ve filtresiz sayı başka bir sorunun cevabı olur.",
    },
    "period_not_applied": {
        "en": "{metric} comes from a table that covers {window}; it cannot be cut to {period}.",
        "tr": "{metric} {window} dönemini kapsayan bir tablodan gelir; {period} dönemine göre kesilemez.",
    },
    "period_unchecked": {
        "en": "The query behind this answer does not show that it covers {period}, so the answer is withheld.",
        "tr": "Bu cevabın sorgusu {period} dönemini kapsadığını göstermiyor; cevap verilmedi.",
    },
    "no_period_data": {
        "en": "There is no data for {period}: {metric}{app_part} has data from {first_day} to {last_day}.",
        "tr": "{period} için veri yok: {metric}{app_part} verisi {first_day} ile {last_day} arasında.",
    },
    "no_filter_rows": {
        "en": "{metric}{app_part} has no rows for {filter}.[[ Values with data: {values}.]]",
        "tr": "{metric}{app_part} için {filter} satırı yok.[[ Verisi olanlar: {values}.]]",
    },
    "typo": {
        "en": "(Read {written} as {app}.)",
        "tr": "({written} yazımı {app} olarak okundu.)",
    },
    "trend_prev": {
        "en": "Trend: {prev} for {p_start} to {p_end}, then {value}: {word}.",
        "tr": "Eğilim: {p_start} ile {p_end} arası {prev}, sonra {value}: {word}.",
    },
    "headline": {
        "en": "Dashboard headline ({window}): {headline}.",
        "tr": "Panodaki özet değer ({window}): {headline}.",
    },
    "total": {
        "en": "{metric}, all apps together[[, {first_day} to {last_day}]]: {value}, {aggregation_text}.",
        "tr": "{metric}, tüm uygulamalar birlikte[[, {first_day} ile {last_day} arası]]: {value}, {aggregation_text}.",
    },
    "ranked": {
        "en": "{metric}[[, {first_day} to {last_day}]], {order}: {items}.",
        "tr": "{metric}[[, {first_day} ile {last_day} arası]], {order}: {items}.",
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


def _guess_unit(v: Any) -> str:
    if isinstance(v, (dt.date, str)):
        return "text" if isinstance(v, str) else "date"
    if isinstance(v, int):
        return "count"
    return "ratio"
