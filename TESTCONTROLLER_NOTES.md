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

## 📌 Status Note for TestController Developers
> **Note**: Clarification has been requested from the TestController software developer regarding single-controller multi-threading lock behavior on startup. This behavior may be updated or refined in a future release of TestController. Until then, use per-device controller IDs (`A`, `B`, `C`...) for multi-instrument setups.
