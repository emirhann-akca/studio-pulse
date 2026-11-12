"""Studio Pulse as a desktop app: a double-click opens the local UI in its own window.

`studio-pulse` (a GUI script: no console window on Windows) and `pulse app`:

1. Config: `--config`, else the one remembered in <state>/app.json, else $PULSE_CONFIG,
   else <state>/demo/work/pulse.yaml. On the very first launch that demo is built while
   the window already shows a "preparing" page (about half a minute).
2. One instance: when app.json names a server that still answers /healthz, the window
   opens on it instead of starting a second server.
3. The server binds 127.0.0.1 on a free port inside this process (the same app as
   `pulse serve`).
4. The window: Edge, Chrome, Brave or Chromium in app mode (`--app=URL`, a profile of
   its own under <state>/window), so it has no tabs or address bar. Without one of them
   the default browser opens a tab. Every full page loads /__app/heartbeat.js, which
   pings the server; when no page has pinged for GONE_AFTER_S (the window was closed),
   the server stops and the browser process this app started is ended. A closed window
   on Windows and Linux ends that process at once, which stops the server sooner.

`pulse app --install-shortcut` puts a launcher where people look for apps: Desktop and
Start menu shortcuts on Windows, ~/Applications/Studio Pulse.app on macOS (Launchpad
and Spotlight find it), ~/.local/share/applications/studio-pulse.desktop on Linux.
The installers call it; PULSE_NO_SHORTCUT=1 skips it.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

APP_NAME = "Studio Pulse"
GUI_SCRIPT = "studio-pulse"
BUNDLE_ID = "io.github.studio-pulse.app"
PING_EVERY_S = 15
GONE_AFTER_S = 120  # background tabs are throttled to about one timer a minute
BYE_GRACE_S = 20  # after a page says goodbye: longer than PING_EVERY_S (other windows, reloads)
DEMO_SCALE = 0.3

HEARTBEAT_JS = (
    "(function(){var down=false;function ping(){fetch('/__app/ping',{cache:'no-store'})"
    ".then(function(){if(down){location.reload();}})"
    ".catch(function(){if(down)return;down=true;var b=document.createElement('div');"
    "b.setAttribute('role','alert');b.style.cssText='position:fixed;left:0;right:0;bottom:0;"
    "padding:12px 16px;background:#7f1d1d;color:#fff;font:14px system-ui;z-index:99999';"
    "b.textContent='Studio Pulse kapandı / closed: simgeden yeniden açın / reopen it from "
    "its icon.';document.body.appendChild(b);});}ping();setInterval(ping,"
    + str(PING_EVERY_S * 1000)
    + ");addEventListener('pagehide',function(){navigator.sendBeacon('/__app/bye');});})();"
).encode()
HEARTBEAT_TAG = b'<script src="/__app/heartbeat.js" defer></script>'

PREPARING_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="2"><title>Studio Pulse</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f172a;
color:#e2e8f0;font:16px system-ui,sans-serif}main{max-width:34rem;padding:2rem;text-align:center}
h1{font-size:1.4rem}p{color:#94a3b8;line-height:1.5}</style></head><body><main>
<h1>Studio Pulse hazırlanıyor / getting ready</h1>
<p>İlk açılış: üç kurgusal oyunun demo verisi kuruluyor (yaklaşık yarım dakika).
Kendi verin için pulse.local.yaml hazırlayıp <code>pulse app --config</code> ile seç.</p>
<p>First launch: building demo data for three fictional apps (about half a minute).</p>
{error}</main></body></html>"""


# -- where things live ------------------------------------------------------------------
def state_dir() -> Path:
    from .schedule import user_state_dir

    return user_state_dir()


def _state_file(state: Path) -> Path:
    return state / "app.json"


def _read_state(state: Path) -> dict[str, Any]:
    try:
        return json.loads(_state_file(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: Path, data: dict[str, Any]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    tmp = _state_file(state).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _state_file(state))


def demo_config(state: Path) -> Path:
    return state / "demo" / "work" / "pulse.yaml"


def pick_config(explicit: str | None, state: Path, env: Mapping[str, str] | None = None) -> Path:
    """The config to open; the demo's path when there is none yet (it may not exist)."""
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit).expanduser().resolve()
    remembered = _read_state(state).get("config")
    if remembered and Path(remembered).exists():
        return Path(remembered)
    if env.get("PULSE_CONFIG") and Path(env["PULSE_CONFIG"]).exists():
        return Path(env["PULSE_CONFIG"]).resolve()
    return demo_config(state)


