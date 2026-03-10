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

  # Start in background. Export vars explicitly; publisher reads RTSP_CAMERA_REGEX directly
  # (and still supports legacy CAMERA_REGEX as a fallback).
  export PYTHONUNBUFFERED=1
  export WATCH_DIR="$RTSP_WATCH_DIR"
  export MEDIAMTX_HOST="$MEDIAMTX_HOST"
  export MEDIAMTX_PORT="$MEDIAMTX_PORT"
  export POLL_SEC="${RTSP_POLL_SEC:-5}"
  export GLOB_PATTERN="${RTSP_GLOB_PATTERN:-*.mp4}"
  export RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
  export STREAM_PREFIX="${RTSP_STREAM_PREFIX:-}"
  export RTSP_CAMERA_REGEX="${RTSP_CAMERA_REGEX:-^(?P<camera>.+?)-.*\.mp4$}"
  python3 /app/bin/rtsp_publisher.py &
fi

exec python3 /app/bin/blink_service.py
