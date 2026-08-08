import time
import threading
import random
import re
import asyncio
from typing import Dict, Any, List, Optional, Tuple
import pyvisa
from pyvisa.errors import VisaIOError
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
    """Manages active PyVISA connections, resource pooling, and auto-healing."""
    def __init__(self):
        self.lock = threading.Lock()
        self.global_visa_lock = threading.Lock()  # Hardware bus fallback lock
        self.interface_locks: Dict[str, threading.Lock] = {}  # Per-interface lock (e.g. GPIB0, TCPIP0)
        self.resource_cache: Dict[str, Any] = {}
        self.resource_locks: Dict[str, threading.Lock] = {}
        self.unresponsive_cache: Dict[str, float] = {}  # visa_address -> last_fail_timestamp
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

    def get_resource(self, visa_address: str, timeout_ms: int = 3000) -> Tuple[Any, Optional[threading.Lock]]:
        """
        Retrieves a thread-safe connection to the requested VISA address.
        If address starts with 'MOCK::', returns a simulated resource.
        NOTE: Must be called from a worker thread or via async_get_resource on event loop.
        """
        if not visa_address:
            raise ValueError("Empty VISA address")

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

            if visa_address.upper().startswith("MOCK::"):
                resource = MockVisaResource(visa_address)
                resource.timeout = timeout_ms
                self.resource_cache[visa_address] = resource
                logger.info("VISAMANAGER", f"Created cached Mock resource: {visa_address}")
                return resource, res_lock

            if not self.rm:
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
                self.resource_cache[visa_address] = resource
                logger.info("VISAMANAGER", f"Connected and cached physical resource: {visa_address}")
                return resource, res_lock
            except Exception as e:
                logger.error("VISAMANAGER", f"Error opening VISA resource {visa_address}: {e}")
                raise

    async def async_get_resource(self, visa_address: str, timeout_ms: int = 3000) -> Tuple[Any, Optional[threading.Lock]]:
        """Asynchronously acquires resource, offloading blocking open_resource calls off the main event loop."""
        return await asyncio.to_thread(self.get_resource, visa_address, timeout_ms)

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

    def is_scannable_resource(self, addr: str) -> bool:
        """Helper to filter out raw board interfaces, secondary GPIB addresses, and non-instrument endpoints."""
        u = addr.upper()
        if u.endswith("::INTFC") or u.endswith("::RAW"):
            return False
        if u.startswith("GPIB") and len(addr.split("::")) > 3:
            return False
        if "USB" in u and "::0::INSTR" in u:
            return False
        return True

    def query_idn(self, visa_address: str, timeout_ms: int = 1500, force: bool = False) -> Optional[str]:
        """Queries *IDN? of a resource with safety, returning None if failure."""
        is_mock = visa_address.upper().startswith("MOCK::")
        
        if not is_mock and not force:
            with self.unresponsive_lock:
                now = time.time()
                if visa_address in self.unresponsive_cache:
                    last_fail = self.unresponsive_cache[visa_address]
                    if now - last_fail < 120.0:
                        return None

        try:
            res, res_lock = self.get_resource(visa_address, timeout_ms=timeout_ms)
        except Exception:
            if not is_mock:
                with self.unresponsive_lock:
                    self.unresponsive_cache[visa_address] = time.time()
            return None

        with res_lock:
            try:
                old_timeout = getattr(res, "timeout", timeout_ms)
                try:
                    res.timeout = timeout_ms
                except Exception:
                    pass
                
                res.write("*IDN?")
                idn = res.read().strip()
                
                try:
                    res.timeout = old_timeout
                except Exception:
                    pass
                
                if not is_mock:
                    with self.unresponsive_lock:
                        self.unresponsive_cache.pop(visa_address, None)
                        
                return idn
            except Exception:
                if not is_mock:
                    with self.unresponsive_lock:
                        self.unresponsive_cache[visa_address] = time.time()
                return None

    def scan_all_hardware(self) -> List[Dict[str, str]]:
        """
        Scans all physical VISA ports + any Mock slots, queries *IDN?, and returns results.
        Runs securely without causing blocks.
        """
        discovered: List[Dict[str, str]] = []
        
        physical_resources = self.list_physical_resources()
        for r in physical_resources:
            if not self.is_scannable_resource(r):
                continue

            idn = self.query_idn(r, timeout_ms=300, force=False)
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

    def heal_mappings(self, current_mappings: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        USB Lottery Healing:
        Compares expected IDN patterns against active devices. Refuses ambiguous
        healing if multiple online devices match the pattern.
        """
        healing_actions: List[Dict[str, Any]] = []
        if not current_mappings:
            return healing_actions

        scanned_devices = self.scan_all_hardware()
        scanned_online = [d for d in scanned_devices if d["status"] == "online" and d["idn"]]
        scanned_by_addr = {d["visa_address"]: d["idn"] for d in scanned_online}

        for addr_str, mapping in current_mappings.items():
            expected_visa_addr = mapping.get("visa_address", "")
            idn_pattern = mapping.get("idn_pattern", "").strip()
            description = mapping.get("description", "")

            if not idn_pattern:
                continue

            # Look up existing scan result first to avoid redundant query
            current_idn = scanned_by_addr.get(expected_visa_addr)
            if not current_idn:
                current_idn = self.query_idn(expected_visa_addr, timeout_ms=500)

            if current_idn and idn_pattern.lower() in current_idn.lower():
                continue

            matches = [
                dev for dev in scanned_online
                if idn_pattern.lower() in dev["idn"].lower()
            ]
            
            if len(matches) > 1:
                logger.warning("HEALER", f"Healing skipped for slot {addr_str}: IDN pattern '{idn_pattern}' matches multiple active devices.")
                continue

            if len(matches) == 1:
                found_dev = matches[0]
                found_new_address = found_dev["visa_address"]
                if found_new_address != expected_visa_addr:
                    healing_actions.append({
                        "virtual_address": int(addr_str),
                        "description": description,
                        "idn_pattern": idn_pattern,
                        "old_visa_address": expected_visa_addr,
                        "new_visa_address": found_new_address,
                        "matched_idn": found_dev["idn"]
                    })

        return healing_actions


