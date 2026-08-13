import asyncio
import os
import sys
import warnings

# Suppress noisy PyVISA UserWarning regarding manual SCPI terminators
warnings.filterwarnings("ignore", message="write message already ends with termination characters")

from vmsg_core.config_manager import ConfigManager
from vmsg_core.visa_manager import VisaManager
from vmsg_core.prologix_server import PrologixSocketServer
from vmsg_core.web_app import create_app
from vmsg_core.logger import logger
from vmsg_core.version import __version__
from vmsg_core import crashlog

async def main():
    # Set custom event loop exception handler to suppress benign ConnectionResetErrors on Windows
    loop = asyncio.get_running_loop()
    
    def custom_exception_handler(loop, context):
        exception = context.get("exception")
        message = context.get("message", "")
        # Filter ConnectionResetError or WinError 10054 inside asyncio socket connection_lost callbacks
        if isinstance(exception, ConnectionResetError) or (exception and "10054" in str(exception)):
            handle = context.get("handle")
            if handle and hasattr(handle, "_callback"):
                cb_name = getattr(handle._callback, "__name__", "")
                if cb_name == "_call_connection_lost":
                    return
            if "_call_connection_lost" in str(handle) or "_call_connection_lost" in message:
                return
        loop.default_exception_handler(context)
        
    loop.set_exception_handler(custom_exception_handler)

    logger.info("MAIN", "Initializing VISA Mapping TCP/IP Socket Gateway (VMSG)...")

    # 1. Initialize configuration manager
    config = ConfigManager()
    
    # 2. Initialize VISA manager
    visa = VisaManager()
    
    # Ports and bind address are overridable so a packaged build can be verified,
    # containerised, or run alongside a live instance without clashing.
    bind_host = (os.environ.get("VMSG_BIND_HOST") or "0.0.0.0").strip()
    socket_port = int(os.environ.get("VMSG_SOCKET_PORT") or 1234)
    http_port = int(os.environ.get("VMSG_HTTP_PORT") or 8080)

    # 3. Create Prologix socket server (default port 1234)
    socket_server = PrologixSocketServer(bind_host, socket_port, config, visa)

    # 4. LXI raw-socket, VXI-11 RPC and mDNS discovery.
    # Ported from BenchForge: a full VXI-11 stack (portmap + core + abort
    # channels) rather than the previous raw-5025-only emulator. All three share
    # one registry that resolves slots against the live VMSG mappings, so a
    # VXI-11 client and a Prologix client see the same instruments.
    from vmsg_core.vxi11_lxi_emulator import LXIRawSocketServer, LXIDiscoveryResponder
    from vmsg_core.vxi11_emulator import VXI11EmulatorServer
    from vmsg_core.vxi11_bridge import VmsgInstrumentRegistry

    instrument_registry = VmsgInstrumentRegistry(config, visa)
    lxi_server = None
    lxi_mdns = None
    vxi11_server = None

    if config.get_setting("lxi_raw_socket_enabled", True):
        lxi_port = int(config.get_setting("lxi_raw_socket_port", 5025))
        lxi_server = LXIRawSocketServer(host=bind_host, port=lxi_port,
                                        registry=instrument_registry)
        lxi_server.start()

    if config.get_setting("vxi11_enabled", True):
        try:
            vxi11_server = VXI11EmulatorServer(host=bind_host,
                                               registry=instrument_registry)
            vxi11_server.start()
        except OSError as e:
            # Portmap on 111 is privileged on Linux and often already taken on
            # Windows; VXI-11 is optional, so log and carry on.
            logger.warning("MAIN", f"VXI-11 not started: {e}")
            vxi11_server = None

    if config.get_setting("lxi_mdns_enabled", True):
        lxi_mdns = LXIDiscoveryResponder(model_name="VMSG Multi-Protocol Gateway")
        lxi_mdns.start()


    # 5. Create FastAPI App (default port 8080)
    app = create_app(config, visa, socket_server)

    # 6. Configure and start Uvicorn web server asynchronously
    import uvicorn
    uvicorn_config = uvicorn.Config(
        app,
        host=bind_host,
        port=http_port,
        log_level="warning",  # Keep output clean, custom logs handle API visibility
        loop="asyncio"
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    # Print startup banner directly to console
    print("================================================================================")
    print(f"  VISA Mapping TCP/IP Socket Gateway (VMSG) v{__version__}")
    print(f"  Prologix Control Socket : tcp://{bind_host}:{socket_port}")
    if lxi_server:
        print(f"  LXI Raw SCPI Socket     : tcp://{bind_host}:{lxi_server.port}")
    if vxi11_server:
        print(f"  VXI-11 RPC (LXI)        : tcp://{bind_host}:{vxi11_server.core_port} "
              f"(portmap {vxi11_server.portmap_port})")
    print(f"  Web Dashboard & REST API : http://localhost:{http_port}")
    print(f"  Config File Path         : {config.filepath}")
    visalib_str = str(getattr(visa.rm, "visalib", ""))
    backend_type = "Pure-Python (@py)" if ("py" in visalib_str.lower()) else ("NI-VISA System" if visa.rm else "None (Mock Only)")
    print(f"  VISA Backend Loaded      : {backend_type}")
    print("================================================================================\n")

    logger.info("MAIN", "Starting servers...")
    
    # Run socket server and web server concurrently
    socket_task = asyncio.create_task(socket_server.start())
    web_task = asyncio.create_task(uvicorn_server.serve())

    try:
        done, pending = await asyncio.wait(
            [socket_task, web_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for t in done:
            if not t.cancelled() and t.exception():
                logger.error("MAIN", f"Server task exited unexpectedly: {t.exception()}")
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("MAIN", "Shutdown signal received.")
    finally:
        logger.info("MAIN", "Cleaning up and stopping servers...")
        
        # Cancel any pending server tasks
        for task in [socket_task, web_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning("MAIN", f"Error cancelling task: {e}")

        # Stop socket server explicitly
        await socket_server.stop()

        # Stop the threaded listeners. These were previously left running, so a
        # restart could find 5025/111/1024 still held by the old process.
        for name, srv in (("LXI raw socket", lxi_server),
                          ("VXI-11", vxi11_server),
                          ("mDNS responder", lxi_mdns)):
            if srv is None:
                continue
            try:
                srv.stop()
                logger.info("MAIN", f"{name} stopped.")
            except Exception as e:
                logger.warning("MAIN", f"Error stopping {name}: {e}")

        # Clean up PyVISA connections
        visa.purge_all_resources()
        # Save any dirty config state synchronously before exiting
        config.save_config_sync()
        logger.info("MAIN", "Cleanup finished. Exit.")

if __name__ == "__main__":
    # VMSG's own log buffer lives in memory, so a hard failure would otherwise
    # take the diagnostic trail with it - exactly when it is most needed. This
    # writes the traceback to disk and prints where it went. Nothing is uploaded.
    def _on_unhandled(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        path = None
        try:
            path = crashlog.write_crash_report(exc_type, exc_value, exc_tb)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        if path:
            print(f"\n[Main] Crash report written to: {path}", file=sys.stderr)

    sys.excepthook = _on_unhandled

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Main] Interrupted by user. Exiting.")
        sys.exit(0)
