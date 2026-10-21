#!/usr/bin/env bash
#
# Compile the pictures from timelapse.py into a video with ffmpeg.
#
# Usage:
#   ./make_video.sh [options] [FOLDER] [OUTPUT]
#
#   FOLDER   folder with the pictures   (default: timelapse_frames)
#   OUTPUT   video file to write        (default: timelapse.mp4)
#
# Options:
#   -r FPS     frames per second in the video (default 30)
#   -s         stamp the full date and time (2026-10-17 20:38:10)
#              in the top-right corner
#   -d         discreet stamp: only the hour and minutes (20:38)
#   -t TITLE   print a sentence before the timestamp, e.g. -t "My garden"
#              (switches the stamp on by itself; combine with -d for a
#              short one, e.g. "My garden - 20:38")
#   -w WIDTH   scale the video to this width, e.g. 1280 (default: picture size)
#   -q CRF     quality (optional, default 23, which suits most timelapses):
#              lower = better quality and bigger file (18-20),
#              higher = smaller file and softer picture (26-28)
#   -n         don't use a GPU; encode on the CPU only
#   -y         overwrite OUTPUT if it already exists
#   -h         show this help
#
# If -s and -d are both given, -d wins.
# A GPU is used automatically when ffmpeg can use one (much faster):
# NVIDIA first, then AMD/Intel on Linux (a dedicated card before the
# integrated one), or Apple on macOS. Otherwise the CPU does the work.
# Use -n to force the CPU. AMD/Intel on Linux need their VAAPI driver:
#   sudo apt install mesa-va-drivers intel-media-va-driver vainfo
#
# Examples:
#   ./make_video.sh
#   ./make_video.sh -r 25 -s timelapse_frames garden.mp4
#   ./make_video.sh -d -t "Garden, spring 2026" -y timelapse_frames garden.mp4
#
# Needs ffmpeg (with ffprobe): sudo apt install ffmpeg / brew install ffmpeg

set -euo pipefail
export LC_ALL=C   # sort pictures by name in byte order = time order

# Print the comment block at the top of this file as the help text.
usage() { awk 'NR >= 3 { if (/^#/) { sub(/^# ?/, ""); print } else exit }' "$0"; }
die()   { echo "Error: $*" >&2; exit 1; }

FPS=30
STAMP=0
DISCREET=0
TITLE=""
WIDTH=""
CRF=23
OVERWRITE=0
CPU_ONLY=0

while getopts ":r:sdt:w:q:ynh" opt; do
  case "$opt" in
    r) FPS="$OPTARG" ;;
    s) STAMP=1 ;;
    d) STAMP=1; DISCREET=1 ;;
    t) TITLE="$OPTARG"; STAMP=1 ;;
    w) WIDTH="$OPTARG" ;;
    q) CRF="$OPTARG" ;;
    y) OVERWRITE=1 ;;
    n) CPU_ONLY=1 ;;
    h) usage; exit 0 ;;
    :) die "-$OPTARG needs a value (see -h)" ;;
    *) die "unknown option -$OPTARG (see -h)" ;;
  esac
done
shift $((OPTIND - 1))

FOLDER="${1:-timelapse_frames}"
OUTPUT="${2:-timelapse.mp4}"

[[ "$FPS" =~ ^[0-9]+([.][0-9]+)?$ ]] && [[ "$FPS" != 0 ]] || die "-r must be a number, e.g. 30"
[[ -z "$WIDTH" || "$WIDTH" =~ ^[0-9]+$ ]] || die "-w must be a whole number, e.g. 1280"
[[ "$CRF" =~ ^[0-9]+$ ]] || die "-q must be a whole number, e.g. 23"
command -v ffmpeg  >/dev/null || die "ffmpeg isn't installed (sudo apt install ffmpeg / brew install ffmpeg)"
command -v ffprobe >/dev/null || die "ffprobe isn't installed (it comes with ffmpeg)"
[[ -d "$FOLDER" ]] || die "folder '$FOLDER' not found"

# Absolute paths, because ffmpeg runs from inside the picture folder.
FOLDER="$(cd "$FOLDER" && pwd)"
OUT_DIR="$(cd "$(dirname "$OUTPUT")" 2>/dev/null && pwd)" || die "folder for '$OUTPUT' doesn't exist"
OUTPUT="$OUT_DIR/$(basename "$OUTPUT")"
if [[ -e "$OUTPUT" && $OVERWRITE -eq 0 ]]; then
  die "'$OUTPUT' already exists; use -y to overwrite it"
fi

