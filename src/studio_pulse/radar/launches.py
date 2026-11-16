"""New launches ("Yeni Çıkanlar"): games released in the last days, found in a watched
publisher's catalogue or in the tracked charts (models/fct_mr_launches.sql).

Used by the digest (`launches` facts and section), the local page `/radar/yeni` of
`pulse serve` and `pulse radar launches` (list, or the shareable deck with --share).

Only games the radar sees are listed: those that reach a tracked Apple chart or belong to a
watched publisher (radar.publishers, radar.publisher_preset). A game released only on Google
Play, or by a publisher nobody watches and outside the charts, is not seen.

Everything here that came from a store (names, developers, URLs) is store data: links are
rebuilt from validated ids, and image URLs are kept only when they are https URLs on Apple's
image CDN (apple.image_url), so a page or a deck can show them with a strict policy.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import duckdb

from .apple import image_url
from .digest import COUNTRY, LABELS, LIST, num
from .http import APPLE_IMAGE_HOSTS

DAY_OPTIONS = (7, 14, 30)
FOUND_VIA = ("both", "publisher", "chart")
APPLE_ID = re.compile(r"^[0-9]{5,12}$")
STOREFRONT = re.compile(r"^[a-z]{2}$")
PLAY_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
IMG_ORIGINS = tuple(f"https://{h}" for h in sorted(APPLE_IMAGE_HOSTS))

T = {
    "tr": {
        "title": "Yeni çıkanlar",
        "section": "Yeni çıkanlar (son {d} gün)",
        "sub": (
            "Son günlerde çıkan oyunlar: izlenen yayıncıların kataloglarında ya da radarın "
            "takip ettiği listelerde görülenler. Yalnızca bu listelere giren ya da izlenen "
            "yayıncılara ait oyunlar görünür; yalnız Google Play'de çıkanlar görünmez."
        ),
        "intro": (
            "{n} oyun, {start} – {end} arasında çıktı ({p} izlenen yayıncının kataloğu ve "
            "takip edilen listeler)."
        ),
        "none": "Bu pencerede yeni oyun görülmedi.",
        "more": "… ve {n} oyun daha (tamamı ambarda, fct_mr_launches).",
        "released": "çıkış",
        "best": "en iyi sıra",
        "mechanic": "mekanik",
        "via": {"both": "yayıncı + liste", "publisher": "izlenen yayıncı", "chart": "liste"},
        "via_long": {
            "both": "İzlenen yayıncının kataloğunda ve listelerde",
            "publisher": "İzlenen yayıncının kataloğunda",
            "chart": "Takip edilen bir listede",
        },
        "days": "Pencere",
        "days_opt": "Son {d} gün",
        "mech_filter": "Mekanik",
        "all": "Tümü",
        "apply": "Uygula",
        "reset": "Sıfırla",
        "results": "{n} oyun",
        "store": "Mağazada aç",
        "first_entry": "listeye ilk giriş",
        "age": "{d} günlük",
        "back": "Pazar radarı",
        "local": "Yerel sayfa: mağaza verisi bu bilgisayarda kalır; görseller Apple'ın sunucusundan yüklenir.",
        "empty": (
            "Henüz yeni çıkan yok. Radar her günlük yenilemede listeleri ve izlenen "
            "yayıncıları okur (pulse radar run); hazır yayıncı listesi için pulse.yaml'a "
            "radar.publisher_preset: casual ekleyin."
        ),
        "share": "Paylaşılabilir haftalık sunum: pulse radar launches --days 7 --share",
        "watched": "{n} yayıncı izleniyor",
        "no_tags": "etiket yok",
    },
    "en": {
        "title": "New launches",
        "section": "New launches (last {d} days)",
        "sub": (
            "Games released in the last days: seen in a watched publisher's catalogue or in "
            "a chart the radar tracks. Only games that reach those charts or belong to a "
            "watched publisher are seen; Google Play-only launches are not."
        ),
        "intro": (
            "{n} games released between {start} and {end} ({p} watched publishers' catalogues "
            "and the tracked charts)."
        ),
        "none": "No new game seen in this window.",
        "more": "… and {n} more (all in the warehouse, fct_mr_launches).",
        "released": "released",
        "best": "best rank",
        "mechanic": "mechanic",
        "via": {"both": "publisher + chart", "publisher": "watched publisher", "chart": "chart"},
        "via_long": {
            "both": "In a watched publisher's catalogue and in the charts",
            "publisher": "In a watched publisher's catalogue",
            "chart": "In a tracked chart",
        },
        "days": "Window",
        "days_opt": "Last {d} days",
        "mech_filter": "Mechanic",
        "all": "All",
        "apply": "Apply",
        "reset": "Reset",
        "results": "{n} games",
        "store": "Open in store",
        "first_entry": "first chart entry",
        "age": "{d} days old",
        "back": "Market radar",
        "local": "Local page: store data stays on this computer; images load from Apple's servers.",
        "empty": (
            "No new launches yet. The radar reads the charts and the watched publishers at "
            "every daily refresh (pulse radar run); for a ready-made publisher list add "
            "radar.publisher_preset: casual to pulse.yaml."
        ),
        "share": "Shareable weekly deck: pulse radar launches --days 7 --share",
        "watched": "{n} publishers watched",
        "no_tags": "no tags",
    },
}


def window_start(day: dt.date, days: int) -> dt.date:
    """The first day of a `days`-day window that ends on `day` (both days included)."""
    return day - dt.timedelta(days=int(days) - 1)


def latest_day(con: duckdb.DuckDBPyConnection) -> dt.date | None:
    """The newest day the radar observed anything (a chart list or a publisher catalogue)."""
    best = None
    for sql in (
        "SELECT MAX(snapshot_date) FROM stg_mr_list_days",
        "SELECT MAX(snapshot_date) FROM raw_mr_app WHERE source = 'publisher'",
    ):
        try:
            d = con.execute(sql).fetchone()[0]
        except duckdb.Error:  # a warehouse without the radar tables
            continue
        if d is not None and (best is None or d > best):
            best = d
    return best


def recent(con: duckdb.DuckDBPyConnection, day: dt.date, days: int) -> list[dict[str, Any]]:
    """Games released in the `days` calendar days up to and including `day` (age 0 to
    days - 1), one row each: the newest snapshot that listed it, so a window longer than
    launch_days still lists a game the radar saw while it was new (its `age_days` is
    recomputed for `day`). Newest release first, then publisher + chart, then best rank."""
    try:
        cur = con.execute(
            """
            SELECT * FROM fct_mr_launches
            WHERE snapshot_date <= ? AND snapshot_date > ? - ?::INTEGER
              AND release_date <= ? AND release_date > ? - ?::INTEGER
            QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC) = 1
            ORDER BY release_date DESC, found_via = 'both' DESC, best_rank NULLS LAST,
                     name, app_id
            """,
            [day, day, int(days), day, day, int(days)],
        )
    except duckdb.Error:  # no radar run yet: the model does not exist
        return []
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    for r in rows:
        if isinstance(r.get("release_date"), dt.date):
            r["age_days"] = (day - r["release_date"]).days
    return rows


def screenshots(row: dict[str, Any], n: int = 2) -> list[str]:
    try:
        urls = json.loads(row.get("screenshot_urls") or "[]")
    except ValueError:
        return []
    return [u for u in (image_url(x) for x in urls if isinstance(x, str)) if u][:n]


def store_link(app_id: Any, country: Any) -> str | None:
    aid, cc = str(app_id or ""), str(country or "us").lower()
    if not APPLE_ID.match(aid):
        return None
    return f"https://apps.apple.com/{cc if STOREFRONT.match(cc) else 'us'}/app/id{aid}"


def play_link(play_id: Any) -> str | None:
    pid = str(play_id or "")
    if not PLAY_ID.match(pid) or len(pid) > 150:
        return None
    return f"https://play.google.com/store/apps/details?id={pid}"


def watched_names(publishers: list[dict[str, str]]) -> dict[str, str]:
    return {str(p["id"]): str(p.get("name") or p["id"]) for p in publishers}


def best_text(row: dict[str, Any], lang: str) -> str | None:
    if row.get("best_rank") is None or not row.get("best_list"):
        return None
    cc, _, lst = str(row["best_list"]).partition(":")
    return (
        f"{COUNTRY[lang].get(cc, cc.upper())} {LIST[lang].get(lst, lst)} #{int(row['best_rank'])}"
    )


def view(row: dict[str, Any], lang: str, watched: dict[str, str]) -> dict[str, Any]:
    """Display values for one game (labels in the page language, validated links)."""
    name = str(row.get("name") or row.get("app_id") or "?")
    dev_id = str(row.get("developer_id") or "")
    developer = str(row.get("developer") or "")
    via = row.get("found_via") if row.get("found_via") in FOUND_VIA else "chart"
    release = row.get("release_date")
    return {
        "app_id": str(row.get("app_id") or ""),
        "name": name,
        "publisher": watched.get(dev_id) or developer,
        "developer": developer,
        "watched": dev_id in watched,
        "release": release.isoformat() if isinstance(release, dt.date) else str(release or ""),
        "age": row.get("age_days"),
        "via": via,
        "via_label": T[lang]["via"][via],
        "icon": image_url(row.get("icon_url")),
        "initials": "".join(w[0] for w in name.split()[:2] if w).upper() or "?",
        "shots": screenshots(row),
        "store_url": store_link(
            row.get("app_id"), row.get("store_country") or row.get("best_country") or "us"
        ),
        "play_url": play_link(row.get("play_id")),
        "best": best_text(row, lang),
        "countries": [str(c) for c in row.get("countries") or []],
        "first_entry": bool(row.get("first_chart_entry")),
        "mechanics": [str(m) for m in row.get("mechanics") or []],
        "mechanic_primary": row.get("mechanic_primary"),
        "tag_source": row.get("tag_source"),
    }


def facts_section(
    con: duckdb.DuckDBPyConnection, day: dt.date, days: int, publishers: list[dict[str, str]]
) -> dict[str, Any]:
    """The `launches` block of the facts JSON (every number carries a basis)."""
    rows = recent(con, day, days)
    watched = watched_names(publishers)
    counts = {k: sum(1 for r in rows if r["found_via"] == k) for k in FOUND_VIA}
    items = []
    for r in rows:
        v = view(r, "en", watched)
        items.append(
            {
                "app_id": v["app_id"],
                "app_key": f"apple:{v['app_id']}",
                "name": v["name"],
                "developer": v["developer"],
                "publisher": watched.get(str(r.get("developer_id") or "")),
                "found_via": v["via"],
                "release": num(v["release"], "observed", source=r.get("release_source")),
                "age": num(r.get("age_days"), "observed"),
                "first_seen": r["first_seen"].isoformat() if r.get("first_seen") else None,
                "best_rank": (
                    {
                        "value": int(r["best_rank"]),
                        "basis": "observed",
                        "list": r["best_list"].split(":", 1)[1] if r.get("best_list") else None,
                        "country": r.get("best_country"),
                    }
                    if r.get("best_rank") is not None
                    else None
                ),
                "countries": v["countries"],
                "first_chart_entry": v["first_entry"],
                "mechanic_primary": v["mechanic_primary"],
                "mechanics": v["mechanics"],
                "tag_source": v["tag_source"],
                "store_url": v["store_url"],
            }
        )
    return {
        "window": {
            "days": int(days),
            "from": window_start(day, days).isoformat(),
            "to": day.isoformat(),
            "basis": "observed",
        },
        "count": num(len(rows), "observed"),
        "by_found_via": {k: num(v, "observed") for k, v in counts.items()},
        "watched_publishers": num(len(publishers), "observed"),
        "items": items,
    }


def render_md(facts: dict[str, Any], lang: str, limit: int = 40) -> list[str]:
    """The digest's new-launches section (empty list when the facts have none)."""
    lb = facts.get("launches")
    if not lb:
        return []
    t = T[lang]
    w = lb["window"]
    L = [f"## {t['section'].format(d=w['days'])}", ""]
    items = lb.get("items") or []
    if not items:
        return L + [t["none"], ""]
    L.append(
        t["intro"].format(
            n=lb["count"]["value"],
            start=w["from"],
            end=w["to"],
            p=lb["watched_publishers"]["value"],
        )
        + f" ({LABELS[lang]['observed']})"
    )
    L.append("")
    for it in items[:limit]:
        parts = [f"{t['released']} {it['release']['value']}"]
        parts.append(t["via"][it["found_via"]])
        br = it.get("best_rank")
        if br:
            cc = br.get("country") or ""
            parts.append(
                f"{t['best']}: {COUNTRY[lang].get(cc, cc.upper())} "
                f"{LIST[lang].get(br.get('list') or '', br.get('list') or '')} #{br['value']}"
            )
        if it.get("mechanics"):
            parts.append(f"{t['mechanic']}: {', '.join(it['mechanics'])}")
        who = it.get("publisher") or it.get("developer") or "-"
        L.append(f"- {it['name']} ({who}): " + " · ".join(parts) + f" ({LABELS[lang]['observed']})")
    if len(items) > limit:
        L.append(t["more"].format(n=len(items) - limit))
    L.append("")
    return L
