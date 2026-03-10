#!/usr/bin/env python3
"""Publish latest downloaded Blink MP4 clips to MediaMTX as RTSP streams.

Goal
- Reuse our working BirdWatch downloader (which writes MP4 clips to a directory).
- Provide stable RTSP paths for BirdNET-Go UI / VLC without relying on blinkbridge's
  BlinkPy polling/login.

How it works
- Watches a directory for MP4 files that match a pattern (default: *.mp4).
- Groups files by "camera" inferred from filename prefix before the first '-' character.
  Example: bird-feeder-2026-03-08t12-30-26-00-00.mp4 -> camera="bird" (not desired)

So we instead support CAMERA_REGEX to capture the camera name. Default expects:
  <camera-name>-YYYY-...
  where camera-name may contain dashes.

Example filenames we have seen:
  bird-feeder-2026-03-08t12-30-26-00-00.mp4

This script will infer camera="bird-feeder".

For each camera, it runs an ffmpeg process that loops the newest clip and publishes to:
  rtsp://<MEDIAMTX_HOST>:<MEDIAMTX_PORT>/<STREAM_NAME>

When a newer file appears for that camera, the ffmpeg process is restarted.

Env
- WATCH_DIR: directory to scan (default: /watch)
- GLOB_PATTERN: glob to match clips (default: *.mp4)
- CAMERA_REGEX: regex with a named group (?P<camera>...) (default below)
- POLL_SEC: rescan interval seconds (default: 5)
- MEDIAMTX_HOST: default: mediamtx
- MEDIAMTX_PORT: default: 8554
- RTSP_TRANSPORT: tcp|udp (default: tcp)
- STREAM_PREFIX: optional prefix for paths (default: "")

Notes
- This is intentionally simple: loop latest clip. No still-frame concat pipeline.
  It should be good enough for BirdNET-Go to connect to a stable RTSP URL.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CAMERA_REGEX = r"^(?P<camera>.+?)-\d{4}-\d{2}-\d{2}[Tt]\d{2}-\d{2}-\d{2}(?:-\d{1,6})?(?:[+-]\d{2}-\d{2})?\.mp4$"


def slugify(name: str) -> str:
    out = name.strip().lower()
    out = re.sub(r"[^a-z0-9]+", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    return out or "camera"


@dataclass
class StreamProc:
    camera: str
    stream_name: str
    src: Path
    proc: subprocess.Popen


def start_ffmpeg(*, src: Path, rtsp_url: str, transport: str) -> subprocess.Popen:
    # Loop the file forever. -re to simulate realtime.
    # Transcode audio to AAC for compatibility; copy video if possible.
    ffmpeg_bin = os.getenv("FFMPEG_BIN", "ffmpeg")
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stream_loop",
        "-1",
        "-re",
        "-i",
        str(src),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-f",
        "rtsp",
        "-rtsp_transport",
        transport,
        rtsp_url,
    ]
    return subprocess.Popen(cmd)


def stop_proc(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGTERM)
        for _ in range(20):
            if p.poll() is not None:
                return
            time.sleep(0.1)
        p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def main() -> int:
    watch_dir = Path(os.getenv("WATCH_DIR", "/watch")).resolve()
    glob_pattern = os.getenv("GLOB_PATTERN", "*.mp4")
    poll_sec = float(os.getenv("POLL_SEC", "5") or "5")
    mediamtx_host = os.getenv("MEDIAMTX_HOST", "mediamtx")
    mediamtx_port = int(os.getenv("MEDIAMTX_PORT", "8554") or "8554")
    transport = os.getenv("RTSP_TRANSPORT", "tcp")
    stream_prefix = (os.getenv("STREAM_PREFIX", "") or "").strip("/")

    cam_re = re.compile(os.getenv("CAMERA_REGEX", DEFAULT_CAMERA_REGEX), re.IGNORECASE)

    print(f"[rtsp-publisher] watch_dir={watch_dir} glob={glob_pattern} poll_sec={poll_sec}")
    print(f"[rtsp-publisher] mediamtx=rtsp://{mediamtx_host}:{mediamtx_port} transport={transport}")
    print(f"[rtsp-publisher] camera_regex={cam_re.pattern}")

    if not watch_dir.exists():
        print(f"[rtsp-publisher] ERROR watch_dir does not exist: {watch_dir}", file=sys.stderr)
        return 2

    procs: dict[str, StreamProc] = {}

    last_skip_report: float = 0.0
    last_file_count: int | None = None
    last_cam_summary: tuple[tuple[str, str], ...] | None = None

    try:
        while True:
            files = sorted(watch_dir.glob(glob_pattern))
            newest_by_cam: dict[str, Path] = {}
            skipped: list[str] = []

            for f in files:
                m = cam_re.match(f.name)
                if m:
                    cam = m.group("camera")
                else:
                    # Back-compat fallback: old persisted names looked like blink_<timestamp>.mp4
                    # and carry no camera information. Publish them under a generic stream
                    # instead of silently ignoring them.
                    if re.match(r"^blink_\d{4}-\d{2}-\d{2}[Tt]\d{2}-\d{2}-\d{2}", f.name, re.IGNORECASE):
                        cam = "blink"
                    elif re.match(r"^download-\d{4}-\d{2}-\d{2}[Tt]\d{2}-\d{2}-\d{2}", f.name, re.IGNORECASE):
                        cam = "download"
                    else:
                        skipped.append(f.name)
                        continue
                prev = newest_by_cam.get(cam)
                if not prev or f.stat().st_mtime > prev.stat().st_mtime:
                    newest_by_cam[cam] = f

            cam_summary = tuple(sorted((cam, path.name) for cam, path in newest_by_cam.items()))
            if last_file_count != len(files) or last_cam_summary != cam_summary:
                print(f"[rtsp-publisher] scan files={len(files)} matched_cams={len(newest_by_cam)}")
                for cam, path_name in cam_summary:
                    print(f"[rtsp-publisher] matched cam={cam} newest={path_name}")
                last_file_count = len(files)
                last_cam_summary = cam_summary

            now = time.time()
            if skipped and now - last_skip_report >= max(30.0, poll_sec):
                sample = ", ".join(skipped[:5])
                extra = "" if len(skipped) <= 5 else f" (+{len(skipped) - 5} more)"
                print(f"[rtsp-publisher] skipped {len(skipped)} file(s): {sample}{extra}")
                last_skip_report = now

            # start/restart streams
            for cam, newest in newest_by_cam.items():
                stream_name = slugify(cam)
                if stream_prefix:
                    path = f"{stream_prefix}/{stream_name}"
                else:
                    path = stream_name

                rtsp_url = f"rtsp://{mediamtx_host}:{mediamtx_port}/{path}"

                existing = procs.get(cam)
                if existing and existing.src == newest and existing.proc.poll() is None:
                    continue

                if existing:
                    print(f"[rtsp-publisher] restarting cam={cam} src={newest.name}")
                    stop_proc(existing.proc)
                else:
                    print(f"[rtsp-publisher] starting cam={cam} src={newest.name}")

                p = start_ffmpeg(src=newest, rtsp_url=rtsp_url, transport=transport)
                procs[cam] = StreamProc(camera=cam, stream_name=path, src=newest, proc=p)
                print(f"[rtsp-publisher] cam={cam} url={rtsp_url}")

            # cleanup dead
            for cam in list(procs.keys()):
                if procs[cam].proc.poll() is not None:
                    print(f"[rtsp-publisher] cam={cam} ffmpeg exited code={procs[cam].proc.returncode}")
                    procs.pop(cam, None)

            time.sleep(max(1.0, poll_sec))

    except KeyboardInterrupt:
        print("[rtsp-publisher] shutting down")
    finally:
        for sp in procs.values():
            stop_proc(sp.proc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
