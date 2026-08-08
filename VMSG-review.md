# VMSG Code Review — Optimizations and Alternative Approaches

**Repo:** `Cyclotronic/VMSG` @ `main` (1 commit)
**Scope reviewed:** `vmsg.py`, `vmsg_core/*.py`, `static/index.html` (structure only), `build_binary.py`, CI workflow, `test_emulator.py`
**Date:** 2026-08-08

---

## 1. Verdict

The architecture is sound and the problem framing is correct. Emulating a Prologix Ethernet controller in front of PyVISA is the right way to get VISA-only instruments into TestController, and the per-client session model plus resource pooling are the right primitives.

The issues below are almost all in the *implementation* of that architecture rather than the architecture itself. Three of them are serious enough that I'd fix them before doing anything else:

1. **A full `mappings.json` disk write occurs on every SCPI query** — synchronously, on the asyncio event loop thread.
2. **Binary data is corrupted end-to-end.** Arb waveform uploads to the 33250A and binary `CURVE?`/buffer dumps cannot work through VMSG today.
3. **A single global lock serializes all hardware I/O across all interfaces**, and it is acquired on the event loop in error paths, freezing every other client for up to the timeout period.

Everything else is refinement.

---

## 2. What the design gets right

Worth stating explicitly, since the rest of this document is critical:

- **Per-client session state** (`client_sessions` keyed by peer tuple) is the correct model, and it's the thing that makes the documented A/B/C/D/E TestController workaround work at all. Most naive Prologix emulators keep one global `++addr` and fall apart with concurrent clients.
- **Resource pooling with persistent PyVISA sessions** is right. Open/close per transaction on a GPIB board is genuinely slow and can leave the bus in odd states.
- **Unresponsive-address cooldown caching** (`unresponsive_cache`, 120 s) is a good instinct — scanning a bus with dead addresses is the classic way to hang a UI thread.
- **`is_scannable_resource()`** filtering out `::INTFC`, `::RAW`, and secondary GPIB addresses avoids a real and painful timeout cascade.
- **`TESTCONTROLLER_NOTES.md`** is genuinely useful diagnostic writing — the Java `SharedInterfacePrologixUSB` thread-shutdown analysis is the kind of thing that saves someone else a weekend.
- **Single event loop hosting both uvicorn and the socket server** avoids a whole class of cross-thread bugs. Correct call.

---

## 3. Critical issues

### 3.1 Disk write on every query — `prologix_server.py:28`, `config_manager.py:128`

`set_client_setting()` calls `self.config.update_setting(key, value)` on every invocation. `update_settings()` unconditionally calls `_save_config_unlocked()`, which does a full `json.dump()` to disk.

The hot path is:

```python
# prologix_server.py:409 — after every command containing "?"
if "?" in command:
    self.set_client_setting(client_addr, "last_query_addr", curr_addr)

# prologix_server.py:433 — and again to clear it
self.set_client_setting(client_addr, "last_query_addr", None)
```

`"last_query_addr"` is not in `DEFAULT_CONFIG["settings"]`, so `update_settings()` skips the assignment — but still falls through to `_save_config_unlocked()` and `logger.configure()`. **Two full config file writes per query, both blocking, both on the event loop thread.** A five-instrument TestController setup polling at a few Hz is writing that file dozens of times per second and stalling every other socket while it does.

There's a second problem layered on top: `set_client_setting` deliberately mutates *global* config so the dashboard can display it. That means client A issuing `++addr 5` changes the default inherited by every subsequently connecting client, and persists it. Per-client isolation is undermined by the very function meant to implement it.

**Fix:**

```python
# Session-only keys never touch the config store
_SESSION_ONLY = {"last_query_addr"}

def set_client_setting(self, client_addr, key, value):
    if client_addr not in self.client_sessions:
        self.client_sessions[client_addr] = self.config.get_settings().copy()
    self.client_sessions[client_addr][key] = value
    if key in _SESSION_ONLY:
        return
    # For dashboard display only — in-memory, not persisted
    self.config.set_runtime_setting(key, value)
```

