"""Connections / data flow (`/connections`, "Bağlantılar"): where every number comes from.

One view that answers "is the data flowing?" for the whole studio:
- the refresh itself: the last run, the last success, whether the daily schedule is
  registered *for this config* (the scheduler entry has one fixed name, so it can belong to
  another config), the next run, and a scheduled run that failed before it could write the
  run history (a config error, a crash) read from the end of the schedule's own log file;
- per app and per source: connected or not, the last successful fetch (meta_loads), the
  rows dated in the last 7 days, the error of the last run when the source failed, and for
  anything missing or broken the single next step (which key, which settings field, which
  command);
- the studio-wide feeds: market radar, Google Play pages, the watch list, industry news,
  estimates, the weekly ad-intel extract, game dossiers, the SDK collector, the LLM and
  notifications.

Read only: it never fetches anything, and every value is rendered autoescaped.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any

from .. import runstate, schedule

ORDER = {"error": 0, "stale": 1, "nodata": 2, "watch_wait": 3, "ok": 4, "info": 5,
         "ondemand": 6, "disabled": 7, "off": 8}  # fmt: skip
STALE_AFTER_H = 26  # a daily refresh missed once (a sleeping laptop catches up at wake)
LAG_OK_DAYS = 3  # the same threshold as the Settings page and the Today brief

# labels shared with the Settings page (serve/settings_page.py keeps its own copy for its
# forms; both read the same source kinds)
KINDS = {
    "ga4_bigquery": ("GA4 (BigQuery)", "GA4 (BigQuery)"),
    "ga4_files": ("GA4 (dışa aktarım dosyaları)", "GA4 (export files)"),
    "crashlytics_bigquery": ("Crashlytics (BigQuery)", "Crashlytics (BigQuery)"),
    "crashlytics_files": ("Crashlytics (dosyalar)", "Crashlytics (files)"),
    "pulse_sdk": ("Studio Pulse SDK", "Studio Pulse SDK"),
    "play_console": ("Google Play raporları", "Google Play reports"),
    "app_store": ("App Store satış raporları", "App Store sales reports"),
    "play_reviews": ("Google Play yorumları", "Google Play reviews"),
    "app_store_reviews": ("App Store yorumları", "App Store reviews"),
    "android_vitals": ("Android vitals", "Android vitals"),
    "revenuecat": ("RevenueCat", "RevenueCat"),
    "admob": ("AdMob", "AdMob"),
    "unity_jsonl": ("Unity telemetri", "Unity telemetry"),
    "meta_ads": ("Meta reklam harcaması", "Meta ad spend"),
    "google_ads": ("Google Ads harcaması", "Google Ads spend"),
    "tiktok_ads": ("TikTok reklam harcaması", "TikTok ad spend"),
    "applovin_ads": ("AppLovin reklam harcaması", "AppLovin ad spend"),
    "unity_ads": ("Unity Ads reklam harcaması", "Unity Ads ad spend"),
    "ua_csv": ("Reklam harcaması (CSV)", "Ad spend (CSV)"),
}
STORE_NAMES = {"app_store": "App Store", "google_play": "Google Play", "apple": "App Store",
               "play": "Google Play"}  # fmt: skip

# environment variables a source cannot run without (sources with a `replay:` need none)
UA_NETWORKS = ("applovin_ads", "unity_ads", "meta_ads", "google_ads", "tiktok_ads")
_UA_KEYS = {
    "applovin_ads": "PULSE_APPLOVIN_REPORT_KEY",
    "unity_ads": "PULSE_UNITY_ADS_KEY_ID + PULSE_UNITY_ADS_SECRET_KEY",
    "meta_ads": "PULSE_META_ACCESS_TOKEN",
    "google_ads": "PULSE_GOOGLE_ADS_DEVELOPER_TOKEN",
    "tiktok_ads": "PULSE_TIKTOK_ACCESS_TOKEN",
}
LAUNCHD_STATE = {
    "not running": ("beklemede", "idle"),
    "running": ("çalışıyor", "running"),
    "waiting": ("bekliyor", "waiting"),
    "spawn scheduled": ("başlatılıyor", "starting"),
}

T: dict[str, dict[str, str]] = {
    "tr": {
        "title": "Bağlantılar",
        "open_app": "Uygulama sayfası →",
        "sub": "Her uygulamanın ve stüdyonun veri kaynakları: bağlı mı, en son ne zaman veri geldi, son 7 günde kaç satır, sıradaki yenileme ne zaman ve eksik olan için tek adım.",
        "refresh": "Yenileme",
        "last_run": "Son yenileme",
        "last_ok": "Son başarılı",
        "next_run": "Sonraki yenileme",
        "never": "hiç",
        "none": "yok",
        "running": "şu an çalışıyor",
        "took": "{s} sürdü",
        "sched_off": "Günlük yenileme kurulu değil: veriler yalnız elle yenilenir.",
        "sched_off_step": "Ayarlar → Günlük yenileme → Kur",
        "sched_other": "Günlük yenileme başka bir yapılandırma için kurulu ({path}); bu yapılandırma otomatik yenilenmiyor.",
        "sched_other_step": "Bu yapılandırma için kurmak isterseniz Ayarlar → Günlük yenileme → Kur (öbürünün yerini alır).",
        "sched_at": "her gün {at}",
        "sched_wake": "bilgisayar o saatte uykudaysa uyanınca çalışır",
        "stale": "Son başarılı yenileme {age}; veriler güncel değil.",
        "stale_step": "Veri sağlığı → Şimdi yenile; tekrarlarsa aşağıdaki hatalı kaynağa bakın.",
        "failed": "Son yenileme başarısız oldu: {error}",
        "bg_failed": "Zamanlanmış yenileme {when} çıkış kodu {rc} ile bitti ve kayıt bırakamadı: {line}",
        "bg_locked": "Zamanlanmış yenileme {when} başlarken başka bir yenileme sürüyordu; bir sonraki saatte yeniden dener.",
        "bg_log": "Ayrıntı günlük dosyasında: {path}",
        "all_ok": "Veri akıyor: son yenileme başarılı, tüm bağlı kaynaklar güncel.",
        "apps": "Uygulamalar",
        "studio": "Stüdyo geneli",
        "source": "Kaynak",
        "state": "Durum",
        "fetched": "Son veri çekme",
        "through": "Veri sonu",
        "rows7": "Satır (7 gün)",
        "next": "Sıradaki",
        "step": "Ne yapmalı",
        "s_ok": "veri geliyor",
        "s_stale": "bayat",
        "s_nodata": "veri yok",
        "s_error": "hata",
        "s_off": "bağlı değil",
        "s_disabled": "kapalı",
        "s_ondemand": "isteğe bağlı",
        "s_watch_wait": "ilk okuma bekleniyor",
        "days": "{n} gün",
        "today": "bugün",
        "pkg": "<paket adı>",
        "weekly_next": "{date} (haftalık)",
        "on_demand": "elle",
        "with_run": "yenilemeyle",
        # per-source steps
        "step_env": "Şu ortam değişkenini ekleyin: {names} (pulse.settings.env ya da yapılandırmanın env_file dosyası); sonra Şimdi yenile.",
        "step_adc": "Bu bilgisayarda bir kez `gcloud auth application-default login` çalıştırın (GA4 için kullanılan Google hesabıyla).",
        "step_perm": "Google hesabının bu projede BigQuery okuma izni olmalı (BigQuery Data Viewer + Job User); sonra Şimdi yenile.",
        "step_error": "Yukarıdaki hatayı giderip Veri sağlığı → Şimdi yenile.",
        "step_enable": "Kaynak yapılandırmada `enabled: false`; açmak için o satırı silin.",
        "step_sdk_cloud": "Mağaza derlemelerinin olayları bulut toplayıcıdan gelir: deploy/cloudrun/bootstrap.sh ile kurun, sonra pulse.yaml'da collector.url, read_key ve bigquery satırlarını açın.",
        "step_sdk_bundles": "Toplayıcıya bu paketlerden olay gelmedi ({bundles}): SDK'nın derlemede açık olduğunu ve bundle_ids listesini kontrol edin.",
        "step_ga4": "Firebase → Proje ayarları → Entegrasyonlar → BigQuery: Google Analytics günlük dışa aktarımı açık mı, kontrol edin.",
        "step_crash": "Crashlytics yalnız çökme olan günlerde satır yazar; az kullanıcıda birkaç günlük boşluk normaldir. Sürerse Firebase → Crashlytics → BigQuery bağlantısını kontrol edin.",
        "step_stale": "{lag} gündür yeni gün gelmiyor ve son yenilemede hata yok: kaynağın kendisi yeni veri yazmıyor; dışa aktarımını kontrol edin.",
        "step_nodata": "Son yenileme bu kaynaktan satır getirmedi; kaynağın yolunu / kimliğini kontrol edin.",
        "step_play": "Ayarlar → {app} → Google Play'i bağla (rapor kovası + paket adı; yeni anahtar gerekmez).",
        "step_asc": "Ayarlar → {app} → App Store'u bağla (Satış erişimli App Store Connect API anahtarı).",
        "step_ua": "Reklam harcaması için ağın kaynağını pulse.yaml'da apps.{app}.sources altına ekleyin: {opts}.",
        "step_watch": "Herkese açık mağaza sayfası (puan, sürüm, sıra) anahtarsız izlenir: `pulse radar watch add {target}`.",
        "step_watch_radar": "Önce pazar radarını açın: pulse.yaml → radar.enabled: true (yeni anahtar gerekmez).",
        "step_watch_wait": "İzleme listesinde; ilk okuma bir sonraki yenilemede.",
        "row_play_reports": "Mağaza raporları · Google Play",
        "row_asc_reports": "Mağaza raporları · App Store",
        "row_public": "Herkese açık mağaza sayfası (izleme listesi)",
        "row_ua": "Reklam ağları (UA)",
        # studio rows
        "radar": "Pazar radarı (App Store listeleri)",
        "play_pages": "Google Play sayfaları",
        "watch": "İzleme listesi",
        "news": "Sektör haberleri",
        "estimates": "Tahminler (indirme / gelir bantları)",
        "adintel": "Reklam istihbaratı (haftalık, Google Ads Transparency)",
        "dossiers": "Oyun dosyaları",
        "collector": "SDK toplayıcı",
        "llm": "Yapay zekâ (LLM)",
        "notify": "Bildirimler (Slack)",
        "step_radar": "pulse.yaml → radar.enabled: true (yeni anahtar gerekmez; App Store ve Google Play'in herkese açık sayfaları).",
        "step_play_pages": "pulse.yaml → radar.play.enabled: true.",
        "step_watch_empty": "Kendi oyunlarınızı ekleyin: `pulse radar watch add <paket adı ya da mağaza bağlantısı>`.",
        "step_news": "pulse.yaml → radar.news.enabled: true (RSS akışları; anahtar gerekmez).",
        "step_news_feeds": "Okunamayan akış: {feeds}. Bir sonraki yenilemede yeniden denenir; sürerse radar.news.feeds listesinden çıkarın.",
        "step_estimates": "pulse.yaml → radar.estimates.enabled: true.",
        "step_estimates_wait": "Bantlar için listede yeterli oyunun Play sayacı birikmeli (birkaç hafta); kendiliğinden dolar.",
        "step_adintel": "pulse.yaml → radar.adintel.enabled: true (BigQuery'deki herkese açık veri kümesi; yeni anahtar gerekmez, haftada ~27 GB, ücretsiz kotanın içinde).",
        "step_dossiers": "Bir oyun için: Pazar radarı → oyun → `pulse radar dossier <Apple kimliği | Play paketi>` (LLM kullanır, günlük yenilemede çalışmaz).",
        "step_llm": "pulse.yaml → llm.provider: gemini | anthropic | openai (haber etiketleri, günün özeti ve içgörü metinleri için).",
        "step_notify": "Ayarlar → Bildirimler: Slack webhook bağlantısını yapıştırın.",
        "collector_local": "yerel ({url})",
        "collector_cloud": "bulut (BigQuery)",
        "collector_down": "ulaşılamıyor",
        "feeds_ok": "{n} akış okundu",
        "watching": "{n} oyun izleniyor",
        "dossier_n": "{n} dosya",
        "advertisers": "{n} reklamveren",
        "provider": "sağlayıcı: {p}",
    },
    "en": {
        "title": "Connections",
        "open_app": "App page →",
        "sub": "Every data source of every app and of the studio: connected or not, when data last arrived, rows in the last 7 days, the next refresh, and one step for whatever is missing.",
        "refresh": "Refresh",
        "last_run": "Last refresh",
        "last_ok": "Last success",
        "next_run": "Next refresh",
        "never": "never",
        "none": "none",
        "running": "running now",
        "took": "took {s}",
        "sched_off": "The daily refresh is not set up: data is only refreshed by hand.",
        "sched_off_step": "Settings → Daily refresh → Set up",
        "sched_other": "The daily refresh is set up for another config ({path}); this config is not refreshed automatically.",
        "sched_other_step": "To refresh this config instead: Settings → Daily refresh → Set up (replaces the other one).",
        "sched_at": "every day at {at}",
        "sched_wake": "if the computer sleeps then, it runs at wake",
        "stale": "The last successful refresh was {age}; the data is not current.",
        "stale_step": "Data health → Refresh now; if it repeats, look at the failing source below.",
        "failed": "The last refresh failed: {error}",
        "bg_failed": "The scheduled refresh at {when} exited with code {rc} before it could record a run: {line}",
        "bg_locked": "The scheduled refresh at {when} found another refresh running; it tries again at the next scheduled time.",
        "bg_log": "Details in the log file: {path}",
        "all_ok": "Data is flowing: the last refresh succeeded and every connected source is current.",
        "apps": "Apps",
        "studio": "Studio-wide",
        "source": "Source",
        "state": "Status",
        "fetched": "Last fetch",
        "through": "Data through",
        "rows7": "Rows (7 days)",
        "next": "Next",
        "step": "What to do",
        "s_ok": "flowing",
        "s_stale": "stale",
        "s_nodata": "no data",
        "s_error": "error",
        "s_off": "not connected",
        "s_disabled": "off",
        "s_ondemand": "on demand",
        "s_watch_wait": "awaiting first read",
        "days": "{n} d",
        "today": "today",
        "pkg": "<package name>",
        "weekly_next": "{date} (weekly)",
        "on_demand": "by hand",
        "with_run": "with the refresh",
        "step_env": "Add the environment variable {names} (pulse.settings.env or the config's env_file), then Refresh now.",
        "step_adc": "Run `gcloud auth application-default login` once on this computer (with the Google account used for GA4).",
        "step_perm": "The Google account needs BigQuery read access in this project (BigQuery Data Viewer + Job User); then Refresh now.",
        "step_error": "Fix the error above, then Data health → Refresh now.",
        "step_enable": "The source has `enabled: false` in the config; delete that line to turn it on.",
        "step_sdk_cloud": "Store builds send their events to the cloud collector: set it up with deploy/cloudrun/bootstrap.sh, then enable collector.url, read_key and bigquery in pulse.yaml.",
        "step_sdk_bundles": "No events from these packages reached the collector ({bundles}): check that the SDK is on in the build and the bundle_ids list.",
        "step_ga4": "Firebase → Project settings → Integrations → BigQuery: check that the daily Google Analytics export is on.",
        "step_crash": "Crashlytics writes rows only on days with a crash; with few users a gap of a few days is normal. If it lasts, check Firebase → Crashlytics → BigQuery.",
        "step_stale": "No new day for {lag} days and no error in the last refresh: the source itself writes no new data; check its export.",
        "step_nodata": "The last refresh got no rows from this source; check its path / id.",
        "step_play": "Settings → {app} → Connect Google Play (reports bucket + package name; no new key).",
        "step_asc": "Settings → {app} → Connect the App Store (an App Store Connect API key with Sales access).",
        "step_ua": "For ad spend, add the network's source under apps.{app}.sources in pulse.yaml: {opts}.",
        "step_watch": "The public store page (rating, version, rank) is tracked without keys: `pulse radar watch add {target}`.",
        "step_watch_radar": "Turn the market radar on first: pulse.yaml → radar.enabled: true (no new key).",
        "step_watch_wait": "On the watchlist; first read at the next refresh.",
        "row_play_reports": "Store reports · Google Play",
        "row_asc_reports": "Store reports · App Store",
        "row_public": "Public store page (watchlist)",
        "row_ua": "Ad networks (UA)",
        "radar": "Market radar (App Store charts)",
        "play_pages": "Google Play pages",
        "watch": "Watchlist",
        "news": "Industry news",
        "estimates": "Estimates (download / revenue bands)",
        "adintel": "Ad intel (weekly, Google Ads Transparency)",
        "dossiers": "Game dossiers",
        "collector": "SDK collector",
        "llm": "AI (LLM)",
        "notify": "Notifications (Slack)",
        "step_radar": "pulse.yaml → radar.enabled: true (no new key; public App Store and Google Play pages).",
        "step_play_pages": "pulse.yaml → radar.play.enabled: true.",
        "step_watch_empty": "Add your own games: `pulse radar watch add <package name or store URL>`.",
        "step_news": "pulse.yaml → radar.news.enabled: true (RSS feeds; no key).",
        "step_news_feeds": "Feeds not read: {feeds}. Retried at the next refresh; if it lasts, drop them from radar.news.feeds.",
        "step_estimates": "pulse.yaml → radar.estimates.enabled: true.",
        "step_estimates_wait": "Bands need enough charted games with Play counters (a few weeks); this fills by itself.",
        "step_adintel": "pulse.yaml → radar.adintel.enabled: true (a public BigQuery dataset; no new key, ~27 GB a week, inside the free tier).",
        "step_dossiers": "For one game: `pulse radar dossier <Apple id | Play package>` (uses the LLM; not part of the daily refresh).",
        "step_llm": "pulse.yaml → llm.provider: gemini | anthropic | openai (news tags, the daily summary and insight texts).",
        "step_notify": "Settings → Notifications: paste a Slack webhook URL.",
        "collector_local": "local ({url})",
        "collector_cloud": "cloud (BigQuery)",
        "collector_down": "not reachable",
        "feeds_ok": "{n} feed(s) read",
        "watching": "{n} game(s) watched",
        "dossier_n": "{n} dossier(s)",
        "advertisers": "{n} advertiser(s)",
        "provider": "provider: {p}",
    },
}


# -- small helpers ---------------------------------------------------------------------------


def duration(seconds: Any, lang: str) -> str:
    """329.35 -> '5 dk 29 sn' / '5 min 29 s'."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    m, s = divmod(s, 60)
    if lang == "tr":
        return f"{m} dk {s} sn" if m else f"{s} sn"
    return f"{m} min {s} s" if m else f"{s} s"


