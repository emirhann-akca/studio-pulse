"""The shared shell of every server page: design system, language, icons and texts.

Every page extends base.html, which draws the Studio's sidebar (apps, tools, settings)
around the page, so moving between the Studio, data health, alerts, live view, insights
and the market radar never feels like changing products. The CSS and JS are the
studio-ui design system shared with Creative Forge (static/ui.css, static/ui.js),
inlined into each page: the app makes no external requests.

Language: Turkish by default, English with ?lang=en; the choice is remembered in a
cookie, so links between pages do not need to carry it.
"""

from __future__ import annotations

import datetime as dt
from importlib import resources
from typing import Any

from .. import runstate

LANGS = ("tr", "en")
COOKIE = "ui-lang"


def _static(name: str) -> str:
    return (
        resources.files("studio_pulse.serve").joinpath("static", name).read_text(encoding="utf-8")
    )


UI_CSS = _static("ui.css")
UI_JS = _static("ui.js")


def request_lang(request: Any, explicit: str | None = None) -> str:
    """?lang= first, then the remembered cookie, then Turkish."""
    if explicit in LANGS:
        return str(explicit)
    if request is not None:
        q = request.query_params.get("lang")
        if q in LANGS:
            return str(q)
        c = request.cookies.get(COOKIE)
        if c in LANGS:
            return str(c)
    return "tr"


_P = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'
_PATHS = {
    "logo": '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "radar": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l6-6"/>',
    "ads": '<path d="M3 11v2a1 1 0 0 0 1 1h3l5 4V6L7 10H4a1 1 0 0 0-1 1z"/><path d="M16 9a3 3 0 0 1 0 6M19 6a7 7 0 0 1 0 12"/>',
    "live": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "health": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "alerts": '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    "insights": '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7-4.7-1.8 4.7-1.8z"/><path d="M19 15l.8 2.2 2.2.8-2.2.8L19 21l-.8-2.2-2.2-.8 2.2-.8z"/>',
    "ask": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
    "dashboard": '<path d="M4 20V10M10 20V4M16 20v-7M21 20H3"/>',
    "settings": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "plug": '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0zM12 17v5"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "warn": '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17h.01"/>',
    "levels": '<path d="M4 20h4v-6H4zM10 20h4V9h-4zM16 20h4V4h-4z"/>',
    "apps": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
}


def icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" {_P} aria-hidden="true">{_PATHS.get(name, "")}</svg>'


def age(iso: str | None, lang: str = "tr") -> str:
    t = runstate.parse_iso(iso)
    if t is None:
        return "hiç" if lang == "tr" else "never"
    s = (dt.datetime.now(dt.UTC) - t).total_seconds()
    if lang == "tr":
        if s < 90:
            return f"{int(s)} sn önce"
        if s < 5400:
            return f"{int(s // 60)} dk önce"
        if s < 172800:
            return f"{s / 3600:.1f} sa önce".replace(".", ",")
        return f"{s / 86400:.1f} gün önce".replace(".", ",")
    if s < 90:
        return f"{int(s)} s ago"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.1f} days ago"


