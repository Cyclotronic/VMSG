# TestController Integration Notes: Multi-Device Controller Sharing Behavior

## 🔍 Technical Summary of Observed Behavior

When configuring multiple instruments in **TestController** connected via Prologix Ethernet (e.g. VMSG or physical Prologix Ethernet interface), an erratic startup issue can occur if all instruments are assigned to the **same Controller Instance ID** (for example, setting all devices to Controller `A`).

---

## 🛑 Observed Failure Mode (Single Controller ID `A`)

### 1. TestController Java Thread Architecture
- In TestController, `PrologixEthernet` extends `SharedInterfacePrologixUSB`.
- When multiple devices share Controller ID `A` (`PrologixEthernet|id:A|address:127.0.0.1`), TestController attempts to multiplex all device threads over a single underlying Java `SocketInterface` instance.

### 2. Startup Race Condition & Silent Thread Shutdown
- Upon launching TestController, a background thread is spawned for every configured instrument simultaneously (`Start thread for: PrologixEthernet A:4`, `A:2`, `A:6`, `A:7`, `A:5`).
- Thread 1 acquires the socket lock to execute controller initialization (`++auto 0`, `++mode 1`).
- When Threads 2–5 attempt to initialize while Thread 1 holds the socket lock, TestController's internal thread manager sees that the shared interface is locked, marks the remaining threads as unserviceable, and immediately shuts them down:
  ```text
  Start thread for: PrologixEthernet A:6 - Keithley 2001M
  Stopping thread for: PrologixEthernet A:6 - Keithley 2001M
  Start thread for: PrologixEthernet A:7 - Keithley 2002
  Stopping thread for: PrologixEthernet A:7 - Keithley 2002
  Start thread for: PrologixEthernet A:5 - Keithley 2010
  Stopping thread for: PrologixEthernet A:5 - Keithley 2010
  ```
- **Symptom**: Only 1 or 2 instruments load successfully on startup; the rest silently fail to connect or initialize without attempting network traffic.

---

## 🛠️ Recommended Configuration Workaround (Per-Device Socket Isolation)

To eliminate startup race conditions in TestController, configure a **separate Controller Instance ID** (`A`, `B`, `C`, `D`, `E`) for each device in `settingsGPIB.txt` and map them individually in `settingsLoad.txt`:

### `settingsGPIB.txt`:
```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:|
PrologixEthernet|id:B|address:127.0.0.1|baudrate:|settings:|
PrologixEthernet|id:C|address:127.0.0.1|baudrate:|settings:|
PrologixEthernet|id:D|address:127.0.0.1|baudrate:|settings:|
PrologixEthernet|id:E|address:127.0.0.1|baudrate:|settings:|
```

### `settingsLoad.txt`:
```text
Device:Agilent 34411A|PortType:GPIB|Address:A:4|Baudrate:9600|Enabled:1
Device:Fluke PM6690|PortType:GPIB|Address:B:2|Baudrate:9600|Enabled:1
Device:Keithley 2001M|PortType:GPIB|Address:C:6|Baudrate:9600|Enabled:1
Device:Keithley 2002|PortType:GPIB|Address:D:7|Baudrate:9600|Enabled:1
Device:Keithley 2010|PortType:GPIB|Address:E:5|Baudrate:9600|Enabled:1
```

### Why This Fixes It:
- Each device thread opens its own independent TCP socket connection to VMSG (`127.0.0.1:1234`).
- Eliminates Java socket lock contention during startup.
- VMSG seamlessly handles each concurrent socket connection in parallel.
- All instruments discover, initialize, and stream data 100% reliably.

---

## 📖 TestController Configuration File Reference

*Derived from the parsers themselves (`SharedInterface`, `SharedInterfaceList`, `SharedInterfacePrologixUSB`, `LoadDeviceConfig`, `Support.ConfigLineSplitter`) rather than from documentation, so it reflects what the code actually accepts. Files live in TestController's `Settings` folder.*

### Common line syntax
Both config files are parsed line by line. In **both**, a line is ignored unless it contains `|`, and a line beginning with `;` is a comment. Fields are `name:value` pairs separated by `|`; **both name and value are trimmed**, so alignment spaces are harmless. A value may itself contain `:` — only the *first* colon splits (that is why `settings:port:1235` works).

### `settingsGPIB.txt` — shared interface (adapter) definitions
```text
<Type>|id:<ID>|address:<HOST or PORT>|baudrate:<SETTINGS>|settings:<OPTIONS>|
```