Add a `set_runtime_setting()` that updates the in-memory dict without a save, and either debounce persistence (dirty flag + 5 s flush task) or only persist on explicit dashboard action. Prologix hardware itself only writes to EEPROM when `savecfg` is 1 — honoring that flag would be both faster and more faithful.

### 3.2 Binary data is corrupted — `prologix_server.py:117`, `:480`

Three separate places destroy 8-bit data:

```python
buffer += data.decode('utf-8', errors='replace')   # line 117 — inbound corruption
...
response = response.strip()                        # line 480 — outbound corruption
```

plus `res.write()` / `res.read()` are the string-mode PyVISA calls, which apply encoding on both sides.

Consequences for your bench specifically:

- **33250A arb upload.** `DATA:DAC VOLATILE, #800004000<4000 bytes>` — any byte ≥ 0x80 becomes U+FFFD, and any 0x0A inside the payload splits the "line". Arb download through VMSG is not possible today.
- **Tek `CURVE?` and Keithley buffer dumps** in `FORMAT:DATA REAL` — same failure outbound, plus `.strip()` eats leading/trailing bytes that are part of the data.
- Even in ASCII mode, `.strip()` removing leading whitespace is a subtle deviation from what a Prologix passes through.

**Fix:** go byte-oriented throughout. Keep `bytearray` buffers on the socket side, split on `b"\n"`, and use `write_raw()` / `read_raw()` against PyVISA. Decode to `str` only for logging (`repr()` on a truncated slice). This is a moderate refactor but it's the difference between "works for `*IDN?`" and "works for real instrument work."

A halfway measure if you want to stage it: keep the text path as-is but add a length-prefixed passthrough mode triggered by detecting `#` block headers. I'd recommend just doing the full byte refactor — it's cleaner than the special case.

### 3.3 Global lock held on the event loop — `visa_manager.py:94`, `prologix_server.py:400/414/470`

`global_visa_lock` is a `threading.Lock` serializing all hardware I/O. Two problems.

**(a) It's too coarse.** A USB-TMC 34411A, a LAN scope, and a GPIB board share no bus, but a 10-NPLC Keithley integration holding the lock for 200 ms stalls all of them. The comment says "hardware bus serialization" — that intent is right, but the granularity is wrong. Lock per *interface*, not globally:

```python
def _interface_key(visa_addr: str) -> str:
    # "GPIB0::7::INSTR" -> "GPIB0"; "TCPIP0::1.2.3.4::inst0::INSTR" -> "TCPIP0"
    return visa_addr.split("::", 1)[0].upper()

def get_interface_lock(self, visa_addr):
    key = _interface_key(visa_addr)
    with self.lock:
        return self.interface_locks.setdefault(key, threading.Lock())
```

TCPIP and USB endpoints arguably need no interface lock at all — only the per-resource lock. GPIB and ASRL do.

**(b) It's acquired synchronously on the event loop.** Both error handlers do this:

```python
except Exception as e:
    ...
    with self.visa_manager.global_visa_lock:   # line 414, NOT in a thread
        with res_lock:
            res.clear()
```

`res.clear()` is a blocking VISA call and the lock acquisition can block for the full duration of another instrument's read. This runs directly in the coroutine, so the entire gateway — all clients, the web UI, everything — freezes. Every VISA touch must go through `asyncio.to_thread`, including recovery paths. Same bug in `perform_instrument_read`'s handler at line 493.

### 3.4 Query write+read is not atomic

`route_instrument_cmd` acquires locks for the write, releases, then `perform_instrument_read` acquires them again for the read. Between those two acquisitions another client can address and write to the same instrument. Two clients querying the same slot can receive each other's responses.

Your documented A–E workaround makes each TestController device its own socket *and* its own slot, so it mostly avoids this — but the dashboard SCPI console (`/api/send_command`) targets slots directly and can collide with TestController on the same instrument. That's a plausible everyday scenario: you're watching a Keithley in TestController and poke it from the web console.

**Fix:** for `auto == 1`, hold `res_lock` across the write and the read in a single `to_thread` call. For `auto == 0`, the write→`++read` pair spans two client messages, so the correct scope is a *session-owned* lease on the resource, released on `++read`, on `++addr` change, or on a timeout watchdog.

