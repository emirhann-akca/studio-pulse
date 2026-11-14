"""Google Ads Transparency Center: the public BigQuery dataset
`bigquery-public-data.google_ads_transparency_center` (tables `creative_stats` and
`removed_creative_stats`), read per advertiser for a game dossier.

Checked on 14 Nov 2026 with the BigQuery API and INFORMATION_SCHEMA:
- `creative_stats`: ~164 million rows, ~141 GB, neither partitioned nor clustered. One row per
  creative: advertiser_id, advertiser_disclosed_name, advertiser_legal_name, ad_format_type
  (TEXT / IMAGE / VIDEO), advertiser_location, topic, region_stats (REPEATED: region_code,
  first_shown, last_shown, times_shown_* bounds, surface_serving_stats), audience fields.
- Bytes a query is billed for are the full columns it names: advertiser_id 3.93 GB,
  advertiser_disclosed_name 3.81 GB, ad_format_type 1.04 GB, region_stats.region_code 2.25 GB,
  first_shown and last_shown 6.27 GB each. Every column that identifies an advertiser is over
  2 GB, so an exact per-advertiser lookup needs more than a 2 GB cap allows.
- `TABLESAMPLE` lowers the dry-run estimate but not the check: BigQuery compares
  `maximum_bytes_billed` with the unsampled size and refuses the job ("3812622336 or higher
  required" for the name column alone), so sampling is not a way under the cap.

So this module (1) checks the expected columns in INFORMATION_SCHEMA (cheap, once a day),
(2) builds ONE query for all requested advertisers (the scan cost is shared), (3) dry-runs it
and refuses when the estimate is over the cap, reporting the estimate, and (4) runs it with
`maximum_bytes_billed` = the cap otherwise (BigQueryExecutor). Results are cached on disk by
query hash for `cache_days`, so a weekly batch can serve every dossier of the week.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...connectors.executors import BigQueryExecutor, BytesCapExceeded
from ...llm import redact

DATASET = "bigquery-public-data.google_ads_transparency_center"
TABLE = f"`{DATASET}.creative_stats`"
EXPECTED = {
    "advertiser_id",
    "advertiser_disclosed_name",
    "ad_format_type",
    "region_stats",
    "region_stats.region_code",
    "region_stats.first_shown",
    "region_stats.last_shown",
}
SCHEMA_SQL = (
    f"SELECT field_path FROM `{DATASET}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
    "WHERE table_name = 'creative_stats'"
)
_STOP = {
    "ltd",
    "limited",
    "inc",
    "llc",
    "gmbh",
    "co",
    "corp",
    "corporation",
    "company",
    "pte",
    "sa",
    "as",
    "ab",
    "oy",
    "bv",
    "srl",
    "fz",
    "fze",
    "the",
    "games",
    "game",
    "studio",
    "studios",
    "technology",
    "technologies",
    "mobile",
    "global",
    "international",
    "sirketi",
    "anonim",
    "teknoloji",
}


def name_pattern(name: str) -> str | None:
    """'Oakever Games Ltd' -> 'oakever': the longest run of distinctive words, lower-case,
    [a-z0-9 ] only. Generic words (games, ltd, studio ...) and words under three letters end a
    run; nothing else reaches the SQL text."""
    folded = unicodedata.normalize("NFKD", (name or "").replace("ı", "i").replace("İ", "I"))
    ascii_ = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    words = re.sub(r"[^a-z0-9 ]+", " ", ascii_).split()
    # the longest contiguous run of distinctive words, so LIKE '%...%' still matches the name
    runs: list[list[str]] = [[]]
    for w in words:
        if w in _STOP or len(w) < 3:
            runs.append([])
        else:
            runs[-1].append(w)
    best = max(runs, key=lambda r: (len(" ".join(r)), -runs.index(r)))
    return " ".join(best) if best else None


def build_query(patterns: list[str]) -> str:
    """One scan for all advertisers: per advertiser and region, creatives and first/last shown."""
    if not patterns:
        raise ValueError("no advertiser pattern")
    for p in patterns:
        if not re.fullmatch(r"[a-z0-9 ]+", p):
            raise ValueError(f"unsafe advertiser pattern {p!r}")
    cond = " OR ".join(f"LOWER(advertiser_disclosed_name) LIKE '%{p}%'" for p in patterns)
    return f"""
