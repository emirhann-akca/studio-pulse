"""`pulse schedule`: generated plist / task XML and the exact scheduler commands.

The OS schedulers are replaced by fakes here (an injected runner, or a fake
executable through PULSE_SCHEDULER_CMD); the real launchctl / schtasks round
trip runs in the schedule-smoke CI job.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from studio_pulse import background, cli, schedule

NS = {"t": schedule.TASK_NS}
TR = "Türkçe Şık"  # "Türkçe Şık", built from code points


def _job(tmp_path: Path, name: str = "refresh", **kw) -> schedule.Job:
    base = tmp_path / TR
    defaults = dict(
        name=name,
        hour=9,
        minute=5,
        args=["run", "--config", str(base / "pulse.yaml"), "--notify"],
        workdir=base,
        log_dir=base / "pulse-state" / "logs",
        program=[str(base / "bin" / "pulse-bg")],
        config=base / "pulse.yaml",
        state_dir=base / "pulse-state",
    )
    defaults.update(kw)
    return schedule.Job(**defaults)


class Recorder:
    """Fake launchctl: `registered` holds the targets that `print` finds."""

    def __init__(self, fail: dict[str, list[int]] | None = None, registered=(), linger: int = 0):
        self.calls: list[list[str]] = []
        self.fail = fail or {}
        self.registered = set(registered)
        self.linger = linger  # `print` still finds a booted-out job this many times
        self.leaving: dict[str, int] = {}

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        verb, rest = argv[1], argv[2:]
        key = f"{verb} {rest[0]}" if rest else verb
        codes = self.fail.get(key)
        code = (codes.pop(0) if len(codes) > 1 else codes[0]) if codes else 0
        out = ""
        if verb == "print":
            if rest[0] in self.leaving:
                self.leaving[rest[0]] -= 1
                if self.leaving[rest[0]] < 0:
                    del self.leaving[rest[0]]
                    self.registered.discard(rest[0])
            code = 0 if rest[0] in self.registered else 113
            if code == 0:
                out = "x = {\n\tstate = not running\n\truns = 3\n\tlast exit code = 0\n}\n"
        elif verb == "bootstrap" and code == 0:
            self.registered.add(f"{rest[0]}/{Path(rest[1]).stem}")
        elif verb == "bootout":
            if self.linger:
                self.leaving[rest[0]] = self.linger
            else:
                self.registered.discard(rest[0])
        return schedule.CmdResult(code, out, "" if code == 0 else "Bootstrap failed: 5")


# -- plist -----------------------------------------------------------------------


def test_plist_is_valid_and_complete(tmp_path):
    job = _job(tmp_path)
    data = plistlib.loads(schedule.render_plist(job))
    assert data["Label"] == "dev.studio-pulse.refresh"
    assert data["ProgramArguments"] == [
        job.program[0],
        "--log-file",
        str(job.log_dir / "pulse.log"),
        *job.args,
    ]
    assert TR in data["ProgramArguments"][2]  # non-ASCII path survives
    assert data["StartCalendarInterval"] == {"Hour": 9, "Minute": 5}
    assert data["RunAtLoad"] is False
    assert data["WorkingDirectory"] == str(job.workdir)
    assert data["EnvironmentVariables"]["PYTHONUTF8"] == "1"
    assert data["ProcessType"] == "Background"
    assert data["StandardErrorPath"].endswith("launchd.log")


@pytest.mark.skipif(not shutil.which("plutil"), reason="plutil is macOS-only")
def test_plist_passes_plutil_lint(tmp_path):
    p = tmp_path / "job.plist"
    p.write_bytes(schedule.render_plist(_job(tmp_path)))
    out = subprocess.run(
        ["plutil", "-lint", str(p)], capture_output=True, text=True, encoding="utf-8"
    )
    assert out.returncode == 0 and out.stdout.strip().endswith("OK")


def test_launchd_commands(tmp_path):
    run = Recorder()
    b = schedule.Launchd(run, agents_dir=tmp_path / "LaunchAgents", uid=501)
    job = _job(tmp_path, "test-abc")
    assert b.install(job) == "gui/501"
    plist = tmp_path / "LaunchAgents" / "dev.studio-pulse.test-abc.plist"
    assert plistlib.loads(plist.read_bytes())["Label"] == "dev.studio-pulse.test-abc"
    st = b.status("test-abc")
    b.run_now("test-abc")
    assert b.remove("test-abc") is True
    gui, user = "gui/501/dev.studio-pulse.test-abc", "user/501/dev.studio-pulse.test-abc"
    assert run.calls == [
        ["launchctl", "print", gui],
        ["launchctl", "print", user],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "print", gui],
        ["launchctl", "print", gui],
        ["launchctl", "kickstart", gui],
        ["launchctl", "print", gui],
        ["launchctl", "bootout", gui],
        ["launchctl", "print", gui],  # gone from both domains?
        ["launchctl", "print", user],
    ]
    assert st["registered"] and st["id"] == gui and st["state"] == "not running"
    assert st["last_exit_code"] == 0 and st["runs"] == 3
    assert not plist.exists() and not b.status("test-abc")["registered"]
    assert not any(c[1] in ("load", "unload") for c in run.calls)  # no legacy verbs


def test_launchd_reinstall_boots_out_the_old_job_first(tmp_path):
    run = Recorder(registered={"gui/501/dev.studio-pulse.refresh"})
    schedule.Launchd(run, agents_dir=tmp_path, uid=501).install(_job(tmp_path))
    verbs = [c[1:3] for c in run.calls]
    assert verbs.index(["bootout", "gui/501/dev.studio-pulse.refresh"]) < verbs.index(
        ["bootstrap", "gui/501"]
    )


def test_launchd_falls_back_to_the_user_domain_without_a_gui_session(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.time, "sleep", lambda s: None)
    run = Recorder(fail={"bootstrap gui/501": [125]})
    b = schedule.Launchd(run, agents_dir=tmp_path, uid=501)
    assert b.install(_job(tmp_path)) == "user/501"
    assert b.status("refresh")["id"] == "user/501/dev.studio-pulse.refresh"
    b.run_now("refresh")
    assert run.calls[-1] == ["launchctl", "kickstart", "user/501/dev.studio-pulse.refresh"]


def test_launchd_bootstrap_is_retried_then_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.time, "sleep", lambda s: None)
    flaky = Recorder(fail={"bootstrap gui/501": [5, 0]})
    assert (
        schedule.Launchd(flaky, agents_dir=tmp_path, uid=501).install(_job(tmp_path)) == "gui/501"
    )
    broken = Recorder(fail={"bootstrap gui/501": [5], "bootstrap user/501": [5]})
    with pytest.raises(schedule.ScheduleError, match="failed in every domain"):
        schedule.Launchd(broken, agents_dir=tmp_path, uid=501).install(_job(tmp_path))


def test_launchd_remove_waits_for_a_running_job_to_go(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.time, "sleep", lambda s: None)
    target = "gui/501/dev.studio-pulse.refresh"
    run = Recorder(registered={target}, linger=3)
    b = schedule.Launchd(run, agents_dir=tmp_path, uid=501)
    assert b.remove("refresh") is True
    assert not b.status("refresh")["registered"]
    assert [c[1] for c in run.calls].count("print") >= 4
    stuck = Recorder(registered={target}, linger=10_000)
    with pytest.raises(schedule.ScheduleError, match="did not remove"):
        schedule.Launchd(stuck, agents_dir=tmp_path, uid=501).remove("refresh", wait_s=0)


def test_parse_launchctl_print_never_exited():
    text = "gui/501/dev.studio-pulse.x = {\n\tstate = running\n\tpid = 4242\n\tlast exit code = (never exited)\n}"
    assert schedule.parse_launchctl_print(text) == {
        "state": "running",
        "pid": 4242,
        "last_exit_code": "(never exited)",
    }


# -- Task Scheduler ------------------------------------------------------------------


def test_task_xml_is_utf16_and_laptop_safe(tmp_path):
    job = _job(tmp_path)
    data = schedule.render_task_xml(job)
    assert data[:2] == b"\xff\xfe"
    text = data[2:].decode("utf-16-le")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>') and "\r\n" in text
    root = ET.fromstring(data)  # the parser honours the BOM and the declaration
    s = root.find("t:Settings", NS)

    def val(path, node=s):
        return node.find(path, NS).text

    assert val("t:StartWhenAvailable") == "true"
    assert val("t:DisallowStartIfOnBatteries") == "false"
    assert val("t:StopIfGoingOnBatteries") == "false"
    assert val("t:MultipleInstancesPolicy") == "IgnoreNew"
    assert val("t:ExecutionTimeLimit") == "PT2H"
    trig = root.find("t:Triggers/t:CalendarTrigger", NS)
    assert val("t:StartBoundary", trig).endswith("T09:05:00")
    assert val("t:ScheduleByDay/t:DaysInterval", trig) == "1"
    p = root.find("t:Principals/t:Principal", NS)
    assert val("t:LogonType", p) == "InteractiveToken" and val("t:RunLevel", p) == "LeastPrivilege"
    ex = root.find("t:Actions/t:Exec", NS)
    assert val("t:Command", ex) == job.program[0]
    assert val("t:Arguments", ex) == subprocess.list2cmdline(
        ["--log-file", str(job.log_file), *job.args]
    )
    assert TR in val("t:WorkingDirectory", ex)
    assert schedule.MARKER in root.find("t:RegistrationInfo/t:Description", NS).text


def test_schtasks_commands(tmp_path):
    seen: dict[str, bytes] = {}

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        if argv[1] == "/create":
            seen["xml"] = Path(argv[argv.index("/xml") + 1]).read_bytes()
        out = ""
        if argv[1] == "/query" and "/v" in argv:
            out = (
                '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result"\r\n'
                '"PC","\\StudioPulse\\refresh","11/12/2026 9:05:00 AM","Ready","Interactive only","11/11/2026 9:05:01 AM","0"\r\n'
            )
        return schedule.CmdResult(0, out, "")

    calls: list[list[str]] = []
    b = schedule.Schtasks(run)
    job = _job(tmp_path)
    b.install(job)
    st = b.status("refresh")
    b.run_now("refresh")
    assert b.remove("refresh") is True
    assert calls[0][:4] == ["schtasks", "/create", "/tn", "StudioPulse\\refresh"]
    assert calls[0][4] == "/xml" and calls[0][6] == "/f"
    assert not Path(calls[0][5]).exists()  # the temporary XML is cleaned up
    assert ET.fromstring(seen["xml"]).tag == f"{{{schedule.TASK_NS}}}Task"
    assert calls[1] == ["schtasks", "/query", "/tn", "StudioPulse\\refresh", "/v", "/fo", "csv"]
    assert calls[2] == ["schtasks", "/run", "/tn", "StudioPulse\\refresh"]
    assert calls[3] == ["schtasks", "/query", "/tn", "StudioPulse\\refresh"]
    assert calls[4] == ["schtasks", "/delete", "/tn", "StudioPulse\\refresh", "/f"]
    assert st["registered"] and st["last_result"] == 0 and st["state"] == "Ready"


def test_schtasks_query_is_read_by_position_not_localised_header():
    tr = (
        '"Ana Bilgisayar Adı","Görev Adı","Sonraki Çalışma","Durum","x","Son Çalışma","Son Sonuç"\r\n'
        '"PC","\\StudioPulse\\refresh","12.11.2026 09:05:00","Hazır","x","11.11.2026 09:05:01","267011"\r\n'
    )
    assert schedule.parse_schtasks_query(tr)["last_result"] == 267011


def test_remove_of_missing_task_reports_false():
    b = schedule.Schtasks(lambda argv: schedule.CmdResult(1, "", "ERROR: not found"))
    assert b.remove("nope") is False


# -- validation, launcher, background runner -------------------------------------------


@pytest.mark.parametrize("bad", ["", "a b", "../x", "x" * 65, "é"])
def test_job_names_are_validated(bad):
    with pytest.raises(schedule.ScheduleError):
        schedule.check_name(bad)


@pytest.mark.parametrize("bad", ["9", "24:00", "09:60", "9.30", "nine"])
def test_times_are_validated(bad):
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_time(bad)


def test_linux_has_no_backend(monkeypatch):
    monkeypatch.delenv("PULSE_SCHEDULER_BACKEND", raising=False)
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    with pytest.raises(schedule.ScheduleError, match="cron"):
        schedule.backend()


def test_background_launcher_is_installed_next_to_python():
    prog = schedule.background_program()
    assert Path(prog[0]).exists()
    if len(prog) == 1:
        assert Path(prog[0]).stem == "pulse-bg"


def test_pulse_bg_logs_and_returns_exit_code(tmp_path):
    log = tmp_path / TR / "pulse.log"
    assert background.main(["--log-file", str(log), "models"]) == 0
    text = log.read_text(encoding="utf-8")
    assert "stg_active_users" in text and "=== exit 0" in text
    assert background.main(["--log-file", str(log), "no-such-command"]) == 2
    assert "=== exit 2" in log.read_text(encoding="utf-8")


# -- CLI round trip through a fake scheduler executable -------------------------------

FAKE = r"""
import json, sys
from pathlib import Path
state = Path(sys.argv[1]); tool = sys.argv[2]; args = sys.argv[3:]
reg = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
log = reg.setdefault("_calls", [])
log.append([tool] + args)
code, out = 0, ""
if tool == "launchctl":
    verb = args[0]
    if verb == "bootstrap":
        reg[args[1] + "/" + Path(args[2]).stem] = "loaded"
    elif verb in ("print", "kickstart"):
        if args[1] not in reg:
            code = 113
        elif verb == "print":
            out = "state = not running\nlast exit code = 0\n"
    elif verb == "bootout":
        code = 0 if reg.pop(args[1], None) else 113