### 3.5 `DEFAULT_CONFIG.copy()` is a shallow copy — `config_manager.py:40, 64, 67`

```python
self.config = DEFAULT_CONFIG.copy()
```

`self.config["settings"] is DEFAULT_CONFIG["settings"]`. On a first run (or after a load failure), every subsequent `update_settings()` mutates the module-level defaults. The "merge missing keys from defaults" loop at line 57 then re-injects mutated values into any later-loaded config, and a reset-to-defaults restores whatever the last session happened to set. Same aliasing on `["mappings"]` via `set_mapping`.

`import copy; self.config = copy.deepcopy(DEFAULT_CONFIG)` — one-line fix, latent bug removed.

### 3.6 Config writes are not atomic — `config_manager.py:79`

`open(path, "w")` truncates before writing. Given the current write frequency (§3.1), the window for a crash or power loss to leave a zero-byte `mappings.json` is not theoretical. Standard fix:

```python
tmp = self.filepath + ".tmp"
with open(tmp, "w") as f:
    json.dump(self.config, f, indent=4)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, self.filepath)
```

### 3.7 Healing collapses duplicate models onto one address — `visa_manager.py:heal_mappings`

`idn_pattern` matching is a naive case-insensitive substring, and the loop `break`s on first match. `auto_assign_devices` sets `idn_pattern = description = idn.split(",")[1][:25]` — i.e. just the model number.

So two instruments of the same model (two 34401As, two 2000s) both match the same pattern, and healing will point *both* virtual slots at whichever the scan returned first. Silent, persistent, and it writes the wrong mapping to disk. On a bench that accumulates duplicates from surplus channels, this will eventually bite.

**Fix:** fingerprint on the serial number (IDN field index 2 for most IEEE-488.2 instruments), fall back to model only when no serial is available, and — importantly — **refuse to heal when a pattern matches more than one online device**, logging a warning instead. Ambiguity should fail loudly, not pick arbitrarily.

---

## 4. Protocol fidelity gaps

These matter because clients are written against real Prologix behavior.

| Gap | Location | Impact |
|---|---|---|
| Line splitting only on `\n` | `prologix_server.py:120` | A client sending bare `\r` never dispatches; buffer grows without bound. Prologix accepts CR, LF, or CR+LF. Split on both, and cap buffer length. |
| Unknown `++cmd` returns an error *string* | `:337` | Real Prologix ignores unrecognized commands. Injecting `Error: Unknown command ++clr\r\n` into the stream desynchronizes the client, which reads it as instrument data on its next read. **Silently ignore instead.** |
| All the other `Error: ...` returns | `:361, :372, :444, :455` | Same desync hazard. A Prologix returns *nothing* on an unmapped/absent device — or just the EOT char. `unmapped_behavior: "timeout"` gets closer but still returns `Error: VI_ERROR_TMO...` as text after sleeping. Return the bare terminator. I'd make timeout-mode the default. |
| Missing `++clr`, `++trg`, `++llo`, `++loc`, `++ifc`, `++srq`, `++status` | `execute_prologix_cmd` | `++clr` → `res.clear()` (SDC), `++trg` → `res.assert_trigger()` (GET), `++srq` → poll SRQ line. These are used by real clients; `++trg` in particular for synchronized multi-instrument measurements. All map cleanly onto PyVISA. |
| `++addr` rejects secondary addresses | `:176` | `++addr 5 96` (PAD + SAD) fails `int()`. Rare, but trivial to parse. |
| `++read eoi` / `++read <char>` args parsed then discarded | `:271` | Works by accident because VISA reads terminate on EOI/termchar. `++read <char>` semantics (read until specific byte) are genuinely unimplemented. |
| `++spoll` returns 0 on failure | `:520` | 0 is a valid STB meaning "no service requested." Masking a comms failure as a valid status byte will send a client into an infinite poll loop. Prologix times out; mirror that. |
| `read_stb` skips the interface lock | `:516` | Inconsistent with read/write paths — a serial poll can interleave with a data transfer on the same GPIB bus. |

