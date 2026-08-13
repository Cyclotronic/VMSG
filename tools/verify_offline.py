#!/usr/bin/env python3
"""
Offline Prologix protocol fidelity check - no VISA hardware required.

Pattern borrowed from BenchForge's tools/verify_offline.py. It asserts the
command surface and the concurrency properties that a single-client test cannot
catch, using only MOCK:: instruments so it is safe in CI and as a pre-commit
gate.

Deliberately does NOT use the running gateway: it starts its own server on a
scratch port with a temporary config, so it can never disturb a live bench.

    python tools/verify_offline.py

Exit code 0 when everything matches, 1 otherwise.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vmsg_core.config_manager import ConfigManager           # noqa: E402
from vmsg_core.prologix_server import PrologixSocketServer   # noqa: E402
from vmsg_core.visa_manager import VisaManager               # noqa: E402

PORT = 14555
failures = []


def fail(section, detail):
    failures.append(f"{section}: {detail}")
    print(f"  [FAIL] {section}: {detail}")


def ok(section, detail=""):
    print(f"  [ok]   {section}" + (f": {detail}" if detail else ""))


# --------------------------------------------------------------------------
# Scratch gateway
# --------------------------------------------------------------------------

def build_config():
    """Temporary config with three mock instruments. Never touches mappings.json."""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({
        "settings": {
            "addr": 1, "auto": 0, "mode": 1, "eos": 0, "eoi": 1,
            "read_tmo_ms": 3000, "eot_enable": 0, "eot_char": 4,
            "unmapped_behavior": "message",
            "log_level": "ERROR", "enable_stdout": False,
        },
        "mappings": {
            "1": {"visa_address": "MOCK::DMM::INSTR", "idn_pattern": "", "description": "mock dmm"},
            "2": {"visa_address": "MOCK::SCOPE::INSTR", "idn_pattern": "", "description": "mock scope"},
            "3": {"visa_address": "MOCK::GENERIC::HP_53131A::INSTR", "idn_pattern": "", "description": "mock counter"},
        },
    }, tmp)
    tmp.close()
    return ConfigManager(filepath=tmp.name), tmp.name


class Gateway:
    def __init__(self):
        self.loop = None
        self.server = None
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        cfg, self.cfg_path = build_config()
        visa = VisaManager()
        self.server = PrologixSocketServer("127.0.0.1", PORT, cfg, visa)

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.create_task(self.server.start())
            self.loop.call_later(0.4, self._ready.set)
            self.loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError("gateway did not start")
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"nothing listening on {PORT}")

    def stop(self):
        if self.loop:
            # Close the listeners and cancel outstanding tasks before stopping
            # the loop, otherwise asyncio prints "Task was destroyed but it is
            # pending!" and CI output looks like a failure when it is not.
            async def shutdown():
                try:
                    await self.server.stop()
                except Exception:
                    pass
                current = asyncio.current_task()
                pending = [t for t in asyncio.all_tasks() if t is not current]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            fut = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        try:
            os.unlink(self.cfg_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Client helper
# --------------------------------------------------------------------------

class Client:
    def __init__(self, timeout=5.0):
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def send(self, *lines):
        for ln in lines:
            self.s.sendall((ln + "\n").encode())

    def line(self, timeout=5.0):
        self.s.settimeout(timeout)
        while b"\r\n" not in self.buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf.split(b"\r\n", 1)
        return out.decode(errors="replace")

    def query(self, cmd, addr=None):
        if addr is not None:
            self.send(f"++addr {addr}")
        self.send(cmd, "++read eoi")
        return self.line()

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# 1. Prologix command surface
# --------------------------------------------------------------------------

def check_command_surface():
    print("\n1. Prologix command surface")
    c = Client()
    try:
        matrix = [
            ("++ver", lambda v: "Prologix" in v and "GPIB" in v.upper()),
            ("++addr", lambda v: v.strip().isdigit()),
            ("++auto", lambda v: v.strip() in ("0", "1")),
            ("++mode", lambda v: v.strip() in ("0", "1")),
            ("++eos", lambda v: v.strip() in ("0", "1", "2", "3")),
            ("++eoi", lambda v: v.strip() in ("0", "1")),
            ("++read_tmo_ms", lambda v: v.strip().isdigit()),
            ("++eot_enable", lambda v: v.strip() in ("0", "1")),
            ("++eot_char", lambda v: v.strip().isdigit()),
        ]
        for cmd, predicate in matrix:
            c.send(cmd)
            try:
                val = c.line(timeout=3)
            except Exception as e:
                fail(cmd, f"no reply ({type(e).__name__})")
                continue
            if predicate(val):
                ok(cmd, repr(val))
            else:
                fail(cmd, f"unexpected reply {val!r}")

        # Setters must round-trip.
        for cmd, value in (("++addr", "7"), ("++read_tmo_ms", "1500"), ("++eos", "2")):
            c.send(f"{cmd} {value}")
            c.send(cmd)
            got = c.line(timeout=3).strip()
            if got == value:
                ok(f"{cmd} round-trip", value)
            else:
                fail(f"{cmd} round-trip", f"set {value}, read back {got!r}")
    finally:
        c.close()


# --------------------------------------------------------------------------
# 2. Routing and addressing
# --------------------------------------------------------------------------

def check_routing():
    print("\n2. Addressing routes to the right instrument")
    c = Client()
    try:
        expect = {1: "HEWLETT-PACKARD", 2: "TEKTRONIX", 3: "HP_53131A"}
        for slot, needle in expect.items():
            reply = c.query("*IDN?", addr=slot)
            if needle in reply:
                ok(f"slot {slot}", reply[:44])
            else:
                fail(f"slot {slot}", f"expected {needle!r}, got {reply!r}")

        # Unmapped slot must not answer with another instrument's data.
        reply = c.query("*IDN?", addr=29)
        if reply.strip() == "":
            ok("unmapped slot 29", "empty response")
        else:
            fail("unmapped slot 29", f"leaked {reply!r}")
    finally:
        c.close()


# --------------------------------------------------------------------------
# 3. Concurrency - the property a single-client test cannot see
# --------------------------------------------------------------------------

def check_concurrency():
    print("\n3. Concurrent sessions stay isolated")
    results, errors = {}, []

    def worker(slot, needle, iterations=25):
        try:
            c = Client()
            bad = 0
            for _ in range(iterations):
                reply = c.query("*IDN?", addr=slot)
                if needle not in reply:
                    bad += 1
            results[slot] = bad
            c.close()
        except Exception as e:
            errors.append(f"slot {slot}: {type(e).__name__}: {e}")

    targets = [(1, "HEWLETT-PACKARD"), (2, "TEKTRONIX"), (3, "HP_53131A")]
    threads = [threading.Thread(target=worker, args=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    if errors:
        for e in errors:
            fail("concurrency", e)
        return
    total_bad = sum(results.values())
    if total_bad == 0:
        ok("cross-talk", f"0 mismatches across {sum(25 for _ in targets)} interleaved queries")
    else:
        fail("cross-talk", f"{total_bad} responses went to the wrong session")


# --------------------------------------------------------------------------
# 4. Resource limits
# --------------------------------------------------------------------------

def check_limits():
    print("\n4. Resource limits are enforced")
    from vmsg_core.netutil import DEFAULT_MAX_CLIENT_HANDLERS

    socks = []
    try:
        for _ in range(DEFAULT_MAX_CLIENT_HANDLERS + 6):
            try:
                s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
                s.settimeout(1.0)
                socks.append(s)
            except OSError:
                break
        alive = 0
        for s in socks:
            try:
                s.sendall(b"++ver\n")
                if s.recv(200):
                    alive += 1
            except OSError:
                pass
        if alive <= DEFAULT_MAX_CLIENT_HANDLERS:
            ok("client ceiling", f"{alive} served, limit {DEFAULT_MAX_CLIENT_HANDLERS}")
        else:
            fail("client ceiling", f"{alive} served, above limit {DEFAULT_MAX_CLIENT_HANDLERS}")
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass

    # An overlong line with no terminator must close the connection, not
    # silently truncate and desynchronise the command stream.
    from vmsg_core.netutil import MAX_PENDING_TEXT_CHARS
    c = Client()
    try:
        c.s.sendall(b"A" * (MAX_PENDING_TEXT_CHARS + 4096))
        c.s.settimeout(5)
        try:
            closed = (c.s.recv(100) == b"")
        except (ConnectionResetError, socket.timeout, OSError):
            closed = True
        if closed:
            ok("oversize line", "connection closed rather than truncated")
        else:
            fail("oversize line", "connection stayed open after exceeding the cap")
    finally:
        c.close()


# --------------------------------------------------------------------------

def main():
    print("VMSG offline protocol fidelity")
    print("=" * 60)
    gw = Gateway()
    try:
        gw.start()
        print(f"scratch gateway listening on 127.0.0.1:{PORT} (mock instruments only)")
        check_command_surface()
        check_routing()
        check_concurrency()
        check_limits()
    except Exception as e:
        fail("harness", f"{type(e).__name__}: {e}")
    finally:
        gw.stop()

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED - {len(failures)} check(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All offline fidelity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