def local_time(value: Any, lang: str) -> str:
    """An ISO time or datetime as this computer's local 'YYYY-MM-DD HH:MM'."""
    t = value if isinstance(value, dt.datetime) else runstate.parse_iso(value)
    if t is None:
        return "—"
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone().strftime("%Y-%m-%d %H:%M")


def ago(value: Any, lang: str) -> str:
    """'3 sa önce' for an ISO string or a (naive = UTC) datetime; '—' for nothing."""
    from .ui import age

    if value is None or value == "":
        return "—"
    if isinstance(value, dt.datetime):
        value = (value if value.tzinfo else value.replace(tzinfo=dt.UTC)).isoformat()
    elif isinstance(value, dt.date):
        value = dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC).isoformat()
    return age(str(value), lang)


def source_label(key: Any, lang: str) -> str:
    """`play_reviews@ios` -> 'Google Play yorumları (ios)'."""
    kind, _, suffix = str(key or "").partition("@")
    label = KINDS.get(kind, (kind, kind))[0 if lang == "tr" else 1]
    return f"{label} ({suffix})" if suffix else label


def num(value: Any, lang: str) -> str:
    """12345 -> '12.345' (tr) / '12,345' (en)."""
    if value is None:
        return "—"
    try:
        s = f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)
    return s.replace(",", ".") if lang == "tr" else s