WITH c AS (
  SELECT advertiser_id, advertiser_disclosed_name AS name, ad_format_type AS fmt, region_stats
  FROM {TABLE}
  WHERE {cond}
), adv AS (
  SELECT advertiser_id, ANY_VALUE(name) AS name, COUNT(*) AS creatives,
         COUNTIF(fmt = 'VIDEO') AS video, COUNTIF(fmt = 'IMAGE') AS image,
         COUNTIF(fmt = 'TEXT') AS text
  FROM c GROUP BY advertiser_id
), reg AS (
  SELECT c.advertiser_id, r.region_code, COUNT(*) AS creatives,
         MIN(r.first_shown) AS first_shown, MAX(r.last_shown) AS last_shown
  FROM c, UNNEST(c.region_stats) AS r GROUP BY 1, 2
)
SELECT adv.advertiser_id, adv.name, adv.creatives, adv.video, adv.image, adv.text,
       reg.region_code, reg.creatives AS region_creatives, reg.first_shown, reg.last_shown
FROM adv LEFT JOIN reg USING (advertiser_id)
ORDER BY adv.creatives DESC, adv.advertiser_id, region_creatives DESC
""".strip()


@dataclass
class AdIntelResult:
    status: str  # ok | refused | not_configured | error | schema_changed
    source: str = "google_ads_transparency_center"
    patterns: list[str] = field(default_factory=list)
    estimate_bytes: int | None = None
    cap_bytes: int | None = None
    billed_bytes: int | None = None
    advertisers: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    cached: bool = False
    ran_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "patterns": self.patterns,
            "estimate_gb": None
            if self.estimate_bytes is None
            else round(self.estimate_bytes / 1e9, 2),
            "cap_gb": None if self.cap_bytes is None else round(self.cap_bytes / 1e9, 2),
            "billed_gb": None if self.billed_bytes is None else round(self.billed_bytes / 1e9, 3),
            "advertisers": self.advertisers,
            "message": self.message,
            "cached": self.cached,
            "ran_at": self.ran_at,
            "basis": "platform_reported",
        }


def summarize(rows: list[dict[str, Any]], today: dt.date) -> list[dict[str, Any]]:
    """Flat (advertiser x region) rows -> one entry per advertiser."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        a = out.setdefault(
            r["advertiser_id"],
            {
                "advertiser_id": r["advertiser_id"],
                "name": r["name"],
                "creatives": r["creatives"],
                "formats": {"video": r["video"], "image": r["image"], "text": r["text"]},
                "regions": [],
                "first_shown": None,
                "last_shown": None,
                "url": f"https://adstransparency.google.com/advertiser/{r['advertiser_id']}",
            },
        )
        if r.get("region_code"):
            a["regions"].append(
                {
                    "region": r["region_code"],
                    "creatives": r["region_creatives"],
                    "first_shown": r["first_shown"],
                    "last_shown": r["last_shown"],
                }
            )
            for k, pick in (("first_shown", min), ("last_shown", max)):
                if r.get(k):
                    a[k] = r[k] if a[k] is None else pick(a[k], r[k])
    res = []
    for a in out.values():
        a["regions"] = sorted(a["regions"], key=lambda x: (-x["creatives"], x["region"]))[:12]
        last = a.get("last_shown")
        try:
            a["days_since_last_shown"] = (today - dt.date.fromisoformat(str(last)[:10])).days
        except ValueError:
            a["days_since_last_shown"] = None
        res.append(a)
    return sorted(res, key=lambda a: (-a["creatives"], a["advertiser_id"]))


