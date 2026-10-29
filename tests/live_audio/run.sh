#!/bin/bash
# usage: run.sh LIVE_AUDIO_JS SCENARIO SECONDS TAG  -> results in $LT/out/TAG.*
LT=$(dirname "$(readlink -f "$0")"); JS=$1; SC=$2; SEC=$3; TAG=$4
OUT=$LT/out; mkdir -p "$OUT"; rm -f "$OUT/$TAG".*
PORT=$((18900 + RANDOM % 80))
SINK=falcon_test_$TAG
NODE=$(pw-cli create-node adapter "{ factory.name=support.null-audio-sink node.name=$SINK node.description=$SINK media.class=Audio/Sink audio.position=[FL FR] object.linger=true }" 2>&1 | grep -oE '^[0-9]+|id [0-9]+' | grep -oE '[0-9]+' | head -1)
sleep 0.5
python3 -I "$LT/testserver.py" $PORT "$JS" "$SC" "$OUT/$TAG.events" & SRV=$!
sleep 0.7
timeout $((SEC + 6)) pw-record --target "$SINK" -P stream.capture.sink=true --format s16 --rate 48000 --channels 1 "$OUT/$TAG.wav" >/dev/null 2>&1 & REC=$!
PROF=$(mktemp -d)
PULSE_SINK=$SINK timeout $SEC google-chrome --headless=new --no-sandbox --disable-gpu --autoplay-policy=no-user-gesture-required \
  --user-data-dir="$PROF" --enable-logging=stderr --v=0 "http://127.0.0.1:$PORT/" 2> "$OUT/$TAG.chrome" >/dev/null
sleep 1; kill $REC 2>/dev/null; kill $SRV 2>/dev/null; wait 2>/dev/null
rm -rf "$PROF"; "$LT/cleanup.sh"
grep -E 'CONSOLE' "$OUT/$TAG.chrome" | grep -vE 'GPU|gles' | sed -E 's/.*CONSOLE[^"]*//' | sort | uniq -c | head -8 > "$OUT/$TAG.console"
echo "node=$NODE port=$PORT"
