"""
Bridge between the VXI-11 / LXI emulators and VMSG's real instruments.

The VXI-11 stack was ported from BenchForge, where it answers from a registry of
*simulated* instruments. VMSG's job is the opposite: forward to *physical*
instruments through PyVISA. Fortunately the emulators only need a very small
surface from their backend:

    registry.devices                  mapping of address -> device
    registry.get_device(address)      device or None
    registry.process_command(a, txt)  reply text, or None for a pure write
    device.name                       label used in diagnostics
    device.status_byte_base           STB bits the device asserts on its own
    device.raise_error(code)          record a SCPI error

This module implements exactly that surface on top of ConfigManager (which slot
maps to which VISA resource) and VisaManager (the pooled PyVISA sessions), so
the emulator code itself needed no modification.

Locking note: transactions take the interface lock *outer* and the resource lock
*inner*, matching prologix_server.py. A shared bus such as GPIB is shared with
the Prologix listener too, and reversing this order would deadlock the two
against each other.
"""

from typing import Dict, Optional

from .logger import logger

# Mirrors the SCPI error codes the VXI-11 emulator references. Kept here so the
# ported emulator does not need BenchForge's device_emulator module.
ERR_QUERY_INTERRUPTED = -410
ERR_QUERY_UNTERMINATED = -420

SCPI_ERRORS = {
    ERR_QUERY_INTERRUPTED: ("Query INTERRUPTED", 0x04),
    ERR_QUERY_UNTERMINATED: ("Query UNTERMINATED", 0x04),
}


class VirtualInstrument:
    """Constant holder only - the emulator refers to these as class attributes."""
    ERR_QUERY_INTERRUPTED = ERR_QUERY_INTERRUPTED
    ERR_QUERY_UNTERMINATED = ERR_QUERY_UNTERMINATED
    SCPI_ERRORS = SCPI_ERRORS


class MappedInstrument:
    """A configured VMSG slot, presented the way the emulators expect."""

    def __init__(self, address: int, visa_address: str, description: str = ""):
        self.address = address
        self.visa_address = visa_address
        self.name = description or visa_address
        # Real instruments drive their own status byte; we assert nothing extra.
        self.status_byte_base = 0
        self.last_error: Optional[int] = None

    def raise_error(self, code: int) -> None:
        self.last_error = code
        label = SCPI_ERRORS.get(code, ("Unknown error", 0))[0]
        logger.warning("VXI11", f"Slot {self.address} ({self.name}): {label} ({code})")

    def __repr__(self) -> str:
        return f"<MappedInstrument slot={self.address} visa={self.visa_address!r}>"


class VmsgInstrumentRegistry:
    """Registry facade that resolves slots against the live VMSG configuration.

    Deliberately not cached: mappings can change at runtime through the web UI,
    and a stale registry would route a VXI-11 client to an instrument that has
    since been reassigned.
    """

    def __init__(self, config_manager, visa_manager):
        self.config = config_manager
        self.visa = visa_manager

    # -- surface the emulators use -----------------------------------------

    @property
    def devices(self) -> Dict[int, MappedInstrument]:
        out: Dict[int, MappedInstrument] = {}
        for slot_str, mapping in (self.config.get_mappings() or {}).items():
            try:
                slot = int(slot_str)
            except (TypeError, ValueError):
                continue
            visa_address = (mapping or {}).get("visa_address", "")
            if visa_address:
                out[slot] = MappedInstrument(
                    slot, visa_address, (mapping or {}).get("description", ""))
        return out

    def get_device(self, gpib_address: int) -> Optional[MappedInstrument]:
        mapping = self.config.get_mapping(gpib_address)
        if not mapping:
            return None
        visa_address = mapping.get("visa_address", "")
        if not visa_address:
            return None
        return MappedInstrument(gpib_address, visa_address,
                                mapping.get("description", ""))

    def process_command(self, gpib_address: int, raw_cmd: str) -> Optional[str]:
        """Execute one SCPI exchange against the mapped instrument.

        Returns the reply for a query, or None for a write (and for any failure,
        which the emulator surfaces to the client as a timeout - the same thing a
        real instrument that never answers would produce).
        """
        device = self.get_device(gpib_address)
        if device is None:
            logger.warning("VXI11", f"No instrument mapped to slot {gpib_address}")
            return None

        command = (raw_cmd or "").strip()
        if not command:
            return None

        visa_address = device.visa_address
        timeout_ms = int(self.config.get_setting("read_tmo_ms", 3000))
        is_query = "?" in command

        try:
            resource, res_lock = self.visa.get_resource(visa_address,
                                                        timeout_ms=timeout_ms)
        except Exception as e:
            logger.error("VXI11",
                         f"Slot {gpib_address}: cannot reach {visa_address}: {e}")
            return None

        is_mock = visa_address.upper().startswith("MOCK::")
        interface_lock = None if is_mock else self.visa.get_interface_lock(visa_address)

        def _transaction():
            resource.write(command)
            return resource.read() if is_query else None

        try:
            if interface_lock is None:
                with res_lock:
                    reply = _transaction()
            else:
                with interface_lock:
                    with res_lock:
                        reply = _transaction()
        except Exception as e:
            logger.error("VXI11",
                         f"Slot {gpib_address}: {command!r} failed on "
                         f"{visa_address}: {e}")
            return None

        if reply is None:
            return None
        return reply.rstrip("\r\n")
