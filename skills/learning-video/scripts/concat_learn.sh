#!/bin/bash
# learning-video skill: concat generated clips into the final video.
# Run INSIDE the comfyui container (needs ffmpeg + access to output/video).
# Usage: concat_learn.sh <output_name> <file_prefix>
#   e.g. concat_learn.sh learn_final.mp4 learn_clip
set -e
cd /opt/ComfyUI/output/video
OUT="${1:-learn_final.mp4}"
PREFIX="${2:-learn_clip}"

LIST=/tmp/learn_concat.txt
> $LIST
for i in $(seq -w 1 99); do
  f="${PREFIX}${i}_00001_.mp4"
  [ -f "$f" ] && echo "file '$PWD/$f'" >> $LIST
done
[ -s $LIST ] || { echo "no clips found for prefix ${PREFIX}"; exit 1; }
cat $LIST

ffmpeg -y -v warning -f concat -safe 0 -i $LIST \
  -vf "scale=1920:1080:flags=lanczos,setsar=1" \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -movflags +faststart "/opt/ComfyUI/output/video/$OUT"
echo "WROTE $OUT"
ffprobe -v error -show_entries format=duration -of csv=p=0 "/opt/ComfyUI/output/video/$OUT"
