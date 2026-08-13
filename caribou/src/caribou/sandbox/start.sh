#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Use $HOME which is set to /home/sandboxuser in the Dockerfile
KERNEL_SCRIPT_PATH="$HOME/start_kernel.py"
CONNECTION_FILE_PATH="$HOME/kernel-connection.json"
KERNEL_LOG_PATH="/tmp/kernel.log" # Keep logs in /tmp

echo "[start.sh] Starting Jupyter Kernel ($KERNEL_SCRIPT_PATH) in background..."
# The API/control service always uses the image environment.  Only the analysis
# kernel switches to an explicitly mounted host prefix.
KERNEL_PYTHON="${CARIBOU_PYTHON_EXECUTABLE:-python}"
if [ -n "${CARIBOU_PYTHON_EXECUTABLE:-}" ]; then
    echo "[start.sh] Validating mounted analysis Python: $KERNEL_PYTHON"
    if ! "$KERNEL_PYTHON" -c "import ipykernel" > /tmp/python-env-check.log 2>&1; then
        echo "[start.sh] ERROR: Selected environment cannot import ipykernel."
        cat /tmp/python-env-check.log
        exit 1
    fi
fi
# Start the kernel using the script in the user's home directory.
# Redirect kernel output to a log file.
"$KERNEL_PYTHON" "$KERNEL_SCRIPT_PATH" > "$KERNEL_LOG_PATH" 2>&1 &

# Store the PID of the kernel process
KERNEL_PID=$!
echo "[start.sh] Kernel process started with PID: $KERNEL_PID"

# Wait a few seconds to allow the kernel to initialize and write the connection file
echo "[start.sh] Waiting 5 seconds for kernel to initialize..."
sleep 5

# Check if the kernel process is still running
if ! kill -0 $KERNEL_PID > /dev/null 2>&1; then
    echo "[start.sh] ERROR: Kernel process died shortly after starting. Check $KERNEL_LOG_PATH"
    cat "$KERNEL_LOG_PATH" # Print kernel log on error
    exit 1
fi
echo "[start.sh] Kernel process appears to be running."

# Check if connection file was created using the dynamic path
if [ ! -f "$CONNECTION_FILE_PATH" ]; then
    echo "[start.sh] ERROR: Kernel connection file ($CONNECTION_FILE_PATH) was not created. Check $KERNEL_LOG_PATH"
    cat "$KERNEL_LOG_PATH" # Print kernel log on error
    exit 1
fi
echo "[start.sh] Kernel connection file found at $CONNECTION_FILE_PATH."


echo "[start.sh] Starting FastAPI Uvicorn server..."
# Start the FastAPI application using Uvicorn.
# Assumes kernel_api.py is in the WORKDIR ($HOME).
# --host 0.0.0.0 makes it accessible from outside the container (host machine).
# --port 8000 is the standard port, ensure it's mapped in docker run/compose.
# Use exec to replace the shell process with uvicorn, allowing tini to manage it correctly.
exec /usr/local/envs/rapids/bin/python -m uvicorn kernel_api:app --host 0.0.0.0 --port 8000 --log-level debug
