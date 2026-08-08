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

**Proposed fix (2 lines, `SharedInterfacePrologixEthernet`):**
```java
public String neededCommInterface() {
    if (this.ci == null) {                    // make idempotent
        this.ci = new SocketInterface(this.address, this.getPort());
        this.ci.debugLog = InterfaceThreads.debugAll;
    }
    return null;
}
```
plus moving `getDeviceSettings(localAddress).isOpen = true;` in `SharedInterface.open()` outside the `if (!openStatus())` block so per-device open state is tracked correctly (it currently also makes `close()`'s `openStatus()` bookkeeping wrong).

With this fixed, a single controller ID could serve all instruments, and because TC re-sends `++addr` before every transaction when multiplexing, **the reconnect defect (Failure Mode #2) would not arise either**.

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
> **New (2026-08-08)**: Two additional items to raise: (1) device threads re-send `++auto 0`/`++mode 1` after a menu Reconnect but omit `++addr`, which breaks any adapter whose address state did not survive (see Failure Mode #2); (2) a `NullPointerException` in `SharedInterface.writeWithDelay` during the device-close phase of Reconnect.

