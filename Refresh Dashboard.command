#!/bin/bash
# One-click AEMET dashboard refresh.
# Double-click this file in Finder → runs the fetch, updates dashboard.html,
# opens it in your default browser.

# Move to the folder this file lives in (so relative paths work regardless of where it's launched)
cd "$(dirname "$0")" || exit 1

echo "──────────────────────────────────────────────────────"
echo "  AEMET Weather Dashboard — Refresh"
echo "──────────────────────────────────────────────────────"
echo ""

# Run the fetch
python3 aemet_fetch.py
STATUS=$?

echo ""
if [ $STATUS -eq 0 ]; then
    echo "✓ Refresh complete. Opening dashboard…"
    open index.html
else
    echo "✗ Refresh failed (exit code $STATUS). Check data/last_run.log for details."
fi

echo ""
echo "You can close this window."
