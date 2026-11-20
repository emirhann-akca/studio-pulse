"""tools/new_game.py: one command per new game (collector key, LevelPlay, studio-app.json).

gcloud and the LevelPlay platform are faked: no account, no network. The plan must change
nothing anywhere and no key may ever be printed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load("new_game", ROOT / "tools/new_game.py")
onboard_tests = _load("onboard_tests", ROOT / "tests/test_ads_onboard.py")
mk = tool.make_keys
FIX = ROOT / "tests/fixtures/ads_onboard"

ARGS = ["kupdunyalari", "--name", "Küp Dünyaları", "--ios", "com.example.kup",
        "--android", "com.example.kup"]  # fmt: skip


def env_of(path: Path) -> dict[str, str]:
    return dict(
        ln.split("=", 1) for ln in path.read_text(encoding="utf-8").splitlines() if "=" in ln
    )


@pytest.fixture
def state(tmp_path, capsys):
    """A studio that already has Acorn Mart's key and a deployed collector."""
    s = tmp_path / "state"
    mk.cmd_keys(
        s / "collector.env", ["acornmart=com.example.acorn|com.example.acorn.old"], dry=False
    )
    with (s / "collector.env").open("a", encoding="utf-8") as f:
        f.write("PULSE_COLLECTOR_URL=https://collector.example.run.app\n")
    capsys.readouterr()
    return s


class FakeGcloud:
    def __init__(self, current="old-value"):
        self.calls: list[tuple[list[str], str | None]] = []
        self.current = current

    def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        joined = " ".join(argv)
        if "versions access latest" in joined:
            if self.current is None:
                raise tool.NewGameError("NOT_FOUND")
            return self.current
        if "versions add" in joined:
            return "projects/123/secrets/pulse-write-keys/versions/7"
        if "versions describe latest" in joined:
            return "projects/123/secrets/pulse-write-keys/versions/6"
        if "services describe" in joined:
            return "https://collector.example.run.app"
        if "services update" in joined:
            return ""
        raise AssertionError(joined)


def never(argv, stdin):
    raise AssertionError(f"the plan ran {argv}")


def test_plan_changes_nothing_and_prints_no_key(tmp_path, state, capsys):
    before = (state / "collector.env").read_bytes()
    manifest = tmp_path / "kup" / "studio-app.json"
    lines: list[str] = []
    rc = tool.main([*ARGS, "--manifest", str(manifest)], run=never, client=None, state=state,
                   fetch=never, out=lines.append)  # fmt: skip
    assert rc == 0
    printed = "\n".join(lines) + capsys.readouterr().out
    assert (state / "collector.env").read_bytes() == before, "collector.env untouched"
    assert not manifest.exists() and not (state / "sdk").exists(), "no file written"
    assert "CLOUDSDK_CORE_PROJECT=grich-pulse" in printed and "--project grich-pulse" in printed
    assert "kupdunyalari: write key" in printed and "(new)" in printed
    acorn = env_of(state / "collector.env")["PULSE_WRITE_KEY_ACORNMART"]
    assert f"acornmart: write key {mk.key_id(acorn)} (kept)" in printed
    for secret in env_of(state / "collector.env").values():
        if secret.startswith("pk_") or len(secret) > 40:
            assert secret not in printed
    assert "--update-secrets PULSE_WRITE_KEYS=pulse-write-keys:<yeni sürüm>" in printed
    assert "LEVELPLAY_SECRET_KEY ve LEVELPLAY_REFRESH_TOKEN yok" in printed
    shown = json.loads("\n".join(ln[6:] for ln in lines if ln.startswith("    | ")))
    assert shown["pulse_label"] == "kupdunyalari" and shown["release_inject"] == "store"
    assert shown["platforms"] == {"ios": {"bundle_id": "com.example.kup"},
                                  "android": {"package_name": "com.example.kup"}}  # fmt: skip
    assert shown["networks"]["pulse"]["writeKey"] == "", "the key never goes into the manifest"


