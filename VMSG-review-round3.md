# VMSG Review — Round 3

**Repo:** `Cyclotronic/VMSG` @ `3bd93df`
**Baseline:** `8c8236c` (round 2)
**Method:** full diff review, live instance run, VISA constant verification against installed PyVISA

---

## 1. Round-2 scorecard

| Finding | Status |
|---|---|
| 2.1 `++savecfg 0` kills persistence | **Fixed** — added to `_SESSION_ONLY`, persistence ungated |
| 2.2 `++llo`/`++loc`/`++ifc` return flags | **Fixed structurally** — split out, return `None`. But the REN constants are wrong (§3.1) |
| 2.3 `unmapped_behavior: "message"` naming drift | Not addressed |
| 2.4 Two surviving error strings | **Fixed** — but this is what your note is about (§2) |
| 2.5 Background task GC | **Fixed** — `bg_tasks` set + `add_done_callback` |
| 2.6 `update_settings` accepts arbitrary keys | **Fixed** — `else` branch removed |
| 3.1 `++clr`/`++trg` skip interface lock | **Fixed** |
| 3.2 Healing double-query | **Fixed** — `scanned_by_addr` lookup, 500 ms fallback |
| 3.2 `print()` → `logger` | **Fixed** — all 11 converted. But see §3.2 |
| 3.2 Serial fingerprinting | **Half done** — `create_fingerprint()` written but never called (§3.3) |
| 3.3 `get_logs` full scan | **Fixed** — reverse-iterate with early break |
| 3.4 Healing readiness sleep | Not addressed (minor) |
| 5.1 `mappings.json` in repo | Not addressed, and it's now causing a concrete problem (§3.5) |
| 5.2 `tests/` directory | Not addressed |
| §4 binary path, atomicity, security, driver map, snoop, getcwd | All unchanged |

Also worth noting: the healing fix is better than what I suggested. Falling back to `query_idn(..., 500)` when the address isn't in the scan results handles the case where the mapped address isn't currently enumerable, which a pure lookup would have missed.

---

## 2. Your Section 10 note on returning `None`

**The bug you found is real, and I under-specified the fix in round 2.** But the prescription in the note would reintroduce the desync problem, so I want to separate the two halves.

### 2.1 What's actually broken

I reproduced it against the current build. `test_emulator.py` Test 15:

```
++addr 30       # unmapped
++read eoi
resp = s.recv(1024)     # blocks 3 s, raises socket.timeout, aborts the run
```

`perform_instrument_read` returns `None` for an unmapped slot under `unmapped_behavior: "message"`, and also on empty-VISA-address and connect-failure. Zero bytes go back. Same problem on the write path: `route_instrument_cmd` now returns `None` on connect failure *unconditionally*, so a client with `++auto 1` — which has every reason to expect a response — gets nothing.

So yes, this is a genuine hang source that I introduced by saying "convert these to `None`" without qualifying when.

### 2.2 The rule I should have given

Prologix is strict request/response with **no error channel**. The question isn't "did something fail," it's **"is the client currently waiting for bytes?"** A response is owed exactly when the client has initiated a read:

- an explicit `++read`
- a write while `auto == 1`
- a `++cmd` query with no argument

Writes with `auto == 0` produce nothing on real hardware, success *or* failure. That's the case where returning bytes causes desync.

| Situation | `auto` | Correct response |
|---|---|---|
| `++read` (explicit) | any | Always ≥ terminator. **Never `None`.** |
| Write succeeded | 0 | `None` |
| Write succeeded | 1 | Read result, or terminator on timeout |
| Write failed (connect / IO) | 0 | `None` |
| Write failed | 1 | **Terminator** — emulate the read that would have timed out |
| Unmapped slot, write | 0 | `None` (after the sleep, if emulating timeout) |
| Unmapped slot, write | 1 | Terminator |
| Unmapped slot, `++read` | any | Terminator |
| `++cmd <arg>` | — | `None` |
| `++cmd` (query form) | — | Value + CRLF |

The key insight the note is missing: when a write fails and `auto == 1`, the answer isn't "send an error" — it's "still emulate the read." On real hardware the write fails silently on the bus, then the read times out, and the client gets an empty response. That's a well-defined state every Prologix client already handles.

### 2.3 Why not error text

The note's second bullet — "an error response should be written back to the socket client to notify it of the failure" — is where I'd push back, for four reasons:

