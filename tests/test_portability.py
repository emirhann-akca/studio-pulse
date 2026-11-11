"""Windows / macOS / Linux portability: time zones, encodings, line endings, installers."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import re
import subprocess
import sys
import tomllib
import zoneinfo
from pathlib import Path

import pytest

from studio_pulse import __version__, doctor, fingerprint
from studio_pulse.config import ConfigError, config_from_dict

ROOT = Path(__file__).parents[1]
TR = "Türkçe Şık"  # "Türkçe Şık"


@pytest.fixture()
def tzdata_only():
    """zoneinfo with no system database, as on Windows: only the tzdata package."""
    zoneinfo.reset_tzpath(to=[])
    zoneinfo.ZoneInfo.clear_cache()
    try:
        yield
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


def test_windows_gets_tzdata_from_the_package_metadata():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    tz = [d for d in deps if d.replace(" ", "").startswith("tzdata")]
    assert tz and "sys_platform=='win32'" in tz[0].replace(" ", "").replace('"', "'")


def test_zoneinfo_works_from_tzdata_alone(tzdata_only):
    assert zoneinfo.TZPATH == ()
    importlib.import_module("tzdata")  # dev extra installs it on every OS
    ist = zoneinfo.ZoneInfo("Europe/Istanbul")
    assert ist.utcoffset(dt.datetime(2026, 7, 1)) == dt.timedelta(hours=3)
    ny = zoneinfo.ZoneInfo("America/New_York")
    assert ny.utcoffset(dt.datetime(2026, 1, 15)) == dt.timedelta(hours=-5)
    cfg = config_from_dict({"apps": [{"id": "a", "timezone": "Europe/Istanbul"}]}, Path("."))
    assert cfg.apps[0].timezone == "Europe/Istanbul"
    assert doctor.check_timezones().status == "ok"


def test_without_tzdata_and_system_zones_config_fails_clearly(tzdata_only, monkeypatch):
    for mod in [m for m in sys.modules if m == "tzdata" or m.startswith("tzdata.")] + ["tzdata"]:
        monkeypatch.setitem(sys.modules, mod, None)  # import tzdata -> ImportError
    zoneinfo.ZoneInfo.clear_cache()
    with pytest.raises(ConfigError, match="unknown time zone"):
        config_from_dict({"apps": [{"id": "a", "timezone": "Europe/Istanbul"}]}, Path("."))
    assert doctor.check_timezones().status == "fail"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ships no IANA database")
def test_windows_resolves_zones_without_help():
    assert zoneinfo.ZoneInfo("Europe/Istanbul").utcoffset(dt.datetime(2026, 1, 1)) == dt.timedelta(
        hours=3
    )


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.decode("utf-8").split("\0") if p]


BINARY = {".png", ".parquet", ".duckdb", ".gz", ".whl"}


def test_checkout_has_lf_utf8_text_on_every_os():
    """.gitattributes keeps text LF in the working tree, Windows included."""
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attrs
    bad = []
    for f in _tracked_files():
        if f.suffix in BINARY or "fixtures" in f.parts or not f.exists():
            continue
        data = f.read_bytes()
        if b"\r\n" in data:
            bad.append(f"{f.relative_to(ROOT)}: CRLF")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{f.relative_to(ROOT)}: not UTF-8")
    assert not bad, bad


def test_installers_are_pinned_and_match_the_package():
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps = (ROOT / "install.ps1").read_bytes().decode("ascii")  # irm | iex scripts must be ASCII
    assert f'VERSION="{__version__}"' in sh
    assert f"$Version = '{__version__}'" in ps
    uv_sh = re.search(r'UV_VERSION="(\d+\.\d+\.\d+)"', sh).group(1)
    uv_ps = re.search(r"\$UvVersion = '(\d+\.\d+\.\d+)'", ps).group(1)
    assert uv_sh == uv_ps, "both installers pin the same uv"
    assert "astral.sh/uv/$UV_VERSION/install.sh" in sh
    assert "astral.sh/uv/$UvVersion/install.ps1" in ps
    for text in (sh, ps):
        assert "releases/download/v" in text  # default source: the release wheel
    code = [ln for ln in ps.splitlines() if not ln.lstrip().startswith("#")]
    statement_exit = re.compile(r"(^|[;{}])\s*exit\b", re.I)  # not the word inside messages
    assert not any(statement_exit.search(ln) for ln in code), "never exit: it closes the window"
    assert code[0].strip() == "& {" and code[-1].strip() == "}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax check")
def test_install_sh_parses():
    assert subprocess.run(["sh", "-n", str(ROOT / "install.sh")]).returncode == 0


def test_readme_covers_both_operating_systems():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "install.ps1" in readme and "install.sh" in readme
    assert "PowerShell" in readme and "macOS" in readme
    assert "source .venv/bin/activate" not in readme or ".venv\\Scripts" in readme


def test_console_output_survives_a_legacy_code_page(demo_build, tmp_path):
    """A cp1252/ASCII console (Windows, scheduled runs) must not crash on Şık paths."""
    cfg = demo_build.copy(tmp_path / TR)
    env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    r = subprocess.run(
        [sys.executable, "-m", "studio_pulse.cli", "doctor", "-c", str(cfg.path)],
        capture_output=True,
        env=env,
    )
    out = r.stdout.decode("ascii")
    assert r.returncode == 0, out + r.stderr.decode("ascii", "replace")
    assert "\\xfc" in out or "\\u015e" in out  # escaped, not crashed


def test_fingerprint_formatting_is_canonical():
    c = fingerprint._cell
    assert c(None) == "" and c(True) == "true" and c(-0.0) == "0"
    assert c(0.1 + 0.2) == c(0.3) == "0.3"
    assert c(float("nan")) == "nan" and c(float("-inf")) == "-inf"
    assert c(dt.date(2026, 10, 1)) == "2026-10-01"
    assert c('a,"b"') == '"a,""b"""'
    # A value exactly on a 12-digit rounding tie: the last-bit noise of a parallel sum
    # (seen as 1.49979742188 on macOS vs ...87 on Windows) must not change the text.
    import math

    tie = 1.499797421875
    assert {c(tie), c(math.nextafter(tie, 2)), c(math.nextafter(tie, 0))} == {"1.49979742188"}


