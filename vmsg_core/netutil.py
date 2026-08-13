"""
Socket binding helpers and connection limits.

Ported from the BenchForge project (H:\\AG\\BenchForge, core/netutil.py), which
solved these problems first for its instrument emulators. VMSG has the same
shape of problem - long-lived TCP listeners serving instrument traffic - so the
same answers apply.

Why this module exists
----------------------
Creating a TCP listener is not as portable as it looks.

On POSIX, ``SO_REUSEADDR`` lets a listener rebind a port still held in TIME_WAIT
by a closed connection. Useful, and harmless: a second *live* bind to the same
address is still refused.

On Windows the same option means something else entirely - it permits two live
sockets to bind the *same* address. A second VMSG instance then starts
"successfully" on port 1234 and the operating system delivers connections to
whichever socket it chooses. A stale process keeps answering some fraction of
TestController's traffic, and the symptom looks like intermittent gateway
flakiness rather than a port conflict.

Windows spells the semantics we actually want ``SO_EXCLUSIVEADDRUSE``:
conflicting binds fail loudly with WinError 10048, which callers surface to the
user.

``asyncio.start_server(..., reuse_address=True)`` gives us the dangerous
behaviour, so servers here pre-create their socket with
:func:`create_tcp_listener` and hand it to asyncio via ``sock=``.
"""

import asyncio
import socket
import sys
import threading
from typing import Set

_IS_WINDOWS = sys.platform == "win32"


# Safety envelopes. These are gateway resource limits, not claims about what
# physical Prologix hardware rejects.
MAX_PENDING_TEXT_CHARS = 64 * 1024
MAX_RPC_RECORD_BYTES = 1024 * 1024
DEFAULT_MAX_CLIENT_HANDLERS = 64


class ClientLimiter:
    """Tracks accepted connections and enforces a hard per-server ceiling.

    Without this an unauthenticated listener will happily accept connections
    until the process runs out of descriptors or memory.
    """

    def __init__(self, limit: int = DEFAULT_MAX_CLIENT_HANDLERS):
        if limit < 1:
            raise ValueError("client limit must be at least 1")
        self.limit = limit
        self._clients: Set[object] = set()
        self._lock = threading.Lock()

    def admit(self, key: object) -> bool:
        """Reserve a handler slot for *key*, returning False when full."""
        with self._lock:
            if len(self._clients) >= self.limit:
                return False
            self._clients.add(key)
            return True

    def release(self, key: object) -> None:
        with self._lock:
            self._clients.discard(key)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._clients)


def create_tcp_listener(host: str, port: int, backlog: int = 128) -> socket.socket:
    """Return a bound, listening TCP socket that refuses to share its address.

    Raises OSError if the address is already in use, on every platform.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if _IS_WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(backlog)
    except Exception:
        sock.close()
        raise
    return sock


async def start_exclusive_server(client_connected_cb, host: str, port: int,
                                 backlog: int = 128) -> asyncio.AbstractServer:
    """asyncio.start_server with exclusive address semantics.

    Drop-in for ``asyncio.start_server(cb, host, port, reuse_address=True)``
    without the Windows double-bind hazard.
    """
    sock = create_tcp_listener(host, port, backlog=backlog)
    try:
        return await asyncio.start_server(client_connected_cb, sock=sock)
    except Exception:
        sock.close()
        raise


def create_multicast_listener(port: int, group: str) -> socket.socket:
    """Return a UDP socket bound for multicast reception.

    Multicast is the one case where address sharing is correct: mDNS responders
    are expected to coexist with Bonjour and other discovery services on 5353,
    so this deliberately keeps SO_REUSEADDR on every platform.
    """
    import struct

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        mreq = struct.pack("4sl", socket.inet_aton(group), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except Exception:
        sock.close()
        raise
    return sock


def describe_bind_error(host: str, port: int, err: OSError) -> str:
    """A message that names the likely cause instead of just the errno."""
    winerror = getattr(err, "winerror", None)
    if winerror in (10048, 10013) or err.errno in (48, 98):
        return (f"Cannot bind {host}:{port} - the address is already in use. "
                f"Another VMSG instance (or another program) is holding that "
                f"port. Stop it and retry.")
    return f"Cannot bind {host}:{port} - {err}"
