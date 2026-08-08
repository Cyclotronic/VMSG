import os
import sys

def get_resource_path(relative_path):
    """
    Get the absolute path to a resource, supporting both:
    1. Standard development mode (located relative to this package directory)
    2. Compiled standalone binaries (extracted into temporary directory sys._MEIPASS)
    """
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller temp extraction directory
        return os.path.join(sys._MEIPASS, relative_path)
    
    # Standard development: go up one folder from vmsg_core/ to find the root folder
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

def get_writable_config_path(filename="mappings.json"):
    """
    Get the path to a writable configuration file.
    In compiled single-file binaries, files inside sys._MEIPASS are read-only and deleted on exit.
    To ensure persistence, we default to the Current Working Directory (CWD).
    """
    return os.path.join(os.getcwd(), filename)
