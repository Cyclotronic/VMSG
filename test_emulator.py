import socket
import time
import urllib.request
import json
import concurrent.futures

BASE_URL = "http://127.0.0.1:8080"
SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 1234

def api_put_mapping(addr, visa_addr, idn_pat, desc):
    """Sets a virtual mapping via the FastAPI HTTP REST API."""
    url = f"{BASE_URL}/api/mappings/{addr}"
    data = {
        "visa_address": visa_addr,
        "idn_pattern": idn_pat,
        "description": desc
    }
    req = urllib.request.Request(
        url, 
        data=json.dumps(data).encode('utf-8'), 
        headers={'Content-Type': 'application/json'},
        method='PUT'
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"[-] API Error: {e}")
        return None

def api_delete_mapping(addr):
    """Deletes a virtual mapping via the FastAPI HTTP REST API."""
    url = f"{BASE_URL}/api/mappings/{addr}"
    req = urllib.request.Request(url, method='DELETE')
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"[-] API Delete Error: {e}")
        return None

def connect_socket(timeout=3.0):
    """Establishes a TCP socket connection to the gateway on port 1234."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((SOCKET_HOST, SOCKET_PORT))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s

def run_concurrent_client(addr, expected_idn):
    """Worker function for multi-client concurrency testing."""
    s = connect_socket()
    try:
        s.sendall(b"++auto 1\n")
        time.sleep(0.02)
        cmd = f"++addr {addr}\n".encode()
        s.sendall(cmd)
        time.sleep(0.02)
        s.sendall(b"*IDN?\n")
        resp = s.recv(1024).decode().strip()
        s.close()
        return addr, expected_idn in resp, resp
    except Exception as e:
        s.close()
        return addr, False, str(e)

def main():
    print("================================================================================")
    print("  VISA Mapping TCP/IP Socket Gateway (VMSG) Comprehensive Test Harness Suite  ")
    print("================================================================================")
    
    # Pre-test API availability check
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/status") as res:
            status = json.loads(res.read().decode())
            print(f"[+] Gateway REST API is online. Server status: {status.get('status')}")
    except Exception as e:
        print(f"[-] API Connection Error: {e}")
        print("[-] Ensure VMSG gateway server is running (python -u vmsg.py) before running tests.")
        return

    # Step 1: Pre-populate mock instruments in mappings via Web API
    print("[*] Configuring Virtual Mappings for Test Harness...")
    api_put_mapping(1, "MOCK::DMM::INSTR", "HEWLETT-PACKARD,34401A", "Mock HP Multimeter")
    api_put_mapping(2, "MOCK::SCOPE::INSTR", "TEKTRONIX,TDS 2024", "Mock Tek Scope")
    api_put_mapping(3, "MOCK::GENERIC::HP_53131A::INSTR", "HP_53131A", "Mock Counter")
    print("[+] Base test mappings configured successfully.")

    # Connect primary control socket
    s = connect_socket()
    print("[+] Primary TCP Socket connected successfully to port 1234.")

    try:
        # Test 1: Query version
        print("\n[Test 1] Querying Prologix Controller version (++ver)...")
        s.sendall(b"++ver\n")
        resp = s.recv(1024).decode().strip()
        print(f"[Result] Version response: '{resp}'")
        assert "Prologix GPIB-ETHERNET Controller version" in resp
        print("[+] Test 1 PASSED.")

        # Test 2: Set Address to 1 and query back
        print("\n[Test 2] Setting virtual address to 1 (++addr 1)...")
        s.sendall(b"++addr 1\n")
        time.sleep(0.02)
        s.sendall(b"++addr\n")
        resp = s.recv(1024).decode().strip()
        print(f"[Result] Current address response: '{resp}'")
        assert resp == "1"
        print("[+] Test 2 PASSED.")

        # Test 3: Auto mode (Read-after-write) querying *IDN?
        print("\n[Test 3] Setting ++auto 1 and sending query (*IDN?)...")
        s.sendall(b"++auto 1\n")
        time.sleep(0.02)
        start_time = time.perf_counter()
        s.sendall(b"*IDN?\n")
        resp = s.recv(1024).decode().strip()
        elapsed = (time.perf_counter() - start_time) * 1000
        print(f"[Result] IDN Response: '{resp}' (Latency: {elapsed:.2f} ms)")
        assert "HEWLETT-PACKARD,34401A" in resp
        print("[+] Test 3 PASSED.")

        # Test 4: Auto mode (Read-after-write) querying measurement
        print("\n[Test 4] Sending DMM measurement query (MEAS:VOLT:DC?)...")
        s.sendall(b"MEAS:VOLT:DC?\n")
        resp = s.recv(1024).decode().strip()
        print(f"[Result] Measurement: '{resp}'")
        assert "E" in resp or "." in resp
        print("[+] Test 4 PASSED.")

        # Test 5: Manual read mode (++auto 0)
        print("\n[Test 5] Disabling auto mode (++auto 0) and sending query...")
        s.sendall(b"++auto 0\n")
        time.sleep(0.02)
        s.sendall(b"MEAS:RES?\n")
        time.sleep(0.05)
        s.setblocking(False)
        try:
            junk = s.recv(1024)
            assert False, f"Should not receive data immediately when ++auto 0: {junk}"
        except BlockingIOError:
            print("[Result] Verified: No immediate response was written as expected.")
        finally:
            s.setblocking(True)

        print("[*] Triggering manual read (++read)...")
        s.sendall(b"++read\n")
        resp = s.recv(1024).decode().strip()
        print(f"[Result] Resistance measurement read: '{resp}'")
        assert "E" in resp or "." in resp
        print("[+] Test 5 PASSED.")

        # Test 6: Reset configurations (++rst)
        print("\n[Test 6] Resetting controller parameters (++rst)...")
        s.sendall(b"++rst\n")
        time.sleep(0.02)
        s.sendall(b"++addr\n")
        resp = s.recv(1024).decode().strip()
        assert resp == "1"
        s.sendall(b"++auto\n")
        resp = s.recv(1024).decode().strip()
        assert resp == "1"
        print("[+] Test 6 PASSED.")

        # Test 7: Extended Prologix Commands (lon, savecfg, spoll)
        print("\n[Test 7] Testing extended Prologix commands (++lon, ++savecfg, ++spoll)...")
        s.sendall(b"++lon 1\n")
        time.sleep(0.02)
        s.sendall(b"++lon\n")
        resp = s.recv(1024).decode().strip()
        assert resp == "1"

        s.sendall(b"++savecfg 0\n")
        time.sleep(0.02)
        s.sendall(b"++savecfg\n")
        resp = s.recv(1024).decode().strip()
        assert resp == "0"

        s.sendall(b"++spoll 1\n")
        resp = s.recv(1024).decode().strip()
        assert resp == "16"
        print("[+] Test 7 PASSED.")

        # Test 8: Address 0 Support & Clean Deletion
        print("\n[Test 8] Testing full GPIB Address 0 support...")
        api_put_mapping(0, "MOCK::SCOPE::INSTR", "TEKTRONIX,TDS 2024", "Mock Scope Address 0")
        s.sendall(b"++addr 0\n")
        time.sleep(0.02)
        s.sendall(b"++addr\n")
        resp = s.recv(1024).decode().strip()
        assert resp == "0"

        s.sendall(b"++auto 1\n")
        time.sleep(0.02)
        s.sendall(b"*IDN?\n")
        resp = s.recv(1024).decode().strip()
        assert "TEKTRONIX,TDS 2024" in resp

        s.sendall(b"++addr 1\n")
        time.sleep(0.02)
        api_delete_mapping(0)
        print("[+] Cleaned up virtual slot 0 after test.")
        print("[+] Test 8 PASSED.")

        # Test 9: TestController Config Generator API
        print("\n[Test 9] Testing TestController Config Generator REST API...")
        tc_url = f"{BASE_URL}/api/testcontroller/config?controller_id=A&host=127.0.0.1"
        with urllib.request.urlopen(tc_url) as tc_resp:
            tc_data = json.loads(tc_resp.read().decode())
            assert "PrologixEthernet|id:A|address:127.0.0.1" in tc_data.get("settingsGPIB")
            assert "Device:" in tc_data.get("settingsLoad")
            print("[+] Test 9 PASSED.")

        # Test 10: Parallel Multi-Client Socket Session Isolation
        print("\n[Test 10] Testing Parallel Multi-Client Socket Session Isolation (3 Parallel Sockets)...")
        test_targets = [
            (1, "HEWLETT-PACKARD,34401A"),
            (2, "TEKTRONIX,TDS 2024"),
            (3, "HP_53131A")
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(run_concurrent_client, addr, idn) for addr, idn in test_targets]
            for future in concurrent.futures.as_completed(futures):
                addr, success, response = future.result()
                print(f"[Result] Socket client for slot {addr}: Success={success}, Response='{response}'")
                assert success, f"Parallel socket test failed for address {addr}"
        print("[+] Test 10 PASSED.")

        # Test 11: Query Address Lock Isolation (last_query_addr)
        print("\n[Test 11] Testing Query Address Lock Isolation (last_query_addr)...")
        s.sendall(b"++auto 0\n")
        time.sleep(0.02)
        s.sendall(b"++addr 1\n")
        time.sleep(0.02)
        s.sendall(b"*IDN?\n")
        time.sleep(0.02)
        # Interleave address switch before reading
        s.sendall(b"++addr 2\n")
        time.sleep(0.02)
        s.sendall(b"++read eoi\n")
        resp = s.recv(1024).decode().strip()
        print(f"[Result] Interleaved query response: '{resp}'")
        assert "HEWLETT-PACKARD,34401A" in resp, "last_query_addr failed to lock queried address"
        print("[+] Test 11 PASSED.")

        # Test 12: Auto-Assign REST API
        print("\n[Test 12] Testing Auto-Assign REST API (/api/auto_assign)...")
        req = urllib.request.Request(
            f"{BASE_URL}/api/auto_assign",
            data=json.dumps({"force_overwrite": False, "include_mocks": True}).encode(),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req) as res:
            assign_res = json.loads(res.read().decode())
            print(f"[Result] Auto-assign response: assigned_count={assign_res.get('assigned_count')}")
            assert assign_res.get("status") == "success"
        print("[+] Test 12 PASSED.")

        # Test 13: USB Lottery Healing REST API
        print("\n[Test 13] Testing USB Lottery Healing REST API (/api/heal)...")
        req = urllib.request.Request(f"{BASE_URL}/api/heal", method='POST')
        with urllib.request.urlopen(req) as res:
            heal_res = json.loads(res.read().decode())
            print(f"[Result] Healing status: {heal_res.get('status')}, details={heal_res.get('details')}")
            assert heal_res.get("status") == "success"
        print("[+] Test 13 PASSED.")

        # Test 14: Config Backup & Restore API
        print("\n[Test 14] Testing Config Backup & Restore REST API (/api/config/backup & restore)...")
        with urllib.request.urlopen(f"{BASE_URL}/api/config/backup") as res:
            backup_data = json.loads(res.read().decode())
            assert "settings" in backup_data and "mappings" in backup_data
        
        req = urllib.request.Request(
            f"{BASE_URL}/api/config/restore",
            data=json.dumps(backup_data).encode(),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req) as res:
            restore_res = json.loads(res.read().decode())
            assert restore_res.get("status") == "success"
        print("[+] Test 14 PASSED.")

        # Test 15: Unmapped Address Timeout Protocol Handling
        print("\n[Test 15] Testing Unmapped Address Protocol Response...")
        s.sendall(b"++addr 30\n")
        time.sleep(0.02)
        s.sendall(b"++read eoi\n")
        resp = s.recv(1024).decode().strip()
        print(f"[Result] Unmapped address response: '{resp}'")
        assert "Error:" in resp or resp == ""
        print("[+] Test 15 PASSED.")

        # Restore primary socket to slot 1
        s.sendall(b"++addr 1\n")
        s.sendall(b"++auto 1\n")

        print("\n================================================================================")
        print("  ALL 15 INTEGRATION TESTS PASSED SUCCESSFULLY 100%!  ")
        print("  Your VISA Mapping TCP/IP Socket Gateway (VMSG) Test Harness is Complete. ")
        print("================================================================================")

    except AssertionError as e:
        print(f"\n[-] Assertion Failed during verification: {e}")
    except Exception as e:
        print(f"\n[-] Error during test execution: {e}")
    finally:
        s.close()
        print("[*] Primary Control Socket closed.")

if __name__ == "__main__":
    main()
