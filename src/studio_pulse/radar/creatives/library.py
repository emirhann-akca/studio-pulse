"""Reads for the page and the CLI: creatives with their newest tags and scores, filters, the
summary strip, one creative's detail with similar ones, and the Ad Center brief link."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from importlib import resources
from typing import Any
from urllib.parse import urlencode

import duckdb

from . import google, meta, score, tiktok, youtube
from . import labels as L
from .settings import CreativeSettings

FILTERS = ("genre", "advertiser", "hook", "ai", "format", "region", "source", "min_score",
           "active")  # fmt: skip
MAX_CARDS = 60


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    sql = (
        resources.files("studio_pulse.radar.creatives")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    for stmt in body.split(";"):
        if stmt.strip():
            con.execute(stmt)


_SELECT = """
SELECT c.*, t.tag_source, t.hook, t.hook_confidence, t.ai_likelihood, t.ai_signals,
       t.orientation, t.duration, t.mechanic, t.cta, t.language AS tag_language, t.kind,
       t.why_tr, t.why_en, t.evidence, t.rejected, t.model, t.tagged_at, t.input_hash
FROM mr_creative c
LEFT JOIN (
  SELECT * FROM mr_creative_tag
  QUALIFY ROW_NUMBER() OVER (PARTITION BY creative_key ORDER BY tagged_at DESC) = 1
) t USING (creative_key)
"""


def _rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    try:
        cur = con.execute(_SELECT)
    except duckdb.CatalogException:
        return []
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r, strict=False))
        for k in ("evidence", "rejected"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else ({} if k == "evidence" else [])
            except ValueError:
                d[k] = {} if k == "evidence" else []
        out.append(d)
    return out


def load(
    con: duckdb.DuckDBPyConnection, s: CreativeSettings, as_of: dt.date
) -> list[dict[str, Any]]:
    """Every creative with its newest tag, cluster, score parts and score."""
    rows = score.score_rows(_rows(con), as_of=as_of, active_days=s.active_days, weights=s.weights)
    for r in rows:
        r["links"] = links(r)
        r["is_active"] = r["parts"]["active"]["signal"] is not None and (
            r["parts"]["active"]["signal"] <= s.active_days
        )
    rows.sort(key=lambda r: (-r["score"], str(r["creative_key"])))
    return rows


def links(r: dict[str, Any]) -> dict[str, str | None]:
    """Outbound links and media, rebuilt from validated ids only (never a stored URL)."""
    src, cid = r.get("source"), str(r.get("creative_id") or "")
    out: dict[str, str | None] = {"source": None, "embed": None, "thumb": None}
    if src == "google_atc":
        out["source"] = google.page_url(str(r.get("advertiser_id") or ""), cid)
    elif src == "meta":
        out["source"] = meta.library_url(cid)
    elif src == "tiktok":
        out["source"] = tiktok.library_url(cid)
    vid = str(r.get("youtube_id") or "")
    if youtube.embed_url(vid):  # a YouTube row, or a YouTube video attached by hand
        out.update(embed=youtube.embed_url(vid), thumb=youtube.thumb_url(vid))
        if src == "youtube":
            out["source"] = youtube.watch_url(vid)
        else:
            out["youtube"] = youtube.watch_url(vid)
    return out


def clean_filters(raw: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Only values the page offers survive: nothing a visitor typed is ever echoed back."""
    opts = options(rows)
    f: dict[str, Any] = {}
    for k in ("genre", "advertiser", "hook", "ai", "format", "region", "source"):
        v = raw.get(k)
        if v and v in {o[0] for o in opts[k]}:
            f[k] = v
    try:
        ms = int(str(raw.get("min_score") or "0"))
        if ms in (20, 35, 50, 60, 75):
            f["min_score"] = ms
    except ValueError:
        pass
    if str(raw.get("active") or "") in ("1", "on", "true"):
        f["active"] = True
    return f


