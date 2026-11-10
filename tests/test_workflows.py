"""The Pages workflow publishes the demo as the site root; the refresh template stays inert."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"


def test_pages_deploys_the_demo_dashboard_as_site_root():
    wf = yaml.safe_load((WORKFLOWS / "pages.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["deploy"]["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert any(u.startswith("actions/configure-pages@") for u in uses)
    assert any(u.startswith("actions/deploy-pages@") for u in uses)
    upload = next(
        s for s in steps if s.get("uses", "").startswith("actions/upload-pages-artifact@")
    )
    assert upload["with"]["path"] == "docs/demo"
    assert (WORKFLOWS.parents[1] / "docs" / "demo" / "index.html").exists()


def test_refresh_template_has_no_schedule_and_is_gated():
    wf = yaml.safe_load((WORKFLOWS / "refresh.example.yml").read_text(encoding="utf-8"))
    triggers = wf[True]  # PyYAML reads the bare key `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    job = wf["jobs"]["refresh"]
    assert "vars.PULSE_REFRESH_ENABLED" in job["if"]
    assert any("secrets." in str(s.get("with", "")) for s in job["steps"])


def _wf(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_every_action_is_pinned_to_a_commit():
    import re

    for f in ("ci.yml", "release.yml"):
        for job in _wf(f)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_ci_matrix_covers_three_oses_and_three_pythons():
    jobs = _wf("ci.yml")["jobs"]
    m = jobs["tests"]["strategy"]["matrix"]
    assert set(m["os"]) == {"windows-latest", "macos-latest", "ubuntu-latest"}
    assert set(m["python"]) == {"3.11", "3.12", "3.13"}
    run = " ".join(s.get("run", "") for s in jobs["tests"]["steps"])
    assert "-X warn_default_encoding" in run and "error::EncodingWarning" in run
    assert "fingerprint" in run
    assert set(jobs["user-install"]["strategy"]["matrix"]["os"]) == set(m["os"])
    assert jobs["cross-os-hash"]["needs"] == "tests"


def test_user_install_runs_the_real_installers_in_a_turkish_path():
    steps = _wf("ci.yml")["jobs"]["user-install"]["steps"]
    text = " ".join(s.get("run", "") for s in steps)
    assert "sh install.sh" in text and "install.ps1 | Invoke-Expression" in text
    assert "[char]0x015E" in text  # the work directory has Turkish characters
    for cmd in ("pulse doctor", "pulse demo", "pulse check", "pulse serve --config"):
        assert cmd in text
    assert any(s.get("uses", "").startswith("actions/upload-artifact@") for s in steps)


def test_schedule_smoke_always_removes_what_it_registered():
    job = _wf("ci.yml")["jobs"]["schedule-smoke"]
    assert set(job["strategy"]["matrix"]["os"]) == {"windows-latest", "macos-latest"}
    remove = next(s for s in job["steps"] if "Remove" in s.get("name", ""))
    assert remove["if"] == "always()"
    assert "pulse schedule remove" in remove["run"] and "launchctl print" in remove["run"]
    assert "schtasks /query" in remove["run"]


def test_release_builds_on_version_tags_only():
    wf = _wf("release.yml")
    assert wf[True] == {"push": {"tags": ["v*"]}}
    text = " ".join(s.get("run", "") for j in wf["jobs"].values() for s in j["steps"])
    assert "uv build" in text and "gh release create" in text
