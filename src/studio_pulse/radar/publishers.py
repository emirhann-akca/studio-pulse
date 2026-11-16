"""Curated publisher watch lists (radar/publishers.yaml), opt-in with `radar.publisher_preset`.

Every entry is an App Store artistId that was checked against Apple's public Lookup API (the
file says how). The loader refuses a malformed entry, so a typo in the file fails the config
instead of watching the wrong catalogue.
"""

from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

ARTIST_ID = re.compile(r"^[1-9][0-9]{4,11}$")


class PresetError(ValueError):
    pass


def parse(text: str) -> dict[str, dict[str, Any]]:
    """{preset name: {title, publishers: [{id, name, artist_name, verified_on}]}}."""
    doc = yaml.safe_load(text) or {}
    if doc.get("version") != 1:
        raise PresetError("publishers.yaml: version must be 1")
    out: dict[str, dict[str, Any]] = {}
    for key, preset in (doc.get("presets") or {}).items():
        pubs = []
        seen: set[str] = set()
        for p in (preset or {}).get("publishers") or []:
            pid = str(p.get("id") or "")
            if not ARTIST_ID.match(pid):
                raise PresetError(f"publishers.yaml {key}: {pid!r} is not an App Store artistId")
            if pid in seen:
                raise PresetError(f"publishers.yaml {key}: {pid} is listed twice")
            seen.add(pid)
            name, artist = p.get("name"), p.get("artist_name")
            if not isinstance(name, str) or not name.strip():
                raise PresetError(f"publishers.yaml {key}: {pid} has no name")
            if not isinstance(artist, str) or not artist.strip():
                raise PresetError(f"publishers.yaml {key}: {pid} has no artist_name")
            day = p.get("verified_on")
            if not isinstance(day, dt.date):
                raise PresetError(f"publishers.yaml {key}: {pid} has no verified_on date")
            pubs.append({"id": pid, "name": name.strip(), "artist_name": artist.strip(),
                         "verified_on": day})  # fmt: skip
        if not pubs:
            raise PresetError(f"publishers.yaml {key}: no publishers")
        out[str(key)] = {"title": str((preset or {}).get("title") or key), "publishers": pubs}
    return out


@lru_cache(maxsize=1)
def presets() -> dict[str, dict[str, Any]]:
    text = resources.files("studio_pulse.radar").joinpath("publishers.yaml").read_text("utf-8")
    return parse(text)


def preset_publishers(names: list[str]) -> list[dict[str, str]]:
    """The publishers of the named presets, in file order and without repeats ({id, name})."""
    known = presets()
    unknown = [n for n in names if n not in known]
    if unknown:
        raise PresetError(
            f"unknown publisher preset(s) {unknown} (known: {', '.join(sorted(known))})"
        )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for n in names:
        for p in known[n]["publishers"]:
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append({"id": p["id"], "name": p["name"]})
    return out
