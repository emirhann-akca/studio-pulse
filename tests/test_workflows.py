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


def test_user_install_runs_the_readme_one_liners_in_a_turkish_path():
    steps = _wf("ci.yml")["jobs"]["user-install"]["steps"]
    text = " ".join(s.get("run", "") for s in steps)
    assert "readme_oneliner.py sh" in text and "readme_oneliner.py ps" in text
    assert "cmd /c oneliner.cmd" in text and "PULSE_SOURCE" in text
    assert "[char]0x015E" in text  # the work directory has Turkish characters
    for cmd in ("pulse doctor", "pulse demo", "pulse check", "pulse serve --config"):
        assert cmd in text
    assert any(s.get("uses", "").startswith("actions/upload-artifact@") for s in steps)


def test_install_fallback_job_forces_a_404_and_checks_the_archive_line():
    job = _wf("ci.yml")["jobs"]["install-fallback"]
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "windows-latest"}
    assert "ci-no-such-release" in job["env"]["PULSE_RELEASE_BASE"]
    assert "${{ github.sha }}.zip" in job["env"]["PULSE_ARCHIVE_URL"]
    text = " ".join(s.get("run", "") for s in job["steps"])
    assert "PULSE_SOURCE" not in text and "source: branch archive" in text


def test_readme_one_liners_are_what_ci_runs():
    import importlib.util

    root = WORKFLOWS.parents[1]
    spec = importlib.util.spec_from_file_location(
        "readme_oneliner", root / ".github" / "scripts" / "readme_oneliner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sh = mod.oneliner("sh", readme=root / "README.md")
    ps = mod.oneliner("ps", readme=root / "README.md")
    assert sh == (
        "curl -LsSf https://raw.githubusercontent.com/emirhann-akca/studio-pulse/main/install.sh | sh"
    )
    assert ps.startswith('powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent')
    assert mod.oneliner("sh", "abc123", readme=root / "README.md").endswith(
        "/studio-pulse/abc123/install.sh | sh"
    )


def test_installers_fall_back_to_the_branch_archive_on_404():
    root = WORKFLOWS.parents[1]
    sh = (root / "install.sh").read_text(encoding="utf-8")
    ps = (root / "install.ps1").read_text(encoding="utf-8")
    for text in (sh, ps):
        assert "archive/refs/heads/main.zip" in text and "PULSE_ARCHIVE_URL" in text
        assert "404" in text and "source: branch archive" in text
    assert all(b < 128 for b in (root / "install.ps1").read_bytes())  # irm | iex needs ASCII


def test_optional_extra_and_flutter_jobs():
    jobs = _wf("ci.yml")["jobs"]
    fc = jobs["forecast-extra"]
    assert set(fc["strategy"]["matrix"]["os"]) == {
        "windows-latest",
        "macos-latest",
        "ubuntu-latest",
    }
    assert fc["env"]["PULSE_REQUIRE_FORECAST"] == "1"
    assert ".[dev,forecast]" in " ".join(s.get("run", "") for s in fc["steps"])
    fl = " ".join(s.get("run", "") for s in jobs["flutter-package"]["steps"])
    assert "dart analyze" in fl and "flutter test" in fl


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


def _compare_module():
    import importlib.util

    path = WORKFLOWS.parent / "scripts" / "compare_fingerprints.py"
    spec = importlib.util.spec_from_file_location("compare_fingerprints", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fingerprint_compare_tolerates_summation_order_but_not_real_changes():
    cmp = _compare_module().compare_csv
    head = "app_id,day_n,cohorts,cumulative_arpu_usd,label\n"
    # The CI case: fct_ltv_curve differed in the 13th significant digit between jobs.
    a = head + "a,7,30,0.0595886500436,x\nb,1,9,0.25,y\n"
    b = head + "b,1,9,0.25,y\na,7,30,0.0595886500437,x\n"
    ok, worst, _ = cmp(a, b)
    assert ok and 1e-13 < worst < 1e-11
    assert cmp(a, a) == (True, 0.0, "")
    ok, _, detail = cmp(a, head + "a,7,30,0.0595887,x\nb,1,9,0.25,y\n")  # 1e-6 relative
    assert not ok and "cumulative_arpu_usd" in detail
    ok, _, detail = cmp(a, head + "a,7,31,0.0595886500436,x\nb,1,9,0.25,y\n")  # integer
    assert not ok and "cohorts" in detail
    ok, _, detail = cmp(a, head + "a,7,30,0.0595886500436,z\nb,1,9,0.25,y\n")  # text
    assert not ok and "label" in detail
    ok, _, detail = cmp(a, head + "a,7,30,0.0595886500436,x\n")
    assert not ok and "row counts" in detail
    # near zero the absolute tolerance applies; an empty cell never equals a number
    z = "v\n0\n"
    assert cmp(z, "v\n1e-14\n")[0] and not cmp(z, "v\n\n")[0]


def test_fingerprint_compare_reports_identical_runs_and_tolerated_tables(tmp_path, capsys):
    import json

    mod = _compare_module()
    for job, value in (("ubuntu", "0.0595886500436"), ("macos", "0.0595886500437")):
        d = tmp_path / f"fingerprint-{job}"
        d.mkdir()
        (d / "fct_ltv_curve.csv").write_text(f"x\n{value}\n", encoding="utf-8")
        (d / "fct_same.csv").write_text("y\n1\n", encoding="utf-8")
        tables = {
            "fct_ltv_curve": {"rows": 1, "sha256": job},
            "fct_same": {"rows": 1, "sha256": "s"},
        }
        (d / "fingerprint.json").write_text(
            json.dumps({"sha256": job, "tables": tables}), encoding="utf-8"
        )
    assert mod.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "1 identical" in out and "fct_ltv_curve (max rel" in out and "::error" not in out


def test_ci_regenerates_the_contract_and_smokes_the_collector():
    jobs = _wf("ci.yml")["jobs"]
    lint = " ".join(s.get("run", "") for s in jobs["lint"]["steps"])
    assert "studio_pulse.contract.generate" in lint and "git diff --exit-code" in lint
    smoke = jobs["collector-smoke"]
    assert set(smoke["strategy"]["matrix"]["os"]) == {"windows-latest", "macos-latest"}
    run = " ".join(s.get("run", "") for s in smoke["steps"])
    for needle in (
        "collect",
        "simulate",
        "--verify",
        "/live",
        "demo.lanternleap",
        "serve --config",
        "--check",
    ):
        assert needle in run, needle
