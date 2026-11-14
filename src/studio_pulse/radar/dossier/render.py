"""A dossier's facts rendered as Markdown, in Turkish and English (like the radar digest).
Every number carries its label; LLM output is marked as such, with its evidence pointer."""

from __future__ import annotations

from typing import Any

from ..adintel.meta_library import TOKEN_ENV
from ..digest import COUNTRY, LIST, fmt
from .labels import LABELS
from .rubric import FACTOR_NAMES, VERDICTS

T = {
    "tr": {
        "title": "Oyun dosyası",
        "intro": (
            "Kaynaklar: iTunes Lookup, Google Play ayrıntı sayfası (robots.txt'in izin verdiği yol; "
            "sayfadaki yorumlar dahil), radarın ambarı, AppsFlyer Q3 2026 CPI kıyasları, GameAnalytics "
            "2026 tutundurma bantları. Etiketler: GÖZLEM, MAĞAZA VERİSİ, TAHMİN, LLM (dil modeli; "
            "her iddianın alıntısı metinde ya da ekran görüntüsünde gösterildi, kanıtsızlar elendi), "
            "KIYAS (kaynaklı üçüncü taraf), VARSAYIM (stüdyo yapılandırması), KURAL PUANI."
        ),
        "identity": "Kimlik",
        "developer": "Geliştirici",
        "seller": "Satıcı",
        "genre": "Tür",
        "store": "Mağaza verisi",
        "ratings": "{c}: {n} puan, ortalama {avg}",
        "version": "sürüm {v} ({d})",
        "play_line": "Play: {bucket} kovası, {real} kurulum (sayfa alanı, belgelenmemiş), puan {avg} ({n} puan), reklam {ads}, uygulama içi satın alma {iap}{rng}",
        "yes": "var",
        "no": "yok",
        "lists": "Bugünkü listeler",
        "no_lists": "Radarın listelerinde bugün yok.",
        "history": "Radarda {d} gün görüldü, ilk {fs}, en iyi sıra #{b}",
        "class": "Radar sınıfı: {c} (kural puanı M {m})",
        "cadence": "Güncelleme ritmi",
        "cad_line": "Son güncelleme {d} ({n} gün önce); çıkıştan beri {age} gün",
        "cad_versions": "Bilinen sürümler: {v}",
        "cad_per": "Son 90 günde en az {v} sürüm (bilinen sürümler eksik bir örnek; gerçek sayı daha yüksek olabilir)",
        "shots": "Ekran görüntüleri",
        "shots_line": "{n} görüntü yerel önbellekte ({src}); LLM'e s1..s{n} olarak gönderildi.",
        "no_shots": "Ekran görüntüsü yok.",
        "decon": "Yapısöküm (LLM, kanıtla doğrulandı)",
        "decon_off": "LLM yapısökümü yok: {r}",
        "mechanic": "Birincil mekanik",
        "mech_src": {
            "llm": "LLM, alıntıyla doğrulandı",
            "llm_visual": "LLM, yalnızca ekran görüntüsünden",
            "radar_llm": "radar profili (LLM)",
            "radar_keyword": "radar profili (anahtar kelime)",
            "keyword": "anahtar kelime eşleşmesi",
        },
        "unknown": "bilinmiyor",
        "sections": {
            "core_loop": "Çekirdek döngü",
            "meta_layer": "Meta katman",
            "monetization": "Para kazanma",
            "art_style": "Sanat stili",
            "onboarding": "İlk dakikalar (onboarding)",
            "liveops": "LiveOps işaretleri",
            "strengths": "Neyi doğru yapıyor?",
        },
        "visual": "görsel çıkarım",
        "on_shot": "{s}'de: {w}",
        "proposed": "Önerilen yeni etiketler (taksonomide yok)",
        "rejected": "Elenen iddialar ({n})",
        "unknowns": "Metnin açık bıraktığı sorular",
        "reviews": "Oyuncu yorumları",
        "rev_src": "Play: {n} yorum (sayfadaki 'en alakalı' sıra; rastgele örnek değil). Apple: {a}.",
        "apple_refused": "alınmadı, robots.txt yasaklıyor ({r})",
        "stars": "Yıldız dağılımı: {v} (MAĞAZA VERİSİ); geliştirici yanıtı {r} yorumda",
        "sample_bias": "Bu yorumların ortalaması {m}★, mağaza ortalaması {a}★: Play'in seçtiği yorumlar puanlayanların tamamını temsil etmiyor.",
        "praise": "En çok övülenler",
        "complaints": "En çok şikâyet edilenler",
        "theme_line": "{t}: {c}/{n} yorum (LLM etiketi; ort. {s}★)",
        "rev_off": "Tema çıkarımı yapılmadı: {r}",
        "ads": "Rakip reklamları",
        "atc": "Google Reklam Şeffaflık Merkezi (BigQuery herkese açık veri kümesi)",
        "atc_refused": "Sorgu çalıştırılmadı: kuru çalıştırma {e} GB, tavan {c} GB (hiçbir şey faturalanmadı). Aranan: {p}.",
        "atc_ok": "{name}: {n} reklam (video {v}, görsel {i}, metin {t}); ilk {f}, son {l}; bölgeler: {r} (PLATFORM VERİSİ)",
        "atc_none": "Aranan adlarla reklamveren bulunamadı ({p}).",
        "meta": "Meta Reklam Kütüphanesi (AB reklamları)",
        "meta_nc": "Yapılandırılmadı: {m} tanımlı değil. Bağlayıcı yalnızca kayıtlı biçimli örneklerle test edildi, canlı çalıştırılmadı.",
        "meta_ok": "{name}: {n} reklam, {a} etkin; platformlar {p}; ilk {f}, son {l} (PLATFORM VERİSİ; bağlayıcı canlıda doğrulanmadı)",
        "decision": "Yapmalı mıyız?",
        "verdict": "**Karar: {v}** (kural puanı {s}; YAP ≥ {y}, BEKLE ≥ {b})",
        "reasons": "En güçlü üç neden",
        "gate": "kapı",
        "capped": "Karar en fazla BEKLE: {r}",
        "factor_head": "| Faktör | Puan | Ağırlık | Güven | Neden |",
        "conf": {"high": "yüksek", "low": "düşük"},
        "assumptions": "Varsayımlar",
        "bars": "Prototip kapısı (GameAnalytics 2026, KIYAS): {v}",
        "band": {"median": "medyan", "top 25%": "ilk %25", "top 10%": "ilk %10"},
        "test": "İlk test",
        "test_line": "{k}: {n} video varyantı, {geo} {plat}, {d} gün; bütçe ≈{b} USD (TAHMİN{rng}: kıyas CPI {cpi} USD × {inst} kurulum/varyant, varyant başına en az {floor} USD); geçme kapısı IPM ≥ {ipm} (VARSAYIM) ve CPI ≤ {cpi} USD (KIYAS).",
        "test_kind": {"creative_fake_door": "Kreatif + sahte kapı testi"},
        "variant": "{id}: kanca {h}{show}",
        "show": "; göster: {s}",
        "over_budget": "Uyarı: test bütçesi stüdyo bütçesini aşıyor.",
        "not_advised": "Karar YAPMA: bu test önerilmiyor; aşağıdaki, yine de denemek istenirse en ucuz yoldur.",
        "next": "Geçerse: kısa prototip ve yumuşak çıkış; kapı {v}. Geçmezse dur.",
        "forge": "creative-forge için market_trends.yaml yazıldı (signal: dossier_first_test).",
        "quality": "Koşum ve maliyet",
        "q_req": "İstekler: {n} (ana bilgisayara göre: {h}); önbellekten {c}.",
        "q_llm": "LLM: {p} {m}, {r} istek, {cost} USD (tavan {cap}).",
        "q_llm_none": "LLM: kapalı.",
        "q_bq": "BigQuery: bu koşumda faturalanan {g} GB.",
        "notes": "Notlar",
    },
    "en": {
        "title": "Game dossier",
        "intro": (
            "Sources: iTunes Lookup, the Google Play details page (the path robots.txt allows; "
            "including the reviews on that page), the radar's warehouse, AppsFlyer Q3 2026 CPI "
            "benchmarks, GameAnalytics 2026 retention bands. Labels: OBSERVED, STORE-REPORTED, "
            "ESTIMATE, LLM (language model; every claim's quote was found in the text or it points "
            "at a screenshot, claims without evidence were dropped), BENCHMARK (cited third party), "
            "ASSUMPTION (studio config), RULE SCORE."
        ),
        "identity": "Identity",
        "developer": "Developer",
        "seller": "Seller",
        "genre": "Genre",
        "store": "Store data",
        "ratings": "{c}: {n} ratings, average {avg}",
        "version": "version {v} ({d})",
        "play_line": "Play: {bucket} bucket, {real} installs (page field, undocumented), rating {avg} ({n} ratings), ads {ads}, in-app purchases {iap}{rng}",
        "yes": "yes",
        "no": "no",
        "lists": "Today's charts",
        "no_lists": "Not in the radar's charts today.",
        "history": "Seen by the radar on {d} day(s), first {fs}, best rank #{b}",
        "class": "Radar class: {c} (rule score M {m})",
        "cadence": "Update cadence",
        "cad_line": "Last update {d} ({n} days ago); {age} days since release",
        "cad_versions": "Known versions: {v}",
        "cad_per": "At least {v} versions in the last 90 days (the known versions are a sample; the real count may be higher)",
        "shots": "Screenshots",
        "shots_line": "{n} images in the local cache ({src}); sent to the LLM as s1..s{n}.",
        "no_shots": "No screenshots.",
        "decon": "Deconstruction (LLM, evidence-checked)",
        "decon_off": "No LLM deconstruction: {r}",
        "mechanic": "Primary mechanic",
        "mech_src": {
            "llm": "LLM, verified quote",
            "llm_visual": "LLM, screenshots only",
            "radar_llm": "radar profile (LLM)",
            "radar_keyword": "radar profile (keyword)",
            "keyword": "keyword match",
        },
        "unknown": "unknown",
        "sections": {
            "core_loop": "Core loop",
            "meta_layer": "Meta layer",
            "monetization": "Monetisation",
            "art_style": "Art style",
            "onboarding": "Onboarding",
            "liveops": "LiveOps signals",
            "strengths": "What it does right",
        },
        "visual": "visual inference",
        "on_shot": "on {s}: {w}",
        "proposed": "Proposed new tags (not in the taxonomy)",
        "rejected": "Dropped claims ({n})",
        "unknowns": "Questions the text leaves open",
        "reviews": "Player reviews",
        "rev_src": "Play: {n} reviews (the page's 'most relevant' order; not a random sample). Apple: {a}.",
        "apple_refused": "not fetched, robots.txt disallows it ({r})",
        "stars": "Stars: {v} (STORE-REPORTED); the developer replied to {r}",
        "sample_bias": "These reviews average {m}★ against a store average of {a}★: the reviews Play picks do not represent all raters.",
        "praise": "Most praised",
        "complaints": "Most complained about",
        "theme_line": "{t}: {c}/{n} reviews (LLM label; mean {s}★)",
        "rev_off": "No theme mining: {r}",
        "ads": "Competitor ads",
        "atc": "Google Ads Transparency Center (BigQuery public dataset)",
        "atc_refused": "Query not run: dry run {e} GB, cap {c} GB (nothing billed). Searched: {p}.",
        "atc_ok": "{name}: {n} ads (video {v}, image {i}, text {t}); first {f}, last {l}; regions: {r} (PLATFORM-REPORTED)",
        "atc_none": "No advertiser found for the names searched ({p}).",
        "meta": "Meta Ad Library (EU ads)",
        "meta_nc": "Not configured: {m} is not set. The connector is tested on recorded-shape fixtures only, never run live.",
        "meta_ok": "{name}: {n} ads, {a} active; platforms {p}; first {f}, last {l} (PLATFORM-REPORTED; connector not verified live)",
        "decision": "Should we make it?",
        "verdict": "**Verdict: {v}** (rule score {s}; MAKE IT >= {y}, WAIT >= {b})",
        "reasons": "The three strongest reasons",
        "gate": "gate",
        "capped": "Verdict capped at WAIT: {r}",
        "factor_head": "| Factor | Score | Weight | Confidence | Why |",
        "conf": {"high": "high", "low": "low"},
        "assumptions": "Assumptions",
        "bars": "Prototype gate (GameAnalytics 2026, BENCHMARK): {v}",
        "band": {"median": "median", "top 25%": "top 25%", "top 10%": "top 10%"},
        "test": "First test",
        "test_line": "{k}: {n} video variants, {geo} {plat}, {d} days; budget ≈{b} USD (ESTIMATE{rng}: benchmark CPI {cpi} USD × {inst} installs/variant, at least {floor} USD per variant); pass gate IPM ≥ {ipm} (ASSUMPTION) and CPI ≤ {cpi} USD (BENCHMARK).",
        "test_kind": {"creative_fake_door": "Creative + fake-door test"},
        "variant": "{id}: hook {h}{show}",
        "show": "; show: {s}",
        "over_budget": "Warning: the test budget exceeds the studio budget.",
        "not_advised": "Verdict DON'T: this test is not advised; below is the cheapest way if we try anyway.",
        "next": "If it passes: a short prototype and a soft launch; gate {v}. If not, stop.",
        "forge": "market_trends.yaml written for creative-forge (signal: dossier_first_test).",
        "quality": "Run and cost",
        "q_req": "Requests: {n} (by host: {h}); {c} from cache.",
        "q_llm": "LLM: {p} {m}, {r} request(s), {cost} USD (cap {cap}).",
        "q_llm_none": "LLM: off.",
        "q_bq": "BigQuery: {g} GB billed in this run.",
        "notes": "Notes",
    },
}