# The shell and the server pages (the Studio's own texts are in studio.py; the brain's in
# brain/web.py). One key per string; English and Turkish must have the same keys.
S: dict[str, dict[str, str]] = {
    "tr": {
        "app_live": "canlı",
        "app_testing": "test",
        "app_new": "yeni",
        "app_ga4": "GA4",
        "app_waiting": "bekliyor",
        "app_waiting_long": "SDK bekleniyor: hiçbir derleme henüz veri göndermiyor",
        "app_live_long": "mağazada yayında",
        "app_testing_long": "test derlemeleri veri gönderiyor; mağaza sürümü henüz yok",
        "app_new_long": "SDK'dan ilk olaylar geldi",
        "app_ga4_long": "GA4 / mağaza dışa aktarımlarından",
        "players_now": "Şu an oynayan",
        "failing_checks": "başarısız denetim",
        "alerts_last": "son yenilemedeki uyarı",
        "apps": "Uygulamalar",
        "new_section": "Yeni gelenler",
        "tools": "Araçlar",
        "radar": "Pazar radarı",
        "ads": "Reklamlar",
        "ads_sub": "Creative Forge",
        "ads_off": "Creative Forge açık değil: simgesinden ya da `forge app` ile açın.",
        "live_nav": "Canlı",
        "health": "Veri sağlığı",
        "alerts": "Uyarılar",
        "insights": "İçgörüler",
        "ask": "Sor",
        "dashboard": "Ayrıntılı pano",
        "settings": "Ayarlar",
        "search": "Ara veya git…",
        "search_ph": "Uygulama, sayfa ya da araç ara",
        "light": "Açık tema",
        "dark": "Koyu tema",
        "other_lang": "English",
        "snapshot": "Son başarılı yenilemenin kopyası gösteriliyor.",
        "snapshot_sub": "Şu an bir yenileme ambarı yazıyor; bitince canlı veriye dönülür.",
        "no_wh": "Henüz veri ambarı yok.",
        "no_wh_sub": "Veri Sağlığı sayfasından ilk yenilemeyi başlatın.",
        "local_only": "Yalnız bu bilgisayardan erişilir (127.0.0.1).",
        "refresh_now": "Şimdi yenile",
        "never": "hiç çalışmadı",
        # health
        "h_title": "Veri sağlığı",
        "h_sub": "Yenilemeler, günlük zamanlama, veri kalitesi denetimleri ve kaynakların tazeliği.",
        "h_last": "Son yenileme",
        "h_success": "Son başarılı",
        "h_error": "Son hata",
        "h_none": "yok",
        "h_took": "sürdü",
        "h_started": "başladı",
        "h_last_fail": "son başarısızlık",
        "h_schedule": "Günlük zamanlama",
        "h_sched_on": "kurulu",
        "h_sched_off": "kurulu değil",
        "h_sched_how": "Her sabah otomatik yenileme için bir kez şu komutu çalıştırın:",
        "h_sched_entry": "Zamanlayıcı kaydı",
        "h_sched_state": "Durum",
        "h_sched_next": "Sonraki çalışma",
        "h_sched_exit": "Son çıkış kodu",
        "h_runs": "Son yenilemeler",
        "h_started_utc": "Başlangıç (UTC)",
        "h_cmd": "Komut",
        "h_status": "Durum",
        "h_dur": "Süre",
        "h_alerts": "Uyarı",
        "h_check_err": "Denetim hatası",
        "h_err": "Hata",
        "h_no_runs": "Henüz yenileme kaydı yok.",
        "h_checks": "Veri kalitesi",
        "h_checks_sum": "{passed}/{checks} denetim geçti · {errors} hata · {warnings} uyarı",
        "h_failing": "Başarısız denetimler",
        "h_passing": "Geçen denetimler ({n})",
        "h_check": "Denetim",
        "h_table": "Tablo",
        "h_sev": "Önem",
        "h_rows": "Satır",
        "h_result": "Sonuç",
        "h_no_checks": "Henüz denetim sonucu yok.",
        "h_fresh": "Kaynak tazeliği",
        "h_app": "Uygulama",
        "h_source": "Kaynak",
        "h_through": "Veri sonu",
        "h_lag": "Gecikme (gün)",
        "h_loaded": "Son yükleme (UTC)",
        "h_fresh_note": "Gecikme {as_of} tarihine göre ölçülür.",
        "h_no_fresh": "Henüz yükleme kaydı yok.",
        "st_success": "başarılı",
        "st_failed": "başarısız",
        "st_running": "çalışıyor",
        "st_interrupted": "yarıda kaldı",
        "st_pass": "geçti",
        "st_fail": "başarısız",
        "st_error": "hata",
        "st_warn": "uyarı",
        "run_started": "Yenileme başladı (pid {pid}). Bitince bu sayfa güncellenir.",
        "run_busy": "Zaten bir yenileme çalışıyor (pid {pid}, {since} tarihinden beri).",
        # alerts
        "a_title": "Uyarılar",
        "a_sub": "Kurallar her yenilemede çalışır; eşikler alerts.yaml dosyasında.",
        "a_last": "Son yenilemede",
        "a_ok": "Son yenilemede uyarı yok.",
        "a_history": "Geçmiş (90 gün)",
        "a_fired": "Zaman (UTC)",
        "a_rule": "Kural",
        "a_day": "Gün",
        "a_msg": "Mesaj",
        "a_none": "Kayıtlı uyarı yok.",
        "a_note": "Başarısız veri kalitesi denetimleri Veri Sağlığı sayfasında.",
        "sev_critical": "kritik",
        "sev_warning": "uyarı",
        "sev_info": "bilgi",
        # live
        "l_title": "Canlı",
        "l_sub": "Son 5 dakikada olay gönderen kurulumlar; uygulama, mağaza, platform ve ülkeye göre. 5 saniyede bir yenilenir.",
        "l_active": "Şu an aktif (5 dk)",
        "l_apps": "uygulama",
        "l_events": "Bu dakikadaki olay",
        "l_all": "tüm uygulamalar",
        "l_accepted": "Toplayıcı açıldığından beri",
        "l_dups": "tekrar",
        "l_quar": "karantina",
        "l_collector": "Toplayıcı",
        "l_down": "Toplayıcı çalışmıyor; canlı sayılar yok.",
        "l_down_sub": "Bulut toplayıcı kurulana kadar bu normal. Yerelde denemek için `pulse collect` çalıştırın; sayfa 5 saniyede bir yeniden dener.",
        "l_new": "Yeni uygulama kaydoldu: {name}",
        "l_new_sub": "({app_id}, {platform}) · ilk olay {ago} · yapılandırma değişikliği gerekmez",
        "l_installs": "aktif kurulum",
        "l_last_event": "son olay",
        "l_platform": "Platform",
        "l_store": "Mağaza",
        "l_country": "Ülke",
        "l_build": "Derleme",
        "l_active_col": "Aktif",
        "l_more": "+ {n} satır daha",
        "l_nobody": "Son 5 dakikada kimse yok.",
        "l_levels_link": "Bölümler, ölüm ısı haritası, ayarlar",
        "l_empty": "Henüz hiçbir uygulama veri göndermedi. Bir SDK'yı (ya da `pulse simulate`) toplayıcıya yönlendirin.",
        # levels (sdk app page)
        "lv_back": "Canlı",
        "lv_title": "{name}: bölümler",
        "lv_none": "Ambarda bu uygulamanın bölüm olayı yok. Oyun level_start / level_complete / level_fail gönderdikten sonraki yenilemede görünür.",
        "lv_heat": "Isı haritası bölümü",
        "lv_heat_note": "(en çok ölümün olduğu bölümler önce)",
        "lv_funnel": "Bölüm hunisi",
        "lv_settings": "Oyuncuların kapattığı ayarlar",
        "lv_setting": "Ayar",
        "lv_players": "Oyuncu",
        "lv_off": "Kapatan",
        "lv_share": "Oran",
        "lv_first": "İlk oturumda",
        "lv_median": "Medyan oturum",
        "lv_dq": "Veri kalitesi",
        "lv_dq_line": "{events} olay, {installs} kurulum · sıra kaybı {loss} · tekrar {dup} · karantina {quar} · saat kayması p95 {skew} ms",
        # radar page
        "r_full": "Pazar radarı: tam tablo",
        "set_sub": "Veri kaynaklarını bağlayın ve günlük yenilemeyi kurun. Ayarlar pulse.settings.yaml dosyasına yazılır; pulse.yaml'a dokunulmaz.",
        "set_sources": "Veri kaynakları",
        "set_source": "Kaynak",
        "set_status": "Durum",
        "set_through": "Veri sonu",
        "set_rows": "Satır",
        "set_from": "Ayarlar'dan",
        "set_none": "Bu uygulamaya bağlı kaynak yok.",
        "src_ok": "veri geliyor",
        "src_stale": "bayat",
        "src_nodata": "henüz veri yok",
        "set_play_title": "Google Play'i bağla",
        "set_play_help": "Play Console → Raporları indir → İstatistikler → “Cloud Storage URI'yi kopyala”. Kova kimliği pubsite_prod_rev_ ile başlar. Kova, GA4 için zaten giriş yapılmış Google hesabıyla okunur; yeni anahtar gerekmez, ama o hesabın Play Console'da raporları görme izni olmalı.",
        "set_bucket": "Rapor kovası (Cloud Storage URI)",
        "set_package": "Paket adı",
        "set_asc_title": "App Store'u bağla",
        "set_asc_help": "App Store Connect → Kullanıcılar ve Erişim → Entegrasyonlar → App Store Connect API: “Satış” (Sales) erişimli bir anahtar oluşturup .p8 dosyasını indirin. Satıcı numarası Ödemeler ve Mali Raporlar sayfasında, Apple kimliği uygulamanın App Information sayfasında. Anahtarın kendisi bu bilgisayarda kalır; yalnız dosya yolu kaydedilir.",
        "set_apple_id": "Apple kimliği (Apple ID)",
        "set_vendor": "Satıcı numarası (Vendor number)",
        "set_key_id": "Anahtar kimliği (Key ID)",
        "set_issuer": "Yayıncı kimliği (Issuer ID)",
        "set_key_path": ".p8 dosyasının yolu",
        "set_keys_state": "Kayıtlı anahtar değişkenleri (değerler gösterilmez)",
        "set_var_on": "ayarlı",
        "set_var_off": "yok",
        "set_save": "Kaydet",
        "set_test": "Bağlantıyı dene",
        "set_saved": "Kaydedildi. Bir sonraki yenilemede bu kaynak okunur (Veri sağlığı → Şimdi yenile).",
        "set_unknown_app": "Bu uygulama yapılandırmada yok.",
        "set_bad_bucket": "Rapor kovası pubsite_prod_rev_ ile başlamalı (Play Console'daki Cloud Storage URI).",
        "set_bad_package": "Paket adı com.ornek.oyun biçiminde olmalı.",
        "set_bad_ids": "Apple kimliği ve satıcı numarası yalnız rakamlardan oluşmalı.",
        "set_bad_key": "Anahtar kimliği 10 karakter (büyük harf ve rakam), yayıncı kimliği bir UUID olmalı.",
        "set_bad_key_path": "Bu yolda bir .p8 dosyası bulunamadı.",
        "set_test_first": "Önce bu kaynağı kaydedin.",
        "set_play_ok": "Bağlantı çalışıyor: bu paket için {n} kurulum raporu bulundu.",
        "set_play_empty": "Kovaya erişildi ama bu paket adına kurulum raporu yok; paket adını kontrol edin (raporların oluşması yayından sonra birkaç gün sürebilir).",
        "set_asc_ok": "App Store Connect anahtarı çalışıyor.",
        "set_test_fail": "Bağlanamadı: {detail}",
        "set_sched_title": "Günlük yenileme",
        "set_at": "Saat",
        "set_install": "Kur",
        "set_sched_ok": "Günlük yenileme kuruldu: her gün {at}.",
        "set_sched_help": "Bilgisayar açıkken her gün verileri yeniler, uyarıları ve içgörüleri hesaplar.",
        "set_where": "Ayar dosyası",
        "static_dash": "Pano",
        "static_ua": "Kullanıcı edinme",
        "static_exp": "Deneyler",
    },
    "en": {
        "app_live": "live",
        "app_testing": "testing",
        "app_new": "new",
        "app_ga4": "GA4",
        "app_waiting": "waiting",
        "app_waiting_long": "awaiting SDK: no build sends data yet",
        "app_live_long": "live in the store",
        "app_testing_long": "test builds send data; no store release yet",
        "app_new_long": "first events from the SDK",
        "app_ga4_long": "from GA4 / store exports",
        "players_now": "Playing now",
        "failing_checks": "failing check(s)",
        "alerts_last": "alert(s) in the last refresh",
        "apps": "Apps",
        "new_section": "New arrivals",
        "tools": "Tools",
        "radar": "Market radar",
        "ads": "Ads",
        "ads_sub": "Creative Forge",
        "ads_off": "Creative Forge is not open: start it from its icon or with `forge app`.",
        "live_nav": "Live",
        "health": "Data health",
        "alerts": "Alerts",
        "insights": "Insights",
        "ask": "Ask",
        "dashboard": "Full dashboard",
        "settings": "Settings",
        "search": "Search or jump to…",
        "search_ph": "Search apps, pages or tools",
        "light": "Light theme",
        "dark": "Dark theme",
        "other_lang": "Türkçe",
        "snapshot": "Showing the snapshot of the last good refresh.",
        "snapshot_sub": "A refresh is writing the warehouse right now; live data returns when it finishes.",
        "no_wh": "No warehouse yet.",
        "no_wh_sub": "Start the first refresh from the Data health page.",
        "local_only": "Served to this computer only (127.0.0.1).",
        "refresh_now": "Refresh now",
        "never": "never run",
        "h_title": "Data health",
        "h_sub": "Refreshes, the daily schedule, data-quality checks and how fresh each source is.",
        "h_last": "Last refresh",
        "h_success": "Last success",
        "h_error": "Last error",
        "h_none": "none",
        "h_took": "took",
        "h_started": "started",
        "h_last_fail": "last failure",
        "h_schedule": "Daily schedule",
        "h_sched_on": "registered",
        "h_sched_off": "not registered",
        "h_sched_how": "Run this once for an automatic refresh every morning:",
        "h_sched_entry": "Scheduler entry",
        "h_sched_state": "State",
        "h_sched_next": "Next run",
        "h_sched_exit": "Last exit code",
        "h_runs": "Recent refreshes",
        "h_started_utc": "Started (UTC)",
        "h_cmd": "Command",
        "h_status": "Status",
        "h_dur": "Duration",
        "h_alerts": "Alerts",
        "h_check_err": "Check errors",
        "h_err": "Error",
        "h_no_runs": "No runs recorded yet.",
        "h_checks": "Data quality",
        "h_checks_sum": "{passed} of {checks} checks passed · {errors} error(s) · {warnings} warning(s)",
        "h_failing": "Failing checks",
        "h_passing": "Passing checks ({n})",
        "h_check": "Check",
        "h_table": "Table",
        "h_sev": "Severity",
        "h_rows": "Rows",
        "h_result": "Result",
        "h_no_checks": "No check results yet.",
        "h_fresh": "Source freshness",
        "h_app": "App",
        "h_source": "Source",
        "h_through": "Data through",
        "h_lag": "Lag (days)",
        "h_loaded": "Last load (UTC)",
        "h_fresh_note": "Lag is measured against {as_of}.",
        "h_no_fresh": "No loads recorded yet.",
        "st_success": "success",
        "st_failed": "failed",
        "st_running": "running",
        "st_interrupted": "interrupted",
        "st_pass": "pass",
        "st_fail": "fail",
        "st_error": "error",
        "st_warn": "warning",
        "run_started": "Refresh started (pid {pid}). This page updates when it finishes.",
        "run_busy": "A refresh is already running (pid {pid}, since {since}).",
        "a_title": "Alerts",
        "a_sub": "Rules run on every refresh; thresholds live in alerts.yaml.",
        "a_last": "Last refresh",
        "a_ok": "No alerts fired in the last refresh.",
        "a_history": "History (90 days)",
        "a_fired": "Fired (UTC)",
        "a_rule": "Rule",
        "a_day": "Day",
        "a_msg": "Message",
        "a_none": "No alerts recorded.",
        "a_note": "Failed data-quality checks are on the Data health page.",
        "sev_critical": "critical",
        "sev_warning": "warning",
        "sev_info": "info",
        "l_title": "Live",
        "l_sub": "Installs with an event in the last 5 minutes, per app, store, platform and country. Updates every 5 seconds.",
        "l_active": "Active now (5 min)",
        "l_apps": "app(s)",
        "l_events": "Events this minute",
        "l_all": "all apps",
        "l_accepted": "Accepted since start",
        "l_dups": "duplicates",
        "l_quar": "quarantined",
        "l_collector": "Collector",
        "l_down": "Collector not reachable; no live numbers.",
        "l_down_sub": "This is normal until the cloud collector is set up. To try it locally, run `pulse collect`; this page retries every 5 seconds.",
        "l_new": "New app registered: {name}",
        "l_new_sub": "({app_id}, {platform}) · first event {ago} · no config edit needed",
        "l_installs": "active installs",
        "l_last_event": "last event",
        "l_platform": "Platform",
        "l_store": "Store",
        "l_country": "Country",
        "l_build": "Build",
        "l_active_col": "Active",
        "l_more": "+ {n} more",
        "l_nobody": "Nobody in the last 5 minutes.",
        "l_levels_link": "Levels, death heatmap, settings",
        "l_empty": "No app has sent anything yet. Point an SDK (or `pulse simulate`) at the collector.",
        "lv_back": "Live",
        "lv_title": "{name}: levels",
        "lv_none": "No level events in the warehouse for this app yet. Levels appear after the next refresh once the game sends level_start / level_complete / level_fail.",
        "lv_heat": "Heatmap level",
        "lv_heat_note": "(levels with the most deaths first)",
        "lv_funnel": "Level funnel",
        "lv_settings": "Settings players turn off",
        "lv_setting": "Setting",
        "lv_players": "Players",
        "lv_off": "Turned off",
        "lv_share": "Share",
        "lv_first": "In first session",
        "lv_median": "Median session",
        "lv_dq": "Data quality",
        "lv_dq_line": "{events} events from {installs} installs · sequence loss {loss} · duplicates {dup} · quarantined {quar} · clock skew p95 {skew} ms",
        "r_full": "Market radar: full table",
        "set_sub": "Connect data sources and set up the daily refresh. Settings are written to pulse.settings.yaml; pulse.yaml is not touched.",
        "set_sources": "Data sources",
        "set_source": "Source",
        "set_status": "Status",
        "set_through": "Data through",
        "set_rows": "Rows",
        "set_from": "from Settings",
        "set_none": "No sources connected to this app.",
        "src_ok": "data flowing",
        "src_stale": "stale",
        "src_nodata": "no data yet",
        "set_play_title": "Connect Google Play",
        "set_play_help": "Play Console → Download reports → Statistics → “Copy Cloud Storage URI”. The bucket id starts with pubsite_prod_rev_. The bucket is read with the Google account already signed in for GA4; no new key is needed, but that account must be allowed to see reports in the Play Console.",
        "set_bucket": "Reports bucket (Cloud Storage URI)",
        "set_package": "Package name",
        "set_asc_title": "Connect the App Store",
        "set_asc_help": "App Store Connect → Users and Access → Integrations → App Store Connect API: create a key with Sales access and download its .p8 file. The vendor number is on Payments and Financial Reports, the Apple ID on the app's App Information page. The key itself stays on this computer; only its path is saved.",
        "set_apple_id": "Apple ID",
        "set_vendor": "Vendor number",
        "set_key_id": "Key ID",
        "set_issuer": "Issuer ID",
        "set_key_path": "Path to the .p8 file",
        "set_keys_state": "Stored key variables (values never shown)",
        "set_var_on": "set",
        "set_var_off": "missing",
        "set_save": "Save",
        "set_test": "Test connection",
        "set_saved": "Saved. The next refresh reads this source (Data health → Refresh now).",
        "set_unknown_app": "This app is not in the config.",
        "set_bad_bucket": "The reports bucket starts with pubsite_prod_rev_ (the Cloud Storage URI in the Play Console).",
        "set_bad_package": "The package name looks like com.example.game.",
        "set_bad_ids": "The Apple ID and vendor number are digits only.",
        "set_bad_key": "The Key ID is 10 upper-case letters and digits; the Issuer ID is a UUID.",
        "set_bad_key_path": "No .p8 file at this path.",
        "set_test_first": "Save this source first.",
        "set_play_ok": "Connected: {n} install report(s) found for this package.",
        "set_play_empty": "The bucket answered but has no install reports for this package; check the package name (reports appear a few days after release).",
        "set_asc_ok": "The App Store Connect key works.",
        "set_test_fail": "Could not connect: {detail}",
        "set_sched_title": "Daily refresh",
        "set_at": "Time",
        "set_install": "Set up",
        "set_sched_ok": "Daily refresh set up: every day at {at}.",
        "set_sched_help": "While the computer is on, it refreshes the data every day and computes alerts and insights.",
        "set_where": "Settings file",
        "static_dash": "Dashboard",
        "static_ua": "User acquisition",
        "static_exp": "Experiments",
    },
}

