"""Fail (with an annotation) unless the built wheel carries everything a user install needs."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

NEED = [
    "studio_pulse/_models/fct_app_daily.sql",
    "studio_pulse/_defaults/alerts.yaml",
    "studio_pulse/_defaults/checks.yaml",
    "studio_pulse/raw_schema.sql",
    "studio_pulse/serve/static/htmx.min.js",
    "studio_pulse/serve/static/htmx.LICENSE",
    "studio_pulse/serve/static/pulse.js",
    "studio_pulse/serve/templates/base.html",
    "studio_pulse/desktop_assets/studio-pulse.ico",  # Windows shortcut icon
    "studio_pulse/desktop_assets/studio-pulse.icns",  # macOS app bundle icon
    "studio_pulse/desktop_assets/studio-pulse.png",  # Linux .desktop icon
    "studio_pulse/serve/templates/live.html",
    "studio_pulse/contract/pulse_v2.json",  # the collector validates with it
    "studio_pulse/_models/stg_pulse_events.sql",
    "studio_pulse/_models/stg_cohort_dims.sql",
    "studio_pulse/brain/metrics.yaml",
    "studio_pulse/brain/verified_queries.yaml",
    "studio_pulse/brain/eval/golden.yaml",
    "studio_pulse/brain/templates/insights.html",
    "studio_pulse/serve/templates/radar.html",
    "studio_pulse/radar/schema.sql",
    "studio_pulse/radar/models/fct_mr_breakout_daily.sql",
    "studio_pulse/radar/taxonomy.yaml",
    "studio_pulse/radar/checks.yaml",
    "studio_pulse/radar/estimate/ios_android.yaml",
    "studio_pulse/radar/estimate/revenue_anchors.yaml",
    "studio_pulse/radar/estimate/revenue_per_download.yaml",
    "studio_pulse/radar/estimate/models/stg_mr_rank_input.sql",
    "studio_pulse/radar/estimate/schema.sql",
    "studio_pulse/radar/estimate/checks.yaml",
    "studio_pulse/radar/estimate/models/stg_mr_play_counter.sql",
    "studio_pulse/serve/templates/radar_game.html",
]


def main(dist: Path) -> int:
    wheels = sorted(dist.glob("*.whl"))
    if not wheels:
        print(f"::error title=wheel contents::no wheel in {dist}")
        return 1
    whl = zipfile.ZipFile(wheels[0])
    names = set(whl.namelist())
    missing = [n for n in NEED if n not in names]
    ep = next((n for n in names if n.endswith("entry_points.txt")), None)
    eps = whl.read(ep).decode("utf-8") if ep else ""
    for section, script in (("[console_scripts]", "pulse ="), ("[gui_scripts]", "pulse-bg =")):
        if section not in eps or script not in eps:
            missing.append(f"{section} {script.rstrip(' =')}")
    meta = whl.read(next(n for n in names if n.endswith("METADATA"))).decode("utf-8")
    if "Requires-Dist: tzdata" not in meta or "win32" not in meta:
        missing.append("tzdata dependency for Windows")
    if missing:
        print(f"::error title=wheel contents ({wheels[0].name})::missing " + ", ".join(missing))
        return 1
    print(f"{wheels[0].name}: {len(names)} files, entry points and package data present")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