def build_demo(state: Path, scale: float = DEMO_SCALE) -> Path:
    from .cli import main as cli_main

    base = state / "demo"
    rc = cli_main(
        [
            "demo",
            "--scale",
            str(scale),
            "--out",
            str(base / "site"),
            "--workdir",
            str(base / "work"),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"pulse demo exited {rc}")
    return demo_config(state)


# -- the window -------------------------------------------------------------------------
def _windows_candidates(env: Mapping[str, str]) -> list[str]:
    roots = [env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")]
    rel = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
        r"Chromium\Application\chrome.exe",
    ]
    return [str(Path(r) / p) for p in rel for r in roots if r]


def _mac_candidates(home: Path) -> list[str]:
    apps = [
        ("Google Chrome", "Google Chrome"),
        ("Microsoft Edge", "Microsoft Edge"),
        ("Brave Browser", "Brave Browser"),
        ("Chromium", "Chromium"),
    ]
    out = []
    for bundle, exe in apps:
        for root in (Path("/Applications"), home / "Applications"):
            out.append(str(root / f"{bundle}.app" / "Contents" / "MacOS" / exe))
    return out


LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)


def find_browser(
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> str | None:
    """A Chromium-family browser that can open an app window, or None."""
    env = os.environ if env is None else env
    if env.get("PULSE_APP_BROWSER"):
        return env["PULSE_APP_BROWSER"]
    if platform == "win32":
        return next((c for c in _windows_candidates(env) if exists(c)), None)
    if platform == "darwin":
        return next((c for c in _mac_candidates(home or Path.home()) if exists(c)), None)
    return next((p for p in (which(n) for n in LINUX_BROWSERS) if p), None)


def window_command(browser: str, url: str, profile: Path) -> list[str]:
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1320,880",
    ]


