# VMSG Review — Round 2

**Repo:** `Cyclotronic/VMSG` @ `8c8236c` ("Optimize locking, config writes, startup healing, and incremental log updates")
**Baseline:** `233ff07` (reviewed previously)
**Method:** full diff review plus a live instance run against the mock instruments

---

## 1. Scorecard against round 1

| # | Finding | Status |
|---|---|---|
| 3.1 | Disk write on every query | **Fixed** — `_SESSION_ONLY` guard + `set_runtime_setting()` |
| 3.2 | Binary corruption end-to-end | **Not fixed** — still UTF-8 decode + string-mode PyVISA |
| 3.3a | Global lock too coarse | **Fixed** — `get_interface_lock()` keyed on interface prefix |
| 3.3b | Blocking VISA calls on event loop in error paths | **Fixed** — recovery wrapped in `asyncio.to_thread` |
| 3.4 | Query write+read not atomic | **Not fixed** |
| 3.5 | Shallow `DEFAULT_CONFIG.copy()` | **Fixed** — `deepcopy` throughout, including getters |
| 3.6 | Non-atomic config writes | **Fixed** — tmp + `fsync` + `os.replace` |
| 3.7 | Duplicate-model healing collapse | **Partly fixed** — ambiguity now refused; serial fingerprinting still absent |
| §4 | Split on CR as well as LF | **Fixed** — `re.split(r'\r\n|\n|\r')`, correctly handles CRLF spanning TCP segments |
| §4 | Unknown `++cmd` returns error text | **Fixed** — now returns `None` |
| §4 | Error strings in data stream | **Mostly fixed** — two remain (see §2.4) |
| §4 | Missing `++clr` / `++trg` | **Fixed** |
| §4 | Missing `++llo` / `++loc` / `++ifc` | **Regressed** — accepted but behave incorrectly (see §2.2) |
| §4 | `++addr` secondary address | **Fixed** — `arg.split()[0]` |
| §4 | `++spoll` returns 0 on failure | Not fixed |
| §4 | `read_stb` skipped the bus lock | **Fixed** |
| §5.2 | Log polling ships whole buffer | **Fixed** — `?since=` cursor, client tracks `lastLogId` |
| §5.3 | `logs.pop(0)` O(n) | **Fixed** — `deque(maxlen=)` |
| §5.3 | Eager f-string on hot path | Not fixed |
| §5.4 | Startup healing blocks listeners | **Fixed** (move); redundant re-query remains |
| §5.5 | Mock write sleep | **Fixed** — `delay_s` param, defaults 0 |
| §6 | Restore bypasses validation | **Partly fixed** — now routes through `update_settings`, but see §3.1 |
| §6 | Bind, CORS, unauth restart | Not addressed |
| §7 | Driver map matches VISA address string | Not fixed |
| §7 | Snoop substring matching | Not fixed |
| §7 | `os.getcwd()` config path | Not fixed |
| §7 | Dead `if not res_lock:` branch | **Fixed** |
| §7 | pyproject / pinning / tests / macOS CI | Not addressed |

Good hit rate on the ones that mattered. The interface-lock and config-write changes are clean, and the `deepcopy` on the *getters* (not just the constructor) is a nice extra — it closes an aliasing hole I didn't call out, where a caller mutating a returned mapping dict would have reached into live config.

The read-terminator handling is also better than what I suggested: trimming exactly one trailing `\r\n`/`\n`/`\r` instead of `.strip()` preserves leading whitespace and internal structure. Right call.

---

## 2. New issues introduced by this commit

### 2.1 `++savecfg 0` silently disables all config persistence — **critical, reproduced**

Persistence is now gated on the Prologix `savecfg` flag:

```python
# config_manager.py — in set_mapping, delete_mapping, clear_all_mappings, update_settings
if self.config["settings"].get("savecfg", 1) == 1:
    self._save_config_unlocked()
```

But `savecfg` is also a per-client Prologix setting, and `set_client_setting()` propagates it into the *global* runtime config for dashboard display. So any socket client sending `++savecfg 0` — a routine thing for real Prologix clients, since it avoids EEPROM wear on actual hardware — turns off VMSG's own disk persistence for the entire process.

Reproduced on a live instance:

```
PUT /api/mappings/1        -> {"status":"success", ...}
mapping 1 persisted?        True

[socket client sends "++savecfg 0"]

PUT /api/mappings/5        -> {"status":"success", ...}
slot 5 persisted to disk?   False
```