1. **It looks like instrument data.** `Error: Connect failed (VI_ERROR_RSRC_NFOUND (-1073807343): Insufficient location information...)` has commas, parentheses, and a numeric field. TestController will parse it as an `*IDN?` response and may bind a driver to it, or display it as a measurement.
2. **No client has a parser for it.** Anything written against real Prologix hardware has never seen such a string, because the hardware never emits one.
3. **With `auto == 0` it poisons the next read.** The error sits in the socket buffer; the client's subsequent `++read` returns it instead of the actual data. That's the desync hazard, just deferred.
4. **You already built the correct channel.** This commit moved all 11 `print()` calls into the dashboard log feed, and the snoop view filters per-slot. Errors belong there — out-of-band, timestamped, filterable — not inline on the data path.

Crucially, **the existing test already accepts the correct behavior**: Test 15 asserts `"Error:" in resp or resp == ""`, and `"\r\n".strip()` is `""`. Returning the bare terminator passes it as written. No error text is needed to make the test green.

### 2.4 The concrete patch

Add one helper and use it at every failure return:

```python
def _empty_response(self, client_addr: tuple) -> str:
    """The response a real Prologix emits when a read yields nothing."""
    term = "\r\n"
    if self.get_client_setting(client_addr, "eot_enable") == 1:
        term = chr(self.get_client_setting(client_addr, "eot_char")) + term
    return term
```

In `perform_instrument_read`, replace every `return None` with `return self._empty_response(client_addr)` — there are three (unmapped+message, empty visa_addr, connect failure). That function is only ever called when someone is waiting, so it should have no `None` return path at all; consider tightening the signature to `-> str`.

In `route_instrument_cmd`, replace the failure returns with:

```python
return self._empty_response(client_addr) if auto_mode == 1 else None
```

### 2.5 If you want the error text for hand debugging

That's a legitimate need — poking at VMSG with Tera Term or netcat, a silent failure is genuinely unhelpful. Make it an explicit, orthogonal setting rather than the default:

```python
"verbose_errors": False,   # emit human-readable errors on the socket; breaks machine clients
```

Gate the error strings on it, default off, and label it in the admin panel as "Interactive debugging — do not enable with TestController connected." That gets you the diagnostic experience without making the protocol lie to automated clients.

While you're there: `unmapped_behavior` values should be renamed. `"message"` no longer sends a message, and once §2.4 lands neither mode does. `fast_fail` / `emulate_timeout` describes what they actually do, and `emulate_timeout` is the better default since it matches real hardware.

---

## 3. New issues this round

### 3.1 `++loc` does the opposite of what it says — **verified**

```python
if cmd == "loc":
    res.control_ren(1)  # Go To Local (GTL)
elif cmd == "llo":
    res.control_ren(3)  # Local Lockout (LLO)
```

Both constants are wrong. From the installed PyVISA (`constants.RENLineOperation`):

```
deassert = 0    asrt = 1           deassert_gtl = 2    asrt_address = 3
asrt_llo = 4    asrt_address_llo = 5    address_gtl = 6
```

So `control_ren(1)` is **assert REN** — it puts the instrument *into* remote, the exact opposite of go-to-local. And `control_ren(3)` is `asrt_address`, not local lockout.

Correct values:

```python
from pyvisa import constants
if cmd == "loc":
    res.control_ren(constants.RENLineOperation.address_gtl)        # 6
elif cmd == "llo":
    res.control_ren(constants.RENLineOperation.asrt_address_llo)   # 5
```

Use the named constants rather than integer literals — that's what made this slip through. The practical symptom: you finish a session, send `++loc` to get the front panel back, and the instrument stays locked in remote.

`++ifc` also falls into this branch but has no body — it opens the resource, takes both locks, and does nothing. Interface Clear is a bus-level operation, not a per-resource one; either implement it via the interface resource or drop it from the branch and log "not supported on this backend." Right now it pays two lock acquisitions to no effect.

### 3.2 The server now prints nothing at all on startup — **verified**

Converting the `print()` calls to `logger.*` was right, but they all became `logger.info(...)` and the default `log_level` is `WARN` with `enable_stdout: False`. Measured on a fresh run:

```
--- stdout bytes: 0 ---
```

For a standalone binary someone double-clicks, that's a real regression. There's no confirmation it started, no port numbers, no indication of which VISA backend loaded or that none did. If a port is already bound, the user sees an empty console and has to guess.

Keep a small unconditional startup banner on stdout — separate from the log system — covering: version, the two bind addresses, the resolved config file path (which also surfaces the `os.getcwd()` issue), and the VISA backend that initialized. Everything after startup can stay in the log feed.

