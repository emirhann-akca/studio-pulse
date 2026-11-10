"""Run lock, heartbeat, history and snapshot."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from studio_pulse import cli, runstate


def test_pid_alive_without_signalling():
    assert runstate.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not runstate.pid_alive(p.pid)
    assert not runstate.pid_alive(0) and not runstate.pid_alive(-5)


def test_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "run.lock"
    with runstate.RunLock(path, command="run") as first:
        assert path.exists() and first.held
        with pytest.raises(runstate.LockedError, match="locked: another pulse refresh"):
            runstate.RunLock(path).acquire()
    assert not path.exists()


def _write_lock(path: Path, **info) -> None:
    data = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "command": "run",
        "started_at": runstate.now_iso(),
        "token": "other",
    }
    data.update(info)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_lock_of_a_dead_process_is_broken(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    path = tmp_path / "run.lock"
    _write_lock(path, pid=p.pid)
    with runstate.RunLock(path) as lock:
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == lock.info["token"]


def test_old_lock_is_broken_but_fresh_live_lock_is_not(tmp_path):
    path = tmp_path / "run.lock"
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=7)).isoformat()
    _write_lock(path, host="another-host", pid=1, started_at=old)
    runstate.RunLock(path).acquire().release()
    _write_lock(path, host="another-host", pid=1)
    with pytest.raises(runstate.LockedError):
        runstate.RunLock(path).acquire()


def test_release_never_removes_someone_elses_lock(tmp_path):
    path = tmp_path / "run.lock"
    lock = runstate.RunLock(path).acquire()
    _write_lock(path, token="replaced")
    lock.release()
    assert path.exists()


def test_recorder_writes_heartbeat_and_history(tmp_path):
    with runstate.RunRecorder(tmp_path, "run") as rec:
        hb = runstate.read_heartbeat(tmp_path)
        assert hb["status"] == "running" and hb["pid"] == os.getpid()
        rec.summary["alerts"] = 2
    hb = runstate.read_heartbeat(tmp_path)
    assert hb["status"] == "success" and hb["last_success"] == hb["last_end"]
    assert hb["summary"] == {"alerts": 2} and hb["duration_s"] >= 0
    with pytest.raises(RuntimeError), runstate.RunRecorder(tmp_path, "run"):
        raise RuntimeError("boom")
    hb2 = runstate.read_heartbeat(tmp_path)
    assert hb2["status"] == "failed" and hb2["error"] == "RuntimeError: boom"
    assert hb2["last_success"] == hb["last_success"]  # kept from the earlier success
    hist = runstate.read_history(tmp_path)
    assert [h["status"] for h in hist] == ["failed", "success"]
    assert not (tmp_path / runstate.LOCK_NAME).exists()


def test_running_heartbeat_of_a_dead_process_reads_as_interrupted(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    runstate.write_json_atomic(
        runstate.heartbeat_path(tmp_path),
        {"status": "running", "pid": p.pid, "host": socket.gethostname()},
    )
    assert runstate.read_heartbeat(tmp_path)["status"] == "interrupted"


def test_snapshot_copies_the_closed_warehouse(tmp_path):
    db = tmp_path / "w.duckdb"
    db.write_bytes(b"x" * 100)
    snap = runstate.make_snapshot(db, tmp_path / "state")
    assert snap.read_bytes() == db.read_bytes()


HOLDER = """
import sys, time
from pathlib import Path
from studio_pulse import runstate
lock = runstate.RunLock(Path(sys.argv[1]) / runstate.LOCK_NAME, command="run").acquire()
print("held", flush=True)
sys.stdin.readline()
lock.release()
"""


def test_second_concurrent_pulse_run_exits_locked(demo_build, tmp_path, capsys):
    """A refresh held by another process makes `pulse run` exit 75 with 'locked'."""
    cfg = demo_build.copy(tmp_path)
    cfg_path = cfg.path
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(cfg.state_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.perf_counter()
        rc = cli.main(["run", "-c", str(cfg_path)])
        err = capsys.readouterr().err
        assert rc == runstate.EXIT_LOCKED == 75
        assert "locked" in err and f"pid {holder.pid}" in err
        assert time.perf_counter() - t0 < 10  # refused at once, nothing was loaded
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=20)
    assert cli.main(["run", "-c", str(cfg_path)]) == 0
