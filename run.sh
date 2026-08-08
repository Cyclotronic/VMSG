#!/bin/bash
# Startup launcher script for VISA Mapping TCP/IP Socket Gateway (VMSG)

echo "============================================="
echo "  VISA Mapping TCP/IP Socket Gateway (VMSG)"
echo "  (Implementing Prologix Controls)"
echo "============================================="

# Verify python3 is installed
if ! command -v python3 &> /dev/null; then
    echo "[-] Error: python3 is not installed or not in PATH."
    exit 1
fi

# Run the emulator
echo "[*] Launching VMSG servers on Port 8080 (Web) and Port 1234 (Socket)..."
python3 vmsg.py
