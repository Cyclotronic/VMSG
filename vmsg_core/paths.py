"""
Where VMSG keeps its configuration and logs, on any platform.

Deliberately pure standard library and free of Qt: the crash handler has to be
installed before Qt is imported, so it cannot depend on QStandardPaths.

Two rules the rest of the application relies on:

  * nothing is stored in the Windows registry. QSettings' native backend is the
    registry on Windows, a plist on macOS and an INI file on Linux -- three
    opaque, non-portable stores for the same data. We use a plain INI file in
    the platform's own config directory instead, so the file can be read,
    copied between machines, diffed and deleted by hand.

  * a directory beside the application wins when it is writable, which makes a
    portable install possible: unzip, run, and the settings travel with it.
"""
import os
import sys

APP_NAME = "VMSG"

#: Set to a directory to override everything below. Chiefly for tests and for
#: running several configurations side by side.
CONFIG_DIR_ENV = "BENCHFORGE_CONFIG_DIR"

#: A file of this name beside the executable turns on portable mode.
PORTABLE_MARKER = "vmsg-portable.ini"


def _app_directory() -> str:
    """The directory the application was launched from."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK)


def portable_root() -> str:
    """
    The portable configuration directory, or '' when not in portable mode.

    Portable mode is opt-in: the marker file has to exist. Writing settings
    beside the executable by default would fail under Program Files and would
    silently share one configuration between every user of a machine.
    """
    root = _app_directory()
    marker = os.path.join(root, PORTABLE_MARKER)
    if os.path.isfile(marker) and _writable(root):
        return root
    return ""


def config_dir() -> str:
    """Per-user configuration directory, created on demand."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return override

    portable = portable_root()
    if portable:
        return portable

    if sys.platform == "win32":
        # Roaming: preferences follow the user between machines on a domain.
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/" + APP_NAME)
    # XDG, with the specification's own fallback.
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def log_dir() -> str:
    """
    Directory for crash reports.

    Separate from config on every platform, because logs are machine-local
    diagnostics rather than preferences and should not roam or sync.
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return os.path.join(override, "logs")

    portable = portable_root()
    if portable:
        return os.path.join(portable, "logs")

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME, "logs")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/" + APP_NAME)
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.expanduser("~/.local/state"))
    return os.path.join(base, APP_NAME, "logs")


def settings_file() -> str:
    """Full path to the INI file holding user preferences."""
    return os.path.join(config_dir(), "vmsg.ini")


def ensure_dir(path: str) -> bool:
    """Create a directory if it is missing. False when that is not possible."""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False
