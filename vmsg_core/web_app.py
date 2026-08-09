import os
import asyncio
import re
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Body, APIRouter
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config_manager import ConfigManager
from .visa_manager import VisaManager
from .logger import logger
from .path_helper import get_resource_path

class MappingModel(BaseModel):
    visa_address: str = Field(..., description="The physical or mock VISA resource string")
    idn_pattern: str = Field("", description="Identity substring from the instrument's *IDN? response")
    description: str = Field("", description="Human-readable label for the instrument")
    listen_port: Optional[int] = Field(None, ge=1025, le=65000, description="Dedicated TCP listener port for this slot (multi-port mode)")

class SettingsModel(BaseModel):
    addr: Optional[int] = Field(None, ge=0, le=30)
    auto: Optional[int] = Field(None, ge=0, le=1)
    mode: Optional[int] = Field(None, ge=0, le=1)
    eos: Optional[int] = Field(None, ge=0, le=3)
    eoi: Optional[int] = Field(None, ge=0, le=1)
    read_tmo_ms: Optional[int] = Field(None, gt=0)
    eot_enable: Optional[int] = Field(None, ge=0, le=1)
    eot_char: Optional[int] = Field(None, ge=0, le=255)
    lon: Optional[int] = Field(None, ge=0, le=1)
    savecfg: Optional[int] = Field(None, ge=0, le=1)
    unmapped_behavior: Optional[str] = Field(None, description="Behavior for unmapped virtual addresses: 'message' or 'timeout'")
    scan_serial_ports: Optional[bool] = Field(None, description="Toggle PyVISA scanning of ASRL serial/COM ports")
    tc_enable_driver_validation: Optional[bool] = Field(None, description="Optionally validate TestController driver names against Devices folder or stock list")
    tc_scan_serial_ports: Optional[bool] = Field(None, description="Emit ScanSerialPorts:1 in the TestController export")
    tc_excluded_serial_ports: Optional[str] = Field(None, description="Comma-separated host serial ports TestController should skip")
    tc_force_addr: Optional[bool] = Field(None, description="Emit settings:++addr N so TestController re-addresses on every reconnect")
    tc_devices_path: Optional[str] = Field(None, description="Path to the TestController Devices folder, used to validate driver names")
    multi_port_enabled: Optional[bool] = Field(None, description="Give mapped slots their own TCP listener ports")
    multi_port_base: Optional[int] = Field(None, ge=1025, le=65000, description="First port used when auto-allocating dedicated ports")
    log_level: Optional[str] = Field(None, description="Logging verbosity (DEBUG, INFO, WARN, ERROR)")
    enable_stdout: Optional[bool] = Field(None, description="Toggle standard output console printing")
    log_category_traffic: Optional[bool] = Field(None, description="Toggle traffic logs")
    log_category_visa: Optional[bool] = Field(None, description="Toggle PyVISA interaction logs")
    log_category_system: Optional[bool] = Field(None, description="Toggle main loop and system logs")

class ConsoleCommandModel(BaseModel):
    command: str = Field(..., description="Prologix (starts with ++) or instrument SCPI command")
    virtual_address: Optional[int] = Field(None, ge=0, le=30, description="Virtual address to send command to (uses default if empty)")

class AutoAssignModel(BaseModel):
    force_overwrite: bool = Field(False, description="Whether to overwrite existing slot mappings")
    include_mocks: bool = Field(True, description="Whether to include simulated mock devices in auto-assignment")
    assign_ports: bool = Field(False, description="Also give each assigned slot its own dedicated listener port")

