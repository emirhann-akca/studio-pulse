"""Recorded store responses for dossier tests and `pulse radar dossier --replay DIR`.

The radar's replay format (radar/replay.py) plus two things a dossier needs:
- `"host": "itunes.apple.com"` on an entry restricts it to that host (both stores answer
  `/robots.txt`); entries with a host are matched first.
- `"play_page": {"fields": "play/x.json", "reviews": "play/x_reviews.json"}` builds a Play
  details page from recorded parser fields (`synth_play_page`) plus a reviews block shaped like
  the one Play embeds (`synth_reviews_block`), so both real parsers run on it.
Binary files (screenshots) are served as recorded bytes with their `content_type`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from requests.models import PreparedRequest, Response

from ...connectors.replay import ReplaySession
from ..replay import RadarReplayAdapter, synth_play_page


def synth_reviews_block(reviews: list[dict[str, Any]]) -> str:
    """A `ds:` data block with reviews where Play puts them (id, author, stars, text, time,
    thumbs, reply, version). Author fields are synthetic placeholders."""
    rows = []
    for i, r in enumerate(reviews):
        row: list[Any] = [None] * 11
        row[0] = r.get("id", f"review-{i}")
        row[1] = ["Example Player", [None, 2, None, [None, None, "https://example.invalid/a.png"]]]
        row[2] = int(r["score"])
        row[4] = r["text"]
        row[5] = [int(r["ts"]), 0]
        row[6] = int(r.get("thumbs", 0))
        row[7] = ["Example Dev", r["reply"], [int(r["ts"]) + 3600, 0]] if r.get("reply") else None
        row[9] = ["0", "Example Player"]
        row[10] = r.get("version")
        rows.append(row)
    data = json.dumps([rows, None], ensure_ascii=False)
    return f"<script>AF_initDataCallback({{key: 'ds:10', hash: '7', data:{data}, sideChannel: {{}}}});</script>"


class DossierReplayAdapter(RadarReplayAdapter):
    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        parts = urlsplit(request.url or "")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for e in self.entries:
            if e.get("host") and e["host"] != parts.hostname:
                continue
            if e["path"] != parts.path:
                continue
            if any(query.get(k) != str(v) for k, v in (e.get("query") or {}).items()):
                continue
            if "play_page" in e:
                pp = e["play_page"]
                fields = json.loads((self.root / pp["fields"]).read_text(encoding="utf-8"))
                revs = (
                    json.loads((self.root / pp["reviews"]).read_text(encoding="utf-8"))
                    if pp.get("reviews")
                    else []
                )
                html = synth_play_page(fields).replace(
                    "</body>", synth_reviews_block(revs) + "</body>"
                )
                return self._raw(request, html.encode("utf-8"), "text/html; charset=utf-8", 200)
            if e.get("host"):
                body = (
                    (self.root / e["file"]).read_bytes()
                    if "file" in e
                    else json.dumps(e.get("body", {})).encode("utf-8")
                )
                return self._raw(
                    request,
                    body,
                    e.get("content_type", "application/json"),
                    int(e.get("status", 200)),
                )
        return super().send(request, **kwargs)

    def _raw(self, request: PreparedRequest, body: bytes, ctype: str, status: int) -> Response:
        self.requests.append(self._recorded(request))
        resp = Response()
        resp.status_code = status
        resp.headers["Content-Type"] = ctype
        resp._content = body
        resp.encoding = "utf-8"
        resp.url = request.url or ""
        resp.request = request
        resp.reason = "OK" if status < 400 else "Recorded error"
        return resp


class DossierReplaySession(ReplaySession):
    def __init__(self, root: str | Path, entries: list[dict[str, Any]] | None = None) -> None:
        super(ReplaySession, self).__init__()
        self.adapter = DossierReplayAdapter(root, entries)
        self.mount("https://", self.adapter)
        self.mount("http://", self.adapter)
