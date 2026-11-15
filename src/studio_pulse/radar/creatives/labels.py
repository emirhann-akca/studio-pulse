"""Creative tag vocabulary (hook types, AI-likelihood signals, CTAs) and the page's words in
Turkish and English. Every value the LLM may return is listed here; anything else is dropped."""

from __future__ import annotations

# Hook in the first three seconds. "unknown" is never returned by the model: it is what
# metadata-only creatives get.
HOOKS: dict[str, tuple[str, str]] = {
    "fail_almost_win": ("Başarısızlık / az kalsın", "Fail / almost win"),
    "satisfying_asmr": ("Tatmin edici / ASMR", "Satisfying / ASMR"),
    "challenge": ("“Yalnız %X çözebilir” meydan okuması", "“Only X% can solve” challenge"),
    "fake_gameplay": ("Kurgu / stilize oynanış", "Fake / stylised gameplay"),
    "real_gameplay": ("Gerçek oynanış", "Real gameplay"),
    "ugc_person": ("UGC / kişi", "UGC / person"),
    "ai_character_scene": ("Yapay zekâ karakter / sahne", "AI-generated character / scene"),
    "before_after": ("Önce / sonra", "Before / after"),
    "story_drama": ("Hikâye / dram", "Story / drama"),
    "celebrity_meme": ("Ünlü / mim", "Celebrity / meme"),
    "other": ("Diğer", "Other"),
    "unknown": ("Etiketsiz", "Untagged"),
}
LLM_HOOKS = tuple(k for k in HOOKS if k != "unknown")

AI_LEVELS: dict[str, tuple[str, str]] = {
    "low": ("düşük", "low"),
    "medium": ("orta", "medium"),
    "high": ("yüksek", "high"),
    "unknown": ("bilinmiyor", "unknown"),
}
AI_SIGNALS: dict[str, tuple[str, str]] = {
    "morphing": ("biçim kayması / eriyen nesneler", "morphing or melting objects"),
    "hands_faces": ("tutarsız el / yüz", "inconsistent hands or faces"),
    "garbled_text": ("bozuk yazı", "garbled text"),
    "synthetic_voice": ("sentetik ses", "synthetic voice"),
    "uncanny_motion": ("doğal olmayan hareket", "uncanny motion"),
    "ai_render_style": ("tipik YZ görsel üslubu", "typical AI render style"),
    "none_observed": ("belirti görülmedi", "no sign observed"),
}
ORIENTATIONS = ("portrait", "landscape", "square", "unknown")
DURATIONS = ("under_15s", "15_30s", "30_60s", "over_60s", "unknown")
ORIENT_NAMES: dict[str, tuple[str, str]] = {
    "portrait": ("Dikey", "Portrait"),
    "landscape": ("Yatay", "Landscape"),
    "square": ("Kare", "Square"),
    "unknown": ("Bilinmiyor", "Unknown"),
}
DURATION_NAMES: dict[str, tuple[str, str]] = {
    "under_15s": ("15 sn'den kısa", "Under 15 s"),
    "15_30s": ("15-30 sn", "15-30 s"),
    "30_60s": ("30-60 sn", "30-60 s"),
    "over_60s": ("60 sn'den uzun", "Over 60 s"),
    "unknown": ("Bilinmiyor", "Unknown"),
}
CTAS: dict[str, tuple[str, str]] = {
    "play_now": ("Hemen oyna", "Play now"),
    "download_install": ("İndir / yükle", "Download / install"),
    "try_free": ("Ücretsiz dene", "Try free"),
    "other": ("Diğer", "Other"),
    "none": ("Yok", "None"),
    "unknown": ("Bilinmiyor", "Unknown"),
}
KINDS = ("video", "image", "text", "playable", "unknown")
TAG_SOURCES: dict[str, tuple[str, str]] = {
    "llm_video": ("LLM videoyu izledi (gözlem; kodla doğrulanamaz)",
                  "LLM watched the video (observation; code cannot verify it)"),
    "llm_text": ("LLM, reklam metninden (alıntılar doğrulandı)",
                 "LLM, from the ad text (quotes verified)"),
    "keyword": ("anahtar kelime tahmini (reklam metni)", "keyword guess (ad text)"),
    "metadata": ("yalnız meta veri (biçim, tarih, bölge)", "metadata only (format, dates, regions)"),
}  # fmt: skip
SOURCES: dict[str, tuple[str, str]] = {
    "google_atc": ("Google Reklam Şeffaflık Merkezi", "Google Ads Transparency Center"),
    "meta": ("Meta Reklam Kütüphanesi", "Meta Ad Library"),
    "tiktok": ("TikTok Ticari İçerik Kütüphanesi", "TikTok Commercial Content Library"),
    "youtube": ("YouTube (elle eklendi)", "YouTube (added by hand)"),
}
FORMATS: dict[str, tuple[str, str]] = {
    "VIDEO": ("Video", "Video"),
    "IMAGE": ("Görsel", "Image"),
    "TEXT": ("Metin", "Text"),
    "PLAYABLE": ("Oynanabilir", "Playable"),
    "UNKNOWN": ("Bilinmiyor", "Unknown"),
}
GENRE_NAMES: dict[str, tuple[str, str]] = {
    "puzzle": ("Bulmaca", "Puzzle"),
    "sort": ("Sıralama", "Sort"),
    "casual": ("Casual", "Casual"),
    "word": ("Kelime", "Word"),
    "board": ("Masa oyunu", "Board"),
    "other": ("Diğer", "Other"),
}
PARTS: dict[str, tuple[str, str]] = {
    "longevity": ("Yayında kalma süresi", "Days live"),
    "active": ("Hâlâ yayında", "Still running"),
    "variants": ("Aynı fikrin varyantları", "Variants of the concept"),
    "spread": ("Bölge / ağ yaygınlığı", "Regions / networks"),
    "volume": ("Gösterim / erişim kanıtı", "Impressions / reach evidence"),
}