LIGHT = {
    "tr": {"ok": "Son yenileme {age}", "running": "Yenileniyor…", "failed": "Son yenileme başarısız · {age}",
           "stale": "Son başarılı yenileme {age}", "never": "Henüz yenileme yok"},
    "en": {"ok": "Last refresh {age}", "running": "Refreshing…", "failed": "Last refresh failed · {age}",
           "stale": "Last successful refresh {age}", "never": "No refresh yet"},
}  # fmt: skip


def light_status(hb: dict | None, lang: str) -> dict[str, str]:
    """The one-line freshness every page shows (heartbeat only: no scheduler call)."""
    hb = hb or {}
    L = LIGHT[lang]
    last_ok = runstate.parse_iso(hb.get("last_success"))
    status = hb.get("status")
    if status == "running":
        return {"verdict": "running", "text": L["running"]}
    if status in ("failed", "interrupted"):
        return {
            "verdict": "failed",
            "text": L["failed"].format(age=ago(hb.get("last_end") or hb.get("last_start"), lang)),
        }
    if last_ok is None:
        return {"verdict": "stale", "text": L["never"]}
    if dt.datetime.now(dt.UTC) - last_ok > dt.timedelta(hours=STALE_AFTER_H):
        return {
            "verdict": "stale",
            "text": L["stale"].format(age=ago(hb.get("last_success"), lang)),
        }
    return {"verdict": "ok", "text": L["ok"].format(age=ago(hb.get("last_success"), lang))}


