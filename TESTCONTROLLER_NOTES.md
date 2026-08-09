# TestController Integration Notes

**TestController is an excellent tool and is the reason VMSG exists.** This document records behaviors we have observed when using TestController with VMSG to multiplex multiple instruments over a Prologix Ethernet gateway, and the configurations that make multi-instrument setups reliable. Issues discussed here have been reported to the TestController developer for their review.

---

## Observed Behavior With Prologix Ethernet

When configuring multiple instruments in TestController connected via a Prologix Ethernet adapter (such as VMSG), we have observed two consistent issues:

**At startup** — If all instruments are assigned to the same Controller Instance ID (for example, all set to Controller `A`), most device threads fail to initialize. Typically only one or two instruments load successfully; the rest stop silently without attempting network communication.

**On menu Reconnect** — If instruments are configured with separate Controller IDs (`A`, `B`, `C`, etc.) but without explicit address configuration, pressing "Reconnect" causes all but one instrument to report identity mismatches and stop. The instrument at slot 1 (the default) continues working while the others are dropped.

---

## Recommended Configuration Workaround

**Option 1: Per-Device Controller IDs (Most Reliable)**

Configure a separate Controller Instance ID for each instrument. This is the most straightforward approach and eliminates startup issues entirely:

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

Each device thread opens its own TCP socket connection to VMSG, eliminating contention and ensuring reliable cold starts and reconnects. This is the approach VMSG's auto-assign feature uses by default.

---

**Option 2: Single Controller with Forced Addressing**

If you prefer a single Controller Instance ID, force the address on every reconnect using the `++addr` command:

```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 1|
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 2|
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 3|
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 4|
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 5|
```

Each device line specifies its own GPIB address in the `settings:` field. Since TestController re-initializes the controller on reconnect, the explicit `++addr` ensures the address is re-asserted even after a connection loss. This approach has been verified to work reliably in practice.

---

## TestController Configuration File Reference

*This reference documents the configuration syntax for `settingsGPIB.txt` and `settingsLoad.txt` as TestController accepts it. Files live in TestController's `Settings` folder.*

### Common line syntax
Both config files are parsed line by line. A line is ignored unless it contains `|`, and a line beginning with `;` is a comment. Fields are `name:value` pairs separated by `|`; both name and value are trimmed, so alignment spaces are harmless. A value may itself contain `:` — only the *first* colon splits (that is why `settings:port:1235` works).

### `settingsGPIB.txt` — adapter definitions
```text
<Type>|id:<ID>|address:<HOST or PORT>|baudrate:<SETTINGS>|settings:<OPTIONS>|
```

| Field | Meaning |
| :--- | :--- |
| `<Type>` | Interface class matched by line prefix. Recognised: `PrologixEthernet`, `PrologixUSB`, `AR488Lan`, `AR488`, `Kofen`, `NSGPIB232CT`, `KeysightE5810`, `Modbus`, `SharedInterface`. **Order matters:** `PrologixUSB` is tested before `PrologixEthernet`, so the prefix must match exactly. |
| `id` | Controller identifier referenced by devices. Filtered to a letter A–Z; empty id is auto-assigned the first free letter. |
| `address` | Hostname/IP for socket types, COM port for serial types. |
| `baudrate` | Serial types only. Ignored for Ethernet. |
| `settings` | Semicolon-separated options (see below). |

### The `settings:` field — discovered configuration options

The `settings:` field accepts semicolon-separated options that we've discovered but haven't found complete documentation for:

| Option | Effect |
| :--- | :--- |
| `port:<N>` | TCP port for socket interfaces. **Defaults to 1234** if absent. Supported by `PrologixEthernet`, `AR488Lan`, and `Kofen`. Example: `settings:port:1235` |
| `++<command>` | Any option starting with `++` is sent to the controller during initialization. Multiple commands can be chained with `;`. Example: `settings:++addr 5;++read_tmo_ms 2000` |

Examples:
```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:|                        # default, port 1234
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:port:1235|               # alternate port
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 5|                # force address
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:port:1235;++addr 5|      # both options
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
| `ScanSerialPorts` | Non-zero enables host serial port discovery at startup. Keep it `0` or `1`. |
| `ExcludedSerialPorts` | Comma-separated port names to skip during discovery; case-insensitive. |
| `Device` | Must exactly match a `#name` in `Devices/*.txt`. An unrecognised name prevents subsequent devices from loading. |
| `PortType` | Use `GPIB` for instruments through a shared interface, including `PrologixEthernet`. |
| `Address` | For GPIB: `<controller-id>:<gpib-address>`. Address is clamped to 0–30. |
| `Baudrate` | Serial devices only; ignored for GPIB but the field must be present. |
| `Enabled` | `1` loads the device, `0` skips it. |

### Discovering valid driver names
Driver names come from `#name` lines in `Devices/*.txt`. To list all available names:
```bash
grep -rhi "^#name" Devices/ | sed 's/^#name[[:space:]]*//' | sort -u
```

A stock 1.8.x installation has roughly 1,000 driver names. Note that similar-sounding names are **not** interchangeable: `Tektronix TDS2024` doesn't exist (the TDS entries are `TDS3012C`-style), while `Fluke PM6690`, `Keithley 2001M`, and `HP E3633A` do exist.

---

## Developer Communication

Issues observed during this integration have been reported to the TestController developer:

1. **Startup issue with single Controller ID** — Multiple device threads fail to initialize when assigned to the same controller.
2. **Reconnect address caching** — Instrument addresses are not re-asserted when reconnecting, causing devices to query the wrong instrument.
3. **Configuration validation** — An unrecognised device name silently prevents subsequent devices from loading.

The TestController developer's response to these reports will inform future VMSG improvements. In the meantime, the configuration approaches documented above provide reliable operation.

