#!/bin/bash
# Launcher script for VMSG integration tests

echo "============================================="
echo "  VMSG - Integration Test Client"
echo "============================================="

# Verify python3 is installed
if ! command -v python3 &> /dev/null; then
    echo "[-] Error: python3 is not installed or not in PATH."
    exit 1
fi

# Run the test suites
python3 test_emulator.py
python3 test_round5_verification.py
