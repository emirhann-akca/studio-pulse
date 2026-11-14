"""Checks for outbound URLs that come from configuration, the environment or data.

Used where a URL is not a constant of this package: the OpenAI-compatible LLM server
(`PULSE_LLM_BASE_URL`, often a GPU box on the LAN) and the Slack webhook. The radar's
store fetcher has its own, stricter host allow-list (radar/http.py).

What is refused: schemes other than the ones the caller names, user:password@ in the
URL (credentials belong in headers, and they end up in logs), cloud metadata endpoints
(a request there from a cloud VM hands out its credentials), link-local addresses, and,
unless the caller allows them, loopback and private addresses. Host names are not
resolved here, so a public name that resolves to a private address is not caught; the
callers that need more pin the host (radar) or only send to an address the operator set.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

METADATA_HOSTS = frozenset(
    {"169.254.169.254", "fd00:ec2::254", "metadata.google.internal", "metadata", "100.100.100.200"}
)


class UnsafeURL(ValueError):
    """The URL was refused before anything was sent to it."""


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_local(host: str) -> bool:
    """Loopback, a private (RFC 1918 / ULA) address or a .local / localhost name."""
    h = host.lower().strip("[]")
    if h == "localhost" or h.endswith((".local", ".localhost", ".lan", ".internal")):
        return True
    ip = _ip(h)
    return bool(ip and (ip.is_loopback or ip.is_private) and not ip.is_link_local)


def check_url(
    url: str,
    *,
    schemes: tuple[str, ...] = ("https",),
    allow_local: bool = False,
    what: str = "URL",
) -> str:
    """Return `url` unchanged, or raise UnsafeURL saying why it was refused."""
    try:
        p = urlsplit(str(url).strip())
        port = p.port
    except ValueError as exc:
        raise UnsafeURL(f"{what} is not a valid URL: {exc}") from None
    del port  # parsed only to reject a malformed port
    host = (p.hostname or "").lower()
    if p.scheme not in schemes:
        raise UnsafeURL(f"{what} must use {' or '.join(schemes)}, not {p.scheme or 'nothing'}")
    if not host:
        raise UnsafeURL(f"{what} has no host")
    if p.username is not None or p.password is not None:
        raise UnsafeURL(f"{what} must not carry user:password@; put credentials in the environment")
    ip = _ip(host)
    if host in METADATA_HOSTS or (ip is not None and (ip.is_link_local or ip.is_multicast)):
        raise UnsafeURL(f"{what} points at a link-local or cloud metadata address ({host})")
    if not allow_local and (is_local(host) or (ip is not None and not ip.is_global)):
        raise UnsafeURL(f"{what} points at this machine or a private network ({host})")
    return url
