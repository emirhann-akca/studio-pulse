"""Reklam Merkezi (/reklam): one screen per game for its ad campaigns, and the way to make and publish new ones.

Left of the page, as everywhere, the sidebar; at the top a game switcher with every configured app (the last one
chosen is remembered per viewer in a cookie), the period (last day, 7 / 30 days, custom; always against the period
of the same length before), the data's freshness per ad network with the one step to connect a missing network, and
"Şimdi yenile" (re-reads the game's ad networks through today) with an optional 15-minute auto-refresh.

Tabs:

* Genel bakış: KPIs (spend, impressions, video views, installs, CPI, D0 / D7 ROAS, payback), the grounded AI summary
  (serve/reklam_ai.py), charts with change markers (serve/reklam_charts.py; one axis each, spend and installs side by
  side), the network -> campaign -> creative table with a detail drawer per creative.
* Kreatif üret: the "Yeni kreatif" wizard (creative-forge `forge make`), its progress, and the review list
  (approve / reject bound to the file's SHA-256 by creative-forge itself).
* Yayınla: approved exports -> `forge publish` -> a starter campaign config -> `forge campaign create` (every object
  PAUSED; AppLovin / Unity / Mintegral get a manual upload pack to download), then the separate, typed activation
  under the caps, and pause.
* Oynanabilir: playable sources and their per-network builds, previews in a sandboxed frame, packing.
* Rakip kreatifleri: a link to the market radar's competitor creative library (/radar/kreatifler).

Everything creative-forge does runs through serve/forge_bridge.py (the `forge` CLI with JSON output); without
creative-forge the page still shows every number, read-only. Nothing is game-specific: a new app in the config (or
registered by the SDK collector) appears in the switcher with the same page.

Security as on every page (serve/security.py): GET never changes anything; every POST carries the per-process token
(htmx sends it), passes the Guard's Origin / Fetch-Metadata checks and is re-checked here; input is validated and
never echoed into HTML unescaped (Jinja autoescape); files are served only from the paths the last creative-forge
snapshot listed, by an opaque token; a playable preview runs in a sandboxed frame with its own CSP.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import secrets
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from . import reklam_charts as C
from . import reklam_data as D
from . import reklam_events as E
from .forge_bridge import (
    API_NETWORKS,
    MANUAL_NETWORKS,
    NAME_RE,
    PLAYABLE_NETWORKS,
    Forge,
    ForgeError,
)
from .ui import request_lang

COOKIE = "reklam-app"
RANGES = ("today", "7d", "30d", "custom")
TABS = ("genel", "uret", "yayinla", "oynanabilir")
MECHANICS = ("goods-sort", "merge", "match-3", "tile-match", "block-puzzle", "physics", "idle", "runner",
             "hidden-object", "word", "color-sort", "screw-pin", "jam", "other")  # fmt: skip
LANGS = ("tr", "en", "de")
COMPETITOR_URL = "/radar/kreatifler"
# The competitor library links here: /reklam?brief=…&hook=…&mechanic=…&orientation=…&genre=…&ref=<key>&lang=…
# (brief built from its tags, at most 400 characters). The wizard opens prefilled; nothing runs until "Üret".
PREFILL = ("brief", "hook", "mechanic", "orientation", "genre", "ref")
HOOKS = (
    "satisfying-clean",
    "fail-state",
    "near-miss",
    "challenge",
    "before-after",
    "reveal",
    "rescue",
    "asmr",
    "tutorial",
    "real-hand",
    "character-reaction",
)  # creative-forge's hook_type vocabulary
MECHANIC_ALIASES = {
    "physics-draw": "physics",
    "idle-tycoon": "idle",
    "hexa-sort": "color-sort",
}  # radar -> forge

T: dict[str, dict[str, str]] = {
    "tr": {
        "title": "Reklam Merkezi",
        "sub": "Kampanyalar, kreatifler ve yayın: bir oyunun bütün reklam durumu tek ekranda.",
        "game": "Oyun",
        "demo": "Örnek veri",
        "demo_long": "Bu ekrandaki sayılar `pulse demo`'nun kurgusal oyunlarından; gerçek reklam hesabı bağlanınca gerçek veri kendiliğinden akar.",
        "tab_genel": "Genel bakış",
        "tab_uret": "Kreatif üret",
        "tab_yayinla": "Yayınla",
        "tab_oynanabilir": "Oynanabilir",
        "tab_rakip": "Rakip kreatifleri",
        "r_today": "Son gün",
        "r_today_live": "Bugün",
        "r_7d": "7 gün",
        "r_30d": "30 gün",
        "r_custom": "Özel",
        "from": "Başlangıç",
        "to": "Bitiş",
        "apply": "Uygula",
        "period_line": "{a} – {b} · karşılaştırma: {pa} – {pb}",
        "refresh": "Şimdi yenile",
        "refresh_busy": "Yenileme sürüyor…",
        "refresh_started": "Reklam ağları yeniden okunuyor (bugün dahil); bitince sayfa yenilenir.",
        "refresh_locked": "Başka bir yenileme sürüyor; bitince tekrar deneyin.",
        "refresh_none": "Bu oyuna bağlı reklam ağı yok; önce bir ağ bağlayın.",
        "auto": "15 dk'da bir otomatik yenile",
        "fresh_through": "{d} gününe kadar",
        "fresh_loaded": "yüklendi {ago}",
        "fresh_replay": "kayıtlı yanıt (örnek)",
        "fresh_env": "eksik anahtar: {names}",
        "fresh_none": "henüz veri yok",
        "missing": "Bağlı değil",
        "connect": "Bağla",
        "connect_step": "pulse.yaml → apps.{app}.sources.{kind} ve {env}; adım adım: Bağlantılar.",
        "k_spend": "Harcama",
        "k_imps": "Gösterim",
        "k_views": "Video izlenme",
        "k_installs": "Kurulum",
        "k_cpi": "CPI",
        "k_d0": "D0 ROAS",
        "k_d7": "D7 ROAS",
        "k_payback": "Geri ödeme",
        "k_prev": "önceki",
        "k_vs": "önceki döneme göre",
        "k_views_na": "ağlar bildirmiyor",
        "k_views_note": "Meta 3 sn, TikTok 2 sn izlenme; diğer ağlar izlenme bildirmez.",
        "k_d7_note": "Günü tamamlanmış kohortlar ({n} gün); gelir, ağ kurulumları × uygulamanın kohort ARPU'su.",
        "k_d7_wait": "7. günü dolmuş kohort yok",
        "k_payback_day": "{d}. gün",
        "k_payback_none": "henüz yok",
        "k_payback_note": "Havuzlanmış ROAS eğrisinin %100'e ulaştığı ilk gün (son 30 gün, gözlenen {n} gün).",
        "k_installs_note": "Ağların kendi bildirdiği kurulumlar (MMP ataması değil).",
        "k_cpi_note": "Harcama / ağ kurulumu.",
        "ai": "Yapay zekâ özeti",
        "ai_note": "Sayıları kod hesaplar ve doğrular; model yalnız anlatır. “Anlamlı” = günlük değerlerde Welch testi, p < 0,05.",
        "ai_loading": "Özet hazırlanıyor…",
        "ai_template": "şablon (model kapalı)",
        "ai_narrator": "model anlatımı, sayılar doğrulandı",
        "ai_cached": "önbellekten",
        "ch_spend": "Günlük harcama",
        "ch_installs": "Günlük kurulum",
        "ch_cpi": "CPI",
        "ch_roas": "ROAS (kohort, harcama gününe göre)",
        "ch_roas_sub": "D7 yalnız 7. günü dolmuş kohortlar",
        "ch_fat_ipm": "Kreatif yorgunluğu: IPM",
        "ch_fat_ctr": "Kreatif yorgunluğu: CTR",
        "ch_fat_sub": "Yayındaki haftaya göre, dönemin en çok harcanan kreatifleri",
        "ch_split": "Ağ dağılımı",
        "ch_split_sub": "Dönemin harcaması; kurulum ve CPI yanında",
        "week": "Hafta",
        "week_n": "{n}. hafta",
        "network": "Ağ",
        "changes": "Değişiklikler",
        "changes_sub": "Grafiklerdeki numaralar bu listeyle aynı. Kaynak: Creative Forge denetim kaydı ya da değişiklik dosyası.",
        "changes_none": "Bu dönemde kayıtlı değişiklik yok.",
        "src_forge": "Creative Forge",
        "src_demo": "örnek",
        "src_file": "dosya",
        "table": "Kampanyalar ve kreatifler",
        "table_sub": "Ağ → kampanya → kreatif. Sütun başlığına tıklayarak sıralayın; satırı açıp kapatın.",
        "c_name": "Ad",
        "c_status": "Durum",
        "c_spend": "Harcama",
        "c_delta": "Δ önceki",
        "c_imps": "Gösterim",
        "c_views": "İzlenme",
        "c_complete": "Tamamlanma",
        "c_ctr": "CTR",
        "c_ipm": "IPM",
        "c_installs": "Kurulum",
        "c_cpi": "CPI",
        "c_roas": "D7 ROAS",
        "c_trend": "Harcama eğrisi",
        "st_live": "yayında",
        "st_stopped": "teslimat yok",
        "st_active": "aktif",
        "st_paused": "duraklatılmış",
        "detail": "Ayrıntı",
        "expand": "Aç / kapat",
        "no_ua": "Bu oyunun reklam verisi yok.",
        "no_ua_short": "reklam verisi yok",
        "reject_note": "Reddetmek için kısa bir not yazın (neden?).",
        "pl_sample_done": "Örnek oynanabilir hazır; şimdi ağlar için paketleyin.",
        "no_ua_sub": "Reklam ağlarından biri bağlandığında kampanyalar burada görünür. Aşağıdaki adım yeterli.",
        "no_period": "Seçili dönemde harcama yok; daha uzun bir dönem seçin.",
        "close": "Kapat",
        "d_title": "Kreatif",
        "d_preview": "Önizleme",
        "d_no_preview": "Önizleme yok: bu kreatif Creative Forge'da bulunamadı.",
        "d_over_time": "Zaman içinde",
        "d_where": "Nerede yayında",
        "d_fatigue": "Yorgunluk",
        "d_tired": "Yorulma belirtisi: IPM ilk haftaya göre {c} ({p}).",
        "d_fresh": "Yorgunluk belirtisi yok: IPM ilk haftaya göre {c} ({p}).",
        "d_watch": "IPM ilk haftaya göre {c} ({p}): düşüş var ama henüz anlamlı değil; izlemeye devam.",
        "d_young": "Henüz erken: {n} gündür yayında (14 gün gerekir).",
        "d_thin": "Ölçmek için gösterim az ({n}).",
        "d_live": "{a} – {b} · {n} gün",
        "d_forge": "Creative Forge",
        "d_qa": "QA",
        "d_review": "İnceleme",
        "d_period": "Seçili dönem",
        "d_all": "Tüm zaman",
        "forge_off": "Creative Forge bulunamadı: üretme ve yayınlama kapalı, sayılar okunmaya devam eder.",
        "forge_off_how": "Kurmak için Creative Forge README'deki tek satırlık kurulum; başka bir yerdeyse pulse.yaml → reklam.forge: <forge yolu> ya da PULSE_FORGE_BIN.",
        "forge_err": "Creative Forge yanıt vermedi: {e}",
        "forge_where": "Creative Forge klasörü: {root}",
        "w_title": "Yeni kreatif",
        "w_sub": "Creative Forge brif'ten varyantlar üretir, kalite kontrolünden geçirir; sonuçlar aşağıda incelemeyi bekler. Hiçbir şey kendiliğinden yayınlanmaz.",
        "w_format": "Biçim",
        "w_video": "Video",
        "w_static": "Durağan görsel",
        "w_static_off": "Creative Forge henüz durağan görsel üretmiyor.",
        "w_playable": "Oynanabilir",
        "w_playable_go": "Oynanabilir sekmesinde paketlenir",
        "w_brief": "Brif",
        "w_brief_ph": "ör. Tozlu raflar ilk 3 saniyede temizlenir; amaç IPM'i kontrolün üstüne çıkarmak.",
        "w_concept": "Kazanan konseptten başla",
        "w_from_ref": "Rakip kreatifinden geldi: {ref}. Brif ve etiketler doldu; değiştirip “Üret”e basın.",
        "w_hook": "Öne alınacak kanca",
        "w_concept_none": "— boş brif —",
        "w_concept_hint": "Dönemin en yüksek IPM'li konseptleri. Rakip kütüphanesinden seçmek: {link}",
        "w_variants": "Varyant sayısı",
        "w_langs": "Diller",
        "w_mechanic": "Gösterilen mekanik",
        "w_mode": "Üretim",
        "w_fake": "Deneme (ücretsiz, sahte sağlayıcılar, sentetik oynanış)",
        "w_real": "Gerçek (Vertex AI, ücretli)",
        "w_budget": "Bütçe tavanı (USD)",
        "w_gameplay": "Oynanış videosu (dosya yolu)",
        "w_real_note": "Gerçek üretim bütçe tavanı ve oyunun gerçek oynanış videosu ister; Creative Forge her ücretli çağrıdan önce tavanı ayırır.",
        "w_go": "Üret",
        "w_bad": "Formu kontrol edin: {e}",
        "jobs": "Üretim işleri",
        "job_running": "Üretiliyor… {d}/{t} adım · {m}",
        "job_done": "Hazır: {n} çıktı incelemede.",
        "job_failed": "Üretim başarısız: {e}",
        "review": "İnceleme",
        "review_sub": "Onay dosyanın SHA-256'sına bağlanır; dosya yeniden üretilirse onay düşer. Reddetmek için not gerekir.",
        "review_none": "Bu oyun için Creative Forge'da çıktı yok. Yukarıdaki sihirbazla üretin.",
        "approve": "Onayla",
        "reject": "Reddet",
        "note": "Not",
        "note_ph": "neden?",
        "rv_pending": "bekliyor",
        "rv_approve": "onaylandı",
        "rv_reject": "reddedildi",
        "rv_stale": "onay düştü (dosya değişti)",
        "qa_pass": "QA geçti",
        "qa_warn": "QA uyarı",
        "qa_fail": "QA kaldı",
        "qa_skip": "QA atlandı",
        "failed": "başarısız denetimler: {c}",
        "run": "Çıktı",
        "p_title": "Yayınla",
        "p_sub": "Onaylı ve QA'dan geçmiş dosyalar Creative Forge'un yayın klasörüne kopyalanır, kampanyalar her ağda DURAKLATILMIŞ oluşturulur. Yayına almak ayrı adımdır: tutar yazılarak onaylanır ve tavanların altında kalır.",
        "p_run": "Onaylı kreatifler",
        "p_run_none": "Onaylı kreatif yok: önce Kreatif üret sekmesinde onaylayın.",
        "p_n_approved": "{n} onaylı",
        "p_networks": "Ağlar",
        "p_api": "API ile (duraklatılmış oluşturulur)",
        "p_manual": "Elle yükleme paketi (bu ağlar duraklatılmış oluşturmayı belgelemiyor)",
        "p_name": "Kampanya adı",
        "p_name_hint": "küçük harf, rakam ve tire",
        "p_daily": "Günlük bütçe (USD)",
        "p_total": "Toplam tavan (USD)",
        "p_countries": "Ülkeler",
        "p_store": "Mağaza bağlantısı",
        "p_go": "Kampanyayı hazırla (duraklatılmış)",
        "p_mock_note": "Yeni yapılandırma deneme ağıyla (mock) yazılır: hiçbir istek makineden çıkmaz. Gerçek hesap için Creative Forge'daki yapılandırmaya gerçek kimlikleri yazıp mode: sandbox / live yapın.",
        "p_done": "Hazır: {n} kampanya duraklatılmış oluşturuldu{packs}. Hiçbiri yayında değil.",
        "p_done_packs": ", {n} elle yükleme paketi",
        "p_bad": "Hazırlanamadı: {e}",
        "camps": "Kampanyalar (Creative Forge)",
        "camps_none": "Bu oyun için Creative Forge kampanyası yok.",
        "c_mode": "Mod",
        "c_budget": "Bütçe",
        "c_dates": "Tarihler",
        "c_sync": "Son eşitleme",
        "activate": "Yayına al",
        "activate_ph": "Tam olarak yazın: {phrase}",
        "activate_help": "Yayına almak para harcatır. Onaylamak için tutarı içeren ifadeyi yazın: {phrase}. Günlük {daily} {cur}, toplam tavan {cap} {cur}, son gün {end}.",
        "live_box": "Gerçek hesap (live): para harcar",
        "pause": "Durdur",
        "act_done": "{name} yayına alındı.",
        "pause_done": "{name} durduruldu.",
        "act_bad": "Yayına alınmadı: {e}",
        "mode_mock": "deneme ağı",
        "mode_sandbox": "sandbox",
        "mode_live": "gerçek hesap",
        "mode_manual": "elle",
        "packs": "Elle yükleme paketleri",
        "pack_files": "{n} dosya",
        "download": "İndir (.zip)",
        "steps": "Adımlar",
        "configs": "Yapılandırmalar",
        "pl_title": "Oynanabilir reklamlar",
        "pl_sub": "Kaynak oyun dosyası her ağın kurallarına göre paketlenir ve denetlenir. Oynanabilir dosyalar ağ paneline elle yüklenir: paketlenmiş dosyayı indirin.",
        "pl_none": "Bu oyun için oynanabilir kaynak yok.",
        "pl_sample": "Örnek oynanabilir oluştur",
        "pl_sample_note": "Akışı denemek için küçük bir renk sıralama oyunu; gerçek oynanabilir oyunun kendisinden yapılır.",
        "pl_pack": "Paketle",
        "pl_preview": "Önizle",
        "pl_builds": "Ağ paketleri",
        "pl_built": "Paketlendi: {n} ağ.",
        "pl_bad": "Paketlenemedi: {e}",
        "pl_status_pass": "kurallara uygun",
        "pl_status_warn": "uyarı",
        "pl_status_uncertain": "belirsiz",
        "pl_status_fail": "kurala takıldı",
        "pl_frame": "Oynanabilir önizleme (korumalı çerçeve)",
        "bad_input": "Geçersiz istek.",
        "not_found": "Bulunamadı.",
        "na": "—",
    },
    "en": {
        "title": "Ad Centre",
        "sub": "Campaigns, creatives and publishing: a game's whole ad picture on one screen.",
        "game": "Game",
        "demo": "Sample data",
        "demo_long": "The numbers on this screen are from `pulse demo`'s fictional games; real data flows in by itself once an ad account is connected.",
        "tab_genel": "Overview",
        "tab_uret": "Make creatives",
        "tab_yayinla": "Publish",
        "tab_oynanabilir": "Playables",
        "tab_rakip": "Competitor creatives",
        "r_today": "Last day",
        "r_today_live": "Today",
        "r_7d": "7 days",
        "r_30d": "30 days",
        "r_custom": "Custom",
        "from": "From",
        "to": "To",
        "apply": "Apply",
        "period_line": "{a} – {b} · compared with {pa} – {pb}",
        "refresh": "Refresh now",
        "refresh_busy": "Refreshing…",
        "refresh_started": "Reading the ad networks again (today included); the page reloads when it is done.",
        "refresh_locked": "Another refresh is running; try again when it ends.",
        "refresh_none": "No ad network is connected to this game; connect one first.",
        "auto": "Auto-refresh every 15 min",
        "fresh_through": "through {d}",
        "fresh_loaded": "loaded {ago}",
        "fresh_replay": "recorded responses (sample)",
        "fresh_env": "missing key: {names}",
        "fresh_none": "no data yet",
        "missing": "Not connected",
        "connect": "Connect",
        "connect_step": "pulse.yaml → apps.{app}.sources.{kind} and {env}; step by step: Connections.",
        "k_spend": "Spend",
        "k_imps": "Impressions",
        "k_views": "Video views",
        "k_installs": "Installs",
        "k_cpi": "CPI",
        "k_d0": "D0 ROAS",
        "k_d7": "D7 ROAS",
        "k_payback": "Payback",
        "k_prev": "previous",
        "k_vs": "on the previous period",
        "k_views_na": "not reported",
        "k_views_note": "Meta 3-second and TikTok 2-second views; other networks report no views.",
        "k_d7_note": "Cohorts whose day 7 is complete ({n} days); revenue = network installs × the app's cohort ARPU.",
        "k_d7_wait": "no cohort has reached day 7",
        "k_payback_day": "day {d}",
        "k_payback_none": "not yet",
        "k_payback_note": "First day the pooled ROAS curve reaches 100% (last 30 days, {n} days observed).",
        "k_installs_note": "Installs as the networks report them (not MMP-attributed).",
        "k_cpi_note": "Spend / network installs.",
        "ai": "AI summary",
        "ai_note": "Code computes and checks the numbers; the model only words them. “Significant” = Welch test on daily values, p < 0.05.",
        "ai_loading": "Preparing the summary…",
        "ai_template": "template (model off)",
        "ai_narrator": "model wording, numbers verified",
        "ai_cached": "cached",
        "ch_spend": "Daily spend",
        "ch_installs": "Daily installs",
        "ch_cpi": "CPI",
        "ch_roas": "ROAS (cohort, by spend day)",
        "ch_roas_sub": "D7 only for cohorts whose day 7 is complete",
        "ch_fat_ipm": "Creative fatigue: IPM",
        "ch_fat_ctr": "Creative fatigue: CTR",
        "ch_fat_sub": "By week live, the period's biggest creatives",
        "ch_split": "Network split",
        "ch_split_sub": "The period's spend; installs and CPI alongside",
        "week": "Week",
        "week_n": "week {n}",
        "network": "Network",
        "changes": "Changes",
        "changes_sub": "The numbers on the charts match this list. Source: creative-forge's audit log or a change file.",
        "changes_none": "No changes recorded in this period.",
        "src_forge": "Creative Forge",
        "src_demo": "sample",
        "src_file": "file",
        "table": "Campaigns and creatives",
        "table_sub": "Network → campaign → creative. Click a header to sort; open and close rows.",
        "c_name": "Name",
        "c_status": "Status",
        "c_spend": "Spend",
        "c_delta": "Δ previous",
        "c_imps": "Impressions",
        "c_views": "Views",
        "c_complete": "Completion",
        "c_ctr": "CTR",
        "c_ipm": "IPM",
        "c_installs": "Installs",
        "c_cpi": "CPI",
        "c_roas": "D7 ROAS",
        "c_trend": "Spend trend",
        "st_live": "delivering",
        "st_stopped": "no delivery",
        "st_active": "active",
        "st_paused": "paused",
        "detail": "Details",
        "expand": "Open / close",
        "no_ua": "This game has no ad data.",
        "no_ua_short": "no ad data",
        "reject_note": "Write a short note to reject (why?).",
        "pl_sample_done": "Sample playable ready; now pack it for the networks.",
        "no_ua_sub": "Campaigns appear here once one of its ad networks is connected. The step below is enough.",
        "no_period": "No spend in the selected period; pick a longer one.",
        "close": "Close",
        "d_title": "Creative",
        "d_preview": "Preview",
        "d_no_preview": "No preview: this creative is not in creative-forge.",
        "d_over_time": "Over time",
        "d_where": "Where it runs",
        "d_fatigue": "Fatigue",
        "d_tired": "Signs of fatigue: IPM {c} on its first week ({p}).",
        "d_fresh": "No sign of fatigue: IPM {c} on its first week ({p}).",
        "d_watch": "IPM {c} on its first week ({p}): a drop, not significant yet; keep watching.",
        "d_young": "Too early: live for {n} days (14 needed).",
        "d_thin": "Too few impressions to tell ({n}).",
        "d_live": "{a} – {b} · {n} days",
        "d_forge": "Creative Forge",
        "d_qa": "QA",
        "d_review": "Review",
        "d_period": "Selected period",
        "d_all": "All time",
        "forge_off": "creative-forge not found: making and publishing are off; the numbers keep working.",
        "forge_off_how": "Install it with creative-forge's one-line installer; if it lives elsewhere, pulse.yaml → reklam.forge: <path to forge> or PULSE_FORGE_BIN.",
        "forge_err": "creative-forge did not answer: {e}",
        "forge_where": "creative-forge folder: {root}",
        "w_title": "New creative",
        "w_sub": "creative-forge renders variants from a brief and runs QA; the results wait for review below. Nothing is published by itself.",
        "w_format": "Format",
        "w_video": "Video",
        "w_static": "Static image",
        "w_static_off": "creative-forge does not make static images yet.",
        "w_playable": "Playable",
        "w_playable_go": "Packed on the Playables tab",
        "w_brief": "Brief",
        "w_brief_ph": "e.g. Dusty shelves cleared in the first 3 seconds; the goal is an IPM above the control.",
        "w_concept": "Start from a winning concept",
        "w_from_ref": "From a competitor creative: {ref}. The brief and tags are filled in; edit them and press “Make”.",
        "w_hook": "Hook to lead with",
        "w_concept_none": "— empty brief —",
        "w_concept_hint": "The period's highest-IPM concepts. To pick from the competitor library: {link}",
        "w_variants": "Variants",
        "w_langs": "Languages",
        "w_mechanic": "Mechanic shown",
        "w_mode": "Generation",
        "w_fake": "Trial (free, fake providers, synthetic gameplay)",
        "w_real": "Real (Vertex AI, paid)",
        "w_budget": "Budget cap (USD)",
        "w_gameplay": "Gameplay video (file path)",
        "w_real_note": "A real run needs a budget cap and a real gameplay capture; creative-forge reserves the cap before every paid call.",
        "w_go": "Make",
        "w_bad": "Check the form: {e}",
        "jobs": "Jobs",
        "job_running": "Rendering… step {d}/{t} · {m}",
        "job_done": "Ready: {n} export(s) waiting for review.",
        "job_failed": "Rendering failed: {e}",
        "review": "Review",
        "review_sub": "An approval is bound to the file's SHA-256; a re-render voids it. A rejection needs a note.",
        "review_none": "No exports for this game in creative-forge. Make some with the wizard above.",
        "approve": "Approve",
        "reject": "Reject",
        "note": "Note",
        "note_ph": "why?",
        "rv_pending": "pending",
        "rv_approve": "approved",
        "rv_reject": "rejected",
        "rv_stale": "approval void (file changed)",
        "qa_pass": "QA passed",
        "qa_warn": "QA warning",
        "qa_fail": "QA failed",
        "qa_skip": "QA skipped",
        "failed": "failed checks: {c}",
        "run": "Run",
        "p_title": "Publish",
        "p_sub": "Approved, QA-clean files are copied to creative-forge's delivery folder and campaigns are created PAUSED on every network. Switching on is a separate step: confirmed by typing the amount, under the caps.",
        "p_run": "Approved creatives",
        "p_run_none": "No approved creatives: approve some on the Make creatives tab first.",
        "p_n_approved": "{n} approved",
        "p_networks": "Networks",
        "p_api": "Through the API (created paused)",
        "p_manual": "Manual upload pack (these networks do not document a paused create)",
        "p_name": "Campaign name",
        "p_name_hint": "lower case, digits and dashes",
        "p_daily": "Daily budget (USD)",
        "p_total": "Total cap (USD)",
        "p_countries": "Countries",
        "p_store": "Store link",
        "p_go": "Prepare the campaign (paused)",
        "p_mock_note": "A new config is written for the mock network: no request leaves the machine. For a real account, put the real ids into the config in creative-forge and set mode: sandbox / live.",
        "p_done": "Ready: {n} campaign(s) created paused{packs}. None is active.",
        "p_done_packs": ", {n} manual upload pack(s)",
        "p_bad": "Not prepared: {e}",
        "camps": "Campaigns (creative-forge)",
        "camps_none": "No creative-forge campaigns for this game.",
        "c_mode": "Mode",
        "c_budget": "Budget",
        "c_dates": "Dates",
        "c_sync": "Last sync",
        "activate": "Switch on",
        "activate_ph": "Type exactly: {phrase}",
        "activate_help": "Switching on spends money. Type the phrase with the amount to confirm: {phrase}. Daily {daily} {cur}, total cap {cap} {cur}, last day {end}.",
        "live_box": "Real account (live): spends money",
        "pause": "Pause",
        "act_done": "{name} switched on.",
        "pause_done": "{name} paused.",
        "act_bad": "Not switched on: {e}",
        "mode_mock": "mock network",
        "mode_sandbox": "sandbox",
        "mode_live": "real account",
        "mode_manual": "manual",
        "packs": "Manual upload packs",
        "pack_files": "{n} file(s)",
        "download": "Download (.zip)",
        "steps": "Steps",
        "configs": "Configs",
        "pl_title": "Playable ads",
        "pl_sub": "A source build is packed and checked against each network's rules. Playables are uploaded in the network's dashboard by hand: download the packed file.",
        "pl_none": "No playable sources for this game.",
        "pl_sample": "Create a sample playable",
        "pl_sample_note": "A small colour-sorting game to try the flow; a real playable is built from the game itself.",
        "pl_pack": "Pack",
        "pl_preview": "Preview",
        "pl_builds": "Network builds",
        "pl_built": "Packed: {n} network(s).",
        "pl_bad": "Not packed: {e}",
        "pl_status_pass": "passes the rules",
        "pl_status_warn": "warning",
        "pl_status_uncertain": "uncertain",
        "pl_status_fail": "breaks a rule",
        "pl_frame": "Playable preview (sandboxed frame)",
        "bad_input": "Invalid request.",
        "not_found": "Not found.",
        "na": "—",
    },
}

PLAYABLE_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'"
)


class Form(dict):
    """A url-encoded form body (htmx posts forms url-encoded): `get` gives the first value, `getlist` all."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        v = super().get(key)
        return v[0] if v else default

    def getlist(self, key: str) -> list[str]:
        return list(super().get(key) or [])

    def flat(self) -> dict[str, str]:
        return {k: v[0] for k, v in self.items() if v}


