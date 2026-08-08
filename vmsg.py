import asyncio
import sys
import warnings

# Suppress noisy PyVISA UserWarning regarding manual SCPI terminators
warnings.filterwarnings("ignore", message="write message already ends with termination characters")

from vmsg_core.config_manager import ConfigManager
from vmsg_core.visa_manager import VisaManager
from vmsg_core.prologix_server import PrologixSocketServer
from vmsg_core.web_app import create_app
from vmsg_core.logger import logger

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
    
    # 3. Create Prologix socket server on Port 1234
    socket_server = PrologixSocketServer("0.0.0.0", 1234, config, visa)
    
    # 4. Create FastAPI App on Port 8080
    app = create_app(config, visa, socket_server)

    # 5. Configure and start Uvicorn web server asynchronously
    import uvicorn
    uvicorn_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="warning",  # Keep output clean, custom logs handle API visibility
        loop="asyncio"
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    logger.info("MAIN", "Starting servers...")
    
    # Run socket server and web server concurrently
    socket_task = asyncio.create_task(socket_server.start())
    web_task = asyncio.create_task(uvicorn_server.serve())

    # 6. Run USB Lottery Healing asynchronously after servers are up
    async def _async_startup_healing():
        await asyncio.sleep(0.5)
        logger.info("HEALER", "Executing startup automated USB Lottery Healing...")
        try:
            mappings = config.get_mappings()
            healing_actions = await asyncio.to_thread(visa.heal_mappings, mappings)
            for action in healing_actions:
                addr = action["virtual_address"]
                new_addr = action["new_visa_address"]
                mapping_entry = mappings.get(str(addr), {})
                config.set_mapping(
                    address=addr,
                    visa_address=new_addr,
                    idn_pattern=mapping_entry.get("idn_pattern", ""),
                    description=mapping_entry.get("description", "")
                )
                logger.info("HEALER", f"Auto-Healed Address {addr} on startup: {action['old_visa_address']} -> {new_addr}")
            if not healing_actions:
                logger.info("HEALER", "All expected instruments are present and verified at their ports.")
        except Exception as e:
            logger.warning("HEALER", f"Startup Lottery Healing encountered an issue: {e}")

    asyncio.create_task(_async_startup_healing())

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
        # Clean up PyVISA connections
        visa.purge_all_resources()
        logger.info("MAIN", "Cleanup finished. Exit.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Main] Interrupted by user. Exiting.")
        sys.exit(0)