PAGE_ICONS = {
    "market": "radar",
    "live": "live",
    "health": "health",
    "alerts": "alerts",
    "insights": "insights",
    "ask": "ask",
    "dashboard": "dashboard",
    "settings": "settings",
    "radar": "radar",
}


def commands(
    s: dict[str, str], apps: list[dict[str, Any]], nav: dict[str, Any]
) -> list[dict[str, str]]:
    """The ⌘K palette: every app, then every page this copy of the app has."""
    out = [{"label": a["name"], "href": nav["app"](a["id"]), "hint": s["apps"]} for a in apps]
    out.append({"label": s["radar"], "href": nav["market"], "hint": s["tools"]})
    if nav.get("static"):
        d = nav["dash"]
        return out + [
            {"label": s["static_dash"], "href": d + "index.html", "hint": s["tools"]},
            {"label": s["static_ua"], "href": d + "ua.html", "hint": s["tools"]},
            {"label": s["static_exp"], "href": d + "experiments.html", "hint": s["tools"]},
        ]
    return out + [
        {"label": s[k], "href": href, "hint": s["tools"]}
        for k, href in (
            ("live_nav", "/live"),
            ("insights", "/insights"),
            ("ask", "/ask"),
            ("alerts", "/alerts"),
            ("health", "/health"),
            ("settings", "/settings"),
            ("dashboard", "/dashboard/index.html"),
        )
    ]
