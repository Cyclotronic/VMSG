import time
import threading
import random
import asyncio
from typing import Dict, Any, List, Optional, Tuple
import pyvisa
from .logger import logger

class MockVisaResource:
    """Simulates a physical instrument's response behavior for testing and out-of-the-box operation."""
    def __init__(self, visa_address: str, idn_string: str = "Mock Instrument,Generic,1.0", delay_s: float = 0.0):
        self.visa_address = visa_address
        self.idn_string = idn_string
        self.timeout = 3000
        self.delay_s = delay_s
        self._last_command = ""
        self.lock = threading.Lock()
        
        # Parse device type from address for custom behaviors
        self.device_type = "generic"
        if "DMM" in visa_address.upper():
            self.device_type = "dmm"
            self.idn_string = "HEWLETT-PACKARD,34401A,0,10.0-1.0-1.0"
        elif "SCOPE" in visa_address.upper():
            self.device_type = "scope"
            self.idn_string = "TEKTRONIX,TDS 2024,0,CF:91.1CT FV:v4.12"
        elif "GENERIC" in visa_address.upper():
            parts = visa_address.split("::")
            if len(parts) > 2:
                self.idn_string = parts[2]

    def write(self, command: str) -> int:
        """Simulates writing a command."""
        with self.lock:
            self._last_command = command.strip()
            if self.delay_s > 0:
                time.sleep(self.delay_s)
            return len(command)

    def read(self) -> str:
        """Simulates reading a response based on the last command."""
        with self.lock:
            cmd = self._last_command.upper()
            if cmd == "*IDN?":
                return self.idn_string + "\n"
            elif cmd == "*OPC?":
                return "1\n"
            elif cmd == "*TST?":
                return "0\n"
            
            # HP 34401A / Generic DMM simulation
            if self.device_type == "dmm":
                if "MEAS:VOLT:DC?" in cmd or "READ?" in cmd or "VAL?" in cmd:
                    val = random.normalvariate(5.0, 0.001)
                    return f"{val:+.8E}\n"
                elif "MEAS:RES?" in cmd:
                    val = 1000.0 + random.uniform(-1.0, 1.0)
                    return f"{val:+.8E}\n"
                elif "MEAS:CURR:DC?" in cmd:
                    val = random.uniform(0.01, 0.05)
                    return f"{val:+.8E}\n"
                
            # Tektronix / Generic Scope simulation
            elif self.device_type == "scope":
                if "MEAS:VAL?" in cmd or "MEASUREMENT:IMMED:VALUE?" in cmd:
                    return f"{random.uniform(3.28, 3.32)}\n"
                elif "WAV:DATA?" in cmd or "CURVE?" in cmd:
                    # Return 10 points of a sine wave
                    points = [int(128 + 127 * random.normalvariate(0, 0.01)) for _ in range(20)]
                    return ",".join(map(str, points)) + "\n"

            # Default fallback responses for general commands
            if "?" in cmd:
                if "VOLT" in cmd:
                    return "+5.00000000E+00\n"
                elif "FREQ" in cmd:
                    return "+1.00000000E+03\n"
                elif "STAT" in cmd:
                    return "1\n"
                return "MOCK_RESPONSE: OK\n"
            return ""

    def read_stb(self) -> int:
        """Simulates reading the status byte (serial poll) of the mock instrument."""
        return 16

    def close(self) -> None:
        pass


