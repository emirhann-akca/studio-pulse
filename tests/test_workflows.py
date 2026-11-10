"""The Pages workflow publishes the demo as the site root; the refresh template stays inert."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"


def test_pages_deploys_the_demo_dashboard_as_site_root():
    wf = yaml.safe_load((WORKFLOWS / "pages.yml").read_text())
    steps = wf["jobs"]["deploy"]["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert any(u.startswith("actions/configure-pages@") for u in uses)
    assert any(u.startswith("actions/deploy-pages@") for u in uses)
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-pages-artifact@"))
    assert upload["with"]["path"] == "docs/demo"
    assert (WORKFLOWS.parents[1] / "docs" / "demo" / "index.html").exists()


def test_refresh_template_has_no_schedule_and_is_gated():
    wf = yaml.safe_load((WORKFLOWS / "refresh.example.yml").read_text())
    triggers = wf[True]  # PyYAML reads the bare key `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    job = wf["jobs"]["refresh"]
    assert "vars.PULSE_REFRESH_ENABLED" in job["if"]
    assert any("secrets." in str(s.get("with", "")) for s in job["steps"])
