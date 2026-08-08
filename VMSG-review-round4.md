# VMSG Review — Round 4: Concurrency, Latency, and Instrument Contention

**Repo:** `Cyclotronic/VMSG` @ `7690b74`
**Focus:** multiple simultaneous TCP sockets, low-latency polling across all instruments, and access contention
**Method:** code review plus measurements against a live instance (mock instruments, multi-threaded load clients)

---

## 1. Round-3 scorecard

| Finding | Status |
|---|---|
| §2 `None`-response hang / response policy | **Fixed** — `_empty_response()` helper, applied at all 9 failure returns, `perform_instrument_read` tightened to `-> str` |
| §3.1 `control_ren` constants backwards | **Fixed** — `RENLineOperation.address_gtl` / `asrt_address_llo`, `++ifc` logs unsupported |
| §3.2 Silent startup | **Fixed** — banner added. But backend detection is wrong (§4.6) |
| §3.3 `create_fingerprint()` dead | **Fixed** — wired into `auto_assign_devices` |
| §3.4 `savecfg` vestigial | Partly — force-set now inside the lock; still in `DEFAULT_CONFIG` and `SettingsModel` |
| §3.5 `mappings.json` tracked | **Fixed** — untracked, `.gitignore`d, `mappings.example.json` added with seed-on-first-run |
| §2.5 `unmapped_behavior` rename / `verbose_errors` | Not addressed |
| Byte path, atomicity, security, tests | Not addressed |

The `_empty_response` rollout is correct and complete — I checked every return path, including the `auto == 1` conditional on the write side. The seed-from-example logic in `load_config` is a nice touch beyond what I suggested.

---

## 2. Measured behavior

All numbers from a live instance, mock instruments, `TCP_NODELAY` on both ends, loopback.

### Steady state — good

```
1 client,  1 instrument          : p50=0.22ms  p99=0.59ms  max=0.74ms
3 clients, 3 distinct instruments: p50=0.56ms  p99=1.35ms  max=1.63ms
6 clients, same instrument       : p50=0.93ms  p99=1.58ms  max=1.89ms
```

The event-loop-plus-thread-offload design is sound. Scaling from 1 to 3 concurrent clients costs ~0.34 ms and the distribution stays tight. There is no throughput or overhead problem here.

### Failure mode 1 — cross-client response swapping: **13.5%**

Two clients, both mapped to slot 1, polling the same instrument concurrently. Client A sends `MEAS:VOLT:DC?`, client B sends `*IDN?`, 400 iterations each:

```
Client A (MEAS:VOLT:DC?) received an *IDN? reply : 54/400  (13.5%)
   e.g. 'HEWLETT-PACKARD,34401A,0,10.0-1.0-1.0'
Client B (*IDN?) received a voltage reading      : 55/400  (13.8%)
   e.g. '+4.99992490E+00'
```

**One in seven readings goes to the wrong client.** No error, no warning, no log entry — just a wrong number delivered as if it were correct. For a metrology tool this is the most serious class of defect possible: silently wrong data.

Cause is the non-atomic query I've flagged since round 1. `route_instrument_cmd` acquires `interface_lock` + `res_lock` for the write, releases both, then `perform_instrument_read` acquires them again for the read. Between those two acquisitions another client's write lands, and the instrument's output queue no longer corresponds to who asked.

Note this is *not* the pathological case — this is two clients doing exactly what your requirement describes. And it does not require duplicate mappings: the dashboard SCPI console plus a TestController socket on the same slot is enough.

### Failure mode 2 — one bad instrument freezes the entire gateway: **10.0 s**

One client addresses a slot mapped to an unreachable endpoint (`TCPIP::10.255.255.1::5025::SOCKET` — simulating a powered-off or unplugged instrument). A second, unrelated client polls a healthy mock throughout:

```
healthy client BEFORE : p50=0.35ms   max=0.79ms
blackhole client blocked 10.32s
healthy client DURING : p50=0.34ms   max=10013.28ms
web /api/status       : p50=2.2ms    max=10005.0ms
```