def test_fingerprint_ignores_load_times_but_sees_values(demo_build, tmp_path):
    import duckdb

    cfg = demo_build.copy(tmp_path)
    con = duckdb.connect(str(cfg.warehouse))
    before = fingerprint.fingerprint(con, tmp_path / "fp")
    con.execute("UPDATE fct_source_freshness SET last_loaded_at = last_loaded_at + INTERVAL 1 HOUR")
    assert fingerprint.fingerprint(con)["sha256"] == before["sha256"]
    con.execute("UPDATE fct_app_daily SET dau = dau + 1 WHERE rowid = 0")
    after = fingerprint.fingerprint(con)
    con.close()
    changed = [t for t in after["tables"] if after["tables"][t] != before["tables"][t]]
    assert changed == ["fct_app_daily"]
    saved = json.loads((tmp_path / "fp" / "fingerprint.json").read_text(encoding="utf-8"))
    assert saved == before
    assert (tmp_path / "fp" / "fct_app_daily.csv").read_bytes().count(b"\r") == 0


def test_doctor_json_never_prints_credentials(tmp_path, monkeypatch, capsys, demo_build):
    from studio_pulse import cli

    secret = "1//0g-super-secret-refresh-token"
    adc = tmp_path / "adc.json"
    adc.write_text(
        json.dumps({"type": "authorized_user", "client_secret": "cs-xyz", "refresh_token": secret}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    cfg = demo_build.copy(tmp_path / "d")
    assert cli.main(["doctor", "-c", str(cfg.path), "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True
    names = {c["name"] for c in data["checks"]}
    assert {"python", "time zones", "duckdb", "config", "warehouse", "schedule"} <= names
    assert secret not in out and "cs-xyz" not in out
    assert "authorized_user" in out


def test_doctor_fails_when_bigquery_config_has_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    cfg = tmp_path / "pulse.yaml"
    cfg.write_text(
        "warehouse: w.duckdb\napps:\n  - id: g\n    sources:\n"
        "      ga4_bigquery: {project: p, dataset: analytics_1}\n",
        encoding="utf-8",
    )
    res = {r.name: r for r in doctor.run(str(cfg))}
    assert res["google credentials"].status == "fail"
