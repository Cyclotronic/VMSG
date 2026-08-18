#!/usr/bin/env python3
"""
Measure what VMSG itself costs, by asking the same instrument the same questions
twice: once through the gateway, once directly through VISA.

tools/benchmark.py measures the gateway path in isolation. It cannot tell you how
much of a slow reading is VMSG and how much is the instrument, because it has
nothing to compare against. This does the A/B.

Requires pyvisa and a VISA runtime, and instruments reachable BOTH ways - i.e.
locally attached, and mapped into VMSG. On a remote gateway there is no direct
path to compare with and this tool does not apply.

    python tools/vmsg_overhead.py --map 3=GPIB0::8::INSTR
    python tools/vmsg_overhead.py --map 1=USB0::0x0A69::0x0880::630041501550::INSTR \
                                  --map 3=GPIB0::8::INSTR --interleave

Two regimes are measured because they can differ:

  steady-state  one instrument, repeated queries. No address switching.
  interleaved   round-robin across instruments, one query each. Forces VMSG to
                re-address between every query, which is what TestController does
                when several devices log together.

MEASUREMENT NOTE, learned the hard way: do not put settling sleeps or
drain-to-silence loops in the timed path. A drain that waits 100 ms for silence
that never arrives bills 100 ms to the gateway on every query, and the result is
a made-up overhead figure that looks convincing. With "++auto 0" set once at the
start, "++addr N" followed immediately by the query is correct and needs no
padding. Replies are parsed as numbers here so that a desynchronised read is
reported as a bad reply instead of being silently counted as a fast one.
"""

import argparse
import socket
import statistics
import sys
import time

DEFAULT_QUERY = "MEAS:VOLT?"


class Gateway:
    """Prologix-style access to VMSG. One socket, no padding in the timed path."""

    def __init__(self, host, port, timeout):
        self.s = socket.socket()
        self.s.settimeout(timeout)
        self.s.connect((host, port))
        self._w("++auto 0")

    def _w(self, cmd):
        self.s.sendall((cmd + "\n").encode())

    def ask(self, addr, query):
        self._w(f"++addr {addr}")
        self._w(query)
        self._w("++read eoi")
        data = b""
        while b"\n" not in data:
            chunk = self.s.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode(errors="replace").strip()

    def close(self):
        self.s.close()


def numeric(reply):
    try:
        float(reply.split(",")[0])
        return True
    except (ValueError, IndexError, AttributeError):
        return False


def time_calls(fn, reps):
    """Median/min/max wall time in ms, plus a count of non-numeric replies."""
    fn()  # warm up, discarded
    times, bad = [], 0
    for _ in range(reps):
        t0 = time.perf_counter()
        replies = fn()
        times.append((time.perf_counter() - t0) * 1000)
        bad += sum(0 if numeric(r) else 1 for r in replies)
    return statistics.median(times), min(times), max(times), bad


def report(label, gw_stats, visa_stats, per_cycle):
    gm, _, _, gbad = gw_stats
    vm, _, _, vbad = visa_stats
    delta = gm - vm
    mult = gm / vm if vm else float("nan")
    print(f"  {label:<28}{vm:>11.1f}{gm:>11.1f}{delta:>+11.1f}{mult:>9.2f}x")
    if gbad or vbad:
        print(f"  {'':28}  !! bad replies - gateway {gbad}, direct {vbad}")
    return delta / per_cycle if per_cycle else 0.0


def main():
    p = argparse.ArgumentParser(
        description="Compare VMSG against direct VISA on the same instruments.")
    p.add_argument("--map", action="append", metavar="ADDR=RESOURCE", required=True,
                   help="VMSG address mapped to its VISA resource. Repeatable. "
                        "VMSG addresses are virtual and need not match the "
                        "instrument's real GPIB address.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1234)
    p.add_argument("--query", default=DEFAULT_QUERY)
    p.add_argument("--reps", type=int, default=20)
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--interleave", action="store_true",
                   help="Also run the round-robin regime (needs 2+ mappings).")
    args = p.parse_args()

    try:
        import pyvisa
    except ImportError:
        sys.exit("pyvisa is required: pip install pyvisa (plus a VISA runtime)")

    pairs = []
    for m in args.map:
        if "=" not in m:
            sys.exit(f"--map needs ADDR=RESOURCE, got {m!r}")
        a, res = m.split("=", 1)
        pairs.append((int(a), res))

    rm = pyvisa.ResourceManager()
    gw = Gateway(args.host, args.port, args.timeout)
    devs = {}
    for addr, res in pairs:
        d = rm.open_resource(res)
        d.timeout = int(args.timeout * 1000)
        devs[addr] = d

    print(f"query: {args.query}   reps: {args.reps}\n")
    print(f"  {'regime':<28}{'direct ms':>11}{'VMSG ms':>11}{'delta':>11}{'mult':>10}")
    print("  " + "-" * 71)

    overheads = []
    for addr, res in pairs:
        d = devs[addr]
        g = time_calls(lambda: [gw.ask(addr, args.query)], args.reps)
        v = time_calls(lambda: [d.query(args.query)], args.reps)
        overheads.append(report(f"steady  addr {addr}", g, v, 1))

    if args.interleave:
        if len(pairs) < 2:
            print("\n  --interleave needs 2+ mappings, skipped")
        else:
            addrs = [a for a, _ in pairs]
            g = time_calls(
                lambda: [gw.ask(a, args.query) for a in addrs], args.reps)
            v = time_calls(
                lambda: [devs[a].query(args.query) for a in addrs], args.reps)
            overheads.append(
                report(f"interleaved x{len(addrs)}", g, v, len(addrs)))

    print()
    worst = max(overheads) if overheads else 0.0
    print(f"  worst-case VMSG overhead: {worst:+.1f} ms per query")
    if worst < 5:
        print("  -> negligible. A slow reading is the instrument, not the gateway.")
    else:
        print("  -> material. Worth profiling the gateway path.")

    for d in devs.values():
        d.close()
    gw.close()


if __name__ == "__main__":
    main()
