from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

from capalis.adapters.http import TransportError, UrllibTransport
from capalis.adapters.net_safety import UnsafeAddress, check_origin, is_public, public_addresses
from capalis.adapters.web import HttpGetAdapter
from capalis.core.model import Target
from capalis.runtime.ports import Invocation, Outcome
from fakes import resolve


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:0:10.0.0.1",
    "192.0.2.1",
])
def test_internal_and_reserved_addresses_are_not_public(address: str) -> None:
    assert not is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_ordinary_internet_addresses_are_public(address: str) -> None:
    assert is_public(ipaddress.ip_address(address))


def test_origins_are_checked_by_name_and_by_every_answer() -> None:
    assert check_origin("https://shop.example.com", resolve) is None
    for origin, why in (("https://localhost", "internal host name"), ("https://app.localhost", "internal host name"),
                        ("https://metadata.google.internal", "internal host name"),
                        ("https://intranet.example", "private or internal"), ("https://10.0.0.5", "private"),
                        ("https://nowhere.invalid", "does not resolve"), ("http://shop.example.com", "plain https"),
                        ("https://user:pw@shop.example.com", "plain https")):
        reason = check_origin(origin, resolve)
        assert reason is not None and why in reason, (origin, reason)

    def mixed(host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    assert "private or internal" in (check_origin("https://half.example", mixed) or "")


def test_the_web_client_never_connects_to_an_internal_address() -> None:
    # A name that passed an earlier check but now resolves inward (DNS rebinding) is refused at connect time.
    transport = UrllibTransport(public_only=True, resolve=resolve)
    with pytest.raises(TransportError) as info:
        transport.send("GET", "https://rebind.example/", {}, None, 1)
    assert info.value.refused and not info.value.sent
    with pytest.raises(UnsafeAddress, match="private or internal"):
        public_addresses("rebind.example", 443, resolve)


def test_web_checks_refuse_internal_origins_at_request_time() -> None:
    sent: list[str] = []

    class Never:
        def send(self, *args: Any, **kwargs: Any) -> Any:
            sent.append(args[1])
            raise AssertionError("must not be called")

    adapter = HttpGetAdapter(Never(), resolve=resolve)
    for origin in ("https://intranet.example", "https://169.254.169.254", "https://localhost"):
        target = Target("http.origin", origin)
        result = adapter.invoke(Invocation(adapter.capability_id, target, {"origin": origin, "path": "/"}, "k", 5))
        assert result.outcome is Outcome.REJECTED and "public addresses" in (result.error or ""), origin
    assert sent == []
