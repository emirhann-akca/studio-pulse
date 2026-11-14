"""The local UI's HTTP hardening (serve/security.py): headers and CSP, CSRF on every
state-changing route, Fetch Metadata, the access token for a network bind, and the
desktop app's own endpoints."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_live_page import SNAP  # tests/ is on sys.path (no package)

from studio_pulse import cli
from studio_pulse.serve import security, server
from studio_pulse.serve.app import create_app

BASE = "http://127.0.0.1:8765"
TOKEN = "csrf-t0ken"
ACCESS = "a" * 8 + "-access-token-for-tests"
TEMPLATES = Path(cli.__file__).parent  # every package template, the brain's and radar's too


@pytest.fixture(scope="module")
def cfg(demo_build, tmp_path_factory):
    cfg = demo_build.copy(tmp_path_factory.mktemp("ui_security"))
    assert cli.main(["run", "-c", str(cfg.path)]) == 0  # the dashboard site exists
    return cfg


@pytest.fixture(scope="module")
def app(cfg):
    return create_app(cfg, token=TOKEN, launcher=lambda c: 4242, live_fetcher=lambda: SNAP)


@pytest.fixture()
def client(app):
    return TestClient(app, base_url=BASE, cookies={"ui-lang": "en"})


def _csp(r) -> dict[str, str]:
    out = {}
    for part in r.headers["content-security-policy"].split(";"):
        name, _, value = part.strip().partition(" ")
        out[name] = value
    return out


def test_every_response_carries_the_security_headers(client):
    for path, status in [
        ("/studio", 200),
        ("/health", 200),
        ("/static/htmx.min.js", 200),
        ("/dashboard/index.html", 200),
        ("/no-such-page", 404),
    ]:
        r = client.get(path)
        assert r.status_code == status, path
        assert r.headers["x-frame-options"] == "DENY", path
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "no-referrer", path
        assert r.headers["cross-origin-opener-policy"] == "same-origin", path
        csp = _csp(r)
        assert csp["frame-ancestors"] == "'none'" and csp["object-src"] == "'none'", path
        assert "'unsafe-inline'" not in csp["script-src"], path
        assert "'unsafe-eval'" not in csp["script-src"], path
    assert client.get("/studio").headers["cache-control"] == "no-store"  # pages carry the token
    # a host refused by the Host check still gets the headers
    r = client.get("/studio", headers={"host": "evil.example"})
    assert r.status_code == 400 and r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("path", ["/studio", "/health", "/dashboard/index.html", "/ask"])
def test_each_inline_script_is_allowed_by_its_hash_and_nothing_else_is_inline(client, path):
    r = client.get(path)
    allowed = _csp(r)["script-src"].split()
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", r.text, re.S)
    code = [body for attrs, body in inline if "application/json" not in attrs]
    assert code, path  # the shell (or the dashboard) has one
    for body in code:
        assert security.csp_hash(body) in allowed, f"{path}: an inline script the CSP blocks"
    markup = re.sub(r"<(script|style)\b.*?</\1>", "", r.text, flags=re.S | re.I)
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", markup, re.I), f"{path}: an inline event handler"
    assert "javascript:" not in markup.lower(), path


def test_no_template_uses_inline_handlers_or_hx_on():
    """hx-on needs eval and on*= needs 'unsafe-inline': neither runs under the CSP."""
    offenders = []
    for t in TEMPLATES.rglob("*.html"):
        text = t.read_text(encoding="utf-8")
        if re.search(r"\shx-on[:-]|\son[a-z]+\s*=\s*[\"']", text, re.I):
            offenders.append(t.name)
    assert not offenders


def _unsafe_routes(app) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        for m in sorted(getattr(route, "methods", None) or ()):
            if m not in security.SAFE_METHODS:
                out.append((m, route.path))
    return out


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_csrf_every_state_changing_route_refuses_requests_without_the_token(app, client):
    routes = _unsafe_routes(app)
    # the routes there are today; a new one is covered by the Guard without being listed
    assert {p for _, p in routes} >= {"/run", "/ask", "/studio/{app_id}/ask", "/settings/slack"}
    for method, path in routes:
        url = _concrete(path)
        r = client.request(method, url)
        assert r.status_code == 403, (method, path)
        r = client.request(method, url, headers={"X-Pulse-Token": "wrong"})
        assert r.status_code == 403, (method, path)
        r = client.request(
            method, url, headers={"X-Pulse-Token": TOKEN, "Origin": "https://evil.example"}
        )
        assert r.status_code == 403, (method, path)
        r = client.request(
            method, url, headers={"X-Pulse-Token": TOKEN, "Origin": "null"}
        )  # a sandboxed iframe or a file: page
        assert r.status_code == 403, (method, path)
        r = client.request(
            method, url, headers={"X-Pulse-Token": TOKEN, "Sec-Fetch-Site": "cross-site"}
        )
        assert r.status_code == 403, (method, path)


def test_csrf_the_ui_own_requests_pass(client):
    ok = {"X-Pulse-Token": TOKEN, "Origin": BASE, "Sec-Fetch-Site": "same-origin"}
    assert client.post("/run", headers=ok).status_code == 202
    r = client.post("/settings/slack", data={"webhook": "not a hook"}, headers=ok)
    assert r.status_code == 200  # reached the route, which says what is wrong
    # a port of another local app is another origin
    r = client.post("/run", headers={**ok, "Origin": "http://127.0.0.1:9999"})
    assert r.status_code == 403


def test_fetch_metadata_refuses_cross_site_subresources_but_not_navigation(client):
    embed = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
    assert client.get("/api/status", headers=embed).status_code == 403
    assert client.get("/studio", headers={**embed, "Sec-Fetch-Mode": "cors"}).status_code == 403
    nav = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    assert client.get("/studio", headers=nav).status_code == 200  # a link from elsewhere
    assert client.get("/studio", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_network_bind_needs_the_access_token(cfg):
    app = create_app(
        cfg, token=TOKEN, access_token=ACCESS, any_host=True, live_fetcher=lambda: SNAP
    )
    c = TestClient(app, base_url="http://192.168.1.20:8765", follow_redirects=False)
    assert c.get("/healthz").status_code == 200  # liveness only, no data
    for path in ("/studio", "/api/status", "/dashboard/index.html", "/static/htmx.min.js"):
        r = c.get(path)
        assert r.status_code == 401 and TOKEN not in r.text, path
    assert c.get("/studio", params={"access": "wrong"}).status_code == 401
    assert c.get("/studio", headers={"Cookie": "pulse_access=wrong"}).status_code == 401
    # the printed URL: traded once for a cookie, then dropped from the address bar
    r = c.get("/studio", params={"access": ACCESS, "lang": "en"})
    assert r.status_code == 303 and r.headers["location"] == "/studio?lang=en"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "pulse_access=" in cookie
    for sneaky in (
        "/%2Fevil.example",
        "/%5Cevil.example",
        "/%2F%5Cevil.example",
    ):  # //host, /\\host
        r = c.get(sneaky, params={"access": ACCESS})
        loc = r.headers["location"]
        assert r.status_code == 303 and loc.startswith("/") and loc[1] not in "/\\", loc
    c.cookies.set("pulse_access", ACCESS)
    assert c.get("/studio").status_code == 200
    # with the cookie, a POST still needs the CSRF token and the same origin
    assert c.post("/run").status_code == 403
    r = c.post("/run", headers={"X-Pulse-Token": TOKEN, "Origin": "http://192.168.1.20:8765"})
    assert r.status_code == 202
    c.cookies.clear()
    r = c.get("/api/status", headers={"Authorization": f"Bearer {ACCESS}"})
    assert r.status_code == 200
    with pytest.raises(ValueError):
        create_app(cfg, token=TOKEN, any_host=True)  # any Host only behind a token
    with pytest.raises(ValueError):
        security.Guard(None, csrf_token=TOKEN, hosts=None, access_token="short")


def test_serve_on_the_network_prints_a_url_with_a_fresh_access_token(cfg, monkeypatch, capsys):
    seen: list[dict] = []

    class Sock:
        def getsockname(self):
            return ("0.0.0.0", 8765)

    class Srv:
        def run(self, sockets):
            pass

    def fake_create_app(c, **kw):
        seen.append(kw)
        return object()

    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr(server, "bind", lambda host, port: Sock())
    monkeypatch.setattr(server, "_server", lambda app, sock: Srv())
    assert server.serve(cfg, host="0.0.0.0", port=8765) == 0
    assert server.serve(cfg, host="127.0.0.1", port=8765) == 0
    net, local = seen
    assert net["any_host"] and len(net["access_token"]) >= 32
    assert local["access_token"] is None and not local["any_host"]
    out = capsys.readouterr()
    assert f"?access={net['access_token']}" in out.out
    assert "access token" in out.err


def test_desktop_endpoints_refuse_other_sites_and_other_hosts():
    from studio_pulse.desktop import Heartbeat

    async def ui(scope, receive, send):  # pragma: no cover - not reached
        raise AssertionError

    beat = Heartbeat(ui)
    c = TestClient(beat, base_url="http://127.0.0.1")
    assert c.get("/__app/bye", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/__app/bye", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.get("/__app/ping", headers={"host": "evil.example"}).status_code == 403
    assert beat.bye_at is None and not beat.pinged
    assert c.get("/__app/ping", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert beat.pinged


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.domain)//",
        "file:///etc/passwd#",
        "http://evil.example:8766/",
        "http://127.0.0.1.evil.example:8766/",
    ],
)
def test_only_a_local_creative_forge_url_becomes_a_link(app, client, tmp_path, monkeypatch, url):
    """Creative Forge's app.json names the URL the sidebar links to; it is only trusted to
    name a port on this machine (the link is in every page, and it is opened)."""
    import json

    opened: list[str] = []
    monkeypatch.setenv("CREATIVE_FORGE_HOME", str(tmp_path))
    (tmp_path / "data" / "app").mkdir(parents=True)
    (tmp_path / "data" / "app" / "app.json").write_text(json.dumps({"url": url}), encoding="utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda u, timeout=None: opened.append(u))
    html = client.get("/studio").text
    assert opened == [] and url not in html


def test_app_json_may_only_name_a_local_port(tmp_path, monkeypatch):
    """The desktop app opens the URL another instance left in app.json; a tampered file
    must not send the window (or a probe) anywhere else."""
    from studio_pulse import desktop

    probed: list[str] = []
    monkeypatch.setattr(
        desktop, "_get", lambda url, timeout=2.0: probed.append(url) or (200, b'"ok"')
    )
    for url in (
        "https://evil.example/",
        "file:///etc/passwd",
        "http://127.0.0.1.evil.example:1/",
        42,
    ):
        desktop._write_state(tmp_path, {"url": url})
        assert desktop.running_url(tmp_path) is None and probed == []
    desktop._write_state(tmp_path, {"url": "http://127.0.0.1:8765/"})
    assert desktop.running_url(tmp_path) == "http://127.0.0.1:8765/"


def test_a_dashboard_written_on_windows_keeps_its_script(cfg, client):
    """site.py writes text files, so on Windows the inline script has CRLF line ends and
    another SHA-256; the CSP allows both forms (seen on the first CI run)."""
    from studio_pulse.dashboard.assets import JS

    page = cfg.site_dir / "crlf-check.html"
    page.write_bytes(
        f"<!doctype html><html><body><script>{JS}</script></body></html>".replace(
            "\n", "\r\n"
        ).encode()
    )
    try:
        r = client.get("/dashboard/crlf-check.html")
        body = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
        assert "\r\n" in body and security.csp_hash(body) in _csp(r)["script-src"].split()
    finally:
        page.unlink()
