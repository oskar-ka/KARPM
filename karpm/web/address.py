"""Working out which address to tell the user to open.

Printing `http://0.0.0.0:8080` is technically what was asked for and useless to
read: it is not an address anything can browse to. What a person wants is the
address their phone should open.
"""

from __future__ import annotations

import socket

# Binding to any of these means "every interface", not one address.
ANY = ("0.0.0.0", "::", "")
LOCAL = ("127.0.0.1", "localhost", "::1")

# TEST-NET-3. Nothing is sent - a UDP connect only consults the routing table,
# which is what names the local address - but a documentation range keeps this
# from ever being a real host if that changed.
_PROBE = ("203.0.113.1", 9)


def lan_address() -> str | None:
    """This machine's address on its own network, or None if it has none.

    A machine with no route out (unplugged, or an interface still coming up)
    genuinely has no answer here, and saying so is better than guessing.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(_PROBE)
            found = probe.getsockname()[0]
    except OSError:
        return None
    return None if found.startswith("127.") else found


def describe(host: str, port: int) -> list[str]:
    """The lines to print at startup: where it is, and who can reach it."""
    if host in LOCAL:
        return [f"KARPM web UI on http://{host}:{port}",
                "  only this machine can reach it. From another, either",
                f"  ssh -N -L {port}:localhost:{port} <this machine>,",
                "  or restart with --lan to open it to your network."]

    lines = []
    if host in ANY:
        found = lan_address()
        if found:
            lines.append(f"KARPM web UI on http://{found}:{port}")
            lines.append(f"  and http://127.0.0.1:{port} on this machine")
        else:
            lines.append(f"KARPM web UI on every interface, port {port}")
            lines.append("  this machine has no network address at the moment,")
            lines.append(f"  so for now only http://127.0.0.1:{port} works")
    else:
        lines.append(f"KARPM web UI on http://{host}:{port}")

    lines.append("  no login: anyone who can reach it can change your searches")
    lines.append("  and spend Claude credits by re-scoring")
    return lines
