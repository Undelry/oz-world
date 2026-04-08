#!/bin/bash
# ============================================
# OZ Demo Video Recorder with Synced Audio
# Generates TTS via macOS `say`, records screen via FFmpeg,
# mixes audio and video with proper timing.
#
# Why not capture system audio live?
# BlackHole works but requires routing system audio through it,
# which mutes the user's speakers. Generating audio offline and
# mixing in post is more reliable and doesn't change audio routing.
# ============================================
set -e

FFMPEG="/opt/homebrew/bin/ffmpeg"
OZ_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT="$OZ_DIR/oz_demo_audio_${TIMESTAMP}.mp4"
AUDIO_DIR="/tmp/oz_audio_${TIMESTAMP}"
DURATION=42
PORT=8767

mkdir -p "$AUDIO_DIR"

echo "=== OZ Demo Recorder (with audio) ==="
echo "Output: $OUTPUT"
echo ""

# === 1. Generate TTS audio files ===
echo "[1/4] Generating TTS audio files..."

# Voice line: time_in_seconds:text (matches oz_demo.js timeline)
declare -a LINES=(
    "1.0:こんにちは！OZの世界へようこそ！"
    "6.5:OZは音声で操作できる、3Dバーチャルワールドです"
    "10.0:コーダーのところに行って"
    "14.5:ワーカーたちに話しかけると、声で返事してくれます"
    "22.0:モニターを見せて"
    "26.0:紫の島に行って"
    "30.0:全ての操作を音声だけで。PCとスマホの全てがOZの中に"
    "37.0:OZ。新しい世界へ"
)

for i in "${!LINES[@]}"; do
    line="${LINES[$i]}"
    time="${line%%:*}"
    text="${line#*:}"
    output_file="$AUDIO_DIR/line_${i}.aiff"
    say -v Kyoko -r 200 -o "$output_file" "$text"
    printf "  %s @ %ss: %s\n" "line_${i}" "$time" "$text"
done

# === 2. Build a single audio track ===
echo ""
echo "[2/4] Building synced audio track..."

# Silence base
"$FFMPEG" -y -f lavfi -i "anullsrc=r=44100:cl=stereo" -t $DURATION -c:a pcm_s16le "$AUDIO_DIR/silence.wav" >/dev/null 2>&1

# Build inputs and filter graph
INPUTS=( -i "$AUDIO_DIR/silence.wav" )
FILTER=""
for i in "${!LINES[@]}"; do
    line="${LINES[$i]}"
    time="${line%%:*}"
    delay_ms=$(awk "BEGIN { printf \"%d\", $time * 1000 }")
    INPUTS+=( -i "$AUDIO_DIR/line_${i}.aiff" )
    FILTER="${FILTER}[$((i+1)):a]adelay=${delay_ms}|${delay_ms}[d${i}];"
done

# Mix all delayed tracks with silence base
MIX_INPUTS="[0:a]"
for i in "${!LINES[@]}"; do
    MIX_INPUTS="${MIX_INPUTS}[d${i}]"
