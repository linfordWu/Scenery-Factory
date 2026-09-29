#!/bin/bash
# learning-video skill: remove edge-hugging watermarks (e.g. "由AI生成") from a video.
# ffmpeg's delogo filter refuses rectangles touching the frame border, so this uses
# crop -> gblur -> overlay on the two bottom corners instead.
# Run INSIDE the comfyui container. Usage:
#   dewatermark.sh <input.mp4> <output.mp4> [y]      # y defaults: height-57 for 1080 -> 1023
set -e
IN="$1"; OUT="$2"
[ -n "$IN" ] && [ -n "$OUT" ] || { echo "usage: dewatermark.sh <in> <out> [y]"; exit 1; }

DIM=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$IN")
W=$(echo "$DIM" | cut -d, -f1); H=$(echo "$DIM" | cut -d, -f2)
Y="${3:-$((H - 57))}"
RX=$((W - 290))

FILTER="[0:v]split=2[main][t1];[t1]crop=270:55:10:${Y},gblur=sigma=25[L];[main][L]overlay=10:${Y}[m1];[m1]split=2[m2][t2];[t2]crop=270:55:${RX}:${Y},gblur=sigma=25[R];[m2][R]overlay=${RX}:${Y}"

ffmpeg -y -v warning -i "$IN" -filter_complex "$FILTER" \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p -c:a copy -movflags +faststart "$OUT"
echo "WROTE $OUT (${W}x${H}, delogo y=${Y})"
