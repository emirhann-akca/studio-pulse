"""HTTP hardening for every response of the local UI (`pulse serve`, the desktop app).

One ASGI middleware, outermost, so it also covers 4xx answers and the /dashboard and
/static mounts. In order:

1. Host allow-list: a request whose Host header is not this machine is refused
   (DNS rebinding). `hosts=None` (a wildcard bind such as 0.0.0.0) accepts any Host,
   which is only safe together with step 2.
2. Access token, only when the UI listens on a non-loopback address: the URL printed
   at start carries `?access=<token>`; the first request trades it for an HttpOnly,
   SameSite=Strict cookie and is redirected to the same URL without it. Every other
   request without that cookie (or an `Authorization: Bearer` header) gets 401, so
   another machine on the network cannot read a page, and with it the CSRF token.
3. CSRF on every state-changing method (anything but GET/HEAD/OPTIONS), whatever the
   route: the per-process token in the X-Pulse-Token header (pages send it through
   htmx's hx-headers; another site cannot read it), an Origin, when sent, equal to
   this server's own, and no cross-site Fetch Metadata. The routes keep their own
   token checks; this is the floor a new route cannot forget.
4. Fetch Metadata resource isolation for reads: a cross-site request that is not a
   top-level navigation (an <img>, <script> or fetch() from another site) is refused.
5. Security headers: a Content-Security-Policy without 'unsafe-inline' for scripts (the
   two inline scripts this package writes are allowed by their SHA-256), no framing,
   nosniff, no Referer, same-origin opener and resource policies, and no-store on
   HTML (pages carry the token). A header a route set itself is left as it is.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TOKEN_HEADER = "x-pulse-token"
ACCESS_PARAM = "access"
ACCESS_COOKIE = "pulse_access"
# Answers without the access token: liveness only (no data, no token).
OPEN_PATHS = frozenset({"/healthz"})
CROSS_SITE = frozenset({"cross-site", "same-site"})


class _AnyHost(frozenset):
    """'Every host', for the routes' own Origin checks behind a wildcard bind: the Guard
    then compares Origin with the request's Host header itself."""

    def __contains__(self, item: object) -> bool:
        return True


ANY_HOST: frozenset[str] = _AnyHost()


def hostname(value: str) -> str:
    """Host header or Origin -> bare host name (no scheme, port or IPv6 brackets), lower case."""
    value = value.strip()
    return (urlsplit(value if "//" in value else "//" + value).hostname or "").lower()


def is_loopback(host: str) -> bool:
    return host.strip("[]").lower() in {"127.0.0.1", "localhost", "::1"}


def csp_hash(script: str) -> str:
    """CSP source for one inline <script> whose text is exactly `script`."""
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def content_security_policy(script_hashes: Iterable[str] = ()) -> str:
    # style-src keeps 'unsafe-inline': the charts and templates use style attributes,
    # which CSP cannot allow by hash. Scripts are the injection risk and stay strict.
    scripts = " ".join(["'self'", *script_hashes])
    return (
        "default-src 'self'; "
        f"script-src {scripts}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    )


def security_headers(csp: str) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-security-policy", csp.encode("ascii")),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    ]


class Guard:
    """The middleware described in the module docstring."""

    def __init__(
        self,
        app: Any,
        *,
        csrf_token: str,
        hosts: set[str] | frozenset[str] | None,
        access_token: str | None = None,
        script_hashes: Iterable[str] = (),
        token_header: str = TOKEN_HEADER,
        refused_host: str = "only answers requests for this machine",
    ) -> None:
        if access_token is not None and len(access_token) < 16:
            raise ValueError("the access token must be at least 16 characters")
        self.app = app
        self.csrf_token = csrf_token
        self.hosts = None if hosts is None else {h.lower().strip("[]") for h in hosts}
        self.access_token = access_token
        self.headers = security_headers(content_security_policy(script_hashes))
        self.token_header = token_header.lower()
        self.refused_host = refused_host

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refusal = self.check(Request(scope))
        target = refusal if refusal is not None else self.app

        async def with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in self.headers if k not in present]
                ctype = dict(headers).get(b"content-type", b"")
                if ctype.startswith(b"text/html") and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await target(scope, receive, with_headers)

    # -- the checks; None means "let it through" ---------------------------------------
    def check(self, request: Request) -> Response | None:
        host_header = request.headers.get("host", "")
        if self.hosts is not None and hostname(host_header) not in self.hosts:
            return PlainTextResponse(f"studio-pulse {self.refused_host}", 400)
        if self.access_token is not None and request.url.path not in OPEN_PATHS:
            refused = self._access(request)
            if refused is not None:
                return refused
        site = request.headers.get("sec-fetch-site")
        if request.method not in SAFE_METHODS:
            sent = request.headers.get(self.token_header, "")
            if not secrets.compare_digest(sent.encode(), self.csrf_token.encode()):
                return PlainTextResponse("missing or wrong token", 403)
            origin = request.headers.get("origin")
            if origin is not None and not self._same_origin(origin, host_header):
                return PlainTextResponse("cross-origin request refused", 403)
            if site in CROSS_SITE:
                return PlainTextResponse("cross-site request refused", 403)
            return None
        if site == "cross-site" and request.headers.get("sec-fetch-mode") != "navigate":
            return PlainTextResponse("cross-site request refused", 403)
        return None

    @staticmethod
    def _same_origin(origin: str, host_header: str) -> bool:
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and parts.netloc.lower() == host_header.lower()

    def _access(self, request: Request) -> Response | None:
        assert self.access_token is not None
        token = self.access_token.encode()
        offered = request.query_params.get(ACCESS_PARAM)
        if offered is not None:
            if not secrets.compare_digest(offered.encode(), token):
                return PlainTextResponse("wrong access token", 401)
            rest = [(k, v) for k, v in request.query_params.multi_items() if k != ACCESS_PARAM]
            # one leading slash: "//host" or "/\\host" would send the browser to another site
            url = "/" + request.url.path.lstrip("/\\") + ("?" + urlencode(rest) if rest else "")
            resp = RedirectResponse(url, status_code=303)
            resp.set_cookie(
                ACCESS_COOKIE, self.access_token, httponly=True, samesite="strict", path="/"
            )
            return resp
        cookie = request.cookies.get(ACCESS_COOKIE, "")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        for candidate in (cookie, bearer):
            if candidate and secrets.compare_digest(candidate.encode(), token):
                return None
        return PlainTextResponse(
            "this UI is reachable from the network and needs its access token: open the URL "
            "with ?access=... that the server printed when it started",
            401,
        )
