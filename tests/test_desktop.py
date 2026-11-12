"""The desktop app: window lookup, heartbeat, lifecycle, single instance, launchers."""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from studio_pulse import desktop


def minimal_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "pulse.yaml"
    cfg.write_text("apps:\n  - {id: demo_x, name: Demo X}\nwarehouse: w.duckdb\n", encoding="utf-8")
    return cfg


# -- the window ---------------------------------------------------------------------------
def test_find_browser_prefers_edge_on_windows_and_chrome_on_macos(tmp_path):
    env = {"ProgramFiles(x86)": r"C:\PF86", "ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\U\L"}
    edge = str(Path(r"C:\PF86") / r"Microsoft\Edge\Application\msedge.exe")
    chrome = str(Path(r"C:\U\L") / r"Google\Chrome\Application\chrome.exe")
    have = {edge, chrome}
    assert desktop.find_browser("win32", env, exists=have.__contains__) == edge
    assert desktop.find_browser("win32", env, exists={chrome}.__contains__) == chrome
    assert desktop.find_browser("win32", env, exists=lambda p: False) is None

    home = tmp_path
    mac_edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    mac_chrome = str(home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    got = desktop.find_browser("darwin", {}, exists={mac_edge, mac_chrome}.__contains__, home=home)
    assert got == mac_chrome  # Chrome first, in /Applications or ~/Applications

    which = {"chromium": "/usr/bin/chromium"}.get
    assert desktop.find_browser("linux", {}, which=which) == "/usr/bin/chromium"
    assert desktop.find_browser("linux", {"PULSE_APP_BROWSER": "/x/b"}, which=which) == "/x/b"


def test_window_command_is_an_app_window_with_its_own_profile(tmp_path):
    cmd = desktop.window_command("/b/chrome", "http://127.0.0.1:5/", tmp_path / "w")
    assert cmd[0] == "/b/chrome" and "--app=http://127.0.0.1:5/" in cmd
    assert f"--user-data-dir={tmp_path / 'w'}" in cmd and "--no-first-run" in cmd


# -- heartbeat ----------------------------------------------------------------------------
async def _page(scope, receive, send):
    body = {
        "/": b"<!doctype html><html><body><h1>x</h1></body></html>",
        "/frag": b"<tr><td>htmx fragment</td></tr>",
        "/api": b'{"ok":true}',
    }[scope["path"]]
    ctype = b"application/json" if scope["path"] == "/api" else b"text/html; charset=utf-8"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]})  # fmt: skip
    await send({"type": "http.response.body", "body": body[:10], "more_body": True})
    await send({"type": "http.response.body", "body": body[10:]})


def test_heartbeat_adds_its_script_to_full_pages_only():
    now = [100.0]
    beat = desktop.Heartbeat(_page, clock=lambda: now[0])
    c = TestClient(beat, base_url="http://127.0.0.1")
    page = c.get("/")
    assert desktop.HEARTBEAT_TAG.decode() in page.text and page.text.endswith("</body></html>")
    assert int(page.headers["content-length"]) == len(page.content)
    assert desktop.HEARTBEAT_TAG.decode() not in c.get("/frag").text
    assert c.get("/api").json() == {"ok": True}
    js = c.get("/__app/heartbeat.js")
    assert js.status_code == 200 and "/__app/ping" in js.text and "/__app/bye" in js.text
    assert not beat.pinged
    now[0] = 130.0
    assert c.get("/__app/ping").status_code == 200
    assert beat.pinged and beat.last_ping == 130.0
    now[0] = 140.0
    assert c.post("/__app/bye").status_code == 200 and beat.bye_at == 140.0
    # a ping in the same clock tick (Windows: ~16 ms) still cancels the goodbye: order decides
    assert c.get("/__app/ping").status_code == 200 and beat.bye_at is None


# -- config and state ---------------------------------------------------------------------
def test_pick_config_order(tmp_path):
    state = tmp_path / "state"
    explicit = minimal_config(tmp_path)
    assert desktop.pick_config(str(explicit), state, env={}) == explicit.resolve()
    assert desktop.pick_config(None, state, env={}) == desktop.demo_config(state)
    assert (
        desktop.pick_config(None, state, env={"PULSE_CONFIG": str(explicit)}) == explicit.resolve()
    )
    desktop._write_state(state, {"config": str(explicit)})
    assert desktop.pick_config(None, state, env={}) == explicit