def test_apply_binds_the_key_rolls_the_collector_and_writes_the_files(tmp_path, state, capsys):
    gc = FakeGcloud()
    manifest = tmp_path / "kup" / "studio-app.json"
    lines: list[str] = []
    rc = tool.main([*ARGS, "--manifest", str(manifest), "--apply"], run=gc, client=None,
                   state=state, fetch=lambda url: '{"ok": true}', out=lines.append)  # fmt: skip
    assert rc == 0
    env = env_of(state / "collector.env")
    key = env["PULSE_WRITE_KEY_KUPDUNYALARI"]
    assert env["PULSE_WRITE_APPS_KUPDUNYALARI"] == "com.example.kup"
    assert env["PULSE_WRITE_KEY_ACORNMART"].startswith("pk_acornmart_"), "other games kept"
    argvs = [" ".join(a) for a, _ in gc.calls]
    assert [a.split(" ")[1:4] for a in argvs] == [
        ["secrets", "versions", "access"],
        ["secrets", "versions", "add"],
        ["run", "services", "update"],
        ["run", "services", "describe"],
    ]
    assert all("--project grich-pulse" in a for a in argvs)
    add = gc.calls[1]
    assert add[1] == env["PULSE_WRITE_KEYS"], "the keys go through stdin"
    assert not any(key in a for a in argvs), "never on a command line"
    assert "--update-secrets=PULSE_WRITE_KEYS=pulse-write-keys:7" in argvs[2]
    sdk = env_of(state / "sdk" / "kupdunyalari.env")
    assert sdk == {
        "PULSE_COLLECTOR_URL": "https://collector.example.run.app",
        "PULSE_WRITE_KEY": key,
    }
    assert (state / "sdk" / "acornmart.env").exists()
    if os.name == "posix":
        assert (state / "sdk" / "kupdunyalari.env").stat().st_mode & 0o777 == 0o600
    written = json.loads(manifest.read_text(encoding="utf-8"))
    assert written["name"] == "Küp Dünyaları" and written["pulse_label"] == "kupdunyalari"
    assert key not in manifest.read_text(encoding="utf-8")
    printed = "\n".join(lines) + capsys.readouterr().out
    assert key not in printed and "/health" in printed


def test_an_up_to_date_secret_still_pins_the_service(tmp_path, state, capsys):
    tool.main([*ARGS, "--manifest", str(tmp_path / "m.json"), "--apply"], run=FakeGcloud(),
              client=None, state=state, fetch=lambda u: "ok", out=lambda s: None)  # fmt: skip
    keys = env_of(state / "collector.env")["PULSE_WRITE_KEYS"]
    gc = FakeGcloud(current=keys)
    rc = tool.main([*ARGS, "--manifest", str(tmp_path / "m.json"), "--apply"], run=gc,
                   client=None, state=state, fetch=lambda u: "ok", out=lambda s: None)  # fmt: skip
    assert rc == 0
    argvs = [" ".join(a) for a, _ in gc.calls]
    assert not any("versions add" in a for a in argvs), "no new version for the same keys"
    assert any("pulse-write-keys:6" in a for a in argvs), "the service gets the current version"


def test_the_collector_url_is_recorded_when_missing(tmp_path, capsys):
    s = tmp_path / "state"
    mk.cmd_keys(s / "collector.env", ["acornmart=com.example.acorn"], dry=False)
    tool.main([*ARGS, "--manifest", str(tmp_path / "m.json"), "--apply"], run=FakeGcloud(None),
              client=None, state=s, fetch=lambda u: "ok", out=lambda x: None)  # fmt: skip
    assert env_of(s / "collector.env")["PULSE_COLLECTOR_URL"] == "https://collector.example.run.app"
    assert env_of(s / "sdk" / "kupdunyalari.env")["PULSE_COLLECTOR_URL"].startswith("https://")


