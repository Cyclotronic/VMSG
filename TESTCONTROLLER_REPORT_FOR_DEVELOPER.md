# TestController + Prologix Ethernet — Four Findings From A Bench Investigation

**From:** the VMSG project (VISA Mapping TCP/IP Socket Gateway)
**Date:** 8 August 2026
**TestController version tested:** the 1.8.x build dated 2026-07-25 (`TestController.jar`, `Main-Class: dk.hkj.main.Main`)
**Bench:** Fluke PM6690, Agilent 34411A, Keithley 2010, Keithley 2001M, Keithley 2002

---

## First, an apology and an explanation

I want to be upfront about how this report was produced, because I would rather you hear it from me plainly than discover it later.

While trying to work out why my own software was misbehaving with TestController, I ran out of things I could learn from the outside. I decompiled four classes from `TestController.jar` to understand what the code was actually doing. I know that is an uncomfortable thing to do with someone else's work, and I did not take the decision lightly. My intent was diagnosis, not appropriation.

For the record, and to be as clear as I can:

- Nothing has been redistributed. The decompiled sources and the patched jar exist only on my bench machine, and I am happy to delete them at any time on your word.
- No attempt was made to circumvent anything — the jar is unsigned and has no protection of any kind, and I have not touched licensing, branding, or anything unrelated to these four defects.
- I am not publishing a fork or a patched build. This report exists so that **you** can decide what, if anything, to do.
- If you would prefer I had simply filed a vague bug report and left it there, I understand, and I apologise for overstepping.

My honest motivation was this: I could not tell whether the problem was in my software or yours, and guessing wrong in public would have been unfair to you. Reading the code was the only way I could be certain before saying anything.

TestController is genuinely excellent, and it is the reason my project exists at all — I built a VISA-to-Prologix gateway specifically so that instruments TestController cannot otherwise reach could be used with it. I would not have spent this many hours on it if I did not think it was worth the effort.

---

## Why this may matter beyond my niche

I want to be careful not to overstate this, since you know your own code far better than I do. But if I have read it correctly, three of these four findings affect **anyone using a real Prologix Ethernet adapter with more than one instrument**, which I suspect is the common case rather than an edge case — a single Prologix adapter sits on a GPIB bus with several instruments, and that is exactly the configuration that appears to be affected.

Concretely, if this analysis holds:

- Multiple instruments could share **one** Prologix adapter and one controller ID reliably, rather than needing a separate adapter or a separate controller entry per instrument.
- A `++addr` command would be re-sent after any reconnection, so an adapter that lost power (or a network blip that dropped the TCP session) could not leave TestController quietly addressing the wrong instrument.
- A misconfigured device name would no longer silently remove every device below it from the list.

The third and fourth points are the ones I would gently flag as data-integrity issues rather than convenience issues, because in both cases the failure is silent and the user has no obvious signal that anything went wrong.

I could of course be wrong about any of this. Everything below is offered as "here is what I observed and what I think it means," not as a verdict.

---

## What I observed, and what I think causes it

I have kept each finding to: the symptom, the line I believe is responsible, and the evidence.

### 1. `SharedInterfacePrologixEthernet.neededCommInterface()` — replaces a socket that is already in use

**Symptom.** Configure several instruments on a single controller ID. At startup most device threads stop immediately:

```text
Start thread for: PrologixEthernet A:2 - Agilent 34411A
Start thread for: PrologixEthernet A:1 - Fluke PM6690
Stopping thread for: PrologixEthernet A:1 - Fluke PM6690     (~1 ms later)
Stopping thread for: PrologixEthernet A:4 - Keithley 2001M
Stopping thread for: PrologixEthernet A:5 - Keithley 2002
Stopping thread for: PrologixEthernet A:3 - Keithley 2010
```

**What I think is happening.** `InterfaceThreads.addDevicesShared()` calls `neededCommInterface()` once per configured device, and device threads are started inside that same loop. Each call assigns a brand-new, unopened `SocketInterface` to the shared `ci` field, discarding the previous one — including one that a running thread has already opened. That thread is then holding a reference to an interface whose socket was swapped out from under it, `open()` early-returns because `openStatus()` is already true, `initDevice()` sees `isOpen() == false`, and the thread is stopped.