def options(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, int]]]:
    def count(key: Any) -> list[tuple[str, int]]:
        c = Counter(v for r in rows for v in key(r) if v)
        return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "genre": count(lambda r: [r.get("genre")]),
        "advertiser": count(lambda r: [r.get("advertiser_key")]),
        "hook": count(lambda r: [r.get("hook") or "unknown"]),
        "ai": count(lambda r: [r.get("ai_likelihood") or "unknown"]),
        "format": count(lambda r: [r.get("format")]),
        "region": count(lambda r: list(r.get("regions") or [])),
        "source": count(lambda r: [r.get("source")]),
    }


def apply(rows: list[dict[str, Any]], f: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if "genre" in f and r.get("genre") != f["genre"]:
            continue
        if "advertiser" in f and r.get("advertiser_key") != f["advertiser"]:
            continue
        if "hook" in f and (r.get("hook") or "unknown") != f["hook"]:
            continue
        if "ai" in f and (r.get("ai_likelihood") or "unknown") != f["ai"]:
            continue
        if "format" in f and r.get("format") != f["format"]:
            continue
        if "region" in f and f["region"] not in (r.get("regions") or []):
            continue
        if "source" in f and r.get("source") != f["source"]:
            continue
        if "min_score" in f and r["score"] < f["min_score"]:
            continue
        if f.get("active") and not r["is_active"]:
            continue
        out.append(r)
    return out


def summary(rows: list[dict[str, Any]], s: CreativeSettings, as_of: dt.date) -> dict[str, Any]:
    """This month in our genres: top hooks, the AI-likelihood share, the most active advertisers."""
    since = as_of - dt.timedelta(days=30)
    month = [
        r
        for r in rows
        if r.get("genre") in s.genres and r.get("last_shown") and r["last_shown"] >= since
    ]
    tagged = [r for r in month if (r.get("hook") or "unknown") not in ("unknown",)]
    hooks = Counter(r["hook"] for r in tagged)
    videos = [r for r in month if r.get("tag_source") == "llm_video"]
    ai = sum(1 for r in videos if r.get("ai_likelihood") in ("medium", "high"))
    act = Counter(r["advertiser_key"] for r in month if r["is_active"])
    names = {r["advertiser_key"]: r.get("advertiser_name") for r in month}
    return {
        "creatives": len(month),
        "tagged": len(tagged),
        "top_hooks": [
            {"hook": h, "n": n, "share": n / len(tagged)} for h, n in hooks.most_common(5)
        ],
        "ai": {"n": ai, "of": len(videos), "share": (ai / len(videos)) if videos else None},
        "top_advertisers": [
            {"key": k, "name": names.get(k) or k, "active": n} for k, n in act.most_common(5)
        ],
    }


def find(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    return next((r for r in rows if r["creative_key"] == key), None)


def similar(rows: list[dict[str, Any]], r: dict[str, Any], n: int = 6) -> list[dict[str, Any]]:
    """The same concept first (advertiser + hook + mechanic), then the same hook and
    mechanic at other advertisers, then the same hook in the same genre."""
    me = r["creative_key"]
    out: list[dict[str, Any]] = []

    def add(pred: Any) -> None:
        for x in rows:
            if len(out) >= n:
                return
            if x["creative_key"] != me and x not in out and pred(x):
                out.append(x)

    hook = r.get("hook")
    if hook and hook != "unknown":
        if r.get("cluster"):
            add(lambda x: x.get("cluster") == r["cluster"])
        if r.get("mechanic"):
            add(lambda x: x.get("hook") == hook and x.get("mechanic") == r["mechanic"])
        add(lambda x: x.get("hook") == hook and x.get("genre") == r.get("genre"))
    return out


def signal_reason(r: dict[str, Any], lang: str, fmt_num: Any = str) -> str | None:
    """When no grounded LLM reason exists: what the public signals say about the advertiser's
    behaviour (built from the score's own inputs, never from a model)."""
    p = r.get("parts") or {}
    bits: list[str] = []
    days = (p.get("longevity") or {}).get("signal")
    ago = (p.get("active") or {}).get("signal")
    spread = (p.get("spread") or {}).get("signal") or {}
    vol = p.get("volume") or {}
    tr = lang == "tr"
    if days and days >= 60:
        bits.append(f"{days} gündür yayında" if tr else f"live for {days} days")
    if ago is not None and r.get("is_active"):
        bits.append("hâlâ gösteriliyor" if tr else "still running")
    if spread.get("regions", 0) >= 5:
        n = spread["regions"]
        bits.append(f"{n} bölgede" if tr else f"in {n} regions")
    if vol.get("signal"):
        n = fmt_num(vol["signal"])
        if vol.get("kind") == "times_shown":
            bits.append(f"en az {n} gösterim" if tr else f"at least {n} impressions")
        else:
            bits.append(f"AB erişimi {n}" if tr else f"EU reach {n}")
    if len(bits) < 2:
        return None
    tail = (
        ": reklamveren bu kreatife bütçe vermeyi sürdürüyor."
        if tr
        else ": the advertiser keeps paying for this creative."
    )
    text = ", ".join(bits)
    return text[0].upper() + text[1:] + tail


def brief_link(r: dict[str, Any], lang: str) -> str:
    """/reklam?brief=... for the Ad Center wizard (another page; coordinated by URL only).
    Parameters: brief (text), hook, mechanic, orientation, genre, ref (creative key), lang."""
    hook = r.get("hook") if r.get("hook") not in (None, "unknown") else None
    parts = []
    if hook:
        parts.append(("Kanca: " if lang == "tr" else "Hook: ") + L.name(L.HOOKS, hook, lang))
    if r.get("mechanic"):
        from ...serve.ui import mechanic as mech_name

        parts.append(
            ("Mekanik: " if lang == "tr" else "Mechanic: ") + mech_name(r["mechanic"], lang)
        )
    if r.get("orientation") not in (None, "unknown"):
        parts.append(("Yön: " if lang == "tr" else "Orientation: ") + str(r["orientation"]))
    why = r.get("why_tr") if lang == "tr" else r.get("why_en")
    if why:
        parts.append(("Neden: " if lang == "tr" else "Why: ") + str(why))
    q = {
        "brief": "; ".join(parts)[:400]
        or ("Rakip kreatif" if lang == "tr" else "Competitor creative"),
        "ref": r["creative_key"],
        "lang": lang,
    }
    if hook:
        q["hook"] = hook
    if r.get("mechanic"):
        q["mechanic"] = r["mechanic"]
    if r.get("orientation") not in (None, "unknown"):
        q["orientation"] = r["orientation"]
    if r.get("genre"):
        q["genre"] = r["genre"]
    return "/reklam?" + urlencode(q)


def runs(con: duckdb.DuckDBPyConnection, limit: int = 8) -> list[dict[str, Any]]:
    try:
        cur = con.execute(
            "SELECT run_at, source, status, estimated_bytes, billed_bytes, rows, cost_usd "
            "FROM mr_creative_run ORDER BY run_at DESC LIMIT ?",
            [limit],
        )
    except duckdb.CatalogException:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def to_json(r: dict[str, Any]) -> dict[str, Any]:
    """A creative for --json: dates as text, no internal fields."""
    keep = (
        "creative_key", "source", "creative_id", "advertiser_key", "advertiser_name", "genre",
        "format", "regions", "first_shown", "last_shown", "times_shown_min", "eu_reach",
        "reach_bucket", "platforms", "languages", "youtube_id", "tag_source", "hook",
        "hook_confidence", "ai_likelihood", "ai_signals", "orientation", "duration", "mechanic",
        "cta", "why_tr", "why_en", "evidence", "cluster", "variants", "score", "parts", "links",
        "is_active",
    )  # fmt: skip
    out = {k: r.get(k) for k in keep}
    for k in ("first_shown", "last_shown"):
        if out[k]:
            out[k] = out[k].isoformat()
    out["basis"] = "estimate"
    return out