Every socket and the entire web dashboard stalled for **10.0 seconds** because one client touched one dead instrument.

Cause: `VisaManager.get_resource()` is called from the coroutine, not from a thread:

```python
res, res_lock = self.visa_manager.get_resource(visa_addr, timeout_ms=read_tmo_ms)
```

On a cache miss it runs `self.rm.open_resource(visa_address)` — a fully blocking VISA call — **on the event loop**, and does so while holding `VisaManager.lock`, a single global mutex over the whole resource dictionary. Nothing else on the loop runs until it returns.

This recurs on every cache miss, which means it recurs after every `purge_resource()` — so a flaky instrument that keeps erroring and getting purged will re-stall the whole gateway on each retry.

---

## 3. Contention issues not yet visible in measurement

### 3.1 A wedged VISA call is permanent, not just slow

Nothing bounds the total time of an operation. `resource.timeout` is set, but backends do occasionally hang past it — a wedged GPIB board or a pyvisa-py socket read that never returns. There is no `asyncio.wait_for` around any `to_thread` call.

When that happens the worker thread holds `res_lock` **and** `interface_lock` forever. Every client targeting that interface — the whole GPIB bus, not just the one instrument — blocks permanently, with no recovery short of restarting VMSG. That's strictly worse than the 10 s stall because it never clears.

Minimum fix, independent of any refactor:

```python
try:
    response = await asyncio.wait_for(
        asyncio.to_thread(_execute_read), timeout=(read_tmo_ms / 1000.0) + 1.0
    )
except asyncio.TimeoutError:
    self.visa_manager.mark_unhealthy(visa_addr)
    return self._empty_response(client_addr)
```

`wait_for` won't kill the thread, but it frees the *client* and lets you mark the resource unhealthy and refuse further traffic to it until a background reopen succeeds. That converts a permanent gateway-wide hang into one degraded instrument.

### 3.2 `resource.timeout` is shared mutable state across clients

```python
with self.lock:
    if visa_address in self.resource_cache:
        resource = self.resource_cache[visa_address]
        resource.timeout = timeout_ms      # <-- shared object, NOT under res_lock
        return resource, res_lock
```

The timeout is written under `VisaManager.lock` but not under `res_lock`, and the resource object is shared. So:

- Client A sets `++read_tmo_ms 100` for fast polling; client B sets `5000` for a slow integration. Whichever called `get_resource` most recently wins **for both**.
- The write can land in the middle of another client's in-flight operation, changing the timeout of a transfer already underway.

Per-client `read_tmo_ms` is therefore not actually per-client. Timeout must be applied inside the same critical section as the operation it governs.

### 3.3 Locks are unfair; blocked waiters consume executor threads

`threading.Lock` gives no FIFO guarantee. Under sustained polling from several sockets onto one GPIB bus, a client can be starved for an unbounded interval — there is no worst-case latency bound, which is exactly what "reliable low-latency polling" needs.

Compounding it: the locks are acquired *inside* `asyncio.to_thread`, so every blocked waiter occupies a default-executor thread doing nothing. The default executor is `min(32, cpu+4)`. Once it saturates, `to_thread` calls queue behind it — and that queue is shared across *all* instruments. A slow GPIB bus with several waiters can starve a fast LAN instrument of workers even though the two share no hardware.

### 3.4 Config lock on the command hot path

`route_instrument_cmd` takes `ConfigManager.lock` up to six times per command (four `get_client_setting` fallbacks, `get_mapping`, `get_setting("unmapped_behavior")`), all on the event loop. Individually microseconds — but `_save_config_unlocked()` runs under that same lock and does an `fsync`. A dashboard mapping edit therefore fsyncs while holding a lock that every in-flight socket command needs, on the thread that serves all of them. Rare, but it's a multi-millisecond jitter spike correlated with UI activity.

