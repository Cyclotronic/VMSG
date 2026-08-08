# VMSG Review — Round 5

**Repo:** `Cyclotronic/VMSG` @ `62015f1`
**Baseline:** `7690b74` (round 4)
**Method:** diff review plus measurements against a live instance, re-running the round-4 test harness

---

## 1. Round-4 scorecard

| Finding | Status |
|---|---|
| §2 Cross-client response swap (13.5%) | **Fixed for `auto=1` (0.0%). Still broken for `auto=0` — measured 90–98% (§3.1)** |
| §2 Event-loop freeze on blocking open | **Fixed** — web UI max 10,005 ms → 85.6 ms. But socket clients still stall (§3.3) |
| §3.1 No `wait_for` on VISA calls | **Partly** — added to transaction and read; missing on resource acquisition (§3.3) |
| §3.2 Shared `resource.timeout` mutation | Not addressed |
| §3.3 Unfair locks / executor coupling | Not addressed |
| §3.4 Config lock + fsync on hot path | **Regressed** — see §3.2 |
| §3.5 `client_sessions` keyed by peer tuple | Not addressed |
| §4.6 Banner backend detection | **Fixed** — now correctly reports `Pure-Python (@py)` |
| Per-client setting isolation | **Improved** — `_SESSION_ONLY` expanded to all Prologix settings (§4.1) |

The two structural fixes that landed are real and I measured both. Query atomicity under `auto=1` went to zero, and folding write+read into a single `_execute_transaction` under one lock acquisition is exactly the right shape. `async_get_resource` genuinely freed the event loop.

---

## 2. Measurements

### Query atomicity — fixed for one mode, wide open in the other

Two clients on slot 1, A polling `MEAS:VOLT:DC?`, B polling `*IDN?`, 400 iterations each:

```
auto=1: A got IDN   0/400 (0.0%)  | B got voltage   0/400 (0.0%)
auto=0: A got IDN 362/400 (90.5%) | B got voltage 391/400 (97.8%)
```

### Event loop freed, but clients still serialize

```
web /api/status during a 10 s blocking open : p50=2.0ms  max=85.6ms   (was max=10,005ms)
socket client on a healthy mock, same window: p50=0.34ms max=10,012ms (unchanged)
two distinct dead instruments concurrently  : 10.22 s and 20.03 s (serialized)
```

### Config file is torn ~4% of the time

Concurrent mapping writes with a reader sampling the file:

```
reads=45,596   unparseable snapshots=1,769 (3.9%)   distinct file sizes seen=124
```

---

## 3. Critical findings

### 3.1 `auto=0` — the mode TestController actually uses — is unprotected

This is the headline. The atomicity fix folded write+read into one transaction, but only on the `auto == 1` branch:

```python
def _execute_transaction():
    with interface_lock:
        with res_lock:
            res.write(command_with_term)
            if auto_mode == 1:
                return res.read()
            return None          # <-- auto=0: lock released here
```

With `auto == 0` the write releases both locks and returns. The client's subsequent `++read` enters `perform_instrument_read`, which acquires them again. Another client's write lands in the gap. Measured at **90.5% / 97.8%** wrong.

The reason this matters more than the original 13.5%: your own `TESTCONTROLLER_NOTES.md` documents TestController's init sequence as `++auto 0`, `++mode 1`. **The production path is the unprotected one.** The fix covers the mode used by the web console and by `test_emulator.py`, which is why the test suite passes.

Worth being clear about the shape of the problem: `auto=0` splits one logical query across *two client messages*. No lock held inside a single command handler can span that. It needs session-scoped ownership.

**Recommended fix — resource lease.** From the moment a session writes a query to a resource until it issues the matching `++read` (or a lease timeout of `read_tmo_ms` expires, or the session changes `++addr`, or disconnects), that resource is owned by that session. Other sessions' transactions to the same resource wait on an `asyncio.Condition` — on the event loop, not in executor threads, so no thread is consumed while waiting.

```python
async def acquire_lease(self, visa_addr, session_id, timeout_s):
    async with self._lease_cv:
        deadline = loop.time() + timeout_s
        while (owner := self._leases.get(visa_addr)) and owner.session != session_id:
            if owner.expires < loop.time():
                self._leases.pop(visa_addr, None); break
            if not await self._wait_until(deadline): raise LeaseTimeout
        self._leases[visa_addr] = Lease(session_id, loop.time() + timeout_s)
```

