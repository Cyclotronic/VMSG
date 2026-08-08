import sys
import os
import subprocess

def main():
    print("====================================================")
    print("       VMSG Standalone Binary Compiler Helper       ")
    print("====================================================")

    # 1. Check for PyInstaller
    try:
        import PyInstaller
        print("[+] PyInstaller is installed.")
    except ImportError:
        print("[!] PyInstaller is not installed.")
        print("[*] Installing PyInstaller via pip...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
            print("[+] PyInstaller installed successfully.")
        except Exception as e:
            print(f"[-] Error installing PyInstaller: {e}")
            print("[*] Please install it manually with: pip install pyinstaller")
            sys.exit(1)

    # 2. Determine OS-specific separator for --add-data
    # Windows uses ';' whereas Linux/macOS uses ':'
    sep = ";" if os.name == 'nt' else ":"
    static_data = f"static{sep}static"

    # 3. Construct PyInstaller command running PyInstaller as a module via sys.executable
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--clean",
        "--name=vmsg",
        f"--add-data={static_data}",
        "--hidden-import=pyvisa_py",
        "--hidden-import=serial",
        "--hidden-import=usb",
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespan",
        "--hidden-import=uvicorn.lifespan.on",
        "vmsg.py"
    ]

    print("\n[*] Constructing compilation command:")
    print("    " + " ".join(cmd))
    print("\n[*] Launching PyInstaller build process...")
    
    try:
        subprocess.check_call(cmd)
        print("\n====================================================")
        print("[+] BUILD COMPLETED SUCCESSFULLY!")
        print(f"[+] Standalone binary is located in: {os.path.abspath('dist')}")
        print("====================================================")
    except subprocess.CalledProcessError:
        print("\n[-] Build failed. Please check the logs above for errors.")
        sys.exit(1)

if __name__ == "__main__":
    main()
