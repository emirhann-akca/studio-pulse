"""Changes to campaigns and creatives, as dated events for the ad centre's markers and summary.

Two sources, one shape (creative-forge's `forge snapshot` timeline, schema creative-forge/snapshot/v1):

* creative-forge's audit log, read through `forge snapshot --json`: campaigns created paused, switched on, paused
  (by hand, automatically or by a safety rule), budgets changed outside forge, manual upload packs, refusals. Each
  event carries its audit record's hash. A campaign belongs to a game when its store id (package / App Store id)
  is one of the ids that game has in the config.
* files listed under `reklam: {changes: [...]}` in the config, `{"app": <id>, "events": [...]}`: the demo writes
  its fictional changes this way, and any other tool can too.

An event's effect day is the day its numbers can first show it: a change recorded before noon (UTC) counts from
that day, a later one from the next day ("dün akşam değiştirdik" shows up in today's numbers).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

TEXT: dict[str, dict[str, str]] = {
    "tr": {
        "activated": "{campaign} yayına alındı",
        "created_paused": "{campaign} oluşturuldu (duraklatılmış)",
        "paused": "{campaign} durduruldu",
        "auto_paused": "{campaign}: otomatik durdurma ({target})",
        "safety_paused": "{campaign} güvenlik kuralıyla durduruldu",
        "creative_added": "{campaign}: yeni kreatif {creatives}",
        "creative_paused": "{campaign}: kreatif durduruldu {creatives}",
        "budget_changed": "{campaign}: günlük bütçe {before} → {after}",
        "manual_pack": "Elle yükleme paketi hazırlandı",
        "stopped": "{campaign}: oluşturma durdu",
        "refused": "{campaign}: yayına alma reddedildi",
        "status_drift": "{campaign}: ağdaki durum değişti ({network})",
    },
    "en": {
        "activated": "{campaign} switched on",
        "created_paused": "{campaign} created (paused)",
        "paused": "{campaign} paused",
        "auto_paused": "{campaign}: paused automatically ({target})",
        "safety_paused": "{campaign} paused by a safety rule",
        "creative_added": "{campaign}: new creative {creatives}",
        "creative_paused": "{campaign}: creative paused {creatives}",
        "budget_changed": "{campaign}: daily budget {before} → {after}",
        "manual_pack": "Manual upload pack written",
        "stopped": "{campaign}: creation stopped",
        "refused": "{campaign}: activation refused",
        "status_drift": "{campaign}: status changed on the network ({network})",
    },
}
# changes that can move delivery numbers (the summary compares before and after these)
MOVERS = (
    "activated",
    "paused",
    "auto_paused",
    "safety_paused",
    "creative_added",
    "creative_paused",
    "budget_changed",
)


def parse_at(at: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def effect_day(at: Any) -> dt.date | None:
    t = parse_at(at)
    if t is None:
        return None
    t = t.astimezone(dt.UTC)
    return t.date() if t.hour < 12 else t.date() + dt.timedelta(days=1)


def from_files(cfg: Any, app_id: str) -> list[dict[str, Any]]:
    out = []
    base = Path(getattr(cfg, "base_dir", None) or ".")
    for f in ((getattr(cfg, "raw", None) or {}).get("reklam") or {}).get("changes") or []:
        p = Path(str(f)).expanduser()
        p = p if p.is_absolute() else base / p
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("app") != app_id:
            continue
        for e in data.get("events") or []:
            if isinstance(e, dict) and e.get("kind") and e.get("at"):
                out.append({**e, "source": "demo" if data.get("demo") else "file"})
    return out


def from_forge(snap: dict[str, Any] | None, ids: set[str]) -> list[dict[str, Any]]:
    if not snap:
        return []
    return [
        {**e, "source": "forge"} for e in snap.get("timeline") or [] if e.get("store_id") in ids
    ]


def load(cfg: Any, app_id: str, snap: dict[str, Any] | None, ids: set[str]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for e in from_files(cfg, app_id) + from_forge(snap, ids):
        key = e.get("hash") or (e.get("at"), e.get("kind"), e.get("campaign"))
        if key in seen:
            continue
        seen.add(key)
        e = dict(e)
        e["day"] = effect_day(e.get("at"))
        if e["day"] is None:
            continue
        out.append(e)
    out.sort(key=lambda e: (str(e.get("at")), str(e.get("campaign"))))
    return out


def short(name: Any, n: int = 42) -> str:
    """A creative-forge ad name as concept/hook · format · language; anything else cut to n characters."""
    from ..connectors.ua.creative import parse_cf1

    s = str(name or "")
    g = parse_cf1(s)
    if g:
        parts = [g["concept"]] + [g[k] for k in ("hook", "segment", "end_card") if g[k] != "main"]
        return f"{'/'.join(parts)} · {g['format']} · {g['language']}"
    return s if len(s) <= n else s[: n - 1] + "…"


def text(e: dict[str, Any], lang: str, money: Any, quote: Any = None) -> str:
    """One line per change. `quote` wraps names from data (the AI summary quotes them as untrusted data)."""
    q = quote or (lambda s: s)
    t = TEXT.get(lang, TEXT["en"]).get(str(e.get("kind")))
    d = e.get("detail") or {}
    camp = (
        e.get("campaign_name") or e.get("campaign") or ("kampanya" if lang == "tr" else "campaign")
    )
    creatives = ", ".join(q(short(c)) for c in (d.get("creatives") or [])[:2])
    if t is None:
        return f"{q(short(camp, 36))}: {e.get('kind')}"
    return t.format(
        campaign=q(short(camp, 36)),
        creatives=creatives,
        before=money(d.get("before")),
        after=money(d.get("after")),
        target=q(short(d.get("target") or "", 30)),
        network=q(str(d.get("network") or "")),
    )