**Supporting evidence.** On the gateway side I logged **exactly one** TCP connection and **no** `*IDN?` traffic at all for the four stopped threads — they never reached the network. The timing also matches: the four threads that never opened anything die in about 1 ms, while the one thread that *did* open a socket dies about 900 ms later (its 200 ms settle, then `*IDN?`, then the 700 ms retry) because its writes go to the discarded socket.

**Suggested change.**
```java
public String neededCommInterface() {
    if (this.ci == null) {                       // only create when there is none
        this.ci = new SocketInterface(this.address, this.getPort());
        this.ci.debugLog = InterfaceThreads.debugAll;
    }
    return null;
}
```
`reset()` still sets `ci` to null, so a Reconnect correctly gets a fresh socket.

---

### 2. `SharedInterface.writeRead()` — write-then-read is not atomic

**Symptom.** With finding 1 fixed and several threads genuinely sharing one interface, replies occasionally arrive damaged:

```text
Device "KEITHLEY INSTRUMENTS INC.,MODEL 2002," do not match answer:
        "EITHLEY INSTRUMENTS INC.,MODEL 2002,4461274,B02  /A02"
```

That is the correct instrument's IDN with its leading `K` missing — consumed by another thread's `flush()` while this thread's reply was in flight.

**What I think is happening.** `write()` and `read()` are each `synchronized`, but the pair is not, so another thread can interleave between them. Besides eating bytes, an interleaved `write()` calls `setActualAddress()`, which can re-address the bus between a thread's write and its read.

**Suggested change.** Make the transaction atomic:
```java
public synchronized String writeRead(int localAddress, String msg, int timeout) {
```

I mention this one with particular diffidence, since I appreciate that adding synchronisation can have consequences elsewhere that I am not in a position to see.

---

### 3. `SharedInterface.open()` — per-device open state is only recorded for the first device

**What I think is happening.** `getDeviceSettings(localAddress).isOpen = true` sits inside the `if (!openStatus())` block, so only the device that physically opened the interface is ever marked open. Every later device sharing that interface is not counted. `close()` then decrements against an undercount, so closing the first device makes `openStatus()` false and tears down the socket the remaining devices are still using.

**Suggested change.** Record it for every caller:
```java
        this.init();
    }
    this.getDeviceSettings((int)localAddress).isOpen = true;   // outside the if
}
```

---

### 4. `SharedInterfacePrologixUSB` — the address cache is not invalidated on reconnect

This is the one I would most encourage a look at, because the failure is silent.

**Symptom.** With one controller ID per instrument, pressing **Reconnect** loses every device but one:

```text
PrologixEthernet C:3 Device "KEITHLEY...MODEL 2010,"  do not match answer: "FLUKE, PM6690, ..."
PrologixEthernet E:5 Device "KEITHLEY...MODEL 2002,"  do not match answer: "FLUKE, PM6690, ..."
PrologixEthernet D:4 Device "KEITHLEY...MODEL 2001M," do not match answer: "FLUKE, PM6690, ..."
PrologixEthernet B:2 Device "Agilent...34411A,"       do not match answer: "FLUKE, PM6690, ..."
Found Fluke PM6690 on PrologixEthernet A:1
Stopping thread for: PrologixEthernet C:3 / E:5 / D:4 / B:2
```

Every device received the answer belonging to the instrument at the controller's *default* address.

**What I think is happening.** `selectedLocalAddress` and `selectedTimeout` cache what the adapter was last told, but they are only initialised when the object is constructed. `init()` does run on every open — the trace confirms `++auto 0` and `++mode 1` are re-sent after a Reconnect — but the cache survives it. So `setActualAddress()` compares the wanted address against a value from *before* the disconnect, finds them equal, and skips `++addr`, while the freshly opened controller is sitting at its own default address.

**Why I believe this affects real hardware too.** On a physical Prologix adapter the address is volatile unless stored with `++savecfg`. An adapter that has been power-cycled, or a TCP session re-established against one, is at its saved or default address, not the one TestController last set. The stale cache then means measurements are silently taken from the wrong instrument. My gateway made this obvious because it starts each new connection from a known default; on real hardware I suspect it would appear only intermittently, which is worse.

**Suggested change.** A newly opened controller has unknown state, so invalidate the cache in `init()`:
```java
public void init() {
    this.selectedLocalAddress = -1;
    this.selectedTimeout = -1;
    this.writeWithDelay("++auto 0");
    this.writeWithDelay("++mode 1");
    ...
```

---

