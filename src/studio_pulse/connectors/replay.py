"""Recorded HTTP responses for API connectors (demo and tests; no network).

A replay directory holds a manifest, `replay.json`, and the response bodies:

    {"requests": [
      {"method": "GET", "path": "/v27.0/act_1/insights", "file": "meta_p1.json"},
      {"method": "GET", "path": "/v27.0/act_1/insights", "query": {"after": "c2"},
       "file": "meta_p2.json"},
      {"method": "POST", "path": "/v22/customers/1/googleAds:search",
       "json": {"pageToken": "t2"}, "status": 200, "file": "gads_p2.json"}
    ]}

A request is answered by the entry with the same method and path whose
`query` (and `json` body) keys all match the request; when several match, the
most specific one (most matched keys) wins. A request that matches nothing
fails loudly: a replay session never falls through to the network.

`ReplaySession` is a `requests.Session`, so the connectors (and dlt's REST
client, which accepts a session) run the same code against recorded responses
as against the live API. Every request is kept in `.recorded` so tests can
assert what would have been sent (auth headers present, no secret in a URL).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.models import PreparedRequest, Response

MANIFEST = "replay.json"


class ReplayError(RuntimeError):
    """A request had no recorded response."""


@dataclass
class Recorded:
    method: str
    url: str
    path: str
    query: dict[str, str]
    body: Any
    headers: dict[str, str]


def _body(req: PreparedRequest) -> Any:
    raw = req.body
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


class ReplayAdapter(BaseAdapter):
    def __init__(self, root: str | Path, entries: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.root = Path(root)
        if entries is None:
            manifest = self.root / MANIFEST
            if not manifest.exists():
                raise ReplayError(f"no {MANIFEST} in {self.root}")
            entries = json.loads(manifest.read_text(encoding="utf-8"))["requests"]
        self.entries = entries
        self.requests: list[Recorded] = []

    def _match(self, rec: Recorded) -> dict[str, Any]:
        best, best_score = None, -1
        for e in self.entries:
            if e.get("method", "GET").upper() != rec.method or e["path"] != rec.path:
                continue
            want_q = {k: str(v) for k, v in (e.get("query") or {}).items()}
            if any(rec.query.get(k) != v for k, v in want_q.items()):
                continue
            want_j = e.get("json") or {}
            body = rec.body if isinstance(rec.body, dict) else {}
            if any(body.get(k) != v for k, v in want_j.items()):
                continue
            score = len(want_q) + len(want_j)
            if score > best_score:
                best, best_score = e, score
        if best is None:
            raise ReplayError(f"no recorded response for {rec.method} {rec.path} {rec.query}")
        return best

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        parts = urlsplit(request.url or "")
        rec = Recorded(
            method=(request.method or "GET").upper(),
            url=request.url or "",
            path=parts.path,
            query=dict(parse_qsl(parts.query, keep_blank_values=True)),
            body=_body(request),
            headers={k: v for k, v in request.headers.items()},
        )
        self.requests.append(rec)
        entry = self._match(rec)
        resp = Response()
        resp.status_code = int(entry.get("status", 200))
        resp.headers["Content-Type"] = entry.get("content_type", "application/json")
        if "file" in entry:
            resp._content = (self.root / entry["file"]).read_bytes()
        else:
            resp._content = json.dumps(entry.get("body", {})).encode("utf-8")
        resp.encoding = "utf-8"
        resp.url = request.url or ""
        resp.request = request
        resp.reason = "OK" if resp.status_code < 400 else "Recorded error"
        return resp

    def close(self) -> None:
        pass


class ReplaySession(requests.Session):
    """A requests session that only answers from a replay directory."""

    def __init__(self, root: str | Path, entries: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.adapter = ReplayAdapter(root, entries)
        self.mount("https://", self.adapter)
        self.mount("http://", self.adapter)

    @property
    def recorded(self) -> list[Recorded]:
        return self.adapter.requests
