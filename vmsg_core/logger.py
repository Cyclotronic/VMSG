import datetime
import threading
from typing import List, Dict, Any

class SystemLogger:
    """A thread-safe in-memory log buffer that stores recent logs for retrieval by the web UI."""
    def __init__(self, max_logs: int = 1000):
        self.max_logs = max_logs
        self.logs: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        
        # Dynamic logging filters (defaults)
        self.log_level = "WARN"
        self.enable_stdout = False  # Disabled by default for clean operation
        self.log_category_traffic = True
        self.log_category_visa = True
        self.log_category_system = True

    def configure(self, settings: Dict[str, Any]) -> None:
        """Dynamically configures log filtering parameters."""
        with self.lock:
            self.log_level = str(settings.get("log_level", "WARN")).upper()
            self.enable_stdout = bool(settings.get("enable_stdout", False))
            self.log_category_traffic = bool(settings.get("log_category_traffic", True))
            self.log_category_visa = bool(settings.get("log_category_visa", True))
            self.log_category_system = bool(settings.get("log_category_system", True))

    def log(self, level: str, category: str, message: str) -> None:
        """Appends a log message to the buffer, applying dynamic filters."""
        level_upper = level.upper()
        cat_upper = category.upper()
        
        # 1. Level threshold check
        level_map = {"DEBUG": 0, "INFO": 1, "WARN": 2, "WARNING": 2, "ERROR": 3}
        current_threshold = level_map.get(self.log_level, 0)
        incoming_level = level_map.get(level_upper, 1)
        if incoming_level < current_threshold:
            return
            
        # 2. Category toggles check
        if cat_upper in ["TRAFFIC_IN", "TRAFFIC_OUT"]:
            if not self.log_category_traffic:
                return
        elif cat_upper in ["INSTR_WRITE", "INSTR_READ", "SCAN", "HEALER", "VISAMANAGER"]:
            if not self.log_category_visa:
                return
        else:
            if not self.log_category_system:
                return

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_entry = {
            "timestamp": timestamp,
            "level": level_upper,
            "category": cat_upper,
            "message": message
        }
        with self.lock:
            self.logs.append(log_entry)
            if len(self.logs) > self.max_logs:
                self.logs.pop(0)
        
        # 4. Standard output printing
        if self.enable_stdout:
            print(f"[{timestamp}] [{level_upper}] [{cat_upper}] {message}", flush=True)

    def debug(self, category: str, message: str) -> None:
        self.log("DEBUG", category, message)

    def info(self, category: str, message: str) -> None:
        self.log("INFO", category, message)

    def warning(self, category: str, message: str) -> None:
        self.log("WARN", category, message)

    def error(self, category: str, message: str) -> None:
        self.log("ERROR", category, message)

    def get_logs(self) -> List[Dict[str, Any]]:
        """Returns a copy of the log buffer."""
        with self.lock:
            return list(self.logs)

    def clear(self) -> None:
        """Clears the log buffer."""
        with self.lock:
            self.logs.clear()

# Global logger instance
logger = SystemLogger()