### 5. A separate, smaller one: `break` where I think `continue` is meant

`InterfaceThreads.addDevicesShared()` (and the `addDevicesSerial` / `addDevicesSocket` equivalents):

```java
DeviceInterface di = InterfaceThreads.findDeviceInterfaceFromDeviceDefinition(def);
if (di == null) break;        // aborts the whole loop
```

If a `settingsLoad.txt` entry names a device with no matching driver, every device *after* it is skipped with no message. I hit this by accident with a generated config containing one unrecognised name, and TestController loaded zero GPIB devices and fell back to repeatedly scanning serial ports — it took me a long time to work out why, because nothing in the log pointed at the config file.

Even keeping the current behaviour, a single log line naming the unresolved device would have saved me an evening.

---

## What I did to test the theory

I recompiled the four affected classes with the changes above and ran the result against real instruments. I want to be clear that this was to validate the diagnosis before sending it to you — not to produce something to distribute.

**Single controller ID, all five instruments on one socket** — the configuration that previously failed:

```text
Found Fluke PM6690    on PrologixEthernet A:1 sn: 979819
Found Agilent 34411A  on PrologixEthernet A:2 sn: MY48005929
Found Keithley 2010   on PrologixEthernet A:3 sn: 636735
Found Keithley 2001M  on PrologixEthernet A:4 sn: 1150952
Found Keithley 2002   on PrologixEthernet A:5 sn: 4461274

30-second soak, one socket: 1721 log lines, 0 anomalies, 85-87 receives per device
```

**Reconnect**, tested with one controller ID per instrument and **no** workaround in the config — the exact setup that previously lost four devices:

```text
20:02:38.453  ++auto 0 / ++mode 1  on all five threads
20:02:38.656  ++addr 4   ++addr 3   ++addr 1   ++addr 5   ++addr 2
Found: Agilent 34411A, Fluke PM6690, Keithley 2001M, Keithley 2010, Keithley 2002
```

All five recovered. My gateway's log agrees: five connections, five `++addr` commands, five distinct addresses, no warnings.

One thing the patch did **not** fix, which you may want to look at independently — a `NullPointerException` during the device-close phase of a Reconnect, where close commands are sent on an interface that has already been torn down:

```text
java.lang.NullPointerException
    at dk.hkj.shared.SharedInterface.writeWithDelay(SharedInterface.java:116)
    at dk.hkj.shared.SharedInterfacePrologixUSB.write(SharedInterfacePrologixUSB.java:76)
    at dk.hkj.comm.GpibInterface.write(GpibInterface.java:64)
    at dk.hkj.main.SCPICommand.writeReadInternal(SCPICommand.java:385)
    at dk.hkj.main.DeviceInterface.doCommand(DeviceInterface.java:85)
    at dk.hkj.devices.DeviceSCPI.close(DeviceSCPI.java:157)
    at dk.hkj.main.InterfaceThreads$DeviceThread.run(InterfaceThreads.java:1751)
```

It appears harmless in practice, since the reopen succeeds either way.

---

## Two things I learned that are simply useful

Neither is a defect — I mention them because I could not find them documented, and both turned out to be genuinely handy.

**The `settings:` field accepts a port.** `SharedInterface.getPort()` parses `port:<N>` from `settings:` and defaults to 1234, and `PrologixEthernet` honours it. So an alternate port *is* reachable even though `address:` does not accept a `host:port` suffix:
```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:port:1235|
```

**The `settings:` field also injects controller commands.** Any `;`-separated element beginning with `++` is sent during `init()`. This let me work around finding 4 without modifying anything:
```text
PrologixEthernet|id:A|address:127.0.0.1|baudrate:|settings:++addr 1|
```

If these are intentional features, they deserve a line in the documentation — they are useful. If they are incidental, they are still the cleanest workaround available today for anyone hitting finding 4.

---

## Where this leaves things

I have no expectation that you act on any of this, and no sense of entitlement about it — it is your project and your call. If the analysis is useful, I am glad. If I have misread something, I would honestly like to know, and I will correct my own notes accordingly.

If it would help, I am happy to:

- test any change you make against a five-instrument bench with full protocol traces on both sides,
- provide the complete debug logs for any scenario above,
- re-run anything with different instruments or timing, or
- simply stay out of the way.

Thank you for TestController, and thank you for reading a report this long. I hope the tone has come across as it was meant — appreciative, and a bit sheepish about the method.
