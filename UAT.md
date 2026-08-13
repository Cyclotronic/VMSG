# User Acceptance Testing

Automated checks answer *"does the code do what the code says"*. They do not
answer *"does this behave correctly on a real bench"* — and for VMSG that gap is
most of the risk, because the failures that actually matter here have all been
timing, hardware state, and client behaviour rather than logic.

Everything in CI runs against mock instruments at sub-millisecond latency. Real
instruments are slow, occasionally wrong, and sometimes mid-integration when you
address them. TestController is a third-party client whose reconnect behaviour we
do not control. Neither is reproducible in CI.

**Complete this checklist before promoting a prerelease to a full release.**
Record each run in `docs/uat-runs/` (copy the template at the bottom).

---

## Environment

Record what was actually on the bench. A pass on three mocks and a pass on five
real instruments across two interfaces are not the same result.

| Field | Value |
| :--- | :--- |
| VMSG version / commit | |
| Artifact tested | source / `vmsg-windows-amd64.exe` / `vmsg-linux-amd64` |
| Host OS | |
| VISA backend | NI-VISA / pyvisa-py |
| TestController version | |
| Instruments (model + interface) | |
| Date / tester | |

---

## 1. Cold start

- [ ] Gateway starts; banner lists the expected listeners
- [ ] Every mapped instrument answers `*IDN?` with the *correct* identity
      (not merely *an* identity — a wrong-instrument reply is the failure mode
      that started this work)
- [ ] No WARN/ERROR in the log during a clean start
- [ ] `tools/benchmark.py --test all` completes with 0 failed queries

## 2. TestController integration

The primary client. Both cold start and Reconnect must be exercised — they fail
differently.

- [ ] Export a config from VMSG and load it in TestController
- [ ] Cold start: all devices appear and read correctly
- [ ] **Menu → Reconnect: all devices recover with correct identities**
- [ ] Sustained polling for ≥ 5 minutes with no dropped devices
- [ ] No `do not match answer` errors in the TestController log

> Reconnect is the known-difficult path: TestController may omit `++addr` after
> reconnecting, so every device can end up addressing the default slot. See
> `TESTCONTROLLER_NOTES.md`. Verify the `settings:++addr N` workaround is still
> effective on this build.

## 3. Protocol paths

Each protocol reaches instruments by a different route through the gateway;
passing on one says little about the others.

- [ ] Prologix socket (1234): `++ver`, `++addr`, query, `++read eoi`
- [ ] LXI raw socket (5025): `*IDN?` returns the mapped instrument
- [ ] VXI-11: a real client (PyVISA `TCPIP::<host>::inst0::INSTR`) creates a
      link and reads — **not yet validated against real hardware**
- [ ] mDNS: the gateway is discoverable from another host on the network

## 4. Concurrency on real hardware

Mock instruments cannot produce bus contention. This is where lock ordering and
shared-bus serialisation actually get tested.

- [ ] Several instruments polled simultaneously; no cross-talk (no reply
      delivered to the wrong session)
- [ ] Instruments on a *shared* bus (e.g. multiple GPIB addresses) stay correct
      under concurrent load
- [ ] A hardware scan while traffic is live does not corrupt an in-flight
      transaction
- [ ] Latency is plausible for the instruments involved (an NPLC-10 reading is
      legitimately slow; the gateway should not add much on top)

## 5. Failure and recovery

- [ ] Power-cycle an instrument mid-session: the error is logged with a stated
      reason, and the instrument recovers without restarting VMSG
- [ ] Disconnect and reconnect a client cleanly
- [ ] Unmapped slot returns an empty response rather than another instrument's
      data
- [ ] Cooldowns, when they trigger, name the instrument and the reason

## 6. Packaged artifact

Only for a release candidate — the whole point is testing what users download.

- [ ] `tools/verify_frozen_build.py` passes against the built artifact
- [ ] SHA-256 matches the published `.sha256`
- [ ] Runs on a machine **without Python installed**
- [ ] Dashboard loads and controls instruments from the packaged binary
- [ ] `build-info.json` records the expected version and dependencies

## 7. Security posture

Listeners bind `0.0.0.0` by choice, so the API token is the boundary rather
than one layer of several.

- [ ] Control API rejects requests with no token (401)
- [ ] Dashboard works without a manual login step
- [ ] Token persists across a restart
- [ ] `VMSG_API_TOKEN` override is honoured

---

## Release promotion

1. Tag a release candidate: `v1.3.0-rc1` → publishes as a GitHub **prerelease**
2. Work this checklist against the prerelease artifact
3. File anything found; fix and cut a new rc
4. When the checklist is clean, tag the full release and promote it
5. Commit the completed run under `docs/uat-runs/`

A release may be cut with known gaps — but they must be *written down* here and
in the release notes, not carried in someone's memory.

---

## Run record template

```markdown
# UAT run: v1.3.0-rc1

- Date:
- Tester:
- Artifact:            vmsg-windows-amd64.exe (sha256 ...)
- Host:                Windows 11, NI-VISA
- TestController:      3.41
- Instruments:         Keithley 2001M (GPIB0::15), Keithley 2002 (GPIB0::17),
                       Agilent 34411A (TCPIP), ...

## Result: PASS / PASS WITH NOTES / FAIL

| Section | Result | Notes |
| :--- | :--- | :--- |
| 1 Cold start | | |
| 2 TestController | | |
| 3 Protocol paths | | |
| 4 Concurrency | | |
| 5 Failure/recovery | | |
| 6 Packaged artifact | | |
| 7 Security | | |

## Issues found

## Known gaps accepted for this release
```

---

## Currently outstanding

Carried forward until a bench run clears them:

- **VXI-11 against real instruments.** Verified against mocks only. The GPIB bus
  on the development host returns `VI_ERROR_BERR` through the Prologix path too,
  so the fault is hardware and unrelated — but it means the VXI-11 path has
  never touched a physical instrument.
- **TestController against the current build.** The auth, VXI-11 and netutil
  changes have not been exercised by a real TestController session.
- **Linux packaged artifact.** Built in CI, never run on a Linux host with real
  instruments. VXI-11 binds portmap on port 111, which is privileged on Linux;
  the code logs and continues, but the degraded path is untested.
