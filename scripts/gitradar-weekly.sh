#!/bin/bash
# gitradar-weekly.sh — shell wrapper for weekly mode
# Runs the full re-evaluation scan (re-evaluates cached repos for new activity).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/gitradar-discover.py" --mode weekly