Fix: make config reads lock-free by storing an immutable snapshot dict and swapping the reference on write (readers just grab the current reference; CPython attribute reads are atomic). And move `fsync` outside the lock — build the JSON under the lock, write and fsync outside it.

### 3.5 `client_sessions` is keyed by peer tuple

`(ip, port)` is reused after the ephemeral port cycles. Two sequential connections from the same source port would share session state, and `active_connections` (a set) would under-count. Long-running gateway with frequent TestController reconnects makes this reachable. Key on a per-connection UUID or the `StreamWriter` identity.

### 3.6 Still open from earlier rounds, relevant here

- **`last_query_addr`** — a query written but never read leaves the value sticky, so a later `++read` at a different address reads the wrong instrument. Under multi-socket polling this is per-session, so it can't cross clients, but it can still misroute within a session.
- **Unbounded socket buffer** — a client that sends no terminator grows `buffer` without limit.
- **Eager f-strings** on the traffic path — `repr(response)` is computed for every read even though the default `WARN` level discards it.

---

## 4. What I'd change architecturally

### 4.1 The design constraint worth stating

Per-socket command serialization is **correct and must stay**. Prologix responses carry no tag or sequence number, so the client matches them positionally to the commands it sent. Overlapping or reordering responses on one socket would break every client. So a single socket cannot poll multiple instruments concurrently — if one instrument is slow, everything behind it on that socket waits.

That means concurrency has to come from *multiple sockets*, which is exactly the A–E controller-ID arrangement you already documented in `TESTCONTROLLER_NOTES.md`. That workaround isn't just a TestController quirk fix — it's the load-bearing concurrency mechanism for the whole design, and the README should say so.

### 4.2 Per-bus-domain worker threads with transaction queues

This is the change that fixes findings 2.2, 3.1, 3.2, 3.3 and the 13.5% swap rate all at once, and it's the pattern you already use in POS.

Group resources into **bus domains** — the unit that genuinely cannot be used in parallel:

| Resource | Domain | Rationale |
|---|---|---|
| `GPIB0::5`, `GPIB0::7` | `GPIB0` | one board, one bus — must serialize |
| `ASRL3::INSTR` | `ASRL3` | one UART |
| `TCPIP::host::INSTR` | that resource | independent endpoint |
| `USB::...::INSTR` | that resource | independent endpoint |
| `MOCK::...` | that resource | independent |

One thread and one `queue.Queue` per domain. Submit a transaction, get back an `asyncio.Future`:

```python
async def submit(self, domain, txn) -> Any:
    fut = asyncio.get_running_loop().create_future()
    self.queues[domain].put((txn, fut, loop))
    return await asyncio.wait_for(fut, timeout=txn.deadline_s)
```

What this buys:

- **Atomicity for free.** A query is one transaction — write and read inside a single work item, never interleaved. The 13.5% swap rate goes to zero by construction, with no locks at all.
- **FIFO fairness.** `queue.Queue` is ordered, so worst-case latency is bounded by queue depth × per-transaction time. That's the guarantee "reliable low-latency" actually requires.
- **Bounded threads.** One per domain, not one per in-flight operation. No executor saturation, no cross-instrument starvation.
- **Blocking opens move off the loop.** The worker owns `open_resource()`, so the 10 s stall becomes 10 s of delay *on that one domain* while everything else runs normally.
- **Correct per-client timeouts.** The worker sets `resource.timeout` immediately before each operation, inside the only thread that touches the resource.
- **Natural backpressure.** A bounded queue lets you reject or fast-fail instead of accumulating unbounded latency.

### 4.3 This also removes `last_query_addr`

With `auto == 0`, the sequence is `MEAS:VOLT?` then `++read` — two client messages. Rather than tracking which address was last queried, have the worker perform write-and-read as one transaction whenever the command contains `?`, and buffer the result in a per-session pending slot. The subsequent `++read` returns the buffered value.

