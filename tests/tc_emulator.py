#!/usr/bin/env python3
"""
TestController protocol emulator for VMSG.

Drives VMSG exactly the way TestController's PrologixEthernet
(extends SharedInterfacePrologixUSB) does, per TESTCONTROLLER_NOTES.md:
  - one TCP socket per controller ID (A, B, C...)
  - init sequence: ++auto 0, ++mode 1
  - addressing:    ++addr <slot>
  - query:         write "<scpi>", then "++read eoi"   (auto=0)
  - all device threads spawn simultaneously at startup

Config is parsed from the files VMSG actually exported.
"""
import socket, struct, time, re

HOST, PORT = "127.0.0.1", 1234


def parse_configs(gpib_path, load_path):
    controllers = {}
    for line in open(gpib_path):
        m = re.match(r"PrologixEthernet\|id:(\w+)\|address:([^|]+)\|", line.strip())
        if m:
            controllers[m.group(1)] = m.group(2)
    devices = []
    for line in open(load_path):
        m = re.match(r"Device:([^|]+)\|PortType:GPIB\|Address:(\w+):(\d+)\|.*Enabled:1", line.strip())
        if m:
            devices.append({"driver": m.group(1), "ctrl": m.group(2), "slot": int(m.group(3))})
    return controllers, devices


class TCDevice:
    """One TestController device thread: its own socket, its own controller ID."""

    def __init__(self, driver, ctrl, slot, host):
        self.driver, self.ctrl, self.slot, self.host = driver, ctrl, slot, host
        self.sock = None
        self.idn = None
        self.errors = []
        self.reconnects = 0
        self.polls = 0
        self.wrong = 0
        self.empty = 0
        self.latencies = []

    def connect(self):
        self.sock = socket.create_connection((self.host, PORT), timeout=6)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # TestController controller init
        self._send("++auto 0")
        self._send("++mode 1")
        self._send(f"++addr {self.slot}")

    def abrupt_disconnect(self):
        """RST instead of FIN - simulates a cable pull / app kill."""
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                 struct.pack("ii", 1, 0))
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def clean_disconnect(self):
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _send(self, cmd):
        self.sock.sendall((cmd + "\n").encode())

    def query(self, scpi, tag=None):
        """auto=0 query: write, then ++read eoi."""
        t0 = time.perf_counter()
        self._send(scpi)
        self._send("++read eoi")
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            self.errors.append(f"timeout on {scpi}")
            return None
        self.latencies.append((time.perf_counter() - t0) * 1000)
        resp = data.decode(errors="replace").strip()
        self.polls += 1
        if not resp:
            self.empty += 1
        elif tag and tag not in resp:
            self.wrong += 1
            if len(self.errors) < 5:
                self.errors.append(f"expected {tag!r} got {resp[:60]!r}")
        return resp

    def identify(self):
        self.idn = self.query("*IDN?")
        return self.idn