The API returns HTTP 200 `"status":"success"` while the write is silently discarded. Configure your bench in the dashboard, restart, and the work is gone with no error anywhere.

**This is already latent in the repo.** The committed `mappings.json` has:

```json
"savecfg": 0,
"lon": 1,
```

— state left over from a socket session. A fresh clone therefore starts with persistence disabled from the first run.

**Fix:** these are unrelated concepts that happen to share a name. Emulate `++savecfg` as a pure state flag (store it, echo it back, do nothing else) and keep VMSG's own persistence unconditional, or gate it on a separate `persist_config` setting that only the dashboard can change. Add `"savecfg"` to `_SESSION_ONLY` so it never reaches global config at all.

### 2.2 `++llo`, `++loc`, `++ifc` are worse than unimplemented — **reproduced**

They were folded into the flag-storage branch alongside `lon` and `savecfg`:

```python
elif cmd in ["lon", "savecfg", "llo", "loc", "ifc"]:
    if arg is None:
        return f"{self.get_client_setting(client_addr, cmd, 0)}\r\n"
```

But these three are *actions*, not settings — they take no argument. Live:

```
++loc  -> b'0\r\n'
++llo  -> b'0\r\n'
++ifc  -> b'0\r\n'
```

Two problems at once. The instrument stays in remote (nobody returned it to local control), *and* a spurious `0\r\n` lands in the data stream — precisely the desync hazard the unknown-command fix was meant to eliminate. A client that sends `++loc` at the end of a session and then reads will get `0` where it expects instrument data.

Before this commit these fell through to the unknown-command branch, which now correctly returns `None`. So this specific path went from harmless to harmful.

**Fix:** split them out, always return `None`, and map to real operations:

```python
elif cmd in ("llo", "loc", "ifc"):
    curr_addr = self.get_client_setting(client_addr, "addr")
    await self._bus_control(curr_addr, cmd)   # control_ren / GTL / interface clear
    return None
```

PyVISA exposes `resource.control_ren(mode)` on GPIB resources — `VI_GPIB_REN_ADDRESS_GTL` for `++loc`, `VI_GPIB_REN_ASSERT_LLO` for `++llo`. For non-GPIB backends, log a "not supported on this interface" warning and still return `None`. `++srq` is still absent entirely and returns nothing, which is at least safe.

### 2.3 `unmapped_behavior: "message"` no longer produces a message

The `"message"` branch now returns `None` — nothing is sent at all. Confirmed live: `++addr 29` on an unmapped slot followed by `*IDN?` produces no response.

That's the *right* wire behavior, but the setting name and its UI description are now wrong. The admin panel and README both describe it as returning "friendly warning messages," and a user picking it will reasonably expect to see something. The two modes are now "fail fast, silently" and "sleep the timeout, then send an empty terminator."

**Fix:** rename to `fast_fail` / `emulate_timeout` (with a migration for existing configs), update the panel copy and README, and consider making `emulate_timeout` the default since it's what a real Prologix does.

### 2.4 Two error strings survived the cleanup

```python
# route_instrument_cmd
return f"Error: Connection to physical instrument failed ({e})\r\n"
# perform_instrument_read
return f"Error: Connect failed ({e})\r\n"
```

Every other error return was converted to `None` or a bare terminator; these two weren't. They fire on the exact path most likely to hit in practice — an instrument powered off or unplugged since VMSG started — and a VISA exception string is long, contains commas, and will be parsed as a measurement value by an unsuspecting client. Convert both to the terminator-only response.

### 2.5 The healing task can be garbage-collected mid-flight

```python
asyncio.create_task(_async_startup_healing())
```

The return value is discarded. `asyncio` only holds a weak reference to running tasks, so this can be collected before completion — a documented and genuinely intermittent CPython behavior. Startup healing would then silently stop partway through with no error. Keep a strong reference:

```python
self._bg_tasks = set()
t = asyncio.create_task(_async_startup_healing())
self._bg_tasks.add(t)
t.add_done_callback(self._bg_tasks.discard)
```

Same pattern applies to the `_delayed_stop` / `_delayed_restart` tasks in `web_app.py`.

### 2.6 `update_settings` now accepts arbitrary keys

