#!/usr/bin/env python3
"""
Integration test suite for VISA Mapping TCP/IP Socket Gateway (VMSG).

Tests Prologix GPIB-ETHERNET emulation protocol commands, SCPI pass-through,
parallel socket session isolation, query lock isolation, and REST API endpoints.
"""

import os
import socket
import sys
import time
import urllib.request
import urllib.error
import json
import concurrent.futures

BASE_URL = "http://127.0.0.1:8080"
SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 1234

# The control API requires a token; installing a global opener authenticates
# every urllib call below without touching each call site.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_auth_helper import install as _install_api_token  # noqa: E402
_API_TOKEN = _install_api_token()


def api_get(path):
    """GET helper returning parsed JSON."""
    with urllib.request.urlopen(f"{BASE_URL}{path}") as res:
        return json.loads(res.read().decode())


def snapshot_config():
    """Captures the gateway's full persistent config (settings + mappings)."""
    return api_get("/api/config/backup")


def restore_config(snapshot):
    """Restores a config snapshot so the suite leaves the gateway exactly as found."""
    req = urllib.request.Request(
        f"{BASE_URL}/api/config/restore",
        data=json.dumps(snapshot).encode(),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())


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
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[-] API Delete Error: {e}")
        return None
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


# ------------------------------------------------------------------------------
# Individual Test Functions
# ------------------------------------------------------------------------------

def test_01_prologix_version_query(s):
    """Tests Prologix Controller version query (++ver)."""
    print("\n[Test 1] Querying Prologix Controller version (++ver)...")
    s.sendall(b"++ver\n")
    resp = s.recv(1024).decode().strip()
    print(f"[Result] Version response: '{resp}'")
    assert "Prologix GPIB-ETHERNET Controller version" in resp
    print("[+] Test 1 PASSED.")


def test_02_prologix_address_selection(s):
    """Tests setting and querying virtual address (++addr 1)."""
    print("\n[Test 2] Setting virtual address to 1 (++addr 1)...")
    s.sendall(b"++addr 1\n")
    time.sleep(0.02)
    s.sendall(b"++addr\n")
    resp = s.recv(1024).decode().strip()
    print(f"[Result] Current address response: '{resp}'")
    assert resp == "1"
    print("[+] Test 2 PASSED.")


def test_03_prologix_auto_read_mode(s):
    """Tests auto read-after-write mode (++auto 1) with *IDN? query."""
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


def test_04_scpi_measurement_query(s):
    """Tests SCPI measurement pass-through query (MEAS:VOLT:DC?)."""
    print("\n[Test 4] Sending DMM measurement query (MEAS:VOLT:DC?)...")
    s.sendall(b"MEAS:VOLT:DC?\n")
    resp = s.recv(1024).decode().strip()
    print(f"[Result] Measurement: '{resp}'")
    assert "E" in resp or "." in resp
    print("[+] Test 4 PASSED.")


def test_05_prologix_manual_read_mode(s):
    """Tests manual read mode (++auto 0 and ++read)."""
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


def test_06_prologix_controller_reset(s):
    """Tests controller parameters reset (++rst)."""
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


def test_07_prologix_extended_commands(s):
    """Tests extended Prologix commands (++lon, ++savecfg, ++spoll)."""
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


def test_08_gpib_address_zero_support(s):
    """Tests full GPIB Address 0 mapping and query support."""
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


