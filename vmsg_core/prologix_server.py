import asyncio
import socket
import re
import time
from typing import Dict, Any, Optional
from .config_manager import ConfigManager
from .visa_manager import VisaManager
from .logger import logger

from pyvisa.constants import RENLineOperation

_SESSION_ONLY = {"last_query_addr", "savecfg", "llo", "loc", "ifc"}

class PrologixSocketServer:
    """Asynchronous TCP Socket Server running on Port 1234 that implements the VISA Mapping TCP/IP Socket Gateway (VMSG) with Prologix Ethernet compatible control."""
    def __init__(self, host: str, port: int, config_manager: ConfigManager, visa_manager: VisaManager):
        self.host = host
        self.port = port
        self.config = config_manager
        self.visa_manager = visa_manager
        self.server: Optional[asyncio.Server] = None
        self.active_connections = set()
        self.active_tasks = set()
        self.client_sessions: Dict[tuple, Dict[str, Any]] = {}
        self.is_running = False

    def get_client_setting(self, client_addr: tuple, key: str, default: Any = None) -> Any:
        """Gets a setting value specific to a TCP client connection session."""
        if client_addr in self.client_sessions and key in self.client_sessions[client_addr]:
            return self.client_sessions[client_addr][key]
        return self.config.get_setting(key, default)


    def set_client_setting(self, client_addr: tuple, key: str, value: Any) -> None:
        """Sets a setting value specific to a TCP client session without causing disk I/O for session-only keys."""
        if client_addr not in self.client_sessions:
            self.client_sessions[client_addr] = self.config.get_settings().copy()
        self.client_sessions[client_addr][key] = value
        if key in _SESSION_ONLY:
            return
        # Update runtime config in-memory for UI visibility without forcing immediate disk write
        self.config.set_runtime_setting(key, value)

    def _empty_response(self, client_addr: tuple) -> str:
        """The response a real Prologix emits when a read yields nothing (or times out)."""
        eot_enable = self.get_client_setting(client_addr, "eot_enable", 0)
        eot_char = self.get_client_setting(client_addr, "eot_char", 4)
        term = "\r\n"
        if eot_enable == 1:
            term = chr(eot_char) + term
        return term

    async def start(self) -> None:
        """Starts the TCP socket server."""
        self.is_running = True
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port,
            reuse_address=True
        )
        sock = self.server.sockets[0]
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        logger.info("SOCKET_SERVER", f"VMSG TCP Socket Server started on {self.host}:{self.port} (Prologix compatible)")
        
        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SOCKET_SERVER", f"TCP Socket Server loop encountered exception: {e}")

    async def stop(self) -> None:
        """Stops the TCP socket server."""
        self.is_running = False
        if self.server:
            self.server.close()
            
            for task in list(self.active_tasks):
                if not task.done():
                    task.cancel()
            
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=1.0)
            except asyncio.TimeoutError:
                logger.warning("SOCKET_SERVER", "Socket server shutdown timed out.")
            except Exception as e:
                logger.warning("SOCKET_SERVER", f"Error during socket server shutdown: {e}")
                
            logger.info("SOCKET_SERVER", "VMSG TCP Socket Server stopped.")

    def _get_eos_terminator(self, eos_val: int) -> str:
        """Gets string terminator for a given EOS value."""
        if eos_val == 0:
            return "\r\n"
        elif eos_val == 1:
            return "\r"
        elif eos_val == 2:
            return "\n"
        return ""

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handles a client TCP connection with isolated per-client session state."""
        client_address = writer.get_extra_info('peername')
        logger.info("SOCKET_SERVER", f"New client connection established from {client_address}")
        self.active_connections.add(client_address)
        self.client_sessions[client_address] = self.config.get_settings().copy()

        current_task = asyncio.current_task()
        if current_task:
            self.active_tasks.add(current_task)

        try:
            sock = writer.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception as e:
            logger.warning("SOCKET_SERVER", f"Could not set TCP_NODELAY on client socket: {e}")

        buffer = ""
        try:
            while self.is_running:
                data = await reader.read(4096)
                if not data:
                    break

                buffer += data.decode('utf-8', errors='replace')
                
                # Split commands on CR, LF, or CR+LF
                lines = re.split(r'\r\n|\n|\r', buffer)
                # Keep remaining incomplete line in buffer
                buffer = lines.pop()

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    response = await self.process_command(line, client_address)
                    if response is not None:
                        writer.write(response.encode('utf-8'))
                        await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SOCKET_SERVER", f"Error serving client {client_address}: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self.active_connections.discard(client_address)
            self.client_sessions.pop(client_address, None)
            if current_task:
                self.active_tasks.discard(current_task)
            logger.info("SOCKET_SERVER", f"Client connection closed: {client_address}")

    async def process_command(self, line: str, client_addr: tuple) -> Optional[str]:
        """
        Parses and executes a command received from a socket client.
        If command is a Prologix command (starts with ++), handles it.
        Otherwise, routes to the active GPIB instrument.
        """
        logger.info("TRAFFIC_IN", f"[{client_addr[0]}:{client_addr[1]}] -> {line}")
        
        if line.startswith("++"):
            parts = line[2:].strip().split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else None
            
            return await self.execute_prologix_cmd(cmd, arg, client_addr)
        else:
            return await self.route_instrument_cmd(line, client_addr)

    async def execute_prologix_cmd(self, cmd: str, arg: Optional[str], client_addr: tuple) -> Optional[str]:
        """Executes a parsed Prologix command and returns output (terminated with CR+LF)."""
        
        # 1. ++addr (GPIB Address)
        if cmd == "addr":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'addr')}\r\n"
            else:
                try:
                    val = int(arg.split()[0])
                    if 0 <= val <= 30:
                        self.set_client_setting(client_addr, "addr", val)
                        logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Selected virtual address changed to {val}")
                        return None
                    return None
                except ValueError:
                    return None

        # 2. ++auto (Read-After-Write)
        elif cmd == "auto":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'auto')}\r\n"
            else:
                val = 1 if arg in ["1", "on", "true"] else 0
                self.set_client_setting(client_addr, "auto", val)
                logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Read-after-write auto mode set to {val}")
                return None

        # 3. ++mode (Controller vs Device Mode)
        elif cmd == "mode":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'mode')}\r\n"
            else:
                val = 1 if arg in ["1", "controller"] else 0
                self.set_client_setting(client_addr, "mode", val)
                logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Prologix mode set to {val} ({'Controller' if val else 'Device'})")
                return None

        # 4. ++ver (Version query)
        elif cmd == "ver":
            return "Prologix GPIB-ETHERNET Controller version 6.1.0.0\r\n"

        # 5. ++eos (Termination format)
        elif cmd == "eos":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'eos')}\r\n"
            else:
                try:
                    val = int(arg)
                    if 0 <= val <= 3:
                        self.set_client_setting(client_addr, "eos", val)
                        logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] EOS terminator set to {val}")
                        return None
                    return None
                except ValueError:
                    return None

        # 6. ++eoi (Assert EOI)
        elif cmd == "eoi":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'eoi')}\r\n"
            else:
                val = 1 if arg in ["1", "on", "true"] else 0
                self.set_client_setting(client_addr, "eoi", val)
                return None

        # 7. ++read_tmo_ms (Read Timeout)
        elif cmd == "read_tmo_ms":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'read_tmo_ms')}\r\n"
            else:
                try:
                    val = int(arg)
                    if val > 0:
                        self.set_client_setting(client_addr, "read_tmo_ms", val)
                        logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Read timeout set to {val} ms")
                        return None
                    return None
                except ValueError:
                    return None

        # 8. ++eot_enable (Append char on EOI)
        elif cmd == "eot_enable":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'eot_enable')}\r\n"
            else:
                val = 1 if arg in ["1", "on", "true"] else 0
                self.set_client_setting(client_addr, "eot_enable", val)
                return None

        # 9. ++eot_char (EOT Character Code)
        elif cmd == "eot_char":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'eot_char')}\r\n"
            else:
                try:
                    val = int(arg)
                    if 0 <= val <= 255:
                        self.set_client_setting(client_addr, "eot_char", val)
                        return None
                    return None
                except ValueError:
                    return None

        # 10. ++read (Perform manual read from device)
        elif cmd == "read":
            return await self.perform_instrument_read(client_addr)

        # 11. ++clr (Device Clear)
        elif cmd == "clr":
            curr_addr = self.get_client_setting(client_addr, "addr")
            mapping = self.config.get_mapping(curr_addr)
            if mapping and mapping.get("visa_address"):
                visa_addr = mapping["visa_address"]
                interface_lock = self.visa_manager.get_interface_lock(visa_addr)
                def _exec_clear():
                    try:
                        res, res_lock = self.visa_manager.get_resource(visa_addr)
                        with interface_lock:
                            with res_lock:
                                if hasattr(res, "clear"):
                                    res.clear()
                    except Exception as e:
                        logger.warning("SOCKET_SERVER", f"Clear failed on {visa_addr}: {e}")
                await asyncio.to_thread(_exec_clear)
            return None

        # 12. ++trg (Group Execute Trigger)
        elif cmd == "trg":
            curr_addr = self.get_client_setting(client_addr, "addr")
            mapping = self.config.get_mapping(curr_addr)
            if mapping and mapping.get("visa_address"):
                visa_addr = mapping["visa_address"]
                interface_lock = self.visa_manager.get_interface_lock(visa_addr)
                def _exec_trg():
                    try:
                        res, res_lock = self.visa_manager.get_resource(visa_addr)
                        with interface_lock:
                            with res_lock:
                                if hasattr(res, "assert_trigger"):
                                    res.assert_trigger()
                                else:
                                    res.write("*TRG")
                    except Exception as e:
                        logger.warning("SOCKET_SERVER", f"Trigger failed on {visa_addr}: {e}")
                await asyncio.to_thread(_exec_trg)
            return None

        # 13. ++llo, ++loc, ++ifc (GPIB Bus Control Actions)
        elif cmd in ["llo", "loc", "ifc"]:
            curr_addr = self.get_client_setting(client_addr, "addr")
            mapping = self.config.get_mapping(curr_addr)
            if mapping and mapping.get("visa_address"):
                visa_addr = mapping["visa_address"]
                interface_lock = self.visa_manager.get_interface_lock(visa_addr)
                def _exec_bus_action():
                    try:
                        res, res_lock = self.visa_manager.get_resource(visa_addr)
                        with interface_lock:
                            with res_lock:
                                if hasattr(res, "control_ren"):
                                    if cmd == "loc":
                                        res.control_ren(RENLineOperation.address_gtl)
                                    elif cmd == "llo":
                                        res.control_ren(RENLineOperation.asrt_address_llo)
                                elif cmd == "ifc":
                                    logger.warning("SOCKET_SERVER", f"Interface Clear (IFC) not supported on backend for {visa_addr}")
                    except Exception as e:
                        logger.warning("SOCKET_SERVER", f"Bus action {cmd} failed on {visa_addr}: {e}")
                await asyncio.to_thread(_exec_bus_action)
            return None

        # 14. ++lon, ++savecfg (Session state flags)
        elif cmd in ["lon", "savecfg"]:
            if arg is None:
                return f"{self.get_client_setting(client_addr, cmd, 0)}\r\n"
            else:
                val = 1 if arg in ["1", "on", "true"] else 0
                self.set_client_setting(client_addr, cmd, val)
                return None

        # 15. ++rst (Reset configurations to defaults)
        elif cmd == "rst":
            defaults = {
                "addr": 1,
                "auto": 1,
                "mode": 1,
                "eos": 0,
                "eoi": 1,
                "read_tmo_ms": 3000,
                "eot_enable": 0,
                "eot_char": 4
            }
            for k, v in defaults.items():
                self.set_client_setting(client_addr, k, v)
            logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Reset settings to Prologix factory defaults.")
            return None

        # 16. ++spoll (Serial poll)
        elif cmd == "spoll":
            if arg is None:
                addr_to_poll = self.get_client_setting(client_addr, "addr")
            else:
                try:
                    addr_to_poll = int(arg)
                except ValueError:
                    return None

            if not (0 <= addr_to_poll <= 30):
                return None

            stb_val = await self.perform_serial_poll(addr_to_poll)
            return f"{stb_val}\r\n"

        # 17. ++help (Help text)
        elif cmd == "help":
            help_text = (
                "Prologix Emulator Command Help:\r\n"
                "  ++addr [<0-30>]       Set/Query virtual instrument GPIB address\r\n"
                "  ++auto [0|1]          Set/Query read-after-write auto mode\r\n"
                "  ++mode [0|1]          Set/Query mode (0=Device, 1=Controller)\r\n"
                "  ++read [eoi|<char>]   Read response from current instrument\r\n"
                "  ++clr                 Send Selected Device Clear\r\n"
                "  ++trg                 Send Group Execute Trigger (*TRG)\r\n"
                "  ++loc                 Go To Local (GTL)\r\n"
                "  ++llo                 Local Lockout (LLO)\r\n"
                "  ++ver                 Query Prologix controller version\r\n"
                "  ++eos [0|1|2|3]       Set/Query EOS formatting (0=CR+LF, 1=CR, 2=LF, 3=None)\r\n"
                "  ++eoi [0|1]           Set/Query whether to assert EOI line\r\n"
                "  ++read_tmo_ms <ms>    Set/Query timeout in milliseconds\r\n"
                "  ++eot_enable [0|1]    Set/Query appending of EOT char on EOI\r\n"
                "  ++eot_char <0-255>    Set/Query character ASCII to append on EOI\r\n"
                "  ++spoll [<0-30>]      Perform serial poll on instrument\r\n"
                "  ++rst                 Reset configuration to defaults\r\n"
                "  ++help                Display this command list\r\n"
            )
            return help_text

        # Unknown commands: silently log without injecting error strings into client data stream
        else:
            logger.warning("SOCKET_SERVER", f"Received unknown command: ++{cmd}")
            return None

    async def route_instrument_cmd(self, command: str, client_addr: tuple) -> Optional[str]:
        """Routes regular instrument command to physical/mock instrument."""
        curr_addr = self.get_client_setting(client_addr, "addr")
        auto_mode = self.get_client_setting(client_addr, "auto")
        read_tmo_ms = self.get_client_setting(client_addr, "read_tmo_ms")
        eos_val = self.get_client_setting(client_addr, "eos")
        
        mapping = self.config.get_mapping(curr_addr)
        unmapped_behavior = self.config.get_setting("unmapped_behavior", "message")

        if not mapping:
            if unmapped_behavior == "timeout":
                logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] Addressing unmapped virtual slot {curr_addr}. Simulating typical GPIB timeout...")
                await asyncio.sleep(read_tmo_ms / 1000.0)
                return self._empty_response(client_addr) if auto_mode == 1 else None
            else:
                logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] No instrument mapped to virtual address {curr_addr}.")
                return self._empty_response(client_addr) if auto_mode == 1 else None

        visa_addr = mapping.get("visa_address")
        if not visa_addr:
            return self._empty_response(client_addr) if auto_mode == 1 else None

        try:
            res, res_lock = self.visa_manager.get_resource(visa_addr, timeout_ms=read_tmo_ms)
        except Exception as e:
            logger.error("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] Failed to acquire connection to {visa_addr}: {e}")
            return self._empty_response(client_addr) if auto_mode == 1 else None

        eos_terminator = self._get_eos_terminator(eos_val)
        command_with_term = command + eos_terminator
        interface_lock = self.visa_manager.get_interface_lock(visa_addr)

        def _execute_write():
            is_mock = visa_addr.upper().startswith("MOCK::")
            if is_mock:
                with res_lock:
                    res.write(command_with_term)
            else:
                with interface_lock:
                    with res_lock:
                        res.write(command_with_term)

        try:
            logger.info("INSTR_WRITE", f"[{client_addr[0]}:{client_addr[1]}] Sending to {visa_addr} (Addr {curr_addr}): {repr(command_with_term)}")
            await asyncio.to_thread(_execute_write)
            if "?" in command:
                self.set_client_setting(client_addr, "last_query_addr", curr_addr)
        except Exception as e:
            logger.error("INSTR_WRITE", f"[{client_addr[0]}:{client_addr[1]}] Write failed to {visa_addr}: {e}")
            if not visa_addr.upper().startswith("MOCK::"):
                def _cleanup():
                    try:
                        with interface_lock:
                            with res_lock:
                                if hasattr(res, "clear"):
                                    res.clear()
                    except Exception:
                        self.visa_manager.purge_resource(visa_addr)
                await asyncio.to_thread(_cleanup)
            return self._empty_response(client_addr) if auto_mode == 1 else None

        if auto_mode == 1:
            return await self.perform_instrument_read(client_addr)

        return None

    async def perform_instrument_read(self, client_addr: tuple) -> str:
        """Performs a read operation on the currently addressed instrument and formats output."""
        query_addr = self.get_client_setting(client_addr, "last_query_addr")
        if query_addr is not None:
            curr_addr = query_addr
            self.set_client_setting(client_addr, "last_query_addr", None)
        else:
            curr_addr = self.get_client_setting(client_addr, "addr")
            
        read_tmo_ms = self.get_client_setting(client_addr, "read_tmo_ms")
        eot_enable = self.get_client_setting(client_addr, "eot_enable")
        eot_char = self.get_client_setting(client_addr, "eot_char")
        
        mapping = self.config.get_mapping(curr_addr)
        unmapped_behavior = self.config.get_setting("unmapped_behavior", "message")

        if not mapping:
            if unmapped_behavior == "timeout":
                logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] Read requested on unmapped virtual slot {curr_addr}. Simulating timeout...")
                await asyncio.sleep(read_tmo_ms / 1000.0)
            return self._empty_response(client_addr)

        visa_addr = mapping.get("visa_address", "")
        if not visa_addr:
            return self._empty_response(client_addr)

        try:
            res, res_lock = self.visa_manager.get_resource(visa_addr, timeout_ms=read_tmo_ms)
        except Exception as e:
            logger.error("INSTR_READ", f"[{client_addr[0]}:{client_addr[1]}] Connection failed to {visa_addr}: {e}")
            return self._empty_response(client_addr)

        interface_lock = self.visa_manager.get_interface_lock(visa_addr)

        def _execute_read():
            is_mock = visa_addr.upper().startswith("MOCK::")
            if is_mock:
                with res_lock:
                    return res.read()
            else:
                with interface_lock:
                    with res_lock:
                        return res.read()

        try:
            response = await asyncio.to_thread(_execute_read)
            logger.info("INSTR_READ", f"[{client_addr[0]}:{client_addr[1]}] Read from {visa_addr} (Addr {curr_addr}): {repr(response)}")
            
            # Format output line endings
            if response.endswith("\r\n"):
                response = response[:-2]
            elif response.endswith("\n") or response.endswith("\r"):
                response = response[:-1]

            term = "\r\n"
            if eot_enable == 1:
                term = chr(eot_char) + term
                
            out = response + term
            logger.info("TRAFFIC_OUT", f"[{client_addr[0]}:{client_addr[1]}] <- {repr(out)}")
            return out
        except Exception as e:
            logger.error("INSTR_READ", f"[{client_addr[0]}:{client_addr[1]}] Read failed from {visa_addr}: {e}")
            if not visa_addr.upper().startswith("MOCK::"):
                def _cleanup():
                    try:
                        with res_lock:
                            if hasattr(res, "clear"):
                                res.clear()
                    except Exception:
                        self.visa_manager.purge_resource(visa_addr)
                await asyncio.to_thread(_cleanup)
                
            return self._empty_response(client_addr)

    async def perform_serial_poll(self, address: int) -> int:
        """Performs a standard GPIB Serial Poll (reads STB) of the specified virtual address."""
        mapping = self.config.get_mapping(address)
        if not mapping:
            return 0
        visa_addr = mapping.get("visa_address")
        if not visa_addr:
            return 0

        try:
            res, res_lock = self.visa_manager.get_resource(visa_addr)
            interface_lock = self.visa_manager.get_interface_lock(visa_addr)
            def _execute_stb():
                with interface_lock:
                    with res_lock:
                        return res.read_stb()
            return await asyncio.to_thread(_execute_stb)
        except Exception as e:
            logger.warning("SOCKET_SERVER", f"Serial poll failed on virtual address {address} ({visa_addr}): {e}")
            return 0