Reentrancy matters here: `++spoll` from the *owning* session must be permitted, because the trigger → serial-poll-until-ready → `++read` pattern is common in TestController's Keithley drivers. A poll from a non-owning session waits.

This also models reality correctly. On a real bus there is one controller, so this interleaving cannot occur; VMSG introduces the possibility by multiplexing, and the lease is what restores the invariant.

**Simpler alternative worth considering.** Given your A–E arrangement already gives each instrument its own socket, you could declare that a slot is owned by one connection at a time and make a second connection's traffic to that slot queue behind it. Less machinery, same guarantee, and it matches how you actually deploy. The lease is the general answer; connection-scoped ownership is the pragmatic one.

Either way: do **not** ship the current state. A gateway that silently hands one client another client's measurement 90% of the time in its primary configuration is worse than one that's slow.

### 3.2 Config write atomicity was reverted

Round 1's fix — tmp file, `fsync`, `os.replace` — has been replaced with:

```python
def _disk_write():
    with open(filepath, "w") as f:          # truncate-in-place
        json.dump(config_snapshot, f, indent=4)
        f.flush()                            # no fsync
threading.Thread(target=_disk_write, daemon=True).start()   # new thread per save
```

Three problems:

1. **Torn file.** `open(w)` truncates before writing, so the file on disk is invalid for the duration. Measured 3.9% of reads unparseable. If VMSG crashes or the machine loses power in that window, `load_config` takes the exception path and resets to defaults — every mapping gone.
2. **No write ordering.** Each save spawns its own thread. Two rapid saves race; nothing guarantees the *newer* snapshot wins. The config on disk can silently be an older state than the config in memory.
3. **Unbounded thread creation.** One thread per save, daemon, never joined. A burst of dashboard edits or auto-assign across 30 slots spawns 30 threads writing the same file.

Also, `.gitignore` still lists `mappings.json.tmp`, now unused.

**Fix:** one long-lived writer thread with a coalescing signal, still writing atomically:

```python
self._dirty = threading.Event()
def _writer_loop(self):
    while True:
        self._dirty.wait(); self._dirty.clear()
        time.sleep(0.25)                      # coalesce bursts
        with self.lock:
            snapshot = copy.deepcopy(self.config)
        tmp = self.filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=4); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.filepath)
```

`_save_config_unlocked` becomes `self._dirty.set()`. That gets the latency benefit you were after — no I/O under the lock, no fsync on the hot path — without giving up durability or ordering. Add a final synchronous flush in the shutdown path so a clean exit doesn't lose the last edit.

### 3.3 Resource acquisition has no timeout and serializes globally

`async_get_resource` moved `open_resource` off the event loop — confirmed, the web UI now stays responsive. But socket clients still stalled 10 s on a healthy instrument during a blocking open, and two distinct dead instruments took 10 s and 20 s rather than running concurrently.

Cause: `get_resource` holds `VisaManager.lock` — one global mutex over the entire resource dictionary — for the full duration of `open_resource`. Every other client's acquisition, for any instrument, queues behind it.

Additionally, `async_get_resource` is the one blocking call with **no `wait_for`**. The transaction and read paths got one; acquisition didn't. So the unbounded-wait path simply moved.

**Fix, two parts:**

1. Narrow the lock. Under `VisaManager.lock`, insert a placeholder/`Event` for the address and release; do the actual `open_resource` outside the lock; re-acquire briefly to store the result and signal waiters. Concurrent requests for the *same* address wait on that Event; requests for *different* addresses proceed immediately.
2. Wrap the acquisition in `wait_for` with an explicit connect budget (a few seconds, separate from `read_tmo_ms`), and on expiry mark the resource unreachable.

### 3.4 Timed-out operations still hold their locks forever

`wait_for` frees the *client*, which is the improvement round 4 asked for. But the orphaned thread keeps running and still holds `res_lock` and `interface_lock`. There's no `mark_unhealthy` / health state, so:

- Every subsequent client on that interface acquires an executor thread, blocks on the held lock, waits out its own `read_tmo_ms + 1.5 s`, and abandons another thread.
- Thread count grows once per attempt with no upper bound.

A wedged instrument therefore degrades from "one permanent hang" to "a steadily growing pile of abandoned threads." Better, but still not recoverable. This needs the `healthy` / `degraded` / `unreachable` state machine from round 4 §4.4: once a resource times out, fast-fail its traffic with `_empty_response()` and retry reopening on a backoff in the background, rather than letting each client discover the problem the slow way.

---

## 4. Smaller issues

### 4.1 `logger.error(..., exc_info=True)` raises `TypeError`

