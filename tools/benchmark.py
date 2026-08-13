#!/usr/bin/env python3
"""
Benchmark a running VMSG gateway (or any Prologix Ethernet endpoint).

Wraps tools/performance_tester.py, ported from BenchForge, so VMSG has a
repeatable performance measurement instead of ad-hoc scripts written per
investigation. Numbers from different sessions are then comparable.

Point it at a mock slot to measure *gateway* overhead, or at a real instrument
to measure the whole path:

    python tools/benchmark.py                      # slot 1, 100 queries
    python tools/benchmark.py --address 4 --queries 200
    python tools/benchmark.py --test all --threads 5
    python tools/benchmark.py --host 192.168.1.50 --port 1234

Exit code is 1 if any selected test reports a failed query.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from performance_tester import PerformanceTester  # noqa: E402


def show(result):
    print(f"\n--- {result.test_name} ---")
    print(f"  queries      : {result.successful_queries} ok / "
          f"{result.failed_queries} failed  (of {result.total_queries})")
    print(f"  duration     : {result.total_duration_sec:.2f} s")
    print(f"  throughput   : {result.queries_per_second} queries/sec")
    if result.latencies_ms:
        print(f"  latency (ms) : min {result.min_latency_ms}  "
              f"avg {result.avg_latency_ms}  p95 {result.p95_latency_ms}  "
              f"max {result.max_latency_ms}")
    for line in result.details:
        print(f"  {line}")
    print(f"  verdict      : {'PASS' if result.passed else 'FAIL'}")
    return result.passed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1234)
    ap.add_argument("--address", type=int, default=1,
                    help="virtual GPIB slot to query (default 1)")
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--threads", type=int, default=4,
                    help="concurrent sockets for the concurrency test")
    ap.add_argument("--command", default="*IDN?")
    ap.add_argument("--test", default="latency",
                    choices=["latency", "concurrency", "switching", "all"])
    ap.add_argument("--addresses", default="",
                    help="comma-separated slots for the concurrency and "
                         "switching tests, e.g. 1,2,3 (must all be mapped, or "
                         "they will read as failures)")
    args = ap.parse_args()

    slots = None
    if args.addresses.strip():
        slots = [int(x) for x in args.addresses.split(",") if x.strip()]

    t = PerformanceTester(host=args.host, port=args.port)
    print(f"VMSG benchmark -> {args.host}:{args.port}  slot {args.address}  "
          f"command {args.command!r}")

    passed = True
    if args.test in ("latency", "all"):
        passed &= show(t.run_latency_throughput_test(
            num_queries=args.queries, address=args.address, command=args.command))
    if args.test in ("concurrency", "all"):
        passed &= show(t.run_concurrency_stress_test(
            num_threads=args.threads,
            queries_per_thread=max(1, args.queries // max(1, args.threads)),
            device_addresses=slots))
    if args.test in ("switching", "all"):
        passed &= show(t.run_address_switching_test(
            switch_count=args.queries, addresses=slots))

    print("\n" + ("All selected tests passed." if passed else "One or more tests FAILED."))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