# TestController driver names that ship with stock TestController. The exporter must never
# emit a name outside this set: InterfaceThreads.addDevicesShared() uses `break` (not
# `continue`) when a driver is unknown, so one bad name silently drops every device after
# it. See TESTCONTROLLER_NOTES.md.
KNOWN_TC_DRIVERS = {
    "Agilent 34401A", "Hewlett-Packard 34401A", "Agilent 34410A", "Agilent 34411A",
    "Keysight 34460A", "Keysight 34461A", "Keysight 34465A", "Agilent 3458A",
    "Keithley 2000", "Keithley 2001", "Keithley 2001M", "Keithley 2002",
    "Keithley 2010", "Keithley 2015", "Keithley 199", "HP3478A",
    "Fluke 45", "Fluke 8845A", "Fluke PM6685", "Fluke PM6690",
    "Agilent 53131A", "Agilent 53132A", "Agilent 53181A",
    "Agilent 33250A", "Keysight 33250A",
    "HP E3632A", "HP E3633A", "HP E3634A",
    "Siglent SDM3055", "Siglent SDM3065X", "R&S HMC8012",
}


def create_app(config: ConfigManager, visa: VisaManager, socket_server=None) -> FastAPI:
    """Creates and configures the FastAPI application instance."""
    app = FastAPI(
        title="VISA Mapping TCP/IP Socket Gateway (VMSG)",
        description="A premium web dashboard and API for managing the VISA Mapping TCP/IP Socket Gateway, implementing Prologix compatible control.",
        version="1.0.0"
    )

    # Store references in app.state
    app.state.config = config
    app.state.visa = visa
    app.state.socket_server = socket_server

    # Allow CORS for easy debugging
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API Router
    api = APIRouter(prefix="/api")

    # 1. Status Endpoints
    @api.get("/status")
    def get_status():
        socket_active = False
        client_count = 0
        active_connections_list = []
        sessions_info = []
        if app.state.socket_server:
            socket_active = app.state.socket_server.is_running
            client_count = len(app.state.socket_server.active_connections)
            active_connections_list = [f"{c[0]}:{c[1]}" for c in app.state.socket_server.active_connections]
            if hasattr(app.state.socket_server, "client_sessions"):
                for client_peer, sess in list(app.state.socket_server.client_sessions.items()):
                    sessions_info.append({
                        "peer": f"{client_peer[0]}:{client_peer[1]}",
                        "addr": sess.get("addr", 1),
                        "auto": sess.get("auto", 0),
                        "mode": sess.get("mode", 1),
                        "read_tmo_ms": sess.get("read_tmo_ms", 3000)
                    })

        settings = app.state.config.get_settings()
        current_addr = settings["addr"]
        current_mapping = app.state.config.get_mapping(current_addr)

        return {
            "status": "online",
            "socket_server": {
                "active": socket_active,
                "port": 1234,
                "client_count": client_count,
                "clients": active_connections_list,
                "sessions": sessions_info
            },
            "prologix_settings": settings,
            "active_instrument": {
                "address": current_addr,
                "mapping": current_mapping
            }
        }

    # 2. Config & Mapping Endpoints
    @api.get("/mappings")
    def get_mappings():
        return app.state.config.get_mappings()

    async def _apply_port_bindings() -> None:
        """Re-syncs dedicated listeners after anything that can change them."""
        srv = app.state.socket_server
        if srv:
            try:
                await srv.sync_port_listeners()
            except Exception as e:
                logger.warning("WEB_API", f"Could not sync dedicated listener ports: {e}")

    @api.put("/mappings/{address}")
    async def update_mapping(address: int, data: MappingModel):
        if not (0 <= address <= 30):
            raise HTTPException(status_code=400, detail="Address must be between 0 and 30")
        try:
            app.state.config.set_mapping(
                address=address,
                visa_address=data.visa_address,
                idn_pattern=data.idn_pattern,
                description=data.description,
                listen_port=data.listen_port
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        logger.info("WEB_API", f"Updated virtual address {address} mapping -> {data.visa_address}")
        await _apply_port_bindings()
        return {"status": "success", "message": f"Mapping for address {address} updated."}

    @api.delete("/mappings/{address}")
    async def delete_mapping(address: int):
        if not (0 <= address <= 30):
            raise HTTPException(status_code=400, detail="Address must be between 0 and 30")
        deleted = app.state.config.delete_mapping(address)
        if deleted:
            logger.info("WEB_API", f"Deleted virtual address {address} mapping")
            await _apply_port_bindings()
            return {"status": "success", "message": f"Mapping for address {address} deleted."}
        else:
            raise HTTPException(status_code=404, detail=f"No mapping found for address {address}")

    @api.delete("/mappings")
    async def clear_all_mappings(confirm: bool = False):
        if not confirm:
            raise HTTPException(status_code=400, detail="Confirmation required to clear all mappings. Pass ?confirm=true")
        try:
            app.state.config.clear_all_mappings()
            logger.info("WEB_API", "Cleared all virtual address mappings")
            await _apply_port_bindings()
            return {"status": "success", "message": "All virtual address mappings cleared."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @api.post("/cooldown/clear")
    def clear_cooldown():
        """Clears the unresponsive resource cooldown cache."""
        try:
            with app.state.visa.unresponsive_lock:
                app.state.visa.unresponsive_cache.clear()
            logger.info("WEB_API", "Cleared unresponsive resource cooldown cache.")
            return {"status": "success", "message": "Unresponsive cooldown cache cleared."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # 3. Settings Endpoints
    @api.get("/settings")
    def get_settings():
        return app.state.config.get_settings()

    @api.post("/settings")
    async def update_settings(data: SettingsModel):
        try:
            # Filter None fields
            updates = {k: v for k, v in data.model_dump().items() if v is not None}
            if updates:
                app.state.config.update_settings(updates)
                logger.info("WEB_API", f"Prologix settings updated: {updates}")
                if "multi_port_enabled" in updates or "multi_port_base" in updates:
                    await _apply_port_bindings()
            return {"status": "success", "settings": app.state.config.get_settings()}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @api.get("/config/backup")
    def download_config_backup():
        """Returns the entire mappings.json persistent config as a downloadable attachment."""
        try:
            config_data = app.state.config.config
            headers = {
                "Content-Disposition": "attachment; filename=vmsg_config_backup.json"
            }
            return JSONResponse(content=config_data, headers=headers)
        except Exception as e:
            logger.error("WEB_API", f"Config backup download failed: {e}")
            raise HTTPException(status_code=500, detail=f"Backup failed: {e}")

    @api.post("/config/restore")
    async def restore_config_backup(data: Dict[str, Any] = Body(...)):
        """Restores full settings and virtual mappings from a backup JSON payload."""
        try:
            if "settings" not in data or "mappings" not in data:
                raise HTTPException(status_code=400, detail="Invalid config payload. Must contain 'settings' and 'mappings' keys.")
            
            # Update ConfigManager's internal config dictionary
            restored_mappings = {}
            for addr_str, m_data in data.get("mappings", {}).items():
                try:
                    addr_val = int(addr_str)
                    if 0 <= addr_val <= 30:
                        entry = {
                            "visa_address": str(m_data.get("visa_address", "")),
                            "idn_pattern": str(m_data.get("idn_pattern", "")),
                            "description": str(m_data.get("description", ""))
                        }
                        port = m_data.get("listen_port")
                        if port is not None:
                            try:
                                port = int(port)
                                if 1025 <= port <= 65000 and port != 1234:
                                    entry["listen_port"] = port
                            except (TypeError, ValueError):
                                pass
                        restored_mappings[str(addr_val)] = entry
                except ValueError:
                    continue

            # Apply settings & mappings
            if isinstance(data.get("settings"), dict):
                app.state.config.update_settings(data["settings"], persist=False)

            app.state.config.config["mappings"] = restored_mappings
            app.state.config.save_config()

            # Dynamically propagate logging config to logger
            logger.configure(app.state.config.get_settings())
            await _apply_port_bindings()

            logger.info("WEB_API", "Configuration successfully restored from uploaded JSON backup.")
            return {
                "status": "success",
                "settings": app.state.config.get_settings(),
                "mappings": app.state.config.get_mappings()
            }
        except Exception as e:
            logger.error("WEB_API", f"Config restore failed: {e}")
            raise HTTPException(status_code=500, detail=f"Restore failed: {e}")

    # 4. VISA Hardware Discovery
    @api.get("/scan")
    def scan_hardware():
        """Scans all connected instruments and queries *IDN? responses."""
        try:
            logger.info("WEB_API", "Triggering PyVISA hardware scanning...")
            scan_serial = app.state.config.get_setting("scan_serial_ports", False)
            devices = app.state.visa.scan_all_hardware(scan_serial=scan_serial)
            logger.info("WEB_API", f"Hardware scan finished. Discovered {len(devices)} targets.")
            return devices
        except Exception as e:
            logger.error("WEB_API", f"Hardware scanning failed: {e}")
            raise HTTPException(status_code=500, detail=f"Scan failed: {e}")

    @api.post("/auto_assign")
    async def auto_assign_devices(data: AutoAssignModel):
        """
        Scans all online hardware/mock devices and auto-assigns them to
        available virtual GPIB addresses (0-30). Preserves offline device
        mappings and fingerprints even when force_overwrite is True.
        """
        try:
            force_overwrite = data.force_overwrite
            include_mocks = data.include_mocks
            assign_ports = data.assign_ports
            logger.info("WEB_API", f"Auto-assignment triggered. Force overwrite: {force_overwrite}, Include Mocks: {include_mocks}, Assign ports: {assign_ports}")

            def _port_for(slot: int) -> Optional[int]:
                """Keeps a slot's existing dedicated port; allocates one only when asked."""
                existing = current_mappings.get(str(slot), {}).get("listen_port")
                if existing:
                    return int(existing)
                return app.state.config.next_free_port() if assign_ports else None
            
            # Scan connected hardware
            scan_serial = app.state.config.get_setting("scan_serial_ports", False)
            scanned = app.state.visa.scan_all_hardware(scan_serial=scan_serial)
            online_devices = [d for d in scanned if d.get("status") == "online" and d.get("idn") and "Unknown" not in d.get("idn", "")]
            
            if not include_mocks:
                online_devices = [d for d in online_devices if d.get("type") != "mock"]
            
            if not online_devices:
                return {
                    "status": "success",
                    "assigned_count": 0,
                    "message": "No online instruments found to assign.",
                    "mappings": app.state.config.get_mappings()
                }

            current_mappings = app.state.config.get_mappings()
            mapped_visa_addrs = {m["visa_address"]: addr for addr, m in current_mappings.items()}
            
            assigned_actions = []
            
            for dev in online_devices:
                v_addr = dev["visa_address"]
                idn = dev["idn"]
                
                # Skip if already mapped unless force_overwrite is True
                if v_addr in mapped_visa_addrs and not force_overwrite:
                    continue
                
                desc = idn.split(",")[1].strip() if len(idn.split(",")) > 1 else idn.split(",")[0].strip()
                desc = desc[:25]
                fingerprint = app.state.visa.create_fingerprint(idn)

                # If force_overwrite is True and device is already mapped, update its slot in-place
                if v_addr in mapped_visa_addrs and force_overwrite:
                    assigned_slot = int(mapped_visa_addrs[v_addr])
                    port = _port_for(assigned_slot)
                    app.state.config.set_mapping(
                        address=assigned_slot,
                        visa_address=v_addr,
                        idn_pattern=fingerprint,
                        description=desc,
                        listen_port=port
                    )
                    assigned_actions.append({
                        "virtual_address": assigned_slot,
                        "visa_address": v_addr,
                        "description": desc,
                        "listen_port": port,
                        "overwritten": True
                    })
                    current_mappings[str(assigned_slot)] = {
                        "visa_address": v_addr,
                        "idn_pattern": fingerprint,
                        "description": desc,
                        "listen_port": port
                    }
                    continue

                # Otherwise find lowest available virtual slot (1-30)
                assigned_slot = None
                for candidate in range(1, 31):
                    cand_str = str(candidate)
                    if cand_str not in current_mappings:
                        assigned_slot = candidate
                        break
                
                if assigned_slot is None:
                    # No more available virtual slots
                    break
                    
                cand_str = str(assigned_slot)
                port = _port_for(assigned_slot)
                app.state.config.set_mapping(
                    address=assigned_slot,
                    visa_address=v_addr,
                    idn_pattern=fingerprint,
                    description=desc,
                    listen_port=port
                )

                assigned_actions.append({
                    "virtual_address": assigned_slot,
                    "visa_address": v_addr,
                    "description": desc,
                    "listen_port": port,
                    "overwritten": False
                })

                current_mappings[cand_str] = {
                    "visa_address": v_addr,
                    "idn_pattern": fingerprint,
                    "description": desc,
                    "listen_port": port
                }
                mapped_visa_addrs[v_addr] = assigned_slot

            if assign_ports:
                await _apply_port_bindings()

            return {
                "status": "success",
                "assigned_count": len(assigned_actions),
                "actions": assigned_actions,
                "mappings": app.state.config.get_mappings()
            }
        except Exception as e:
            logger.error("WEB_API", f"Auto-assignment failed: {e}")
            raise HTTPException(status_code=500, detail=f"Auto-assignment failed: {e}")

    def _load_tc_driver_names() -> tuple:
        """Valid TestController driver names, read from the user's Devices folder when
        configured so the check tracks their actual install; returns None if driver
        validation is disabled."""
        if not bool(app.state.config.get_setting("tc_enable_driver_validation", False)):
            return None, "disabled"

        path = (app.state.config.get_setting("tc_devices_path", "") or "").strip()
        if path and os.path.isdir(path):
            names = set()
            try:
                for entry in os.listdir(path):
                    if not entry.lower().endswith(".txt"):
                        continue
                    with open(os.path.join(path, entry), "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if line.startswith("#name"):
                                name = line[5:].strip()
                                if name:
                                    names.add(name)
            except Exception as e:
                logger.warning("WEB_API", f"Could not read TestController Devices folder '{path}': {e}")
                return KNOWN_TC_DRIVERS, "builtin"
            if names:
                return names, path
        return KNOWN_TC_DRIVERS, "builtin"

    def _map_to_testcontroller_driver(idn: str, visa_address: str, description: str) -> Optional[str]:
        """Maps an instrument to a stock TestController driver name, or None when unknown."""
        combined = f"{idn} {description} {visa_address}".upper()

        if "34401A" in combined:
            return "Agilent 34401A"
        if "34411A" in combined:
            return "Agilent 34411A"
        if "34410A" in combined:
            return "Agilent 34410A"
        if "34461A" in combined:
            return "Keysight 34461A"
        if "34465A" in combined:
            return "Keysight 34465A"
        if "34460A" in combined:
            return "Keysight 34460A"
        if "3458A" in combined:
            return "Agilent 3458A"
        if "3478A" in combined:
            return "HP3478A"
        if "2001M" in combined:
            return "Keithley 2001M"
        if "2002" in combined:
            return "Keithley 2002"
        if "2001" in combined:
            return "Keithley 2001"
        if "2010" in combined:
            return "Keithley 2010"
        if "2015" in combined:
            return "Keithley 2015"
        if "MODEL 2000" in combined or "KEITHLEY 2000" in combined:
            return "Keithley 2000"
        if "PM6690" in combined:
            return "Fluke PM6690"
        if "PM6685" in combined:
            return "Fluke PM6685"
        if "53131A" in combined:
            return "Agilent 53131A"
        if "53132A" in combined:
            return "Agilent 53132A"
        if "53181A" in combined:
            return "Agilent 53181A"
        if "33250A" in combined:
            return "Agilent 33250A"
        if "E3632A" in combined:
            return "HP E3632A"
        if "E3633A" in combined:
            return "HP E3633A"
        if "E3634A" in combined:
            return "HP E3634A"
        if "SDM3065" in combined:
            return "Siglent SDM3065X"
        if "SDM3055" in combined:
            return "Siglent SDM3055"
        if "HMC8012" in combined:
            return "R&S HMC8012"
        return None

    # 4b. TestController Subtool Configuration Generator
    @api.get("/testcontroller/config")
    def get_testcontroller_config(controller_id: str = "A", host: str = "127.0.0.1",
                                  separate_adapters: bool = True, use_ports: Optional[bool] = None):
        """
        Generates settingsGPIB.txt and settingsLoad.txt for TestController from the
        active VMSG mappings.
        """
        try:
            cfg = app.state.config
            mappings = cfg.get_mappings()
            settings = cfg.get_settings()
            ctrl_id_base = controller_id.strip().upper() if controller_id.strip() else "A"
            gw_host = host.strip() if host.strip() else "127.0.0.1"

            valid_drivers, driver_source = _load_tc_driver_names()
            force_addr = bool(settings.get("tc_force_addr", True))
            if use_ports is None:
                use_ports = bool(settings.get("multi_port_enabled", False))

            scan_serial = "1" if settings.get("tc_scan_serial_ports", False) else "0"
            excluded = (settings.get("tc_excluded_serial_ports", "") or "").strip()

            gpib_lines = []
            load_lines = [f"ScanSerialPorts:{scan_serial}", f"ExcludedSerialPorts:{excluded}"]

            mapped_devices, excluded_devices = [], []
            sorted_slots = sorted([int(k) for k in mappings.keys() if int(k) != 0])
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

            ctrl_index = 0
            for slot_int in sorted_slots:
                m = mappings[str(slot_int)]
                visa_addr = m.get("visa_address", "")
                idn = m.get("idn_pattern", "")
                desc = m.get("description", "")

                driver = _map_to_testcontroller_driver(idn, visa_addr, desc)
                if valid_drivers is not None:
                    if driver is None or driver not in valid_drivers:
                        excluded_devices.append({
                            "address": slot_int,
                            "visa_address": visa_addr,
                            "description": desc,
                            "idn": idn,
                            "reason": ("No matching TestController driver" if driver is None
                                       else f"Driver '{driver}' not present in {driver_source}")
                        })
                        continue
                else:
                    # Driver validation disabled: export all mapped devices cleanly
                    if not driver:
                        driver = desc or idn or f"Device_Slot_{slot_int}"

                if separate_adapters:
                    device_ctrl_id = alphabet[ctrl_index % len(alphabet)]
                    ctrl_index += 1
                else:
                    device_ctrl_id = ctrl_id_base

                # The settings: field carries a port override and any ++ commands to send
                # at init. init() runs on every (re)connect, so ++addr here is what makes
                # TestController re-address after a menu Reconnect.
                parts = []
                dedicated_port = m.get("listen_port") if use_ports else None
                if dedicated_port:
                    parts.append(f"port:{int(dedicated_port)}")
                if force_addr:
                    parts.append(f"++addr {slot_int}")
                settings_field = ";".join(parts)

                if separate_adapters:
                    gpib_lines.append(
                        f"PrologixEthernet|id:{device_ctrl_id}|address:{gw_host}|baudrate:|settings:{settings_field}|")

                load_lines.append(
                    f"Device:{driver}|PortType:GPIB|Address:{device_ctrl_id}:{slot_int}|Baudrate:9600|Enabled:1")
                mapped_devices.append({
                    "address": slot_int,
                    "controller_id": device_ctrl_id,
                    "driver": driver,
                    "visa_address": visa_addr,
                    "idn": idn,
                    "description": desc,
                    "listen_port": dedicated_port
                })

            if not separate_adapters or not gpib_lines:
                # One controller serving every slot cannot carry a per-device ++addr,
                # so its settings field stays empty.
                gpib_text = f"PrologixEthernet|id:{ctrl_id_base}|address:{gw_host}|baudrate:|settings:|\n"
            else:
                gpib_text = "\n".join(gpib_lines) + "\n"

            return {
                "status": "success",
                "controller_id": ctrl_id_base,
                "host": gw_host,
                "separate_adapters": separate_adapters,
                "force_addr": force_addr,
                "use_ports": use_ports,
                "driver_source": driver_source,
                "scan_serial_ports": scan_serial == "1",
                "excluded_serial_ports": excluded,
                "settingsGPIB": gpib_text,
                "settingsLoad": "\n".join(load_lines) + "\n",
                "mapped_devices": mapped_devices,
                "excluded_devices": excluded_devices
            }
        except Exception as e:
            logger.error("WEB_API", f"TestController config generation failed: {e}")
            raise HTTPException(status_code=500, detail=f"Generation failed: {e}")

    # 4c. Host serial port discovery (for the TestController exclusion list)
    @api.get("/host/serial_ports")
    def list_host_serial_ports():
        """Lists the host's serial ports so the user can choose which TestController skips."""
        try:
            from serial.tools import list_ports
        except ImportError:
            return {"status": "unavailable", "detail": "pyserial is not installed", "ports": []}

        excluded_raw = (app.state.config.get_setting("tc_excluded_serial_ports", "") or "")
        excluded = {p.strip().upper() for p in excluded_raw.split(",") if p.strip()}
        ports = []
        for p in list_ports.comports():
            ports.append({
                "device": p.device,
                "description": p.description or "",
                "hwid": p.hwid or "",
                "excluded": p.device.upper() in excluded
            })
        ports.sort(key=lambda x: x["device"])
        return {"status": "success", "ports": ports,
                "scan_enabled": bool(app.state.config.get_setting("tc_scan_serial_ports", False))}

    # 4d. Dedicated listener port management
    @api.get("/ports")
    def get_listener_ports():
        """Reports dedicated per-slot listener ports: configured vs actually bound."""
        srv = app.state.socket_server
        return {
            "status": "success",
            "multi_port_enabled": bool(app.state.config.get_setting("multi_port_enabled", False)),
            "multi_port_base": app.state.config.get_setting("multi_port_base", 1235),
            "configured": {str(port): slot for port, slot in app.state.config.get_port_bindings().items()},
            "bound": {str(port): slot for port, slot in (srv.port_slot_map.items() if srv else [])},
            "control_port": 1234
        }

    @api.post("/ports/sync")
    async def sync_listener_ports():
        """Applies the configured dedicated ports to the running socket server."""
        srv = app.state.socket_server
        if not srv:
            raise HTTPException(status_code=503, detail="Socket server is not available")
        result = await srv.sync_port_listeners()
        if result["failed"]:
            logger.warning("WEB_API", f"Some dedicated ports could not be bound: {result['failed']}")
        return {"status": "success", **result}

    # 5. Interactive Console Terminal
    @api.post("/send_command")
    async def send_console_command(data: ConsoleCommandModel):
        """Allows direct user entry of Prologix/SCPI commands and returns responses."""
        command = data.command.strip()
        if not command:
            raise HTTPException(status_code=400, detail="Empty command")

        # Capture virtual address context
        settings = app.state.config.get_settings()
        target_addr = data.virtual_address or settings["addr"]
        
        # Handle Prologix configurations
        if command.startswith("++"):
            parts = command[2:].strip().split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else None

            # Execute Prologix command using socket parser's engine
            if app.state.socket_server:
                resp = await app.state.socket_server.execute_prologix_cmd(cmd, arg, ("WEB_CONSOLE", 0))
                return {
                    "source": "prologix_controller",
                    "command": command,
                    "response": resp or "OK"
                }
            else:
                raise HTTPException(status_code=503, detail="Socket server is not fully initialized.")

        # Route regular device query/command
        mapping = app.state.config.get_mapping(target_addr)
        if not mapping:
            raise HTTPException(status_code=404, detail=f"Address {target_addr} is not mapped to any physical resource.")

        visa_addr = mapping.get("visa_address")
        if not visa_addr:
            raise HTTPException(status_code=400, detail=f"Address {target_addr} has an empty VISA resource string.")

        try:
            res, res_lock = app.state.visa.get_resource(visa_addr, timeout_ms=settings["read_tmo_ms"])
            interface_lock = app.state.visa.get_interface_lock(visa_addr)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not connect to {visa_addr}: {e}")

        # Execute on thread to avoid blocking FastAPI event loop
        def _exec_write():
            with interface_lock:
                with res_lock:
                    res.write(command)

        def _exec_read():
            with interface_lock:
                with res_lock:
                    return res.read()

        try:
            logger.info("CONSOLE", f"Sending command '{command}' to slot {target_addr} ({visa_addr})")
            await asyncio.to_thread(_exec_write)
            
            # If it's a query or user wants to read response
            is_query = "?" in command
            response_data = ""
            
            if is_query:
                response_data = await asyncio.to_thread(_exec_read)
                logger.info("CONSOLE", f"Received response from slot {target_addr}: '{response_data.strip()}'")

            return {
                "source": f"instrument_slot_{target_addr}",
                "visa_address": visa_addr,
                "command": command,
                "is_query": is_query,
                "response": response_data.strip() if is_query else "Command sent successfully (no read query executed)"
            }
        except Exception as e:
            if not visa_addr.upper().startswith("MOCK::"):
                app.state.visa.purge_resource(visa_addr)
            raise HTTPException(status_code=500, detail=f"Communication failed: {e}")

    # 6. Stream Live Logs
    @api.get("/logs")
    def get_logs(since: int = 0):
        return logger.get_logs(since_id=since)


    @api.get("/snoop/{address}")
    def snoop_traffic(address: int):
        """Returns logs filtered specifically for the given virtual address or its mapped physical VISA address."""
        if not (0 <= address <= 30):
            raise HTTPException(status_code=400, detail="Address must be between 0 and 30")
        
        mapping = app.state.config.get_mapping(address)
        if not mapping:
            return []
            
        visa_addr = mapping.get("visa_address", "")
        if not visa_addr:
            return []
            
        # Get all logs
        all_logs = logger.get_logs()
        
        # Filter logs using word boundaries for target slot/address
        filtered = []
        slot_pattern = re.compile(r'\b(slot|address|addr)\s+' + str(address) + r'\b', re.IGNORECASE)
        for entry in all_logs:
            msg = entry.get("message", "")
            
            is_match = False
            if visa_addr.lower() in msg.lower():
                is_match = True
            elif slot_pattern.search(msg):
                is_match = True
                
            if is_match:
                filtered.append(entry)
                
        return filtered

    @api.delete("/logs")
    def clear_logs():
        logger.clear()
        return {"status": "success", "message": "Logs cleared."}

    # 7. System Administration
    @api.post("/system/stop")
    async def stop_system():
        """Stops the emulator cleanly after responding to the client."""
        logger.info("WEB_API", "User requested emulator stop from administrative interface.")
        
        async def _delayed_stop():
            await asyncio.sleep(0.5)
            import os
            import signal
            os.kill(os.getpid(), signal.SIGINT)
            
        asyncio.create_task(_delayed_stop())
        return {"status": "success", "message": "Emulator stop sequence initiated."}

    @api.post("/system/restart")
    async def restart_system():
        """Restarts the emulator cleanly after responding to the client."""
        logger.info("WEB_API", "User requested emulator restart from administrative interface.")
        
        async def _delayed_restart():
            await asyncio.sleep(0.5)
            logger.info("MAIN", "Performing clean restart...")
            if app.state.socket_server:
                await app.state.socket_server.stop()
            app.state.visa.purge_all_resources()
            
            import sys
            import subprocess
            import os
            args = [sys.executable] + sys.argv
            subprocess.Popen(args)
            os._exit(0)
            
        asyncio.create_task(_delayed_restart())
        return {"status": "success", "message": "Emulator restart sequence initiated."}

    # Mount API Router
    app.include_router(api)

    # Setup static directory serving via get_resource_path for binary extraction support
    static_dir = get_resource_path("static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)

    # Route root request directly to index.html
    @app.get("/")
    def serve_dashboard():
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        else:
            return {
                "error": "static/index.html is missing. Setup static folder.",
                "status": "online",
                "api_endpoints": "/api/status"
            }

    # Mount static folder for asset loads (images, js, etc.)
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