# -- one launch ---------------------------------------------------------------------------
class FakeWindow:
    """Stands in for the browser: a process that stays up; records the URL it was given."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.procs: list[subprocess.Popen] = []

    def __call__(self, url: str) -> subprocess.Popen:
        self.urls.append(url)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.procs.append(p)
        return p


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310
            return r.status
    except OSError:
        return 0


def _run_in_thread(**kw) -> tuple[threading.Thread, list[int]]:
    rc: list[int] = []
    t = threading.Thread(target=lambda: rc.append(desktop.run(**kw)), daemon=True)
    t.start()
    return t, rc


def _wait_url(state: Path, timeout: float = 20) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = desktop._read_state(state).get("url")
        if url and _get(url + "healthz") == 200:
            return url
        time.sleep(0.05)
    raise AssertionError("the app did not start")


def test_closing_the_last_page_stops_the_server_and_the_window(tmp_path):
    state, win = tmp_path / "state", FakeWindow()
    t, rc = _run_in_thread(config=str(minimal_config(tmp_path)), state=state, launch=win,
                           bye_grace_s=0.5, tick_s=0.05, log=lambda m: None)  # fmt: skip
    url = _wait_url(state)
    assert win.urls == [url + "studio"]
    assert _get(url) == 200 and _get(url + "__app/ping") == 200
    urllib.request.urlopen(urllib.request.Request(url + "__app/bye", method="POST"), timeout=5)  # noqa: S310
    t.join(15)
    assert not t.is_alive() and rc == [0]
    assert _get(url + "healthz") == 0  # the port is closed
    win.procs[0].wait(10)  # the window process was ended
    assert "url" not in desktop._read_state(state)


def test_a_reload_after_bye_keeps_the_server(tmp_path):
    state, win = tmp_path / "state", FakeWindow()
    t, _ = _run_in_thread(config=str(minimal_config(tmp_path)), state=state, launch=win,
                          bye_grace_s=1.0, gone_after_s=5.0, tick_s=0.05, log=lambda m: None)  # fmt: skip
    url = _wait_url(state)
    urllib.request.urlopen(urllib.request.Request(url + "__app/bye", method="POST"), timeout=5)  # noqa: S310
    assert _get(url + "__app/ping") == 200  # the next page loaded
    time.sleep(1.5)
    assert t.is_alive() and _get(url + "healthz") == 200
    t.join(15)  # then nothing pings: gone after 5 s
    assert not t.is_alive()


def test_a_second_launch_opens_a_window_on_the_running_server(tmp_path):
    state, first, second = tmp_path / "state", FakeWindow(), FakeWindow()
    t, _ = _run_in_thread(config=str(minimal_config(tmp_path)), state=state, launch=first,
                          gone_after_s=6.0, tick_s=0.05, log=lambda m: None)  # fmt: skip
    url = _wait_url(state)
    assert desktop.running_url(state) == url
    assert desktop.run(None, state=state, launch=second, log=lambda m: None) == 0
    assert second.urls == [url + "studio"]  # no second server
    for p in second.procs:
        p.kill()
    t.join(20)
    assert not t.is_alive()


def test_first_launch_builds_the_demo_behind_a_preparing_page(tmp_path, capsys):
    state = tmp_path / "state"
    assert desktop.check(None, state=state, scale=0.1) == 0
    out = capsys.readouterr().out
    assert "ok   first page (preparing)" in out and "check: passed" in out
    assert desktop.demo_config(state).exists()


# -- launchers ----------------------------------------------------------------------------
def test_macos_bundle(tmp_path):
    program = tmp_path / "bin" / "studio-pulse"
    app = desktop.mac_bundle(
        tmp_path / "Applications", program, desktop._asset("studio-pulse.icns")
    )
    assert app.name == "Studio Pulse.app"
    with open(app / "Contents" / "Info.plist", "rb") as fh:
        info = plistlib.load(fh)
    assert (
        info["CFBundleExecutable"] == "studio-pulse"
        and info["CFBundleIconFile"] == "studio-pulse.icns"
    )
    assert info["CFBundlePackageType"] == "APPL"
    script = (app / "Contents" / "MacOS" / "studio-pulse").read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\n") and f'exec "{program}" "$@"' in script
    assert (app / "Contents" / "Resources" / "studio-pulse.icns").stat().st_size > 10_000
    if sys.platform != "win32":
        assert (app / "Contents" / "MacOS" / "studio-pulse").stat().st_mode & 0o111


def test_linux_desktop_entry(tmp_path):
    entry = desktop.linux_entry(
        tmp_path / "applications", Path("/opt/bin/studio-pulse"), Path("/i.png")
    )
    text = entry.read_text(encoding="utf-8")
    assert "Name=Studio Pulse" in text and 'Exec="/opt/bin/studio-pulse"' in text
    assert "Terminal=false" in text and "Icon=/i.png" in text


def test_assets_ship_in_the_package():
    for name in ("studio-pulse.ico", "studio-pulse.icns", "studio-pulse.png"):
        assert desktop._asset(name).stat().st_size > 1000


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcut (WScript.Shell)")
def test_windows_shortcut_points_at_the_launcher(tmp_path):
    lnk = desktop.windows_shortcut(
        tmp_path / "Studio Pulse.lnk", Path(sys.executable), desktop._asset("studio-pulse.ico")
    )
    target = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()  # fmt: skip
    assert Path(target) == Path(sys.executable)


def test_state_file_is_json(tmp_path):
    desktop._write_state(tmp_path, {"url": "http://127.0.0.1:1/"})
    assert json.loads((tmp_path / "app.json").read_text(encoding="utf-8"))["url"].endswith(":1/")
