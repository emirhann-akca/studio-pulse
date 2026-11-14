"""Plain HTTP for the report APIs that do not fit dlt's REST client (AppLovin, Unity Ads).

AppLovin's Reporting API takes its key only as a query parameter, and dlt's REST client logs
request parameters at DEBUG level; Unity's Statistics API answers CSV, which dlt's `rest_api`
does not read. These two connectors send their GET requests through `get` instead, with the
same guarantees as the dlt path:

- https only, to a host in `common.UA_HOSTS`, and redirects are never followed (a 3xx is an
  error): the host a credential goes to is fixed by this package;
- 429, 500, 502, 503 and 504 and connection errors / timeouts are retried with exponential
  backoff; a `Retry-After` header is honoured up to `max_wait_s`, and a longer one stops the
  source with a message instead of sleeping through the run;
- requests to one API can be spaced (`Pacer`, Unity allows 1 request per second);
- an error names the API, the HTTP status and the API's own short message, passed through the
  redactor; never the request URL (AppLovin's key is in it) and never a chained exception;
- nothing is logged here, and urllib3's DEBUG line (which carries the full URL) has any
  `api_key=` value masked by `QueryKeyFilter`.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from .common import Redactor, UAError, check_host

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_KEY_IN_QUERY = re.compile(r"(?i)(api_key=)[^&\s\"']+")


class QueryKeyFilter(logging.Filter):
    """Masks `api_key=<value>` in log records (urllib3 logs request URLs at DEBUG)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "api_key=" in record.msg.lower():
            record.msg = _KEY_IN_QUERY.sub(r"\1***", record.msg)
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(
                _KEY_IN_QUERY.sub(r"\1***", a) if isinstance(a, str) else a for a in args
            )
        return True


def mask_urllib3_logs() -> None:
    """Attach QueryKeyFilter to urllib3's connection-pool logger (idempotent)."""
    lg = logging.getLogger("urllib3.connectionpool")
    if not any(isinstance(f, QueryKeyFilter) for f in lg.filters):
        lg.addFilter(QueryKeyFilter())


@dataclass
class Pacer:
    """Keeps at least `min_interval_s` seconds between two requests to one API."""

    min_interval_s: float = 0.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: float | None = field(default=None, repr=False)

    def wait(self) -> None:
        if self._last is not None and self.min_interval_s > 0:
            gap = self.min_interval_s - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()


def retry_after(value: str | None, now: dt.datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta seconds or an HTTP date)."""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        when = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - (now or dt.datetime.now(dt.UTC))).total_seconds())


def api_message(response: Any, redactor: Redactor, limit: int = 240) -> str:
    """The API's own short error text (problem+json title/detail, a JSON message, or text)."""
    text = ""
    try:
        body = response.json()
    except (ValueError, TypeError, AttributeError):
        body = None
    if isinstance(body, dict):
        parts = [
            str(body[k])
            for k in ("title", "detail", "message", "error", "msg", "code")
            if body.get(k) not in (None, "", [], {})
        ]
        text = " - ".join(dict.fromkeys(parts))
    if not text:
        text = str(getattr(response, "text", "") or "")
    text = re.sub(r"\s+", " ", redactor.clean(text)).strip()
    return text[:limit]


def get(
    session: Any,
    url: str,
    *,
    params: dict[str, Any],
    what: str,
    redactor: Redactor,
    auth: Any = None,
    headers: dict[str, str] | None = None,
    auth_hint: str = "",
    timeout: tuple[float, float] = (15.0, 300.0),
    retries: int = 4,
    backoff_s: float = 2.0,
    max_wait_s: float = 120.0,
    pacer: Pacer | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """One GET with retries; returns the response of a 2xx answer or raises UAError."""
    check_host(url, what)
    host = urlsplit(url).hostname
    attempt = 0
    while True:
        attempt += 1
        if pacer is not None:
            pacer.wait()
        try:
            r = session.get(
                url,
                params=params,
                auth=auth,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception as exc:  # the message of a transport error carries the full URL
            if not isinstance(exc, requests.RequestException):
                raise UAError(
                    f"{what} failed: {type(exc).__name__}: {redactor.clean(exc)}"
                ) from None
            if attempt > retries:
                raise UAError(
                    f"{what} failed: {type(exc).__name__} from {host} after {attempt} attempts"
                ) from None
            sleep(backoff_s * 2 ** (attempt - 1))
            continue
        code = int(r.status_code)
        if code in RETRY_STATUS:
            wait = retry_after(r.headers.get("Retry-After"))
            if wait is not None and wait > max_wait_s:
                raise UAError(
                    f"{what}: HTTP {code}, the API asks to wait {wait:.0f} s "
                    f"(more than {max_wait_s:.0f} s); the next run will retry. "
                    + api_message(r, redactor)
                )
            if attempt > retries:
                raise UAError(
                    f"{what}: HTTP {code} after {attempt} attempts: {api_message(r, redactor)}"
                )
            sleep(wait if wait is not None else backoff_s * 2 ** (attempt - 1))
            continue
        if 300 <= code < 400:
            raise UAError(f"{what}: HTTP {code} redirect from {host} refused (not followed)")
        if code in (401, 403):
            why = "unauthorized" if code == 401 else "forbidden"
            msg = api_message(r, redactor)
            raise UAError(
                f"{what}: HTTP {code} {why}; {auth_hint}" + (f" (API: {msg})" if msg else "")
            )
        if code >= 400:
            raise UAError(f"{what}: HTTP {code}: {api_message(r, redactor)}")
        return r


def json_body(response: Any, what: str, redactor: Redactor) -> Any:
    try:
        return json.loads(response.content.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        head = redactor.clean(response.content[:120].decode("utf-8", errors="replace"))
        raise UAError(f"{what}: the answer is not JSON ({head!r})") from None
