#!/usr/bin/env python3
"""
Install VMSG's git hooks into this clone.

Hooks live in tools/hooks/ so they are tracked and reviewable; .git/hooks is
not version controlled, so they have to be copied in per clone.

    python tools/install_hooks.py            # install
    python tools/install_hooks.py --status   # report what is installed
    python tools/install_hooks.py --uninstall

Existing hooks are backed up rather than overwritten.
"""

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE_DIR = os.path.join(ROOT, "tools", "hooks")


def hooks_dir():
    """Honour core.hooksPath, which worktrees and some setups rely on."""
    try:
        configured = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except Exception:
        configured = ""
    if configured:
        return os.path.join(ROOT, configured) if not os.path.isabs(configured) else configured
    try:
        git_dir = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT,
                                 capture_output=True, text=True,
                                 check=True).stdout.strip()
    except subprocess.CalledProcessError:
        sys.exit("ERROR: not a git repository")
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(ROOT, git_dir)
    return os.path.join(git_dir, "hooks")


def available():
    if not os.path.isdir(SOURCE_DIR):
        return []
    return sorted(f for f in os.listdir(SOURCE_DIR)
                  if not f.startswith(".") and
                  os.path.isfile(os.path.join(SOURCE_DIR, f)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    target_dir = hooks_dir()
    names = available()
    if not names:
        sys.exit(f"ERROR: no hooks found in {SOURCE_DIR}")

    if args.status:
        print(f"hooks directory: {target_dir}")
        for name in names:
            installed = os.path.join(target_dir, name)
            if not os.path.exists(installed):
                print(f"  {name}: NOT installed")
                continue
            same = (open(installed, "rb").read()
                    == open(os.path.join(SOURCE_DIR, name), "rb").read())
            runnable = "" if os.access(installed, os.X_OK) else " (NOT EXECUTABLE - git will skip it)"
            print(f"  {name}: installed"
                  + ("" if same else " (DIFFERS from tools/hooks)") + runnable)
        return 0

    os.makedirs(target_dir, exist_ok=True)

    if args.uninstall:
        for name in names:
            installed = os.path.join(target_dir, name)
            if os.path.exists(installed):
                os.remove(installed)
                print(f"  removed {name}")
                backup = installed + ".pre-vmsg"
                if os.path.exists(backup):
                    shutil.move(backup, installed)
                    print(f"  restored previous {name} from backup")
        print("\nHooks uninstalled.")
        return 0

    for name in names:
        src = os.path.join(SOURCE_DIR, name)
        dst = os.path.join(target_dir, name)
        if os.path.exists(dst):
            if open(dst, "rb").read() == open(src, "rb").read():
                print(f"  {name}: already current")
                continue
            backup = dst + ".pre-vmsg"
            if not os.path.exists(backup):
                shutil.copy2(dst, backup)
                print(f"  {name}: existing hook backed up to {os.path.basename(backup)}")
        shutil.copy2(src, dst)
        # Git requires the executable bit on POSIX; harmless on Windows.
        try:
            os.chmod(dst, 0o755)
        except OSError:
            pass
        print(f"  {name}: installed")

    print(f"\nInstalled into {target_dir}")

    # Copying the file and chmod'ing it is not evidence that git will run it.
    # Some filesystems accept the chmod, report the bit back through stat(), and
    # still fail access(X_OK) - notably an SMB share mounted noowners, which is
    # how this repository is reached from the lab workspace. Git then skips the
    # hook with a one-line hint that is easy to read past, and the gate is gone
    # without anything failing. Check the thing git actually checks.
    inert = [n for n in names
             if not os.access(os.path.join(target_dir, n), os.X_OK)]
    if inert:
        print("\n!! WARNING: " + ", ".join(inert) + " cannot be executed here.")
        print("   git will SKIP these hooks. Nothing gates your pushes.")
        print("   This is normal on a network share (SMB/CIFS mounted noowners);")
        print("   the exec bit is displayed but access(X_OK) fails.")
        print("   Run the checks by hand before pushing:")
        print("     python -m pyflakes vmsg.py vmsg_core tests tools build_binary.py")
        print("     python tools/verify_offline.py")
        print("   CI still runs them, but after the push rather than before.")
        return 1

    print("The pre-push hook runs pyflakes and the offline fidelity suite "
          "(~10s).\nBypass with: git push --no-verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
