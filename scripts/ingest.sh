#!/bin/sh
# Log the day's footage: one row per clip, a contact sheet and a transcript per clip.
#   sh scripts/ingest.sh <incoming-dir> <out-dir>
# Reads the camera originals and never writes beside them (rule 1 of the brief: the source
# is never edited). For each clip: ffprobe facts into clips.tsv (the capture time is the
# QuickTime creationdate, the clock the film is cut on), a 4x3 sheet of frames, 16 kHz mono
# audio and a whisper.cpp transcript (srt + full json with token times, for the subtitles).
# Idempotent: a clip whose outputs exist is skipped, so it can run while Photos still exports;
# a file still growing is left for the next run.
set -eu
in=$1 out=$2
# WHISPER_MODEL: the whisper.cpp model file (the film used ggml-large-v3-turbo-q5_0.bin); no default path
model=${WHISPER_MODEL:?set WHISPER_MODEL to a whisper.cpp model file, e.g. ggml-large-v3-turbo-q5_0.bin}
mkdir -p "$out/sheets" "$out/audio" "$out/transcripts"
tsv=$out/clips.tsv
[ -f "$tsv" ] || printf 'clip\tcreated\tduration\twidth\theight\tfps\tcodec\taudio\tsize\n' > "$tsv"

for f in "$in"/*.MOV "$in"/*.mov "$in"/*.MP4 "$in"/*.mp4; do
  [ -f "$f" ] || continue
  c=$(basename "$f"); c=${c%.*}
  # wc -c follows symlinks and behaves the same on macOS and Linux (BSD `stat -f %z` fails on GNU stat)
  a=$(wc -c < "$f" | tr -d ' '); sleep 1; b=$(wc -c < "$f" | tr -d ' ')
  # No pipefail in sh: an unreadable file gives two empty sizes, which must not count as finished.
  [ -n "$a" ] && [ "$a" = "$b" ] || { echo "skip $c (still exporting or unreadable)"; continue; }
  if ! grep -q "^$c	" "$tsv"; then
    j=$(ffprobe -v error -print_format json -show_format -show_streams "$f")
    printf '%s' "$j" | python3 -c '
import json,sys
d=json.load(sys.stdin);c=sys.argv[1];sz=sys.argv[2]
v=next((s for s in d["streams"] if s["codec_type"]=="video"),{})
au=next((s for s in d["streams"] if s["codec_type"]=="audio"),None)
t=d["format"].get("tags",{})
cr=t.get("com.apple.quicktime.creationdate") or t.get("creation_time","")
fr=v.get("avg_frame_rate","0/1");n,dd=(fr.split("/")+["1"])[:2]
fps=round(float(n)/float(dd),3) if float(dd) else 0
# iPhone portrait clips are stored landscape with a rotation side-data entry
rot=0
for sd in v.get("side_data_list",[]): rot=int(sd.get("rotation",0) or 0)
w,h=v.get("width",0),v.get("height",0)
if abs(rot)==90: w,h=h,w
print("\t".join(map(str,[c,cr,round(float(d["format"]["duration"]),2),w,h,fps,v.get("codec_name",""),
  (au["codec_name"]+"/"+au.get("channel_layout","")) if au else "none",sz])))' "$c" "$a" >> "$tsv"
  fi
  [ -f "$out/sheets/$c.jpg" ] || {
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
    step=$(python3 -c "print(max(float('$dur')/12,0.5))")
    ffmpeg -v error -y -i "$f" -vf "fps=1/$step,scale=480:-2,tile=4x3:padding=4" -frames:v 1 -update 1 "$out/sheets/$c.jpg" || echo "sheet failed $c"
  }
  [ -f "$out/audio/$c.wav" ] || ffmpeg -v error -y -i "$f" -vn -ac 1 -ar 16000 "$out/audio/$c.wav" 2>/dev/null || echo "no audio $c"
  if [ -f "$out/audio/$c.wav" ] && [ ! -f "$out/transcripts/$c.json" ]; then
    whisper-cli -m "$model" -f "$out/audio/$c.wav" -l auto -t 8 -ojf -osrt -np \
      -of "$out/transcripts/$c" >/dev/null 2>&1 || echo "whisper failed $c"
  fi
  echo "done $c"
done