def _ev(e: dict[str, Any], lang: str) -> str:
    if e["kind"] == "screenshot":
        return T[lang]["on_shot"].format(s=e["screenshot"], w=e["what"])
    return f"“{e['quote']}” ({e['field']})"


def _claim(c: dict[str, Any], lang: str) -> str:
    t = T[lang]
    text = (c.get("text_tr") if lang == "tr" else None) or c["text"]
    kind = f"[{c['kind']}] " if c.get("kind") else ""
    vis = f" ({t['visual']})" if c.get("visual_only") else ""
    return f"- {kind}{text}{vis} — LLM; " + "; ".join(_ev(e, lang) for e in c["evidence"][:2])


def _bar(b: dict[str, Any], lang: str, ge: bool = False) -> str:
    pct = fmt(b["cut"] * 100, lang, 1)
    pct = f"%{pct}" if lang == "tr" else f"{pct}%"
    if ge:
        return f"D{b['day']} ≥ {pct}"
    return f"D{b['day']} {T[lang]['band'].get(b['label'], b['label'])} {pct}"


def render(d: dict[str, Any], lang: str) -> str:
    t, lab = T[lang], LABELS[lang]
    app = d["app"]
    L: list[str] = [f"# {t['title']} · {app['name']} · {d['as_of']}", "", t["intro"], ""]
    # identity
    L += [f"## {t['identity']}", ""]
    L.append(
        f"- {t['developer']}: {app.get('developer') or '-'}"
        + (f"; {t['seller']}: {app['seller']}" if app.get("seller") else "")
    )
    ap = (d.get("store") or {}).get("apple") or {}
    if ap:
        L.append(f"- {t['genre']}: {ap.get('primary_genre')} ({', '.join(ap.get('genres') or [])})")
    links = [x for x in (app.get("store_url"), app.get("play_url")) if x]
    if links:
        L.append("- " + " · ".join(links))
    L.append("")
    # store data
    L += [f"## {t['store']}", ""]
    for sf in ap.get("storefronts") or []:
        L.append(
            "- "
            + t["ratings"].format(
                c=COUNTRY[lang].get(sf["country"], sf["country"].upper()),
                n=fmt(sf.get("rating_count"), lang),
                avg=fmt(sf.get("rating_avg"), lang, 2),
            )
            + "; "
            + t["version"].format(v=sf.get("version"), d=sf.get("current_version_date"))
            + f" ({lab['observed']}, Lookup)"
        )
    p = (d.get("store") or {}).get("play")
    if p:
        rng = f" ({p['iap_range']})" if p.get("iap_range") else ""
        L.append(
            "- "
            + t["play_line"].format(
                bucket=p.get("bucket") or "-",
                real=fmt(p.get("real_installs"), lang),
                avg=fmt(p.get("rating_avg"), lang, 2),
                n=fmt(p.get("rating_count"), lang),
                ads=t["yes"] if p.get("contains_ads") else t["no"],
                iap=t["yes"] if p.get("offers_iap") else t["no"],
                rng=rng,
            )
            + f" ({lab['store_reported']})"
        )
    w = d.get("warehouse") or {}
    ranks = w.get("ranks_today") or []
    if ranks:
        L.append(
            f"- {t['lists']} ({w.get('latest_day')}): "
            + "; ".join(
                f"{COUNTRY[lang].get(r['country'], r['country'])} {LIST[lang].get(r['list'], r['list'])} #{r['rank']}"
                for r in ranks[:8]
            )
            + f" ({lab['observed']}, Apple RSS)"
        )
    elif w.get("available"):
        L.append(f"- {t['no_lists']}")
    if w.get("chart_history"):
        h = w["chart_history"]
        L.append(
            "- "
            + t["history"].format(d=h["days_charted"], fs=h["first_seen"], b=h["best_rank"])
            + f" ({lab['observed']})"
        )
    if w.get("breakout"):
        b = w["breakout"]
        L.append(
            "- "
            + t["class"].format(c=b["class"] or "-", m=fmt(b["momentum"], lang, 2))
            + f" ({lab['rule_score']})"
        )
    L.append("")
    # cadence
    cad = d.get("cadence") or {}
    L += [f"## {t['cadence']}", ""]
    upd = cad.get("apple_current_version_date") or cad.get("play_updated")
    if upd and cad.get("days_since_update") is not None:
        L.append(
            "- "
            + t["cad_line"].format(
                d=upd, n=cad["days_since_update"], age=cad.get("days_since_release", "-")
            )
            + f" ({lab['observed']})"
        )
    if cad.get("versions_known"):
        L.append(
            "- "
            + t["cad_versions"].format(
                v=", ".join(f"{v['version']} ({v['date']})" for v in cad["versions_known"][-8:])
            )
            + f" ({lab['observed']})"
        )
    if cad.get("versions_90d_min"):
        L.append("- " + t["cad_per"].format(v=cad["versions_90d_min"]) + f" ({lab['observed']})")
    L.append("")
    # screenshots
    shots = d.get("screenshots") or []
    L += [f"## {t['shots']}", ""]
    if shots:
        host = shots[0]["url"].split("/")[2]
        L.append(t["shots_line"].format(n=len(shots), src=host))
    else:
        L.append(t["no_shots"])
    L.append("")
    # deconstruction
    dec = d.get("deconstruction") or {}
    L += [f"## {t['decon']}", ""]
    m = d.get("mechanic") or {}
    mtxt = m.get("value") or t["unknown"]
    if m.get("value"):
        src = t["mech_src"].get(m.get("source") or "", m.get("source"))
        ev = "; ".join(_ev(e, lang) for e in (m.get("evidence") or [])[:2])
        L.append(f"- {t['mechanic']}: **{mtxt}** ({src}{'; ' + ev if ev else ''})")
    else:
        L.append(f"- {t['mechanic']}: {mtxt}")
    if dec.get("status") != "ok":
        L += ["", t["decon_off"].format(r=dec.get("reason") or dec.get("status")), ""]
    else:
        for sec, title in t["sections"].items():
            claims = dec.get(sec) or []
            if not claims:
                continue
            L += ["", f"### {title}", ""]
            L += [_claim(c, lang) for c in claims]
        if dec.get("proposed_tags"):
            L += ["", f"### {t['proposed']}", ""]
            for pt in dec["proposed_tags"]:
                L.append(
                    f"- `{pt['name']}`: {pt['why']} — LLM; "
                    + "; ".join(_ev(e, lang) for e in pt["evidence"][:2])
                )
        if dec.get("unknowns"):
            L += ["", f"### {t['unknowns']}", ""]
            L += [f"- {u} (LLM)" for u in dec["unknowns"]]
        rej = dec.get("rejected") or []
        L += ["", t["rejected"].format(n=len(rej)) + (":" if rej else "")]
        for r in rej[:8]:
            L.append(f"- [{r['section']}] {r['text']} — {r['reason']}")
        L.append("")
    # reviews
    rv = d.get("reviews") or {}
    L += [f"## {t['reviews']}", ""]
    ap_r = (rv.get("sources") or {}).get("apple") or {}
    a_txt = (
        t["apple_refused"].format(r=ap_r.get("reason"))
        if ap_r.get("status") == "refused"
        else (ap_r.get("status") or "-")
    )
    L.append(
        t["rev_src"].format(n=((rv.get("sources") or {}).get("play") or {}).get("n", 0), a=a_txt)
    )
    st = rv.get("stars") or {}
    if rv.get("n"):
        L.append("")
        L.append(
            t["stars"].format(
                v=" · ".join(f"{k}★ {st.get(k, 0)}" for k in ("5", "4", "3", "2", "1")),
                r=(rv.get("replied") or {}).get("value", 0),
            )
        )
        avg = (p or {}).get("rating_avg")
        if avg and rv.get("mean_stars") is not None and abs(avg - rv["mean_stars"]) >= 0.75:
            L.append("")
            L.append(t["sample_bias"].format(m=fmt(rv["mean_stars"], lang, 2), a=fmt(avg, lang, 2)))
    if rv.get("status") == "ok":
        for side in ("praise", "complaints"):
            rows = (rv.get("themes") or {}).get(side) or []
            L += ["", f"### {t[side]}", ""]
            if not rows:
                L.append("-")
            for r in rows[:5]:
                q = " · ".join(f"“{x['text']}” ({x['stars']}★)" for x in r["quotes"])
                L.append(
                    f"- {t['theme_line'].format(t=r[lang], c=r['count'], n=r['of'], s=fmt(r['mean_stars'], lang, 1))}: {q}"
                )
    elif rv.get("n"):
        L.append("")
        L.append(t["rev_off"].format(r=rv.get("reason") or rv.get("status")))
    L.append("")
    # ads
    ads = d.get("ads") or {}
    L += [f"## {t['ads']}", "", f"**{t['atc']}**", ""]
    g = ads.get("google_atc") or {}
    if g.get("status") == "refused":
        L.append(
            t["atc_refused"].format(
                e=fmt(g.get("estimate_gb"), lang, 2),
                c=fmt(g.get("cap_gb"), lang, 2),
                p=", ".join(g.get("patterns") or []),
            )
        )
    elif g.get("status") == "ok" and g.get("advertisers"):
        for a in g["advertisers"][:5]:
            regs = ", ".join(f"{r['region']} {r['creatives']}" for r in a["regions"][:6])
            L.append(
                "- "
                + t["atc_ok"].format(
                    name=a["name"],
                    n=fmt(a["creatives"], lang),
                    v=a["formats"]["video"],
                    i=a["formats"]["image"],
                    t=a["formats"]["text"],
                    f=a["first_shown"],
                    l=a["last_shown"],
                    r=regs,
                )
                + f" · {a['url']}"
            )
    elif g.get("status") == "ok":
        L.append(t["atc_none"].format(p=", ".join(g.get("patterns") or [])))
    else:
        L.append(f"{g.get('status')}: {g.get('message') or ''}".strip())
    mt = ads.get("meta") or {}
    L += ["", f"**{t['meta']}**", ""]
    if mt.get("status") == "ok":
        for pg in mt.get("pages") or []:
            L.append(
                "- "
                + t["meta_ok"].format(
                    name=pg["page_name"],
                    n=pg["ads"],
                    a=pg["active"],
                    p=", ".join(f"{k} {v}" for k, v in pg["platforms"].items()),
                    f=pg["first_start"],
                    l=pg["last_start"],
                )
            )
    elif mt.get("status") == "not_configured":
        L.append(t["meta_nc"].format(m=TOKEN_ENV))
    else:
        L.append(f"{mt.get('status')}: {mt.get('message') or ''}".strip())
    L.append("")
    # decision
    dc = d["decision"]
    L += [f"## {t['decision']}", ""]
    L.append(
        t["verdict"].format(
            v=VERDICTS[lang][dc["verdict"]],
            s=fmt(dc["score"]["value"], lang, 2),
            y=fmt(dc["thresholds"]["yap_min"], lang, 2),
            b=fmt(dc["thresholds"]["bekle_min"], lang, 2),
        )
        + f" ({lab['rule_score']})"
    )
    if dc.get("capped") and dc["verdict"] != "YAPMA":
        L.append("")
        L.append(t["capped"].format(r="; ".join(c[lang] for c in dc["capped"])))
    L += ["", f"### {t['reasons']}", ""]
    for i, r in enumerate(dc["reasons"], 1):
        tag = t["gate"] if r["kind"] == "gate" else f"{FACTOR_NAMES[lang][r['factor']]} {r['sign']}"
        L.append(f"{i}. ({tag}) {r['text'][lang]}")
    L += ["", t["factor_head"], "|---|---|---|---|---|"]
    for f in dc["factors"]:
        L.append(
            f"| {FACTOR_NAMES[lang][f['key']]} | {fmt(f['score']['value'], lang, 2)} | {fmt(f['weight']['value'], lang, 2)} | {t['conf'][f['confidence']]} | {f['reason'][lang]} |"
        )
    L += ["", f"### {t['assumptions']}", ""]
    L += [f"- {a[lang]}" for a in dc["assumptions"]]
    bars = ", ".join(_bar(b, lang) for b in dc["retention_bars"])
    L.append(f"- {t['bars'].format(v=bars)}")
    L.append("")
    # first test
    ft = d["first_test"]
    L += [f"## {t['test']}", ""]
    if dc["verdict"] == "YAPMA":
        L += [f"**{t['not_advised']}**", ""]
    bud = ft.get("budget_usd") or {}
    cpi = (ft.get("cpi_reference") or {}).get("value")
    L.append(
        t["test_line"].format(
            k=t["test_kind"].get(ft["kind"], ft["kind"]),
            n=ft["n_variants"]["value"],
            geo=COUNTRY[lang].get(ft["geo"], ft["geo"].upper()),
            plat=ft["platform"],
            d=ft["days"]["value"],
            b=bud.get("value", "-"),
            rng=f" {bud['low']}–{bud['high']}" if bud.get("low") != bud.get("high") else "",
            floor=fmt(ft.get("min_per_variant_usd", {}).get("value"), lang),
            cpi=fmt(cpi, lang, 2),
            inst=ft["installs_per_variant"]["value"],
            ipm=fmt(ft["ipm_gate"]["value"], lang, 0),
        )
    )
    for v in ft["variants"]:
        show = v.get("show_tr") if lang == "tr" else v.get("show")
        L.append(
            "- "
            + t["variant"].format(
                id=v["id"], h=v["hook"], show=t["show"].format(s=show) if show else ""
            )
        )
    if ft.get("over_budget"):
        L.append(f"- {t['over_budget']}")
    nb = ", ".join(_bar(b, lang, ge=True) for b in ft.get("next_gate") or [])
    L.append(f"- {t['next'].format(v=nb)}")
    L.append(f"- {t['forge']}")
    L.append("")
    # run
    meta = d.get("meta") or {}
    L += [f"## {t['quality']}", ""]
    rq = meta.get("requests") or {}
    L.append(
        "- "
        + t["q_req"].format(
            n=rq.get("sent", 0),
            h=", ".join(f"{k} {v}" for k, v in (rq.get("by_host") or {}).items()) or "-",
            c=rq.get("cache_hits", 0),
        )
    )
    llm = meta.get("llm")
    if llm:
        L.append(
            "- "
            + t["q_llm"].format(
                p=llm["provider"],
                m=llm["model"],
                r=llm["requests"],
                cost=fmt(llm["cost_usd"], lang, 4),
                cap=llm.get("budget_usd"),
            )
        )
    else:
        L.append(f"- {t['q_llm_none']}")
    if meta.get("bigquery_billed_gb") is not None:
        L.append("- " + t["q_bq"].format(g=fmt(meta["bigquery_billed_gb"], lang, 3)))
    if meta.get("notes"):
        L.append(f"- {t['notes']}: " + "; ".join(meta["notes"]))
    return "\n".join(L).rstrip() + "\n"