# The pictures, in time order (same pattern ffmpeg will use below).
PATTERN='[0-9]*.jpg'
shopt -s nullglob
cd "$FOLDER"
FILES=( $PATTERN )
COUNT=${#FILES[@]}
(( COUNT > 0 )) || die "no pictures (like 20261017-203810.jpg) in $FOLDER"

# Size of the first picture; every frame is scaled to it in case the
# camera resolution changed during the timelapse. Sizes must be even.
SIZE=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
         -of csv=p=0:s=x "${FILES[0]}") || die "can't read ${FILES[0]}"
W=${SIZE%%x*}; H=${SIZE##*x}
[[ "$W" =~ ^[0-9]+$ && "$H" =~ ^[0-9]+$ ]] || die "can't read the size of ${FILES[0]}"
if [[ -n "$WIDTH" ]]; then
  H=$(( H * WIDTH / W ))
  W=$WIDTH
fi
W=$(( W / 2 * 2 )); H=$(( H / 2 * 2 ))
FILTER="scale=${W}:${H},format=yuv420p"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Date and time stamp: a subtitle file with one entry per picture,
# taken from the file names (20261017-203810.jpg -> 2026-10-17 20:38:10,
# or just 20:38 with -d). An optional title (-t) goes in front.
if (( STAMP )); then
  FILTERS_LIST="$(ffmpeg -hide_banner -filters 2>/dev/null || true)"
  [[ "$FILTERS_LIST" == *" subtitles "* ]] \
    || die "this ffmpeg has no 'subtitles' filter (needs libass), so -s/-d/-t won't work"

  # Curly braces would be read as subtitle formatting codes; swap them out.
  TITLE="${TITLE//\{/(}"
  TITLE="${TITLE//\}/)}"
  # Newlines in the title would break the subtitle file; make them spaces.
  TITLE="${TITLE//$'\n'/ }"

  # The title goes in through the environment so awk doesn't touch any
  # backslashes or other special characters in it.
  printf '%s\n' "${FILES[@]}" | STAMP_TITLE="$TITLE" awk -v fps="$FPS" -v discreet="$DISCREET" '
    function ts(ms,  h, m, s) {
      h = int(ms / 3600000); ms -= h * 3600000
      m = int(ms / 60000);   ms -= m * 60000
      s = int(ms / 1000);    ms -= s * 1000
      return sprintf("%02d:%02d:%02d,%03d", h, m, s, ms)
    }
    BEGIN { title = ENVIRON["STAMP_TITLE"] }
    {
      n = $0
      if (discreet == 1) {
        text = substr(n,10,2) ":" substr(n,12,2)
      } else {
        text = substr(n,1,4) "-" substr(n,5,2) "-" substr(n,7,2) " " \
               substr(n,10,2) ":" substr(n,12,2) ":" substr(n,14,2)
      }
      if (title != "") text = title " - " text
      # {\an9} = top-right. This tag always uses the 1-9 keypad layout,
      # unlike force_style Alignment, which differs between ffmpeg builds.
      text = "{\\an9}" text
      start = int((NR - 1) * 1000 / fps); end = int(NR * 1000 / fps)
      printf "%d\n%s --> %s\n%s\n\n", NR, ts(start), ts(end), text
    }' > "$TMP/stamp.srt"
  # Font size is relative to a 288-pixel-high canvas, so it scales with the video.
  # The corner (top-right) is set by the {\an9} tag in the subtitle text.
  STYLE="Fontname=DejaVu Sans,Fontsize=11,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=0,MarginR=8,MarginV=6"
  FILTER="$FILTER,subtitles=filename='$TMP/stamp.srt':force_style='$STYLE'"
fi

# Pick the encoder. GPU encoders are tried first, and each one is only used
# if a tiny test encode really works (so a missing GPU or driver just means
# "try the next one"):
#   1. NVIDIA (NVENC)
#   2. AMD / Intel via VAAPI on Linux, dedicated card before integrated
#   3. Apple VideoToolbox on macOS
# Otherwise: H.264 on the CPU, and if that's missing, MPEG-4.
ENCODERS_LIST="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
ENCODER_NAME=""
HW_IN=()                 # extra input options a GPU encoder needs
HW_FILTER=""             # extra filters a GPU encoder needs at the end
PROBE_IN=()
PROBE_VF="format=yuv420p"
CODEC_TRY=()

# Test-encode a tiny black picture with PROBE_IN / PROBE_VF / CODEC_TRY.
probe() {
  ffmpeg -hide_banner -loglevel error ${PROBE_IN[@]+"${PROBE_IN[@]}"} \
    -f lavfi -i color=c=black:s=256x256:d=0.2 \
    -vf "$PROBE_VF" "${CODEC_TRY[@]}" -f null - >/dev/null 2>&1
}

# VAAPI render devices, dedicated graphics cards first. The built-in
# graphics chip is usually the "boot" display (boot_vga=1), so the others
# are the dedicated ones.
vaapi_nodes() {
  local node dev first=() rest=()
  for node in /dev/dri/renderD*; do
    [[ -e "$node" ]] || continue
    dev="/sys/class/drm/$(basename "$node")/device"
    if [[ -r "$dev/boot_vga" && "$(cat "$dev/boot_vga" 2>/dev/null)" == 0 ]]; then
      first+=("$node")
    else
      rest+=("$node")
    fi
  done
  printf '%s\n' ${first[@]+"${first[@]}"} ${rest[@]+"${rest[@]}"}
}

if (( ! CPU_ONLY )); then
  # 1. NVIDIA
  if [[ "$ENCODERS_LIST" == *" h264_nvenc "* ]]; then
    CODEC_TRY=(-c:v h264_nvenc -preset p5 -rc vbr -cq "$CRF" -b:v 0)
    if probe; then
      CODEC=("${CODEC_TRY[@]}")
      ENCODER_NAME="NVIDIA GPU (h264_nvenc)"
    fi
  fi

  # 2. AMD / Intel (Linux, VAAPI)
  if [[ -z "$ENCODER_NAME" && "$ENCODERS_LIST" == *" h264_vaapi "* ]]; then
    while IFS= read -r NODE; do
      [[ -n "$NODE" ]] || continue
      PROBE_IN=(-vaapi_device "$NODE")
      PROBE_VF="format=nv12,hwupload"
      CODEC_TRY=(-c:v h264_vaapi -qp "$CRF")
      if probe; then
        CODEC=("${CODEC_TRY[@]}")
        HW_IN=("${PROBE_IN[@]}")
        HW_FILTER=",format=nv12,hwupload"
        ENCODER_NAME="GPU via VAAPI ($NODE)"
        break
      fi
    done < <(vaapi_nodes)
  fi

  # 3. Apple (macOS)
  if [[ -z "$ENCODER_NAME" && "$ENCODERS_LIST" == *" h264_videotoolbox "* ]]; then
    VTQ=$(( 100 - 2 * CRF ))
    if (( VTQ < 1 )); then VTQ=1; fi
    PROBE_IN=()
    PROBE_VF="format=yuv420p"
    CODEC_TRY=(-c:v h264_videotoolbox -q:v "$VTQ")
    if probe; then
      CODEC=("${CODEC_TRY[@]}")
      ENCODER_NAME="Apple GPU (VideoToolbox)"
    fi
  fi

  if [[ -z "$ENCODER_NAME" ]]; then
    echo "Note: no usable GPU encoder found, using the CPU." >&2
  fi
fi

if [[ -z "$ENCODER_NAME" ]]; then
  if [[ "$ENCODERS_LIST" == *" libx264 "* ]]; then
    CODEC=(-c:v libx264 -preset medium -crf "$CRF")
    ENCODER_NAME="CPU (libx264)"
  else
    echo "Note: libx264 not available, using MPEG-4 instead." >&2
    CODEC=(-c:v mpeg4 -q:v $(( CRF / 6 + 1 )))
    ENCODER_NAME="CPU (mpeg4)"
  fi
fi

# GPU encoders that need the picture uploaded to the card get it here.
FILTER="$FILTER$HW_FILTER"

# Lets .mp4/.mov files start playing before they're fully downloaded.
MOVFLAGS=()
case "${OUTPUT##*.}" in mp4|MP4|mov|MOV|m4v) MOVFLAGS=(-movflags +faststart) ;; esac

SECONDS_LONG=$(awk -v n="$COUNT" -v f="$FPS" 'BEGIN { printf "%.1f", n / f }')
echo "Compiling $(basename "$OUTPUT") from $COUNT pictures at $FPS fps (${SECONDS_LONG}s, ${W}x${H})…"
echo "Encoder: $ENCODER_NAME"

ffmpeg -hide_banner -loglevel error -stats -y \
  ${HW_IN[@]+"${HW_IN[@]}"} \
  -framerate "$FPS" -pattern_type glob -i "$PATTERN" \
  -vf "$FILTER" \
  "${CODEC[@]}" -r "$FPS" "${MOVFLAGS[@]}" \
  "$TMP/out.${OUTPUT##*.}"

mv -f "$TMP/out.${OUTPUT##*.}" "$OUTPUT"
echo "Compiled $OUTPUT"