| Field | Meaning |
| :--- | :--- |
| `<Type>` | Interface class, matched by line prefix. Recognised: `PrologixEthernet`, `PrologixUSB`, `AR488Lan`, `AR488`, `Kofen`, `NSGPIB232CT`, `KeysightE5810`, `Modbus`, `SharedInterface`. **Order matters:** `PrologixUSB` is tested before `PrologixEthernet`, so the prefix must match exactly. |
| `id` | Controller identifier referenced by devices. Filtered to an identifier; an empty id is auto-assigned the first free letter `A`–`Z`. |
| `address` | Hostname/IP for socket types, COM port for serial types. **No `host:port` suffix** — see below. |
| `baudrate` | Serial types only; normalised by `SerialInterface.formatSettingString` (e.g. `9600N81`). Ignored for Ethernet. |
| `settings` | Semicolon-separated options. See next. |

### The `settings:` field — the useful and undocumented part
Split on `;`, each element trimmed, then interpreted:

| Element | Handled by | Effect |
| :--- | :--- | :--- |
| `port:<N>` | `SharedInterface.getPort()` | TCP port for socket interfaces. **Defaults to 1234** when absent. Honoured by `PrologixEthernet`, `AR488Lan` and `Kofen`. |
| `++<command>` | `SharedInterfacePrologixUSB.init()` | Any element beginning with `++` is sent verbatim to the controller during interface init, immediately after `++auto 0` and `++mode 1`. Prologix types only. |
| anything else | — | Ignored silently. |

