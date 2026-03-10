#!/usr/bin/env python3
"""Publish latest downloaded Blink MP4 clips to MediaMTX as RTSP streams.

Goal
- Reuse our working BirdWatch downloader (which writes MP4 clips to a directory).
- Provide stable RTSP paths for BirdNET-Go UI / VLC without relying on blinkbridge's
  BlinkPy polling/login.

How it works
- Watches a directory for MP4 files that match a pattern (default: *.mp4).
- Groups files by camera name using RTSP_CAMERA_REGEX.
- For each camera, keeps a single long-lived publisher process connected to MediaMTX.
- When a new clip appears, that clip is played once with audio.
- After the clip ends, the stream switches to a still frame from the last clip plus silence.
- When a newer clip appears, content changes without dropping the RTSP publisher session.

Env
- WATCH_DIR: directory to scan (default: /watch)
- GLOB_PATTERN: glob to match clips (default: *.mp4)
- RTSP_CAMERA_REGEX: regex with a named group (?P<camera>...)
- POLL_SEC: rescan interval seconds (default: 5)
- MEDIAMTX_HOST: default: mediamtx
- MEDIAMTX_PORT: default: 8554
- RTSP_TRANSPORT: tcp|udp (default: tcp)
- STREAM_PREFIX: optional prefix for paths (default: "")
- RTSP_STILL_HOLD_SEC: 0 means effectively forever; otherwise duration of each still chunk
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CAMERA_REGEX = r"^(?P<camera>.+?)-.*\.mp4$"


def slugify(name: str) -> str:
    out = name.strip().lower()
    out = re.sub(r"[^a-z0-9]+", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    return out or "camera"


def stop_proc(p: subprocess.Popen | None) -> None:
    if not p or p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGTERM)
        for _ in range(30):
            if p.poll() is not None:
                return
            time.sleep(0.1)
        p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def ffmpeg_base() -> list[str]:
    return [os.getenv("FFMPEG_BIN", "ffmpeg"), "-hide_banner", "-loglevel", "warning"]


def run_extract_last_frame(src: Path, out_jpg: Path) -> None:
    cmd = ffmpeg_base() + [
        "-y",
        "-sseof",
        "-0.2",
        "-i",
        str(src),
        "-frames:v",
        "1",
        str(out_jpg),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_publisher(*, fifo_path: Path, rtsp_url: str, transport: str) -> subprocess.Popen:
    # Persistent publisher: reads MPEG-TS from a named pipe and stays connected to MediaMTX.
    cmd = ffmpeg_base() + [
        "-fflags",
        "+genpts",
        "-re",
        "-i",
        str(fifo_path),
        "-c",
        "copy",
        "-f",
        "rtsp",
        "-rtsp_transport",
        transport,
        rtsp_url,
    ]
    return subprocess.Popen(cmd)


def start_clip_to_pipe(src: Path, fifo_path: Path) -> subprocess.Popen:
    # Normalize into MPEG-TS/H264/AAC so the persistent publisher can copy packets through.
    cmd = ffmpeg_base() + [
        "-y",
        "-re",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-f",
        "mpegts",
        str(fifo_path),
    ]
    return subprocess.Popen(cmd)


def start_still_to_pipe(still_jpg: Path, fifo_path: Path, duration_sec: int) -> subprocess.Popen:
    cmd = ffmpeg_base() + [
        "-y",
        "-re",
        "-loop",
        "1",
        "-i",
        str(still_jpg),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono",
        "-t",
        str(duration_sec),
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-f",
        "mpegts",
        str(fifo_path),
    ]
    return subprocess.Popen(cmd)


@dataclass
class CameraStream:
    camera: str
    stream_name: str
    rtsp_url: str
    fifo_path: Path
    fifo_keepalive_fd: int
    work_dir: Path
    publisher: subprocess.Popen
    thread: threading.Thread | None = None
    stop_event: threading.Event | None = None
    current_src: Path | None = None
    desired_src: Path | None = None
    last_still: Path | None = None
    last_error: str | None = None


def camera_worker(stream: CameraStream, still_chunk_sec: int) -> None:
    fifo_path = stream.fifo_path
    work_dir = stream.work_dir
    stop_event = stream.stop_event
    current_clip_proc: subprocess.Popen | None = None
    still_proc: subprocess.Popen | None = None

    try:
        while not stop_event.is_set():
            desired = stream.desired_src
            if desired is None:
                time.sleep(0.5)
                continue

            if stream.current_src != desired:
                stop_proc(current_clip_proc)
                stop_proc(still_proc)
                current_clip_proc = None
                still_proc = None
                stream.current_src = desired
                try:
                    still_jpg = work_dir / "last.jpg"
                    run_extract_last_frame(desired, still_jpg)
                    stream.last_still = still_jpg
                except Exception as exc:
                    stream.last_error = f"extract last frame failed: {exc}"
                    print(f"[rtsp-publisher] cam={stream.camera} {stream.last_error}")
                    time.sleep(1.0)
                    continue

                print(f"[rtsp-publisher] cam={stream.camera} switching src={desired.name}")
                current_clip_proc = start_clip_to_pipe(desired, fifo_path)
                continue

            if current_clip_proc is not None:
                rc = current_clip_proc.poll()
                if rc is None:
                    time.sleep(0.5)
                    continue
                # Clip finished (or failed). Transition to still chunks.
                if rc != 0:
                    print(f"[rtsp-publisher] cam={stream.camera} clip ffmpeg exited code={rc}")
                current_clip_proc = None
                if stream.last_still is None or not stream.last_still.exists():
                    time.sleep(0.5)
                    continue
                still_proc = start_still_to_pipe(stream.last_still, fifo_path, still_chunk_sec)
                continue

            if still_proc is not None:
                rc = still_proc.poll()
                if rc is None:
                    time.sleep(0.5)
                    continue
                still_proc = None
                # Start another still chunk unless a newer clip has been requested.
                if stop_event.is_set():
                    break
                if stream.desired_src != stream.current_src:
                    continue
                if stream.last_still is not None and stream.last_still.exists():
                    still_proc = start_still_to_pipe(stream.last_still, fifo_path, still_chunk_sec)
                    continue
                time.sleep(0.5)
                continue

            # No active producer; bootstrap a still if possible.
            if stream.last_still is not None and stream.last_still.exists():
                still_proc = start_still_to_pipe(stream.last_still, fifo_path, still_chunk_sec)
            else:
                time.sleep(0.5)
    finally:
        stop_proc(current_clip_proc)
        stop_proc(still_proc)


def main() -> int:
    watch_dir = Path(os.getenv("WATCH_DIR", "/watch")).resolve()
    glob_pattern = os.getenv("GLOB_PATTERN", "*.mp4")
    poll_sec = float(os.getenv("POLL_SEC", "5") or "5")
    mediamtx_host = os.getenv("MEDIAMTX_HOST", "mediamtx")
    mediamtx_port = int(os.getenv("MEDIAMTX_PORT", "8554") or "8554")
    transport = os.getenv("RTSP_TRANSPORT", "tcp")
    stream_prefix = (os.getenv("STREAM_PREFIX", "") or "").strip("/")

    camera_regex = os.getenv("RTSP_CAMERA_REGEX") or DEFAULT_CAMERA_REGEX
    cam_re = re.compile(camera_regex, re.IGNORECASE)

    hold_raw = (os.getenv("RTSP_STILL_HOLD_SEC", "0") or "0").strip()
    try:
        hold_int = int(hold_raw)
    except ValueError:
        hold_int = 0
    still_chunk_sec = 300 if hold_int <= 0 else max(1, hold_int)

    print(f"[rtsp-publisher] watch_dir={watch_dir} glob={glob_pattern} poll_sec={poll_sec}")
    print(f"[rtsp-publisher] mediamtx=rtsp://{mediamtx_host}:{mediamtx_port} transport={transport}")
    print(f"[rtsp-publisher] camera_regex={cam_re.pattern}")

    if not watch_dir.exists():
        print(f"[rtsp-publisher] ERROR watch_dir does not exist: {watch_dir}", file=sys.stderr)
        return 2

    streams: dict[str, CameraStream] = {}
    work_root = Path(tempfile.mkdtemp(prefix="rtsp-publisher-"))

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

            for cam, newest in newest_by_cam.items():
                stream_name = slugify(cam)
                path = f"{stream_prefix}/{stream_name}" if stream_prefix else stream_name
                rtsp_url = f"rtsp://{mediamtx_host}:{mediamtx_port}/{path}"
                existing = streams.get(cam)
                if existing is None:
                    cam_dir = work_root / stream_name
                    cam_dir.mkdir(parents=True, exist_ok=True)
                    fifo_path = cam_dir / "stream.ts"
                    try:
                        os.mkfifo(fifo_path)
                    except FileExistsError:
                        pass
                    fifo_keepalive_fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
                    publisher = start_publisher(fifo_path=fifo_path, rtsp_url=rtsp_url, transport=transport)
                    stop_event = threading.Event()
                    stream = CameraStream(
                        camera=cam,
                        stream_name=path,
                        rtsp_url=rtsp_url,
                        fifo_path=fifo_path,
                        fifo_keepalive_fd=fifo_keepalive_fd,
                        work_dir=cam_dir,
                        publisher=publisher,
                        stop_event=stop_event,
                    )
                    stream.desired_src = newest
                    thread = threading.Thread(target=camera_worker, args=(stream, still_chunk_sec), daemon=True)
                    stream.thread = thread
                    thread.start()
                    streams[cam] = stream
                    print(f"[rtsp-publisher] starting cam={cam} src={newest.name}")
                    print(f"[rtsp-publisher] cam={cam} url={rtsp_url}")
                else:
                    if existing.desired_src != newest:
                        existing.desired_src = newest
                        print(f"[rtsp-publisher] queued cam={cam} src={newest.name}")

            for cam in list(streams.keys()):
                stream = streams[cam]
                if stream.publisher.poll() is not None:
                    print(f"[rtsp-publisher] cam={cam} publisher exited code={stream.publisher.returncode}")
                    stream.stop_event.set()
                    streams.pop(cam, None)

            time.sleep(max(1.0, poll_sec))

    except KeyboardInterrupt:
        print("[rtsp-publisher] shutting down")
    finally:
        for stream in streams.values():
            stream.stop_event.set()
        for stream in streams.values():
            stop_proc(stream.publisher)
        for stream in streams.values():
            if stream.thread.is_alive():
                stream.thread.join(timeout=2.0)
            try:
                os.close(stream.fifo_keepalive_fd)
            except Exception:
                pass
        shutil.rmtree(work_root, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