The new `else: self.config["settings"][k] = v` branch stores any key not in `DEFAULT_CONFIG`, unvalidated. Combined with `/api/config/restore` now routing through `update_settings`, a corrupt or hand-edited backup can inject junk into settings — and since `save_config()` runs immediately after, it persists. Separately, `set_runtime_setting("llo"/"loc"/"ifc", ...)` writes three keys that aren't in the schema, so they'll accumulate in `mappings.json` on the next dashboard save.

Keep an explicit allowlist for unknown keys, or drop them as before. The round-1 concern was that restore bypassed validation; it now goes through a validator that has a hole in it.

---

## 3. Refinements to the fixes that landed

### 3.1 `++clr` and `++trg` skip the interface lock

Both take `res_lock` only. On a shared GPIB bus, a Selected Device Clear or Group Execute Trigger issued while another instrument is mid-transfer is a bus-level operation that can disrupt it. Wrap both in `get_interface_lock(visa_addr)` for consistency with read/write/spoll.

Also: `++trg` falls back to `res.write("*TRG")` when `assert_trigger` is unavailable. Worth knowing that's a semantic downgrade — GET is a bus message to all addressed listeners, `*TRG` is a device-specific SCPI command. Fine as a fallback, but log it as a degradation rather than doing it silently.

### 3.2 Healing still double-queries, and ambiguity is invisible

Two leftovers from §5.4 and §3.7:

```python
scanned_devices = self.scan_all_hardware()      # queries *IDN? on every port
...
current_idn = self.query_idn(expected_visa_addr, timeout_ms=1000)   # queries it again
```

The scan already has that IDN. Look it up in `scanned_devices` instead of paying a second round trip per mapping.

And the ambiguity refusal — a good addition — reports via `print()`:

```python
print(f"[VisaManager] Healing skipped for slot {addr_str}: IDN pattern ... matches multiple active devices.")
```

`print()` doesn't reach the dashboard log feed, and `enable_stdout` defaults to `False`, so on a normal run this is invisible. The user sees an instrument that silently didn't heal and no explanation. There are 11 `print()` calls left in `visa_manager.py` — all of them should be `logger.*`. This one especially.

The underlying cause is still unaddressed: `auto_assign_devices` sets `idn_pattern = idn.split(",")[1][:25]`, i.e. the model number only. So duplicate models produce patterns that *always* collide, and healing now permanently refuses rather than silently mismapping. Better failure mode, but still a failure. Use IDN field 2 (serial number) when present:

```python
fields = [f.strip() for f in idn.split(",")]
model = fields[1] if len(fields) > 1 else fields[0]
serial = fields[2] if len(fields) > 2 and fields[2] not in ("", "0") else ""
idn_pattern = f"{model},{serial}" if serial else model
```

### 3.3 `get_logs(since_id)` scans the full buffer

```python
return [entry for entry in self.logs if entry.get("id", 0) > since_id]
```

Every 800 ms this walks all 1000 entries to return the handful that are new. IDs are monotonic and the deque is ordered, so walk backwards and stop:

```python
out = []
for entry in reversed(self.logs):
    if entry["id"] <= since_id:
        break
    out.append(entry)
out.reverse()
return out
```

Also worth returning a `truncated: true` flag when `since_id` is older than the oldest retained entry — under heavy traffic the deque can evict entries the client never saw, and right now that gap is silent.

### 3.4 Startup healing readiness is a sleep

`await asyncio.sleep(0.5)` before healing is a proxy for "servers are listening." It works, but it's timing-dependent. `asyncio.start_server` has already returned by the time `socket_server.start()` is awaited, so an `asyncio.Event` set right after the listener binds would be exact and free.

---

## 4. Still open from round 1, in priority order

**Byte-oriented data path (§3.2).** This is now the single largest gap. `data.decode('utf-8', errors='replace')` on ingress, string-mode `res.write()`/`res.read()`, and `re.split(r'\r\n|\n|\r')` all break on 8-bit payloads. Concretely: 33250A arb download and any `FORMAT:DATA REAL` transfer still cannot pass through VMSG. Everything else in this commit is polish by comparison — this is the thing standing between "works for ASCII readings" and "carries real measurement traffic."

Worth noting the mocks would need to grow alongside it: `MockVisaResource` has no `read_raw`, `write_raw`, `clear`, or `assert_trigger`, so `++clr` on a mock silently does nothing (the `hasattr` check swallows it) and there's no way to CI-test binary passthrough. Adding those four methods makes the whole new command surface testable without hardware.

