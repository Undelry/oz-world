#!/bin/bash
# ============================================
# OZ Demo Video Recorder
# Records the OZ VTuber demo as MP4
# ============================================

set -e

FFMPEG="/opt/homebrew/bin/ffmpeg"
OZ_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="$OZ_DIR/oz_demo_$(date +%Y%m%d_%H%M%S).mp4"
DURATION="${1:-42}"  # Duration in seconds (default 42s)
PORT=8767

echo "=== OZ Demo Video Recorder ==="
echo "Output: $OUTPUT"
echo "Duration: ${DURATION}s"
echo ""

# Check ffmpeg
if [ ! -f "$FFMPEG" ]; then
    echo "ERROR: ffmpeg not found at $FFMPEG"
    exit 1
fi

# Check if OZ server is running on the expected port
if ! curl -s "http://localhost:${PORT}/" > /dev/null 2>&1; then
    echo "WARNING: OZ server does not appear to be running on port ${PORT}"
    echo "Please start it first: cd $OZ_DIR && python oz_webserver.py"
    echo ""
    echo "Trying to continue anyway..."
fi

# --- Method 1: macOS screencapture (built-in, records a window) ---
echo ""
echo "Choose recording method:"
echo "  1) macOS screencapture (interactive window selection)"
echo "  2) FFmpeg screen capture (full screen or region)"
echo ""
read -p "Enter choice [1/2]: " CHOICE

if [ "$CHOICE" = "1" ]; then
    # Use macOS built-in screencapture
    TEMP_MOV="/tmp/oz_demo_temp.mov"

    echo ""
    echo "Step 1: Opening demo page in browser..."
    open "http://localhost:${PORT}/oz_world.html?demo=1"

    echo "Step 2: Waiting 3 seconds for page to load..."
    sleep 3

    echo "Step 3: Starting screen recording (${DURATION}s)..."
    echo "  -> Click on the browser window when prompted"
    echo ""

    # screencapture -V records a specific window
    # -v = video mode, -T = duration
    screencapture -v -T "$DURATION" "$TEMP_MOV" &
    CAPTURE_PID=$!

    echo "Recording PID: $CAPTURE_PID"
    echo "Recording for ${DURATION} seconds... (Ctrl+C to stop early)"

    wait $CAPTURE_PID 2>/dev/null || true

    if [ -f "$TEMP_MOV" ]; then
        echo "Converting to MP4..."
        "$FFMPEG" -y -i "$TEMP_MOV" \
            -c:v libx264 -preset medium -crf 23 \
            -c:a aac -b:a 128k \
            -movflags +faststart \
            "$OUTPUT"
        rm -f "$TEMP_MOV"
        echo ""
        echo "Done! Video saved to: $OUTPUT"
        echo "Size: $(du -h "$OUTPUT" | cut -f1)"
    else
        echo "ERROR: Screen capture failed. Trying FFmpeg method instead..."
        CHOICE="2"
    fi
fi

if [ "$CHOICE" = "2" ]; then
    # Use FFmpeg to capture screen region

    echo ""
    echo "Step 1: Opening demo page in browser..."
    open "http://localhost:${PORT}/oz_world.html?demo=1"

    echo "Step 2: Waiting 3 seconds for page to load..."
    sleep 3

    # Get screen resolution
    SCREEN_W=$(system_profiler SPDisplaysDataType 2>/dev/null | grep Resolution | head -1 | awk '{print $2}')
    SCREEN_H=$(system_profiler SPDisplaysDataType 2>/dev/null | grep Resolution | head -1 | awk '{print $4}')

    if [ -z "$SCREEN_W" ]; then
        SCREEN_W=1920
        SCREEN_H=1080
    fi

    echo "Screen resolution: ${SCREEN_W}x${SCREEN_H}"
    echo ""
    echo "Step 3: Recording screen for ${DURATION}s..."
    echo "  (Press q in this terminal to stop early)"
    echo ""

    # FFmpeg screen capture on macOS using avfoundation
    # Device "1" is typically the screen, "0" is the built-in mic
    "$FFMPEG" -y \
        -f avfoundation \
        -framerate 30 \
        -capture_cursor 1 \
        -i "1:none" \
        -t "$DURATION" \
        -c:v libx264 -preset medium -crf 23 \
        -pix_fmt yuv420p \
        -movflags +faststart \
        "$OUTPUT"

    echo ""
    echo "Done! Video saved to: $OUTPUT"
    echo "Size: $(du -h "$OUTPUT" | cut -f1)"
fi

echo ""
echo "To play: open $OUTPUT"
