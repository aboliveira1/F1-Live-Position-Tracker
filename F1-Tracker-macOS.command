#!/bin/sh
# Start F1 Live Track Positions. The first run sets everything up automatically
# (about a minute, needs internet).
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run_f1_tracker.py "$@"
elif command -v python >/dev/null 2>&1; then
    python run_f1_tracker.py "$@"
else
    echo
    echo "  Python isn't installed. Install Python 3.12 or newer from"
    echo "  https://www.python.org/downloads/ and run this again."
    echo
fi
status=$?
if [ "$status" -ne 0 ]; then
    printf "Press Enter to close... "
    read -r _
fi
exit "$status"
