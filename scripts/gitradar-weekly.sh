#!/bin/bash
# gitradar-weekly.sh — shell wrapper for safe weekly mode
# Runs the full re-evaluation scan, validates output, then scores recommendations.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/gitradar-safe-run.py" --mode weekly
