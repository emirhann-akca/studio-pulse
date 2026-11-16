"""The shareable new-launches deck (`pulse radar launches --share`): 1080x1350 slides.

One HTML file: a cover with the total and the date range, one slide per selected game (icon,
name, publisher, store, two screenshots) and closing slide(s) with the full list. Store text is
escaped (Jinja autoescape) and the file carries its own Content-Security-Policy: no scripts,
images only from Apple's image CDN. Images are not downloaded: the deck links Apple's URLs, so
they load when the file is opened (or printed) with a network connection.

When a Chrome, Edge, Brave or Chromium is installed (desktop.find_browser, PULSE_APP_BROWSER
picks one), the same file is printed to a PDF with a headless browser in a throwaway profile;
without one the PDF step is skipped and the HTML is still written.
"""

from __future__ import annotations

import collections
import datetime as dt
import subprocess
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import jinja2

from .launches import FOUND_VIA, IMG_ORIGINS, view, watched_names, window_start

MONTHS = {
    "tr": ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
LIST_PER_SLIDE = 30  # two columns of 15: long names wrap to two lines and still fit

T = {
    "tr": {
        "title": "Yeni çıkan mobil oyunlar",
        "kicker": "Yeni çıkanlar",
        "cover_what": "yeni mobil oyun",
        "via": {"both": "yayıncı + liste", "publisher": "izlenen yayıncı", "chart": "listede"},
        "released": "Çıkış",
        "best": "En iyi sıra",
        "no_shot": "ekran görüntüsü yok",
        "full_list": "Tam liste",
        "source": "Kaynak: App Store herkese açık verisi (takip edilen listeler ve izlenen yayıncı katalogları).",
        "source_short": "App Store herkese açık verisi",
        "limits": "Yalnız listelere giren ya da izlenen yayıncılara ait oyunlar.",
        "swipe": "Kaydır →",
    },
    "en": {
        "title": "New mobile game launches",
        "kicker": "New launches",
        "cover_what": "new mobile games",
        "via": {"both": "publisher + chart", "publisher": "watched publisher", "chart": "charted"},
        "released": "Released",
        "best": "Best rank",
        "no_shot": "no screenshot",
        "full_list": "Full list",
        "source": "Source: public App Store data (tracked charts and watched publishers' catalogues).",
        "source_short": "Public App Store data",
        "limits": "Only games that reach the tracked charts or belong to watched publishers.",
        "swipe": "Swipe →",
    },
}


def day_text(d: dt.date, lang: str, year: bool = True) -> str:
    m = MONTHS[lang][d.month - 1]
    s = f"{d.day} {m}" if lang == "tr" else f"{m} {d.day}"
    return f"{s} {d.year}" if year and lang == "tr" else (f"{s}, {d.year}" if year else s)


def range_text(start: dt.date, end: dt.date, lang: str) -> str:
    """'9–15 Kas 2026' / 'Nov 9–15, 2026'; months and years only where they differ."""
    if (start.year, start.month) == (end.year, end.month):
        if lang == "tr":
            return f"{start.day}–{end.day} {MONTHS['tr'][end.month - 1]} {end.year}"
        return f"{MONTHS['en'][end.month - 1]} {start.day}–{end.day}, {end.year}"
    return f"{day_text(start, lang, start.year != end.year)} – {day_text(end, lang)}"


def select(cards: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The games that get a slide of their own: found both ways first, then by best rank,
    then the newest release."""
    order = {"both": 0, "chart": 1, "publisher": 2}

    def key(c: dict[str, Any]) -> tuple:
        return (order.get(c["via"], 3), c["best_rank"] is None, c["best_rank"] or 0,
                -(c["release_ord"] or 0), c["name"].lower())  # fmt: skip

    return sorted(cards, key=key)[: max(0, limit)]


def build(
    rows: list[dict[str, Any]],
    *,
    day: dt.date,
    days: int,
    lang: str,
    publishers: list[dict[str, str]],
    limit: int = 20,
    mechanic_label: Callable[[str], str] | None = None,
) -> str:
    """The deck's HTML for the launches `rows` (launches.recent) of the window ending `day`."""
    lang = lang if lang in T else "tr"
    t = T[lang]
    label = mechanic_label or (lambda k: k)
    watched = watched_names(publishers)
    cards = []
    for r in rows:
        v = view(r, lang, watched)
        rel = r.get("release_date")
        v["release_ord"] = rel.toordinal() if isinstance(rel, dt.date) else None
        v["release_text"] = day_text(rel, lang, year=False) if isinstance(rel, dt.date) else ""
        v["best_rank"] = int(r["best_rank"]) if r.get("best_rank") is not None else None
        v["via_label"] = t["via"][v["via"]]
        v["mechanics"] = [label(m) for m in v["mechanics"]]
        v["store_text"] = (v["store_url"] or "").removeprefix("https://")
        cards.append(v)
    counts = {k: sum(1 for c in cards if c["via"] == k) for k in FOUND_VIA}
    mech = collections.Counter(m for r in rows for m in (r.get("mechanics") or []))
    top = [(label(m), n) for m, n in sorted(mech.items(), key=lambda kv: (-kv[1], kv[0]))[:4]]
    listed = sorted(cards, key=lambda c: (-(c["release_ord"] or 0), c["name"].lower()))
    pages = [
        {"start": i + 1, "items": listed[i : i + LIST_PER_SLIDE]}
        for i in range(0, len(listed), LIST_PER_SLIDE)
    ]
    env = jinja2.Environment(autoescape=True, undefined=jinja2.StrictUndefined)
    tpl = env.from_string(
        resources.files("studio_pulse.radar").joinpath("launches_deck.html").read_text("utf-8")
    )
    start = window_start(day, days)
    return tpl.render(
        lang=lang, t=t, total=len(cards), counts=counts, top_mechanics=top,
        range_text=range_text(start, day, lang), slides=select(cards, limit),
        list_pages=pages, mosaic=[c["icon"] for c in listed if c["icon"]][:8],
        img_origins=IMG_ORIGINS,
    )  # fmt: skip


def print_pdf(
    html_path: Path,
    pdf_path: Path,
    *,
    browser: str | None = None,
    run: Callable[..., Any] = subprocess.run,
    timeout_s: float = 180.0,
    log: Callable[[str], None] = print,
) -> Path | None:
    """Print `html_path` to `pdf_path` with a headless Chromium-family browser; None (and a
    note) when there is no browser or printing failed. The HTML is never affected."""
    if browser is None:
        from ..desktop import find_browser

        browser = find_browser()
    if not browser:
        log("PDF skipped: no Chrome, Edge, Brave or Chromium found (PULSE_APP_BROWSER names one)")
        return None
    pdf_path = Path(pdf_path)
    pdf_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="pulse-deck-") as profile:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            "--hide-scrollbars",
            "--no-pdf-header-footer",
            "--virtual-time-budget=20000",
            f"--print-to-pdf={pdf_path}",
            Path(html_path).resolve().as_uri(),
        ]
        try:
            res = run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout_s,
                      check=False)  # fmt: skip
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"PDF skipped: the browser could not print ({type(exc).__name__})")
            return None
    if getattr(res, "returncode", 1) != 0 or not pdf_path.exists() or pdf_path.stat().st_size == 0:
        log(f"PDF skipped: the browser exited {getattr(res, 'returncode', '?')} without a PDF")
        return None
    return pdf_path


def write_share(
    rows: list[dict[str, Any]],
    out_dir: Path,
    *,
    day: dt.date,
    days: int,
    lang: str,
    publishers: list[dict[str, str]],
    limit: int = 20,
    pdf: bool = True,
    mechanic_label: Callable[[str], str] | None = None,
    log: Callable[[str], None] = print,
    printer: Callable[..., Path | None] = print_pdf,
) -> dict[str, str]:
    """Write the deck (and its PDF when a browser can print it); returns the file paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = window_start(day, days)
    stem = f"launches_{start.isoformat()}_{day.isoformat()}.{lang}"
    html_path = out_dir / f"{stem}.html"
    html_path.write_text(
        build(rows, day=day, days=days, lang=lang, publishers=publishers, limit=limit,
              mechanic_label=mechanic_label),
        encoding="utf-8",
    )  # fmt: skip
    files = {"html": str(html_path)}
    if pdf:
        got = printer(html_path, out_dir / f"{stem}.pdf", log=log)
        if got:
            files["pdf"] = str(got)
    return files
