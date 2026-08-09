#!/usr/bin/env python3
"""
Re-apply the VMSG interoperability patches to a TestController.jar.

Why this exists
---------------
TestController's Prologix Ethernet support has four defects that break
multi-instrument use through VMSG (and, we believe, through real Prologix
adapters). They are documented, with evidence, in TESTCONTROLLER_NOTES.md and
TESTCONTROLLER_REPORT_FOR_DEVELOPER.md. If upstream fixes them, delete this
script. Until then, this re-applies the fixes to any TestController build.

How it works
------------
Rather than shipping patched source (which would go stale the moment a new
TestController is released), this decompiles the four affected classes out of
*your* jar, rewrites specific lines, recompiles them against that same jar, and
writes a new jar with only those classes replaced. Your original jar is never
modified.

Every edit is anchored to a distinctive pattern and verified. If a future
TestController changes one of these methods enough that an anchor no longer
matches, the script stops and tells you which patch failed rather than emitting
a partly-patched jar.

Requirements
------------
A Java runtime (JRE 8 or newer) plus two standalone tools, which --fetch-tools
will download for you:
  * CFR 0.152     - decompiler        (github.com/leibnitz27/cfr)
  * ECJ 4.6.1     - Eclipse compiler, runs on a plain JRE, no JDK needed

Usage
-----
  python patch_testcontroller.py --fetch-tools          # one time
  python patch_testcontroller.py                        # patch (auto-locates jar)
  python patch_testcontroller.py --jar path/to/TestController.jar
  python patch_testcontroller.py --check                # report only, write nothing

Then launch the patched build:
  java -jar TestController-patched.jar
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

CFR_URL = "https://github.com/leibnitz27/cfr/releases/download/0.152/cfr-0.152.jar"
ECJ_URL = "https://repo1.maven.org/maven2/org/eclipse/jdt/core/compiler/ecj/4.6.1/ecj-4.6.1.jar"
CFR_NAME = "cfr-0.152.jar"
ECJ_NAME = "ecj-4.6.1.jar"

# Classes we decompile, patch and recompile. Inner classes ride along with their
# outer class, so SharedInterface.java also yields SharedInterface$DeviceSettings.
TARGET_CLASSES = [
    "dk/hkj/shared/SharedInterface",
    "dk/hkj/shared/SharedInterfacePrologixUSB",
    "dk/hkj/shared/SharedInterfacePrologixEthernet",
]

# Each patch: (id, source file, description, find regex, replacement, already-applied regex)
# Anchors are whitespace-tolerant so ordinary formatting drift does not break them.
# The already-applied regex distinguishes "upstream adopted this fix" (fine, skip)
# from "the code changed and the anchor no longer matches" (stop, tell the user).
PATCHES = [
    (
        "1-idempotent-socket",
        "SharedInterfacePrologixEthernet.java",
        "Do not replace a SocketInterface that is already in use",
        re.compile(
            r"(public\s+String\s+neededCommInterface\s*\(\s*\)\s*\{)\s*"
            r"(this\.ci\s*=\s*new\s+SocketInterface\([^;]*;)\s*"
            r"(this\.ci\.debugLog\s*=[^;]*;)\s*"
            r"(return\s+null\s*;)"
        ),
        r"\1\n        if (this.ci == null) {\n            \2\n            \3\n        }\n        \4",
        re.compile(
            r"public\s+String\s+neededCommInterface\s*\(\s*\)\s*\{\s*"
            r"if\s*\(\s*this\.ci\s*==\s*null\s*\)"
        ),
    ),
    (
        "2-atomic-writeread",
        "SharedInterface.java",
        "Make write-then-read atomic so threads cannot consume each other's replies",
        # Parameter names vary across decompiles (localAddress vs n vs ...), so
        # match on the signature shape, not a specific identifier.
        re.compile(
            r"public\s+(?!synchronized)(String\s+writeRead\s*\(\s*int\s+\w+\s*,)"
        ),
        r"public synchronized \1",
        re.compile(r"public\s+synchronized\s+String\s+writeRead\s*\(\s*int\s+\w+\s*,"),
    ),
    (
        "3-open-bookkeeping",
        "SharedInterface.java",
        "Record isOpen for every device sharing the interface, not just the first",
        re.compile(
            r"(this\.init\(\);)\s*"
            r"(this\.getDeviceSettings\((?:\(int\))?\w+\)\.isOpen\s*=\s*true;)\s*"
            r"\}\s*\}"
        ),
        r"\1\n        }\n        \2\n    }",
        # Applied form: the isOpen assignment sits AFTER the closing brace of
        # the if (!openStatus()) block rather than inside it.
        re.compile(
            r"this\.init\(\);\s*\}\s*"
            r"this\.getDeviceSettings\((?:\(int\))?\w+\)\.isOpen\s*=\s*true;"
        ),
    ),
    (
        "4-reset-addr-cache",
        "SharedInterfacePrologixUSB.java",
        "Invalidate the address/timeout cache on every (re)connect",
        re.compile(
            r"(public\s+void\s+init\s*\(\s*\)\s*\{)\s*"
            r"(this\.writeWithDelay\(\"\+\+auto 0\"\);)"
        ),
        r"\1\n        this.selectedLocalAddress = -1;\n        this.selectedTimeout = -1;\n        \2",
        re.compile(
            r"public\s+void\s+init\s*\(\s*\)\s*\{\s*"
            r"this\.selectedLocalAddress\s*=\s*-1;"
        ),
    ),
]

DEFAULT_JAR_LOCATIONS = [
    os.path.expanduser("~/Documents/TestController/TestController.jar"),
    os.path.expanduser("~/TestController/TestController.jar"),
    "./TestController.jar",
]


def log(msg):
    print(msg, flush=True)


def die(msg, code=1):
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def have_java():
    try:
        subprocess.run(["java", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def fetch_tools(tools_dir):
    import urllib.request
    os.makedirs(tools_dir, exist_ok=True)
    for name, url in ((CFR_NAME, CFR_URL), (ECJ_NAME, ECJ_URL)):
        dest = os.path.join(tools_dir, name)
        if os.path.exists(dest):
            log(f"  already present: {name}")
            continue
        log(f"  downloading {name} from {url}")
        urllib.request.urlretrieve(url, dest)
        log(f"  saved {dest} ({os.path.getsize(dest):,} bytes)")


def locate_jar(explicit):
    if explicit:
        if not os.path.isfile(explicit):
            die(f"jar not found: {explicit}")
        return os.path.abspath(explicit)
    for cand in DEFAULT_JAR_LOCATIONS:
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    die("could not find TestController.jar - pass --jar with its path")


def decompile(cfr, jar, workdir):
    classes_dir = os.path.join(workdir, "classes")
    src_dir = os.path.join(workdir, "src")
    os.makedirs(classes_dir, exist_ok=True)

    with zipfile.ZipFile(jar) as z:
        names = set(z.namelist())
        wanted = []
        for cls in TARGET_CLASSES:
            entry = cls + ".class"
            if entry not in names:
                die(f"{entry} is not in this jar - is it really a TestController build?")
            wanted.append(entry)
            # Inner classes must sit beside their outer class or the decompiled
            # source will reference types it never defines (e.g. DeviceSettings).
            wanted.extend(n for n in names if n.startswith(cls + "$") and n.endswith(".class"))

        for entry in wanted:
            target = os.path.join(classes_dir, *entry.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(z.read(entry))

    for cls in TARGET_CLASSES:
        cls_path = os.path.join(classes_dir, *(cls + ".class").split("/"))
        res = subprocess.run(
            ["java", "-jar", cfr, cls_path, "--outputdir", src_dir],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            die(f"decompiling {cls} failed:\n{res.stdout}\n{res.stderr}")
    return src_dir


def apply_patches(src_dir, check_only):
    pkg_dir = os.path.join(src_dir, "dk", "hkj", "shared")
    results, failures = [], []

    for pid, filename, desc, pattern, replacement, applied_pattern in PATCHES:
        path = os.path.join(pkg_dir, filename)
        if not os.path.isfile(path):
            failures.append((pid, desc, f"{filename} was not produced by the decompiler"))
            continue

        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()

        new_text, count = pattern.subn(replacement, text, count=1)
        if count == 0:
            # Distinguish "already patched" from "anchor no longer matches"
            if applied_pattern.search(text):
                results.append((pid, desc, "already applied - skipped"))
            else:
                failures.append((pid, desc, "anchor pattern not found (code may have changed)"))
            continue

        if not check_only:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        results.append((pid, desc, "applied"))

    return results, failures


def compile_sources(ecj, jar, src_dir, workdir):
    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    sources = []
    for root, _, files in os.walk(src_dir):
        sources.extend(os.path.join(root, f) for f in files if f.endswith(".java"))
    if not sources:
        die("no decompiled sources to compile")

    res = subprocess.run(
        ["java", "-jar", ecj, "-source", "1.8", "-target", "1.8", "-nowarn",
         "-cp", jar, "-d", out_dir] + sources,
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        die("recompilation failed. The decompiled source may need a manual touch-up "
            f"for this TestController version:\n{res.stdout}\n{res.stderr}")
    return out_dir


def repackage(jar, out_dir, output_jar):
    replacements = {}
    for root, _, files in os.walk(out_dir):
        for fn in files:
            if fn.endswith(".class"):
                full = os.path.join(root, fn)
                entry = os.path.relpath(full, out_dir).replace(os.sep, "/")
                replacements[entry] = full

    replaced, missing = [], []
    with zipfile.ZipFile(jar) as zin, \
         zipfile.ZipFile(output_jar, "w", zipfile.ZIP_DEFLATED) as zout:
        jar_entries = set(zin.namelist())
        for entry in replacements:
            if entry not in jar_entries:
                missing.append(entry)
        for item in zin.infolist():
            if item.filename in replacements:
                zout.write(replacements[item.filename], item.filename)
                replaced.append(item.filename)
            else:
                zout.writestr(item, zin.read(item.filename))

    if missing:
        die("compiled classes have no counterpart in the jar: " + ", ".join(missing))
    return replaced


def main():
    ap = argparse.ArgumentParser(
        description="Apply VMSG interoperability patches to TestController.jar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--jar", help="path to TestController.jar (auto-detected if omitted)")
    ap.add_argument("--output", help="output jar (default: TestController-patched.jar beside the input)")
    ap.add_argument("--tools-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc-patch-tools"),
                    help="where CFR and ECJ live")
    ap.add_argument("--fetch-tools", action="store_true", help="download CFR and ECJ, then exit")
    ap.add_argument("--check", action="store_true", help="report which patches would apply; write nothing")
    ap.add_argument("--keep-work", action="store_true", help="keep the temporary work directory for inspection")
    args = ap.parse_args()

    log("TestController patcher (VMSG interoperability fixes)")
    log("=" * 60)

    if args.fetch_tools:
        log(f"Fetching tools into {args.tools_dir}")
        log("  CFR  - github.com/leibnitz27/cfr (MIT)")
        log("  ECJ  - Eclipse JDT batch compiler (EPL)")
        fetch_tools(args.tools_dir)
        log("\nTools ready. Now run without --fetch-tools to patch.")
        return

    if not have_java():
        die("no 'java' on PATH. A JRE 8 or newer is required.")

    cfr = os.path.join(args.tools_dir, CFR_NAME)
    ecj = os.path.join(args.tools_dir, ECJ_NAME)
    for tool, name in ((cfr, CFR_NAME), (ecj, ECJ_NAME)):
        if not os.path.isfile(tool):
            die(f"{name} not found in {args.tools_dir}. Run with --fetch-tools first.")

    jar = locate_jar(args.jar)
    output_jar = args.output or os.path.join(os.path.dirname(jar), "TestController-patched.jar")
    log(f"Input jar : {jar}")
    log(f"Output jar: {'(none - check mode)' if args.check else output_jar}")

    with zipfile.ZipFile(jar) as z:
        sigs = [n for n in z.namelist()
                if n.startswith("META-INF/") and n.endswith((".SF", ".RSA", ".DSA", ".EC"))]
    if sigs:
        die("this jar is digitally signed; replacing classes would invalidate the signature. "
            "Not proceeding.")

    workdir = tempfile.mkdtemp(prefix="tcpatch-")
    try:
        log("\n[1/4] Decompiling affected classes...")
        src_dir = decompile(cfr, jar, workdir)
        log(f"      {len(TARGET_CLASSES)} classes decompiled")

        log("\n[2/4] Applying patches...")
        results, failures = apply_patches(src_dir, args.check)
        for pid, desc, status in results:
            log(f"      [ok]   {pid}: {desc}\n             -> {status}")
        for pid, desc, why in failures:
            log(f"      [FAIL] {pid}: {desc}\n             -> {why}")

        if failures:
            die(f"{len(failures)} patch(es) could not be applied to this TestController version.\n"
                "No output jar was written. The affected methods have likely changed;\n"
                "see TESTCONTROLLER_NOTES.md for what each patch does so it can be\n"
                "re-derived by hand.")

        if args.check:
            log(f"\nCheck complete: {len(results)} patch(es) would apply cleanly. Nothing written.")
            return

        log("\n[3/4] Recompiling...")
        out_dir = compile_sources(ecj, jar, src_dir, workdir)
        log("      compiled cleanly")

        log("\n[4/4] Building patched jar...")
        replaced = repackage(jar, out_dir, output_jar)
        log(f"      replaced {len(replaced)} class entries:")
        for entry in sorted(replaced):
            log(f"        {entry}")

        log("\n" + "=" * 60)
        log("Done. Your original jar is untouched.")
        log(f"Run the patched build with:\n  java -jar \"{output_jar}\"")
        log("Add 'debugTime' as an argument for a timestamped protocol trace.")
    finally:
        if args.keep_work:
            log(f"\nWork directory kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
