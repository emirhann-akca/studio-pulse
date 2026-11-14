"""Country from the request, without storing the IP.

The IP address is used in memory only: for this lookup and, hashed with a
per-process random salt, for rate limiting. It is never written to a segment,
the quarantine, SQLite or BigQuery (tests grep every stored byte for it).

Sources, in order:
1. a trusted header (`country_header`, e.g. Cloudflare's CF-IPCountry) when
   the collector runs behind a proxy that sets it;
2. DB-IP "IP to Country Lite" (CC BY 4.0, monthly) read with the optional
   `maxminddb` package (extra `geoip`), installed by `pulse collect setup-geoip`;
3. otherwise "unknown".

setup-geoip reads the download page, takes the .mmdb.gz link and the SHA1SUM
that DB-IP publishes for the uncompressed file (verified against a real
download on 12 Nov 2026), checks it, and writes the database plus an
ATTRIBUTION.txt next to it. Tests never download; they pass a fake fetcher.
"""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

DBIP_PAGE = "https://db-ip.com/db/download/ip-to-country-lite"
ATTRIBUTION = (
    "IP to Country Lite by DB-IP (https://db-ip.com), licensed under the Creative Commons "
    "Attribution 4.0 International License (https://creativecommons.org/licenses/by/4.0/).\n"
    "studio-pulse uses it to derive a country code from the request address; the address "
    "itself is never stored.\n"
)
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
UNKNOWN = "unknown"


class GeoError(RuntimeError):
    pass


def _is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global


class CountryResolver:
    def __init__(self, mmdb: Path | None = None, header: str | None = None, reader: Any = None):
        self.header = header.lower() if header else None
        self.reader = reader
        self.source = "none"
        if reader is not None:
            self.source = "geoip"
        elif mmdb is not None and Path(mmdb).exists():
            try:
                import maxminddb
            except ImportError as exc:  # optional extra
                raise GeoError(
                    "the GeoIP database needs the maxminddb package: pip install 'studio-pulse[geoip]'"
                ) from exc
            self.reader = maxminddb.open_database(str(mmdb))
            self.source = "geoip"
        if self.header:
            self.source = f"header:{header}" + ("+geoip" if self.reader is not None else "")

    def country(self, headers: dict[str, str], ip: str | None) -> str:
        if self.header:
            v = (headers.get(self.header) or "").strip().upper()
            if COUNTRY_RE.match(v) and v not in ("XX", "T1"):  # Cloudflare: unknown / Tor
                return v
        if self.reader is not None and ip and _is_public(ip):
            try:
                rec = self.reader.get(ip) or {}
            except ValueError:
                return UNKNOWN
            code = ((rec.get("country") or {}).get("iso_code") or "").upper()
            if COUNTRY_RE.match(code):
                return code
        return UNKNOWN

    def close(self) -> None:
        close = getattr(self.reader, "close", None)
        if callable(close):
            close()


def _default_fetch(url: str) -> bytes:
    import requests

    from .. import __version__

    r = requests.get(
        url, timeout=60, headers={"User-Agent": f"studio-pulse/{__version__} (setup-geoip)"}
    )
    r.raise_for_status()
    return r.content


def parse_download_page(html: str) -> tuple[str, str]:
    """(mmdb.gz url, sha1 of the uncompressed .mmdb) from DB-IP's lite download page."""
    m = re.search(
        r"https://download\.db-ip\.com/free/dbip-country-lite-\d{4}-\d{2}\.mmdb\.gz", html
    )
    if not m:
        raise GeoError("no .mmdb.gz link on the DB-IP download page")
    # Each format's MD5SUM and SHA1SUM are listed before its download link: take
    # the SHA1SUM closest before the .mmdb link.
    sums = re.findall(r"SHA1SUM</dt>\s*<dd[^>]*>\s*([0-9a-f]{40})", html[: m.start()])
    if not sums:
        raise GeoError("no SHA1SUM for the .mmdb file on the DB-IP download page")
    return m.group(0), sums[-1]


def setup_geoip(
    dest_dir: Path,
    *,
    fetch: Callable[[str], bytes] | None = None,
    url: str | None = None,
    sha1: str | None = None,
    verify_open: bool = True,
    log: Callable[[str], Any] = print,
) -> Path:
    """Download, verify (SHA1 of the .mmdb) and install the DB-IP country database."""
    fetch = fetch or _default_fetch
    if not url or not sha1:
        page_url, page_sha = parse_download_page(fetch(DBIP_PAGE).decode("utf-8", "replace"))
        url, sha1 = url or page_url, sha1 or page_sha
    log(f"downloading {url}")
    raw = fetch(url)
    try:
        data = gzip.decompress(raw) if url.endswith(".gz") else raw
    except OSError as exc:
        raise GeoError(f"not a gzip file: {exc}") from exc
    # DB-IP publishes only a SHA-1; the file comes over https from DB-IP itself
    got = hashlib.sha1(data).hexdigest()  # noqa: S324  # nosec B324
    if got != sha1.lower():
        raise GeoError(f"checksum mismatch: SHA1 {got}, expected {sha1}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    month = re.search(r"(\d{4}-\d{2})", url)
    final = dest_dir / "dbip-country-lite.mmdb"
    tmp = dest_dir / f".dbip-{month.group(1) if month else 'download'}.tmp"
    tmp.write_bytes(data)
    if verify_open:
        try:
            CountryResolver(tmp).close()
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise GeoError(f"the downloaded database does not open: {exc}") from exc
    tmp.replace(final)
    (dest_dir / "ATTRIBUTION.txt").write_text(
        ATTRIBUTION + f"Source: {url}\nSHA1 (uncompressed): {got}\n", encoding="utf-8"
    )
    log(f"installed {final} (SHA1 {got}); attribution in {dest_dir / 'ATTRIBUTION.txt'}")
    return final