# Keyword hooks for ad text (Meta bodies and titles, YouTube titles): whole words, English
# and Turkish. A keyword hit is labelled a guess; the quote is the matched text.
HOOK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "challenge": ("can you", "can't solve", "cannot solve", "iq", "genius", "hardest",
                  "impossible", "only smart", "çözebilir misin", "zeka", "dahi"),
    "fail_almost_win": ("fail", "so close", "almost", "nearly", "oops", "neredeyse",
                        "az kalsın"),
    "satisfying_asmr": ("satisfying", "asmr", "relax", "relaxing", "oddly", "rahatlatıcı",
                        "tatmin"),
    "before_after": ("before and after", "before/after", "makeover", "renovate",
                     "öncesi", "sonrası"),
    "story_drama": ("help her", "help him", "save her", "save him", "story", "drama",
                    "kurtar", "hikaye", "hikâye"),
    "celebrity_meme": ("meme", "viral", "trend", "mim"),
}  # fmt: skip
CTA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "play_now": ("play now", "play game", "play free", "hemen oyna", "şimdi oyna", "oyna"),
    "download_install": ("install", "download", "get it", "indir", "yükle"),
    "try_free": ("try", "free to play", "ücretsiz", "dene"),
}

T: dict[str, dict[str, str]] = {
    "tr": {
        "title": "Rakip Kreatif Kütüphanesi",
        "sub": "Büyük mobil oyun şirketlerinin yayındaki reklam kreatifleri: kanca türüne göre "
        "ayrılmış, yapay zekâ ile üretilmiş olma olasılığı ve kamu sinyallerinden tahmin "
        "edilen kazanan puanıyla. Rakiplerin performansını kimse yayımlamaz: puan bir "
        "TAHMİNDİR, nasıl hesaplandığı her kartta yazar. Video dosyası indirilmez ve saklanmaz.",
        "back": "Pazar radarı",
        "filters": "Filtreler",
        "view_all": "Tüm kreatifler",
        "view_ai": "YZ ile üretilmiş videolar",
        "view_hooks": "Kancaya göre diğer videolar",
        "ai_view_note": "LLM'in izlediği {w} videodan YZ ile üretilmiş olma olasılığı orta ya "
        "da yüksek olanlar. Bu bir olasılıktır, kesinlik değil; işaretler her kartın "
        "ayrıntısında yazar. Google veri kümesi videoyu vermediği için yalnız YouTube "
        "bağlantısı olan kreatifler izlenebilir.",
        "ai_view_empty": "Henüz YZ olasılığı orta/yüksek video yok ({w} video izlendi). Bir "
        "Google kreatifinin videosunu bağlamak için: pulse radar creatives add "
        "<youtube-adresi> --for <kreatif-anahtarı> --tag",
        "hooks_note": "Etiketli kreatifler kanca türüne göre ayrıldı (her türden en yüksek "
        "puanlı sekiz). {u} kreatif etiketsiz: videosu olmayan Google satırları.",
        "hooks_empty": "Henüz etiketli kreatif yok.",
        "more": "{n} kreatif; bu türde hepsini görmek için:",
        "genre": "Tür",
        "advertiser": "Reklamveren",
        "hook": "Kanca (ilk 3 sn)",
        "ai": "YZ ile üretilmiş olma olasılığı",
        "format": "Biçim",
        "region": "Bölge",
        "min_score": "En az kazanan puanı",
        "active": "Yalnız hâlâ yayında",
        "source": "Kaynak",
        "apply": "Uygula",
        "reset": "Temizle",
        "all": "Hepsi",
        "results": "{n} kreatif",
        "showing": "{n} kreatiften ilk {m} tanesi gösteriliyor; filtreyle daraltın.",
        "empty": "Bu filtrelerle kreatif yok.",
        "empty_all": "Henüz kreatif yok. Haftalık tarama: pulse radar creatives refresh "
        "(BigQuery'de herkese açık Google Reklam Şeffaflık verisi; önce kuru çalıştırma, "
        "üst sınırın üstündeyse reddedilir). Bir YouTube reklamını elle eklemek için: "
        "pulse radar creatives add <youtube-adresi> --advertiser <ad>.",
        "summary": "Bu ay, bizim türlerimizde ({genres})",
        "top_hooks": "En sık kancalar",
        "ai_share": "YZ olasılığı orta/yüksek",
        "ai_share_sub": "{a} / {b} etiketli videoda (olasılık, kesinlik değil)",
        "top_advs": "En çok aktif kreatifi olanlar",
        "no_tags": "Etiketli kreatif yok",
        "score": "Kazanan puanı",
        "score_note": "Kamu sinyallerinden tahmin (TAHMİN), gerçek performans değil.",
        "first_shown": "İlk gösterim",
        "last_shown": "Son gösterim",
        "days_live": "{d} gün yayında",
        "running": "hâlâ yayında",
        "stopped": "{d} gündür görünmüyor",
        "regions": "{n} bölge",
        "open_source": "Kaynakta aç",
        "detail": "Ayrıntı",
        "tags": "Etiketler",
        "tag_source": "Etiketin kaynağı",
        "confidence": "güven",
        "orientation": "Yön",
        "duration": "Süre",
        "mechanic": "Gösterilen mekanik",
        "cta": "Eylem çağrısı",
        "language": "Dil",
        "kind": "Tür",
        "why": "Neden işe yarayabilir",
        "why_none": "Gözleme dayalı bir neden yazılmadı.",
        "why_signals": "kamu sinyallerinden çıkarım; videonun içeriği değil",
        "evidence": "Gözlenenler",
        "breakdown": "Puanın parçaları",
        "part": "Parça",
        "signal": "Sinyal",
        "value": "Değer (0-1)",
        "weight": "Ağırlık",
        "points": "Puan",
        "missing": "veri yok",
        "timeline": "Zaman çizelgesi",
        "similar": "Benzer kreatifler",
        "similar_none": "Aynı kanca ve mekanikte başka kreatif yok.",
        "make": "Bu fikirle kreatif üret",
        "make_sub": "Reklam Merkezi sihirbazını bu kreatifin kancası ve mekaniğiyle açar; "
        "rakibin videosu kopyalanmaz.",
        "variants": "{n} varyant",
        "cluster_note": "Varyant: aynı reklamverenin aynı kanca + mekanikle etiketli kreatifleri.",
        "yt_attr": "Video YouTube'dan oynatılır.",
        "no_preview": "Önizleme yok: bu kaynak videoyu yayımlamıyor. Kaynak sayfasında izleyin.",
        "times_shown": "en az {n} gösterim",
        "eu_reach": "AB erişimi {n}",
        "platforms": "Ağlar",
        "ad_text": "Reklam metni",
        "updated": "Son tarama",
        "sources_note": "Kaynaklar: Google Reklam Şeffaflık Merkezi (BigQuery'deki herkese açık "
        "veri kümesi), Meta Reklam Kütüphanesi API'si (anahtar gelince), elle eklenen YouTube "
        "reklamları. TikTok bağlayıcısı onay bekliyor (kapalı).",
        "likelihood": "olasılık",
        "note_google": "Google'daki reklamveren adı",
        "note_meta": "Meta sayfası",
        "note_owner": "Not (elle girildi)",
        "not_game": "Model bunun bir mobil oyun reklamı olmadığını söyledi; etiketlere temkinle bakın.",
        "untagged_note": "Bu kreatif yalnız meta veriden etiketlendi: Google veri kümesi videoyu "
        "vermiyor.",
        "not_found": "Kreatif bulunamadı.",
        "parts_note": "Puan = Σ ağırlık × parça; her parça 0-1 arası ve sinyali arttıkça "
        "azalmaz. Verisi olmayan parça 0 sayılır (kanıt yoksa puan da yok).",
    },
    "en": {
        "title": "Competitor Creative Library",
        "sub": "Ad creatives the big mobile game companies are running: split by hook type, "
        "with the likelihood that they are AI-generated and a winner score estimated from "
        "public signals. Nobody publishes competitors' performance: the score is an "
        "ESTIMATE and every card shows how it is computed. No video file is downloaded or "
        "stored.",
        "back": "Market radar",
        "filters": "Filters",
        "view_all": "All creatives",
        "view_ai": "AI-generated videos",
        "view_hooks": "Other videos by hook",
        "ai_view_note": "Of the {w} videos the LLM watched, those with a medium or high "
        "likelihood of being AI-generated. A likelihood, not a certainty; the signs are listed "
        "on each card's detail page. The Google dataset does not carry the video, so only "
        "creatives with a YouTube link can be watched.",
        "ai_view_empty": "No video with a medium/high AI likelihood yet ({w} watched). To link "
        "the video of a Google creative: pulse radar creatives add <youtube-url> --for "
        "<creative-key> --tag",
        "hooks_note": "Tagged creatives split by hook type (the eight best-scored of each). {u} "
        "creatives are untagged: Google rows without a video.",
        "hooks_empty": "No tagged creative yet.",
        "more": "{n} creatives; to see all of this type:",
        "genre": "Genre",
        "advertiser": "Advertiser",
        "hook": "Hook (first 3 s)",
        "ai": "AI-generated likelihood",
        "format": "Format",
        "region": "Region",
        "min_score": "Minimum winner score",
        "active": "Still running only",
        "source": "Source",
        "apply": "Apply",
        "reset": "Clear",
        "all": "All",
        "results": "{n} creatives",
        "showing": "Showing the first {m} of {n} creatives; narrow the filters.",
        "empty": "No creative matches these filters.",
        "empty_all": "No creatives yet. Weekly scan: pulse radar creatives refresh (the public "
        "Google Ads Transparency data in BigQuery; dry-run first, refused above the cap). To "
        "add a YouTube ad by hand: pulse radar creatives add <youtube-url> --advertiser <name>.",
        "summary": "This month, in our genres ({genres})",
        "top_hooks": "Top hooks",
        "ai_share": "AI likelihood medium/high",
        "ai_share_sub": "{a} of {b} tagged videos (a likelihood, not a certainty)",
        "top_advs": "Most active creatives",
        "no_tags": "No tagged creative",
        "score": "Winner score",
        "score_note": "Estimated from public signals (ESTIMATE), not real performance.",
        "first_shown": "First shown",
        "last_shown": "Last shown",
        "days_live": "{d} days live",
        "running": "still running",
        "stopped": "not seen for {d} days",
        "regions": "{n} regions",
        "open_source": "Open at the source",
        "detail": "Details",
        "tags": "Tags",
        "tag_source": "Tag source",
        "confidence": "confidence",
        "orientation": "Orientation",
        "duration": "Length",
        "mechanic": "Mechanic shown",
        "cta": "Call to action",
        "language": "Language",
        "kind": "Kind",
        "why": "Why it may work",
        "why_none": "No reason grounded in observations was written.",
        "why_signals": "read from public signals, not from the video's content",
        "evidence": "Observed",
        "breakdown": "Score parts",
        "part": "Part",
        "signal": "Signal",
        "value": "Value (0-1)",
        "weight": "Weight",
        "points": "Points",
        "missing": "no data",
        "timeline": "Timeline",
        "similar": "Similar creatives",
        "similar_none": "No other creative with the same hook and mechanic.",
        "make": "Make a creative from this idea",
        "make_sub": "Opens the Ad Center wizard with this creative's hook and mechanic; the "
        "competitor's video is not copied.",
        "variants": "{n} variants",
        "cluster_note": "Variant: the same advertiser's creatives tagged with the same hook "
        "and mechanic.",
        "yt_attr": "The video plays from YouTube.",
        "no_preview": "No preview: this source does not publish the video. Watch it on the "
        "source page.",
        "times_shown": "at least {n} impressions",
        "eu_reach": "EU reach {n}",
        "platforms": "Networks",
        "ad_text": "Ad text",
        "updated": "Last scan",
        "sources_note": "Sources: Google Ads Transparency Center (the public BigQuery "
        "dataset), the Meta Ad Library API (once a token exists), YouTube ads added by hand. "
        "The TikTok connector awaits approval (off).",
        "likelihood": "likelihood",
        "note_google": "Advertiser name at Google",
        "note_meta": "Meta page",
        "note_owner": "Note (typed by hand)",
        "not_game": "The model said this is not a mobile game ad; read the tags with care.",
        "untagged_note": "Tagged from metadata only: the Google dataset does not carry the video.",
        "not_found": "Creative not found.",
        "parts_note": "Score = Σ weight × part; every part is 0-1 and never falls as its "
        "signal grows. A part without data counts as 0 (no evidence, no points).",
    },
}


def name(table: dict[str, tuple[str, str]], key: str | None, lang: str) -> str:
    v = table.get(str(key or ""))
    if v is None:
        return str(key or "-")
    return v[0 if lang == "tr" else 1]
