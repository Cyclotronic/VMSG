import os
import json
import copy
import threading
from typing import Dict, Any, Optional

from .logger import logger
from .path_helper import get_writable_config_path

DEFAULT_MAPPINGS_FILE = get_writable_config_path()

DEFAULT_CONFIG = {
    "settings": {
        "addr": 1,
        "auto": 1,
        "mode": 1,
        "eos": 0,
        "eoi": 1,
        "read_tmo_ms": 3000,
        "eot_enable": 0,
        "eot_char": 4,
        "lon": 0,
        "savecfg": 1,
        "unmapped_behavior": "message",
        "auto_heal_usb": True,
        # Default logging controller settings
        "log_level": "WARN",
        "enable_stdout": False,
        "log_category_traffic": True,
        "log_category_visa": True,
        "log_category_system": True
    },
    "mappings": {}  # Virtual address (0-30) -> {"visa_address": str, "idn_pattern": str, "description": str}
}

class ConfigManager:
    """Manages persistent config mappings and Prologix emulator settings in a thread-safe manner."""
    def __init__(self, filepath: str = DEFAULT_MAPPINGS_FILE):
        self.filepath = filepath
        self.lock = threading.Lock()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.load_config()

    def load_config(self) -> None:
        """Loads configuration from JSON file. Creates default configuration if file does not exist."""
        with self.lock:
            target_path = self.filepath
            if not os.path.exists(target_path):
                example_path = os.path.join(os.path.dirname(self.filepath), "mappings.example.json")
                if os.path.exists(example_path):
                    target_path = example_path

            if os.path.exists(target_path):
                try:
                    with open(target_path, "r") as f:
                        loaded = json.load(f)
                        if "settings" not in loaded:
                            loaded["settings"] = copy.deepcopy(DEFAULT_CONFIG["settings"])
                        if "mappings" not in loaded:
                            loaded["mappings"] = {}
                        
                        # Merge settings with defaults to ensure all keys are present
                        for k, v in DEFAULT_CONFIG["settings"].items():
                            if k not in loaded["settings"]:
                                loaded["settings"][k] = v
                                
                        self.config = loaded
                except Exception as e:
                    print(f"[ConfigManager] Error loading config, resetting to default: {e}")
                    self.config = copy.deepcopy(DEFAULT_CONFIG)
            else:
                self.config = copy.deepcopy(DEFAULT_CONFIG)
                
            self._save_config_unlocked()
            # Ensure savecfg is initialized to 1 for VMSG operations
            self.config["settings"]["savecfg"] = 1

        # Propagate config settings to global logger
        logger.configure(self.config["settings"])

    def _save_config_unlocked(self) -> None:
        """Saves configuration atomically to JSON file."""
        try:
            config_snapshot = copy.deepcopy(self.config)
            filepath = self.filepath
            def _disk_write():
                try:
                    with open(filepath, "w") as f:
                        json.dump(config_snapshot, f, indent=4)
                        f.flush()
                except Exception as e:
                    print(f"[ConfigManager] Error writing config to {filepath}: {e}")

            threading.Thread(target=_disk_write, daemon=True).start()
        except Exception as e:
            print(f"[ConfigManager] Error preparing config write to {self.filepath}: {e}")

    def save_config(self) -> None:
        """Saves configuration to JSON file thread-safely."""
        with self.lock:
            self._save_config_unlocked()

    def get_settings(self) -> Dict[str, Any]:
        """Gets a copy of the Prologix controller settings."""
        with self.lock:
            return copy.deepcopy(self.config["settings"])

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Gets a specific setting value."""
        with self.lock:
            return self.config["settings"].get(key, default)

    def set_runtime_setting(self, key: str, value: Any) -> None:
        """Updates in-memory setting without writing to disk."""
        with self.lock:
            self.config["settings"][key] = value

    def update_settings(self, new_settings: Dict[str, Any], persist: bool = True) -> None:
        """Updates multiple settings and persists changes if requested."""
        with self.lock:
            for k, v in new_settings.items():
                if k in DEFAULT_CONFIG["settings"]:
                    # Validate types / ranges
                    if k == "addr":
                        val = int(v)
                        if 0 <= val <= 30:
                            self.config["settings"][k] = val
                    elif k in ["auto", "mode", "eoi", "eot_enable", "lon", "savecfg"]:
                        self.config["settings"][k] = 1 if v else 0
                    elif k in ["enable_stdout", "log_category_traffic", "log_category_visa", "log_category_system"]:
                        self.config["settings"][k] = True if v else False
                    elif k == "log_level":
                        val = str(v).upper()
                        if val in ["DEBUG", "INFO", "WARN", "WARNING", "ERROR"]:
                            self.config["settings"][k] = val
                    elif k == "eos":
                        val = int(v)
                        if 0 <= val <= 3:
                            self.config["settings"][k] = val
                    elif k == "read_tmo_ms":
                        val = int(v)
                        if val > 0:
                            self.config["settings"][k] = val
                    elif k == "eot_char":
                        val = int(v)
                        if 0 <= val <= 255:
                            self.config["settings"][k] = val
                    else:
                        self.config["settings"][k] = v

            if persist:
                self._save_config_unlocked()
            
        # Dynamically sync configurations to global logger instance
        logger.configure(self.config["settings"])

    def update_setting(self, key: str, value: Any, persist: bool = True) -> None:
        """Updates a single setting."""
        self.update_settings({key: value}, persist=persist)

    def get_mappings(self) -> Dict[str, Dict[str, str]]:
        """Gets virtual GPIB mappings (0-30)."""
        with self.lock:
            return copy.deepcopy(self.config["mappings"])

    def get_mapping(self, address: int) -> Optional[Dict[str, str]]:
        """Gets mapping for a specific virtual address (0-30)."""
        with self.lock:
            addr_str = str(address)
            m = self.config["mappings"].get(addr_str)
            return copy.deepcopy(m) if m else None

    def set_mapping(self, address: int, visa_address: str, idn_pattern: str = "", description: str = "") -> None:
        """Sets or updates mapping for a virtual address (0-30) and persists change."""
        if not (0 <= address <= 30):
            raise ValueError("Virtual address must be between 0 and 30")
        with self.lock:
            addr_str = str(address)
            self.config["mappings"][addr_str] = {
                "visa_address": visa_address,
                "idn_pattern": idn_pattern,
                "description": description
            }
            self._save_config_unlocked()

    def delete_mapping(self, address: int) -> bool:
        """Deletes mapping for a virtual address. Returns True if found and deleted."""
        if not (0 <= address <= 30):
            raise ValueError("Virtual address must be between 0 and 30")
        with self.lock:
            addr_str = str(address)
            if addr_str in self.config["mappings"]:
                del self.config["mappings"][addr_str]
                self._save_config_unlocked()
                return True
            return False

    def clear_all_mappings(self) -> None:
        """Clears all address mappings."""
        with self.lock:
            self.config["mappings"] = {}
            self._save_config_unlocked()


