"""Run lock, heartbeat, run history and warehouse snapshot.

Every refresh (`pulse run`, `pulse demo`) goes through `RunRecorder`:

- a lock file stops two refreshes from writing the warehouse at once (DuckDB
  allows a single writer). It is created with O_CREAT | O_EXCL, which works the
  same on Windows and POSIX; a lock whose process is gone, or that is older than
  `stale_after` seconds, is broken;
- `heartbeat.json` records the last start, last success, duration and error. It
  is what `pulse schedule status`, `pulse doctor` and `pulse serve` read;
- `runs.jsonl` keeps one line per finished run (newest last, capped);
- after a successful run the warehouse is copied to `snapshot.duckdb`, which the
  UI reads while a refresh holds the write lock.

All files live in the config's state_dir (default: <warehouse dir>/pulse-state).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__

LOCK_NAME = "run.lock"
HEARTBEAT_NAME = "heartbeat.json"
HISTORY_NAME = "runs.jsonl"
SNAPSHOT_NAME = "snapshot.duckdb"
HISTORY_KEEP = 200
EXIT_LOCKED = 75  # EX_TEMPFAIL: try again later


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.UTC)


def pid_alive(pid: int) -> bool:
    """Whether a process with this id exists. Never signals the process.

    os.kill(pid, 0) is a liveness probe on POSIX only: on Windows it calls
    TerminateProcess, so Windows asks the kernel through OpenProcess instead.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def replace_file(src: Path, dst: Path) -> None:
    # Windows refuses to replace a file another process has open (a reader of
    # the heartbeat, the UI holding the snapshot); those opens are brief.
    for i in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or i == 39:
                raise
            time.sleep(0.05 * (i + 1))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
    replace_file(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class LockedError(RuntimeError):
    def __init__(self, path: Path, holder: dict[str, Any] | None):
        self.path, self.holder = path, holder or {}
        who = (
            f"pid {self.holder.get('pid')} on {self.holder.get('host')}, "
            f"{self.holder.get('command', '?')} since {self.holder.get('started_at', '?')}"
            if holder
            else "unknown holder"
        )
        super().__init__(f"locked: another pulse refresh holds {path} ({who})")


class RunLock:
    def __init__(self, path: Path, *, command: str = "run", stale_after: float = 6 * 3600):
        self.path = Path(path)
        self.stale_after = stale_after
        self.info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "command": command,
            "started_at": now_iso(),
            "token": secrets.token_hex(8),
        }
        self.held = False

    def read(self, path: Path | None = None) -> dict[str, Any] | None:
        return read_json(path or self.path)

    def is_stale(self, holder: dict[str, Any] | None) -> bool:
        if holder is None:
            # Empty or half-written: the creator may be between open and write.
            try:
                return time.time() - self.path.stat().st_mtime > 60
            except FileNotFoundError:
                return True
        if holder.get("host") == socket.gethostname() and not pid_alive(int(holder.get("pid", 0))):
            return True
        started = parse_iso(holder.get("started_at"))
        if started is None:
            return True
        return (dt.datetime.now(dt.UTC) - started).total_seconds() > self.stale_after

    def _break(self, holder: dict[str, Any] | None) -> None:
        aside = self.path.with_name(f"{self.path.name}.stale-{os.getpid()}-{secrets.token_hex(3)}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return  # someone else broke it first
        if self.read(aside) != holder:
            # A new lock appeared between our read and the rename: put it back.
            try:
                os.link(aside, self.path)
            except OSError:
                pass
        aside.unlink(missing_ok=True)

    def acquire(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = self.read()
                if attempt == 0 and self.is_stale(holder):
                    self._break(holder)
                    continue
                raise LockedError(self.path, holder) from None
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.info, f)
            self.held = True
            return self
        raise LockedError(self.path, self.read())

    def release(self) -> None:
        if not self.held:
            return
        holder = self.read()
        if holder and holder.get("token") == self.info["token"]:
            self.path.unlink(missing_ok=True)
        self.held = False

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


# -- heartbeat and history ------------------------------------------------------


def heartbeat_path(state_dir: Path) -> Path:
    return Path(state_dir) / HEARTBEAT_NAME


def read_heartbeat(state_dir: Path) -> dict[str, Any] | None:
    hb = read_json(heartbeat_path(state_dir))
    if hb and hb.get("status") == "running":
        pid = int(hb.get("pid") or 0)
        if hb.get("host") == socket.gethostname() and not pid_alive(pid):
            hb["status"] = "interrupted"  # the process died without finishing
    return hb


def read_history(state_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    path = Path(state_dir) / HISTORY_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def _append_history(state_dir: Path, record: dict[str, Any]) -> None:
    path = Path(state_dir) / HISTORY_NAME
    lines: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    lines = [*lines[-(HISTORY_KEEP - 1) :], json.dumps(record, sort_keys=True, default=str)]
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    replace_file(tmp, path)


def snapshot_path(state_dir: Path) -> Path:
    return Path(state_dir) / SNAPSHOT_NAME


def make_snapshot(warehouse: Path, state_dir: Path) -> Path | None:
    """Copy the (closed) warehouse file to the UI's fallback snapshot."""
    warehouse = Path(warehouse)
    if not warehouse.exists():
        return None
    dst = snapshot_path(state_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    shutil.copyfile(warehouse, tmp)
    replace_file(tmp, dst)
    return dst


class RunRecorder:
    """Lock + heartbeat around one refresh.

    with RunRecorder(cfg.state_dir, "run") as rec:
        ...
        rec.summary.update(alerts=3)
        rec.fail("2 data-quality check(s) failed")   # optional
    """

    def __init__(self, state_dir: Path, command: str, *, stale_after: float = 6 * 3600):
        self.state_dir = Path(state_dir)
        self.command = command
        self.lock = RunLock(self.state_dir / LOCK_NAME, command=command, stale_after=stale_after)
        self.summary: dict[str, Any] = {}
        self.error: str | None = None
        self._t0 = 0.0
        self._started = ""

    def __enter__(self) -> RunRecorder:
        self.lock.acquire()
        self._t0 = time.perf_counter()
        self._started = now_iso()
        hb = read_json(heartbeat_path(self.state_dir)) or {}
        hb.update(
            status="running",
            command=self.command,
            last_start=self._started,
            pid=os.getpid(),
            host=socket.gethostname(),
            version=__version__,
        )
        write_json_atomic(heartbeat_path(self.state_dir), hb)
        return self

    def fail(self, error: str) -> None:
        self.error = error

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc is not None and self.error is None:
                self.error = f"{exc_type.__name__}: {exc}"
            ended = now_iso()
            ok = self.error is None
            hb = read_json(heartbeat_path(self.state_dir)) or {}
            hb.update(
                status="success" if ok else "failed",
                command=self.command,
                last_start=self._started,
                last_end=ended,
                duration_s=round(time.perf_counter() - self._t0, 2),
                error=self.error,
                pid=os.getpid(),
                host=socket.gethostname(),
                version=__version__,
                summary=self.summary,
            )
            if ok:
                hb["last_success"] = ended
            else:
                hb["last_failure"] = ended
                hb["last_error"] = self.error
            write_json_atomic(heartbeat_path(self.state_dir), hb)
            _append_history(
                self.state_dir,
                {
                    "command": self.command,
                    "start": self._started,
                    "end": ended,
                    "duration_s": hb["duration_s"],
                    "status": hb["status"],
                    "error": self.error,
                    "summary": self.summary,
                },
            )
        finally:
            self.lock.release()
