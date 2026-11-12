"""Recorded store responses for radar tests and `pulse radar run --replay DIR` (no network).

The manifest (`replay.json`) uses studio-pulse's replay format (connectors/replay.py) with two
radar-specific entry kinds:

- `"pool": "apple/lookup_pool_us.json"` answers iTunes Lookup requests from a pool of
  recorded Lookup results: `id=a,b,c` returns the recorded apps among a, b, c (in request
  order) and `bundleId=x` the app with that bundle id. The live radar batches whatever is
  charted that day, so a fixed recorded batch could never match its requests.
- `"play_fields": "play/com.example.json"` answers a Play details request with a page built
  from recorded google-play-scraper `app()` output (`synth_play_page`). The page carries the
  fields at the positions google-play-scraper 1.2.7 reads, so the real parser runs on it.

- `"sequence": ["a.json", "b.json"]` answers successive matching requests with successive
  recorded answers (Apple's Lookup gave UnGodly 0 ratings, then 130, minutes apart).

Any request without a recorded answer raises ReplayError: a replay never reaches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from requests.models import PreparedRequest, Response

from ..connectors.replay import ReplayAdapter, ReplayError, ReplaySession

# google-play-scraper 1.2.7 ElementSpecs.Detail paths inside ds:5 (constants/element.py)
_PLAY_PATHS: dict[str, list[int]] = {
    "title": [1, 2, 0, 0],
    "description": [1, 2, 72, 0, 1],
    "summary": [1, 2, 73, 0, 1],
    "installs": [1, 2, 13, 0],
    "minInstalls": [1, 2, 13, 1],
    "realInstalls": [1, 2, 13, 2],
    "score": [1, 2, 51, 0, 1],
    "ratings": [1, 2, 51, 2, 1],
    "inAppProductPrice": [1, 2, 19, 0],
    "developer": [1, 2, 68, 0],
    "developerEmail": [1, 2, 69, 1, 0],
    "developerAddress": [1, 2, 69, 2, 0],
    "genre": [1, 2, 79, 0, 0, 0],
    "genreId": [1, 2, 79, 0, 0, 2],
    "containsAds": [1, 2, 48],
    "released": [1, 2, 10, 0],
    "version": [1, 2, 140, 0, 0, 0],
}


def _put(root: list[Any], path: list[int], value: Any) -> None:
    node = root
    for i, idx in enumerate(path):
        while len(node) <= idx:
            node.append(None)
        if i == len(path) - 1:
            node[idx] = value
        else:
            if not isinstance(node[idx], list):
                node[idx] = []
            node = node[idx]


def synth_play_page(fields: dict[str, Any]) -> str:
    """A minimal Play details page carrying `fields` where google-play-scraper reads them."""
    ds5: list[Any] = []
    for key, path in _PLAY_PATHS.items():
        v = fields.get(key)
        if v is None:
            continue
        if key == "containsAds":
            v = [1] if v else None
            if v is None:
                continue
        if key == "inAppProductPrice" and not fields.get("offersIAP"):
            continue
        _put(ds5, path, v)
    cats = [[c["name"], None, c["id"], None] for c in fields.get("categories") or []]
    if cats:
        _put(ds5, [1, 2, 118], [cats])
    data = json.dumps(ds5, ensure_ascii=False)
    return (
        "<html><body><script nonce=\"x\">AF_initDataCallback({key: 'ds:5', hash: '1', "
        f"data:{data}, sideChannel: {{}}}});</script></body></html>"
    )


class RadarReplayAdapter(ReplayAdapter):
    def __init__(self, root: str | Path, entries: list[dict[str, Any]] | None = None) -> None:
        super().__init__(root, entries)
        self._served: dict[int, int] = {}

    def _pool(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        doc = json.loads((self.root / entry["pool"]).read_text(encoding="utf-8"))
        return list(doc.get("results") or [])

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        parts = urlsplit(request.url or "")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for i, e in enumerate(self.entries):
            if "sequence" not in e or e["path"] != parts.path:
                continue
            if any(query.get(k) != str(v) for k, v in (e.get("query") or {}).items()):
                continue
            # recorded answers in the order they came: the nth matching request gets the
            # nth file, the last one repeats
            seen = self._served.get(i, 0)
            self._served[i] = seen + 1
            doc = json.loads(
                (self.root / e["sequence"][min(seen, len(e["sequence"]) - 1)]).read_text(
                    encoding="utf-8"
                )
            )
            return self._respond(request, doc)
        for e in self.entries:
            if "pool" not in e or e["path"] != parts.path:
                continue
            if any(query.get(k) != str(v) for k, v in (e.get("query") or {}).items()):
                continue
            if "entity" in query:  # a publisher's catalogue is a recorded file, not a pool
                break
            pool = self._pool(e)
            if "id" in query:
                by_id = {str(x.get("trackId")): x for x in pool}
                results = [by_id[i] for i in query["id"].split(",") if i in by_id]
            elif "bundleId" in query:
                results = [x for x in pool if x.get("bundleId") == query["bundleId"]]
            else:
                break
            return self._respond(request, {"resultCount": len(results), "results": results})
        for e in self.entries:
            if (
                "play_fields" in e
                and e["path"] == parts.path
                and query.get("id") == e.get("query", {}).get("id")
            ):
                fields = json.loads((self.root / e["play_fields"]).read_text(encoding="utf-8"))
                self.requests.append(self._recorded(request))
                resp = Response()
                resp.status_code = 200
                resp.headers["Content-Type"] = "text/html; charset=utf-8"
                resp._content = synth_play_page(fields).encode("utf-8")
                resp.encoding = "utf-8"
                resp.url = request.url or ""
                resp.request = request
                return resp
        return super().send(request, **kwargs)

    def _recorded(self, request: PreparedRequest) -> Any:
        from ..connectors.replay import Recorded, _body

        parts = urlsplit(request.url or "")
        return Recorded(
            method=(request.method or "GET").upper(),
            url=request.url or "",
            path=parts.path,
            query=dict(parse_qsl(parts.query, keep_blank_values=True)),
            body=_body(request),
            headers=dict(request.headers.items()),
        )

    def _respond(self, request: PreparedRequest, body: dict[str, Any]) -> Response:
        self.requests.append(self._recorded(request))
        resp = Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "application/json"
        resp._content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        resp.encoding = "utf-8"
        resp.url = request.url or ""
        resp.request = request
        return resp


class RadarReplaySession(ReplaySession):
    def __init__(self, root: str | Path, entries: list[dict[str, Any]] | None = None) -> None:
        super(ReplaySession, self).__init__()
        self.adapter = RadarReplayAdapter(root, entries)
        self.mount("https://", self.adapter)
        self.mount("http://", self.adapter)


__all__ = ["RadarReplayAdapter", "RadarReplaySession", "ReplayError", "synth_play_page"]
