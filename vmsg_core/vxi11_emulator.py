"""
VXI-11 / ONC-RPC gateway emulator, modelled on a physical Agilent E5810A.

Every behaviour here was measured against real hardware and is recorded in
`profiles/E5810_HARDWARE_PROFILE.md` section 3b, with the raw capture in
`profiles/e5810_vxi11_capture.json`. Where a value looks arbitrary it is
because the hardware chose it; the comments say so.

Dependency-free by design: the verification tools may lean on PyVISA, but the
emulator itself is sockets and struct only.

Two servers cooperate:
    portmapper (TCP/UDP 111) -- answers GETPORT for the core channel only
    core channel (TCP 1024)  -- create_link, device_write, device_read, ...
"""
import random
import socket
import struct
import threading
import time
from typing import Dict, Optional

from .vxi11_bridge import VmsgInstrumentRegistry as InstrumentRegistry, VirtualInstrument
from .diagnostics import ERROR_MEANINGS, INFO, WARN, DiagnosticEmitter
from .netutil import (
    DEFAULT_MAX_CLIENT_HANDLERS, MAX_RPC_RECORD_BYTES, ClientLimiter,
    create_tcp_listener,
)

# --- ONC RPC ---------------------------------------------------------------
MSG_CALL, MSG_REPLY = 0, 1
MSG_ACCEPTED = 0
SUCCESS, PROG_UNAVAIL, PROG_MISMATCH, PROC_UNAVAIL, GARBAGE_ARGS = 0, 1, 2, 3, 4

PORTMAP_PROG, PORTMAP_VERS = 100000, 2
PMAPPROC_NULL, PMAPPROC_GETPORT, PMAPPROC_DUMP = 0, 3, 4

CORE_PROG, CORE_VERS = 0x0607AF, 1      # 395183
ABORT_PROG = 0x0607B0                   # 395184
INTR_PROG, INTR_VERS = 0x0607B1, 1      # 395185

CREATE_LINK, DEVICE_WRITE, DEVICE_READ, DEVICE_READSTB = 10, 11, 12, 13
DEVICE_TRIGGER, DEVICE_CLEAR, DEVICE_REMOTE, DEVICE_LOCAL = 14, 15, 16, 17
DEVICE_LOCK, DEVICE_UNLOCK, DEVICE_ENABLE_SRQ = 18, 19, 20
DEVICE_DOCMD, DESTROY_LINK = 22, 23
CREATE_INTR_CHAN, DESTROY_INTR_CHAN = 25, 26

# --- VXI-11 error codes, as the hardware returns them ----------------------
ERR_NONE = 0
ERR_SYNTAX = 1
ERR_NOT_ACCESSIBLE = 3
ERR_INVALID_LINK = 4
ERR_PARAMETER = 5
ERR_NOT_SUPPORTED = 8
ERR_OUT_OF_RESOURCES = 9
ERR_LOCKED_BY_ANOTHER = 11
ERR_NO_LOCK_HELD = 12
ERR_CHANNEL_NOT_ESTABLISHED = 6
ERR_IO_TIMEOUT = 15
ERR_IO_ERROR = 17

#: The one procedure on the interrupt program, called BY the gateway.
DEVICE_INTR_SRQ = 30

# --- device_read reason bits ----------------------------------------------
REASON_REQCNT, REASON_CHR, REASON_END = 0x01, 0x02, 0x04
READ_TERMCHRSET = 0x80