else:
    verb, name = args[0], args[2]
    if verb == "/create":
        reg[name] = Path(args[4]).read_bytes()[2:].decode("utf-16-le")[:5]
    elif verb in ("/query", "/run"):
        code = 0 if name in reg else 1
        if code == 0 and "/v" in args:
            out = '"H","T","N","Ready","L","R","0"\n"PC","\\' + name + '","n","Ready","l","r","0"\n'
    elif verb == "/delete":
        code = 0 if reg.pop(name, None) else 1
state.write_text(json.dumps(reg), encoding="utf-8")
sys.stdout.write(out)
sys.exit(code)
"""


@pytest.mark.parametrize("backend", ["launchd", "schtasks"])
def test_cli_install_status_run_remove_with_fake_scheduler(
    backend, tmp_path, monkeypatch, capsys, demo_build
):
    cfg = demo_build.copy(tmp_path / "data")
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE, encoding="utf-8")
    state = tmp_path / "fake-state.json"
    cmd = [sys.executable, str(fake), str(state)]
    monkeypatch.setenv(
        "PULSE_SCHEDULER_CMD",
        json.dumps({"launchctl": [*cmd, "launchctl"], "schtasks": [*cmd, "schtasks"]}),
    )
    monkeypatch.setenv("PULSE_SCHEDULER_BACKEND", backend)
    monkeypatch.setenv("PULSE_LAUNCH_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("PULSE_HOME", str(tmp_path / "home"))
    c = ["-c", str(cfg.path), "--name", "test-x"]
    assert cli.main(["schedule", "install", *c, "--at", "03:00"]) == 0
    assert (tmp_path / "home" / "schedules" / "test-x.json").exists()
    capsys.readouterr()
    assert cli.main(["schedule", "status", *c, "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["registered"] and st["record"]["at"] == "03:00"
    assert st["record"]["argv"][-3:] == ["--config", str(cfg.path), "--notify"]
    assert cli.main(["schedule", "run-now", *c]) == 0
    assert cli.main(["schedule", "remove", *c]) == 0
    assert cli.main(["schedule", "status", *c]) == 3
    assert cli.main(["schedule", "remove", *c]) == 3
    assert cli.main(["schedule", "remove", *c, "--missing-ok"]) == 0
    calls = json.loads(state.read_text(encoding="utf-8"))["_calls"]
    verbs = [c[1] for c in calls]
    if backend == "launchd":
        assert verbs[:3] == ["print", "print", "bootstrap"] and "kickstart" in verbs
        assert not (tmp_path / "agents" / "dev.studio-pulse.test-x.plist").exists()
    else:
        assert verbs[0] == "/create" and "/run" in verbs and "/delete" in verbs
    assert not (tmp_path / "home" / "schedules" / "test-x.json").exists()


def test_run_now_wait_ignores_the_previous_run(tmp_path, monkeypatch, capsys, demo_build):
    """A success that finished just before the kick must not count as this run."""
    from studio_pulse import runstate

    cfg = demo_build.copy(tmp_path / "data")
    with runstate.RunRecorder(cfg.state_dir, "demo"):
        pass  # a successful run that ends right now
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import sys; print('state = not running'); sys.exit(0)", encoding="utf-8"
    )  # a scheduler that accepts the kick but starts nothing
    monkeypatch.setenv(
        "PULSE_SCHEDULER_CMD", json.dumps({"launchctl": [sys.executable, str(fake)]})
    )
    monkeypatch.setenv("PULSE_SCHEDULER_BACKEND", "launchd")
    monkeypatch.setenv("PULSE_HOME", str(tmp_path / "home"))
    rc = cli.main(["schedule", "run-now", "-c", str(cfg.path), "--name", "x", "--wait", "2"])
    assert rc == 1 and "No finished run within 2 s" in capsys.readouterr().err