def launchd_state(value: Any, lang: str) -> str:
    pair = LAUNCHD_STATE.get(str(value or "").strip())
    return pair[0 if lang == "tr" else 1] if pair else str(value or "")


def next_occurrence(at: str | None, now: dt.datetime | None = None) -> dt.datetime | None:
    """The next local HH:MM after now (today or tomorrow)."""
    try:
        hour, minute = schedule.parse_time(str(at or ""))
    except schedule.ScheduleError:
        return None
    now = now or dt.datetime.now().astimezone()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return nxt if nxt > now else nxt + dt.timedelta(days=1)


def _same_path(a: Any, b: Any) -> bool:
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return False


def schedule_info(cfg: Any, name: str = schedule.DEFAULT_NAME, *, probe: bool = True) -> dict:
    """The scheduler entry and its install record, judged for *this* config.

    probe=False reads only the install record (no launchctl / schtasks call): for the
    status line every page draws."""
    info: dict[str, Any] = {"registered": False}
    if probe:
        try:
            info = dict(schedule.backend().status(name))
        except schedule.ScheduleError as exc:
            info = {"registered": False, "note": str(exc)}
    try:
        rec = schedule.load_record(name) or {}
    except (OSError, schedule.ScheduleError):
        rec = {}
    registered = bool(info.get("registered")) if probe else bool(rec)
    mine = bool(rec.get("config")) and _same_path(rec["config"], cfg.path)
    if registered and not rec.get("config"):
        mine = True  # an entry installed before install records existed: its config is unknown
    info.update(
        installed=registered,
        for_this_config=registered and mine,
        other_config=rec.get("config") if registered and rec.get("config") and not mine else None,
        at=rec.get("at") if mine else None,
        log_file=rec.get("log_file") if mine else None,
    )
    info["next_run"] = next_occurrence(info["at"]) if info["for_this_config"] else None
    return info


_BLOCK = re.compile(r"^=== (\S+) pulse (.*?) \(pid \d+\)\s*$")
_EXIT = re.compile(r"^=== exit (-?\d+)\s*$")


