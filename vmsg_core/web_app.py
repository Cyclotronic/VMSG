import os
import asyncio
import re
from typing import Dict, Any, Optional, List
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
    idn_pattern: str = Field("", description="Substring to look for in IDN response for USB auto-healing")
    description: str = Field("", description="Human-readable label for the instrument")

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

    @api.put("/mappings/{address}")
    def update_mapping(address: int, data: MappingModel):
        if not (0 <= address <= 30):
            raise HTTPException(status_code=400, detail="Address must be between 0 and 30")
        try:
            app.state.config.set_mapping(
                address=address,
                visa_address=data.visa_address,
                idn_pattern=data.idn_pattern,
                description=data.description
            )
            logger.info("WEB_API", f"Updated virtual address {address} mapping -> {data.visa_address}")
            return {"status": "success", "message": f"Mapping for address {address} updated."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @api.delete("/mappings/{address}")
    def delete_mapping(address: int):
        if not (0 <= address <= 30):
            raise HTTPException(status_code=400, detail="Address must be between 0 and 30")
        deleted = app.state.config.delete_mapping(address)
        if deleted:
            logger.info("WEB_API", f"Deleted virtual address {address} mapping")
            return {"status": "success", "message": f"Mapping for address {address} deleted."}
        else:
            raise HTTPException(status_code=404, detail=f"No mapping found for address {address}")

    # 3. Settings Endpoints
    @api.get("/settings")
    def get_settings():
        return app.state.config.get_settings()

    @api.post("/settings")
    def update_settings(data: SettingsModel):
        try:
            # Filter None fields
            updates = {k: v for k, v in data.model_dump().items() if v is not None}
            if updates:
                app.state.config.update_settings(updates)
                logger.info("WEB_API", f"Prologix settings updated: {updates}")
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
                        restored_mappings[str(addr_val)] = {
                            "visa_address": str(m_data.get("visa_address", "")),
                            "idn_pattern": str(m_data.get("idn_pattern", "")),
                            "description": str(m_data.get("description", ""))
                        }
                except ValueError:
                    continue
            
            # Apply settings & mappings
            if isinstance(data.get("settings"), dict):
                app.state.config.update_settings(data["settings"], persist=False)
            
            app.state.config.config["mappings"] = restored_mappings
            app.state.config.save_config()
            
            # Dynamically propagate logging config to logger
            logger.configure(app.state.config.get_settings())
            
            logger.info("WEB_API", "Configuration successfully restored from uploaded JSON backup.")
            return {
                "status": "success",
                "settings": app.state.config.get_settings(),
                "mappings": app.state.config.get_mappings()
            }
        except Exception as e:
            logger.error("WEB_API", f"Config restore failed: {e}")
            raise HTTPException(status_code=500, detail=f"Restore failed: {e}")

    # 4. VISA Hardware Discovery & Lottery Healing
    @api.get("/scan")
    def scan_hardware():
        """Scans all connected instruments and queries *IDN? responses."""
        try:
            logger.info("WEB_API", "Triggering PyVISA hardware scanning...")
            devices = app.state.visa.scan_all_hardware()
            logger.info("WEB_API", f"Hardware scan finished. Discovered {len(devices)} targets.")
            return devices
        except Exception as e:
            logger.error("WEB_API", f"Hardware scanning failed: {e}")
            raise HTTPException(status_code=500, detail=f"Scan failed: {e}")

    @api.post("/heal")
    def trigger_healing(slot: Optional[int] = None):
        """Runs lottery healing based on currently configured IDN fingerprints."""
        try:
            logger.info("WEB_API", f"Triggering USB Lottery Healing (Slot filter: {slot})...")
            mappings = app.state.config.get_mappings()
            
            if slot is not None:
                slot_str = str(slot)
                mappings = {slot_str: mappings[slot_str]} if slot_str in mappings else {}

            healing_actions = app.state.visa.heal_mappings(mappings)
            
            # Apply healing actions in-place
            for action in healing_actions:
                addr = action["virtual_address"]
                new_addr = action["new_visa_address"]
                mapping_entry = app.state.config.get_mapping(addr)
                if mapping_entry:
                    app.state.config.set_mapping(
                        address=addr,
                        visa_address=new_addr,
                        idn_pattern=mapping_entry["idn_pattern"],
                        description=mapping_entry["description"]
                    )
                    logger.info("HEALER", f"Healed Address {addr}: {action['old_visa_address']} -> {new_addr}")

            return {
                "status": "success",
                "healed_count": len(healing_actions),
                "actions": healing_actions,
                "current_mappings": app.state.config.get_mappings()
            }
        except Exception as e:
            logger.error("WEB_API", f"Lottery healing failed: {e}")
            raise HTTPException(status_code=500, detail=f"Healing failed: {e}")

    @api.post("/auto_assign")
    def auto_assign_devices(data: AutoAssignModel):
        """
        Scans all online hardware/mock devices and auto-assigns them to 
        available virtual GPIB addresses (0-30). Does not overwrite 
        pre-existing mappings unless force_overwrite is set to True.
        """
        try:
            force_overwrite = data.force_overwrite
            include_mocks = data.include_mocks
            logger.info("WEB_API", f"Auto-assignment triggered. Force overwrite: {force_overwrite}, Include Mocks: {include_mocks}")
            
            # Scan connected hardware
            scanned = app.state.visa.scan_all_hardware()
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

            if force_overwrite:
                app.state.config.clear_all_mappings()
                current_mappings = {}
                mapped_visa_addrs = {}
            else:
                current_mappings = app.state.config.get_mappings()
                mapped_visa_addrs = {m["visa_address"]: addr for addr, m in current_mappings.items()}
            
            assigned_actions = []
            
            for dev in online_devices:
                v_addr = dev["visa_address"]
                idn = dev["idn"]
                
                # Skip if already mapped unless forcing overwrite
                if v_addr in mapped_visa_addrs and not force_overwrite:
                    continue
                
                # Find the lowest available virtual slot (1-30)
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
                desc = idn.split(",")[1].strip() if len(idn.split(",")) > 1 else idn.split(",")[0].strip()
                desc = desc[:25]
                fingerprint = app.state.visa.create_fingerprint(idn)
                
                app.state.config.set_mapping(
                    address=assigned_slot,
                    visa_address=v_addr,
                    idn_pattern=fingerprint,
                    description=desc
                )
                
                assigned_actions.append({
                    "virtual_address": assigned_slot,
                    "visa_address": v_addr,
                    "description": desc,
                    "overwritten": force_overwrite
                })
                
                current_mappings[cand_str] = {
                    "visa_address": v_addr,
                    "idn_pattern": fingerprint,
                    "description": desc
                }
                mapped_visa_addrs[v_addr] = assigned_slot
                    
            return {
                "status": "success",
                "assigned_count": len(assigned_actions),
                "actions": assigned_actions,
                "mappings": app.state.config.get_mappings()
            }
        except Exception as e:
            logger.error("WEB_API", f"Auto-assignment failed: {e}")
            raise HTTPException(status_code=500, detail=f"Auto-assignment failed: {e}")

    # 4b. TestController Subtool Configuration Generator
    @api.get("/testcontroller/config")
    def get_testcontroller_config(controller_id: str = "A", host: str = "127.0.0.1", separate_adapters: bool = True):
        """
        Generates settingsGPIB.txt and settingsLoad.txt configurations for TestController 
        based on active VMSG instrument mappings.
        """
        try:
            mappings = app.state.config.get_mappings()
            ctrl_id_base = controller_id.strip().upper() if controller_id.strip() else "A"
            gw_host = host.strip() if host.strip() else "127.0.0.1"

            gpib_lines = []
            load_lines = [
                "ScanSerialPorts:1",
                "ExcludedSerialPorts:"
            ]
            
            mapped_devices = []
            sorted_slots = sorted([int(k) for k in mappings.keys() if int(k) != 0])
            
            def map_to_testcontroller_driver(idn: str, visa_address: str, description: str) -> str:
                combined = f"{idn} {description} {visa_address}".upper()
                
                if "34401A" in combined:
                    return "Agilent 34401A"
                elif "34411A" in combined or "34410A" in combined:
                    return "Agilent 34411A"
                elif "34461A" in combined or "34460A" in combined or "34465A" in combined:
                    return "Keysight 34461A"
                elif "TDS 2024" in combined or "TDS" in combined or "TEKTRONIX" in combined:
                    return "Tektronix TDS2024"
                elif "2000" in combined:
                    return "Keithley 2000"
                elif "2001" in combined:
                    return "Keithley 2001M"
                elif "2002" in combined:
                    return "Keithley 2002"
                elif "2010" in combined:
                    return "Keithley 2010"
                elif "PM6685" in combined or "PM6690" in combined or "PM66" in combined:
                    return "Fluke PM6690"
                elif "33250A" in combined:
                    return "Agilent 33250A"
                elif "E363" in combined:
                    return "HP E3633A"
                elif "SDM30" in combined or "SDM3065" in combined:
                    return "Siglent SDM3065X"
                else:
                    clean_name = description if description else (idn.split(",")[1].strip() if "," in idn else idn)
                    return clean_name if clean_name else "Generic GPIB Device"

            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

            for idx, slot_int in enumerate(sorted_slots):
                slot_str = str(slot_int)
                m = mappings[slot_str]
                visa_addr = m.get("visa_address", "")
                idn = m.get("idn_pattern", "")
                desc = m.get("description", "")
                
                if separate_adapters:
                    device_ctrl_id = alphabet[idx % len(alphabet)]
                    gpib_lines.append(f"PrologixEthernet|id:{device_ctrl_id}|address:{gw_host}|baudrate:|settings:|")
                else:
                    device_ctrl_id = ctrl_id_base

                driver = map_to_testcontroller_driver(idn, visa_addr, desc)
                load_lines.append(f"Device:{driver}|PortType:GPIB|Address:{device_ctrl_id}:{slot_int}|Baudrate:9600|Enabled:1")
                mapped_devices.append({
                    "address": slot_int,
                    "controller_id": device_ctrl_id,
                    "driver": driver,
                    "visa_address": visa_addr,
                    "idn": idn,
                    "description": desc
                })

            if not separate_adapters or not gpib_lines:
                gpib_text = f"PrologixEthernet|id:{ctrl_id_base}|address:{gw_host}|baudrate:|settings:|\n"
            else:
                gpib_text = "\n".join(gpib_lines) + "\n"

            settings_load = "\n".join(load_lines) + "\n"

            return {
                "status": "success",
                "controller_id": ctrl_id_base,
                "host": gw_host,
                "separate_adapters": separate_adapters,
                "settingsGPIB": gpib_text,
                "settingsLoad": settings_load,
                "mapped_devices": mapped_devices
            }
        except Exception as e:
            logger.error("WEB_API", f"TestController config generation failed: {e}")
            raise HTTPException(status_code=500, detail=f"Generation failed: {e}")

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
