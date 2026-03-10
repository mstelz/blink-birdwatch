#!/usr/bin/env bash
set -euo pipefail

# BirdWatch entrypoint
# - Always runs blink_service.py (HTTP bridge + fetch loop)
# - Optionally runs the RTSP publisher in the background to publish downloaded clips to MediaMTX.

if [[ "${ENABLE_RTSP_PUBLISHER:-0}" == "1" ]]; then
  echo "[birdwatch] RTSP publisher enabled"
  : "${MEDIAMTX_HOST:=mediamtx}"
  : "${MEDIAMTX_PORT:=8554}"

  # By default publish clips from the same directory blink_fetch downloads into.
  : "${RTSP_WATCH_DIR:=${BLINK_DOWNLOAD_DIR:-/app/work/blink-downloads}}"

  # Start in background. Assign CAMERA_REGEX separately to avoid weird shell/env mangling
  # when regex text is passed inline with many metacharacters.
  export PYTHONUNBUFFERED=1
  export WATCH_DIR="$RTSP_WATCH_DIR"
  export MEDIAMTX_HOST="$MEDIAMTX_HOST"
  export MEDIAMTX_PORT="$MEDIAMTX_PORT"
  export POLL_SEC="${RTSP_POLL_SEC:-5}"
  export GLOB_PATTERN="${RTSP_GLOB_PATTERN:-*.mp4}"
  export RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
  export STREAM_PREFIX="${RTSP_STREAM_PREFIX:-}"
  export CAMERA_REGEX="${RTSP_CAMERA_REGEX:-^(?P<camera>.+?)-\\d{4}-\\d{2}-\\d{2}[Tt]\\d{2}-\\d{2}-\\d{2}(?:-\\d{1,6})?(?:[+-]?\\d{2}-\\d{2})?\\.mp4$}"
  python3 /app/bin/rtsp_publisher.py &
fi

exec python3 /app/bin/blink_service.py
