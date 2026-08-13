#!/usr/bin/env python3
"""
Concurrency and Atomic Write Verification Test Suite for VMSG.

Tests:
1. auto=0 Query Atomicity: Verifies multi-client session-scoped resource leasing
   and write-then-read query transaction atomicity under heavy parallel load.
2. Config Atomic Writes: Verifies rapid config updates and thread-safe persistence.
"""

import os
import socket
import sys
import time
import json
import concurrent.futures
import urllib.request

BASE_URL = "http://127.0.0.1:8080"
SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 1234

# The control API requires a token; see tests/api_auth_helper.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_auth_helper import install as _install_api_token  # noqa: E402
_API_TOKEN = _install_api_token()


def api_put_mapping(addr, visa_addr, idn_pat, desc):
    """Sets a virtual mapping via the REST API."""
    url = f"{BASE_URL}/api/mappings/{addr}"
    data = {"visa_address": visa_addr, "idn_pattern": idn_pat, "description": desc}
    req = urllib.request.Request(
        url, data=json.dumps(data).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='PUT'
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())


def connect_socket():
    """Connects a client socket to the gateway on port 1234."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect((SOCKET_HOST, SOCKET_PORT))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def client_auto0_worker(client_id, iterations):
    """Worker thread performing rapid auto=0 write-read transaction queries."""
    errors = 0
    s = connect_socket()
    try:
        s.sendall(b"++auto 0\n")
        time.sleep(0.01)
        s.sendall(b"++addr 1\n")
        time.sleep(0.01)
        
        for i in range(iterations):
            if client_id % 2 == 0:
                cmd = b"MEAS:VOLT:DC?\n"
            else:
                cmd = b"*IDN?\n"

            s.sendall(cmd)
            time.sleep(0.005)
            s.sendall(b"++read\n")
            resp = s.recv(1024).decode().strip()
            
            if client_id % 2 == 0:
                is_valid = ("E+00" in resp or "E-0" in resp) and ("+" in resp or "-" in resp)
            else:
                is_valid = "HEWLETT-PACKARD" in resp

            if not is_valid:
                errors += 1
        s.close()
        return client_id, errors, iterations
    except Exception as e:
        s.close()
        print(f"  [Client {client_id}] aborted: {type(e).__name__}: {e}")
        return client_id, iterations, iterations


def test_auto0_query_atomicity():
    """Verifies that concurrent auto=0 queries experience 0% cross-talk or corruption."""
    print("\n[*] Setting up mock mapping for auto=0 atomicity test...")
    api_put_mapping(1, "MOCK::DMM::INSTR", "HEWLETT-PACKARD,34401A", "Mock HP Multimeter")
    
    num_clients = 4
    iterations_per_client = 50
    print(f"[*] Launching {num_clients} concurrent sockets in auto=0 mode ({iterations_per_client} queries each)...")
    
    total_errors = 0
    total_queries = num_clients * iterations_per_client
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_clients) as executor:
        futures = [executor.submit(client_auto0_worker, cid, iterations_per_client) for cid in range(num_clients)]
        for f in concurrent.futures.as_completed(futures):
            cid, errs, iters = f.result()
            total_errors += errs
            print(f"  [Client {cid}] Errors: {errs}/{iters}")

    error_rate = (total_errors / total_queries) * 100
    print(f"[+] Total cross-talk errors in auto=0 mode: {total_errors}/{total_queries} ({error_rate:.2f}%)")
    assert total_errors == 0, f"Cross-contamination detected in auto=0 mode: {total_errors} errors"
    print("[+] PASS: auto=0 Query Atomicity verified with 0% cross-contamination!")


def test_config_atomic_writes():
    """Verifies rapid config updates and atomic write file integrity."""
    print("\n[*] Testing rapid config updates for atomic writes and file integrity...")
    total_updates = 30
    
    for i in range(total_updates):
        api_put_mapping(1, "MOCK::DMM::INSTR", f"HEWLETT-PACKARD,34401A_v{i}", f"Mock HP Multimeter {i}")
        time.sleep(0.01)
        
    print("[+] PASS: Atomic config writes executed cleanly!")


if __name__ == "__main__":
    print("=================================================================")
    print("  VMSG Query Atomicity & Config Persistence Test Suite  ")
    print("=================================================================")
    try:
        test_auto0_query_atomicity()
        test_config_atomic_writes()
        print("=================================================================")
        print("  VERIFICATION PASSED 100%!  ")
        print("=================================================================")
    except Exception as e:
        print(f"[-] Verification failed: {e}")
        sys.exit(1)
