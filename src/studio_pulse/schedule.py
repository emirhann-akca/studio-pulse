"""`pulse schedule`: register the daily refresh with the operating system.

macOS: a LaunchAgent plist in ~/Library/LaunchAgents, loaded with
`launchctl bootstrap gui/<uid>` and removed with `launchctl bootout` (the
non-legacy subcommands). StartCalendarInterval runs a missed job when the Mac
wakes up.

Windows: a Task Scheduler task imported from XML with `schtasks /create /xml`,
because schtasks' flags cannot set the laptop settings: start on battery, keep
running when unplugged, and start a missed run as soon as possible. The task
runs `pulse-bg.exe`, a GUI-subsystem launcher, so no console window flashes.

Linux is not covered: use cron or a systemd timer (`pulse schedule install`
prints the line to add).

The scheduled command is `pulse-bg --log-file <state>/logs/pulse.log run
--config <config>`. Each job is also recorded in <user state>/schedules/<name>.json
so `status` works from any directory.

Testing hooks (environment): PULSE_SCHEDULER_BACKEND=launchd|schtasks picks the
backend regardless of the OS, PULSE_SCHEDULER_CMD='{"launchctl": [...],
"schtasks": [...]}' replaces the scheduler executables with a fake command, and
PULSE_LAUNCH_AGENTS_DIR moves the plist directory.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape  # nosec B406 (escaping only; nothing is parsed)

from . import __version__

LABEL_PREFIX = "dev.studio-pulse."
WIN_FOLDER = "StudioPulse"
MARKER = "managed by pulse schedule"
DEFAULT_NAME = "refresh"
DEFAULT_TIME = "09:30"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class ScheduleError(RuntimeError):
    pass


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CmdResult]


def _decode(data: bytes) -> str:
    if not data:
        return ""
    if sys.platform == "win32":
        # Console tools write in the OEM code page when their output is a pipe.
        for enc in ("oem", "mbcs"):
            try:
                return data.decode(enc, errors="replace")
            except LookupError:
                continue
    return data.decode("utf-8", errors="replace")


def run_command(argv: Sequence[str]) -> CmdResult:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(list(argv), capture_output=True, check=False, **kwargs)
    return CmdResult(p.returncode, _decode(p.stdout), _decode(p.stderr))


def user_state_dir() -> Path:
    """Per-user directory for studio-pulse's own bookkeeping ($PULSE_HOME overrides)."""
    if os.environ.get("PULSE_HOME"):
        return Path(os.environ["PULSE_HOME"])
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "studio-pulse"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "studio-pulse"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "studio-pulse"


def background_program() -> list[str]:
    """argv prefix that runs pulse without a console window."""
    exe_dir = Path(sys.executable).parent
    name = "pulse-bg.exe" if sys.platform == "win32" else "pulse-bg"
    candidates = [exe_dir / name]
    if sys.platform == "win32":
        candidates.append(exe_dir / "Scripts" / name)
    for c in candidates:
        if c.exists():
            return [str(c)]
    py = Path(sys.executable)
    if sys.platform == "win32" and (exe_dir / "pythonw.exe").exists():
        py = exe_dir / "pythonw.exe"
    return [str(py), "-m", "studio_pulse.background"]


def check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ScheduleError(
            f"schedule name {name!r} must be 1-64 characters of letters, digits, '.', '_' or '-'"
        )
    return name


