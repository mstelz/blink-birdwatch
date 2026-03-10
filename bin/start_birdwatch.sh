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

  # Start in background. Export vars explicitly; publisher reads RTSP_CAMERA_REGEX directly.
  export PYTHONUNBUFFERED=1
  export WATCH_DIR="$RTSP_WATCH_DIR"
  export MEDIAMTX_HOST="$MEDIAMTX_HOST"
  export MEDIAMTX_PORT="$MEDIAMTX_PORT"
  export POLL_SEC="${RTSP_POLL_SEC:-5}"
  export GLOB_PATTERN="${RTSP_GLOB_PATTERN:-*.mp4}"
  export RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
  export STREAM_PREFIX="${RTSP_STREAM_PREFIX:-}"
  export RTSP_VIDEO_FPS="${RTSP_VIDEO_FPS:-15}"
  export RTSP_H264_PRESET="${RTSP_H264_PRESET:-veryfast}"
  export RTSP_H264_CRF="${RTSP_H264_CRF:-23}"
  export RTSP_MJPEG_Q="${RTSP_MJPEG_Q:-2}"
  # Force a known-good regex here. The container-start env for RTSP_CAMERA_REGEX has proven
  # vulnerable to mangling in some deployments even when later interactive shells look clean.
  export RTSP_CAMERA_REGEX='^(?P<camera>.+)-\d{4}-.*\.mp4$'
  python3 /app/bin/rtsp_publisher.py &
fi

exec python3 /app/bin/blink_service.py
