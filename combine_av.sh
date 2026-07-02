#!/bin/bash
# combine_av.sh — post-process an `audio+screen` archive into one watchable file
# per session: mux screen.mp4 + audio.m4a -> combined.mp4 (lossless stream-copy),
# keep audio.m4a (for ASR), drop the now-redundant screen.mp4.
#
# Safe: verifies the muxed file has BOTH video+audio streams BEFORE deleting
# screen.mp4. Resumable (skips sessions that already have combined.mp4).
# bash 3.2 compatible (no mapfile). Reads the file list on FD 3 and runs
# `ffmpeg -nostdin` so ffmpeg can never consume the loop's input list.
#
# Excludes any session dir listed (one absolute path per line) in
# $EXCLUDE (default /tmp/ghost_dirs.txt) — used to skip deletion candidates
# so they aren't muxed before review/cleanup.
#
# Usage: ROOT=/Volumes/ColdStore/msds-archive bash combine_av.sh
set -u
ROOT="${ROOT:-/Volumes/ColdStore/msds-archive}"
EXCLUDE="${EXCLUDE:-/tmp/ghost_dirs.txt}"
echo "=== combine start: $(date)  root=$ROOT ==="
LIST=$(mktemp)
find "$ROOT" -path '*/panopto/*' -name 'screen.mp4' > "$LIST"
n=$(wc -l < "$LIST" | tr -d ' ')
echo "found $n screen.mp4 files"
total=0; made=0; skipped=0; failed=0
while IFS= read -r s <&3; do
  [ -n "$s" ] || continue
  total=$((total+1))
  dir=$(dirname "$s"); a="$dir/audio.m4a"; c="$dir/combined.mp4"
  if [ -f "$EXCLUDE" ] && grep -qxF "$dir" "$EXCLUDE" 2>/dev/null; then skipped=$((skipped+1)); continue; fi
  if [ -f "$c" ]; then skipped=$((skipped+1)); rm -f "$s"; continue; fi
  if [ ! -f "$a" ]; then echo "SKIP(no audio): $dir"; failed=$((failed+1)); continue; fi
  tmp="$dir/.combined.partial.mp4"; rm -f "$tmp"
  if ffmpeg -nostdin -y -loglevel error -i "$s" -i "$a" -c copy -map 0:v:0 -map 1:a:0 "$tmp"; then
    v=$(ffprobe -v error -select_streams v -show_entries stream=codec_type -of csv=p=0 "$tmp" </dev/null 2>/dev/null | grep -c video)
    au=$(ffprobe -v error -select_streams a -show_entries stream=codec_type -of csv=p=0 "$tmp" </dev/null 2>/dev/null | grep -c audio)
    if [ "${v:-0}" -ge 1 ] && [ "${au:-0}" -ge 1 ]; then
      mv "$tmp" "$c"; rm -f "$s"; made=$((made+1))
    else
      rm -f "$tmp"; echo "FAIL(verify): $dir"; failed=$((failed+1))
    fi
  else
    rm -f "$tmp"; echo "FAIL(ffmpeg): $dir"; failed=$((failed+1))
  fi
  if [ $((total % 20)) -eq 0 ]; then echo "progress: $total/$n made=$made skip=$skipped fail=$failed"; fi
done 3< "$LIST"
rm -f "$LIST"
echo "=== COMBINE COMPLETE: total=$total made=$made skipped=$skipped failed=$failed  $(date) ==="
