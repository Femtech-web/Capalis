"""Where Capalis's credential-free web reads may go: public addresses only.

The origin is model-written and shadow runs read for real, so an internal address (loopback, a private
network, cloud metadata at 169.254.169.254) must never be reachable. The check runs when a Recipe is
compiled and again on every request, and the request connects to the exact address that was checked, so a
DNS answer that changes in between (rebinding) can't redirect it. The rules mirror Hermes's
``tools.url_safety``.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

Resolver = Callable[..., list[tuple[Any, ...]]]

_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal", "metadata.goog"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")


class UnsafeAddress(ValueError):
    """The host is, or resolves to, an address Capalis must not reach."""


def _embedded_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    # ipaddress reads IPv4-mapped and IPv4-translated IPv6 as unrelated v6 space; classify the IPv4 inside.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _IPV4_TRANSLATED:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _embedded_ipv4(ip)
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                                 or ip.is_multicast or ip.is_unspecified or ip in _CGNAT)


def public_addresses(host: str, port: int, resolve: Resolver = socket.getaddrinfo) -> list[tuple[int, tuple[Any, ...]]]:
    """Every address ``host`` resolves to, as ``(family, sockaddr)``, if all of them are public."""
    name = host.strip().lower().rstrip(".")
    if not name or name in _BLOCKED_HOSTS or name.endswith(".localhost"):
        raise UnsafeAddress(f"{host} is an internal host name")
    try:
        answers = resolve(name, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeAddress(f"{host} does not resolve") from exc
    found = []
    for family, _, _, _, sockaddr in answers:
        try:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeAddress(f"{host} resolved to an unreadable address") from exc
        # One private answer is enough to refuse: the resolver may hand out any of them.
        if not is_public(ip):
            raise UnsafeAddress(f"{host} resolves to a private or internal address ({ip})")
        found.append((family, sockaddr))
    if not found:
        raise UnsafeAddress(f"{host} does not resolve")
    return found


def check_origin(origin: str, resolve: Resolver = socket.getaddrinfo) -> str | None:
    """None when every address of the https origin is public; otherwise the reason it isn't allowed."""
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return f"{origin} is not a plain https origin"
    try:
        public_addresses(parts.hostname, parts.port or 443, resolve)
    except UnsafeAddress as exc:
        return str(exc)
    return None