async def read_form(request: Request, limit: int = 64_000) -> Form:
    """The url-encoded body, at most `limit` bytes (no multipart parser needed: nothing is uploaded here)."""
    from urllib.parse import parse_qs

    body = await request.body()
    if len(body) > limit:
        return Form()
    return Form(parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True))


def register(app: FastAPI, cfg: Any, templates: Any, *, context: Any, open_db: Any, rows: Any,
             hosts: set[str]) -> None:  # fmt: skip
    from ..brain import fmt as BF
    from .reklam_ai import Summarizer

    forge = Forge(cfg)
    app.state.forge = forge
    summarizer = Summarizer(cfg)
    templates.env.globals["reklam_nav"] = True  # the sidebar shows "Reklam Merkezi"

    # ------------------------------------------------------------------------------------------- helpers
    def money(v: Any, lang: str) -> str:
        return C.fmt(v, "usd", lang)

    def apps_list(con: Any) -> list[dict[str, Any]]:
        with_ua = D.has_ua(rows, con) if con is not None else set()
        out = [{"id": a.id, "name": a.name or a.id, "has_ua": a.id in with_ua} for a in cfg.apps]
        out.sort(key=lambda a: (not a["has_ua"], a["name"].lower()))
        return out

    def known(app_id: str) -> Any:
        return next((a for a in cfg.apps if a.id == app_id), None)

    def today() -> dt.date:
        return cfg.as_of or dt.date.today()

    def check_post(request: Request) -> PlainTextResponse | None:
        # the Guard already refused a missing token, a foreign Origin and cross-site requests; this is the
        # route's own check, as /run does it
        tok = request.headers.get("x-pulse-token", "")
        if not secrets.compare_digest(tok.encode(), str(templates.env.globals["token"]).encode()):
            return PlainTextResponse("missing or wrong token", 403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in hosts:
            return PlainTextResponse("cross-origin request refused", 403)
        return None

    def snap(force: bool = False) -> tuple[dict[str, Any] | None, str]:
        if not forge.available:
            return None, "missing"
        return forge.snapshot(force=force)

    def for_app(snapshot: dict[str, Any] | None, app_obj: Any) -> dict[str, Any]:
        """The snapshot's items that belong to this game: by its id, or by a store id it has in the config."""
        if not snapshot:
            return {"creatives": [], "runs": [], "campaigns": [], "packs": [], "playables": [], "published": [],
                    "configs": []}  # fmt: skip
        ids = D.store_ids(app_obj) | {app_obj.id}

        def mine(x: dict[str, Any]) -> bool:
            a = x.get("app") or {}
            a_id = a.get("id") if isinstance(a, dict) else a
            return (
                a_id == app_obj.id
                or (x.get("store_id") in ids)
                or (isinstance(a, dict) and a.get("store_id") in ids)
            )

        return {k: [x for x in snapshot.get(k) or [] if mine(x)]
                for k in ("creatives", "runs", "campaigns", "packs", "playables", "published", "configs")}  # fmt: skip

    def events_for(app_obj: Any, snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
        return E.load(cfg, app_obj.id, snapshot, D.store_ids(app_obj))

    # ------------------------------------------------------------------------------------- the overview data
    def overview(con: Any, app_obj: Any, p: D.Period, lang: str, last: dt.date | None,
                 snapshot: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
        t = T[lang]
        aid = app_obj.id
        tot = D.totals(rows, con, aid, p.start, p.end)
        prev = D.totals(rows, con, aid, p.prev_start, p.prev_end)
        data_end = last or p.end
        roas, proas = D.roas(rows, con, aid, p.start, p.end, data_end), D.roas(rows, con, aid, p.prev_start,
                                                                                p.prev_end, data_end)  # fmt: skip
        pay = D.payback(rows, con, aid)
        by_net = D.daily(rows, con, aid, p.start, p.end, by_network=True)
        all_cur = D.daily(rows, con, aid, p.start, p.end)["(all)"]
        all_prev = D.daily(rows, con, aid, p.prev_start, p.prev_end)["(all)"]
        coh = D.cohort_daily(rows, con, aid, p.start, p.end, data_end)
        dates = p.dates()
        events = events_for(app_obj, snapshot)
        shown = [e for e in events if p.start <= e["day"] <= p.end]
        markers = [C.Marker(e["day"], i + 1, f"{C.day(e['day'], lang)}: {E.text(e, lang, lambda v: money(v, lang))}")
                   for i, e in enumerate(shown)]  # fmt: skip
        nets = sorted(by_net, key=lambda n: D.NETWORKS.index(n) if n in D.NETWORKS else 99)

        def per_net(key: str) -> list[C.Series]:
            return [C.Series(D.label(n), [(by_net[n].get(d) or {}).get(key) if d in by_net[n] else
                                           (0.0 if key in ("spend", "installs") else None) for d in dates],
                             D.slot(n)) for n in nets]  # fmt: skip

        charts = {
            "spend": C.line(
                C.Chart(t["ch_spend"], kind="usd", markers=markers), dates, per_net("spend"), lang
            ),
            "installs": C.line(
                C.Chart(t["ch_installs"], kind="count", markers=markers),
                dates,
                per_net("installs"),
                lang,
            ),  # fmt: skip
            "cpi": C.line(
                C.Chart(t["ch_cpi"], kind="usd", markers=markers), dates, per_net("cpi"), lang
            ),
            "roas": C.line(
                C.Chart(t["ch_roas"], t["ch_roas_sub"], kind="roas", markers=markers),
                dates,
                [
                    C.Series("D0", [(coh.get(d) or {}).get("d0") for d in dates], 1),
                    C.Series("D7", [(coh.get(d) or {}).get("d7") for d in dates], 3),
                ],
                lang,
            ),  # fmt: skip
        }
        fat = D.fatigue_curves(rows, con, aid, p)
        weeks = fat["weeks"]
        names = [E.short(c["ad_name"] or c["ckey"], 28) for c in fat["curves"]]
        wk = lambda w: t["week_n"].format(n=w + 1)  # noqa: E731
        charts["fat_ipm"] = C.line(C.Chart(t["ch_fat_ipm"], t["ch_fat_sub"], kind="ipm", end_labels=False), weeks,
                                   [C.Series(nm, c["ipm"], i + 1) for i, (nm, c) in enumerate(zip(names, fat["curves"],
                                                                                                strict=False))],
                                   lang, x_label=t["week"], x_fmt=wk)  # fmt: skip
        charts["fat_ctr"] = C.line(C.Chart(t["ch_fat_ctr"], t["ch_fat_sub"], kind="pct2", end_labels=False), weeks,
                                   [C.Series(nm, c["ctr"], i + 1) for i, (nm, c) in enumerate(zip(names, fat["curves"],
                                                                                                strict=False))],
                                   lang, x_label=t["week"], x_fmt=wk)  # fmt: skip
        split = D.network_split(rows, con, aid, p)
        charts["split"] = C.hbars(
            C.Chart(t["ch_split"], t["ch_split_sub"], kind="usd"),
            [{"name": D.label(s["network"]), "value": s["spend"], "slot": D.slot(s["network"]),
              "cells": [f"{C.fmt(s.get('installs'), 'count', lang)} {t['c_installs'].lower()}",
                        f"CPI {C.fmt(s.get('cpi'), 'usd', lang)}", C.fmt(s.get("spend_share"), "pct", lang)]}
             for s in split],
            lang, [t["network"], t["c_spend"], t["c_installs"], t["c_cpi"], "%"],
        )  # fmt: skip
        tree = D.tree(rows, con, aid, p)
        roas_by = camp_roas(con, aid, p, data_end)
        cf_camps = {c["name"]: c for c in for_app(snapshot, app_obj)["campaigns"]}
        kpis = kpi_tiles(t, lang, tot, prev, roas, proas, pay, all_cur, dates)
        # the AI summary's inputs (computed once here, worded in the fragment)
        ai_ctx = {
            "period": p, "totals": tot, "prev_totals": prev, "roas": roas, "daily_cur": all_cur,
            "daily_prev": all_prev, "creatives": D.creatives_in(rows, con, aid, p.start, p.end),
            "events": events, "data_end": data_end,
            "campaign_daily": campaign_daily(con, aid, p.start - dt.timedelta(days=10), p.end),
            "tired": tired_creatives(con, aid, p, tree),
        }  # fmt: skip
        return {"kpis": kpis, "charts": charts, "tree": tree, "roas_by": roas_by, "cf_camps": cf_camps,
                "events": shown, "split": split, "ai_ctx": ai_ctx, "totals": tot}  # fmt: skip

    def camp_roas(con: Any, aid: str, p: D.Period, data_end: dt.date) -> dict[str, float]:
        out = {}
        for r in rows(con, "SELECT network, campaign_key, SUM(revenue_d7_usd) / NULLIF(SUM(spend_usd), 0) AS d7 "
                           "FROM fct_ua_cohort_roas WHERE app_id = ? AND cohort_date BETWEEN ? AND ? "
                           "AND cohort_date <= ? AND revenue_d7_usd IS NOT NULL GROUP BY ALL",
                      [aid, p.start, p.end, data_end - dt.timedelta(days=7)]):  # fmt: skip
            out[f"{r['network']}|{r['campaign_key']}"] = r["d7"]
            out.setdefault(r["network"], 0.0)
        for r in rows(con, "SELECT network, SUM(revenue_d7_usd) / NULLIF(SUM(spend_usd), 0) AS d7 "
                           "FROM fct_ua_cohort_roas WHERE app_id = ? AND cohort_date BETWEEN ? AND ? "
                           "AND cohort_date <= ? AND revenue_d7_usd IS NOT NULL GROUP BY ALL",
                      [aid, p.start, p.end, data_end - dt.timedelta(days=7)]):  # fmt: skip
            out[r["network"]] = r["d7"]
        return out

    def campaign_daily(
        con: Any, aid: str, a: dt.date, b: dt.date
    ) -> dict[str, dict[dt.date, dict[str, Any]]]:
        out: dict[str, dict[dt.date, dict[str, Any]]] = defaultdict(dict)
        for r in rows(con, f"SELECT campaign_name, date, {D._AGG} FROM stg_ua_spend WHERE app_id = ? "
                           "AND date BETWEEN ? AND ? GROUP BY campaign_name, date", [aid, a, b]):  # fmt: skip
            out[str(r["campaign_name"])][r["date"]] = D.metrics(r)
        return out

    def tired_creatives(
        con: Any, aid: str, p: D.Period, tree: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Fatigue of the period's 15 biggest live creatives that have run 14 days or more."""
        cands = sorted((k for n in tree for c in n["children"] for k in c["children"]
                        if k.get("live") and (k.get("days_live") or 0) >= 14), key=lambda k: -(k.get("spend") or 0))  # fmt: skip
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for k in cands[:15]:
            if k["creative_key"] in seen:
                continue
            seen.add(k["creative_key"])
            det = D.creative_detail(rows, con, aid, k["creative_key"], p)
            fat = (det or {}).get("fatigue") or {}
            if fat.get("state") == "tired":
                out.append(
                    {"name": k["name"], "days": fat["days"], "change": fat["change"], "p": fat["p"]}
                )
        return out[:2]

    def kpi_tiles(t: dict[str, str], lang: str, tot: dict, prev: dict, roas: dict, proas: dict, pay: dict,
                  daily_cur: dict, dates: list[dt.date]) -> list[dict[str, Any]]:  # fmt: skip
        def series(key: str) -> list[float]:
            return [float((daily_cur.get(d) or {}).get(key) or 0) for d in dates]

        def tile(key: str, label: str, v: Any, pv: Any, kind: str, better: str | None, note: str = "",
                 spark_key: str | None = None, sub: str | None = None) -> dict[str, Any]:  # fmt: skip
            change = (v / pv - 1) if (v is not None and pv) else None
            good = None
            if change is not None and better and abs(change) >= 0.0005:
                good = (change > 0) == (better == "up")
            return {"key": key, "label": label, "value": C.fmt(v, kind, lang), "prev": C.fmt(pv, kind, lang),
                    "change": C.signed_pct(change, lang) if change is not None else None, "good": good,
                    "note": note, "spark": C.spark(series(spark_key)) if spark_key else "", "na": v is None,
                    "sub": sub}  # fmt: skip

        views_sub = None
        if tot.get("views") is not None:
            views_sub = (f"{C.fmt(tot.get('view_rate'), 'pct', lang)} / "
                         f"{t['c_complete'].lower()} {C.fmt(tot.get('completion'), 'pct', lang)}")  # fmt: skip
        out = [
            tile(
                "spend",
                t["k_spend"],
                tot.get("spend"),
                prev.get("spend"),
                "usd",
                None,
                spark_key="spend",
            ),
            tile(
                "imps",
                t["k_imps"],
                tot.get("impressions"),
                prev.get("impressions"),
                "compact",
                None,
                spark_key="impressions",
            ),  # fmt: skip
            tile(
                "views",
                t["k_views"],
                tot.get("views"),
                prev.get("views"),
                "compact",
                "up",
                t["k_views_note"],
                spark_key="views" if tot.get("views") is not None else None,
                sub=views_sub,
            ),  # fmt: skip
            tile(
                "installs",
                t["k_installs"],
                tot.get("installs"),
                prev.get("installs"),
                "count",
                "up",
                t["k_installs_note"],
                spark_key="installs",
            ),  # fmt: skip
            tile(
                "cpi",
                t["k_cpi"],
                tot.get("cpi"),
                prev.get("cpi"),
                "usd",
                "down",
                t["k_cpi_note"],
                spark_key="cpi",
            ),
            tile("d0", t["k_d0"], roas.get("d0"), proas.get("d0"), "roas", "up"),
            tile(
                "d7",
                t["k_d7"],
                roas.get("d7"),
                proas.get("d7"),
                "roas",
                "up",
                t["k_d7_note"].format(n=roas.get("d7_days") or 0),
                sub=None if roas.get("d7") is not None else t["k_d7_wait"],
            ),  # fmt: skip
        ]
        pb = pay.get("day")
        out.append({"key": "payback", "label": t["k_payback"], "na": pb is None,
                    "value": t["k_payback_day"].format(d=pb) if pb is not None else t["k_payback_none"],
                    "prev": None, "change": None, "good": None, "spark": "",
                    "note": t["k_payback_note"].format(n=pay.get("curve_days") or 0), "sub": None})  # fmt: skip
        if tot.get("views") is None:
            out[2]["value"], out[2]["na"], out[2]["sub"] = t["k_views_na"], True, None
        return out

    flashes: dict[str, tuple[str, str, dict[str, Any] | None]] = {}

    def back(app_id: str, tab: str, kind: str, msg: str, *, anchor: str = "",
             form: dict[str, Any] | None = None) -> Response:  # fmt: skip
        """After a POST: a full navigation (htmx HX-Redirect) to the tab, with the message kept server-side
        under a random key, so the next page is a fresh GET and nothing the person typed lands in the URL."""
        key = secrets.token_urlsafe(
            9
        )  # always a new URL: a redirect to the same URL + #anchor would not reload
        flashes[key] = (kind, msg, {k: str(v)[:300] for k, v in (form or {}).items()} or None)
        while len(flashes) > 200:
            flashes.pop(next(iter(flashes)))
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/reklam/{app_id}?sekme={tab}&m={key}{anchor}"},
        )

    # ----------------------------------------------------------------------------------------- the page
    def render(request: Request, app_id: str, tab: str, extra: dict[str, Any] | None = None,
               status: int = 200) -> Response:  # fmt: skip
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(t["not_found"], 404)
        q = request.query_params
        rkey = q.get("range") if q.get("range") in RANGES else "7d"
        flash = flashes.pop(str(q.get("m") or ""), None)
        if flash and flash[1]:
            extra = {"flash": (flash[0], flash[1]), "form": flash[2], **(extra or {})}
        con, source = open_db(cfg)
        try:
            apps = apps_list(con)
            last = D.last_day(rows, con, app_id) if con is not None else None
            p = D.period(rkey, last, today(), q.get("from"), q.get("to"))
            snapshot, problem = snap() if tab != "genel" else cached_snap()
            data: dict[str, Any] = {"has_ua": last is not None}
            fresh = (
                D.freshness(rows, con, cfg, app_id, today())
                if con is not None
                else {"rows": [], "missing": []}
            )
            if tab == "genel" and last is not None:
                data.update(overview(con, app_obj, p, lang, last, snapshot))
            concepts = winning_concepts(con, app_id, p) if tab == "uret" else []
        finally:
            if con is not None:
                con.close()
        prefill = prefill_of(q) if tab == "uret" else {}
        mine = for_app(snapshot, app_obj)
        ctx = context(
            request, "reklam",
            t=T[lang], T=T, lang=lang, source=source, app=app_obj, apps=apps, tab=tab,
            period=p, rkey=rkey, last=last, data=data, fresh=fresh, forge=forge, forge_snap=snapshot,
            forge_problem=problem, mine=mine, demo=bool(cfg.is_demo), fmt=lambda v, k: C.fmt(v, k, lang),
            signed=lambda v: C.signed_pct(v, lang), day=lambda d: C.day(d, lang), short=E.short,
            label=D.label, slot=D.slot, spark=C.spark, ev_text=lambda e: E.text(e, lang, lambda v: money(v, lang)),
            store_url=D.store_url(app_obj), concepts=concepts, competitor_url=COMPETITOR_URL,
            jobs=[j.state() for j in forge.jobs.values() if j.app == app_id][-5:], mechanics=MECHANICS,
            langs=LANGS, api_networks=API_NETWORKS, manual_networks=MANUAL_NETWORKS,
            playable_networks=PLAYABLE_NETWORKS, today=today(), qs=qs_for(request), prefill=prefill,
            **(extra or {}),
        )  # fmt: skip
        resp = templates.TemplateResponse(request, "reklam.html", ctx, status_code=status)
        if request.cookies.get(COOKIE) != app_id:
            resp.set_cookie(COOKIE, app_id, max_age=365 * 86400, samesite="lax", httponly=True)
        return resp

    def cached_snap() -> tuple[dict[str, Any] | None, str]:
        """The overview waits for creative-forge once (the first snapshot, about a second); after that it shows
        the last snapshot and refreshes it in the background."""
        import threading
        import time

        if not forge.available:
            return None, "missing"
        at, s, problem = forge._snap
        if at == 0.0 and s is None and not problem:
            return forge.snapshot()
        if time.monotonic() - at > 30:
            threading.Thread(target=forge.snapshot, daemon=True).start()
        return s, problem

    def prefill_of(q: Any) -> dict[str, str]:
        """The wizard's fields from a competitor-library link, each checked (shown escaped, never run)."""
        out: dict[str, str] = {}
        if q.get("brief"):
            out["brief"] = str(q["brief"])[:400]
        if q.get("hook") in HOOKS:
            out["hook"] = str(q["hook"])
        mech = MECHANIC_ALIASES.get(str(q.get("mechanic") or ""), str(q.get("mechanic") or ""))
        if mech in MECHANICS:
            out["mechanic"] = mech
        for k in ("orientation", "genre"):
            if q.get(k) and re.fullmatch(r"[a-z0-9 _-]{1,30}", str(q[k])):
                out[k] = str(q[k])
        if q.get("ref") and re.fullmatch(r"[\w.:/-]{1,120}", str(q["ref"])):
            out["ref"] = str(q["ref"])
        return out

    def qs_for(request: Request) -> str:
        q = request.query_params
        keep = {k: q[k] for k in ("range", "from", "to") if q.get(k)}
        return "&".join(f"{k}={v}" for k, v in keep.items() if re.fullmatch(r"[0-9a-z-]{1,12}", v))

    def winning_concepts(con: Any, aid: str, p: D.Period) -> list[dict[str, Any]]:
        r = rows(con, "SELECT creative_concept AS concept, SUM(network_installs) * 1000.0 / NULLIF(SUM(impressions), 0) "
                      "AS ipm, SUM(impressions) AS imps FROM stg_ua_spend WHERE app_id = ? AND date BETWEEN ? AND ? "
                      "AND creative_concept IS NOT NULL GROUP BY 1 HAVING SUM(impressions) >= 5000 ORDER BY ipm DESC "
                      "LIMIT 5", [aid, p.prev_start, p.end])  # fmt: skip
        return r

    # ----------------------------------------------------------------------------------------- routes
    @app.get("/reklam", response_class=HTMLResponse)
    def reklam_home(request: Request):
        q = request.query_params
        chosen = q.get("oyun")  # the game switcher's form (works without JavaScript)
        if chosen and known(chosen):
            return RedirectResponse(f"/reklam/{chosen}", status_code=303)
        last = request.cookies.get(COOKIE)
        if any(q.get(k) for k in PREFILL):  # a competitor creative's "make one like this" link
            from urllib.parse import urlencode

            target = last if last and known(last) else (cfg.apps[0].id if cfg.apps else None)
            if target is None:
                return PlainTextResponse(T[request_lang(request)]["not_found"], 404)
            keep = {k: str(q[k])[:400] for k in PREFILL if q.get(k)}
            return RedirectResponse(f"/reklam/{target}?{urlencode({'sekme': 'uret', **keep})}#sihirbaz",
                                    status_code=303)  # fmt: skip
        if last and known(last):
            return RedirectResponse(f"/reklam/{last}", status_code=307)
        con, _ = open_db(cfg)
        try:
            apps = apps_list(con)
        finally:
            if con is not None:
                con.close()
        if not apps:
            return PlainTextResponse(T[request_lang(request)]["not_found"], 404)
        return RedirectResponse(f"/reklam/{apps[0]['id']}", status_code=307)

    @app.get("/reklam/{app_id}", response_class=HTMLResponse)
    def reklam_app(request: Request, app_id: str, sekme: str = "genel"):
        return render(request, app_id, sekme if sekme in TABS else "genel")

    @app.get("/reklam/{app_id}/ozet", response_class=HTMLResponse)
    def reklam_summary(request: Request, app_id: str):
        lang = request_lang(request)
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(T[lang]["not_found"], 404)
        q = request.query_params
        rkey = q.get("range") if q.get("range") in RANGES else "7d"
        con, _ = open_db(cfg)
        try:
            last = D.last_day(rows, con, app_id) if con is not None else None
            if last is None:
                return HTMLResponse("")
            p = D.period(rkey, last, today(), q.get("from"), q.get("to"))
            data = overview(con, app_obj, p, lang, last, cached_snap()[0])
        finally:
            if con is not None:
                con.close()
        out = summarizer.summary(data["ai_ctx"], lang, app_id)
        return templates.TemplateResponse(request, "_reklam_ai.html",
                                          context(request, "fragment", t=T[lang], ai=out, lang=lang))  # fmt: skip

    @app.get("/reklam/{app_id}/kreatif", response_class=HTMLResponse)
    def reklam_creative(request: Request, app_id: str, key: str = ""):
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None or not key or len(key) > 300:
            return PlainTextResponse(t["not_found"], 404)
        q = request.query_params
        rkey = q.get("range") if q.get("range") in RANGES else "7d"
        con, _ = open_db(cfg)
        try:
            last = D.last_day(rows, con, app_id) if con is not None else None
            p = D.period(rkey, last, today(), q.get("from"), q.get("to"))
            det = D.creative_detail(rows, con, app_id, key, p) if con is not None else None
        finally:
            if con is not None:
                con.close()
        if det is None:
            return PlainTextResponse(t["not_found"], 404)
        snapshot = cached_snap()[0]
        cf = None
        if det.get("creative_id") and snapshot:
            cf = next((c for c in for_app(snapshot, app_obj)["creatives"] if c.get("creative_id") == det["creative_id"]),
                      None) or next((c for c in snapshot.get("creatives") or []
                                     if c.get("creative_id") == det["creative_id"]), None)  # fmt: skip
        events = [e for e in events_for(app_obj, snapshot)
                  if any(str(det["ad_name"] or "") == str(x) for x in (e.get("detail") or {}).get("creatives") or [])
                  or any(e.get("campaign_name") == w["campaign"] for w in det["where"])]  # fmt: skip
        dates = [s["date"] for s in det["series"]]
        events = [e for e in events if dates and dates[0] <= e["day"] <= dates[-1]]
        marks = [C.Marker(e["day"], i + 1, f"{C.day(e['day'], lang)}: {E.text(e, lang, lambda v: money(v, lang))}")
                 for i, e in enumerate(events)]  # fmt: skip
        charts = {
            "ipm": C.line(C.Chart(t["c_ipm"], kind="ipm", height=120, markers=marks), dates,
                          [C.Series(t["c_ipm"], [s["ipm"] for s in det["series"]], 1)], lang, zero=False),
            "spend": C.line(C.Chart(t["c_spend"], kind="usd", height=120, markers=marks), dates,
                            [C.Series(t["c_spend"], [s["spend"] for s in det["series"]], 2)], lang),
        }  # fmt: skip
        ctx = context(request, "fragment" if request.headers.get("hx-request") else "reklam", t=t, lang=lang,
                      det=det, cf=cf, charts=charts, app=app_obj, fmt=lambda v, k: C.fmt(v, k, lang),
                      signed=lambda v: C.signed_pct(v, lang), day=lambda d: C.day(d, lang), short=E.short,
                      label=D.label, events=events,
                      ev_text=lambda e: E.text(e, lang, lambda v: money(v, lang)), p_fmt=lambda v: BF.number(v, "p", lang),
                      full=not request.headers.get("hx-request"), chosen="@reklam")  # fmt: skip
        name = (
            "_reklam_drawer.html" if request.headers.get("hx-request") else "reklam_creative.html"
        )
        return templates.TemplateResponse(request, name, ctx)

    @app.post("/reklam/{app_id}/yenile", response_class=HTMLResponse)
    def reklam_refresh(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(t["not_found"], 404)
        kinds = sorted({k.split("@", 1)[0] for k in (app_obj.sources or {})
                        if k.split("@", 1)[0] in (*D.SOURCE.values(), "ua_csv")})  # fmt: skip
        if not kinds:
            return HTMLResponse(
                f'<div class="callout warn" role="status">{t["refresh_none"]}</div>', 409
            )
        from .. import runstate

        lock = runstate.read_json(Path(cfg.state_dir) / runstate.LOCK_NAME)
        if lock and not runstate.RunLock(Path(cfg.state_dir) / runstate.LOCK_NAME).is_stale(lock):
            return HTMLResponse(
                f'<div class="callout warn" role="status">{t["refresh_locked"]}</div>', 409
            )
        launch = getattr(app.state, "reklam_launcher", None) or ua_launcher
        launch(cfg, app_id, kinds)
        return HTMLResponse(
            f'<div class="callout info" role="status" hx-get="/reklam/{app_id}/durum" hx-trigger="every 4s" '
            f'hx-swap="outerHTML">{t["refresh_started"]}</div>', 202)  # fmt: skip

    @app.get("/reklam/{app_id}/durum", response_class=HTMLResponse)
    def reklam_status(request: Request, app_id: str):
        """Polled while a refresh runs; tells htmx to reload the page when it has finished."""
        t = T[request_lang(request)]
        if known(app_id) is None:
            return PlainTextResponse(t["not_found"], 404)
        from .. import runstate

        hb = runstate.read_heartbeat(cfg.state_dir) or {}
        if hb.get("status") == "running":
            return HTMLResponse(f'<div class="callout info" role="status" hx-get="/reklam/{app_id}/durum" '
                                f'hx-trigger="every 4s" hx-swap="outerHTML">{t["refresh_busy"]}</div>')  # fmt: skip
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    # ---- create ---------------------------------------------------------------------------------------
    @app.post("/reklam/{app_id}/uret", response_class=HTMLResponse)
    async def reklam_make(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(t["not_found"], 404)
        form = await read_form(request)
        try:
            fmt_ = str(form.get("format") or "video")
            if fmt_ != "video":
                raise ValueError(t["w_static_off"] if fmt_ == "static" else t["w_playable_go"])
            brief = str(form.get("brief") or "").strip()[:500]
            variants = int(str(form.get("variants") or "2"))
            if not 1 <= variants <= 4:
                raise ValueError(t["w_variants"])
            langs = [x for x in form.getlist("lang") if x in LANGS] or ["tr"]
            mech = str(form.get("mechanic") or "other")
            if mech not in MECHANICS:
                raise ValueError(t["w_mechanic"])
            hook = str(form.get("hook") or "")
            hook = hook if hook in HOOKS else None
            ref = str(form.get("ref") or "")
            ref = ref if re.fullmatch(r"[\w.:/-]{1,120}", ref) else None
            mode = "vertex" if form.get("mode") == "real" else "fake"
            budget = None
            gameplay = None
            if mode == "vertex":
                budget = float(str(form.get("budget") or "0").replace(",", "."))
                if not 0 < budget <= 500:
                    raise ValueError(t["w_budget"])
                gameplay = str(form.get("gameplay") or "").strip()
                if not gameplay or not Path(gameplay).expanduser().is_file():
                    raise ValueError(t["w_gameplay"])
            if not forge.available:
                raise ValueError(t["forge_off"])
            forge.start_make(app=app_id, app_name=app_obj.name or app_id, store_url=D.store_url(app_obj), brief=brief,
                             variants=variants, languages=langs, mechanic=mech, providers=mode, budget=budget,
                             gameplay=str(Path(gameplay).expanduser()) if gameplay else None, hook=hook,
                             ref=ref)  # fmt: skip
        except (ValueError, ForgeError) as e:
            return back(app_id, "uret", "bad", t["w_bad"].format(e=e))
        return back(app_id, "uret", "good", "", anchor="#isler")

    @app.get("/reklam/{app_id}/is/{jid}", response_class=HTMLResponse)
    def reklam_job(request: Request, app_id: str, jid: str):
        lang = request_lang(request)
        job = forge.job(jid)
        if job is None or job.app != app_id:
            return PlainTextResponse(T[lang]["not_found"], 404)
        st = job.state()
        resp = templates.TemplateResponse(request, "_reklam_job.html",
                                          context(request, "fragment", t=T[lang], j=st, app_id=app_id))  # fmt: skip
        if (
            st["state"] != "running"
        ):  # finished while the page watched: reload once to show the new exports
            forge.invalidate()
            resp.headers["HX-Refresh"] = "true"
        return resp

    @app.post("/reklam/{app_id}/inceleme", response_class=HTMLResponse)
    async def reklam_review(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        form = await read_form(request)
        key = str(form.get("key") or "")
        decision = str(form.get("decision") or "")
        note = str(form.get("note") or "").strip()[:300]
        snapshot = snap()[0]
        item = (
            next((c for c in for_app(snapshot, app_obj)["creatives"] if c.get("key") == key), None)
            if app_obj
            else None
        )
        if item is None or decision not in ("approve", "reject"):
            return PlainTextResponse(t["bad_input"], 400)
        if decision == "reject" and not note:
            return back(app_id, "uret", "bad", t["reject_note"])
        run_dir = str(Path(item["path"]).parent)
        try:
            res = forge.review(run_dir, item["file"], decision, note or ("onay" if lang == "tr" else "approved"),
                               "studio-pulse")  # fmt: skip
        except ForgeError as e:
            return back(app_id, "uret", "bad", t["forge_err"].format(e=e))
        if not res.ok:
            return back(app_id, "uret", "bad", t["w_bad"].format(e=res.err))
        snap(force=True)
        return back(app_id, "uret", "good", "", anchor="#inceleme")

    # ---- publish ------------------------------------------------------------------------------------
    @app.post("/reklam/{app_id}/hazirla", response_class=HTMLResponse)
    async def reklam_prepare(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(t["not_found"], 404)
        form = await read_form(request)
        snapshot = snap(force=True)[0]
        mine = for_app(snapshot, app_obj)
        try:
            rid = str(form.get("run") or "")
            run = next((r for r in mine["runs"] if r["rid"] == rid and r["approved"]), None)
            if run is None:
                raise ValueError(t["p_run_none"])
            name = str(form.get("name") or "").strip().lower()
            if not NAME_RE.fullmatch(name):
                raise ValueError(f"{t['p_name']}: {t['p_name_hint']}")
            daily = float(str(form.get("daily") or "0").replace(",", "."))
            total = float(str(form.get("total") or "0").replace(",", "."))
            if not (0 < daily <= 10000 and daily <= total <= 1_000_000):
                raise ValueError(f"{t['p_daily']} / {t['p_total']}")
            countries = [
                c.strip().upper()
                for c in str(form.get("countries") or "TR").split(",")
                if c.strip()
            ]
            if (
                not countries
                or any(not re.fullmatch(r"[A-Z]{2}", c) for c in countries)
                or len(countries) > 30
            ):
                raise ValueError(t["p_countries"])
            nets = [n for n in form.getlist("network") if n in (*API_NETWORKS, *MANUAL_NETWORKS)]
            if not nets:
                raise ValueError(t["p_networks"])
            url = str(form.get("store_url") or D.store_url(app_obj) or "").strip()
            if not url.startswith("https://") or len(url) > 300:
                raise ValueError(t["p_store"])
            root = Path(snapshot["root"]) if snapshot else None  # type: ignore[index]
            if root is None:
                raise ValueError(t["forge_off"])
            dest = root / "deliver" / f"{name}-{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S')}"
            res = forge.publish(run["path"], dest)
            if not res.ok:
                raise ValueError(res.err or "publish")
            res = forge.init_campaign(root=root, name=name, creatives=str(dest), store_url=url, daily=daily,
                                      total=total, countries=countries, networks=nets, app=app_id)  # fmt: skip
            if not res.ok:
                raise ValueError(res.err or "campaign init")
            res = forge.create(res.data["config"])
            if res.data is None:
                raise ValueError(res.err or "campaign create")
            made = [n for n in res.data.get("networks") or [] if n.get("campaign")]
            stopped = [
                f"{n['network']}: {n['stopped']}"
                for n in res.data.get("networks") or []
                if n.get("stopped")
            ]
            if stopped:
                raise ValueError("; ".join(stopped))
        except (ValueError, ForgeError) as e:
            return back(app_id, "yayinla", "bad", t["p_bad"].format(e=e), form=form.flat())
        packs = res.data.get("packs") or []
        msg = t["p_done"].format(
            n=len(made), packs=t["p_done_packs"].format(n=len(packs)) if packs else ""
        )
        snap(force=True)
        return back(app_id, "yayinla", "good", msg)

    @app.post("/reklam/{app_id}/yayina-al", response_class=HTMLResponse)
    async def reklam_activate(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        return await campaign_action(request, app_id, "activate")

    @app.post("/reklam/{app_id}/durdur", response_class=HTMLResponse)
    async def reklam_pause(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        return await campaign_action(request, app_id, "pause")

    async def campaign_action(request: Request, app_id: str, action: str) -> Response:
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(t["not_found"], 404)
        form = await read_form(request)
        ref = str(form.get("ref") or "")
        camp = next(
            (c for c in for_app(snap(force=True)[0], app_obj)["campaigns"] if c.get("ref") == ref),
            None,
        )
        if camp is None:
            return PlainTextResponse(t["bad_input"], 400)
        try:
            if action == "activate":
                phrase = str(form.get("phrase") or "")[:80]
                live = form.get("live") == "1" and camp.get("mode") == "live"
                res = forge.activate(ref, phrase, live=live)
                ok_msg = t["act_done"].format(name=camp["name"])
            else:
                res = forge.pause(ref)
                ok_msg = t["pause_done"].format(name=camp["name"])
        except ForgeError as e:
            return back(app_id, "yayinla", "bad", t["forge_err"].format(e=e))
        snap(force=True)
        if not res.ok:
            err = cf_message(res.err or "", lang)
            return back(app_id, "yayinla", "bad", t["act_bad"].format(e=err))
        return back(app_id, "yayinla", "good", ok_msg)

    @app.get("/reklam/{app_id}/paket/{plan}/{network}.zip")
    def reklam_pack_zip(request: Request, app_id: str, plan: str, network: str):
        t = T[request_lang(request)]
        app_obj = known(app_id)
        if (
            app_obj is None
            or not re.fullmatch(r"[0-9a-z-]{1,40}", plan)
            or network not in MANUAL_NETWORKS
        ):
            return PlainTextResponse(t["not_found"], 404)
        pack = next((x for x in for_app(snap()[0], app_obj)["packs"]
                     if x.get("plan_id") == plan and x.get("network") == network), None)  # fmt: skip
        if pack is None:
            return PlainTextResponse(t["not_found"], 404)
        folder = Path(pack["folder"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix.lower() in (".mp4", ".json", ".txt", ".html", ".zip"):
                    z.write(f, f.name)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{plan}-{network}.zip"'})  # fmt: skip

    # ---- media and playables -----------------------------------------------------------------------------
    @app.get("/reklam/medya/{token}")
    def reklam_media(token: str):
        p = forge.media_path(token)
        if p is None or p.suffix.lower() not in (".mp4", ".jpg", ".jpeg", ".png"):
            return PlainTextResponse("not found", 404)
        from .forge_bridge import MEDIA_SUFFIXES

        return FileResponse(p, media_type=MEDIA_SUFFIXES[p.suffix.lower()],
                            headers={"Cache-Control": "private, max-age=300"})  # fmt: skip

    @app.get("/reklam/oynat/{token}")
    def reklam_play(token: str):
        """A playable's HTML for the preview frame: sandboxed (its own origin, no cookies, no network)."""
        p = forge.media_path(token)
        if p is None:
            return PlainTextResponse("not found", 404)
        body: bytes | None = None
        if p.is_dir() and (p / "index.html").is_file():
            body = (p / "index.html").read_bytes()
        elif p.suffix.lower() == ".html" and p.is_file():
            body = p.read_bytes()
        elif p.suffix.lower() == ".zip" and p.is_file():
            with zipfile.ZipFile(p) as z:
                if "index.html" in z.namelist():
                    body = z.read("index.html")
        if body is None or len(body) > 20_000_000:
            return PlainTextResponse("not found", 404)
        return Response(body, media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": PLAYABLE_CSP, "X-Frame-Options": "SAMEORIGIN",
                                 "Cross-Origin-Resource-Policy": "same-origin", "Cache-Control": "no-store"})  # fmt: skip

    @app.get("/reklam/indir/{token}")
    def reklam_download(token: str):
        p = forge.media_path(token)
        if p is None or not p.is_file():
            if p is not None and p.is_dir():
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(p.rglob("*")):
                        if f.is_file():
                            z.write(f, f.relative_to(p).as_posix())
                return Response(buf.getvalue(), media_type="application/zip",
                                headers={"Content-Disposition": f'attachment; filename="{p.name}.zip"'})  # fmt: skip
            return PlainTextResponse("not found", 404)
        return FileResponse(p, filename=p.name)

    @app.post("/reklam/{app_id}/oynanabilir", response_class=HTMLResponse)
    async def reklam_playable(request: Request, app_id: str):
        refused = check_post(request)
        if refused is not None:
            return refused
        lang = request_lang(request)
        t = T[lang]
        app_obj = known(app_id)
        if app_obj is None:
            return PlainTextResponse(t["not_found"], 404)
        form = await read_form(request)
        snapshot = snap(force=True)[0]
        if snapshot is None:
            return back(app_id, "oynanabilir", "bad", t["forge_off"])
        try:
            if form.get("action") == "sample":
                url = D.store_url(app_obj)
                if not url:
                    raise ValueError(t["p_store"])
                res = forge.run(["playable", "sample", "--json", "--root", snapshot["root"], "--name",
                                 f"{app_id.replace('_', '-')}-sample-{secrets.token_hex(2)}",
                                 "--title", app_obj.name or app_id, "--store-url", url, "--app", app_id,
                                 "--language", "tr" if lang == "tr" else "en"])  # fmt: skip
                if not res.ok:
                    raise ValueError(res.err or "sample")
                msg = t["pl_sample_done"]
            else:
                name = str(form.get("source") or "")
                item = next(
                    (x for x in for_app(snapshot, app_obj)["playables"] if x.get("name") == name),
                    None,
                )
                nets = [n for n in form.getlist("network") if n in PLAYABLE_NETWORKS]
                if item is None or not nets:
                    raise ValueError(t["bad_input"])
                errs = []
                for n in nets:
                    res = forge.playable_pack(item["path"], n, snapshot["root"])
                    if res.data is None:
                        errs.append(f"{n}: {res.err}")
                if errs and len(errs) == len(nets):
                    raise ValueError("; ".join(errs))
                msg = t["pl_built"].format(n=len(nets) - len(errs))
        except (ValueError, ForgeError) as e:
            return back(app_id, "oynanabilir", "bad", t["pl_bad"].format(e=e))
        snap(force=True)
        return back(app_id, "oynanabilir", "good", msg)


CF_TR = [
    (re.compile(r"refused by a safety rule: confirmation does not match; type exactly: (.+)$"),
     "güvenlik kuralı: onay ifadesi tutmuyor; tam olarak şunu yazın: {0}"),
    (re.compile(r"refused by a safety rule: (\S+) is already active$"), "{0} zaten yayında"),
    (re.compile(r"refused by a safety rule: (.+) is in live mode: real money.*$"),
     "{0} gerçek hesapta (live): para harcar; yalnız 'Gerçek hesap' kutusu işaretliyse yapılır"),
    (re.compile(r"refused by a safety rule: (.+) ended on (\S+); create a new campaign instead$"),
     "{0} {1} tarihinde bitti; yeni kampanya hazırlayın"),
    (re.compile(r"refused: (.+)$"), "reddedildi: {0}"),
]  # fmt: skip


def cf_message(err: str, lang: str) -> str:
    """creative-forge's refusals (English) worded for the Turkish page; anything else as it came."""
    text = err.strip().splitlines()[-1] if err.strip() else ""
    text = text.removeprefix("campaign: ")
    if lang != "tr":
        return text
    for rx, fmt_ in CF_TR:
        m = rx.match(text)
        if m:
            return fmt_.format(*m.groups())
    return text


def ua_launcher(cfg: Any, app_id: str, kinds: list[str]) -> int:
    """`pulse-bg run` for one game's ad networks, through today, detached like the Health page's refresh."""
    from .. import schedule

    log = Path(cfg.state_dir) / "logs" / "pulse.log"
    argv = [*schedule.background_program(), "--log-file", str(log), "run", "--config", str(cfg.path),
            "--app", app_id, "--through-today", *[x for k in kinds for x in ("--source", k)]]  # fmt: skip
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "cwd": str(cfg.base_dir)}  # fmt: skip
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs).pid  # noqa: S603  # nosec B603
