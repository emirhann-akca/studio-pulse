"""deploy/cloudrun/make_keys.py: every write key bootstrap.sh makes is bound to its apps."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from studio_pulse.collector.settings import parse_write_keys
from studio_pulse.contract import spec

ROOT = Path(__file__).parents[1]
CLOUDRUN = ROOT / "deploy" / "cloudrun"


def _load():
    loader = importlib.util.spec_from_file_location("make_keys", CLOUDRUN / "make_keys.py")
    mod = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(mod)
    return mod


mk = _load()


def env_of(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep:
            out[name] = value
    return out


def test_every_key_is_bound_to_its_apps_and_never_printed(tmp_path, capsys):
    f = tmp_path / "state" / "collector.env"
    rc = mk.main(["keys", str(f), "puzzle=com.example.puzzle",
                  "runner=com.example.runner|com.example.runner.ios"])  # fmt: skip
    assert rc == 0
    env = env_of(f)
    keys, bound = parse_write_keys(env["PULSE_WRITE_KEYS"])
    puzzle, runner = env["PULSE_WRITE_KEY_PUZZLE"], env["PULSE_WRITE_KEY_RUNNER"]
    assert keys == [puzzle, runner]
    assert bound == {puzzle: ["com.example.puzzle"],
                     runner: ["com.example.runner", "com.example.runner.ios"]}  # fmt: skip
    assert puzzle.startswith("pk_puzzle_") and len(env["PULSE_READ_KEY"]) >= 40
    out = capsys.readouterr().out
    for secret in (puzzle, runner, env["PULSE_READ_KEY"]):
        assert secret not in out
    assert mk.key_id(puzzle) in out and "com.example.runner.ios" in out
    if os.name == "posix":
        assert f.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "spec_",
    [["puzzle"], ["puzzle="], ["puzzle=not a bundle"], ["Bad-Label=com.example.a"],
     ["a=com.example.a", "a=com.example.b"], []],
)  # fmt: skip
def test_an_unbound_or_broken_entry_is_refused_and_nothing_is_written(tmp_path, capsys, spec_):
    f = tmp_path / "collector.env"
    assert mk.main(["keys", str(f), *spec_]) == 2
    assert not f.exists() and "make_keys:" in capsys.readouterr().err


def test_a_second_run_keeps_the_keys_rebinds_and_keeps_other_lines(tmp_path):
    f = tmp_path / "collector.env"
    mk.main(["keys", str(f), "puzzle=com.example.puzzle"])
    first = env_of(f)
    with f.open("a", encoding="utf-8") as fh:
        fh.write("PULSE_COLLECTOR_URL=https://collector.example.com\n")
    mk.main(["keys", str(f), "puzzle=com.example.puzzle|com.example.puzzle.ios",
             "habit=com.example.habit"])  # fmt: skip
    env = env_of(f)
    assert env["PULSE_WRITE_KEY_PUZZLE"] == first["PULSE_WRITE_KEY_PUZZLE"]
    assert env["PULSE_READ_KEY"] == first["PULSE_READ_KEY"]
    assert env["PULSE_COLLECTOR_URL"] == "https://collector.example.com"
    keys, bound = parse_write_keys(env["PULSE_WRITE_KEYS"])
    assert len(keys) == 2 and all(bound.get(k) for k in keys)
    assert bound[env["PULSE_WRITE_KEY_PUZZLE"]] == ["com.example.puzzle", "com.example.puzzle.ios"]
    # a label only the file has keeps its key and its apps
    mk.main(["keys", str(f), "habit=com.example.habit"])
    assert parse_write_keys(env_of(f)["PULSE_WRITE_KEYS"])[1] == bound


def test_a_file_from_the_old_bootstrap_must_bind_its_keys_first(tmp_path, capsys):
    f = tmp_path / "collector.env"
    f.write_text(
        "PULSE_WRITE_KEY_GAME=pk_game_aaaaaaaaaaaaaaaaaaaaaaaa\n"
        "PULSE_WRITE_KEY_APP=pk_app_bbbbbbbbbbbbbbbbbbbbbbbb\n"
        "PULSE_WRITE_KEYS=pk_game_aaaaaaaaaaaaaaaaaaaaaaaa,pk_app_bbbbbbbbbbbbbbbbbbbbbbbb\n"
        "PULSE_READ_KEY=r\n",
        encoding="utf-8",
    )
    before = f.read_text(encoding="utf-8")
    assert mk.main(["keys", str(f), "game=com.example.game"]) == 2
    assert "PULSE_WRITE_KEY_APP" in capsys.readouterr().err
    assert f.read_text(encoding="utf-8") == before
    assert mk.main(["keys", str(f), "game=com.example.game", "app=com.example.app"]) == 0
    keys, bound = parse_write_keys(env_of(f)["PULSE_WRITE_KEYS"])
    assert keys == ["pk_game_aaaaaaaaaaaaaaaaaaaaaaaa", "pk_app_bbbbbbbbbbbbbbbbbbbbbbbb"]
    assert bound == {keys[0]: ["com.example.game"], keys[1]: ["com.example.app"]}


def test_a_key_listed_without_a_label_line_is_refused(tmp_path):
    f = tmp_path / "collector.env"
    f.write_text("PULSE_WRITE_KEYS=pk_orphan_1\n", encoding="utf-8")
    assert mk.main(["keys", str(f), "game=com.example.game"]) == 2


def test_plan_writes_nothing(tmp_path, capsys):
    f = tmp_path / "collector.env"
    assert mk.main(["keys", str(f), "--plan", "puzzle=com.example.puzzle"]) == 0
    assert not f.exists() and "not written" in capsys.readouterr().out


def test_sdk_files_hold_the_url_and_one_write_key_only(tmp_path):
    f = tmp_path / "collector.env"
    mk.main(["keys", str(f), "puzzle=com.example.puzzle", "habit=com.example.habit"])
    assert mk.main(["sdk", str(f), str(tmp_path / "sdk")]) == 2  # no URL yet
    with f.open("a", encoding="utf-8") as fh:
        fh.write("PULSE_COLLECTOR_URL=https://collector.example.com\n")
    assert mk.main(["sdk", str(f), str(tmp_path / "sdk")]) == 0
    env = env_of(f)
    puzzle = env_of(tmp_path / "sdk" / "puzzle.env")
    assert puzzle == {"PULSE_COLLECTOR_URL": "https://collector.example.com",
                      "PULSE_WRITE_KEY": env["PULSE_WRITE_KEY_PUZZLE"]}  # fmt: skip
    text = (tmp_path / "sdk" / "habit.env").read_text(encoding="utf-8")
    assert env["PULSE_READ_KEY"] not in text and env["PULSE_WRITE_KEY_PUZZLE"] not in text


def test_the_app_id_pattern_is_the_contracts():
    assert mk.APP_ID.pattern == spec()["ctx"]["app_id"]["pattern"]


def test_bootstrap_makes_bound_keys_only():
    sh = (CLOUDRUN / "bootstrap.sh").read_text(encoding="utf-8")
    assert "make_keys.py" in sh and 'keys "$ENVF"' in sh and 'sdk "$ENVF"' in sh
    assert "game app" not in sh and "secrets.token_hex" not in sh  # no unbound default
    assert 'APPS="${APPS:-}"' in sh
    readme = (CLOUDRUN / "README.md").read_text(encoding="utf-8")
    assert "APPS=" in readme and "=com.example." in readme


def test_the_script_runs_with_a_bare_interpreter(tmp_path):
    f = tmp_path / "collector.env"
    r = subprocess.run(
        [sys.executable, "-I", str(CLOUDRUN / "make_keys.py"), "keys", str(f),
         "puzzle=com.example.puzzle"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert env_of(f)["PULSE_WRITE_KEY_PUZZLE"] not in r.stdout
    if shutil.which("sh") and os.name == "posix":
        env = {k: v for k, v in os.environ.items() if k != "APPS"}
        r = subprocess.run(["sh", str(CLOUDRUN / "bootstrap.sh"), "p", "--plan"],
                           capture_output=True, text=True, encoding="utf-8", check=False, cwd=ROOT,
                           env=env)  # fmt: skip
        assert r.returncode == 2 and "APPS" in r.stderr  # refuses to run without bound apps
