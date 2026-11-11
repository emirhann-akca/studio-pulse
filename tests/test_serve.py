"""Local web UI: pages, run-now protection, and read-only access next to a writer."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from studio_pulse import cli, runstate, warehouse
from studio_pulse.serve.app import create_app, open_db

HOST = {"host": "127.0.0.1:8765"}


@pytest.fixture(scope="module")
def ui_module(demo_build, tmp_path_factory):
    cfg = demo_build.copy(tmp_path_factory.mktemp("ui"))
    assert cli.main(["run", "-c", str(cfg.path)]) == 0  # site, heartbeat, snapshot
    started: list[str] = []

    def launcher(c):
        started.append(str(c.path))
        return 4242

    app = create_app(cfg, token="t0ken", launcher=launcher)
    return cfg, TestClient(app, base_url="http://127.0.0.1:8765"), started


@pytest.fixture()
def ui(ui_module):
    cfg, client, started = ui_module
    hb = runstate.read_heartbeat(cfg.state_dir)
    started.clear()
    yield ui_module
    runstate.write_json_atomic(runstate.heartbeat_path(cfg.state_dir), hb)


def test_every_page_renders(ui):
    cfg, client, _ = ui
    for path, marker in [
        ("/", "Run refresh now"),
        ("/health", "Data quality"),
        ("/alerts", "History"),
        ("/fragments/status", "Last refresh"),
        ("/dashboard/index.html", "Data quality"),
        ("/dashboard/apps/mossy_match.html", "Mossy Match"),
        ("/static/htmx.min.js", "htmx"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert marker in r.text, path
    health = client.get("/health").text
    assert "source_freshness" in health and "success" in health
    status = client.get("/api/status").json()
    assert status["database"] == "live" and status["heartbeat"]["status"] == "success"
    assert status["checks"]["errors"] == 0


def test_run_now_needs_the_page_token(ui):
    _, client, started = ui
    assert client.post("/run").status_code == 403
    assert client.post("/run", headers={"X-Pulse-Token": "wrong"}).status_code == 403
    r = client.post("/run", headers={"X-Pulse-Token": "t0ken", "Origin": "https://evil.example"})
    assert r.status_code == 403 and not started
    r = client.post("/run", headers={"X-Pulse-Token": "t0ken"})
    assert r.status_code == 202 and "pid 4242" in r.text and len(started) == 1


def test_run_now_refuses_while_a_refresh_holds_the_lock(ui):
    cfg, client, started = ui
    with runstate.RunLock(cfg.state_dir / runstate.LOCK_NAME):
        r = client.post("/run", headers={"X-Pulse-Token": "t0ken"})
    assert r.status_code == 409 and "already running" in r.text and not started


def test_foreign_host_header_is_rejected(ui):
    _, client, _ = ui
    assert client.get("/", headers={"host": "attacker.example:8765"}).status_code == 400
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200
    assert client.get("/", headers={"host": "[::1]:8765"}).status_code == 200


HOLD_RW = """
import sys, duckdb
con = duckdb.connect(sys.argv[1])
print("rw", flush=True)
sys.stdin.readline()
con.close()
"""


def test_ui_reads_snapshot_while_a_writer_holds_the_warehouse(ui):
    cfg, client, _ = ui
    writer = subprocess.Popen(
        [sys.executable, "-c", HOLD_RW, str(cfg.warehouse)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert writer.stdout.readline().strip() == "rw"
        con, source = open_db(cfg)
        assert source == "snapshot"
        con.close()
        page = client.get("/health")
        assert page.status_code == 200 and "snapshot of the last good refresh" in page.text
        assert client.get("/api/status").json()["database"] == "snapshot"
    finally:
        writer.stdin.write("\n")
        writer.stdin.flush()
        writer.wait(timeout=20)
    assert client.get("/api/status").json()["database"] == "live"


def test_ui_never_keeps_the_warehouse_open(ui):
    cfg, client, _ = ui
    assert client.get("/health").status_code == 200
    # A writer in another process can take the file straight away.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, duckdb; duckdb.connect(sys.argv[1]).close()",
            str(cfg.warehouse),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert probe.returncode == 0, probe.stderr
    con = warehouse.connect(cfg.warehouse, wait_s=0)
    con.close()


def test_running_refresh_is_served_from_the_snapshot(ui):
    cfg, client, _ = ui
    hb = runstate.read_heartbeat(cfg.state_dir)
    hb.update(status="running", pid=__import__("os").getpid())
    runstate.write_json_atomic(runstate.heartbeat_path(cfg.state_dir), hb)
    assert client.get("/api/status").json()["database"] == "snapshot"
    assert "running" in client.get("/fragments/status").text


def test_serve_check_command(ui, capsys):
    cfg, _, _ = ui
    assert cli.main(["serve", "-c", str(cfg.path), "--check"]) == 0
    out = capsys.readouterr().out
    assert "UI check passed." in out and "database read by the UI: live" in out
    for page in ("dashboard/ua.html", "dashboard/experiments.html", "dashboard/apps/"):
        assert f"GET /{page}" in out