def scheduled_failure(log_file: Any, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last `pulse-bg run` block of the schedule's log, when it exited non-zero and left
    no run record (it failed before the run could start: a config error, a crash)."""
    if not log_file:
        return None
    path = Path(str(log_file))
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 65536))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = None
    for i in range(len(lines) - 1, -1, -1):
        m = _BLOCK.match(lines[i])
        if m and m.group(2).split()[:1] == ["run"]:
            start = i
            break
    if start is None:
        return None
    rc = None
    body: list[str] = []
    for line in lines[start + 1 :]:
        m = _EXIT.match(line)
        if m:
            rc = int(m.group(1))
            break
        body.append(line)
    if not rc:  # 0, or still running (no exit line yet)
        return None
    when = runstate.parse_iso(_BLOCK.match(lines[start]).group(1))  # type: ignore[union-attr]
    if when is not None:
        for h in history:
            t = runstate.parse_iso(h.get("start"))
            if t is not None and t >= when - dt.timedelta(seconds=5):
                return None  # the run started and recorded its own result (shown as such)
    from ..llm.base import redact

    last = next((ln.strip() for ln in reversed(body) if ln.strip()), "")
    return {
        "when": when,
        "rc": rc,
        "locked": rc == runstate.EXIT_LOCKED,
        "line": redact(last)[:300],
        "log_file": str(path),
    }


def run_state(cfg: Any, hb: dict | None, history: list[dict], sched: dict) -> dict[str, Any]:
    """The refresh as one verdict: ok, running, failed or stale, with the reason."""
    hb = hb or {}
    now = dt.datetime.now(dt.UTC)
    last_ok = runstate.parse_iso(hb.get("last_success"))
    stale = last_ok is None or (now - last_ok) > dt.timedelta(hours=STALE_AFTER_H)
    bg = scheduled_failure(sched.get("log_file"), history) if sched.get("for_this_config") else None
    status = hb.get("status")
    if status == "running":
        verdict = "running"
    elif status in ("failed", "interrupted") or (bg and not bg["locked"]):
        verdict = "failed"
    elif stale:
        verdict = "stale"
    else:
        verdict = "ok"
    return {
        "verdict": verdict,
        "status": status,
        "last_start": hb.get("last_start"),
        "last_success": hb.get("last_success"),
        "duration_s": hb.get("duration_s"),
        "error": hb.get("error") if status in ("failed", "interrupted") else None,
        "source_errors": (hb.get("summary") or {}).get("source_errors") or {},
        "stale": stale,
        "bg": bg,
        "sched": sched,
    }


def _rows(con: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    if con is None:
        return []
    try:
        cur = con.execute(sql, params or [])
    except Exception:  # a missing table (no radar, no SDK): nothing to show
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _one(con: Any, sql: str, params: list[Any] | None = None) -> Any:
    r = _rows(con, sql, params)
    return next(iter(r[0].values())) if r else None


_DATE_COLS = ("date", "cohort_date", "snapshot_date", "as_of_date")


def _rows_7d(
    con: Any, cols: dict[str, set[str]], app_id: str, source: str, tables: list[str], since: dt.date
) -> int | None:
    """Rows dated on or after `since` in the tables this source loads (`_source` narrows a
    table several sources share)."""
    total, seen = 0, False
    for t in tables:
        c = cols.get(t) or set()
        dcol = next((d for d in _DATE_COLS if d in c), None)
        if dcol is None or "app_id" not in c:
            continue
        where = f'app_id = ? AND "{dcol}" >= ?'
        params: list[Any] = [app_id, since]
        if "_source" in c:
            where += " AND _source = ?"
            params.append(source)
        n = _one(con, f'SELECT COUNT(*) FROM "{t}" WHERE {where}', params)
        if n is not None:
            total += int(n)
            seen = True
    return total if seen else None


def missing_env(kind: str, src: dict[str, Any]) -> list[str]:
    """Environment variables this source needs and this process does not have."""
    if src.get("replay") or src.get("response_file"):
        return []

    def need(*names: str) -> list[str]:
        return [n for n in names if not os.environ.get(n, "").strip()]

    if kind == "applovin_ads":
        return need(str(src.get("token_env", "PULSE_APPLOVIN_REPORT_KEY")))
    if kind == "unity_ads":
        return need(
            str(src.get("key_id_env", "PULSE_UNITY_ADS_KEY_ID")),
            str(src.get("secret_env", "PULSE_UNITY_ADS_SECRET_KEY")),
        )
    if kind == "meta_ads":
        return need(str(src.get("token_env", "PULSE_META_ACCESS_TOKEN")))
    if kind == "tiktok_ads":
        return need(str(src.get("token_env", "PULSE_TIKTOK_ACCESS_TOKEN")))
    if kind == "app_store_reviews" or (kind == "app_store" and src.get("vendor_number")):
        if os.environ.get("PULSE_ASC_TOKEN"):
            return []
        out = need("PULSE_ASC_KEY_ID", "PULSE_ASC_ISSUER_ID")
        if not (
            os.environ.get("PULSE_ASC_PRIVATE_KEY") or os.environ.get("PULSE_ASC_PRIVATE_KEY_PATH")
        ):
            out.append("PULSE_ASC_PRIVATE_KEY_PATH")
        return out
    return []


def _error_step(t: dict[str, str], err: str) -> str:
    low = err.lower()
    if "defaultcredentialserror" in low or "application default credentials" in low:
        return t["step_adc"]
    if "403" in low or "permission" in low or "access denied" in low:
        return t["step_perm"]
    m = re.findall(r"\bPULSE_[A-Z0-9_]+\b", err)
    if m:
        return t["step_env"].format(names=", ".join(dict.fromkeys(m)))
    return t["step_error"]


def app_identifiers(app: Any) -> set[str]:
    """Store ids this app is known by in the config (bundle ids, packages, Apple ids)."""
    out: set[str] = set()
    for src in (app.sources or {}).values():
        src = src or {}
        for k in ("package", "apple_id", "sku"):
            if src.get(k):
                out.add(str(src[k]))
        for k in ("bundle_ids", "app_info_ids", "package_names"):
            out.update(str(x) for x in src.get(k) or [])
    return {x for x in out if x}


def public_store(con: Any, app: Any) -> dict[str, Any]:
    """The app's own public store pages as the radar sees them (charts, Lookup, Play
    details), newest day only: one row per store listing with the rating summed over the
    storefronts read that day (count-weighted average), the version and its date, the best
    chart rank of that day, and whether the listing is on the watch list."""
    ids = sorted(app_identifiers(app))
    if not ids:
        return {"rows": [], "watched": [], "target": ""}
    watched = _rows(
        con,
        "SELECT store, app_id FROM raw_mr_watch WHERE removed_at IS NULL "
        "AND (list_contains(?::VARCHAR[], app_id) OR app_id IN (SELECT DISTINCT app_id FROM "
        "raw_mr_app WHERE list_contains(?::VARCHAR[], bundle_id)))",
        [ids, ids],
    )
    found = _rows(
        con,
        """
        WITH latest AS (  -- one row per storefront: its newest read
          SELECT * FROM raw_mr_app
          WHERE list_contains(?::VARCHAR[], app_id) OR list_contains(?::VARCHAR[], bundle_id)
          QUALIFY ROW_NUMBER() OVER (PARTITION BY store, app_id, country
                                     ORDER BY snapshot_date DESC, fetched_at DESC NULLS LAST) = 1
        ), mine AS (  -- the newest day of each listing
          SELECT * FROM latest
          QUALIFY snapshot_date = MAX(snapshot_date) OVER (PARTITION BY store, app_id)
        )
        SELECT store, app_id, MAX(snapshot_date) AS snapshot_date, MAX(fetched_at) AS fetched_at,
          any_value(name) AS name, MAX(version) AS version,
          MAX(current_version_date) AS version_date, any_value(store_url) AS store_url,
          any_value(installs_bucket) AS installs_bucket,
          SUM(rating_count) AS rating_count,
          SUM(rating_avg * rating_count) / NULLIF(SUM(rating_count), 0) AS rating_avg,
          COUNT(DISTINCT country) AS countries
        FROM mine GROUP BY 1, 2 ORDER BY 1, 2
        """,
        [ids, ids],
    )
    out = []
    for r in found:
        rank = _rows(
            con,
            "SELECT country, list_key, MIN(rank) AS rank FROM stg_mr_rank_input "
            "WHERE app_id = ? AND snapshot_date = ? GROUP BY 1, 2 ORDER BY 3 LIMIT 1",
            [r["app_id"], r["snapshot_date"]],
        )
        url = str(r.get("store_url") or "")
        out.append(
            {
                **r,
                "store_name": STORE_NAMES.get(r["store"], r["store"]),
                "store_url": url if url.startswith("https://") else None,
                "best_rank": rank[0] if rank else None,
                "watched": any(w["app_id"] == r["app_id"] for w in watched),
            }
        )
    target = next((i for i in ids if "." in i and not i.isdigit()), None) or ids[0]
    return {"rows": out, "watched": watched, "target": target}


# -- the view --------------------------------------------------------------------------------


def build(
    cfg: Any,
    con: Any,
    lang: str,
    *,
    hb: dict | None,
    history: list[dict],
    sched: dict,
    live: dict | None = None,
) -> dict[str, Any]:
    t = T[lang]
    today = cfg.as_of or dt.date.today()
    since = today - dt.timedelta(days=7)
    run = run_state(cfg, hb, history, sched)
    nxt = sched.get("next_run")
    next_text = nxt.strftime("%Y-%m-%d %H:%M") if nxt else None
    cols: dict[str, set[str]] = {}
    for r in _rows(con, "SELECT table_name, column_name FROM information_schema.columns"):
        cols.setdefault(r["table_name"], set()).add(r["column_name"])
    loads = {
        (r["app_id"], r["source"]): r
        for r in _rows(
            con,
            "SELECT app_id, source, MAX(loaded_at) AS last, list(DISTINCT table_name) AS tables "
            "FROM meta_loads GROUP BY 1, 2",
        )
    }
    fresh = {
        (r["app_id"], r["source"]): r for r in _rows(con, "SELECT * FROM fct_source_freshness")
    }
    errors = run["source_errors"]
    try:
        from ..radar.settings import from_config as radar_settings

        rs = radar_settings(cfg)
    except Exception:  # a broken radar block is reported by the radar row below
        rs = None
    radar_on = bool(rs and rs.enabled)

    def row(label: str, state: str, **kw: Any) -> dict[str, Any]:
        return {"label": label, "state": state, "code": kw.pop("code", None), "fetched": None,
                "through": None, "lag": None, "rows7": None, "next": next_text, "error": None,
                "step": None, "href": None, "detail": None, **kw}  # fmt: skip

    apps_out = []
    for app in cfg.apps:
        name = app.name or app.id
        out = []
        kinds = set()
        for skey, src in (app.sources or {}).items():
            src = src or {}
            kind = skey.split("@", 1)[0]
            kinds.add(kind)
            label = KINDS.get(kind, (kind, kind))[0 if lang == "tr" else 1]
            if src.get("enabled", True) is False:
                out.append(row(label, "disabled", code=skey, step=t["step_enable"], next=None))
                continue
            f = fresh.get((app.id, skey)) or {}
            ld = loads.get((app.id, skey)) or {}
            through = f.get("data_through")
            lag = (today - through).days if through else None
            r = row(
                label, "ok", code=skey,
                fetched=ld.get("last") or f.get("last_loaded_at"),
                through=through, lag=lag,
                rows7=_rows_7d(con, cols, app.id, skey, list(ld.get("tables") or []), since),
            )  # fmt: skip
            err = errors.get(f"{app.id}.{skey}")
            env = missing_env(kind, src)
            if err:
                r.update(state="error", error=err, step=_error_step(t, err))
            elif env:
                r.update(state="error", step=t["step_env"].format(names=", ".join(env)))
            elif through is None:
                r["state"] = "nodata"
                if kind == "pulse_sdk":
                    bq = src.get("bigquery") or (cfg.raw.get("collector") or {}).get("bigquery")
                    r["step"] = (t["step_sdk_bundles"].format(bundles=", ".join(
                        str(b) for b in src.get("bundle_ids") or [])) if bq
                        else t["step_sdk_cloud"])  # fmt: skip
                elif kind == "ga4_bigquery":
                    r["step"] = t["step_ga4"]
                else:
                    r["step"] = t["step_nodata"]
            elif lag is not None and lag > LAG_OK_DAYS:
                r["state"] = "stale"
                r["step"] = {
                    "crashlytics_bigquery": t["step_crash"],
                    "ga4_bigquery": t["step_ga4"],
                }.get(kind, t["step_stale"].format(lag=lag))
            out.append(r)
        # what is not connected yet, with the one step each
        if "play_console" not in kinds:
            out.append(row(t["row_play_reports"], "off", next=None, href=f"/settings#app-{app.id}",
                           step=t["step_play"].format(app=name)))  # fmt: skip
        if "app_store" not in kinds:
            out.append(row(t["row_asc_reports"], "off", next=None, href=f"/settings#app-{app.id}",
                           step=t["step_asc"].format(app=name)))  # fmt: skip
        pub = public_store(con, app)
        if pub["rows"]:
            last = max(
                (p.get("fetched_at") for p in pub["rows"] if p.get("fetched_at")), default=None
            )
            n7 = _one(
                con,
                "SELECT COUNT(*) FROM raw_mr_app WHERE snapshot_date >= ? AND "
                "list_contains(?::VARCHAR[], app_id)",
                [since, [p["app_id"] for p in pub["rows"]]],
            )
            through = max(p["snapshot_date"] for p in pub["rows"])
            lag = (today - through).days
            out.append(row(t["row_public"], "ok" if lag <= LAG_OK_DAYS else "stale",
                           fetched=last, through=through, lag=lag, rows7=n7,
                           next=next_text if radar_on else None,
                           step=None if radar_on else t["step_watch_radar"]))  # fmt: skip
        elif pub["watched"]:
            out.append(row(t["row_public"], "watch_wait", step=t["step_watch_wait"] if radar_on
                           else t["step_watch_radar"], next=next_text if radar_on else None))  # fmt: skip
        else:
            step = (t["step_watch"].format(target=pub["target"] or t["pkg"]) if radar_on
                    else t["step_watch_radar"])  # fmt: skip
            out.append(row(t["row_public"], "off", step=step, next=None))
        if not kinds & set(UA_NETWORKS) and "ua_csv" not in kinds:
            opts = ", ".join(f"{k} ({v})" for k, v in _UA_KEYS.items())
            out.append(row(t["row_ua"], "off", next=None,
                           step=t["step_ua"].format(app=app.id, opts=opts)))  # fmt: skip
        out.sort(key=lambda r: ORDER.get(r["state"], 9))  # what needs a hand first
        apps_out.append({"id": app.id, "name": name, "rows": out,
                         "problems": sum(r["state"] in ("error", "stale", "nodata") for r in out)})  # fmt: skip

    studio = studio_rows(cfg, con, t, lang, rs, today, since, next_text, live)
    problems = [r for a in apps_out for r in a["rows"] if r["state"] in ("error", "stale")]
    problems += [r for r in studio if r["state"] in ("error", "stale")]
    return {"run": run, "apps": apps_out, "studio": studio, "next_text": next_text,
            "n_problems": len(problems), "t": t,
            "state_text": launchd_state(sched.get("state"), lang)}  # fmt: skip


def studio_rows(
    cfg: Any, con: Any, t: dict[str, str], lang: str, rs: Any, today: dt.date,
    since: dt.date, next_text: str | None, live: dict | None,
) -> list[dict[str, Any]]:  # fmt: skip
    out: list[dict[str, Any]] = []

    def row(label: str, state: str, **kw: Any) -> dict[str, Any]:
        return {"label": label, "state": state, "code": kw.pop("code", None), "fetched": None,
                "through": None, "lag": None, "rows7": None, "next": next_text, "error": None,
                "step": None, "href": None, "detail": None, **kw}  # fmt: skip

    radar_on = bool(rs and rs.enabled)
    if not radar_on:
        out.append(row(t["radar"], "off", code="radar", step=t["step_radar"], next=None))
    else:
        last = _rows(con, "SELECT * FROM raw_mr_run ORDER BY started_at DESC LIMIT 1")
        ok = _one(con, "SELECT MAX(finished_at) FROM raw_mr_run WHERE status = 'success'")
        through = _one(con, "SELECT MAX(snapshot_date) FROM raw_mr_chart")
        n7 = _one(con, "SELECT COUNT(*) FROM raw_mr_chart WHERE snapshot_date >= ?", [since])
        r = row(t["radar"], "ok", code="radar", fetched=ok, through=through, rows7=n7,
                lag=(today - through).days if through else None)  # fmt: skip
        if last and last[0].get("status") != "success":
            import json

            try:
                errs = json.loads(last[0].get("summary") or "{}").get("errors") or []
            except ValueError:
                errs = []
            r.update(state="error", error="; ".join(str(e) for e in errs[:3]) or None,
                     step=t["step_error"])  # fmt: skip
        elif through is None:
            r["state"] = "nodata"
        elif r["lag"] is not None and r["lag"] > LAG_OK_DAYS:
            r["state"] = "stale"
        out.append(r)
        # Google Play pages
        if not rs.play_enabled:
            out.append(row(t["play_pages"], "disabled", step=t["step_play_pages"], next=None))
        else:
            p_last = _one(con, "SELECT MAX(fetched_at) FROM raw_mr_app WHERE store = 'play'")
            p_through = _one(con, "SELECT MAX(snapshot_date) FROM raw_mr_app WHERE store = 'play'")
            p7 = _one(con, "SELECT COUNT(*) FROM raw_mr_app WHERE store = 'play' AND snapshot_date >= ?",
                      [since])  # fmt: skip
            out.append(row(t["play_pages"], "ok" if p_through else "nodata", fetched=p_last,
                           through=p_through, rows7=p7,
                           lag=(today - p_through).days if p_through else None))  # fmt: skip
        # watch list
        w = _rows(con, "SELECT store, app_id FROM raw_mr_watch WHERE removed_at IS NULL")
        if not w:
            out.append(row(t["watch"], "off", step=t["step_watch_empty"]))
        else:
            ids = [x["app_id"] for x in w]
            w_last = _one(
                con,
                "SELECT MAX(fetched_at) FROM raw_mr_app WHERE list_contains(?::VARCHAR[], app_id)",
                [ids],
            )
            w7 = _one(con, "SELECT COUNT(*) FROM raw_mr_app WHERE snapshot_date >= ? AND list_contains(?::VARCHAR[], app_id)",
                      [since, ids])  # fmt: skip
            out.append(row(t["watch"], "ok" if w_last else "watch_wait", fetched=w_last, rows7=w7,
                           detail=t["watching"].format(n=len(w)),
                           step=None if w_last else t["step_watch_wait"]))  # fmt: skip
        # news
        if not rs.news_enabled:
            out.append(row(t["news"], "disabled", step=t["step_news"], next=None))
        else:
            feeds = _rows(
                con,
                "SELECT feed_id, status, error, checked_at FROM raw_mr_news_feed "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY checked_at DESC) = 1",
            )
            bad = [f["feed_id"] for f in feeds if f.get("error")]
            n_last = _one(con, "SELECT MAX(checked_at) FROM raw_mr_news_feed WHERE error IS NULL")
            n7 = _one(con, "SELECT COUNT(*) FROM raw_mr_news WHERE first_seen >= ?", [since])
            state = "error" if bad and len(bad) == len(feeds) else ("stale" if bad else "ok")
            if not feeds:
                state = "nodata"
            out.append(row(t["news"], state, fetched=n_last, rows7=n7,
                           detail=t["feeds_ok"].format(n=len(feeds) - len(bad)) if feeds else None,
                           step=t["step_news_feeds"].format(feeds=", ".join(bad)) if bad else None))  # fmt: skip
        # estimates
        from ..radar.estimate.settings import from_config as est_settings

        try:
            es = est_settings(cfg)
        except Exception:
            es = None
        if es is None or not es.enabled:
            out.append(row(t["estimates"], "disabled", step=t["step_estimates"], next=None))
        else:
            e_through = _one(con, "SELECT MAX(snapshot_date) FROM raw_mr_est_game")
            e7 = _one(con, "SELECT COUNT(*) FROM raw_mr_est_game WHERE snapshot_date >= ?", [since])
            out.append(row(t["estimates"], "ok" if e7 else "nodata", through=e_through, rows7=e7,
                           step=None if e7 else t["step_estimates_wait"]))  # fmt: skip
        # weekly ad intel
        from ..radar.adintel.google_atc import settings as adintel_settings

        try:
            ai = adintel_settings(cfg)
        except Exception:
            ai = None
        if ai is None or not ai.enabled:
            out.append(row(t["adintel"], "disabled", step=t["step_adintel"], next=None))
        else:
            last_ok = _rows(
                con,
                "SELECT run_at, extract_date, advertisers FROM raw_mr_adintel_run "
                "WHERE status = 'ok' ORDER BY run_at DESC LIMIT 1",
            )
            last_any = _rows(
                con, "SELECT status, message FROM raw_mr_adintel_run ORDER BY run_at DESC LIMIT 1"
            )
            if last_ok:
                due = last_ok[0]["extract_date"] + dt.timedelta(days=ai.every_days)
                r = row(t["adintel"], "ok", fetched=last_ok[0]["run_at"],
                        through=last_ok[0]["extract_date"],
                        detail=t["advertisers"].format(n=last_ok[0]["advertisers"] or 0),
                        next=t["weekly_next"].format(date=max(due, today)))  # fmt: skip
            else:
                r = row(t["adintel"], "nodata", next=next_text)
            if last_any and last_any[0]["status"] != "ok":
                r.update(state="error", error=last_any[0].get("message"), step=t["step_error"])
            out.append(r)
        # dossiers (on demand)
        n_dos = 0
        try:
            from ..radar.dossier.page import index as dossier_index

            n_dos = len(dossier_index(rs.out_dir))
        except Exception:
            n_dos = 0
        out.append(row(t["dossiers"], "ondemand", next=t["on_demand"], step=t["step_dossiers"],
                       detail=t["dossier_n"].format(n=n_dos)))  # fmt: skip
    # SDK collector
    col = cfg.raw.get("collector") or {}
    if col:
        cloud = bool(col.get("bigquery"))
        mode = (
            t["collector_cloud"]
            if cloud
            else t["collector_local"].format(url=col.get("url") or "—")
        )
        err = (live or {}).get("error")
        n7 = _one(con, "SELECT COUNT(*) FROM raw_pulse_events WHERE date >= ?", [since])
        last = _one(con, "SELECT MAX(loaded_at) FROM meta_loads WHERE source LIKE 'pulse_sdk%'")
        state = "ok" if n7 else "nodata"
        detail = mode + (f" · {t['collector_down']}" if err and err != "off" else "")
        out.append(row(t["collector"], state, fetched=last, rows7=n7, detail=detail,
                       step=None if cloud and n7 else t["step_sdk_cloud"] if not cloud else None))  # fmt: skip
    # LLM and notifications
    llm = cfg.raw.get("llm") or {}
    provider = str(llm.get("provider") or os.environ.get("PULSE_LLM_PROVIDER") or "none").lower()
    if provider in ("none", ""):
        out.append(row(t["llm"], "off", step=t["step_llm"], next=None))
    else:
        out.append(
            row(t["llm"], "info", detail=t["provider"].format(p=provider), next=t["with_run"])
        )
    if os.environ.get("PULSE_SLACK_WEBHOOK_URL"):
        out.append(row(t["notify"], "info", detail="Slack", next=t["with_run"]))
    else:
        out.append(
            row(t["notify"], "off", step=t["step_notify"], href="/settings#notify", next=None)
        )
    return out


# -- English messages written by the pipeline and the checks, worded for the Turkish UI ------

_CHECK_TR = [
    (re.compile(r"^(\d+) failing row\(s\)$"), "{0} satır kuralı bozuyor"),
    (re.compile(r"^(\d+) row\(s\) with NULL in (.+)$"), "{0} satırda {1} boş (NULL)"),
    (re.compile(r"^(\d+) duplicated key\(s\) on \((.+)\)$"), "({1}) üzerinde {0} yinelenen anahtar"),
    (re.compile(r"^(\d+) unexpected value\(s\) in (\S+): (.*)$"), "{1} alanında {0} beklenmeyen değer: {2}"),
    (re.compile(r"^(\d+) row\(s\) with (\S+) outside \[(.+), (.+)\]$"), "{0} satırda {1} [{2}, {3}] aralığının dışında"),
    (re.compile(r"^(\d+) (\S+) value\(s\) missing from (.+)$"), "{0} {1} değeri {2} içinde yok"),
    (re.compile(r"^(\d+) source\(s\) stale: (.*)$"), "{0} kaynak bayat: {1}"),
    (re.compile(r"^(\d+) group\(s\) dropped more than (\d+)%: (.*)$"), "{0} grup %{1}'den fazla düştü: {2}"),
]  # fmt: skip
_PARTS_TR = [
    (re.compile(r"data through (\S+) \((\d+) d, limit (\d+)\)"), r"veri sonu \1 (\2 gün, sınır \3)"),
    (re.compile(r"last loaded (\S+) \((\d+) d, limit (\d+)\)"), r"son yükleme \1 (\2 gün, sınır \3)"),
    (re.compile(r" vs median "), " / medyan "),
]  # fmt: skip
_RUN_TR = [
    (re.compile(r"^(\d+) source\(s\) failed to load: (.*)$"), "{0} kaynak yüklenemedi: {1}"),
    (re.compile(r"^(\d+) data-quality check\(s\) failed: (.*)$"), "{0} veri kalitesi denetimi başarısız: {1}"),
]  # fmt: skip


def check_message(msg: Any, lang: str) -> str:
    """A data-quality check's message (checks.py writes English) in the page's language."""
    text = "" if msg is None else str(msg)
    if lang != "tr":
        return text
    if text == "ok":
        return "tamam"
    for rx, fmt_ in _CHECK_TR:
        m = rx.match(text)
        if m:
            out = fmt_.format(*m.groups())
            for prx, rep in _PARTS_TR:
                out = prx.sub(rep, out)
            return out
    return text


def run_error(msg: Any, lang: str) -> str:
    """A refresh's error line (pipeline.run_all) in the page's language; other text as is."""
    text = "" if msg is None else str(msg)
    if lang != "tr" or not text:
        return text
    out = []
    for part in text.split("; "):
        for rx, fmt_ in _RUN_TR:
            m = rx.match(part)
            if m:
                part = fmt_.format(*m.groups())
                break
        out.append(part)
    return "; ".join(out)
