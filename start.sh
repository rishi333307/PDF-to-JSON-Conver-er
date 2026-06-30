#!/bin/bash
# ===========================================================
#  One-click starter for Mac/Linux.
#  Just double-click this file (or run ./start.sh) every time.
#  First run: creates a virtual environment and installs everything.
#  Every run after that: just starts the server (fast).
# ===========================================================

cd "$(dirname "$0")/backend"

if [ ! -d "venv" ]; then
    echo "[Setup] First time setup - creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    echo "[Setup] Installing required libraries... this may take a minute."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo ""
echo "==========================================================="
echo "  Backend server starting at http://127.0.0.1:5000"
echo "  Opening the upload page in your browser..."
echo "  (Keep this window open while you use the app)"
echo "==========================================================="
echo ""

# Open the frontend through the Flask server itself (not as a local
# file) -- app.py now serves the upload page directly at "/", and the
# page's JavaScript expects to be loaded this way so it can correctly
# detect its own API address. Opening index.html directly as a file
# would break the upload button.
(sleep 1.5 && {
    if command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:5000"        # Mac
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:5000"    # Linux
    fi
}) &

# Start the Flask backend (keeps running until you press Ctrl+C)
python app.py
