"""Run a real collector (uvicorn on 127.0.0.1, a free port) inside this process.

Used by `pulse demo` (with the simulation's clock), the tests and the CI smoke
job: requests travel over a real socket and through the same code as
`pulse collect`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .core import Collector, now_ms
from .settings import CollectorSettings


@contextmanager
def running_collector(
    settings: CollectorSettings, clock: Callable[[], int] = now_ms
) -> Iterator[tuple[str, Collector]]:
    import uvicorn

    from ..serve.server import bind
    from .app import create_app, make_store

    store = make_store(settings)
    col = Collector(settings, store, clock=clock)
    app = create_app(settings, col)
    sock = bind("127.0.0.1", 0)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
    )
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started and t.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("the in-process collector did not start")
    try:
        yield f"http://127.0.0.1:{port}", col
    finally:
        server.should_exit = True
        t.join(timeout=15)
        store.close()
        col.geo.close()