```python
logger.error("SOCKET_SERVER", f"Error serving client {client_address}: {e}", exc_info=True)
```

`SystemLogger.error()` is `(self, category, message)` — no `exc_info`. Verified:

```
TypeError: SystemLogger.error() got an unexpected keyword argument 'exc_info'
```

This is inside `handle_client`'s `except` block, so any error serving a client raises a *second* exception from the handler that was supposed to report it, skipping straight to `finally`. The original error is never logged. Either add `exc_info` support to `SystemLogger` (formatting the traceback into the message) or drop the kwarg.

### 4.2 Debug code committed

```python
vmsg_core/prologix_server.py:150   import traceback; traceback.print_exc()
vmsg_core/prologix_server.py:492   print(f"[DEBUG] route_instrument_cmd exception: {e}")
vmsg_core/prologix_server.py:493   import traceback; traceback.print_exc()
```

These bypass the log system entirely and print to a console that, for the packaged binary, nobody is watching. If you want tracebacks — and you should — route them through `logger` via 4.1 rather than `print`.

### 4.3 The dashboard's Prologix settings panel is now dead

Expanding `_SESSION_ONLY` to cover `addr`, `auto`, `mode`, `eos`, `eoi`, `read_tmo_ms`, `eot_enable`, `eot_char` means `set_client_setting` never calls `set_runtime_setting`. That's correct isolation — client A's `++addr 5` no longer leaks into client B's defaults. But it silently removes the dashboard's live view: `/api/status` returns `prologix_settings` from stored config, which no socket client ever updates, while the UI still presents it as current state.

Either relabel it "defaults for new connections," or add a proper per-connection view — `/api/sessions` returning each connected client's live settings, rendered as a row per socket. Given you're running five TestController connections, the per-session view is genuinely more useful than the single global panel ever was.

### 4.4 `_empty_response()` is now the terminator builder

It's used for the empty case *and* appended to real responses (`response + self._empty_response(...)`). The name no longer describes it. `_terminator()` or `_response_suffix()`.

### 4.5 Inconsistent int coercion

`route_instrument_cmd` now casts everything (`int(self.get_client_setting(...))`), which is a good defensive addition after the `set_runtime_setting` validation gap. `perform_instrument_read` doesn't — `read_tmo_ms` goes straight into `(read_tmo_ms / 1000.0) + 1.5`. Apply the same casts, or better, validate once in `set_client_setting` so neither call site has to.

### 4.6 Still open from earlier rounds

`last_query_addr` staleness; `resource.timeout` shared across clients outside `res_lock`; unfair locks with no FIFO guarantee; `client_sessions` keyed by reusable peer tuple; unbounded socket buffer; eager f-strings on the traffic path; byte-oriented data path; security (bind / CORS / unauthenticated restart); no `tests/`.

---

## 5. Recommended order

**Before anything else**

1. **`auto=0` atomicity** (§3.1). Resource lease or connection-scoped slot ownership. This is a data-integrity defect in the primary deployment configuration.
2. **Restore atomic config writes** (§3.2). Single writer thread, coalescing, tmp + fsync + `os.replace`.
3. **Remove the `exc_info` kwarg and the debug prints** (§4.1, §4.2). Ten-minute fix; right now client errors are invisible.

**Next**

4. Narrow `VisaManager.lock` around `open_resource`; add a connect-budget `wait_for` (§3.3).
5. Resource health states with fast-fail and background reopen (§3.4).
6. Per-session view in the dashboard to replace the now-static settings panel (§4.3).

**Then the queued work**

7. `tests/`. The `auto=0` regression is a fifteen-line test — write both modes, assert no cross-contamination — and it would have caught this before the commit. Five rounds, five sets of findings a suite would have caught first. The round-4 swap-rate harness is directly reusable.
8. Byte-oriented data path; security; the remaining §4.6 items.

---

## 6. Summary

The two headline fixes from round 4 both landed and both measure correctly: `auto=1` query atomicity is at 0% cross-contamination, and the event loop no longer freezes on a blocking open. That's real progress on the hard part.

But the atomicity fix stops at the branch boundary, and the mode it doesn't cover is the one TestController uses — measured 90–98% wrong responses. And the config-write change traded durability for latency in a way that leaves the file invalid on disk ~4% of the time under load, with no write ordering.

Both are fixable without touching the architecture: a session-scoped lease for the first, a single coalescing writer thread for the second. I'd do those two plus the three-line debug cleanup, then re-run the round-4 harness against `auto=0` to confirm before moving on.
