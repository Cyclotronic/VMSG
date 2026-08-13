"""
LXI Raw SCPI Socket and mDNS LXI Discovery Responder (`vxi11_lxi_emulator.py`)

Emulates Keysight E5810A / LXI-compliant instrument discovery & raw socket communication ports:
  1. Port 5025: LXI SCPI Raw Socket Server (direct SCPI streams without ++ syntax).
  2. UDP Port 5353: LXI mDNS UDP Discovery Responder.
"""

import socket
import threading
from typing import Optional
from .vxi11_bridge import VmsgInstrumentRegistry as InstrumentRegistry
from .diagnostics import WARN, DiagnosticEmitter
from .mdns import GatewayDiscoveryResponder
from .netutil import (
    DEFAULT_MAX_CLIENT_HANDLERS, MAX_PENDING_TEXT_CHARS, ClientLimiter,
    create_tcp_listener,
)

LXIDiscoveryResponder = GatewayDiscoveryResponder


class LXIRawSocketServer(DiagnosticEmitter):
    """LXI SCPI Raw Socket Server listening on TCP port 5025 (standard LXI port)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5025, registry: Optional[InstrumentRegistry] = None):
        self.host = host
        self.DIAGNOSTIC_SOURCE = 'lxi-raw'
        self.port = port
        # VMSG port: see vxi11_emulator.py - the registry is mandatory here.
        if registry is None:
            raise ValueError(
                "LXIRawSocketServer requires a registry "
                "(vmsg_core.vxi11_bridge.VmsgInstrumentRegistry)")
        self.registry = registry

        self._server_socket: Optional[socket.socket] = None
        self._is_running = False
        self._server_thread: Optional[threading.Thread] = None
        self._client_limiter = ClientLimiter(DEFAULT_MAX_CLIENT_HANDLERS)
        # Appended and removed by per-client threads, and read by the GUI for
        # its client-count tile. PrologixEmulatorServer guards its equivalent
        # with a lock; this one was missed.
        self.active_clients = []
        self._clients_lock = threading.Lock()
        self.default_address = 6  # Default instrument slot for direct SCPI socket

    def start(self):
        if self._is_running:
            return

        self._server_socket = create_tcp_listener(self.host, self.port, backlog=32)
        self._is_running = True

        self._server_thread = threading.Thread(target=self._accept_loop, daemon=True, name="LXIRawSocketServer")
        self._server_thread.start()
        print(f"[LXIRawSocketServer] Listening on {self.host}:{self.port} (Raw SCPI Port)")

    def stop(self):
        self._is_running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None
        self._client_limiter.close_all()

    def _accept_loop(self):
        while self._is_running and self._server_socket:
            try:
                client_sock, addr = self._server_socket.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                if not self._client_limiter.admit(client_sock):
                    self.diagnose(
                        WARN, 'client connection refused by safety limit',
                        'already handling %d clients; limit is %d'
                        % (self._client_limiter.active_count,
                           self._client_limiter.limit))
                    client_sock.close()
                    continue

                t = threading.Thread(
                    target=self._handle_limited_client,
                    args=(client_sock, f"{addr[0]}:{addr[1]}"),
                    daemon=True,
                )
                try:
                    t.start()
                except Exception:
                    self._client_limiter.release(client_sock)
                    client_sock.close()
                    raise
            except Exception:
                if not self._is_running:
                    break

    def _handle_limited_client(self, client_sock: socket.socket, client_id: str):
        try:
            self._handle_client(client_sock, client_id)
        finally:
            self._client_limiter.release(client_sock)

    def _handle_client(self, client_sock: socket.socket, client_id: str):
        with self._clients_lock:
            self.active_clients.append(client_id)
        buffer = ""
        try:
            while self._is_running:
                data = client_sock.recv(4096)
                if not data:
                    break

                buffer += data.decode("utf-8", errors="replace")
                while "\n" in buffer or "\r" in buffer:
                    delimiter_idx = min(
                        [idx for idx in (buffer.find("\n"), buffer.find("\r")) if idx != -1]
                    )
                    if delimiter_idx > MAX_PENDING_TEXT_CHARS:
                        self.diagnose(
                            WARN, 'client command exceeded safety limit',
                            'more than %d characters before a terminator; '
                            'connection closed' % MAX_PENDING_TEXT_CHARS)
                        return
                    line = buffer[:delimiter_idx].strip()
                    buffer = buffer[delimiter_idx + 1 :]

                    if not line:
                        continue

                    # Determine target GPIB slot: default_address if mapped, or first available slot in registry
                    target_slot = self.default_address
                    if target_slot not in self.registry.devices and self.registry.devices:
                        target_slot = sorted(self.registry.devices.keys())[0]

                    # Directly route SCPI command to virtual instrument at target slot
                    resp = self.registry.process_command(target_slot, line)
                    if resp is not None:
                        out_bytes = (resp + "\r\n").encode("utf-8")
                        client_sock.sendall(out_bytes)

                if len(buffer) > MAX_PENDING_TEXT_CHARS:
                    self.diagnose(
                        WARN, 'client command exceeded safety limit',
                        'more than %d characters without a terminator; '
                        'connection closed' % MAX_PENDING_TEXT_CHARS)
                    break

        except Exception:
            pass
        finally:
            with self._clients_lock:
                if client_id in self.active_clients:
                    self.active_clients.remove(client_id)
            try:
                client_sock.close()
            except Exception:
                pass