def test_09_testcontroller_config_generator_api():
    """Tests TestController Config Generator REST API endpoint & dynamic mapping updates."""
    print("\n[Test 9] Testing TestController Config Generator REST API & Dynamic Mapping Updates...")
    tc_url = f"{BASE_URL}/api/testcontroller/config?controller_id=A&host=127.0.0.1"
    
    # 1. Base export check
    with urllib.request.urlopen(tc_url) as tc_resp:
        tc_data = json.loads(tc_resp.read().decode())
        assert "PrologixEthernet|id:A|address:127.0.0.1" in tc_data.get("settingsGPIB")
        assert "Device:" in tc_data.get("settingsLoad")

    # 2. Add a new mapping at slot 28 and verify TC export updates dynamically
    api_put_mapping(28, "MOCK::DMM::INSTR_TC_TEST", "HEWLETT-PACKARD,34401A", "Dynamic TC Test DMM")
    with urllib.request.urlopen(tc_url) as tc_resp:
        tc_data = json.loads(tc_resp.read().decode())
        load_text = tc_data.get("settingsLoad", "")
        mapped_addrs = [d.get("address") for d in tc_data.get("mapped_devices", [])]
        assert 28 in mapped_addrs, "Newly added slot 28 should be included in TestController export"
        assert ":28|" in load_text or ":28" in load_text, "Newly added slot 28 address should appear in TestController settingsLoad text"

    # 3. Delete mapping at slot 28 and verify TC export updates dynamically
    api_delete_mapping(28)
    with urllib.request.urlopen(tc_url) as tc_resp:
        tc_data = json.loads(tc_resp.read().decode())
        load_text = tc_data.get("settingsLoad", "")
        mapped_addrs = [d.get("address") for d in tc_data.get("mapped_devices", [])]
        assert 28 not in mapped_addrs, "Deleted slot 28 should no longer appear in TestController mapped_devices"
        assert ":28|" not in load_text, "Deleted slot 28 should no longer appear in TestController settingsLoad text"

    print("[+] Test 9 PASSED.")


def test_10_parallel_multi_client_session_isolation():
    """Tests parallel socket session isolation across concurrent clients."""
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


def test_11_query_address_lock_isolation(s):
    """Tests query address lock isolation (last_query_addr)."""
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


def test_12_auto_assign_rest_api():
    """Tests Auto-Assign REST API (/api/auto_assign)."""
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


def test_14_config_backup_and_restore_api():
    """Tests Config Backup & Restore REST API (/api/config/backup & restore)."""
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


def test_15_unmapped_address_protocol_handling(s):
    """Tests unmapped address protocol error response."""
    print("\n[Test 15] Testing Unmapped Address Protocol Response...")
    api_delete_mapping(30)
    s.sendall(b"++addr 30\n")
    time.sleep(0.02)
    s.sendall(b"++read eoi\n")
    resp = s.recv(1024).decode().strip()
    print(f"[Result] Unmapped address response: '{resp}'")
    assert "Error:" in resp or resp == ""
    print("[+] Test 15 PASSED.")


# ------------------------------------------------------------------------------
# Test Runner Entry Point
# ------------------------------------------------------------------------------

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
        sys.exit(1)

    print("[*] Snapshotting live gateway configuration (will be restored at exit)...")
    original_config = snapshot_config()
    print(f"[+] Snapshot captured: {len(original_config.get('mappings', {}))} mapping(s).")

    passed = False
    s = None
    try:
        print("[*] Configuring Virtual Mappings for Test Harness...")
        api_put_mapping(1, "MOCK::DMM::INSTR", "HEWLETT-PACKARD,34401A", "Mock HP Multimeter")
        api_put_mapping(2, "MOCK::SCOPE::INSTR", "TEKTRONIX,TDS 2024", "Mock Tek Scope")
        api_put_mapping(3, "MOCK::GENERIC::HP_53131A::INSTR", "HP_53131A", "Mock Counter")
        print("[+] Base test mappings configured successfully.")

        s = connect_socket()
        print("[+] Primary TCP Socket connected successfully to port 1234.")

        # Execute tests sequentially
        test_01_prologix_version_query(s)
        test_02_prologix_address_selection(s)
        test_03_prologix_auto_read_mode(s)
        test_04_scpi_measurement_query(s)
        test_05_prologix_manual_read_mode(s)
        test_06_prologix_controller_reset(s)
        test_07_prologix_extended_commands(s)
        test_08_gpib_address_zero_support(s)
        test_09_testcontroller_config_generator_api()
        test_10_parallel_multi_client_session_isolation()
        test_11_query_address_lock_isolation(s)
        test_12_auto_assign_rest_api()
        test_14_config_backup_and_restore_api()
        test_15_unmapped_address_protocol_handling(s)
        test_16_optional_testcontroller_driver_validation()
        test_17_multi_port_and_force_addr_settings_toggle()
        test_18_usb_heal_check_status_api()
        test_19_testcontroller_driver_directory_validation_and_rescan()
        test_20_lxi_raw_scpi_socket_port_5025()
        test_21_hardware_preset_profile_switching()

        # Restore primary socket state
        s.sendall(b"++addr 1\n")
        s.sendall(b"++auto 1\n")

        print("\n================================================================================")
        print("  ALL 21 INTEGRATION TESTS PASSED SUCCESSFULLY 100%!  ")
        print("  Your VISA Mapping TCP/IP Socket Gateway (VMSG) Test Harness is Complete. ")
        print("================================================================================")
        passed = True

    except AssertionError as e:
        print(f"\n[-] Assertion Failed during verification: {e}")
    except Exception as e:
        print(f"\n[-] Error during test execution: {e}")
    finally:
        if s is not None:
            s.close()
            print("[*] Primary Control Socket closed.")
        try:
            restore_config(original_config)
            print("[+] Gateway configuration restored to pre-test snapshot.")
        except Exception as e:
            print(f"[-] FAILED to restore gateway configuration: {e}")
            print(f"[-] Recover manually by POSTing this snapshot to {BASE_URL}/api/config/restore:")
            print(json.dumps(original_config))
            passed = False

    if not passed:
        sys.exit(1)


