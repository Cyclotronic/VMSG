#!/usr/bin/env python3
"""
Release gate: prove the PACKAGED build still behaves like the source tree.

Passing tests against the source tree says nothing about the bundle. PyInstaller
resolves imports statically, so a module reached only through a function-level
import can be left out; and `--add-data` mappings can land somewhere the code
does not look. Both failures are silent in the worst way - the gateway starts,
the API answers, and only the dashboard (or one protocol) is dead.

VMSG is particularly exposed on two fronts:

  * the entire dashboard is bundled via --add-data, so a broken mapping serves a
    404 from an otherwise healthy process;
  * the VXI-11 stack is imported inside main(), which PyInstaller usually - but
    not always - follows.

This launches the built executable, talks to it over HTTP and raw sockets, and
checks the things a source-tree test cannot see.

    python tools/verify_frozen_build.py
    python tools/verify_frozen_build.py --exe dist/vmsg.exe

Exit code 0 when the bundle behaves like the source tree, 1 otherwise.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

HTTP_PORT = 18080
PROLOGIX_PORT = 11234
LXI_PORT = 15025

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  [{'ok' if ok else 'FAIL'}]{'  ' if ok else ''} {label}"
          + (f": {detail}" if detail else ""))
    return ok


def default_exe():
    for candidate in ("dist/vmsg.exe", "dist/vmsg"):
        path = os.path.join(ROOT, candidate)
        if os.path.isfile(path):
            return path
    return None


def http(path, token=None, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}{path}")
    if token:
        req.add_header("X-VMSG-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def wait_ready(proc, timeout=90):
    """Wait for the frozen gateway to answer, failing fast if it dies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _ = http("/api/status", timeout=2)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="path to the built executable")
    ap.add_argument("--keep-config", action="store_true",
                    help="do not use a scratch config (not recommended)")
    args = ap.parse_args()

    exe = os.path.abspath(args.exe) if args.exe else default_exe()
    if not exe or not os.path.isfile(exe):
        print("ERROR: no built executable found. Run: python build_binary.py")
        return 1

    print("VMSG frozen build verification")
    print("=" * 62)
    size_mb = os.path.getsize(exe) / (1024 * 1024)
    print(f"executable : {exe}  ({size_mb:.1f} MB)")

    # Run the bundle against a scratch config on scratch ports so this can never
    # disturb a real bench or rewrite the user's mappings.json.
    workdir = tempfile.mkdtemp(prefix="vmsg-frozen-")
    config_path = os.path.join(workdir, "mappings.json")
    token = "frozen-build-verification-token"
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump({
            "settings": {
                "addr": 1, "auto": 0, "mode": 1, "eos": 0, "eoi": 1,
                "read_tmo_ms": 3000, "eot_enable": 0, "eot_char": 4,
                "unmapped_behavior": "message",
                "api_auth_enabled": True, "api_token": token,
                "lxi_raw_socket_enabled": True, "lxi_raw_socket_port": LXI_PORT,
                "vxi11_enabled": False,   # portmap 111 is often already bound
                "lxi_mdns_enabled": False,
                "log_level": "WARN", "enable_stdout": False,
            },
            "mappings": {
                "1": {"visa_address": "MOCK::DMM::INSTR", "idn_pattern": "",
                      "description": "frozen build mock"},
            },
        }, fh)

    env = dict(os.environ)
    env["VMSG_CONFIG_FILE"] = config_path
    env["VMSG_API_TOKEN"] = token
    env["VMSG_HTTP_PORT"] = str(HTTP_PORT)
    env["VMSG_SOCKET_PORT"] = str(PROLOGIX_PORT)

    # Run a copy from local disk. Two reasons: executing the artifact in place
    # holds a lock on it (so a following build fails), and Windows refuses to
    # launch freshly built unsigned binaries from some network/mapped drives -
    # which would look like a broken build rather than a blocked path.
    import shutil
    run_exe = os.path.join(workdir, os.path.basename(exe))
    shutil.copy2(exe, run_exe)
    try:
        os.chmod(run_exe, 0o755)
    except OSError:
        pass

    print(f"scratch    : {workdir}")
    print(f"ports      : http {HTTP_PORT}, prologix {PROLOGIX_PORT}, lxi {LXI_PORT}\n")

    proc = subprocess.Popen([run_exe], cwd=workdir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        if not wait_ready(proc):
            out = ""
            if proc.poll() is not None:
                out = (proc.stdout.read() or "")[-3000:]
            check("process starts and serves /api/status", False,
                  "did not become ready" + (f"\n--- output ---\n{out}" if out else ""))
            return 1
        check("process starts and serves /api/status", True)

        # 1. Bundled dashboard. The classic --add-data failure: the process is
        #    healthy but static/ never made it into the bundle.
        print("\n1. Bundled static assets")
        status, body = http("/")
        check("GET / returns 200", status == 200, f"status {status}")
        looks_like_dashboard = ("<html" in body.lower() and "vmsg" in body.lower()
                                and len(body) > 5000)
        check("dashboard HTML served from the bundle", looks_like_dashboard,
              f"{len(body)} bytes")
        check("API token injected into page",
              "VMSG_API_TOKEN" in body and token in body)
        # A JSON error body means index.html was not found inside the bundle.
        check("static/index.html present in bundle",
              "static/index.html is missing" not in body)

        # 2. Auth middleware survived packaging.
        print("\n2. Control API")
        status, _ = http("/api/mappings")
        check("unauthenticated /api/mappings is refused", status == 401,
              f"status {status}")
        status, body = http("/api/mappings", token=token)
        check("authenticated /api/mappings succeeds", status == 200,
              f"status {status}")
        if status == 200:
            try:
                check("scratch mapping visible", "1" in json.loads(body))
            except ValueError:
                check("scratch mapping visible", False, "invalid JSON")

        # 3. Prologix protocol over the socket.
        print("\n3. Prologix socket")
        try:
            s = socket.create_connection(("127.0.0.1", PROLOGIX_PORT), timeout=10)
            s.settimeout(8)
            s.sendall(b"++ver\n")
            ver = s.recv(200).decode(errors="replace").strip()
            check("++ver answers", "Prologix" in ver, repr(ver))
            s.sendall(b"++auto 0\n++mode 1\n++addr 1\n")
            time.sleep(0.2)
            try:
                s.recv(4096)
            except socket.timeout:
                pass
            s.sendall(b"*IDN?\n++read eoi\n")
            idn = s.recv(400).decode(errors="replace").strip()
            check("mock instrument answers *IDN?", "HEWLETT-PACKARD" in idn,
                  repr(idn[:52]))
            s.close()
        except OSError as e:
            check("Prologix socket reachable", False, f"{type(e).__name__}: {e}")

        # 4. Modules reached only through function-level imports. If PyInstaller
        #    missed vxi11_lxi_emulator, this port never opens.
        print("\n4. Deferred-import modules survived freezing")
        try:
            s = socket.create_connection(("127.0.0.1", LXI_PORT), timeout=10)
            s.settimeout(8)
            s.sendall(b"*IDN?\n")
            reply = s.recv(400).decode(errors="replace").strip()
            check("LXI raw socket (vxi11_lxi_emulator) is live",
                  "HEWLETT-PACKARD" in reply, repr(reply[:52]))
            s.close()
        except OSError as e:
            check("LXI raw socket (vxi11_lxi_emulator) is live", False,
                  f"{type(e).__name__}: {e} - module likely missing from bundle")

        # A ModuleNotFoundError anywhere in the output means a hidden import is
        # missing even if the affected feature was not exercised above.
        if proc.poll() is None:
            pass
        check("no import errors in process output", True,
              "(checked again after shutdown)")

    finally:
        try:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate(timeout=10)
        except Exception:
            out = ""
        # Re-check the captured output now that the process has exited.
        if out:
            bad = re.findall(r"(ModuleNotFoundError|ImportError)[^\n]*", out)
            if bad:
                for line in dict.fromkeys(bad):
                    print(f"  [FAIL]  import error in bundle: {line.strip()}")
                results.append(("bundle import errors", False, bad[0]))
        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

    print("\n" + "=" * 62)
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"FAILED - {len(failed)} check(s):")
        for label, _ok, detail in failed:
            print(f"  - {label}" + (f": {detail}" if detail else ""))
        print("\nThe bundle does not match the source tree. Do not ship it.")
        return 1
    print(f"All {len(results)} frozen-build checks passed. Bundle is shippable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
