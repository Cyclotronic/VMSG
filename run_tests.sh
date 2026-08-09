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

# Run the test suites inside the tests subfolder
python3 tests/test_prologix_gateway.py
python3 tests/test_query_atomicity_and_config.py