# -- the server -------------------------------------------------------------------------
class Switch:
    """ASGI app whose target can change (the preparing page, then the UI)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


class Heartbeat:
    """Serves /__app/ping and /__app/heartbeat.js; adds the script to every full page."""

    def __init__(self, app: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.clock = clock
        self.last_ping = clock()
        self.pinged = False
        self.bye_at: float | None = None  # a page was closed or left

    async def _plain(self, send: Any, status: int, body: bytes, ctype: bytes) -> None:
        headers = [(b"content-type", ctype), (b"cache-control", b"no-store")]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/__app/ping":
            self.last_ping, self.pinged = self.clock(), True
            await self._plain(send, 200, b"ok", b"text/plain")
            return
        if path == "/__app/bye":
            self.bye_at = self.clock()
            await self._plain(send, 200, b"ok", b"text/plain")
            return
        if path == "/__app/heartbeat.js":
            await self._plain(send, 200, HEARTBEAT_JS, b"text/javascript; charset=utf-8")
            return
        held: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def wrapped(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                ctype = dict((k.lower(), v) for k, v in msg.get("headers", [])).get(
                    b"content-type", b""
                )
                if ctype.startswith(b"text/html"):
                    held["start"] = msg
                    return
                await send(msg)
                return
            if msg["type"] == "http.response.body" and "start" in held:
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = b"".join(chunks)
                low = body.lower()
                i = low.rfind(b"</body>")
                if i >= 0 and HEARTBEAT_TAG not in body:  # full pages only, not htmx fragments
                    body = body[:i] + HEARTBEAT_TAG + body[i:]
                start = held["start"]
                headers = [
                    (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(msg)

        await self.app(scope, receive, wrapped)


async def _preparing_app(scope: dict, receive: Any, send: Any, error: str = "") -> None:
    if scope["type"] != "http":
        return
    if scope.get("path") == "/healthz":
        body = b'{"ok":true,"preparing":true}'
        ctype = b"application/json"
    else:
        note = f"<p style='color:#fca5a5'>{error}</p>" if error else ""
        body = PREPARING_HTML.replace("{error}", note).encode()
        ctype = b"text/html; charset=utf-8"
    headers = [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (127.0.0.1)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (OSError, ValueError):
        return 0, b""


def running_url(state: Path) -> str | None:
    """The URL of a live instance recorded in app.json, if its /healthz answers."""
    url = _read_state(state).get("url")
    if not url:
        return None
    code, body = _get(url + "healthz", timeout=1.5)
    return url if code == 200 and b'"ok"' in body else None


class Running:
    """The server thread, its URL, and how to stop it."""

    def __init__(self, server: Any, thread: threading.Thread, url: str, beat: Heartbeat) -> None:
        self.server, self.thread, self.url, self.beat = server, thread, url, beat

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


def start_server(app: Any) -> Running:
    from .serve.server import _server, bind

    beat = Heartbeat(app)
    sock = bind("127.0.0.1", 0)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    server = _server(beat, sock)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("the local server did not start")
    return Running(server, thread, url, beat)


def _ui_app(config: Path) -> Any:
    from .config import load_config
    from .serve.app import create_app

    return create_app(load_config(str(config)))


# -- one launch -------------------------------------------------------------------------
def run(
    config: str | None = None,
    *,
    state: Path | None = None,
    open_window: bool = True,
    launch: Callable[[str], subprocess.Popen | None] | None = None,
    gone_after_s: float = GONE_AFTER_S,
    bye_grace_s: float = BYE_GRACE_S,
    tick_s: float = 1.0,
    demo_scale: float = DEMO_SCALE,
    log: Callable[[str], Any] = print,
) -> int:
    """Open the app; return when its window is gone (or right away with open_window=False)."""
    state = state or state_dir()
    state.mkdir(parents=True, exist_ok=True)
    launch = launch or (lambda url: open_window_at(url, state))
    if config is None:
        url = running_url(state)
        if url:
            log(f"{APP_NAME} is already running at {url}; opening a window on it")
            if open_window:
                launch(url)
            return 0
    cfg_path = pick_config(config, state)
    switch = Switch(_preparing_app)
    running = start_server(switch)
    log(f"{APP_NAME}: {running.url}  (config {cfg_path})")
    _write_state(
        state,
        {
            "url": running.url,
            "pid": os.getpid(),
            "config": str(cfg_path) if config else _read_state(state).get("config"),
            "started": dt.datetime.now().isoformat(timespec="seconds"),
        },
    )
    ready = threading.Event()
    failure: list[str] = []

    def prepare() -> None:
        try:
            path = cfg_path if cfg_path.exists() else build_demo(state, demo_scale)
            switch.app = _ui_app(path)
        except Exception as exc:  # the window says what went wrong instead of hanging
            traceback.print_exc()
            failure.append(f"{type(exc).__name__}: {exc}")

            async def failed(scope: dict, receive: Any, send: Any) -> None:
                await _preparing_app(scope, receive, send, error=failure[0])

            switch.app = failed
        finally:
            ready.set()

    threading.Thread(target=prepare, daemon=True).start()
    proc = launch(running.url) if open_window else None
    try:
        if not open_window:  # serve until Ctrl+C
            ready.wait()
            if failure:
                return 1
            log(f"{APP_NAME}: ready at {running.url} (Ctrl+C to stop)")
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    time.sleep(1.0)
            return 0
        beat = running.beat
        while True:
            time.sleep(tick_s)
            now = time.monotonic()
            if proc is not None and proc.poll() is not None and beat.pinged:
                # the dedicated window process ended: its window was closed
                if now - beat.last_ping > PING_EVERY_S * 2:
                    break
            if beat.bye_at is not None and beat.last_ping < beat.bye_at:
                if now - beat.bye_at > bye_grace_s:  # the last page left and none came back
                    break
            if now - beat.last_ping > gone_after_s:
                break
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
        running.stop()
        data = _read_state(state)
        if data.get("url") == running.url:
            data.pop("url", None)
            data.pop("pid", None)
            _write_state(state, data)
        log(f"{APP_NAME}: stopped")


def open_window_at(url: str, state: Path) -> subprocess.Popen | None:
    browser = find_browser()
    if browser:
        profile = state / "window"
        profile.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(  # noqa: S603 (a local browser and our own URL)
            window_command(browser, url, profile),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    import webbrowser

    webbrowser.open(url)
    return None


def check(config: str | None = None, *, state: Path | None = None, scale: float = 0.2) -> int:
    """Start, wait until the UI is ready (building the demo if needed), load pages, stop."""
    state = state or state_dir()
    cfg_path = pick_config(config, state)
    switch = Switch(_preparing_app)
    running = start_server(switch)
    try:
        code, body = _get(running.url, timeout=10)
        if code != 200:
            print(f"FAIL first page answered {code}")
            return 1
        first = "preparing" if "hazırlanıyor" in body.decode("utf-8", "replace") else "ui"
        print(f"ok   first page ({first})")
        path = cfg_path if cfg_path.exists() else build_demo(state, scale)
        switch.app = _ui_app(path)
        failures = 0
        for page, needle in (("healthz", b'"ok":true'), ("", b"Run refresh now"), ("insights", b"Insights"),
                             ("__app/heartbeat.js", b"__app/ping")):  # fmt: skip
            code, body = _get(running.url + page, timeout=30)
            ok = code == 200 and needle in body
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} /{page} ({code})")
        code, body = _get(running.url, timeout=30)
        if HEARTBEAT_TAG not in body:
            print("FAIL the heartbeat script is not on the page")
            failures += 1
        else:
            print("ok   heartbeat script on the page")
        code, _ = _get(running.url + "__app/ping")
        if not (code == 200 and running.beat.pinged):
            print("FAIL /__app/ping")
            failures += 1
        print(
            f"{APP_NAME} check: {'passed' if not failures else f'{failures} failure(s)'} ({path})"
        )
        return 1 if failures else 0
    finally:
        running.stop()


# -- shortcuts --------------------------------------------------------------------------
def launcher_program() -> Path:
    """The installed GUI script next to this interpreter (the tool's own venv)."""
    exe_dir = Path(sys.executable).parent
    name = f"{GUI_SCRIPT}.exe" if sys.platform == "win32" else GUI_SCRIPT
    for c in (exe_dir / name, exe_dir / "Scripts" / name):
        if c.exists():
            return c
    found = shutil.which(GUI_SCRIPT)
    if found:
        return Path(found)
    raise FileNotFoundError(f"{name} not found next to {sys.executable}; reinstall studio-pulse")


def _asset(name: str) -> Path:
    return Path(str(resources.files("studio_pulse") / "desktop_assets" / name))


def mac_bundle(apps_dir: Path, program: Path, icns: Path) -> Path:
    """~/Applications/Studio Pulse.app: Info.plist, an exec script and the icon."""
    app = apps_dir / f"{APP_NAME}.app"
    macos, res = app / "Contents" / "MacOS", app / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": GUI_SCRIPT,
        "CFBundleIconFile": "studio-pulse.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": _version(),
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    script = macos / GUI_SCRIPT
    quoted = str(program).replace('"', '\\"')
    script.write_text(f'#!/bin/sh\nexec "{quoted}" "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    shutil.copyfile(icns, res / "studio-pulse.icns")
    return app


def linux_entry(apps_dir: Path, program: Path, png: Path) -> Path:
    apps_dir.mkdir(parents=True, exist_ok=True)
    path = apps_dir / "studio-pulse.desktop"
    path.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nComment=App portfolio data, live players, insights and market radar\n"
        f'Exec="{program}"\nIcon={png}\nTerminal=false\nCategories=Development;Office;\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


WIN_SHORTCUT_PS = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PULSE_LNK); "
    "$s.TargetPath = $env:PULSE_LNK_TARGET; $s.IconLocation = $env:PULSE_LNK_ICON; "
    "$s.WorkingDirectory = $env:PULSE_LNK_DIR; $s.Description = $env:PULSE_LNK_DESC; $s.Save()"
)


