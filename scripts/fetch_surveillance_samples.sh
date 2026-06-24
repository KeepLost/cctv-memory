#!/usr/bin/env bash
# Reproducibly fetch + cut the realistic surveillance test clips.
#
# Source: VIRAT Video Dataset, Ground Camera, Release 2.0, hosted publicly on
# Kitware Girder (data.kitware.com) — no login/token/CAPTCHA required.
# Usage/license: VIRAT Ground is under the "VIRAT Video Dataset Protection
# Agreement" (research use); see docs/test-data/surveillance_samples_provenance.md.
#
# The resulting .mp4 clips live under data/test_videos/ which is gitignored, so
# they are LOCAL fixtures (not committed). This script + the provenance doc are the
# versioned, reproducible record. Total download is ~4.6 MB.
#
# Safety: bounded curl timeouts, fixed binary (no shell-interpolated URLs from
# untrusted input), non-interactive ffmpeg (stdin=/dev/null).
set -euo pipefail

OUT_DIR="data/test_videos/surveillance_samples"
TMP_DIR="$(mktemp -d /tmp/virat_src.XXXXXX)"
GIRDER="https://data.kitware.com/api/v1/item"

mkdir -p "$OUT_DIR"

# itemId | original filename | output clip name
CLIPS=(
  "56f588218d777f753209ccde|VIRAT_S_010208_07_000768_000791.mp4|surveillance_virat_parking_10s.mp4"
  "56f5879c8d777f753209cb34|VIRAT_S_010003_07_000608_000636.mp4|surveillance_virat_pedestrian_10s.mp4"
  "56f587aa8d777f753209cb70|VIRAT_S_010005_05_000397_000430.mp4|surveillance_virat_street_10s.mp4"
)

for entry in "${CLIPS[@]}"; do
  IFS='|' read -r item_id orig_name out_name <<<"$entry"
  src="$TMP_DIR/$orig_name"
  echo ">> downloading $orig_name ($item_id)"
  curl -fsSL --max-time 120 -o "$src" "$GIRDER/$item_id/download"
  echo ">> cutting 10s -> $OUT_DIR/$out_name"
  ffmpeg -y -nostdin -ss 00:00:02 -i "$src" -t 10 \
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -an \
    "$OUT_DIR/$out_name" </dev/null >/dev/null 2>&1
  echo ">> ffprobe:"
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height,r_frame_rate,nb_frames \
    -show_entries format=duration,size -of default=noprint_wrappers=1 \
    "$OUT_DIR/$out_name"
  echo
done

rm -rf "$TMP_DIR"
echo "Done. Clips in $OUT_DIR (gitignored, local fixtures)."