def test_an_existing_manifest_keeps_its_ids(tmp_path):
    existing = json.loads((FIX / "studio-app.json").read_text(encoding="utf-8"))
    existing["networks"]["levelplay"] = {
        "ios": {"app_key": "2991f999d", "ad_units": {"rewarded": "tzk9ul10udxdzms4"}}
    }
    existing["networks"]["ump"] = {"ios_app_id": "ca-app-pub-1~2", "android_app_id": ""}
    a = tool.parse(["puzzle", "--name", "Example Puzzle", "--ios", "com.example.puzzle"])
    m = tool.build_manifest(existing, a)
    assert m["networks"]["levelplay"]["ios"] == {
        "app_key": "2991f999d",
        "ad_units": {"rewarded": "tzk9ul10udxdzms4", "interstitial": ""},
    }
    assert m["networks"]["ump"]["ios_app_id"] == "ca-app-pub-1~2"
    assert m["networks"]["unity_ads"] == existing["networks"]["unity_ads"]
    assert m["unity_config"] == "Assets/Resources/studio_app.json", "kept as it was"
    assert m["pulse_label"] == "puzzle" and m["networks"]["pulse"]["collectorUrl"] == ""
    assert tool.build_manifest(m, a) == m, "running it again changes nothing"


def test_levelplay_ids_go_into_the_manifest(tmp_path):
    fake = onboard_tests.FakeLevelPlay()
    client = tool.ads_onboard.LevelPlay(fake, onboard_tests.SECRET, onboard_tests.REFRESH)
    manifest = tmp_path / "studio-app.json"
    args = ["puzzle", "--name", "Example Puzzle", "--ios", "com.example.puzzle",
            "--manifest", str(manifest), "--skip-collector"]  # fmt: skip
    lines: list[str] = []
    rc = tool.main(args, run=never, client=client, state=tmp_path, out=lines.append)
    assert rc == 0 and fake.writes() == [], "the plan only reads LevelPlay"
    shown = json.loads("\n".join(ln[6:] for ln in lines if ln.startswith("    | ")))
    assert shown["networks"]["levelplay"]["ios"]["app_key"] == "1a2b3c4d"
    assert shown["networks"]["levelplay"]["ios"]["ad_units"]["rewarded"] == "rw00000000000001"
    assert shown["networks"]["levelplay"]["ios"]["ad_units"]["interstitial"] == ""
    assert not manifest.exists()

    rc = tool.main([*args, "--apply"], run=never, client=client, state=tmp_path, out=lines.append)
    assert rc == 0, "\n".join(lines)
    written = json.loads(manifest.read_text(encoding="utf-8"))
    units = written["networks"]["levelplay"]["ios"]["ad_units"]
    assert units["interstitial"].startswith("id"), "the missing unit was created"
    assert [c[0] for c in fake.writes()] == ["POST", "PUT"], "create the unit, unpause rewarded"


@pytest.mark.parametrize(
    "argv",
    [
        ["Bad Label", "--name", "X", "--ios", "com.example.x"],
        ["ok", "--name", "X"],
        ["ok", "--name", "X", "--ios", "not an id"],
        ["ok", "--name", "X", "--ios", "com.example.x", "--formats", "rewarded,video"],
    ],
)
def test_bad_input_is_refused(argv, capsys):
    with pytest.raises(SystemExit) as e:
        tool.parse(argv)
    assert e.value.code == 2


def test_gcloud_gets_the_project(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout=" out \n", stderr="")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    assert tool.gcloud_runner("grich-pulse")(["gcloud", "x"], "in") == "out"
    assert seen["env"]["CLOUDSDK_CORE_PROJECT"] == "grich-pulse" and seen["input"] == "in"

    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied"),
    )
    with pytest.raises(tool.NewGameError, match="denied"):
        tool.gcloud_runner("grich-pulse")(["gcloud", "x"], None)


def test_state_dir_matches_the_python_package(tmp_path):
    assert tool.user_state_dir({"PULSE_HOME": str(tmp_path)}, "linux") == tmp_path
    assert tool.user_state_dir({}, "darwin").parts[-3:] == (
        "Library",
        "Application Support",
        "studio-pulse",
    )
    assert (
        tool.user_state_dir({"LOCALAPPDATA": str(tmp_path)}, "win32") == tmp_path / "studio-pulse"
    )
    assert (
        tool.user_state_dir({"XDG_STATE_HOME": str(tmp_path)}, "linux") == tmp_path / "studio-pulse"
    )
