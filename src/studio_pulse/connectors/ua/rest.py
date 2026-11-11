"""dlt's declarative REST client for the ad-network APIs.

The Meta, Google Ads and TikTok connectors describe their endpoint as a dlt
`rest_api` resource (path, params or JSON body, paginator, data selector) and
iterate it. dlt does the HTTP work: pagination, retries with backoff on 429 /
5xx and connection errors (when its own session is used), and response hooks.
The records are then normalised into the `raw_ua_spend` contract and written
with the same slice-replace as every other connector, so a re-run of a window
never duplicates rows and API and CSV sources share one table. dlt's own
loader and pipeline state are not used: nothing is written outside the
warehouse, and dlt's DuckDB extra (which pins duckdb < 2) is not needed.

dlt is an optional dependency (`pip install "studio-pulse[ua]"`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .common import Redactor, UAError

INSTALL_HINT = 'ad-network API sources need dlt: pip install "studio-pulse[ua]"'


def dlt_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("dlt") is not None


def default_session() -> Any:
    """dlt's requests session: retries 429 / 5xx / connection errors with backoff."""
    from dlt.sources.helpers.requests import Client

    return Client(raise_for_status=False).session


def literal(value: str) -> str:
    """A parameter value dlt must not interpolate: dlt reads {..} as an expression."""
    return value.replace("{", "{{").replace("}", "}}")


def strip_query_param(url: str, name: str) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    return urlunsplit(parts._replace(query=urlencode(q)))


def link_paginator(next_url_path: str, strip: str | None = None) -> Any:
    """Follow a next-page URL found in the body, minus a query parameter.

    Meta writes `access_token` into `paging.next`; the token is sent as a
    header instead, so it is removed from the URL before the next request
    (URLs end up in error messages and logs).
    """
    from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

    class _Link(JSONLinkPaginator):
        def update_state(self, response: Any, data: Any = None) -> None:
            super().update_state(response, data)
            if strip and self._next_reference:
                self._next_reference = strip_query_param(str(self._next_reference), strip)

    return _Link(next_url_path=next_url_path)


def fetch(
    *,
    base_url: str,
    endpoint: dict[str, Any],
    paginator: Any,
    headers: dict[str, str],
    session: Any,
    redactor: Redactor,
    what: str,
    response_actions: list[Callable[..., Any]] | None = None,
) -> list[dict[str, Any]]:
    """All records of one endpoint, every page. Errors are re-raised redacted."""
    try:
        from dlt.sources.rest_api import rest_api_source
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise UAError(INSTALL_HINT) from exc
    ep = dict(endpoint)
    ep["paginator"] = paginator
    if response_actions:
        ep["response_actions"] = response_actions
    config: dict[str, Any] = {
        "client": {"base_url": base_url, "headers": headers, "session": session},
        "resources": [{"name": "rows", "endpoint": ep}],
    }
    try:
        source = rest_api_source(config)
        return [dict(r) for r in source.resources["rows"]]
    except UAError:
        raise
    except Exception as exc:  # anything from the HTTP layer
        root: BaseException = exc
        while root.__cause__ is not None and root.__cause__ is not root:
            root = root.__cause__
        if isinstance(root, UAError):
            raise UAError(f"{what} failed: {redactor.clean(root)}") from None
        msg = f"{type(root).__name__}: {root}"
        resp = getattr(root, "response", None)
        if resp is not None:  # the API's own error text is what explains a 4xx
            msg += f" | {getattr(resp, 'text', '')[:300]}"
        raise UAError(f"{what} failed: {redactor.clean(msg)[:700]}") from None