def test_16_optional_testcontroller_driver_validation():
    """Tests optional TestController driver file validation and export behavior."""
    print("\n[Test 16] Testing Optional TestController Driver Validation & Export...")
    
    # 1. Disable driver validation
    settings_payload = {"tc_enable_driver_validation": False}
    req = urllib.request.Request(
        f"{BASE_URL}/api/settings",
        data=json.dumps(settings_payload).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        
    # 2. Map a custom unknown device
    api_put_mapping(29, "MOCK::UNKNOWN_CUSTOM::INSTR", "CUSTOM_MODEL_9999", "Custom Bench Device")
    
    # 3. Request TC config export
    with urllib.request.urlopen(f"{BASE_URL}/api/testcontroller/config") as res:
        tc_cfg = json.loads(res.read().decode())
        excluded = tc_cfg.get("excluded_devices", [])
        # When validation is disabled, no mapped device should be excluded
        custom_excluded = [d for d in excluded if d.get("address") == 29]
        assert len(custom_excluded) == 0, "Custom device should NOT be excluded when driver validation is disabled"
        
    api_delete_mapping(29)
    print("[+] Test 16 PASSED.")


def test_17_multi_port_and_force_addr_settings_toggle():
    """Tests Dedicated Port Per Device (multi_port_enabled) and ++addr toggles."""
    print("\n[Test 17] Testing Dedicated Port Per Device & ++addr Settings Toggles...")
    
    # 1. Enable multi-port mode and force_addr
    payload_on = {
        "multi_port_enabled": True,
        "multi_port_base": 1235,
        "tc_force_addr": True
    }
    req_on = urllib.request.Request(
        f"{BASE_URL}/api/settings",
        data=json.dumps(payload_on).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req_on) as res:
        data = json.loads(res.read().decode())
        assert data.get("settings", {}).get("multi_port_enabled") is True
        assert data.get("settings", {}).get("tc_force_addr") is True

    # 2. Disable multi-port mode and force_addr cleanly
    payload_off = {
        "multi_port_enabled": False,
        "tc_force_addr": False
    }
    req_off = urllib.request.Request(
        f"{BASE_URL}/api/settings",
        data=json.dumps(payload_off).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req_off) as res:
        data = json.loads(res.read().decode())
        assert data.get("settings", {}).get("multi_port_enabled") is False
    print("[+] Test 17 PASSED.")


def test_18_usb_heal_check_status_api():
    """Tests GET /api/heal/check status endpoint."""
    print("\n[Test 18] Testing GET /api/heal/check (USB Healing Candidate Check)...")
    with urllib.request.urlopen(f"{BASE_URL}/api/heal/check") as res:
        check_res = json.loads(res.read().decode())
        assert check_res.get("status") == "success"
        assert isinstance(check_res.get("slots_needing_healing"), list)
    print("[+] Test 18 PASSED.")


def test_19_testcontroller_driver_directory_validation_and_rescan():
    """Tests TestController driver directory validation, missing path warnings, and re-scan API."""
    import os, tempfile, shutil
    print("\n[Test 19] Testing Driver Directory Validation, Rejection of Invalid Path & Re-scan API...")
    
    # 1. Attempting to enable validation with an invalid directory path must be REJECTED (HTTP 400)
    invalid_path = "/non_existent_tc_devices_dir_12345"
    payload_invalid = {
        "tc_enable_driver_validation": True,
        "tc_devices_path": invalid_path
    }
    req_inv = urllib.request.Request(
        f"{BASE_URL}/api/settings",
        data=json.dumps(payload_invalid).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req_inv) as res:
            assert False, "Should have rejected enabling validation with an invalid directory path"
    except urllib.error.HTTPError as err:
        assert err.code == 400, f"Expected HTTP 400 rejection, got {err.code}"

    # 2. Create a temporary valid directory with a mock driver file
    temp_dir = tempfile.mkdtemp(prefix="vmsg_tc_drivers_")
    try:
        mock_driver_path = os.path.join(temp_dir, "mock_scope.txt")
        with open(mock_driver_path, "w") as f:
            f.write("#name TestCustomDriver99\n#type Scope\n")

        # 3. Point settings to valid temp directory
        payload_valid = {
            "tc_enable_driver_validation": True,
            "tc_devices_path": temp_dir
        }
        req_val = urllib.request.Request(
            f"{BASE_URL}/api/settings",
            data=json.dumps(payload_valid).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req_val) as res:
            assert res.status == 200

        # 4. Rescan valid directory
        rescan_valid = urllib.request.Request(
            f"{BASE_URL}/api/testcontroller/rescan_drivers",
            data=b"{}",
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(rescan_valid) as res:
            res_data = json.loads(res.read().decode())
            assert res_data.get("directory_valid") is True, "Valid temp directory should set directory_valid=True"
            assert res_data.get("validation_status") == "active"
            assert res_data.get("driver_count") >= 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # 5. After temp directory is removed, calling config endpoint unsets option
    with urllib.request.urlopen(f"{BASE_URL}/api/testcontroller/config") as res:
        tc_data = json.loads(res.read().decode())
        assert tc_data.get("driver_directory_valid") is False
        assert tc_data.get("driver_validation_enabled") is False, "Driver validation option should auto-unset when directory disappears"

    print("[+] Test 19 PASSED.")


def test_20_lxi_raw_scpi_socket_port_5025():
    """Tests LXI SCPI Raw Socket Server on Port 5025."""
    print("\n[Test 20] Testing LXI SCPI Raw Socket Server (Port 5025)...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect((SOCKET_HOST, 5025))
    s.sendall(b"*IDN?\n")
    data = s.recv(1024).decode().strip()
    s.close()
    print(f"[Result] LXI Port 5025 IDN Response: '{data}'")
    assert len(data) > 0
    print("[+] Test 20 PASSED.")


def test_21_hardware_preset_profile_switching():
    """Tests Hardware Preset Profile Switching via REST API and ++ver response."""
    print("\n[Test 21] Testing Hardware Preset Profile Switching via REST API...")
    
    # 1. Switch preset profile to Keysight E5810A
    payload = {"preset_profile": "Keysight E5810A LAN/GPIB Gateway"}
    req = urllib.request.Request(
        f"{BASE_URL}/api/settings",
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200

    s = connect_socket()
    s.sendall(b"++ver\n")
    ver_resp = s.recv(1024).decode().strip()
    s.close()
    print(f"[Result] E5810A Preset ++ver response: '{ver_resp}'")
    assert "Agilent E5810A" in ver_resp

    # 2. Reset preset profile to Prologix Ethernet Default
    payload_def = {"preset_profile": "Prologix Ethernet (Official v01.06.06.00)"}
    req_def = urllib.request.Request(
        f"{BASE_URL}/api/settings",
        data=json.dumps(payload_def).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req_def) as res:
        assert res.status == 200

    s_def = connect_socket()
    s_def.sendall(b"++ver\n")
    ver_def_resp = s_def.recv(1024).decode().strip()
    s_def.close()
    print(f"[Result] Prologix Default ++ver response: '{ver_def_resp}'")
    assert "Prologix GPIB-ETHERNET Controller version 01.06.06.00" in ver_def_resp

    print("[+] Test 21 PASSED.")


if __name__ == "__main__":
    main()