**One deviation worth adding deliberately:** an opt-in "smart auto" mode where read-after-write only fires for commands containing `?`. Prologix's `auto 1` reads after *every* write, so each non-query command costs a full timeout. Faithful, but slow. Offering `++auto 2` as a VMSG extension would be a large real-world speedup for TestController setups while leaving `0` and `1` exactly Prologix-compatible.

---

## 5. Performance

### 5.1 The threading model is the wrong shape

Currently: `asyncio.to_thread` per operation, onto the default executor, with a global lock inside. Under load, threads pile up *blocked on the lock*, each holding an executor slot for up to `read_tmo_ms`. The default executor is `min(32, cpu+4)` — exhaustible, and exhaustion manifests as unexplained latency rather than an error.

**Better method: one dedicated worker thread per VISA resource, fed by a queue.**

```python
class ResourceWorker:
    def __init__(self, visa_addr, resource, interface_lock):
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    async def submit(self, op) -> Any:
        fut = asyncio.get_running_loop().create_future()
        self.q.put((op, fut, loop))
        return await fut
```

This gives you, for free: strict per-device ordering, natural serialization without a lock, a bounded thread count (one per open instrument), a place to hang per-device stats, and a clean home for the write+read atomicity from §3.4. It's essentially the per-controller I/O locking pattern from POS, applied per resource — so it should feel familiar rather than novel.

### 5.2 Dashboard polling — `index.html:2187-2188, 2059`

```javascript
setInterval(fetchState, 1500);
setInterval(fetchLogs,  800);
snoopTimer = setInterval(pollSnoopData, 1000);
```

`/api/logs` returns the **entire 1000-entry buffer** every 800 ms with no cursor. That's serializing and shipping the full ring buffer ~75 times a minute, and `logger.get_logs()` copies the list under the same lock the traffic path contends on.

**Fix, cheap:** add a monotonic sequence number per log entry and support `GET /api/logs?since=N`.
**Fix, better:** replace all three pollers with a single SSE stream (`/api/events`). One connection, push-driven, no wasted round trips, and the snoop view becomes a filter on the client rather than a separate endpoint. FastAPI does SSE in about 15 lines with an `async` generator; no new dependency needed.

### 5.3 Logger — `logger.py:61`

```python
self.logs.append(entry)
if len(self.logs) > self.max_logs:
    self.logs.pop(0)      # O(n) memmove on every log line past 1000
```

`collections.deque(maxlen=1000)` is O(1) and removes the manual trim entirely.

Separately, the hot path builds the f-string *before* the level check:

```python
logger.info("TRAFFIC_IN", f"[{client_addr[0]}:{client_addr[1]}] -> {line}")
```

Default level is WARN, so this is discarded — after paying for the interpolation, and in the read path after paying for `repr(response)` on a potentially large payload. Either guard with `if logger.enabled("INFO", "TRAFFIC_IN"):` or pass `(fmt, args)` and interpolate lazily.

### 5.4 Startup healing blocks the listeners — `vmsg.py:52`

Healing runs *before* `asyncio.create_task` for either server. `heal_mappings` calls `scan_all_hardware()` (300 ms `*IDN?` per port) and then queries `*IDN?` *again* at 1000 ms per mapping — despite already having those results in `scanned_devices`. On a bench with a dozen resources that's several seconds of dead air before port 1234 even opens, during which TestController's startup will fail to connect.

Two fixes: reuse the scan results instead of re-querying, and move healing into a background task started *after* the listeners are up.

### 5.5 Mock write sleep — `visa_manager.py:38`

`time.sleep(0.005)` inside `MockVisaResource.write()` caps mock throughput at 200 cmd/s and slows CI. Make it a constructor parameter defaulting to 0, set it non-zero only where you actually want to simulate latency.

---

## 6. Security

The gateway binds `0.0.0.0` on both ports with no authentication. On a typical bench LAN that may be an acceptable tradeoff, but the current combination is more exposed than it needs to be:

