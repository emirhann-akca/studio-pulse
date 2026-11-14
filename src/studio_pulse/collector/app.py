"""`pulse collect`: the HTTP collector (FastAPI on uvicorn).

    POST /v2/batch    contract v2 batch (gzip optional), X-Pulse-Key
    POST /v1/ndjson   contract v1 JSON lines (Unity JsonLinesSink HTTP), upgraded
    POST /v2/delete   {"install_id": ..., "app_id": ..., "reason": ...}: deletion / opt-out
                      (rate limited per IP like ingest)
    GET  /live        active installs in the last 5 minutes per app (read key if configured)
    GET  /healthz     liveness, mode, counters (no keys, no data)

Bodies are read with a hard cap before parsing. The request is processed in a
worker thread (the durable write fsyncs), so the event loop never blocks.
"""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..contract import spec
from .core import Collector, HTTPError
from .settings import CollectorSettings


def _headers(request: Request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}


def client_ip(request: Request, hops: int) -> str | None:
    """The peer address, or the address `hops` entries from the right of X-Forwarded-For
    when the collector runs behind that many trusted proxies (Cloud Run: 1)."""
    peer = request.client.host if request.client else None
    if hops <= 0:
        return peer
    xff = [
        p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()
    ]
    return xff[-hops] if len(xff) >= hops else peer


async def read_limited(request: Request, limit: int) -> bytes:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise HTTPError(413, "body_too_large")
    chunks, n = [], 0
    async for chunk in request.stream():
        n += len(chunk)
        if n > limit:
            raise HTTPError(413, "body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def make_store(settings: CollectorSettings) -> Any:
    if settings.mode == "bigquery":
        from .bigquery import BigQueryStore

        return BigQueryStore.from_settings(settings)
    from .store import LocalStore

    return LocalStore(settings.data_dir, fsync=settings.fsync, dedupe_days=settings.dedupe_days)


def create_app(settings: CollectorSettings, collector: Collector | None = None) -> FastAPI:
    col = collector or Collector(settings, make_store(settings))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        close = getattr(col.store, "close", None)
        if callable(close):
            close()

    app = FastAPI(
        title="studio-pulse collector",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.collector = col
    limit = spec()["limits"]["batch_max_bytes"]

    def error(exc: HTTPError) -> JSONResponse:
        headers = {"Retry-After": "5"} if exc.status in (429, 503) else None
        return JSONResponse({"ok": False, "error": exc.reason}, exc.status, headers=headers)

    async def handle(request: Request, fn: Any) -> JSONResponse:
        hdrs = _headers(request)
        try:
            col.check_key(hdrs)  # 401 before reading a body
            raw = await read_limited(request, limit)
            status, body = await run_in_threadpool(
                fn, raw, hdrs, client_ip(request, settings.trusted_proxy_hops)
            )
        except HTTPError as exc:
            return error(exc)
        except OSError as exc:  # the durable write failed: the client must retry
            return JSONResponse({"ok": False, "error": f"storage: {type(exc).__name__}"}, 503)
        return JSONResponse(body, status)

    @app.post("/v2/batch")
    async def batch(request: Request) -> JSONResponse:
        return await handle(request, col.process_v2)

    @app.post("/v1/ndjson")
    async def v1(request: Request) -> JSONResponse:
        return await handle(request, col.process_v1)

    @app.post("/v2/delete")
    async def delete(request: Request) -> JSONResponse:
        hdrs = _headers(request)
        try:
            col.check_key(hdrs)  # 401 before reading a body
            raw = await read_limited(request, 4096)
            try:
                body = json.loads(raw or b"{}")
            except ValueError as exc:
                raise HTTPError(400, "bad_json") from exc
            out = await run_in_threadpool(
                col.delete, body, hdrs, client_ip(request, settings.trusted_proxy_hops)
            )
        except HTTPError as exc:
            return error(exc)
        return JSONResponse(out, 202)

    @app.get("/live")
    async def live(request: Request) -> JSONResponse:
        if settings.read_key:
            given = request.headers.get("x-pulse-read-key") or request.query_params.get("key") or ""
            if not secrets.compare_digest(given, settings.read_key):
                return JSONResponse({"ok": False, "error": "read key required"}, 401)
        snap = await run_in_threadpool(col.live_snapshot)
        return JSONResponse(snap, headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        c = col.counters
        return {
            "ok": True,
            "version": __version__,
            "contract": spec()["version"],
            **settings.public(),
            "country": col.geo.source,
            "accepted": c.accepted,
            "quarantined": c.quarantined,
        }

    return app


def serve(
    settings: CollectorSettings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    log: Any = print,
) -> int:
    import uvicorn

    from ..serve.server import bind

    local = host.strip("[]") in ("127.0.0.1", "localhost", "::1")
    if not local and settings.uses_default_key:
        log(
            "warning: listening on the network with the default local write key; "
            "set collector.write_keys (or PULSE_WRITE_KEYS) before phones or the internet can reach it"
        )
    elif not local and settings.unbound_keys:
        log(
            f"warning: {len(settings.unbound_keys)} write key(s) accept events for any app_id; "
            "bind each key to its apps (collector.write_keys: [{key: ..., apps: [...]}] or "
            "PULSE_WRITE_KEYS=key=app_id|app_id) so a key taken from one build cannot write "
            "events or revenue for another app"
        )
    app = create_app(settings)
    sock = bind(host, port)
    real = sock.getsockname()[1]
    where = settings.data_dir if settings.mode == "local" else settings.bigquery
    log(f"pulse collector on http://{host}:{real}/  ({settings.mode}: {where}; Ctrl+C to stop)")
    if settings.geoip is None and not settings.country_header:
        log(
            "country: unknown for every event (run `pulse collect setup-geoip` or set country_header)"
        )
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    uvicorn.Server(config).run(sockets=[sock])
    return 0


__all__ = ["create_app", "serve", "make_store", "client_ip"]
