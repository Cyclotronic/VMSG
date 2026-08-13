"""
Connectivity & Performance Benchmark Suite (`performance_tester.py`)

Provides automated latency, throughput, multi-thread concurrency, and address switching
stress tests tailored for TestController adapter validation.
"""

import concurrent.futures
import socket
import time
from typing import List, Optional, Callable


class TestResult:
    """Encapsulates test run execution results and statistics."""

    def __init__(self, test_name: str):
        self.test_name = test_name
        self.total_queries = 0
        self.successful_queries = 0
        self.failed_queries = 0
        self.total_duration_sec = 0.0
        self.queries_per_second = 0.0
        self.latencies_ms: List[float] = []
        self.min_latency_ms = 0.0
        self.max_latency_ms = 0.0
        self.avg_latency_ms = 0.0
        self.p95_latency_ms = 0.0
        self.passed = False
        self.details: List[str] = []

    def compute_stats(self):
        """Computes statistical metrics from collected query latencies."""
        if self.latencies_ms:
            self.min_latency_ms = round(min(self.latencies_ms), 2)
            self.max_latency_ms = round(max(self.latencies_ms), 2)
            self.avg_latency_ms = round(sum(self.latencies_ms) / len(self.latencies_ms), 2)
            sorted_lat = sorted(self.latencies_ms)
            p95_idx = int(0.95 * len(sorted_lat))
            self.p95_latency_ms = round(sorted_lat[min(p95_idx, len(sorted_lat) - 1)], 2)

        if self.total_duration_sec > 0:
            self.queries_per_second = round(self.successful_queries / self.total_duration_sec, 2)

        self.passed = self.failed_queries == 0 and self.successful_queries > 0


class PerformanceTester:
    """Executes network benchmarks against Prologix Ethernet emulator or hardware adapter."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1234):
        self.host = host
        self.port = port

    @staticmethod
    def _close(sock):
        """Close if it exists, never raise. For use in finally: blocks."""
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def _connect_socket(self, timeout: float = 3.0) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def run_latency_throughput_test(
        self,
        num_queries: int = 100,
        address: int = 1,
        command: str = "*IDN?",
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> TestResult:
        """Runs single-connection burst latency and QPS throughput test."""
        res = TestResult("SCPI Query Latency & Throughput Test")

        sock = None
        try:
            sock = self._connect_socket()
            sock.sendall(b"++auto 1\n")
            sock.sendall(f"++addr {address}\n".encode())
            time.sleep(0.01)

            t_start_total = time.perf_counter()

            for i in range(num_queries):
                t0 = time.perf_counter()
                cmd_bytes = f"{command}\n".encode()
                sock.sendall(cmd_bytes)

                resp = sock.recv(1024)
                t1 = time.perf_counter()

                latency = (t1 - t0) * 1000.0
                res.latencies_ms.append(latency)

                if resp and len(resp.strip()) > 0:
                    res.successful_queries += 1
                else:
                    res.failed_queries += 1

                res.total_queries += 1

                if progress_cb and i % 10 == 0:
                    progress_cb(i + 1, num_queries)

            res.total_duration_sec = time.perf_counter() - t_start_total

        except Exception as e:
            res.details.append(f"Connection error: {e}")
            res.failed_queries += (num_queries - res.total_queries)
        finally:
            # In a finally: a timeout mid-burst used to skip the close, and the
            # emulator serves a single client -- a leaked socket there blocks
            # the next test rather than merely wasting a descriptor.
            self._close(sock)

        res.compute_stats()
        res.details.append(f"QPS: {res.queries_per_second} queries/sec | Avg Latency: {res.avg_latency_ms} ms")
        return res

    def run_concurrency_stress_test(
        self,
        num_threads: int = 5,
        queries_per_thread: int = 20,
        device_addresses: Optional[List[int]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> TestResult:
        """
        Simulates N concurrent TestController device threads connecting to Prologix simultaneously.
        Verifies thread isolation and absence of thread cross-talk.
        """
        res = TestResult(f"Multi-Thread Concurrency Test ({num_threads} Threads)")
        if not device_addresses:
            device_addresses = [1, 2, 4, 5, 7][:num_threads]

        def worker(thread_idx: int, addr: int):
            worker_latencies = []
            successes = 0
            failures = 0

            sock = None
            try:
                sock = self._connect_socket()
                sock.sendall(b"++auto 1\n")
                sock.sendall(f"++addr {addr}\n".encode())
                time.sleep(0.01)

                for q in range(queries_per_thread):
                    t0 = time.perf_counter()
                    sock.sendall(b"*IDN?\n")
                    resp = sock.recv(1024)
                    t1 = time.perf_counter()

                    lat = (t1 - t0) * 1000.0
                    worker_latencies.append(lat)

                    if resp and len(resp.strip()) > 0:
                        successes += 1
                    else:
                        failures += 1

            except Exception as e:
                failures += (queries_per_thread - (successes + failures))
                res.details.append(f"Worker {thread_idx} exception: {e}")
            finally:
                self._close(sock)

            return worker_latencies, successes, failures

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(worker, i, device_addresses[i % len(device_addresses)])
                for i in range(num_threads)
            ]

            done_count = 0
            for fut in concurrent.futures.as_completed(futures):
                w_lat, w_succ, w_fail = fut.result()
                res.latencies_ms.extend(w_lat)
                res.successful_queries += w_succ
                res.failed_queries += w_fail
                res.total_queries += (w_succ + w_fail)

                done_count += 1
                if progress_cb:
                    progress_cb(done_count, num_threads)

        res.total_duration_sec = time.perf_counter() - t_start
        res.compute_stats()
        res.details.append(
            f"Simulated {num_threads} device threads | Total Queries: {res.total_queries} | QPS: {res.queries_per_second}"
        )
        return res

    def run_address_switching_test(
        self,
        switch_count: int = 50,
        addresses: Optional[List[int]] = None,
    ) -> TestResult:
        """
        Tests address switching speed and zero-crosstalk accuracy by rapidly switching
        ++addr X commands on a single connection.
        """
        res = TestResult("Address Switching Stress Test")
        if not addresses:
            addresses = [1, 2, 4]

        sock = None
        try:
            sock = self._connect_socket()
            sock.sendall(b"++auto 1\n")

            t_start = time.perf_counter()

            for i in range(switch_count):
                target_addr = addresses[i % len(addresses)]

                t0 = time.perf_counter()
                sock.sendall(f"++addr {target_addr}\n".encode())
                sock.sendall(b"*IDN?\n")

                resp = sock.recv(1024)
                t1 = time.perf_counter()

                res.latencies_ms.append((t1 - t0) * 1000.0)

                if resp and len(resp.strip()) > 0:
                    res.successful_queries += 1
                else:
                    res.failed_queries += 1

                res.total_queries += 1

            res.total_duration_sec = time.perf_counter() - t_start

        except Exception as e:
            res.details.append(f"Switching test error: {e}")
        finally:
            self._close(sock)

        res.compute_stats()
        res.details.append(
            f"Switched address {switch_count} times | Avg switch+query time: {res.avg_latency_ms} ms"
        )
        return res