class GoogleAdsTransparency:
    def __init__(
        self,
        *,
        project: str | None,
        cap_bytes: int,
        cache_dir: Path | None = None,
        cache_days: int = 7,
        client: Any = None,
        log: Callable[[str], None] = print,
        today: dt.date | None = None,
    ) -> None:
        self.project = project
        self.cap_bytes = int(cap_bytes)
        self.cache_dir = cache_dir
        self.cache_days = cache_days
        self.client = client
        self.log = log
        self.today = today or dt.date.today()
        self.executor: BigQueryExecutor | None = None
        self.schema_checked = False

    def _redact(self, text: Any) -> str:
        return redact(text, (self.project,) if self.project else ())

    def _exec(self) -> BigQueryExecutor:
        if self.executor is None:
            self.executor = BigQueryExecutor(
                billing_project=self.project or "",
                max_bytes_billed=self.cap_bytes,
                client=self.client,
                log=lambda m: self.log(self._redact(m)),
            )
        return self.executor

    def _cache(self, key: str) -> Path | None:
        return self.cache_dir / f"atc-{key}.json" if self.cache_dir else None

    def check_schema(self) -> list[str]:
        """Expected field paths missing from INFORMATION_SCHEMA (cached for the day)."""
        cp = self._cache(f"schema-{self.today.isoformat()}")
        if cp and cp.exists():
            paths = set(json.loads(cp.read_text(encoding="utf-8")))
        else:
            table = self._exec().run("atc_schema", SCHEMA_SQL)
            paths = set(table.column("field_path").to_pylist()) if table is not None else set()
            if cp and paths:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(sorted(paths)), encoding="utf-8")
        self.schema_checked = True
        return sorted(EXPECTED - paths)

    def lookup(self, names: list[str]) -> AdIntelResult:
        found = sorted(
            {p for p in (name_pattern(n) for n in names) if p}, key=lambda p: (len(p), p)
        )
        patterns: list[str] = []
        for p in found:  # '%pebbleworks%' already matches what '%example pebbleworks%' would
            if not any(q in p for q in patterns):
                patterns.append(p)
        patterns.sort()
        res = AdIntelResult(status="error", patterns=patterns, cap_bytes=self.cap_bytes)
        if not self.project and self.client is None:
            res.status = "not_configured"
            res.message = "no Google Cloud project (gcloud config / GOOGLE_CLOUD_PROJECT)"
            return res
        if not patterns:
            res.message = "no distinctive advertiser name to search for"
            return res
        sql = build_query(patterns)
        key = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
        cp = self._cache(key)
        if cp and cp.exists():
            d = json.loads(cp.read_text(encoding="utf-8"))
            ran = dt.date.fromisoformat(d["ran_at"][:10])
            if (self.today - ran).days <= self.cache_days:
                res.__dict__.update({k: v for k, v in d.items() if k in res.__dict__})
                res.cached = True
                return res
        try:
            missing = self.check_schema()
            if missing:
                res.status = "schema_changed"
                res.message = "columns missing from INFORMATION_SCHEMA: " + ", ".join(missing)
                return res
            ex = self._exec()
            try:
                table = ex.run("atc_advertisers", sql)
            except BytesCapExceeded:
                st = ex.stats[-1]
                res.status = "refused"
                res.estimate_bytes = st.estimated_bytes
                res.message = (
                    f"dry run: {st.estimated_bytes / 1e9:.2f} GB > cap {self.cap_bytes / 1e9:.2f} GB; "
                    "not run (nothing billed)"
                )
                return res
            st = ex.stats[-1]
            rows = table.to_pylist() if table is not None else []
            res.status = "ok"
            res.estimate_bytes, res.billed_bytes = st.estimated_bytes, st.billed_bytes
            res.advertisers = summarize(rows, self.today)
            res.ran_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
            if cp:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(res.__dict__, default=str), encoding="utf-8")
        except Exception as exc:  # auth, quota, API errors: the dossier still renders
            res.status = "error"
            res.message = self._redact(f"{type(exc).__name__}: {exc}")[:400]
        return res

    def billed_bytes(self) -> int:
        return sum(s.billed_bytes or 0 for s in (self.executor.stats if self.executor else []))