def windows_shortcut(lnk: Path, program: Path, ico: Path) -> Path:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PULSE_LNK": str(lnk),
        "PULSE_LNK_TARGET": str(program),
        "PULSE_LNK_ICON": f"{ico},0",
        "PULSE_LNK_DIR": str(Path.home()),
        "PULSE_LNK_DESC": "Studio Pulse: data, live players, insights, market radar",
    }
    subprocess.run(  # noqa: S603 (fixed script; values travel in the environment)
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_SHORTCUT_PS],
        check=True,
        env=env,
        capture_output=True,
    )
    return lnk


def _windows_dirs() -> list[Path]:
    """Desktop (the shell's real path, OneDrive-aware) and the Start menu Programs folder."""
    out = []
    for folder in ("Desktop", "Programs"):
        res = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"[Environment]::GetFolderPath('{folder}')"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        p = res.stdout.strip()
        if p:
            out.append(Path(p))
    return out


def install_shortcuts(
    *, platform: str = sys.platform, home: Path | None = None, program: Path | None = None
) -> list[Path]:
    home = home or Path.home()
    program = program or launcher_program()
    if platform == "darwin":
        return [mac_bundle(home / "Applications", program, _asset("studio-pulse.icns"))]
    if platform == "win32":
        # the icon is copied out of the package so the shortcut survives reinstalls
        icons = state_dir() / "icons"
        icons.mkdir(parents=True, exist_ok=True)
        ico = icons / "studio-pulse.ico"
        shutil.copyfile(_asset("studio-pulse.ico"), ico)
        return [windows_shortcut(d / f"{APP_NAME}.lnk", program, ico) for d in _windows_dirs()]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    icons = data / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    png = icons / "studio-pulse.png"
    shutil.copyfile(_asset("studio-pulse.png"), png)
    return [linux_entry(data / "applications", program, png)]