### 3.3 `create_fingerprint()` is dead code

Written correctly — model plus serial, with sensible fallbacks — but never called. `auto_assign_devices` still does:

```python
desc = idn.split(",")[1].strip() ...
app.state.config.set_mapping(..., idn_pattern=desc, description=desc)
```

Wire it in: `idn_pattern=visa.create_fingerprint(idn)`, keeping `description=desc` for display. As it stands the ambiguity-refusal path in `heal_mappings` is doing all the work and duplicate models simply never heal.

### 3.4 `savecfg` is now vestigial in the config schema

`load_config` force-sets `self.config["settings"]["savecfg"] = 1` on every load, so the persisted value is meaningless — correct outcome, slightly blunt mechanism. Two loose ends: the assignment happens *outside* the `with self.lock` block (harmless in practice, inconsistent with everything else in the class), and `savecfg` is still in `DEFAULT_CONFIG` and in `SettingsModel`, so `POST /api/settings` still accepts it and it round-trips through backup/restore as a no-op field. Drop it from the persisted schema and the API model; it's purely a per-session emulation flag now.

### 3.5 Your committed `mappings.json` has an ambiguous pair

```json
"7":  { "visa_address": "TCPIP::192.168.1.84::INSTR", "idn_pattern": "34411A" },
"11": { "visa_address": "GPIB0::3::INSTR",            "idn_pattern": "34411A" }
```

Both slots fingerprint on `34411A`. If both are online, `heal_mappings` finds two matches and refuses to heal *either* — the new ambiguity guard firing exactly as designed, but leaving both slots unhealable with only a WARN in the log.

Worth noting that serial-number fingerprinting (§3.3) **will not fix this case** if it's one 34411A reachable over both LAN and GPIB — same instrument, same serial, same fingerprint. For that situation the right answer is to leave `idn_pattern` empty on the secondary mapping, since the healer skips patterns that are blank. That's worth surfacing in the UI: a hint next to the field explaining that an empty pattern opts the slot out of healing, plus a warning when you save a pattern that duplicates another slot's.

The broader point from round 2 stands: this file is runtime state carrying your bench's LAN addresses into every clone. `.gitignore` it and ship `mappings.example.json`.

---

## 4. Still open, unchanged

Ranked by what I'd actually do next:

1. **Byte-oriented data path.** Unchanged across three commits and still the largest functional gap. `data.decode('utf-8', errors='replace')`, string-mode `res.write()`/`res.read()`, and `re.split(r'\r\n|\n|\r')` all destroy 8-bit payloads. 33250A arb download and `FORMAT:DATA REAL` transfers cannot pass through. Needs mock `read_raw`/`write_raw` alongside it so it's CI-testable.
2. **No `tests/` directory.** Every bug in §3.1 and §2 is a two-line unit test against `execute_prologix_cmd`. The `control_ren` constants would have been caught by asserting the argument passed to a mock resource. Three rounds of review have each found parser bugs that a test file would have caught first.
3. **Write+read atomicity.** Two clients on the same slot can still swap responses.
4. **Security.** `0.0.0.0` on both ports, `allow_origins=["*"]` with credentials, unauthenticated `/api/system/restart` running `subprocess.Popen`.
5. `last_query_addr` staleness; driver map matching the VISA address string; snoop substring matching; `os.getcwd()` config path; unbounded socket buffer; eager f-strings on the hot path; `++spoll` returning 0 on failure.

---

## 5. Suggested next commit

1. `_empty_response()` helper; apply the §2.2 response table. Fixes the hang without error text. (§2.4)
2. Fix `control_ren` constants; use `pyvisa.constants.RENLineOperation` names. Empty `++ifc` branch → log unsupported. (§3.1)
3. Unconditional startup banner on stdout: version, ports, config path, VISA backend. (§3.2)
4. Wire `create_fingerprint()` into `auto_assign_devices`. (§3.3)
5. Remove `savecfg` from `DEFAULT_CONFIG` and `SettingsModel`; move the force-set inside the lock. (§3.4)
6. `.gitignore mappings.json`; ship `mappings.example.json`. Blank the `idn_pattern` on slot 7 or 11. (§3.5)
7. Rename `unmapped_behavior` values; add `verbose_errors` (default off). (§2.5)

Then `tests/` and the byte refactor as one change. Items 1–7 are a couple of hours; the fact that three consecutive review rounds have each turned up a fresh parser bug is the strongest argument for doing the test file before the next feature.