def parse_time(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError(f"--at must be HH:MM (24-hour), got {value!r}")
    return int(m.group(1)), int(m.group(2))


@dataclass
class Job:
    name: str
    hour: int
    minute: int
    args: list[str]
    workdir: Path
    log_dir: Path
    program: list[str] = field(default_factory=background_program)
    config: Path | None = None
    state_dir: Path | None = None

    def __post_init__(self) -> None:
        check_name(self.name)

    @property
    def label(self) -> str:
        return LABEL_PREFIX + self.name

    @property
    def task_name(self) -> str:
        return f"{WIN_FOLDER}\\{self.name}"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "pulse.log"

    def argv(self) -> list[str]:
        return [*self.program, "--log-file", str(self.log_file), *self.args]

    def describe(self) -> str:
        return f"studio-pulse {' '.join(self.args[:1])} at {self.hour:02d}:{self.minute:02d} ({MARKER})"


# -- macOS -----------------------------------------------------------------------


def render_plist(job: Job) -> bytes:
    data: dict[str, Any] = {
        "Label": job.label,
        "ProgramArguments": job.argv(),
        "WorkingDirectory": str(job.workdir),
        "StartCalendarInterval": {"Hour": job.hour, "Minute": job.minute},
        "RunAtLoad": False,
        "EnvironmentVariables": {"PYTHONUTF8": "1", "PULSE_SCHEDULED": "1"},
        "StandardOutPath": str(job.log_dir / "launchd.log"),
        "StandardErrorPath": str(job.log_dir / "launchd.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False)


_PRINT_FIELDS = {
    "state": re.compile(r"^\s*state = (.+)$", re.M),
    "last_exit_code": re.compile(r"^\s*last exit code = (.+)$", re.M),
    "runs": re.compile(r"^\s*runs = (\d+)$", re.M),
    "pid": re.compile(r"^\s*pid = (\d+)$", re.M),
}


def parse_launchctl_print(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, rx in _PRINT_FIELDS.items():
        m = rx.search(text)
        if m:
            v = m.group(1).strip()
            out[key] = int(v) if v.lstrip("-").isdigit() else v
    return out


class Launchd:
    """LaunchAgent in the user's GUI domain (gui/<uid>).

    A session without a GUI login (ssh, some CI machines) has no gui/<uid>
    domain; install then falls back to the user's background domain
    (user/<uid>), and the other commands find the job in whichever of the two
    domains holds it.
    """

    kind = "launchd"

    def __init__(
        self,
        run: Runner = run_command,
        *,
        launchctl: Sequence[str] = ("launchctl",),
        agents_dir: Path | None = None,
        uid: int | None = None,
    ):
        self.run = run
        self.launchctl = list(launchctl)
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"
        if uid is None:
            uid = os.getuid() if hasattr(os, "getuid") else 0
        self.uid = uid
        self.domains = [f"gui/{uid}", f"user/{uid}"]

    def plist_path(self, name: str) -> Path:
        return self.agents_dir / f"{LABEL_PREFIX}{name}.plist"

    def render(self, job: Job) -> bytes:
        return render_plist(job)

    def _lc(self, *args: str) -> CmdResult:
        return self.run([*self.launchctl, *args])

    def _find(self, name: str) -> tuple[str | None, CmdResult | None]:
        for d in self.domains:
            r = self._lc("print", f"{d}/{LABEL_PREFIX}{name}")
            if r.returncode == 0:
                return d, r
        return None, None

    def install(self, job: Job) -> str:
        path = self.plist_path(job.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        job.log_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(self.render(job))
        os.replace(tmp, path)
        found, _ = self._find(job.name)
        if found:
            self._lc("bootout", f"{found}/{job.label}")
        errors = []
        for domain in self.domains:
            for attempt in range(3):
                r = self._lc("bootstrap", domain, str(path))
                if r.returncode == 0:
                    return domain
                # bootstrap right after bootout can fail with EIO while launchd
                # finishes tearing the old instance down; retry briefly.
                time.sleep(0.5 * (attempt + 1))
            errors.append(f"{domain}: exit {r.returncode} {(r.stderr or r.stdout).strip()}")
        raise ScheduleError(
            f"launchctl bootstrap of {path} failed in every domain: " + "; ".join(errors)
        )

    def status(self, name: str) -> dict[str, Any]:
        domain, r = self._find(name)
        info: dict[str, Any] = {
            "backend": self.kind,
            "id": f"{domain or self.domains[0]}/{LABEL_PREFIX}{name}",
            "definition": str(self.plist_path(name)),
            "definition_exists": self.plist_path(name).exists(),
            "registered": domain is not None,
        }
        if r is not None:
            info.update(parse_launchctl_print(r.stdout))
        return info

    def run_now(self, name: str) -> None:
        domain, _ = self._find(name)
        if domain is None:
            raise ScheduleError(f"{LABEL_PREFIX}{name} is not registered with launchd")
        r = self._lc("kickstart", f"{domain}/{LABEL_PREFIX}{name}")
        if r.returncode != 0:
            raise ScheduleError(
                f"launchctl kickstart failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}"
            )

    def remove(self, name: str, wait_s: float = 20.0) -> bool:
        domain, _ = self._find(name)
        if domain:
            r = self._lc("bootout", f"{domain}/{LABEL_PREFIX}{name}")
            # A running job is stopped first; launchd lists it until it exits.
            deadline = time.monotonic() + wait_s
            while self._find(name)[0]:
                if time.monotonic() >= deadline:
                    raise ScheduleError(
                        f"launchctl bootout {domain}/{LABEL_PREFIX}{name} did not remove the job "
                        f"(exit {r.returncode}): {(r.stderr or r.stdout).strip()}"
                    )
                time.sleep(0.5)
        path = self.plist_path(name)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return bool(domain) or existed


# -- Windows ---------------------------------------------------------------------


def render_task_xml(job: Job, start_date: dt.date | None = None) -> bytes:
    """Task Scheduler XML, UTF-16 LE with BOM (the encoding Task Scheduler exports)."""
    start = start_date or dt.date(2026, 1, 1)
    command, *rest = job.argv()
    arguments = subprocess.list2cmdline(rest)
    body = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{TASK_NS}">
  <RegistrationInfo>
    <Author>studio-pulse {escape(__version__)}</Author>
    <Description>{escape(job.describe())}</Description>
    <URI>\\{escape(job.task_name)}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.isoformat()}T{job.hour:02d}:{job.minute:02d}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(job.workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    return b"\xff\xfe" + body.replace("\n", "\r\n").encode("utf-16-le")


# Column positions of `schtasks /query /v /fo csv`; the header is localised.
_SCHTASKS_COLS = {"next_run": 2, "state": 3, "last_run": 5, "last_result": 6}


def parse_schtasks_query(text: str) -> dict[str, Any]:
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if len(rows) < 2:
        return {}
    data = rows[1]
    out: dict[str, Any] = {}
    for key, i in _SCHTASKS_COLS.items():
        if i < len(data):
            v = data[i].strip()
            out[key] = int(v) if v.lstrip("-").isdigit() else v
    return out


class Schtasks:
    kind = "schtasks"

    def __init__(self, run: Runner = run_command, *, schtasks: Sequence[str] = ("schtasks",)):
        self.run = run
        self.schtasks = list(schtasks)

    def target(self, name: str) -> str:
        return f"{WIN_FOLDER}\\{name}"

    def render(self, job: Job) -> bytes:
        return render_task_xml(job)

    def install(self, job: Job) -> None:
        job.log_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="pulse-task-", suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.render(job))
            r = self.run([*self.schtasks, "/create", "/tn", job.task_name, "/xml", tmp, "/f"])
        finally:
            Path(tmp).unlink(missing_ok=True)
        if r.returncode != 0:
            raise ScheduleError(
                f"schtasks /create failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}"
            )

    def status(self, name: str) -> dict[str, Any]:
        r = self.run([*self.schtasks, "/query", "/tn", self.target(name), "/v", "/fo", "csv"])
        info: dict[str, Any] = {
            "backend": self.kind,
            "id": "\\" + self.target(name),
            "registered": r.returncode == 0,
        }
        if r.returncode == 0:
            info.update(parse_schtasks_query(r.stdout))
        return info

    def run_now(self, name: str) -> None:
        r = self.run([*self.schtasks, "/run", "/tn", self.target(name)])
        if r.returncode != 0:
            raise ScheduleError(
                f"schtasks /run failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}"
            )

    def remove(self, name: str) -> bool:
        if self.run([*self.schtasks, "/query", "/tn", self.target(name)]).returncode != 0:
            return False
        r = self.run([*self.schtasks, "/delete", "/tn", self.target(name), "/f"])
        if r.returncode != 0:
            raise ScheduleError(
                f"schtasks /delete failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}"
            )
        return True


# -- selection and registry ------------------------------------------------------


def _fake_cmd(tool: str) -> list[str] | None:
    raw = os.environ.get("PULSE_SCHEDULER_CMD")
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise ScheduleError(f"PULSE_SCHEDULER_CMD is not JSON: {exc}") from exc
    v = spec.get(tool)
    return [str(x) for x in v] if v else None


def daily_job(
    cfg: Any, hour: int, minute: int, name: str = DEFAULT_NAME, notify: bool = False
) -> Job:
    """The daily `pulse run` for one config (the CLI's `schedule install` and the Settings page)."""
    return Job(
        name=name,
        hour=hour,
        minute=minute,
        args=["run", "--config", str(cfg.path)] + (["--notify"] if notify else []),
        workdir=cfg.base_dir,
        log_dir=Path(cfg.state_dir) / "logs",
        config=cfg.path,
        state_dir=cfg.state_dir,
    )


def backend(name: str | None = None, run: Runner = run_command) -> Launchd | Schtasks:
    choice = name or os.environ.get("PULSE_SCHEDULER_BACKEND")
    if not choice:
        if sys.platform == "darwin":
            choice = "launchd"
        elif sys.platform == "win32":
            choice = "schtasks"
        else:
            raise ScheduleError(
                "pulse schedule supports macOS (launchd) and Windows (Task Scheduler). "
                "On Linux, run the refresh from cron or a systemd timer."
            )
    if choice == "launchd":
        agents = os.environ.get("PULSE_LAUNCH_AGENTS_DIR")
        return Launchd(
            run,
            launchctl=_fake_cmd("launchctl") or ["launchctl"],
            agents_dir=Path(agents) if agents else None,
        )
    if choice == "schtasks":
        return Schtasks(run, schtasks=_fake_cmd("schtasks") or ["schtasks"])
    raise ScheduleError(f"unknown scheduler backend {choice!r}")


def registry_path(name: str) -> Path:
    return user_state_dir() / "schedules" / f"{check_name(name)}.json"


def save_record(job: Job, backend_kind: str) -> Path:
    from .runstate import write_json_atomic

    rec = {
        "name": job.name,
        "backend": backend_kind,
        "at": f"{job.hour:02d}:{job.minute:02d}",
        "config": str(job.config) if job.config else None,
        "state_dir": str(job.state_dir) if job.state_dir else None,
        "argv": job.argv(),
        "log_file": str(job.log_file),
        "installed_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "version": __version__,
    }
    path = registry_path(job.name)
    write_json_atomic(path, rec)
    return path


def load_record(name: str) -> dict[str, Any] | None:
    from .runstate import read_json

    return read_json(registry_path(name))


def delete_record(name: str) -> None:
    registry_path(name).unlink(missing_ok=True)


def list_records() -> list[dict[str, Any]]:
    from .runstate import read_json

    d = user_state_dir() / "schedules"
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        rec = read_json(p)
        if rec:
            out.append(rec)
    return out
