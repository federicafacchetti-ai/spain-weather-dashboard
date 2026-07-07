#!/bin/bash
# Start the local dashboard server. Opens the dashboard in your browser
# with a working Refresh button. Keep this Terminal window open while using
# the dashboard. Close the window (or Ctrl-C) to stop the server.

cd "$(dirname "$0")" || exit 1

echo "Starting dashboard server…"
python3 server.py
