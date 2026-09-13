"""The startup banner: which address to tell someone to open.

`http://0.0.0.0:8080` is what was asked for and useless to read, so binding to
every interface has to be reported as an address a phone can actually open.
"""

import socket

import pytest

from karpm.web import address


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_localhost_says_so_and_offers_a_way_in(host):
    lines = address.describe(host, 8080)
    assert f"http://{host}:8080" in lines[0]
    joined = " ".join(lines)
    assert "only this machine" in joined
    assert "ssh -N -L 8080:localhost:8080" in joined
    assert "--lan" in joined
    # Nothing is exposed, so there is nothing to warn about.
    assert "no login" not in joined


def test_binding_everything_prints_the_address_rather_than_0000(monkeypatch):
    monkeypatch.setattr(address, "lan_address", lambda: "192.168.1.42")
    lines = address.describe("0.0.0.0", 8080)
    assert lines[0] == "KARPM web UI on http://192.168.1.42:8080"
    assert "0.0.0.0" not in " ".join(lines)
    assert "http://127.0.0.1:8080" in lines[1]


def test_binding_everything_warns_about_the_missing_login(monkeypatch):
    monkeypatch.setattr(address, "lan_address", lambda: "192.168.1.42")
    assert "no login" in " ".join(address.describe("0.0.0.0", 8080))


def test_a_machine_with_no_network_says_so(monkeypatch):
    """Better than printing an address that does not exist."""
    monkeypatch.setattr(address, "lan_address", lambda: None)
    joined = " ".join(address.describe("0.0.0.0", 8080))
    assert "no network address" in joined
    assert "http://127.0.0.1:8080" in joined


def test_an_explicit_address_is_printed_as_given(monkeypatch):
    monkeypatch.setattr(address, "lan_address", lambda: "192.168.1.42")
    lines = address.describe("10.0.0.5", 9000)
    assert lines[0] == "KARPM web UI on http://10.0.0.5:9000"
    assert "no login" in " ".join(lines)


def test_the_port_is_carried_through():
    assert "9999" in " ".join(address.describe("127.0.0.1", 9999))


def test_lan_address_is_a_real_address_or_nothing():
    found = address.lan_address()
    if found is None:
        return                          # a machine with no route out
    socket.inet_aton(found)             # raises if it is not an IPv4 address
    assert not found.startswith("127."), "loopback is not a LAN address"


def test_lan_address_sends_nothing(monkeypatch):
    """It reads the routing table. A banner must not make a network request."""
    sent = []

    class Probe:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def settimeout(self, _): pass
        def connect(self, target): sent.append(("connect", target))
        def send(self, *a): sent.append(("send", a))
        def sendto(self, *a): sent.append(("sendto", a))
        def getsockname(self): return ("192.168.1.42", 51234)

    monkeypatch.setattr(socket, "socket", lambda *a, **k: Probe())
    assert address.lan_address() == "192.168.1.42"
    assert [kind for kind, _ in sent] == ["connect"]


def test_no_route_is_not_an_error(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(socket, "socket", refuse)
    assert address.lan_address() is None


def test_loopback_only_counts_as_no_lan_address(monkeypatch):
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def settimeout(self, _): pass
        def connect(self, _): pass
        def getsockname(self): return ("127.0.1.1", 51234)

    monkeypatch.setattr(socket, "socket", lambda *a, **k: Probe())
    assert address.lan_address() is None