done
MIX_COUNT=$((${#LINES[@]} + 1))
FILTER="${FILTER}${MIX_INPUTS}amix=inputs=${MIX_COUNT}:duration=longest:dropout_transition=0[aout]"

"$FFMPEG" -y "${INPUTS[@]}" -filter_complex "$FILTER" -map "[aout]" -t $DURATION -c:a aac -b:a 192k "$AUDIO_DIR/voice_track.m4a" >/dev/null 2>&1

echo "  Audio track ready"

# === 3. Open demo and start screen recording ===
echo ""
echo "[3/4] Opening demo and recording screen for ${DURATION}s..."

# Open OZ demo in Arc and ensure it's the front tab
osascript -e '
tell application "Arc"
    activate
    delay 0.5
    tell front window
        make new tab with properties {URL:"http://localhost:'${PORT}'/oz_world.html?demo=1"}
    end tell
end tell
' > /dev/null

# Wait for the page to fully load (oz_world.html shows a loading screen for ~1.5s)
sleep 4

# Verify the demo tab is actually loaded by checking the page title via JavaScript
PAGE_TITLE=$(osascript -e '
tell application "Arc"
    tell front window
        tell active tab
            execute javascript "document.title"
        end tell
    end tell
end tell
' 2>/dev/null | tr -d '"')

echo "  Active tab title: $PAGE_TITLE"

if [[ "$PAGE_TITLE" != *"OZ"* ]] && [[ "$PAGE_TITLE" != *"Virtual"* ]]; then
    echo "  ERROR: OZ demo is not the active tab!"
    echo "  Aborting recording."
    exit 1
fi

# Make absolutely sure Arc is at the front
osascript -e 'tell application "Arc" to activate' > /dev/null
sleep 0.5

# Get Arc WebView position+size via Accessibility (AXSplitGroup is the web area).
# This excludes Arc's left sidebar and top URL bar — exactly what we want.
WEBVIEW_INFO=$(osascript << 'OSAEOF' 2>/dev/null
tell application "Arc" to activate
delay 0.3
tell application "System Events"
    tell process "Arc"
        repeat with i from 1 to count of windows
            try
                set w to window i
                set wn to name of w
                if wn contains "OZ" or wn contains "Virtual" or wn contains "localhost" then
                    set elemList to entire contents of w
                    repeat with elem in elemList
                        try
                            if role of elem is "AXSplitGroup" then
                                set p to position of elem
                                set s to size of elem
                                return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
                            end if
                        end try
                    end repeat
                end if
            end try
        end repeat
    end tell
end tell
return "0,0,1470,956"
OSAEOF
)

echo "  Arc WebView (logical px): $WEBVIEW_INFO"

WV_X=$(echo "$WEBVIEW_INFO" | cut -d, -f1)
WV_Y=$(echo "$WEBVIEW_INFO" | cut -d, -f2)
WV_W=$(echo "$WEBVIEW_INFO" | cut -d, -f3)
WV_H=$(echo "$WEBVIEW_INFO" | cut -d, -f4)

# Default fallback
if [ -z "$WV_X" ] || [ -z "$WV_W" ]; then
    WV_X=242; WV_Y=83; WV_W=1218; WV_H=863
fi

# avfoundation captures device pixels (Retina 2x of logical px)
CROP_X=$((WV_X * 2))
CROP_Y=$((WV_Y * 2))
CROP_W=$((WV_W * 2))
CROP_H=$((WV_H * 2))

# Round crop dimensions to even numbers (h264 requirement)
CROP_W=$(( (CROP_W / 2) * 2 ))
CROP_H=$(( (CROP_H / 2) * 2 ))

echo "  Crop region (device px): ${CROP_W}x${CROP_H}+${CROP_X}+${CROP_Y}"

# Make absolutely sure Arc is at the front
osascript -e 'tell application "Arc" to activate' > /dev/null
sleep 1.5

VIDEO_FILE="$AUDIO_DIR/screen.mp4"

# Record screen via avfoundation, then crop to Arc's WebView area only
"$FFMPEG" -y \
    -f avfoundation \
    -framerate 30 \
    -capture_cursor 1 \
    -i "1:none" \
    -t $DURATION \
    -vf "crop=${CROP_W}:${CROP_H}:${CROP_X}:${CROP_Y}" \
    -c:v libx264 -preset fast -crf 23 \
    -pix_fmt yuv420p \
    "$VIDEO_FILE" >/dev/null 2>&1

# === 4. Combine video + audio ===
echo ""
echo "[4/4] Combining video + audio..."

"$FFMPEG" -y \
    -i "$VIDEO_FILE" \
    -i "$AUDIO_DIR/voice_track.m4a" \
    -c:v copy -c:a aac -b:a 192k \
    -shortest \
    -movflags +faststart \
    "$OUTPUT" >/dev/null 2>&1

echo ""
echo "=== Done ==="
echo "Output: $OUTPUT"
echo "Size: $(du -h "$OUTPUT" | cut -f1)"

# Cleanup
rm -rf "$AUDIO_DIR"

echo ""
echo "Play: open \"$OUTPUT\""