- **`POST /api/system/restart`** runs `subprocess.Popen([sys.executable] + sys.argv)` then `os._exit(0)`. **`POST /api/system/stop`** kills the process. Both unauthenticated, both reachable from any host on the LAN.
- **`CORSMiddleware(allow_origins=["*"], allow_credentials=True)`** — that combination is rejected by the CORS spec so browsers won't honor it, but the wildcard origin still means any web page you visit can issue no-preflight POSTs to `http://<bench-ip>:8080/api/system/restart` or `/api/send_command`. DNS rebinding makes `localhost` binding insufficient on its own.
- **`/api/send_command`** sends arbitrary SCPI to physical hardware. For a source-measure unit or a supply, that's a "set the output to 40 V" primitive exposed to the network.
- **`/api/config/restore`** assigns `data["settings"]` wholesale with no validation (`web_app.py`), bypassing the type/range checks in `update_settings`. A backup with `"read_tmo_ms": "abc"` will crash the socket path on the next command. Route it through `update_settings()` instead.

**Recommended baseline:** default bind to `127.0.0.1` with an explicit `--bind 0.0.0.0` opt-in; restrict CORS to same-origin (or drop the middleware, since the UI is served from the same origin and doesn't need it); add `Host` header validation against a allowlist to close rebinding; put the destructive endpoints behind a token read from config, generated on first run and printed to the console. And add a short README note — anyone running this on a lab network shared with other people should know what it exposes.

---

## 7. Code quality and maintainability

- **`get_writable_config_path()` uses `os.getcwd()`.** Launching the binary from a different directory silently starts with a fresh config and no warning. Use `platformdirs` (or hand-rolled `%APPDATA%` / `~/.config/vmsg`) with a CWD fallback for portable-mode use, and log the resolved path at startup.
- **Dead code:** `if not res_lock:` in `query_idn` is unreachable — `get_resource` always returns a lock. `assigned` in `auto_assign_devices` is set and never read.
- **`map_to_testcontroller_driver` matches against the VISA address string.** `combined = f"{idn} {description} {visa_address}".upper()` — USB resource strings contain hex VID/PID and serials that regularly contain digit sequences like `2000` or `34401`. Match against the IDN *model field* (`idn.split(",")[1]`) only. Also, `"TEKTRONIX" in combined → "Tektronix TDS2024"` maps *every* Tek instrument to a TDS2024 driver; that catch-all should return a generic instead.
- **Snoop filtering greps log message text** (`web_app.py`) with `f"address {address}" in msg.lower()` — `"address 1"` is a substring of `"address 10"`, so slot 1's snoop view shows slots 10–19's traffic. Tag log entries with structured fields (`slot`, `visa_address`, `client`) and filter on those.
- **`auto_assign_devices` never rewinds `slot_idx`**, so freed low slots can't be reused within one pass. Minor, but the loop would be clearer as "compute set of free slots, then zip with devices."
- **No `pyproject.toml`**, and `requirements.txt` uses `>=` with no upper bounds. A FastAPI or Pydantic major bump will break release builds with no warning. Pin a constraints file for the CI/binary builds even if the loose spec stays for source installs.
- **Testing.** `test_emulator.py` is a decent smoke test but requires a live server and uses `sleep(0.02)` for synchronization — it'll go flaky in CI eventually, and the workflow's `python vmsg.py & sleep 3` has no readiness check or timeout guard. The Prologix parser is pure logic and trivially unit-testable with no server and no hardware. I'd add:
  - `pytest` unit tests over `execute_prologix_cmd` with a fake config/VISA manager — covers EOS/EOI/terminator/`++addr` semantics in milliseconds.
  - API tests via `httpx.ASGITransport(app=app)` — no socket, no port, no sleep.
  - Keep `test_emulator.py` as an end-to-end test, but gate readiness on polling `/api/status` rather than `sleep 3`.
- **CI covers only x86-64 Windows and Linux.** Your LGI project already does four-platform CI including both macOS architectures; the same matrix applies here. Also worth adding SHA-256 checksums to release assets.

---

## 8. Alternative approaches worth evaluating

### 8.1 Replace hand-rolled mocks with `pyvisa-sim`

`MockVisaResource` is ~70 lines of hardcoded `if "MEAS:VOLT:DC?" in cmd` branches that will grow every time you want another simulated instrument. `pyvisa-sim` is a maintained PyVISA backend that defines instruments declaratively in YAML — channels, error states, coupled properties, per-command responses. You'd get a much richer simulated bench for less code, and you could ship simulation profiles matching your actual instruments (2001/2002/2010, 33250A) rather than generic stand-ins.

The tradeoff: it's a real dependency in the PyInstaller bundle, and the current mocks work with zero setup. Worth it if you expect the simulated-device list to grow past three.

### 8.2 Per-instrument listening ports

The entire `++addr` multiplexing layer — including the fragile `last_query_addr` state — exists because one socket serves 31 virtual addresses. If TestController's `PrologixEthernet|address:` field accepts `host:port` (worth a quick test; the docs I've seen only show a bare host), you could bind one port per instrument, each socket permanently bound to one resource. That removes `last_query_addr` entirely, makes write+read atomicity trivial, and eliminates the whole class of cross-talk bugs.

