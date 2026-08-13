#!/usr/bin/env python3
"""
Build the standalone VMSG executable.

    python build_binary.py                 # pre-flight checks, then build
    python build_binary.py --skip-checks   # build anyway (not for a release)
    python build_binary.py --no-verify     # skip the post-build frozen check

The pre-flight checks are not ceremony. VMSG is packaged as a single console
binary; a module that fails to import, or a dashboard that did not make it into
the bundle, produces a process that starts cleanly and is broken in one specific
way nobody notices until a user hits it. Lint and the offline fidelity suite
catch that class of problem before a build is produced, and
tools/verify_frozen_build.py catches the packaging-specific half afterwards.

Packaging is defined by vmsg.spec, which is tracked and reviewable, rather than
by command-line flags assembled here.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(SCRIPT_DIR, "vmsg.spec")
DIST = os.path.join(SCRIPT_DIR, "dist")
EXE_NAME = "vmsg.exe" if os.name == "nt" else "vmsg"


def run(label, cmd):
    """Run one pre-flight check. Returns True when it passed."""
    print(f"\n--- {label} ---")
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if result.returncode == 0:
        print(f"[+] {label}: passed")
        return True
    print(f"[-] {label}: FAILED (exit {result.returncode})")
    return False


def preflight():
    checks = [
        ("static analysis",
         [sys.executable, "-m", "pyflakes", "vmsg.py", "vmsg_core", "tests", "tools"]),
        ("offline protocol fidelity",
         [sys.executable, os.path.join("tools", "verify_offline.py")]),
    ]
    # Run every check rather than stopping at the first failure: one build
    # attempt should report everything that needs fixing.
    return all([run(label, cmd) for label, cmd in checks])


def check_toolchain():
    try:
        import PyInstaller  # noqa: F401
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        print("[-] PyInstaller is not installed.")
        print("    A build step should not silently modify its own toolchain,")
        print("    so this does not auto-install. Install the pinned versions:")
        print("        pip install -r requirements-release.txt")
        return False
    import PyInstaller
    print(f"[+] PyInstaller {PyInstaller.__version__}")
    return True


def write_provenance(exe_path):
    """Record what produced this artifact, next to the artifact."""
    import importlib.metadata as md

    packages = {}
    for name in ("pyinstaller", "fastapi", "uvicorn", "pyvisa", "pyvisa-py",
                 "pyserial", "pyusb", "starlette", "pydantic"):
        try:
            packages[name] = md.version(name)
        except Exception:
            packages[name] = None

    digest = hashlib.sha256()
    with open(exe_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)

    sys.path.insert(0, SCRIPT_DIR)
    try:
        from vmsg_core.version import __version__
    except Exception:
        __version__ = "unknown"

    info = {
        "vmsg_version": __version__,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "artifact": os.path.basename(exe_path),
        "size_bytes": os.path.getsize(exe_path),
        "sha256": digest.hexdigest(),
        "packages": packages,
    }
    out = os.path.join(DIST, "build-info.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # A checksum file users can verify a download against.
    with open(os.path.join(DIST, os.path.basename(exe_path) + ".sha256"),
              "w", encoding="utf-8") as fh:
        fh.write(f"{info['sha256']}  {os.path.basename(exe_path)}\n")

    return info


def main():
    ap = argparse.ArgumentParser(
        description="Build the standalone VMSG executable.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--skip-checks", action="store_true",
                    help="skip pre-flight checks (throwaway builds only)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-build frozen verification")
    args = ap.parse_args()

    print("=" * 60)
    print("  VMSG standalone binary build")
    print("=" * 60)

    if not check_toolchain():
        return 1
    if not os.path.isfile(SPEC):
        print(f"[-] Missing {SPEC}. It is tracked in git; restore it.")
        return 1

    if args.skip_checks:
        print("\n!! pre-flight checks SKIPPED -- do not ship this build")
    elif not preflight():
        print("\n[-] Pre-flight checks failed. Nothing was built.")
        print("    Fix the above, or use --skip-checks for a throwaway build.")
        return 1

    print("\n--- PyInstaller ---")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", SPEC],
        cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print("\n[-] Build failed. See the PyInstaller output above.")
        return 1

    exe_path = os.path.join(DIST, EXE_NAME)
    if not os.path.isfile(exe_path):
        print(f"\n[-] Build reported success but {exe_path} does not exist.")
        return 1

    info = write_provenance(exe_path)
    print("\n--- artifact ---")
    print(f"  path    : {exe_path}")
    print(f"  version : {info['vmsg_version']}")
    print(f"  size    : {info['size_bytes'] / (1024 * 1024):.1f} MB")
    print(f"  sha256  : {info['sha256']}")
    print(f"  built   : {info['built_at']} on Python {info['python']}")
    print("  details : dist/build-info.json")

    if args.no_verify:
        print("\n!! frozen-build verification SKIPPED -- do not ship this build")
        return 0

    print("\n--- frozen build verification ---")
    verify = subprocess.run(
        [sys.executable, os.path.join("tools", "verify_frozen_build.py"),
         "--exe", exe_path],
        cwd=SCRIPT_DIR)
    if verify.returncode != 0:
        print("\n[-] The packaged build does not behave like the source tree.")
        print("    The artifact exists but should NOT be shipped.")
        return 1

    print("\n" + "=" * 60)
    print("  BUILD COMPLETE AND VERIFIED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
