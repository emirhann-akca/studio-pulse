"""Narration: template first, an optional model may restyle, the verifier decides.

1. A fixed template per insight kind and language is filled with the
   evidence facts, each formatted by fmt.number (TR `%60,9`, EN `60.9%`).
   Slots in [[...]] are optional (dropped when a fact is missing); `|abs`
   prints the magnitude when the words carry the direction.
2. If an LLM client is configured, it may rewrite the paragraph; the
   rewrite is accepted only if verify.py finds every number in the cited
   evidence. One retry with the rejected numbers named, then the template.
3. Recommendations are fixed action texts, each citing evidence ids.
4. Correlational findings get a fixed sentence and an A/B-test action;
   no model wording can remove them because they are appended after it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import fmt, llm, verify
from .evidence import Fact, Insight

T = dict[str, dict[str, tuple[str, str]]]

TEMPLATES: T = {
    "data_trust_block": {
        "en": ("Data first: {app}", ""),
        "tr": ("Önce veri: {app}", ""),
    },
    "data_trust_warn": {
        "en": ("Data check: {app}", ""),
        "tr": ("Veri kontrolü: {app}", ""),
    },
    "anomaly_drop": {
        "en": (
            "{metric} dropped ({app})",
            "{metric} was {value} on {date}, {change|abs} below the trailing {baseline_days}-day "
            "{baseline_kind} {baseline} (rule limit {threshold})[[; z = {z}, n = {n}]].",
        ),
        "tr": (
            "{metric_tr} düştü ({app})",
            "{metric_tr} {date} tarihinde {value}; son {baseline_days} günün {baseline_kind} "
            "{baseline} değerinin {change|abs} altında (kural sınırı {threshold})[[; z = {z}, n = {n}]].",
        ),
    },
    "anomaly_rise": {
        "en": (
            "{metric} rose ({app})",
            "{metric} was {value} on {date}, {change|abs} above the trailing {baseline_days}-day "
            "{baseline_kind} {baseline} (rule limit {threshold}).",
        ),
        "tr": (
            "{metric_tr} yükseldi ({app})",
            "{metric_tr} {date} tarihinde {value}; son {baseline_days} günün {baseline_kind} "
            "{baseline} değerinin {change|abs} üstünde (kural sınırı {threshold}).",
        ),
    },
    "anomaly_above": {
        "en": (
            "{metric} above its limit ({app})",
            "{metric} was {value} on {date}, above {threshold}[[ ({where})]].",
        ),
        "tr": (
            "{metric_tr} sınırın üstünde ({app})",
            "{metric_tr} {date} tarihinde {value}; sınır {threshold}[[ ({where})]].",
        ),
    },
    "anomaly_below": {
        "en": (
            "{metric} below its limit ({app})",
            "{metric} was {value} on {date}, below {threshold}[[ ({where})]].",
        ),
        "tr": (
            "{metric_tr} sınırın altında ({app})",
            "{metric_tr} {date} tarihinde {value}; alt sınır {threshold}[[ ({where})]].",
        ),
    },
    "anomaly_band": {
        "en": (
            "{metric} outside its expected band ({app})",
            "{metric} was {value} on {date}; the weekday-aware forecast expected {baseline} "
            "(band edge {threshold}).",
        ),
        "tr": (
            "{metric_tr} beklenen bandın dışında ({app})",
            "{metric_tr} {date} tarihinde {value}; haftanın gününe göre tahmin {baseline} "
            "(bant sınırı {threshold}).",
        ),
    },
    "retention_drop_cause": {
        "en": (
            "D{day_n} retention fell to {r1} ({app}): concentrated in {cause_dim} = {cause_value}",
            "Cohort {cur_end}: {r1} against {r0} for the cohorts {base_start} to {base_end} "
            "(pooled; n = {n1}). Users with {cause_dim} = {cause_value} kept {cause_rate}, the "
            "rest of the same cohorts {rest_rate} ({cause_share} of users, {cause_q})"
            "[[; that cell explains {cause_ep} of the change]][[, and kept {cause_rate0} in the "
            "baseline]]. The change {delta} splits into rate {rate} within segments and mix {mix} "
            "between them.",
        ),
        "tr": (
            "D{day_n} elde tutma {r1} değerine düştü ({app}): {cause_dim} = {cause_value} kırılımında yoğun",
            "{cur_end} kohortu: {r1}; {base_start} ile {base_end} arası kohortlarda {r0} "
            "(havuzlanmış; n = {n1}). {cause_dim} = {cause_value} olan kullanıcılar {cause_rate}, "
            "aynı kohortların geri kalanı {rest_rate} tuttu (kullanıcıların {cause_share} kadarı, "
            "{cause_q})[[; bu hücre değişimin {cause_ep} kadarını açıklıyor]][[ ve taban dönemde "
            "{cause_rate0} tutuyordu]]. {delta} değişim, segment içi oran {rate} ve segmentler arası "
            "karışım {mix} olarak ayrışıyor.",
        ),
    },
    "retention_drop_broad": {
        "en": (
            "D{day_n} retention fell to {r1} ({app}) across segments",
            "Cohort {cur_end}: {r1} against {r0} for the cohorts {base_start} to {base_end} "
            "(pooled; n = {n1}). No segment of the {cells_scanned} scanned cells retains clearly "
            "worse than the rest: the cause touches all users (store listing, traffic mix, a "
            "server-side change). Rate {rate}, mix {mix}.",
        ),
        "tr": (
            "D{day_n} elde tutma tüm segmentlerde {r1} değerine düştü ({app})",
            "{cur_end} kohortu: {r1}; {base_start} ile {base_end} arası kohortlarda {r0} "
            "(havuzlanmış; n = {n1}). Taranan {cells_scanned} hücrenin hiçbiri geri kalandan açıkça "
            "kötü değil: neden bütün kullanıcılara dokunuyor (mağaza sayfası, trafik karışımı, "
            "sunucu tarafı değişiklik). Oran {rate}, karışım {mix}.",
        ),
    },
    "retention_drop_nodims": {
        "en": (
            "D{day_n} retention fell to {r1} ({app}); the cause cannot be located yet",
            "Cohort {cur_end}: {r1} against {r0} for the cohorts {base_start} to {base_end} "
            "(pooled; n = {n1}). This warehouse splits cohorts by platform only: rate {rate}, "
            "mix {mix}. Missing dimensions: {missing_dims}.",
        ),
        "tr": (
            "D{day_n} elde tutma {r1} değerine düştü ({app}); neden henüz yerelleştirilemiyor",
            "{cur_end} kohortu: {r1}; {base_start} ile {base_end} arası kohortlarda {r0} "
            "(havuzlanmış; n = {n1}). Bu ambar kohortları yalnız platforma göre ayırıyor: oran "
            "{rate}, karışım {mix}. Eksik boyutlar: {missing_dims}.",
        ),
    },
    "difficult_level": {
        "en": (
            "Level {level} is a difficulty spike ({app})",
            "Fail rate {fail_rate} ({ci_level}% CI {fail_lo}–{fail_hi}; n = {n} attempts) against "
            "the app's median level {med_fail}; robust z {robust_z}. Churn at this level "
            "{churn_rate} (median {med_churn}); {attempts} attempts per completion.",
        ),
        "tr": (
            "Seviye {level} bir zorluk sıçraması ({app})",
            "Başarısızlık oranı {fail_rate} (%{ci_level} GA {fail_lo}–{fail_hi}; n = {n} deneme), "
            "uygulamanın medyan seviyesi {med_fail}; sağlam z {robust_z}. Bu seviyede bırakma "
            "{churn_rate} (medyan {med_churn}); tamamlama başına {attempts} deneme.",
        ),
    },
    "level_survival": {
        "en": (
            "Half of the starters are gone by level {median_level} ({app})",
            "{s_median} of the {starters} players who started are still playing after level "
            "{median_level} ({ci_level}% CI {s_lo}–{s_hi}).[[ Steepest drop: level {steep_level}, "
            "where {steep_hazard} of those reaching it stop (typical level {typical_hazard}).]]",
        ),
        "tr": (
            "Başlayanların yarısı seviye {median_level} itibarıyla kayıp ({app})",
            "Başlayan {starters} oyuncunun {s_median} kadarı seviye {median_level} sonrasında hâlâ "
            "oynuyor (%{ci_level} GA {s_lo}–{s_hi}).[[ En dik düşüş seviye {steep_level}: ulaşanların "
            "{steep_hazard} kadarı orada bırakıyor (tipik seviye {typical_hazard}).]]",
        ),
    },
    "level_survival_nomedian": {
        "en": (
            "Most starters are still playing at level {last_level} ({app})",
            "{s_last} of the {starters} players who started are still playing at level "
            "{last_level} ({ci_level}% CI {s_lo}–{s_hi}).",
        ),
        "tr": (
            "Başlayanların çoğu seviye {last_level} noktasında hâlâ oynuyor ({app})",
            "Başlayan {starters} oyuncunun {s_last} kadarı seviye {last_level} noktasında hâlâ "
            "oynuyor (%{ci_level} GA {s_lo}–{s_hi}).",
        ),
    },
    "perf_regression": {
        "en": (
            "Version {version_new} made {tier}-tier frames slower ({app})",
            "Median frame time {ft_new} on {version_new} against {ft_old} on {version_old} in the "
            "same days ({rel|abs} slower[[; {ci_level}% CI {ft_new_lo}–{ft_new_hi}]]; {windows_new} "
            "vs {windows_old} perf windows, since {since}).",
        ),
        "tr": (
            "{version_new} sürümü {tier} sınıf cihazlarda kareleri yavaşlattı ({app})",
            "Medyan kare süresi {version_new} sürümünde {ft_new}, aynı günlerde {version_old} "
            "sürümünde {ft_old} ({rel|abs} daha yavaş[[; %{ci_level} GA {ft_new_lo}–{ft_new_hi}]]; "
            "{windows_new} ve {windows_old} performans penceresi, {since} tarihinden beri).",
        ),
    },
    "crash_regression_version": {
        "en": (
            "Crashes up since version {version} appeared ({app})",
            "Crashed users per DAU {crash_after} since {since} ({days_after} days) against "
            "{crash_before} in the {days_before} days before ({ratio}x); crash-free users "
            "{crash_free_after}.[[ Play vitals user-perceived crash rate {vitals_after} vs "
            "{vitals_before} (Play's bad-behaviour threshold {play_bad}).]]",
        ),
        "tr": (
            "{version} sürümü çıktığından beri çökmeler arttı ({app})",
            "DAU başına çöken kullanıcı {since} tarihinden beri {crash_after} ({days_after} gün), "
            "önceki {days_before} günde {crash_before} ({ratio} kat); çökmesiz kullanıcı "
            "{crash_free_after}.[[ Play vitals kullanıcı tarafından görülen çökme oranı "
            "{vitals_after}, önce {vitals_before} (Play'in kötü davranış eşiği {play_bad}).]]",
        ),
    },
    "crash_regression": {
        "en": (
            "Crashes up in the last days ({app})",
            "Crashed users per DAU {crash_after} in the last {days_after} days against "
            "{crash_before} in the {days_before} days before ({ratio}x); crash-free users "
            "{crash_free_after}.",
        ),
        "tr": (
            "Son günlerde çökmeler arttı ({app})",
            "DAU başına çöken kullanıcı son {days_after} günde {crash_after}, önceki "
            "{days_before} günde {crash_before} ({ratio} kat); çökmesiz kullanıcı {crash_free_after}.",
        ),
    },
    "money_drop": {
        "en": (
            "{metric} down ({app})",
            "{metric} {after} in the last {recent_days} days against {before} in the "
            "{prior_days} before ({rel|abs} lower; pooled).",
        ),
        "tr": (
            "{metric_tr} düştü ({app})",
            "{metric_tr} son {recent_days} günde {after}, önceki {prior_days} günde {before} "
            "({rel|abs} düşük; havuzlanmış).",
        ),
    },
    "money_rise": {
        "en": (
            "{metric} up ({app})",
            "{metric} {after} in the last {recent_days} days against {before} in the "
            "{prior_days} before ({rel|abs} higher; pooled).",
        ),
        "tr": (
            "{metric_tr} yükseldi ({app})",
            "{metric_tr} son {recent_days} günde {after}, önceki {prior_days} günde {before} "
            "({rel|abs} yüksek; havuzlanmış).",
        ),
    },
    "segment_correlation": {
        "en": (
            "{property} = {value_b} goes with a different D{day_n} ({app})",
            "Players with {property} = {value_b} on their first day have D{day_n} {rate_b}, those "
            "with {value_a} {rate_a} (n = {n_b} / {n_a}; difference {diff}, {ci_level}% CI "
            "{diff_lo} to {diff_hi}). This is an association: players who change {property} may "
            "differ in engagement, device or cohort.[[ Within day-0 session strata the difference "
            "is {diff_adjusted}.]] It does not show that the setting changes retention.",
        ),
        "tr": (
            "{property} = {value_b} farklı bir D{day_n} ile birlikte görülüyor ({app})",
            "İlk gün {property} = {value_b} olan oyuncuların D{day_n} değeri {rate_b}, {value_a} "
            "olanların {rate_a} (n = {n_b} / {n_a}; fark {diff}, %{ci_level} GA {diff_lo} ile "
            "{diff_hi}). Bu bir ilişkidir; {property} değerini değiştiren oyuncular etkileşim, "
            "cihaz ya da kohort bakımından farklı olabilir.[[ İlk gün oturum katmanlarında fark "
            "{diff_adjusted}.]] Ayarın elde tutmayı değiştirdiğini göstermez.",
        ),
    },
}

REASONS: T = {
    "old_source": {
        "en": (
            "",
            "{primary_source} data ends {data_through}, {lag_days} days before {as_of} (more than {old_after}): explanations are withheld until it is fresh.",
        ),
        "tr": (
            "",
            "{primary_source} verisi {data_through} tarihinde bitiyor, {as_of} tarihinden {lag_days} gün önce ({old_after} günden fazla): veri tazelenene kadar açıklama yapılmıyor.",
        ),
    },
    "stale_source": {
        "en": (
            "",
            "{primary_source} data ends {data_through}, {lag_days} days before {as_of} (more than {stale_after}): the newest days may be incomplete.",
        ),
        "tr": (
            "",
            "{primary_source} verisi {data_through} tarihinde bitiyor, {as_of} tarihinden {lag_days} gün önce ({stale_after} günden fazla): en yeni günler eksik olabilir.",
        ),
    },
    "stale_other": {
        "en": ("", "Sources more than {stale_after} days behind: {stale_sources}."),
        "tr": ("", "{stale_after} günden fazla geride kalan kaynaklar: {stale_sources}."),
    },
    "failed_check": {
        "en": (
            "",
            "Failed error-level data-quality checks ({failed_checks}): {failed_check_names}.",
        ),
        "tr": (
            "",
            "Başarısız hata düzeyi veri kalitesi kontrolleri ({failed_checks}): {failed_check_names}.",
        ),
    },
    "failed_warning": {
        "en": ("", "Data-quality warnings ({failed_checks}): {failed_check_names}."),
        "tr": ("", "Veri kalitesi uyarıları ({failed_checks}): {failed_check_names}."),
    },
    "missing_days": {
        "en": ("", "{missing_days} day(s) without DAU inside the {window_days} days with data."),
        "tr": ("", "Verili {window_days} günün arasında DAU'su olmayan {missing_days} gün var."),
    },
    "zero_days": {
        "en": (
            "",
            "{zero_days} day(s) with zero DAU inside the active period; the longest run is {zero_run} days ({zero_from} to {zero_to}): either nobody played or the export missed those days.",
        ),
        "tr": (
            "",
            "Aktif dönem içinde DAU'su sıfır olan {zero_days} gün var; en uzun seri {zero_run} gün ({zero_from} ile {zero_to} arası): ya kimse oynamadı ya da dışa aktarım bu günleri kaçırdı.",
        ),
    },
    "volume_drop": {
        "en": (
            "",
            "The last day's DAU ({dau_last}) is under half of the week before (median {dau_prior_median}): an export may be incomplete.",
        ),
        "tr": (
            "",
            "Son günün DAU'su ({dau_last}) önceki haftanın yarısının altında (medyan {dau_prior_median}): bir dışa aktarım eksik olabilir.",
        ),
    },
    "small_sample": {
        "en": (
            "",
            "Median DAU over the last two weeks {dau_median_14d}[[, median install cohort {cohort_median_14d}]], under {min_n}: findings are directional only.",
        ),
        "tr": (
            "",
            "Son iki haftada medyan DAU {dau_median_14d}[[, medyan kurulum kohortu {cohort_median_14d}]]; {min_n} altında: bulgular yalnız yön gösterir.",
        ),
    },
}

CAVEATS: dict[str, dict[str, str]] = {
    "association_in_time": {
        "en": "Association in time: the split shows where the change is, not what caused it.",
        "tr": "Zamansal ilişki: kırılım değişimin nerede olduğunu gösterir, neyin yol açtığını değil.",
    },
    "levels_in_order": {
        "en": "Levels are assumed to be played in order.",
        "tr": "Seviyelerin sırayla oynandığı varsayılır.",
    },
    "window_start": {
        "en": "Players who started before the data window are not seen.",
        "tr": "Veri penceresinden önce başlayan oyuncular görülmez.",
    },
    "small_sample": {
        "en": "Small sample: treat the numbers as directional.",
        "tr": "Küçük örneklem: sayılar yalnız yön gösterir.",
    },
    "user_days": {
        "en": "Counted in user-days: a user who crashes on several days counts each day.",
        "tr": "Kullanıcı-gün olarak sayılır: birkaç gün çöken kullanıcı her gün için sayılır.",
    },
    "dims_missing": {
        "en": "Country, version, source and tier are not in this warehouse's cohort tables.",
        "tr": "Ülke, sürüm, kaynak ve cihaz sınıfı bu ambarın kohort tablolarında yok.",
    },
    "pair_cause": {
        "en": "The cell combines two dimensions; neither explains the change on its own.",
        "tr": "Hücre iki boyutun birleşimi; ikisi de değişimi tek başına açıklamıyor.",
    },
    "gpu_bound": {
        "en": "GPU-bound: the GPU p95 is close to the frame p95 (a heavier shader or overdraw).",
        "tr": "GPU sınırlı: GPU p95 değeri kare p95 değerine yakın (daha ağır bir gölgelendirici ya da üst üste çizim).",
    },
    "correlational": {"en": "", "tr": ""},
}

ACTIONS: dict[str, dict[str, str]] = {
    "reduce_difficulty": {
        "en": "Lower this level's difficulty, or offer a hint or booster after repeated fails; ship it as an A/B test.",
        "tr": "Bu seviyenin zorluğunu düşürün ya da tekrarlanan başarısızlıklardan sonra ipucu veya booster önerin; değişikliği A/B testi olarak yayınlayın.",
    },
    "ab_test_change": {
        "en": "Validate the change with an A/B test (pulse experiment) before rolling it out to everyone.",
        "tr": "Değişikliği herkese açmadan önce bir A/B testiyle doğrulayın (pulse experiment).",
    },
    "rollback_or_fix_version": {
        "en": "Roll back or hotfix the release, then compare the next version here.",
        "tr": "Sürümü geri alın ya da düzeltme yayınlayın, sonra bir sonraki sürümü burada karşılaştırın.",
    },
    "investigate_anomaly": {
        "en": "Check releases, campaigns and data health around that day.",
        "tr": "O gün çevresindeki sürümleri, kampanyaları ve veri sağlığını kontrol edin.",
    },
    "investigate_segment": {
        "en": "Look at what changed for this segment on those days (store listing, campaign, OS update).",
        "tr": "Bu segment için o günlerde neyin değiştiğine bakın (mağaza sayfası, kampanya, işletim sistemi güncellemesi).",
    },
    "enable_cohort_dims": {
        "en": "Enable the cohort_dims pull on the GA4 source to split cohorts by country, version, source and tier.",
        "tr": "Kohortları ülke, sürüm, kaynak ve cihaz sınıfına göre ayırmak için GA4 kaynağında cohort_dims çekmesini açın.",
    },
    "investigate_crashes": {
        "en": "Open the period's crash clusters in Crashlytics.",
        "tr": "Dönemin çökme kümelerini Crashlytics'te açın.",
    },
    "fix_data_source": {
        "en": "Fix the data first: reload the stale source or fix the failed check, then read the insights.",
        "tr": "Önce veriyi düzeltin: bayat kaynağı yeniden yükleyin ya da başarısız kontrolü giderin, sonra içgörüleri okuyun.",
    },
    "small_sample_directional": {
        "en": "Wait for more users before acting on these numbers.",
        "tr": "Bu sayılara göre karar vermeden önce daha fazla kullanıcı bekleyin.",
    },
    "check_monetization": {
        "en": "Check pricing, ad placements and network fill for the period.",
        "tr": "Dönemin fiyatlarını, reklam yerleşimlerini ve ağ doluluğunu kontrol edin.",
    },
    "ab_test_setting": {
        "en": "To learn whether the setting itself matters, A/B test its default (pulse experiment).",
        "tr": "Ayarın kendisinin etkisini öğrenmek için varsayılan değerini A/B testiyle değiştirin (pulse experiment).",
    },
}

CORRELATIONAL = {
    "en": "This is an association, not a measured cause; to test a cause, run an A/B experiment (pulse experiment).",
    "tr": "Bu bir ilişkidir, ölçülmüş bir neden değildir; nedeni test etmek için bir A/B deneyi kurun (pulse experiment).",
}

TEXT_VALUES = {
    "pooled": {"en": "pooled rate", "tr": "havuzlanmış oranı"},
    "mean_of_daily": {"en": "mean", "tr": "ortalaması"},
    "low": {"en": "low", "tr": "düşük"},
    "mid": {"en": "mid", "tr": "orta"},
    "high": {"en": "high", "tr": "yüksek"},
}

_OPT = re.compile(r"\[\[(.*?)\]\]", re.S)
_SLOT = re.compile(r"\{([\w.]+)(\|abs)?\}")


class TemplateError(KeyError):
    pass


def _dimension_names(value: str, lang: str) -> str:
    from .catalog import load

    dims = load().dimensions
    parts = [p.strip() for p in value.replace(",", " × ").split(" × ")]
    if all(p in dims for p in parts):
        return " × ".join(dims[p].name[lang] for p in parts)
    return value


def _value(name: str, abs_: bool, facts: dict[str, Fact], extra: dict[str, str], lang: str) -> str:
    if name in extra:
        return extra[name]
    f = facts[name]
    v = f.value
    if f.unit == "text" and isinstance(v, str) and v in TEXT_VALUES:
        return TEXT_VALUES[v][lang]
    if f.unit == "text" and isinstance(v, str) and (name.endswith("_dim") or name == "dimension"):
        return _dimension_names(v, lang)
    if abs_ and isinstance(v, (int, float)):
        v = abs(v)
    return fmt.number(v, f.unit, lang)


def render(
    template: str, facts: dict[str, Fact], lang: str, extra: dict[str, str] | None = None
) -> str:
    extra = extra or {}

    def present(name: str) -> bool:
        return name in extra or (name in facts and facts[name].value is not None)

    def opt(m: re.Match[str]) -> str:
        inner = m.group(1)
        return inner if all(present(n) for n, _ in _SLOT.findall(inner)) else ""

    text = _OPT.sub(opt, template)

    def slot(m: re.Match[str]) -> str:
        name = m.group(1)
        if not present(name):
            raise TemplateError(f"missing fact {name!r}")
        return _value(name, bool(m.group(2)), facts, extra, lang)

    return _cap(_SLOT.sub(slot, text), lang)


_SENTENCE = re.compile(r"(^|[.!?]\s+)([a-zçğıöşü])")


def _cap(text: str, lang: str) -> str:
    """Sentence case where a sentence starts with a slot ("crash-free users ..." -> "Crash-free ...")."""

    def up(m: re.Match[str]) -> str:
        ch = m.group(2)
        word = re.match(r"\S*", text[m.start(2) :]).group(0)  # type: ignore[union-attr]
        if re.search(r"[_\d]", word) or re.search(r"[A-Z]", word[1:]):
            return m.group(0)  # an identifier (app id, eCPM) keeps its case
        return m.group(1) + ("İ" if lang == "tr" and ch == "i" else ch.upper())

    return _SENTENCE.sub(up, text)


@dataclass
class Narration:
    title: str
    body: str
    caveats: list[str]
    actions: list[tuple[str, list[str]]]
    lang: str
    source: str = "template"  # template | narrator
    verified: bool = True
    unsupported: list[str] = field(default_factory=list)

    def text(self) -> str:
        parts = [self.title, self.body, *self.caveats, *(a for a, _ in self.actions)]
        return "\n".join(p for p in parts if p)


def _body(
    ins: Insight, facts: dict[str, Fact], lang: str, extra: dict[str, str]
) -> tuple[str, str]:
    if ins.kind == "data_trust":
        title = render(TEMPLATES[ins.template][lang][0], facts, lang, extra)
        reasons = [r for r in ins.main.caveats if r in REASONS]
        body = " ".join(render(REASONS[r][lang][1], facts, lang, extra) for r in reasons)
        return title, body
    title_t, body_t = TEMPLATES[ins.template][lang]
    return render(title_t, facts, lang, extra), render(body_t, facts, lang, extra)


def template_narration(ins: Insight, lang: str) -> Narration:
    facts = ins.facts()
    extra = {"app": ins.app_id or "portfolio"}
    title, body = _body(ins, facts, lang, extra)
    caveats = []
    for ev in ins.evidence:
        for c in ev.caveats:
            text = CAVEATS.get(c, {}).get(lang)
            if text and text not in caveats:
                caveats.append(text)
    if not ins.causal and ins.kind != "segment":
        caveats.append(CORRELATIONAL[lang])
    actions = [(ACTIONS[r.action][lang], list(r.evidence)) for r in ins.recommendations]
    n = Narration(title, body, caveats, actions, lang)
    bad = verify.unsupported(n.text(), ins.evidence, lang)
    if bad:  # a template bug: never show an unverified number
        n = Narration(
            f"{ins.kind} ({ins.app_id})",
            "",
            [],
            actions,
            lang,
            verified=False,
            unsupported=bad,
        )
    return n


_FORBIDDEN_TEXT = re.compile(
    r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|<\s*/?\s*script|```|"
    r"\b(?:DROP|DELETE|INSERT|UPDATE|ATTACH|COPY|PRAGMA|INSTALL)\b",
    re.IGNORECASE,
)


def restyle(
    text: str,
    evidence: list[Any],
    facts: dict[str, Fact],
    lang: str,
    client: llm.LLMClient | None,
) -> tuple[str, str]:
    """(text, source): the model's rewrite if it passes every check, else the input.

    Checks: every number verified against the evidence; no URL, e-mail, script
    or SQL statement word (an injected instruction in the data would show up
    there); not more than twice as long; one retry with the rejected numbers.
    """
    if client is None or not text:
        return text, "template"
    facts_json = {
        k: {
            "value": fmt.number(f.value, f.unit, lang),
            "untrusted": f.unit in ("text", "version", "data"),
        }
        for k, f in facts.items()
    }
    payload: dict[str, Any] = {"lang": lang, "text": text, "facts": facts_json}
    for _attempt in range(2):
        try:
            reply = client.generate(llm.request("restyle", llm.SYSTEM_RESTYLE, payload))
        except Exception:  # a provider failure falls back to the template
            return text, "template"
        new = (reply.data or {}).get("text") if reply.data else None
        if not isinstance(new, str) or not new.strip():
            continue
        new = new.strip()
        bad = verify.unsupported(new, evidence, lang)
        if not bad and not _FORBIDDEN_TEXT.search(new) and len(new) <= 2 * len(text) + 40:
            return new, "narrator"
        payload = {**payload, "rejected_numbers": bad}
    return text, "template"


def narrate(ins: Insight, lang: str, client: llm.LLMClient | None = None) -> Narration:
    base = template_narration(ins, lang)
    if client is None or not base.verified:
        return base
    body, source = restyle(base.body, ins.evidence, ins.facts(), lang, client)
    if source == "template":
        return base
    return Narration(base.title, body, base.caveats, base.actions, lang, source)