If the port field turns out to be fixed at 1234, a partial version still helps: **bind the session to its resource on first `++addr` and hold a lease** for the connection's lifetime, since TestController's per-device controller IDs mean each socket only ever talks to one instrument in practice.

### 8.3 Drop `last_query_addr` regardless

Independent of the above, this mechanism is a net negative as implemented. It only matters for `auto == 0` flows, where a client normally doesn't change `++addr` between the query write and the `++read`. Meanwhile it introduces a real hazard: if a query is written but never read — client crashes, TestController abandons the transaction — the value stays sticky, and a *later* `++read` at a different address silently reads the stale instrument. Real Prologix reads from whatever is currently addressed. Matching that is simpler and safer.

### 8.4 Discovery advertisement

VMSG could advertise itself over mDNS (`_prologix._tcp` or a custom service type). Your LGI tool already discovers E5810A gateways; teaching it to discover VMSG instances too would give you one inventory view across real and virtual gateways, and POS could pick it up as a scan target without manual host entry. Small addition, good fit with the existing toolchain.

---

## 9. Prioritized action list

**Do first — correctness and data integrity**

1. Remove config persistence from the socket hot path (§3.1). Biggest single win, smallest diff.
2. `copy.deepcopy(DEFAULT_CONFIG)` (§3.5) and atomic config writes (§3.6). Two-line fixes.
3. Move all VISA calls in error/recovery paths into `asyncio.to_thread` (§3.3b).
4. Serial-number fingerprinting for healing, and refuse ambiguous matches (§3.7).
5. Validate `/api/config/restore` through `update_settings` (§6).

**Do next — makes it usable for real instrument work**

6. Byte-oriented socket and VISA paths; drop `.strip()` and UTF-8 decode from the data path (§3.2).
7. Replace `global_visa_lock` with per-interface locks (§3.3a).
8. Atomic write+read for `auto == 1` (§3.4).
9. Silently ignore unknown `++` commands; return bare terminators instead of error text (§4).
10. Add `++clr`, `++trg`, `++srq`; split lines on CR as well as LF (§4).

**Then — performance and polish**

11. Per-resource worker threads replacing ad-hoc `to_thread` + global lock (§5.1).
12. SSE for logs/status/snoop; `deque` for the log buffer; lazy log formatting (§5.2, §5.3).
13. Move startup healing to a post-listen background task and reuse scan results (§5.4).
14. Bind localhost by default, tighten CORS, token-gate destructive endpoints (§6).
15. `pyproject.toml`, pinned constraints, pytest unit tests, macOS CI (§7).

Items 1–5 are roughly an evening. Item 6 is the one meaningful refactor, and it's the one that determines whether VMSG can carry real measurement traffic or stays a `*IDN?`-and-ASCII-readings tool.

---

## 10. Note on Returning `None` in Socket Responses

Returning `None` from command routing/reads when a slot is unmapped or connection fails results in the socket server sending zero bytes back to the client. This causes standard blocking TCP clients (including `test_emulator.py`) to block on `s.recv()` and hang indefinitely.

To prevent client-side hangs:
* **Unmapped Slots**: Reads must return `\r\n` (after blocking for `read_tmo_ms` if simulating a timeout) or return an error message (if `unmapped_behavior == "message"`).
* **Connection Failures**: An error response should be written back to the socket client to notify it of the failure rather than silently ignoring the request.