def uninstall_shortcuts(*, platform: str = sys.platform, home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    if platform == "darwin":
        targets = [home / "Applications" / f"{APP_NAME}.app"]
    elif platform == "win32":
        targets = [d / f"{APP_NAME}.lnk" for d in _windows_dirs()]
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        targets = [data / "applications" / "studio-pulse.desktop"]
    removed = []
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t)
            removed.append(t)
        elif t.exists():
            t.unlink()
            removed.append(t)
    return removed


def _version() -> str:
    from . import __version__

    return __version__


# -- entry points -----------------------------------------------------------------------
def add_parser(sub: argparse._SubParsersAction) -> None:
    a = sub.add_parser(
        "app", help="open Studio Pulse in its own window (what the desktop icon runs)"
    )
    a.add_argument("-c", "--config", help="config to open (remembered for the icon)")
    a.add_argument("--no-window", action="store_true", help="serve only; print the URL")
    a.add_argument(
        "--check", action="store_true", help="start, build the demo if needed, load pages, stop"
    )
    a.add_argument(
        "--install-shortcut",
        action="store_true",
        help="add the desktop / Start menu / Applications launcher",
    )
    a.add_argument("--uninstall-shortcut", action="store_true", help="remove that launcher")
    a.set_defaults(func=cmd_app)


def cmd_app(args: argparse.Namespace) -> int:
    if args.install_shortcut:
        for p in install_shortcuts():
            print(f"launcher: {p}")
        return 0
    if args.uninstall_shortcut:
        for p in uninstall_shortcuts():
            print(f"removed: {p}")
        return 0
    if args.check:
        return check(args.config)
    if args.config:
        state = state_dir()
        data = _read_state(state)
        data["config"] = str(Path(args.config).expanduser().resolve())
        _write_state(state, data)
    return run(args.config, open_window=not args.no_window)


def gui_main(argv: list[str] | None = None) -> int:
    """The `studio-pulse` GUI script: output goes to a log file (there is no console)."""
    from .background import _rotate

    args = list(sys.argv[1:] if argv is None else argv)
    log_file = state_dir() / "logs" / "app.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _rotate(log_file)
    with open(log_file, "a", encoding="utf-8", errors="backslashreplace", buffering=1) as log:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"=== {stamp} {GUI_SCRIPT} {' '.join(args)} (pid {os.getpid()})")
            from .cli import main as cli_main

            rc = cli_main(["app", *args])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            traceback.print_exc()
            rc = 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    return int(rc or 0)