That makes the auto=0 path atomic too, matches Prologix semantics closely enough (the read blocks on the instrument either way), and deletes the stale-state hazard entirely. Non-query writes stay write-only, so you don't burn `read_tmo_ms` on them.

### 4.4 Health state per resource

Add an explicit state machine — `healthy` / `degraded` / `unreachable` — driven by transaction outcomes, and surface it on the patch-panel grid. When a domain is `unreachable`, fast-fail its queue with empty responses instead of making each client wait out a full timeout. Feed reopen attempts through a background task with backoff rather than doing them inline on the next client request. This is what turns the blackhole case from "everyone waits 10 s" into "that tile is red and its slot returns immediately."

### 4.5 Per-resource metrics

Given the requirement is explicitly about reliable low latency, you need to be able to see it. Track per resource: queue depth, in-flight count, p50/p99 transaction time, timeout count, consecutive-error count. Expose on `/api/status` and render on the dashboard. Right now there's no way to tell whether the gateway is meeting a latency target or which instrument is dragging.

### 4.6 Startup banner reports the wrong backend

```python
backend_type = "Pure-Python (@py)" if (visa.rm and "@py" in str(type(visa.rm))) else ("NI-VISA System" if visa.rm else "None (Mock Only)")
```

`type(rm)` is always `pyvisa.highlevel.ResourceManager` regardless of backend, so the `@py` test never matches and the banner **always** says "NI-VISA System." Verified on this machine, which has no NI-VISA:

```
type(rm) = <class 'pyvisa.highlevel.ResourceManager'>
'@py' in str(type(rm)) -> False
rm.visalib               -> Visa Library at py
```

Use `str(rm.visalib)` or `rm.visalib.library_path`. This matters more than it looks — "which VISA backend am I actually on" is the first question when diagnosing an instrument that won't talk, and the banner currently answers it wrong every time.

---

## 5. Recommended order

**Immediate — correctness and availability, small diffs**

1. Move `get_resource()` into the worker thread (or wrap the call site in `asyncio.to_thread`) so `open_resource` never runs on the event loop. Fixes the 10 s gateway-wide freeze.
2. Wrap every `to_thread` VISA call in `asyncio.wait_for` with `read_tmo_ms + 1 s`. Converts a permanent hang into one degraded instrument.
3. Hold `res_lock` across write-and-read for `auto == 1`, and set `resource.timeout` inside that critical section. Interim fix for the 13.5% swap and the shared-timeout bug, ahead of the full refactor.
4. Fix the banner backend detection.

**Then — the structural change**

5. Per-bus-domain worker threads with transaction queues (§4.2), including the auto=0 pending-response slot (§4.3) and resource health states (§4.4).
6. Lock-free config reads via immutable snapshot; `fsync` outside the lock.
7. Per-resource metrics on `/api/status` and the dashboard.

**Still queued from earlier rounds**

8. `tests/` — items 1, 2 and 3 above are all testable against mocks with no hardware, and the swap-rate test in §2 is about 30 lines. Four rounds have now each found something a test suite would have caught.
9. Byte-oriented data path (arb upload, `FORMAT:DATA REAL`).
10. Security: bind, CORS, unauthenticated `/api/system/restart`.
11. `client_sessions` keyed by connection identity; socket buffer cap; lazy log formatting.

---

## 6. Summary

At steady state the gateway is fast and the concurrency model holds up — sub-millisecond p50 with three concurrent sockets is a good result. The problems are all in the transition and failure paths, and two of them are serious for your stated use case: one client in seven gets another client's reading when two sockets share an instrument, and one unreachable instrument freezes every socket and the dashboard for the full TCP connect timeout.

Both trace to the same root: **the unit of work is a single VISA call rather than a transaction, and resource acquisition happens on the event loop.** The per-bus-domain worker queue in §4.2 addresses both, plus fairness, thread bounding, and per-client timeouts. Items 1–3 in §5 are worth doing first as small independent patches — they remove the two measured failures within an evening, and they'll still be correct after the refactor lands.