`init()` runs on **every** open, including after a menu Reconnect — which is what makes `++addr` here an effective workaround for the reconnect defect (Failure Mode #2).

Examples:
```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:|                        # plain, port 1234
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:port:1235|               # alternate port
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 5|                # force address at init
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:port:1235;++addr 5|      # both
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++read_tmo_ms 2000|      # any ++ command works
```

### `settingsLoad.txt` — device list
Two optional header lines, then one line per device:
```text
ScanSerialPorts:<0|1>
ExcludedSerialPorts:<COM1,COM7,...>
Device:<DriverName>|PortType:<Type>|Address:<ID>:<GPIB>|Baudrate:<rate>|Enabled:<0|1>
```

| Field | Meaning |
| :--- | :--- |
| `ScanSerialPorts` | Parsed with `Integer.parseInt`; non-zero enables probing of host serial ports at startup. A non-numeric value raises an uncaught `NumberFormatException` — keep it `0` or `1`. |
| `ExcludedSerialPorts` | Comma-separated port names skipped during that scan; compared case-insensitively after trimming. |
| `Device` | **Must exactly match a `#name` in a `Devices/*.txt` definition.** An unknown name aborts loading of every *subsequent* device (see the defect note below) — this is the single most damaging syntax error in the file. |
| `PortType` | `GPIB` for anything reached through a shared interface, including `PrologixEthernet`. |
| `Address` | For GPIB: `<controller id>:<gpib address>`. Parsed as an integer after the colon, clamped to 0–255, then clamped again to a maximum of 30 by `setLocalAddress` for non-Modbus interfaces. |
| `Baudrate` | Serial devices only; ignored for GPIB, though the field must still be present. |
| `Enabled` | `1` loads the device, `0` skips it. |

### Discovering valid driver names
Driver names come from the `#name` lines inside `Devices/*.txt`. To list every name your installation accepts:
```bash
grep -rhi "^#name" Devices/ | sed 's/^#name[[:space:]]*//' | sort -u
```
A stock 1.8.x install exposes roughly 1,000 names. Note that similar-sounding names are *not* interchangeable: `Tektronix TDS2024` does not exist (the TDS entries are `TDS3012C`-style), while `Fluke PM6690`, `Keithley 2001M` and `HP E3633A` do.

---

## 📌 Address Syntax Note — CORRECTED 2026-08-08

> **Previous claim (WRONG):** *"Dedicated per-port listening schemes are incompatible with TestController."*

The `address:` field indeed does not accept a `host:port` suffix — but **the `settings:` field does accept a port**, so per-port schemes *are* supported. Verified by decompiling `TestController.jar` (CFR 0.152):

```java
// dk.hkj.shared.SharedInterface
protected int getPort() {
    for (String s : this.settings.split("[;]"))
        if (s.trim().toLowerCase().startsWith("port:"))
            return Integer.parseInt(s.substring(5).trim());
    return 1234;                               // default only
}
// dk.hkj.shared.SharedInterfacePrologixEthernet
public String neededCommInterface() {
    this.ci = new SocketInterface(this.address, this.getPort());   // <-- honours getPort()
    ...
}
```

**Usable syntax:** `PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:port:1235|`

This matters architecturally: per-port listeners give every controller a distinct, **routable** identity that works for **remote** TestController hosts — unlike the loopback-alias scheme (Option A), which is local-only. VMSG can bind one listener per instrument and map port→slot deterministically, making the address unambiguous even when a client never sends `++addr`.

---

## ✅ Working Fix for Reconnect: Force `++addr` via the `settings:` Field

The same `settings:` field also injects arbitrary controller commands at init time:

```java
// dk.hkj.shared.SharedInterfacePrologixUSB
public void init() {
    this.writeWithDelay("++auto 0");
    this.writeWithDelay("++mode 1");
    for (String s : this.settings.split("[;]"))
        if (s.trim().startsWith("++")) this.writeWithDelay(s);   // <-- any ++ command
}
```

`init()` runs on **every** open, including reconnects (that is why `++auto 0`/`++mode 1` reappear after a menu Reconnect). Putting the device's address in `settings:` therefore re-asserts it on every reconnect, closing the gap left by TC's cached `selectedLocalAddress`:

```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 1|
PrologixEthernet|id:B|address:127.0.0.1|baudrate:|settings:++addr 2|
PrologixEthernet|id:C|address:127.0.0.1|baudrate:|settings:++addr 3|
PrologixEthernet|id:D|address:127.0.0.1|baudrate:|settings:++addr 4|
PrologixEthernet|id:E|address:127.0.0.1|baudrate:|settings:++addr 5|
```

Verified on a 5-instrument bench 2026-08-08 — cold start now emits `++auto 0` → `++mode 1` → `++addr N` per controller, and all five devices identify. **Requires no change to VMSG and no change to TestController.** Combine with `port:` using `;` (e.g. `settings:port:1235;++addr 5`).

---

## 🔬 Root Cause: Single-Controller Startup Failure (source-level)

*Reproduced 2026-08-08 with all 5 devices on controller `A`; VMSG recorded **exactly one** TCP connection, and four device threads died in ~1 ms without any network I/O. The original "thread manager marks threads unserviceable due to socket lock contention" hypothesis is **incorrect**.*

The actual defect is a **use-after-replace on the shared comm-interface field**:

1. `InterfaceThreads.addDevicesShared()` iterates the configured devices and, for each one, calls `shi.neededCommInterface()`.
2. For `PrologixEthernet` that method is **not idempotent** — every call assigns a brand-new, unopened `SocketInterface` to the shared `this.ci` field, discarding the previous one *even if a thread has already opened it*.
3. Device threads are started inside that same loop, so a thread that has already opened socket *#k* is left pointing at unopened socket *#k+1*.
4. `SharedInterface.open()` then early-outs, because it only acts when `openStatus()` is false — and it never marks `isOpen` for the second and later devices:

```java
public synchronized void open(int localAddress) {
    if (this.ci == null) return;
    if (!this.openStatus()) {                 // false for every device after the first
        this.ci.open();
        this.init();
        this.getDeviceSettings(localAddress).isOpen = true;   // never reached for the rest
    }
}
```

5. `DeviceThread.initDevice()` checks `cPort.isOpen()` → `ci.isOpen()` → false on the replaced socket → returns false → *"Stopping thread for: …"*.

**Observed signature matches exactly:** four threads fail in ~1 ms (they never opened anything), while the one thread that *did* open a socket fails ~900 ms later (200 ms settle + `*IDN?` + 700 ms retry) because its writes go to the replaced, unopened socket — which is also why VMSG sees a single connection and no `*IDN?`.

---

## ✅ VERIFIED PATCH — Four Defects, Built And Tested On The Bench (2026-08-08)

The diagnosis above was confirmed by **patching `TestController.jar` and running it against real instruments**. With the four changes below, **all five instruments start and run on a single socket under a single controller ID** — the configuration the per-device workaround exists to avoid.

```text
Found Fluke PM6690    on PrologixEthernet A:1 sn: 979819
Found Agilent 34411A  on PrologixEthernet A:2 sn: MY48005929
Found Keithley 2010   on PrologixEthernet A:3 sn: 636735
Found Keithley 2001M  on PrologixEthernet A:4 sn: 1150952
Found Keithley 2002   on PrologixEthernet A:5 sn: 4461274

30 s soak, one socket: 1721 log lines, 0 anomalies, 85-87 receives per device
```

**Menu Reconnect is fixed too.** Re-tested on per-controller IDs with **empty** `settings:` fields — the exact configuration that lost four of five devices before the patch. Every thread now re-addresses on reconnect and all five recover:

```text
20:02:38.453  ++auto 0 / ++mode 1   x5      (interface init on all five threads)
20:02:38.656  ++addr 4 ++addr 3 ++addr 1 ++addr 5 ++addr 2     <-- restored by the Defect 4 fix
Found Agilent 34411A / Fluke PM6690 / Keithley 2001M / Keithley 2010 / Keithley 2002
```
VMSG's side agrees: 5 new connections, 5 `++addr` commands, five distinct session addresses, zero warnings. Before the patch this same click produced four `do not match` errors against the slot-1 instrument and four stopped threads.

### Defect 1 — `SharedInterfacePrologixEthernet.neededCommInterface()` is not idempotent
```java
public String neededCommInterface() {
    if (this.ci == null) {                       // ADDED: keep the socket already in use
        this.ci = new SocketInterface(this.address, this.getPort());
        this.ci.debugLog = InterfaceThreads.debugAll;
    }
    return null;
}
```
`reset()` still nulls `ci`, so a Reconnect gets a fresh socket. This alone turned "four threads dead with no network traffic" into "all five threads talking".

### Defect 2 — `SharedInterface.writeRead()` is not atomic
`write()` and `read()` were individually `synchronized`, but the write-then-read pair was not, so on a shared interface one thread's `flush()` can consume another's in-flight reply, and an interleaved write can re-address the bus between a thread's write and its read.
```java
public synchronized String writeRead(int localAddress, String msg, int timeout) {   // ADDED synchronized
```
**Evidence:** with only Defect 1 fixed, a thread received `"EITHLEY INSTRUMENTS INC.,MODEL 2002,…"` — the 2002's IDN with the leading `K` eaten by another thread's flush. Invisible until Defect 1 let threads actually share the socket.

### Defect 3 — `SharedInterface.open()` loses per-device open state
`isOpen` was only recorded for the device that physically opened the interface, so `close()`'s `openStatus()` reference counting is wrong and closing one device tears down the socket the others are still using.
```java
        this.init();
    }
    this.getDeviceSettings((int)localAddress).isOpen = true;   // MOVED outside if (!openStatus())
}
```

### Defect 4 — `SharedInterfacePrologixUSB` keeps a stale address cache across reconnects
`selectedLocalAddress` / `selectedTimeout` record what the adapter was last told, but are only initialised at construction — never on (re)connect. `init()` does run on every open, yet the stale cache survives it, so `setActualAddress()` skips `++addr` whenever the wanted address happens to equal the pre-disconnect value while the freshly opened controller sits at *its* default.
```java
public void init() {
    this.selectedLocalAddress = -1;              // ADDED: new connection, unknown state
    this.selectedTimeout = -1;                   // ADDED
    this.writeWithDelay("++auto 0");
    this.writeWithDelay("++mode 1");
    ...
```
**This is the true root cause of Failure Mode #2 below.** It is correct for physical adapters too: an adapter that lost power also lost its runtime address. With this fix the `settings:++addr` workaround becomes unnecessary.

### Re-applying the patch to a future TestController release

`tools/patch_testcontroller.py` automates all of this. It decompiles the affected classes out of **your** jar (rather than shipping stale source), rewrites the four methods, recompiles against that same jar, and writes `TestController-patched.jar` alongside the original, which is never modified.

```bash
python tools/patch_testcontroller.py --fetch-tools     # one time: downloads CFR + ECJ
python tools/patch_testcontroller.py                   # patch (auto-locates the jar)
python tools/patch_testcontroller.py --check           # report only, writes nothing
```

Every edit is anchored to a distinctive pattern and verified. If a future release changes one of these methods enough that an anchor no longer matches, the script **stops and names the failing patch** instead of writing a partly-patched jar — so a silent half-patch is not possible. It also detects the good news case: if a fix has been adopted upstream, that patch reports *"already applied upstream - skipped"*. It refuses to run on a signed jar.

### Reproducing the patch build by hand
The jar is **unsigned** (`Main-Class: dk.hkj.main.Main`, no `.SF`/`.RSA`), and the affected classes decompile with CFR 0.152 into source that recompiles **without edits**. No JDK is required — the Eclipse batch compiler runs on a plain JRE 8:
```bash
java -jar cfr-0.152.jar TestController.jar --outputdir src        # decompile the 3 classes
java -jar ecj-4.6.1.jar -source 1.8 -target 1.8 \
     -cp TestController.jar -d out  patch/dk/hkj/shared/*.java    # recompile patched sources
# then replace the 4 .class entries in a copy of the jar
```



---

## 🐞 Related Defect: One Unknown Driver Name Silently Drops All Remaining Devices

```java
// InterfaceThreads.addDevicesShared()
DeviceInterface di = InterfaceThreads.findDeviceInterfaceFromDeviceDefinition(def);
if (di == null) break;        // <-- aborts the whole loop; should be `continue`
```

If any `settingsLoad.txt` entry names a device with no matching driver, **every device after it is silently skipped** — no message, no GUI entry. Observed directly: a 13-device export containing `Device:63004-150-60` and `Device:Offline Instrument` (no TC drivers) caused TC to load **zero** GPIB devices and fall back to endless serial rescans. `addDevicesSerial()` and `addDevicesSocket()` contain the same `break`.

**Consequence for VMSG:** the `/api/testcontroller/config` exporter must never emit a device name that is not a real TC driver — an unmapped instrument's raw description in that field breaks the entire configuration, not just its own line.

---

## 🛑 Observed Failure Mode #2: Menu "Reconnect" Loses All But One Device

*Diagnosed 2026-08-08 against a live 5-instrument bench (PM6690, 34411A, Keithley 2010/2001M/2002) with TestController running in `debugTime` mode and VMSG at DEBUG logging. Both wire-side traces captured.*

> **Root cause identified and patched — see Defect 4 above.** The omission described below is not a deliberate optimisation but a stale cache: `selectedLocalAddress` survives the disconnect, so `setActualAddress()` believes the controller is already on the right address. Resetting it in `init()` fixes this class of failure in every mode.

### Root Cause: TC Omits `++addr` on Reconnect

TestController's `SharedInterfacePrologixUSB` caches the adapter's current GPIB address in the Java interface object and only emits `++addr` when it believes the address needs *changing*. This is correct for physical Prologix adapters, where the address is **adapter state** (persisted in the adapter, surviving TCP reconnects). It breaks against VMSG, which models the address as **connection state** seeded from a single stored default.

**Cold start (works)** — every device thread sends the full init:
```text
++auto 0  →  ++mode 1  →  ++addr <slot>  →  *IDN?  →  ++read_tmo_ms 3000  →  ++read eoi
```

**Menu Reconnect (fails)** — every device thread re-sends `++auto 0` and `++mode 1` but **skips `++addr`** and goes straight to `*IDN?` + `++read eoi`. Every fresh VMSG session initializes to the stored default address (slot 1), so *all five* IDN probes are answered by the slot-1 instrument:

```text
PrologixEthernet C:3 Device "KEITHLEY...MODEL 2010,"  do not match answer: "FLUKE, PM6690, ..."
PrologixEthernet E:5 Device "KEITHLEY...MODEL 2002,"  do not match answer: "FLUKE, PM6690, ..."
PrologixEthernet D:4 Device "KEITHLEY...MODEL 2001M," do not match answer: "FLUKE, PM6690, ..."
PrologixEthernet B:2 Device "Agilent...34411A,"       do not match answer: "FLUKE, PM6690, ..."
Found Fluke PM6690 on PrologixEthernet A:1
Stopping thread for: PrologixEthernet C:3 / E:5 / D:4 / B:2
```

Only the device mapped at the default slot survives; TC stops the other threads. VMSG logs no errors — the protocol exchange is "valid," just mis-routed.

A secondary TC-side defect was captured during the same reconnect: the device-close phase throws `java.lang.NullPointerException at dk.hkj.shared.SharedInterface.writeWithDelay(SharedInterface.java:116)` while sending close commands (`*RST`/`*CLS`) on an interface that has already been torn down.

### The Correct Fix Is TC-Side
Since TC already re-sends `++auto 0` / `++mode 1` on reconnect, also re-sending `++addr` would be symmetric, cost-free, and correct even for physical adapters (an adapter that lost power also lost its runtime address). Both this and the close-phase NPE above should be reported to the TestController developer.

### VMSG-Side Mitigation Options (Analysis)

**Option A — Loopback alias per controller (local deployments only).**
The entire `127.0.0.0/8` block is loopback, so `127.0.0.2`, `127.0.0.3`, … all reach a VMSG listener bound to `0.0.0.0:1234`, each with a distinct *destination IP* that VMSG can read per-connection (`writer.get_extra_info('sockname')`). The exporter would emit `address:127.0.0.2` for controller A, `127.0.0.3` for B, etc. (TC's address parser accepts plain IPs), and VMSG would key persistent addr state — or a direct IP→slot mapping — off the destination IP. This makes each controller a true virtual adapter with its own identity, and reconnects route correctly with no `++addr` at all.
**Limitation:** only works when TC and VMSG share a host. A remote TC would require one routable IP per instrument on the VMSG host, which does not scale. Documented for local use; not a general solution.

**Option B — Per-port listeners.** Blocked: TC's `address:` field does not accept `host:port` (see Address Syntax Limitation above). Would otherwise be the clean remote-capable scheme (`1234`, `1235`, …). Becomes viable only if TC adds a port field.

**Option C — Orphaned-address affinity (time-windowed "last addr" adoption).**
VMSG tracks each session's current addr. When a session closes, its addr goes into an orphan pool with a timestamp. When a *new* session issues instrument traffic before any `++addr`, and **exactly one** orphan exists within the window (~10 s), the session adopts it.
- **Deterministically correct** for the common single-drop reconnects: instrument power-cycle, network blip, one device stopped/started in TC.
- **Cannot disambiguate the all-at-once menu Reconnect**: packet-level capture shows the five old sockets close over ~125 ms and the five new sockets open ~1 ms apart, with **no correlation between close order and open order** (TC thread scheduling shuffles both). With five orphans in the window, any assignment is a guess; a random permutation averages ~1 correct match — no better than today's deterministic "default slot survives," and nondeterministic behavior is harder to debug. Rule: **adopt only when unambiguous, otherwise fall back to the stored default.**
- Combined with per-device reconnects, this may make the mass-Reconnect recoverable one device at a time (each individual retry is a single-orphan event). Needs verification of whether TC's per-device reconnect also skips `++addr`.

**Option D — Architecture-level rethink (if remote TC support becomes a requirement).** Candidates: a VMSG-specific identifying handshake (requires TC cooperation, same as the TC-side fix); or accepting the constraint and documenting that remote TC deployments must treat menu Reconnect as "restart TC" (cold start always works).

---

## 📌 Status Note for TestController Developers
> **Note**: Clarification has been requested from the TestController software developer regarding single-controller multi-threading lock behavior on startup. This behavior may be updated or refined in a future release of TestController. Until then, use per-device controller IDs (`A`, `B`, `C`...) for multi-instrument setups.

**Summary for the developer (2026-08-08).** Five items, four of them patched and verified against real instruments (see the VERIFIED PATCH section above for exact diffs and evidence):

| # | Class / method | Defect | Effect |
| :-- | :--- | :--- | :--- |
| 1 | `SharedInterfacePrologixEthernet.neededCommInterface()` | Not idempotent; replaces the shared `ci` on every call | Single controller ID: all but one device thread stopped at startup, no network traffic |
| 2 | `SharedInterface.writeRead()` | Write-then-read pair not `synchronized` | Threads sharing an interface consume each other's replies (observed: IDN missing its first character) |
| 3 | `SharedInterface.open()` | `isOpen` recorded only for the first device | `close()` reference counting wrong; closing one device drops the socket under the others |
| 4 | `SharedInterfacePrologixUSB.init()` | `selectedLocalAddress` / `selectedTimeout` not reset on (re)connect | Menu Reconnect skips `++addr`; devices address the wrong instrument and are stopped on IDN mismatch |
| 5 | `InterfaceThreads.addDevicesShared()` (also `…Serial`, `…Socket`) | `break` instead of `continue` when a driver name is unknown | One unrecognised device name silently drops **every remaining device**; no message to the user |

Also worth a look, not patched: a `NullPointerException` in `SharedInterface.writeWithDelay` (line ~116) during the device-close phase of Reconnect, when close commands (`*RST`/`*CLS`) are sent on an already torn-down interface.

With #1–#4 applied, a **single** controller ID serves all instruments correctly, which removes the need for the per-device `A`/`B`/`C` workaround entirely.