**Write+read atomicity (§3.4).** Unchanged. The interface locks actually increase concurrency, so two clients hitting the same slot — TestController plus the dashboard SCPI console, say — remain able to swap responses.

**Security (§6).** Unchanged: `0.0.0.0` on both ports, `allow_origins=["*"]` with `allow_credentials=True`, and unauthenticated `/api/system/restart` running `subprocess.Popen`. Given the config-persistence bug above, an unauthenticated restart is now also a way to lose configuration.

**`last_query_addr` (§8.3).** Still present with the stale-value hazard: a query written but never read leaves the value set, and a later `++read` at a different address reads the wrong instrument.

**Driver-map matching on VISA address string (§7).** `combined = f"{idn} {description} {visa_address}".upper()` — unchanged. USB resource strings carry hex VID/PID and serials that will eventually collide with a model number. Match on the IDN model field only, and make the `"TEKTRONIX" in combined → "Tektronix TDS2024"` catch-all return a generic driver.

**Snoop filtering (§7).** `f"address {address}" in msg.lower()` — unchanged. Slot 1's snoop view will show slots 10–19's traffic.

**Config path via `os.getcwd()` (§7).** Unchanged, and it interacts badly with §2.1: run the binary from a different directory and you get a fresh config; run it after a `++savecfg 0` and you get no config at all.

---

## 5. New recommendations

### 5.1 `mappings.json` should not be in the repo

It's runtime state, not a shipped default. Three problems:

- It carries your bench's actual configuration into everyone's clone.
- It ships with `savecfg: 0` and `lon: 1`, so a fresh clone starts in the broken-persistence state described in §2.1.
- Once a user runs VMSG, their local file is modified, and any `git pull` conflicts or clobbers it.

Add `mappings.json` to `.gitignore`, ship `mappings.example.json` with clean defaults and mock mappings only, and have `ConfigManager` seed from the example on first run if no config exists.

`VMSG-review.md` also got committed — probably not intended as a shipped artifact.

### 5.2 The `++loc` bug is a five-line unit test

`execute_prologix_cmd` is now the most-changed and most bug-prone surface in the project, and it has zero direct test coverage. Every issue in §2.2 and §2.4 is a one-assert test:

```python
async def test_action_commands_return_nothing(server):
    for cmd in ("loc", "llo", "ifc", "clr", "trg"):
        assert await server.execute_prologix_cmd(cmd, None, CLIENT) is None
```

No server process, no sockets, no hardware — a fake config and VISA manager is enough. The current `test_emulator.py` requires a live instance and `sleep()`-based synchronization, which is why none of this got caught. I'd make adding a `tests/` directory with parser-level tests the next structural change after the byte refactor.

### 5.3 Consider a settings schema instead of the branching validator

`update_settings` is now a 30-line `if/elif` chain with an escape hatch at the end, and the escape hatch is where §2.6 comes from. Since Pydantic is already a dependency via FastAPI, defining the settings as a model gives you validation, coercion, defaults, and rejection of unknown keys in one place — and `/api/settings`, `/api/config/restore`, and the socket path would all share it rather than each having slightly different rules.

### 5.4 Prune `interface_locks`, and skip the lock where it isn't needed

`interface_locks` grows without bound (trivially small in practice, but unbounded). More usefully: TCPIP and USB endpoints don't share a bus the way GPIB and ASRL do. Returning a no-op context manager for `TCPIP*`/`USB*` would let a LAN scope and a USB DMM run genuinely in parallel rather than serializing per-interface. Cheap change, real throughput gain on a mixed bench.

---

## 6. Suggested next commit

1. Decouple `++savecfg` from config persistence; add it to `_SESSION_ONLY`. (§2.1)
2. Split `llo`/`loc`/`ifc` out of the flag branch; always return `None`. (§2.2)
3. Convert the two surviving `Error: Connect...` returns to bare terminators. (§2.4)
4. Hold references to background tasks. (§2.5)
5. `.gitignore mappings.json`, ship `mappings.example.json` with `savecfg: 1`. (§5.1)
6. `print()` → `logger.*` in `visa_manager.py`. (§3.2)

That's a small, low-risk commit that clears every regression this round introduced. Then the byte-oriented refactor as its own change, with mock `read_raw`/`write_raw` and a `tests/` directory landing alongside it.
