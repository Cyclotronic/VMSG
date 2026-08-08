import asyncio
import socket
import time
from typing import Dict, Any, Optional
from .config_manager import ConfigManager
from .visa_manager import VisaManager
from .logger import logger

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

    def get_client_setting(self, client_addr: tuple, key: str) -> Any:
        """Gets a setting value specific to a TCP client connection session."""
        if client_addr in self.client_sessions and key in self.client_sessions[client_addr]:
            return self.client_sessions[client_addr][key]
        return self.config.get_setting(key)

    def set_client_setting(self, client_addr: tuple, key: str, value: Any) -> None:
        """Sets a setting value specific to a TCP client connection session while updating global defaults."""
        if client_addr not in self.client_sessions:
            self.client_sessions[client_addr] = self.config.get_settings().copy()
        self.client_sessions[client_addr][key] = value
        # Also update global config settings for UI display
        self.config.update_setting(key, value)

    async def start(self) -> None:
        """Starts the TCP socket server."""
        self.is_running = True
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port,
            reuse_address=True
        )
        # Apply TCP_NODELAY (disable Nagle's) on the listening socket
        sock = self.server.sockets[0]
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        logger.info("SOCKET_SERVER", f"VMSG TCP Socket Server started on {self.host}:{self.port} (Prologix compatible)")
        
        # Keep running
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
            
            # Cancel all active client tasks
            for task in list(self.active_tasks):
                if not task.done():
                    task.cancel()
            
            try:
                # Wait with timeout to prevent hanging
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

        # Track the active task for cancellation on stop
        current_task = asyncio.current_task()
        if current_task:
            self.active_tasks.add(current_task)

        # Disable Nagle's algorithm on the client socket to optimize latency
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
                    # Client closed connection
                    break

                buffer += data.decode('utf-8', errors='replace')
                
                # Process lines as they arrive (terminated by LF or CR+LF)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
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
            # Process Prologix configuration commands
            parts = line[2:].strip().split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else None
            
            return await self.execute_prologix_cmd(cmd, arg, client_addr)
        else:
            # Process regular instrument (SCPI) commands
            return await self.route_instrument_cmd(line, client_addr)

    async def execute_prologix_cmd(self, cmd: str, arg: Optional[str], client_addr: tuple) -> Optional[str]:
        """Executes a parsed Prologix command and returns output (terminated with CR+LF)."""
        
        # 1. ++addr (GPIB Address)
        if cmd == "addr":
            if arg is None:
                # Query per-client active address
                return f"{self.get_client_setting(client_addr, 'addr')}\r\n"
            else:
                try:
                    val = int(arg)
                    if 0 <= val <= 30:
                        self.set_client_setting(client_addr, "addr", val)
                        logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Selected virtual address changed to {val}")
                        return None
                    return "Error: Address must be 0 to 30\r\n"
                except ValueError:
                    return "Error: Invalid address format\r\n"

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
                    return "Error: EOS must be 0, 1, 2, or 3\r\n"
                except ValueError:
                    return "Error: Invalid EOS format\r\n"

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
                    return "Error: Timeout must be > 0\r\n"
                except ValueError:
                    return "Error: Invalid timeout format\r\n"

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
                    return "Error: EOT char must be 0 to 255\r\n"
                except ValueError:
                    return "Error: Invalid EOT char format\r\n"

        # 10. ++read (Perform manual read from device)
        elif cmd == "read":
            # Determine read completion rule based on arguments
            # Prologix support: ++read, ++read eoi, ++read <char>
            return await self.perform_instrument_read(client_addr)

        # 11. ++rst (Reset configurations to defaults)
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

        # 12. ++lon (Listen Only Mode)
        elif cmd == "lon":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'lon')}\r\n"
            else:
                val = 1 if arg in ["1", "on", "true"] else 0
                self.set_client_setting(client_addr, "lon", val)
                logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Listen-only mode set to {val}")
                return None

        # 13. ++savecfg (Save configuration toggle / no-op)
        elif cmd == "savecfg":
            if arg is None:
                return f"{self.get_client_setting(client_addr, 'savecfg')}\r\n"
            else:
                val = 1 if arg in ["1", "on", "true"] else 0
                self.set_client_setting(client_addr, "savecfg", val)
                logger.info("SOCKET_SERVER", f"[{client_addr[0]}:{client_addr[1]}] Auto-save configuration set to {val}")
                return None

        # 14. ++spoll (Serial poll)
        elif cmd == "spoll":
            if arg is None:
                addr_to_poll = self.get_client_setting(client_addr, "addr")
            else:
                try:
                    addr_to_poll = int(arg)
                except ValueError:
                    return "Error: Invalid address format for spoll\r\n"

            if not (0 <= addr_to_poll <= 30):
                return "Error: Address must be 0 to 30 for spoll\r\n"

            stb_val = await self.perform_serial_poll(addr_to_poll)
            return f"{stb_val}\r\n"

        # 15. ++help (Help text)
        elif cmd == "help":
            help_text = (
                "Prologix Emulator Command Help:\r\n"
                "  ++addr [<0-30>]       Set/Query virtual instrument GPIB address\r\n"
                "  ++auto [0|1]          Set/Query read-after-write auto mode\r\n"
                "  ++mode [0|1]          Set/Query mode (0=Device, 1=Controller)\r\n"
                "  ++read [eoi|<char>]   Read response from current instrument\r\n"
                "  ++ver                 Query Prologix controller version\r\n"
                "  ++eos [0|1|2|3]       Set/Query EOS formatting (0=CR+LF, 1=CR, 2=LF, 3=None)\r\n"
                "  ++eoi [0|1]           Set/Query whether to assert EOI line\r\n"
                "  ++read_tmo_ms <ms>    Set/Query timeout in milliseconds\r\n"
                "  ++eot_enable [0|1]    Set/Query appending of EOT char on EOI\r\n"
                "  ++eot_char <0-255>    Set/Query character ASCII to append on EOI\r\n"
                "  ++lon [0|1]           Set/Query listen only mode\r\n"
                "  ++savecfg [0|1]       Set/Query automatic config saving\r\n"
                "  ++spoll [<0-30>]      Perform serial poll on instrument\r\n"
                "  ++rst                 Reset configuration to defaults\r\n"
                "  ++help                Display this command list\r\n"
            )
            return help_text

        # Unknown commands
        else:
            logger.warning("SOCKET_SERVER", f"Received unknown command: ++{cmd}")
            return f"Error: Unknown command ++{cmd}\r\n"

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
                logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] Addressing unmapped virtual slot {curr_addr}. Simulating typical GPIB physical bus timeout (blocking for {read_tmo_ms} ms)...")
                await asyncio.sleep(read_tmo_ms / 1000.0)
                return "Error: VI_ERROR_TMO (-1073807339): Timeout expired before operation completed.\r\n"
            else:
                msg = f"No instrument mapped to virtual address {curr_addr}."
                logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] {msg}")
                return f"Error: {msg}\r\n"

        visa_addr = mapping.get("visa_address")
        if not visa_addr:
            msg = f"Empty VISA address configured for address {curr_addr}."
            logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] {msg}")
            return f"Error: {msg}\r\n"

        # Route write in a thread to keep socket event loop unblocked
        try:
            res, res_lock = self.visa_manager.get_resource(visa_addr, timeout_ms=read_tmo_ms)
        except Exception as e:
            logger.error("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] Failed to acquire connection to {visa_addr}: {e}")
            return f"Error: Connection to physical instrument failed ({e})\r\n"

        # Apply EOS setting to the sent command
        eos_terminator = self._get_eos_terminator(eos_val)
        command_with_term = command + eos_terminator

        def _execute_write():
            is_mock = visa_addr.upper().startswith("MOCK::")
            if is_mock:
                with res_lock:
                    res.write(command_with_term)
            else:
                with self.visa_manager.global_visa_lock:
                    with res_lock:
                        res.write(command_with_term)

        try:
            logger.info("INSTR_WRITE", f"[{client_addr[0]}:{client_addr[1]}] Sending to {visa_addr} (Addr {curr_addr}): {repr(command_with_term)}")
            await asyncio.to_thread(_execute_write)
            # Track which address received a query command so subsequent ++read reads from the exact queried instrument
            if "?" in command:
                self.set_client_setting(client_addr, "last_query_addr", curr_addr)
        except Exception as e:
            logger.error("INSTR_WRITE", f"[{client_addr[0]}:{client_addr[1]}] Write failed to {visa_addr}: {e}")
            if not visa_addr.upper().startswith("MOCK::"):
                try:
                    with self.visa_manager.global_visa_lock:
                        with res_lock:
                            if hasattr(res, "clear"):
                                res.clear()
                except Exception:
                    self.visa_manager.purge_resource(visa_addr)
            return f"Error: Write failed to instrument ({e})\r\n"

        # If auto read-after-write is enabled, read response immediately
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
                logger.warning("INSTR_ROUTING", f"[{client_addr[0]}:{client_addr[1]}] Read requested on unmapped virtual slot {curr_addr}. Simulating typical GPIB physical bus timeout (blocking for {read_tmo_ms} ms)...")
                await asyncio.sleep(read_tmo_ms / 1000.0)
                term = "\r\n"
                if eot_enable == 1:
                    term = chr(eot_char) + term
                return term
            else:
                return f"Error: No instrument mapped to virtual address {curr_addr}.\r\n"

        visa_addr = mapping.get("visa_address", "")
        if not visa_addr:
            return "Error: Empty VISA address\r\n"

        try:
            res, res_lock = self.visa_manager.get_resource(visa_addr, timeout_ms=read_tmo_ms)
        except Exception as e:
            return f"Error: Connect failed ({e})\r\n"

        def _execute_read():
            is_mock = visa_addr.upper().startswith("MOCK::")
            if is_mock:
                with res_lock:
                    return res.read()
            else:
                with self.visa_manager.global_visa_lock:
                    with res_lock:
                        return res.read()

        try:
            # Read in thread to keep event loop unblocked
            response = await asyncio.to_thread(_execute_read)
            logger.info("INSTR_READ", f"[{client_addr[0]}:{client_addr[1]}] Read from {visa_addr} (Addr {curr_addr}): {repr(response)}")
            
            # Format output line endings. Normalise incoming ending and apply appropriate Prologix formatting
            response = response.strip()
            
            # Format the response depending on eot_enable and eot_char
            term = "\r\n"  # Prologix Ethernet output delimiter
            if eot_enable == 1:
                term = chr(eot_char) + term
                
            out = response + term
            logger.info("TRAFFIC_OUT", f"[{client_addr[0]}:{client_addr[1]}] <- {repr(out)}")
            return out
        except Exception as e:
            logger.error("INSTR_READ", f"[{client_addr[0]}:{client_addr[1]}] Read failed from {visa_addr}: {e}")
            if not visa_addr.upper().startswith("MOCK::"):
                try:
                    with res_lock:
                        if hasattr(res, "clear"):
                            res.clear()
                except Exception:
                    self.visa_manager.purge_resource(visa_addr)
            # Return standard Prologix Ethernet empty string response on timeout
            if "VI_ERROR_TMO" in str(e) or "Timeout" in str(e):
                term = "\r\n"
                if eot_enable == 1:
                    term = chr(eot_char) + term
                return term
            return f"Error: Read failed from instrument ({e})\r\n"

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
            def _execute_stb():
                with res_lock:
                    return res.read_stb()
            return await asyncio.to_thread(_execute_stb)
        except Exception as e:
            logger.warning("SOCKET_SERVER", f"Serial poll failed on virtual address {address} ({visa_addr}): {e}")
            return 0
