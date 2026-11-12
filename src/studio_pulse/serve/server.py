"""Run the UI with uvicorn on a socket bound here (so `--port 0` knows its port)."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from ..config import Config
from .app import LOCAL_HOSTS, create_app


def bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if sys.platform != "win32":  # on Windows SO_REUSEADDR lets others steal the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host.strip("[]"), port))
    sock.listen(128)
    return sock


def _url(host: str, port: int) -> str:
    h = host.strip("[]")
    if h in ("0.0.0.0", "::"):
        h = "127.0.0.1"
    return f"http://[{h}]:{port}/" if ":" in h else f"http://{h}:{port}/"


def _server(app, sock: socket.socket) -> uvicorn.Server:
    config = uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False)
    return uvicorn.Server(config)


def serve(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    check: bool = False,
) -> int:
    local = host.strip("[]") in LOCAL_HOSTS
    if not local:
        print(
            f"warning: binding {host} makes the UI reachable from other machines on your network",
            file=sys.stderr,
        )
    app = create_app(cfg, allowed_hosts=set() if local else {host})
    sock = bind("127.0.0.1" if check else host, 0 if check else port)
    real_port = sock.getsockname()[1]
    url = _url("127.0.0.1" if check else host, real_port)
    server = _server(app, sock)
    if check:
        return _self_check(server, sock, url, cfg)
    print(f"studio-pulse UI: {url}  (config {cfg.path}; Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    server.run(sockets=[sock])
    return 0


def _get(
    url: str, *, method: str = "GET", headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (local URL)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _self_check(server: uvicorn.Server, sock: socket.socket, base: str, cfg: Config) -> int:
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline and t.is_alive():
        time.sleep(0.05)
    if not server.started:
        print(f"FAIL server did not start on {base}")
        return 1
    pages: list[tuple[str, str]] = [
        ("healthz", '"ok":true'),
        ("", "Run refresh now"),
        ("health", "Data quality"),
        ("alerts", "History"),
        ("api/status", '"version"'),
        ("static/htmx.min.js", "htmx"),
        ("insights", "Insights"),
        ("insights?lang=tr", "İçgörüler"),
        # with data the router must answer from the verified-query library
        (
            "ask?lang=tr&q=Veri%20g%C3%BCncel%20mi%3F",
            "Doğrulanmış sorgu" if Path(cfg.warehouse).exists() else "Sor",
        ),
    ]
    site = Path(cfg.site_dir)
    if (site / "index.html").exists():
        pages.append(("dashboard/index.html", "<!doctype html>"))
        pages += [
            (f"dashboard/{p.name}", "<!doctype html>")
            for p in sorted(site.glob("*.html"))
            if p.name != "index.html"
        ]
        pages += [
            (f"dashboard/apps/{p.name}", "<!doctype html>")
            for p in sorted((site / "apps").glob("*.html"))
        ]
    failures = 0
    for path, marker in pages:
        t0 = time.perf_counter()
        status, body = _get(base + path)
        text = body.decode("utf-8", errors="replace")
        if marker.startswith('"'):  # JSON: compare without whitespace
            text = text.replace(" ", "")
        ok = status == 200 and marker in text
        failures += not ok
        ms = (time.perf_counter() - t0) * 1000
        print(f"{'ok  ' if ok else 'FAIL'} GET /{path:<34} {status} {ms:6.0f} ms")
    for post in ("run", "ask"):
        status, _ = _get(base + post, method="POST")
        ok = status == 403
        failures += not ok
        print(
            f"{'ok  ' if ok else 'FAIL'} POST /{post:<4} without token          {status} (must be 403)"
        )
    status, body = _get(base + "api/status")
    try:
        db = json.loads(body).get("database")
    except ValueError:
        db = "?"
    print(f"database read by the UI: {db}")
    server.should_exit = True
    t.join(timeout=15)
    print("UI check passed." if not failures else f"UI check: {failures} failure(s).")
    return 0 if not failures else 1