def xdr_opaque(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data + b"\x00" * ((-len(data)) % 4)


def read_xdr_opaque(buf: bytes, off: int):
    (length,) = struct.unpack(">I", buf[off:off + 4])
    off += 4
    data = buf[off:off + length]
    return data, off + length + ((-length) % 4)


class EmulatedLink:
    """Server-side state for one VXI-11 link."""

    def __init__(self, lid: int, device: str, address: Optional[int],
                 client_id: int):
        self.lid = lid
        self.device = device
        self.address = address
        self.client_id = client_id
        self.pending = b""          # instrument output buffer
        self.locked_by: Optional[int] = None


class VXI11EmulatorServer(DiagnosticEmitter):
    """
    Emulates the VXI-11 side of an E5810A LAN/GPIB gateway.

    MEASURED constants, all constant across every link on the real unit:
        abortPort    975
        maxRecvSize  16384
        core channel registered on port 1024; abort and interrupt channels
        are NOT registered with the portmapper at all.
    """

    DIAGNOSTIC_SOURCE = "e5810"

    #: MEASURED: the abort port is NOT constant. Three consecutive runs against
    #: the same unit advertised 975, 1005 and 1002 -- it is allocated per boot,
    #: near the core channel. A fixed 975, which we shipped briefly, is wrong.
    ABORT_PORT_RANGE = (975, 1010)
    MAX_RECV_SIZE = 16384

    #: The hardware waits this much longer than the io_timeout a client asks
    #: for. MEASURED: 500 ms -> 666 ms, 2000 ms -> 2167 ms.
    TIMEOUT_OVERHEAD_MS = 166

    #: Interface names the gateway accepts. MEASURED: every address 0-31 links
    #: successfully whether or not an instrument is present -- presence is only
    #: discovered when a read times out -- and the bare interface name works
    #: too. 'inst0' is REJECTED; this gateway has no such logical device.
    INTERFACE = "gpib0"
    MAX_GPIB_ADDRESS = 31

    def __init__(self, host="0.0.0.0", core_port=1024, portmap_port=111,
                 registry: Optional[InstrumentRegistry] = None):
        self.host = host
        self.core_port = core_port
        self.portmap_port = portmap_port
        # VMSG port: the registry resolves against live VMSG mappings, so it has
        # no meaningful default. Requiring it here beats a confusing TypeError.
        if registry is None:
            raise ValueError(
                "VXI11EmulatorServer requires a registry "
                "(vmsg_core.vxi11_bridge.VmsgInstrumentRegistry)")
        self.registry = registry

        self.links: Dict[int, EmulatedLink] = {}
        self._lock = threading.Lock()
        self._running = False
        self._sockets = []
        self._threads = []
        self._client_limiter = ClientLimiter(DEFAULT_MAX_CLIENT_HANDLERS)
        self.packet_callbacks = []
        # Mirrors the Prologix server's attribute so the GUI can treat either
        # gateway the same way.
        self.active_clients: Dict[str, str] = {}
        self.connection_policy = "vxi11"

        # Allocated once per run, like the hardware allocates it per boot.
        self.abort_port = random.randint(*self.ABORT_PORT_RANGE)

        # Interrupt channel state. MEASURED as fully supported: create_intr_chan
        # and device_enable_srq both return 0, and the gateway holds the
        # callback connection open until destroy_intr_chan.
        self._intr_sock: Optional[socket.socket] = None
        self._intr_target = None
        self._srq_handles: Dict[int, bytes] = {}

        # Link identifiers on the real unit look like heap pointers: large,
        # non-sequential, mostly descending by ~1856. Small sequential integers
        # would be an obvious tell, so mimic the shape rather than the values.
        self._next_lid = random.randint(29_000_000, 33_000_000)

    # -- notification -------------------------------------------------------
    def add_warning_callback(self, cb):
        """Protocol misuse only -- a filtered view of the diagnostic channel."""
        def only_warnings(record):
            if record.get("level") == WARN and record.get("code") is not None:
                cb(record)

        self.add_diagnostic_callback(only_warnings)

    def add_packet_callback(self, cb):
        if cb not in self.packet_callbacks:
            self.packet_callbacks.append(cb)

    def _notify_packet(self, direction, data, address):
        event = {
            "timestamp": time.strftime("%H:%M:%S.")
            + f"{int(time.time() * 1000) % 1000:03d}",
            "client": "vxi11", "direction": direction, "address": address,
            "raw_bytes": data, "text": data.decode("utf-8", errors="replace"),
            "latency_ms": 0.0, "policy": "vxi11",
        }
        for cb in self.packet_callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _raise_error(self, address, code):
        device = self.registry.get_device(address) if address is not None else None
        if device is None:
            return
        device.raise_error(code)
        text = VirtualInstrument.SCPI_ERRORS.get(code, ("Unknown error", 0))[0]
        self.diagnose(
            WARN, f'{code},"{text}"', ERROR_MEANINGS.get(code, ""),
            address=address, code=code, device=device.name,
            extra={"entry": f'{code},"{text}"', "text": text})

    # -- link helpers -------------------------------------------------------
    def _allocate_lid(self) -> int:
        with self._lock:
            lid = self._next_lid
            self._next_lid -= 1856          # MEASURED spacing on the hardware
            return lid & 0xFFFFFFFF

    def _parse_device(self, device: str):
        """
        Resolves a VXI-11 device string to a GPIB address.

        Returns (address, error). address is None for the bare interface name.
        MEASURED: any address 0-31 links successfully, present or not.
        """
        name = device.strip()
        if name == self.INTERFACE:
            return None, ERR_NONE
        # There is deliberately no lenient mode. 'inst0' is refused because the
        # hardware refuses it; a client that hardcodes it must see the same
        # failure here that it would see on the bench. The debug log explains
        # the refusal rather than leaving a bare error 3 to be puzzled over.
        if not name.startswith(self.INTERFACE + ","):
            return None, ERR_NOT_ACCESSIBLE
        suffix = name[len(self.INTERFACE) + 1:]
        if not suffix.isdigit():
            return None, ERR_NOT_ACCESSIBLE
        address = int(suffix)
        if address > self.MAX_GPIB_ADDRESS:
            return None, ERR_NOT_ACCESSIBLE
        return address, ERR_NONE

    # -- core channel procedures -------------------------------------------
    def _create_link(self, args: bytes) -> bytes:
        client_id, _lock_device, _lock_timeout = struct.unpack(">III", args[:12])
        device, _ = read_xdr_opaque(args, 12)
        name = device.decode(errors="replace")
        address, error = self._parse_device(name)
        if error:
            # Worth explaining rather than merely refusing: a client that
            # hardcodes 'inst0' gets a bare error 3 and no hint why, which is
            # a genuinely expensive thing to debug from the client side.
            if name.strip() == "inst0":
                detail = ("This gateway has no logical device 'inst0' -- "
                          "MEASURED on the physical E5810A. GPIB instruments "
                          "are addressed as 'gpib0,<primary address>'. "
                          "See docs/TESTCONTROLLER_OBSERVATIONS.md.")
            else:
                detail = ("Accepted device strings are 'gpib0' and "
                          "'gpib0,0'..'gpib0,%d'." % self.MAX_GPIB_ADDRESS)
            self.diagnose(WARN, "create_link refused: %r" % name, detail,
                          code=error)
            return struct.pack(">IIII", error, 0, 0, 0)

        link = EmulatedLink(self._allocate_lid(), name, address, client_id)
        with self._lock:
            self.links[link.lid] = link
        known = (self.registry.get_device(address) is not None
                 if address is not None else False)
        self.diagnose(
            INFO, "link created: %s" % name,
            "lid=%d%s" % (link.lid,
                          "" if address is None or known
                          else "  (no instrument at this address -- the real "
                               "gateway links anyway and fails on read)"),
            address=address)
        return struct.pack(">IIII", ERR_NONE, link.lid, self.abort_port,
                           self.MAX_RECV_SIZE)

    def _link_for(self, lid):
        with self._lock:
            return self.links.get(lid)

    def _device_write(self, args: bytes) -> bytes:
        lid, _io_timeout, _lock_timeout, _flags = struct.unpack(">IIII", args[:16])
        data, _ = read_xdr_opaque(args, 16)
        link = self._link_for(lid)
        if link is None:
            # MEASURED: a write on a destroyed link reports an I/O TIMEOUT,
            # not the invalid-link error most implementations return.
            return struct.pack(">II", ERR_IO_TIMEOUT, 0)
        if link.address is None:
            return struct.pack(">II", ERR_NONE, len(data))

        self._notify_packet("IN", data, link.address)
        text = data.decode("utf-8", errors="replace").strip()

        # A new query while an earlier reply is unread discards it, exactly as
        # on the GPIB side of a Prologix.
        if link.pending:
            self._raise_error(link.address, VirtualInstrument.ERR_QUERY_INTERRUPTED)
            link.pending = b""

        reply = self.registry.process_command(link.address, text)
        if reply is not None:
            payload = reply if reply.endswith("\n") else reply + "\n"
            link.pending = payload.encode("utf-8", errors="replace")
        return struct.pack(">II", ERR_NONE, len(data))

    def _device_read(self, args: bytes) -> bytes:
        (lid, request_size, io_timeout, _lock_timeout, flags,
         term_char) = struct.unpack(">IIIIII", args[:24])
        link = self._link_for(lid)
        if link is None:
            return struct.pack(">II", ERR_IO_TIMEOUT, 0) + xdr_opaque(b"")

        if not link.pending:
            # MEASURED: the gateway waits the requested io_timeout plus a
            # consistent ~166 ms, then reports error 15 with reason 0.
            time.sleep((io_timeout + self.TIMEOUT_OVERHEAD_MS) / 1000.0)
            if link.address is not None:
                self._raise_error(link.address,
                                  VirtualInstrument.ERR_QUERY_UNTERMINATED)
            return struct.pack(">II", ERR_IO_TIMEOUT, 0) + xdr_opaque(b"")

        buffer = link.pending
        reason = 0

        # Terminator match, when the client asked for one.
        cut = len(buffer)
        if flags & READ_TERMCHRSET:
            idx = buffer.find(bytes([term_char & 0xFF]))
            if idx != -1:
                cut = idx + 1
                reason |= REASON_CHR

        # The buffer filling stops the read first, and leaves the remainder.
        if request_size and cut > request_size:
            cut = request_size
            reason = REASON_REQCNT

        chunk, link.pending = buffer[:cut], buffer[cut:]
        # END is asserted only when the instrument's whole message has gone.
        if not link.pending:
            reason |= REASON_END
        if not reason:
            reason = REASON_REQCNT

        self._notify_packet("OUT", chunk, link.address)
        return struct.pack(">II", ERR_NONE, reason) + xdr_opaque(chunk)

    def _device_readstb(self, args: bytes) -> bytes:
        (lid,) = struct.unpack(">I", args[:4])
        link = self._link_for(lid)
        if link is None:
            return struct.pack(">II", ERR_IO_TIMEOUT, 0)
        # MEASURED: a poll of an absent address returns 0 with no error.
        device = (self.registry.get_device(link.address)
                  if link.address is not None else None)
        if device is None:
            return struct.pack(">II", ERR_NONE, 0)
        stb = device.status_byte_base | (0x10 if link.pending else 0)
        return struct.pack(">II", ERR_NONE, stb)

    def _device_generic(self, args: bytes, clear=False) -> bytes:
        """device_clear / trigger / remote / local -- all MEASURED as error 0."""
        (lid,) = struct.unpack(">I", args[:4])
        link = self._link_for(lid)
        if link is None:
            return struct.pack(">I", ERR_IO_TIMEOUT)
        if clear:
            link.pending = b""
        return struct.pack(">I", ERR_NONE)

    def _device_lock(self, args: bytes) -> bytes:
        (lid,) = struct.unpack(">I", args[:4])
        link = self._link_for(lid)
        if link is None:
            return struct.pack(">I", ERR_IO_TIMEOUT)
        with self._lock:
            for other in self.links.values():
                if (other is not link and other.address == link.address
                        and other.locked_by is not None):
                    return struct.pack(">I", ERR_LOCKED_BY_ANOTHER)
            link.locked_by = link.client_id
        return struct.pack(">I", ERR_NONE)

    def _device_unlock(self, args: bytes) -> bytes:
        (lid,) = struct.unpack(">I", args[:4])
        link = self._link_for(lid)
        if link is None:
            return struct.pack(">I", ERR_IO_TIMEOUT)
        if link.locked_by is None:
            # MEASURED: unlocking a link that holds no lock.
            return struct.pack(">I", ERR_NO_LOCK_HELD)
        link.locked_by = None
        return struct.pack(">I", ERR_NONE)

    def _destroy_link(self, args: bytes) -> bytes:
        (lid,) = struct.unpack(">I", args[:4])
        with self._lock:
            link = self.links.pop(lid, None)
        if link is not None:
            self.diagnose(INFO, "link destroyed: %s" % link.device,
                          "lid=%d" % lid, address=link.address)
        # MEASURED: destroying an already-destroyed link DOES report the
        # invalid-link error, even though write and read on it report a
        # timeout. The asymmetry is the hardware's, not an oversight here.
        return struct.pack(">I", ERR_NONE if link else ERR_INVALID_LINK)

    # -- interrupt channel --------------------------------------------------
    def _create_intr_chan(self, args: bytes) -> bytes:
        """
        Open the reverse-direction channel the client asked for.

        The roles invert here: the gateway becomes the RPC client and calls
        back into a server the application is running. MEASURED: the E5810A
        opens the connection during create_intr_chan and holds it open. A
        client that accepts and immediately closes gets no channel -- that is
        how our own capture first appeared to time out.
        """
        host_addr, host_port, prog, vers, family = struct.unpack(
            ">IIIII", args[:20])
        target = (socket.inet_ntoa(struct.pack(">I", host_addr)), host_port)

        self._destroy_intr_chan()          # only one channel at a time
        try:
            sock = socket.socket()
            sock.settimeout(5.0)
            sock.connect(target)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception as exc:
            self.diagnose(WARN, "create_intr_chan could not call back",
                          "%s:%d unreachable -- %s" % (target[0], target[1], exc))
            return struct.pack(">I", ERR_NOT_ACCESSIBLE)

        self._intr_sock = sock
        self._intr_target = (target, prog, vers, family)
        self.diagnose(INFO, "interrupt channel established",
                      "callback to %s:%d, prog %d v%d, %s"
                      % (target[0], target[1], prog, vers,
                         "TCP" if family == 0 else "UDP"))
        return struct.pack(">I", ERR_NONE)

    def _destroy_intr_chan(self) -> bytes:
        # MEASURED: 0 the first time, then 6 "channel not established".
        if self._intr_sock is None:
            return struct.pack(">I", ERR_CHANNEL_NOT_ESTABLISHED)
        try:
            self._intr_sock.close()
        except Exception:
            pass
        self._intr_sock = None
        self._intr_target = None
        self._srq_handles.clear()
        self.diagnose(INFO, "interrupt channel torn down", "")
        return struct.pack(">I", ERR_NONE)

    def _device_enable_srq(self, args: bytes) -> bytes:
        lid, enable = struct.unpack(">II", args[:8])
        handle, _ = read_xdr_opaque(args, 8)
        link = self._link_for(lid)
        if link is None:
            return struct.pack(">I", ERR_IO_TIMEOUT)
        if enable:
            # The handle is opaque to us and comes back verbatim in the
            # interrupt, which is how the client knows which link fired.
            self._srq_handles[lid] = handle
        else:
            self._srq_handles.pop(lid, None)
        return struct.pack(">I", ERR_NONE)

    def deliver_srq(self, address: int):
        """
        Call device_intr_srq on the client, as the gateway does when an
        instrument asserts SRQ. No reply is expected -- the VXI-11 interrupt
        procedure is a one-way notification.
        """
        if self._intr_sock is None:
            return False
        link = next((lk for lk in self.links.values()
                     if lk.address == address and lk.lid in self._srq_handles),
                    None)
        if link is None:
            return False

        handle = self._srq_handles[link.lid]
        body = struct.pack(">IIIIII", random.randint(1, 0x7FFFFFFF), MSG_CALL,
                           2, INTR_PROG, INTR_VERS, DEVICE_INTR_SRQ)
        body += struct.pack(">IIII", 0, 0, 0, 0)
        body += xdr_opaque(handle)
        try:
            self._intr_sock.sendall(
                struct.pack(">I", 0x80000000 | len(body)) + body)
        except Exception as exc:
            self.diagnose(WARN, "SRQ delivery failed", str(exc), address=address)
            return False
        self.diagnose(INFO, "SRQ delivered", "handle=%r" % handle,
                      address=address)
        return True

    CORE_DISPATCH = {
        CREATE_LINK: "_create_link",
        DEVICE_WRITE: "_device_write",
        DEVICE_READ: "_device_read",
        DEVICE_READSTB: "_device_readstb",
        DEVICE_LOCK: "_device_lock",
        DEVICE_UNLOCK: "_device_unlock",
        DESTROY_LINK: "_destroy_link",
    }

    def _handle_core(self, proc: int, args: bytes):
        handler = self.CORE_DISPATCH.get(proc)
        if handler:
            return SUCCESS, getattr(self, handler)(args)
        if proc == DEVICE_CLEAR:
            return SUCCESS, self._device_generic(args, clear=True)
        # DEVICE_ENABLE_SRQ is deliberately NOT here: it carries a handle that
        # has to be stored, and the generic path would answer 0 while quietly
        # discarding it -- which is exactly what it did until this was caught.
        if proc in (DEVICE_TRIGGER, DEVICE_REMOTE, DEVICE_LOCAL):
            return SUCCESS, self._device_generic(args)
        if proc == CREATE_INTR_CHAN:
            return SUCCESS, self._create_intr_chan(args)
        if proc == DESTROY_INTR_CHAN:
            return SUCCESS, self._destroy_intr_chan()
        if proc == DEVICE_ENABLE_SRQ:
            return SUCCESS, self._device_enable_srq(args)
        if proc == DEVICE_DOCMD:
            # MEASURED: the E5810A answers 8 "operation not supported" to every
            # docmd probed -- Send Command, Bus Status, ATN and Bus Address.
            # This gateway does not expose bus-level operations that way.
            return SUCCESS, struct.pack(">I", ERR_NOT_SUPPORTED) + xdr_opaque(b"")
        if proc == 0:                                    # NULL
            return SUCCESS, b""
        return PROC_UNAVAIL, b""

    # -- portmapper ---------------------------------------------------------
    def _handle_portmap(self, proc: int, args: bytes):
        if proc == PMAPPROC_NULL:
            return SUCCESS, b""
        if proc == PMAPPROC_GETPORT:
            prog, _vers, _prot, _port = struct.unpack(">IIII", args[:16])
            # MEASURED: only the core channel is registered. The abort and
            # interrupt programs return 0 even though create_link advertises
            # an abort port.
            port = self.core_port if prog == CORE_PROG else 0
            return SUCCESS, struct.pack(">I", port)
        if proc == PMAPPROC_DUMP:
            entry = struct.pack(">I", 1) + struct.pack(
                ">IIII", CORE_PROG, CORE_VERS, 6, self.core_port)
            return SUCCESS, entry + struct.pack(">I", 0)
        return PROC_UNAVAIL, b""

    # -- RPC framing --------------------------------------------------------
    def _dispatch(self, payload: bytes) -> bytes:
        if len(payload) < 24:
            return b""
        xid, msg_type, rpcvers, prog, vers, proc = struct.unpack(
            ">IIIIII", payload[:24])
        if msg_type != MSG_CALL or rpcvers != 2:
            return b""

        off = 24
        for _ in range(2):                       # cred and verf
            off += 4
            (length,) = struct.unpack(">I", payload[off:off + 4])
            off += 4 + length + ((-length) % 4)
        args = payload[off:]

        if prog == PORTMAP_PROG:
            accept, result = self._handle_portmap(proc, args)
        elif prog == CORE_PROG:
            accept, result = (self._handle_core(proc, args)
                              if vers == CORE_VERS else (PROG_MISMATCH, b""))
        elif prog == ABORT_PROG:
            # MEASURED, and it is a stub. The E5810A answers program 395184 on
            # the CORE port -- not on the port create_link advertises -- and
            # returns 4 "invalid link identifier" for every lid, valid or
            # garbage. Aborting a genuinely blocked read has no effect: the
            # read still runs its full io_timeout.
            #
            # This gateway does not implement abort. Reproducing that is the
            # point; a working abort here would let a client succeed against
            # the emulator and hang against the hardware.
            accept, result = SUCCESS, struct.pack(">I", ERR_INVALID_LINK)
        elif prog == INTR_PROG:
            accept, result = PROG_UNAVAIL, b""
        else:
            accept, result = PROG_UNAVAIL, b""

        reply = struct.pack(">III", xid, MSG_REPLY, MSG_ACCEPTED)
        reply += struct.pack(">II", 0, 0)        # null verf
        reply += struct.pack(">I", accept) + result
        return reply

    def _serve_stream(self, conn):
        buffer = b""
        try:
            while self._running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    if len(buffer) < 4:
                        break
                    (marker,) = struct.unpack(">I", buffer[:4])
                    length = marker & 0x7FFFFFFF
                    if length > MAX_RPC_RECORD_BYTES:
                        self.diagnose(
                            WARN, 'RPC record exceeded safety limit',
                            'client declared %d bytes; limit is %d; connection '
                            'closed' % (length, MAX_RPC_RECORD_BYTES))
                        return
                    if len(buffer) < 4 + length:
                        break
                    payload, buffer = buffer[4:4 + length], buffer[4 + length:]
                    reply = self._dispatch(payload)
                    if reply:
                        conn.sendall(struct.pack(">I", 0x80000000 | len(reply))
                                     + reply)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _silent_accept_loop(self, sock):
        """
        Accept and hold, never reply. Reproduces the advertised abort port,
        which on the hardware answers nothing at all -- not even PROG_UNAVAIL
        to a well-formed call for a program it does not serve.
        """
        # Bounded: the hardware holds these connections open, but an emulator
        # that accumulates them without limit hands a misbehaving client a way
        # to exhaust our descriptors. Oldest is dropped past the ceiling.
        MAX_HELD = 16
        held = []
        while self._running:
            try:
                conn, _addr = sock.accept()
            except Exception:
                break
            held.append(conn)
            while len(held) > MAX_HELD:
                stale = held.pop(0)
                try:
                    stale.close()
                except Exception:
                    pass
            self.diagnose(
                INFO, "connection to the abort port",
                "accepted and deliberately left unanswered -- MEASURED: the "
                "E5810A does not serve RPC here. device_abort is answered on "
                "the core port, and is a stub that always returns error 4.")
        for conn in held:
            try:
                conn.close()
            except Exception:
                pass

    def _accept_loop(self, sock):
        while self._running:
            try:
                conn, _addr = sock.accept()
            except Exception:
                break
            if not self._client_limiter.admit(conn):
                self.diagnose(
                    WARN, 'client connection refused by safety limit',
                    'already handling %d clients; limit is %d'
                    % (self._client_limiter.active_count,
                       self._client_limiter.limit))
                conn.close()
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            t = threading.Thread(target=self._serve_limited_stream, args=(conn,),
                                 daemon=True)
            try:
                t.start()
            except Exception:
                self._client_limiter.release(conn)
                conn.close()
                continue
            # Drop finished threads rather than retaining every Thread object
            # for the life of the process: a long session with many short
            # client connections would otherwise grow this list without bound.
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)

    def _serve_limited_stream(self, conn):
        try:
            self._serve_stream(conn)
        finally:
            self._client_limiter.release(conn)

    def _udp_loop(self, sock):
        """The portmapper is reachable over UDP as well as TCP."""
        while self._running:
            try:
                data, addr = sock.recvfrom(65536)
            except Exception:
                break
            reply = self._dispatch(data)
            if reply:
                try:
                    sock.sendto(reply, addr)
                except Exception:
                    pass

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True

        for port in (self.portmap_port, self.core_port):
            sock = create_tcp_listener(self.host, port, backlog=64)
            self._sockets.append(sock)
            t = threading.Thread(target=self._accept_loop, args=(sock,),
                                 daemon=True)
            t.start()
            self._threads.append(t)

        # The advertised abort port. MEASURED: the hardware accepts TCP
        # connections here and then never speaks -- not RPC, not anything. A
        # client that follows the specification and calls device_abort on this
        # port hangs, so the emulator must hang it too.
        try:
            abort_sock = create_tcp_listener(self.host, self.abort_port,
                                             backlog=8)
            self._sockets.append(abort_sock)
            t = threading.Thread(target=self._silent_accept_loop,
                                 args=(abort_sock,), daemon=True)
            t.start()
            self._threads.append(t)
        except Exception as exc:
            self.diagnose(WARN, "abort port could not bind",
                          "port %d -- %s. Clients calling device_abort there "
                          "will see a refused connection instead of the "
                          "hardware's silence." % (self.abort_port, exc))

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind((self.host, self.portmap_port))
        self._sockets.append(udp)
        t = threading.Thread(target=self._udp_loop, args=(udp,), daemon=True)
        t.start()
        self._threads.append(t)

        print("[VXI11EmulatorServer] portmapper %s:%d, core channel %s:%d"
              % (self.host, self.portmap_port, self.host, self.core_port))

    def stop(self):
        self._running = False
        for sock in self._sockets:
            try:
                sock.close()
            except Exception:
                pass
        self._sockets.clear()
        self._client_limiter.close_all()
        with self._lock:
            self.links.clear()
        print("[VXI11EmulatorServer] Stopped")