class VisaManager:
    """Manages active PyVISA connections and resource pooling."""
    def __init__(self):
        self.lock = threading.Lock()
        self.global_visa_lock = threading.Lock()  # Hardware bus fallback lock
        self.interface_locks: Dict[str, threading.Lock] = {}  # Per-interface lock (e.g. GPIB0, TCPIP0)
        self.resource_cache: Dict[str, Any] = {}
        self.resource_locks: Dict[str, threading.Lock] = {}
        self.pending_opens: Dict[str, threading.Event] = {}
        # visa_address -> {"time": last_fail_timestamp, "count": consecutive_failures}
        self.unresponsive_cache: Dict[str, Dict[str, float]] = {}
        self.unresponsive_lock = threading.Lock()
        self.rm: Optional[pyvisa.ResourceManager] = None
        
        # Initialize ResourceManager
        self._init_resource_manager()

    def _init_resource_manager(self) -> None:
        """Initializes PyVISA ResourceManager with fallback to Pure-Python backend (@py)."""
        try:
            self.rm = pyvisa.ResourceManager()
            logger.info("VISAMANAGER", "Successfully initialized default PyVISA ResourceManager.")
        except Exception as e_ni:
            logger.info("VISAMANAGER", f"Default NI-VISA init failed: {e_ni}. Trying pure-python @py backend...")
            try:
                self.rm = pyvisa.ResourceManager("@py")
                logger.info("VISAMANAGER", "Successfully initialized pure-python (@py) PyVISA ResourceManager.")
            except Exception as e_py:
                self.rm = None
                logger.error("VISAMANAGER", f"Could not initialize any VISA ResourceManager: {e_py}")

    def get_interface_lock(self, visa_address: str) -> threading.Lock:
        """Retrieves or creates a lock specific to the hardware interface (e.g., GPIB0, USB0, TCPIP0)."""
        if not visa_address:
            return self.global_visa_lock
        interface_key = visa_address.split("::", 1)[0].upper()
        with self.lock:
            if interface_key not in self.interface_locks:
                self.interface_locks[interface_key] = threading.Lock()
            return self.interface_locks[interface_key]

    def list_physical_resources(self) -> List[str]:
        """Lists connected physical VISA resources."""
        if not self.rm:
            return []
        try:
            return list(self.rm.list_resources())
        except Exception as e:
            logger.error("VISAMANAGER", f"Error listing physical resources: {e}")
            return []

    # Cooldown escalation: each successive failure backs off further, but never so far
    # that a working instrument stays fast-failed for long. A single hiccup must not
    # take a healthy device out of service - that is why the steps start small.
    _COOLDOWN_STEPS = (2.0, 5.0, 15.0, 30.0)

    def _cooldown_for(self, count: int) -> float:
        idx = min(max(count, 1), len(self._COOLDOWN_STEPS)) - 1
        return self._COOLDOWN_STEPS[idx]

    def _record_unresponsive_failure(self, visa_address: str, reason: str = "") -> None:
        """Records a failed access so subsequent calls fast-fail briefly instead of blocking.

        Always logs: a silent cooldown makes a healthy-but-busy instrument look dead
        with nothing in the log to explain why.
        """
        if visa_address.upper().startswith("MOCK::"):
            return
        with self.unresponsive_lock:
            entry = self.unresponsive_cache.get(visa_address)
            count = (entry["count"] + 1) if entry else 1
            self.unresponsive_cache[visa_address] = {"time": time.time(), "count": count}
        cooldown = self._cooldown_for(count)
        detail = f" ({reason})" if reason else ""
        logger.warning(
            "VISAMANAGER",
            f"{visa_address} marked unresponsive{detail}; failure #{count}, "
            f"fast-failing for {cooldown:.0f}s"
        )

    def clear_unresponsive(self, visa_address: str) -> None:
        """Clears any cooldown on a resource after a known-good access."""
        with self.unresponsive_lock:
            if self.unresponsive_cache.pop(visa_address, None) is not None:
                logger.info("VISAMANAGER", f"{visa_address} responded; cooldown cleared.")

    def _is_unresponsive(self, visa_address: str) -> bool:
        if visa_address.upper().startswith("MOCK::"):
            return False
        with self.unresponsive_lock:
            entry = self.unresponsive_cache.get(visa_address)
            if not entry:
                return False
            return (time.time() - entry["time"]) < self._cooldown_for(entry["count"])

    def get_resource(self, visa_address: str, timeout_ms: int = 3000) -> Tuple[Any, Optional[threading.Lock]]:
        """
        Retrieves a thread-safe connection to the requested VISA address.
        If address starts with 'MOCK::', returns a simulated resource.
        NOTE: Must be called from a worker thread or via async_get_resource on event loop.
        """
        if not visa_address:
            raise ValueError("Empty VISA address")

        while True:
            with self.lock:
                if visa_address not in self.resource_locks:
                    self.resource_locks[visa_address] = threading.Lock()
                res_lock = self.resource_locks[visa_address]

                if visa_address in self.resource_cache:
                    resource = self.resource_cache[visa_address]
                    try:
                        if hasattr(resource, "timeout"):
                            resource.timeout = timeout_ms
                    except Exception:
                        pass
                    return resource, res_lock

            # The cooldown guards the *open* path only. Handing back an already-open
            # handle costs nothing, so a probe that timed out (a scan losing a race for
            # a busy bus, say) must not deny a working handle to client traffic - if the
            # instrument really is gone, the caller's own VISA timeout reports it.
            if self._is_unresponsive(visa_address):
                raise RuntimeError(f"Resource {visa_address} is recently unresponsive (cooldown active).")

            with self.lock:
                if visa_address.upper().startswith("MOCK::"):
                    resource = MockVisaResource(visa_address)
                    resource.timeout = timeout_ms
                    self.resource_cache[visa_address] = resource
                    logger.info("VISAMANAGER", f"Created cached Mock resource: {visa_address}")
                    return resource, res_lock

                if visa_address in self.pending_opens:
                    open_event = self.pending_opens[visa_address]
                else:
                    open_event = threading.Event()
                    self.pending_opens[visa_address] = open_event
                    break  # This thread will perform open_resource

            open_event.wait(timeout=5.0)

        if not self.rm:
            with self.lock:
                self.pending_opens.pop(visa_address, None)
                open_event.set()
            raise RuntimeError("No VISA ResourceManager available to connect to physical hardware.")

        try:
            resource = self.rm.open_resource(visa_address)
            resource.timeout = timeout_ms
            if hasattr(resource, "write_termination"):
                try:
                    resource.write_termination = ""
                except Exception:
                    pass
            if visa_address.upper().startswith("USB") and hasattr(resource, "read_termination"):
                try:
                    resource.read_termination = "\n"
                except Exception:
                    pass
            
            with self.lock:
                self.resource_cache[visa_address] = resource
                self.pending_opens.pop(visa_address, None)
                open_event.set()
            self.clear_unresponsive(visa_address)

            logger.info("VISAMANAGER", f"Connected and cached physical resource: {visa_address}")
            return resource, res_lock
        except Exception as e:
            with self.lock:
                self.pending_opens.pop(visa_address, None)
                open_event.set()
            self._record_unresponsive_failure(visa_address, f"open failed: {e}")
            logger.error("VISAMANAGER", f"Error opening VISA resource {visa_address}: {e}")
            raise

    async def async_get_resource(self, visa_address: str, timeout_ms: int = 3000) -> Tuple[Any, Optional[threading.Lock]]:
        """Asynchronously acquires resource with connect budget timeout off the main event loop."""
        connect_budget_s = min(5.0, (timeout_ms / 1000.0) + 2.0)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.get_resource, visa_address, timeout_ms),
                timeout=connect_budget_s
            )
        except asyncio.TimeoutError:
            # An already-open resource that blew the budget says nothing about the
            # instrument - we never reached it. Blaming the device here fast-fails a
            # healthy instrument for a scheduling delay on our side.
            with self.lock:
                already_open = visa_address in self.resource_cache
            if already_open:
                logger.warning(
                    "VISAMANAGER",
                    f"Timed out acquiring cached handle for {visa_address} within "
                    f"{connect_budget_s:.1f}s (gateway contention, not the instrument)."
                )
            else:
                self._record_unresponsive_failure(
                    visa_address, f"connect exceeded {connect_budget_s:.1f}s budget"
                )
            raise

    def purge_resource(self, visa_address: str) -> None:
        """Closes and removes a resource from cache (e.g., after a fatal connection error)."""
        with self.lock:
            if visa_address in self.resource_cache:
                res = self.resource_cache[visa_address]
                try:
                    res.close()
                except Exception:
                    pass
                del self.resource_cache[visa_address]
                logger.info("VISAMANAGER", f"Purged resource from cache: {visa_address}")

    def purge_all_resources(self) -> None:
        """Closes and purges all cached resources."""
        with self.lock:
            for addr, res in list(self.resource_cache.items()):
                try:
                    res.close()
                except Exception:
                    pass
                logger.info("VISAMANAGER", f"Purged resource from cache: {addr}")
            self.resource_cache.clear()

    def is_scannable_resource(self, addr: str, scan_serial: bool = False) -> bool:
        """Helper to filter out raw board interfaces, serial COM ports (unless enabled), and secondary GPIB addresses."""
        u = addr.upper()
        if not scan_serial and (u.startswith("ASRL") or "::ASRL" in u):
            return False
        if u.endswith("::INTFC") or u.endswith("::RAW"):
            return False
        if u.startswith("GPIB") and len(addr.split("::")) > 3:
            return False
        if "USB" in u and "::0::INSTR" in u:
            return False
        return True

    def query_idn(self, visa_address: str, timeout_ms: int = 1500, force: bool = False) -> Optional[str]:
        """Queries *IDN? of a resource with safety, returning None if failure.

        Takes the interface lock as well as the resource lock. A scan shares the
        physical bus with live client traffic, so an unsynchronised *IDN? here can
        land between another device's write and its read and be picked up as that
        device's answer. Lock order (interface then resource) matches the socket
        server's transaction path - reversing it would deadlock.
        """
        is_mock = visa_address.upper().startswith("MOCK::")

        if not is_mock and not force:
            if self._is_unresponsive(visa_address):
                return None

        try:
            res, res_lock = self.get_resource(visa_address, timeout_ms=timeout_ms)
        except Exception as e:
            self._record_unresponsive_failure(visa_address, f"scan could not open: {e}")
            return None

        interface_lock = None if is_mock else self.get_interface_lock(visa_address)

        def _do_query() -> str:
            old_timeout = getattr(res, "timeout", timeout_ms)
            try:
                res.timeout = timeout_ms
            except Exception:
                pass
            try:
                res.write("*IDN?")
                return res.read().strip()
            finally:
                try:
                    res.timeout = old_timeout
                except Exception:
                    pass

        try:
            if interface_lock is None:
                with res_lock:
                    idn = _do_query()
            else:
                with interface_lock:
                    with res_lock:
                        idn = _do_query()
        except Exception as e:
            self._record_unresponsive_failure(visa_address, f"*IDN? failed: {e}")
            return None

        self.clear_unresponsive(visa_address)
        return idn

    def scan_all_hardware(self, scan_serial: bool = False) -> List[Dict[str, str]]:
        """
        Scans all physical VISA ports + any Mock slots, queries *IDN?, and returns results.
        Runs securely without causing blocks.
        """
        discovered: List[Dict[str, str]] = []
        
        physical_resources = self.list_physical_resources()
        for r in physical_resources:
            if not self.is_scannable_resource(r, scan_serial=scan_serial):
                continue

            # 300 ms was too tight: an instrument mid-integration (e.g. NPLC 10) answers
            # well after that, so a present device got logged offline and cooled down.
            idn = self.query_idn(r, timeout_ms=1000, force=False)
            discovered.append({
                "visa_address": r,
                "idn": idn or "Unknown / No Response",
                "type": "physical",
                "status": "online" if idn else "offline"
            })
            
        mock_candidates = [
            "MOCK::DMM::INSTR",
            "MOCK::SCOPE::INSTR",
            "MOCK::GENERIC::HP_53131A::INSTR"
        ]
        for m in mock_candidates:
            idn = self.query_idn(m, timeout_ms=300)
            discovered.append({
                "visa_address": m,
                "idn": idn or "Unknown Mock",
                "type": "mock",
                "status": "online"
            })
            
        return discovered

    def create_fingerprint(self, idn: str) -> str:
        """Constructs an IDN fingerprint string combining model and serial number if present."""
        if not idn:
            return ""
        parts = [p.strip() for p in idn.split(",")]
        model = parts[1] if len(parts) > 1 else parts[0]
        serial = parts[2] if len(parts) > 2 and parts[2] not in ("", "0") else ""
        return f"{model},{serial}" if serial else model




