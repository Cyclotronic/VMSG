# Performance Notes

What VMSG costs, measured rather than assumed.

The short answer: **VMSG adds about 1 ms per query and nothing measurable when
switching between instruments.** If a reading is slow, the instrument is slow.

---

## Method

The same instruments were asked the same question twice — once through VMSG's
Prologix socket, once directly through NI-VISA — on a host with the instruments
locally attached, so both paths were available simultaneously.

```sh
python tools/vmsg_overhead.py \
    --map 1=USB0::0x0A69::0x0880::630041501550::INSTR \
    --map 3=GPIB0::8::INSTR \
    --map 2=GPIB0::7::INSTR \
    --interleave
```

Two regimes, because they can differ:

- **steady-state** — one instrument, repeated queries, no address switching.
- **interleaved** — round-robin across all three, one query each. Forces VMSG to
  re-address between every query. This is what TestController does when several
  devices log at once, and it is the case where a gateway would be expected to
  cost the most.

---

## Results

`MEAS:VOLT?`, 20 repetitions, median wall time:

| Regime | Direct VISA | Via VMSG | Delta | Multiplier |
| :--- | ---: | ---: | ---: | ---: |
| Chroma 63004 (USB) | 21.6 ms | 22.6 ms | +0.9 ms | 1.04× |
| HP E3633A (GPIB) | 118.3 ms | 119.7 ms | +1.4 ms | 1.01× |
| HP E3631A (GPIB) | 232.9 ms | 233.0 ms | +0.0 ms | 1.00× |
| All three, round-robin | 361.3 ms | 361.2 ms | −0.1 ms | 1.00× |

Worst case **+1.4 ms per query**. Address switching is free: the interleaved
delta is within run-to-run noise of zero, so `++addr` costs nothing measurable.

## What this means

The cost of a reading is set by the instrument and its transport, not by VMSG.
Per-query cost varied 10× *between instruments on the same bus* — 22 ms for the
Chroma over USB against 233 ms for an E3631A over GPIB — while the gateway
contributed roughly 1 ms throughout.

Two consequences worth carrying into driver work:

- **Reducing query count is the only meaningful lever.** Cost scales with the
  number of queries, and every query pays that instrument's fixed floor. On an
  E3631A even a trivial register read such as `STAT:QUES:COND?` costs ~93 ms.
- **Don't optimise the gateway to fix a slow log.** There is ~1 ms available
  there. The instrument holds the rest.

---

## Measuring this correctly

Both of these were got wrong before they were got right, and both produced
confident, entirely fictional numbers.

**Keep settling delays out of the timed path.** A drain-to-silence loop with a
100 ms quiet threshold bills 100 ms to whatever it is wrapping, on every call,
because the silence it waits for never arrives. An early version of this
measurement reported "+127 ms per address switch" that was, in full, the
measuring code's own padding. With `++auto 0` set once, `++addr N` followed
immediately by the query needs no settling at all.

**Check that replies still line up.** Removing padding risks reading one
instrument's answer as another's, which shows up as an *improbably fast* result.
`vmsg_overhead.py` parses every reply as a number and reports non-numeric ones,
so a desynchronised read is visible instead of flattering.

A general-purpose gateway benchmark, which does not need VISA and works against a
remote VMSG, is `tools/benchmark.py`. Use `vmsg_overhead.py` only when the
instruments are reachable both ways — it is the comparison that isolates VMSG's
own contribution.
