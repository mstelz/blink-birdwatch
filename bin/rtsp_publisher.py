#!/usr/bin/env python3
"""Publish latest downloaded Blink MP4 clips to MediaMTX as RTSP streams.

Design goals
- Keep one long-lived ffmpeg RTSP publisher process per camera.
- Avoid reconnecting MediaMTX or downstream RTSP readers when content changes.
- Play each new clip once with audio, then fall back to the clip's last frame plus silence.
- Avoid MPEG-TS segment handoff corruption by keeping the RTSP publisher alive and feeding it
  continuous raw MJPEG frames + PCM audio over local TCP sockets.

How it works
- Watches a directory for MP4 files that match a pattern (default: *.mp4).
- Groups files by camera name using RTSP_CAMERA_REGEX.
- For each camera, starts one persistent ffmpeg publisher with two local TCP inputs:
  * audio: mono 48 kHz s16le PCM
  * video: concatenated MJPEG frames at RTSP_VIDEO_FPS
- When a new clip appears, short-lived ffmpeg decoders emit raw audio/video to stdout.
  Python forwards those bytes into the already-connected local sockets.
- After the clip ends, lightweight Python filler threads keep sending the last JPEG frame and
  silent PCM until a newer clip arrives.
- Switching clips only swaps the upstream feeders; the RTSP publisher process stays put.

Env
- WATCH_DIR: directory to scan (default: /watch)
- GLOB_PATTERN: glob to match clips (default: *.mp4)
- RTSP_CAMERA_REGEX: regex with a named group (?P<camera>...)
- POLL_SEC: rescan interval seconds (default: 5)
- MEDIAMTX_HOST: default: mediamtx
- MEDIAMTX_PORT: default: 8554
- RTSP_TRANSPORT: tcp|udp (default: tcp)
- STREAM_PREFIX: optional prefix for paths (default: "")
- RTSP_STILL_HOLD_SEC: kept for compatibility; 0 means effectively forever. Non-zero values only
  affect log messaging because the new design holds the still indefinitely until replaced.
- RTSP_VIDEO_FPS: publisher input/output fps for clips + stills (default: 15)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_CAMERA_REGEX = r"^(?P<camera>.+)-\d{4}-.*\.mp4$"
TIMESTAMP_IN_NAME_RE = re.compile(
    r"^(?P<camera>.+)-(?P<stamp>\d{4}-\d{2}-\d{2}[Tt]\d{2}-\d{2}-\d{2}(?:-\d{1,6})?(?:[+-]?\d{2}-\d{2})?)\.mp4$",
    re.IGNORECASE,
)
AUDIO_SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 1
PCM_BYTES_PER_SAMPLE = 2


@dataclass
class ClipProbe:
    has_video: bool
    has_audio: bool


@dataclass
class PumpedProc:
    proc: subprocess.Popen
    stop_event: threading.Event
    threads: list[threading.Thread]


class StreamSocketServer:
    def __init__(self, label: str):
        self.label = label
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(1)
        self._listen.settimeout(0.5)
        self.host, self.port = self._listen.getsockname()
        self.url = f"tcp://{self.host}:{self.port}"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._conn: socket.socket | None = None
        self._accept_thread = threading.Thread(target=self._accept_loop, name=f"accept-{label}", daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                old = self._conn
                self._conn = conn
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass

    def _drop_conn(self, conn: socket.socket | None = None) -> None:
        with self._lock:
            target = self._conn if conn is None or self._conn is conn else None
            if target is not None:
                self._conn = None
        if target is not None:
            try:
                target.close()
            except OSError:
                pass

    def wait_for_connection(self, timeout_sec: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.1, timeout_sec)
        while not self._stop.is_set() and time.monotonic() < deadline:
            with self._lock:
                if self._conn is not None:
                    return True
            time.sleep(0.05)
        with self._lock:
            return self._conn is not None

    def write(self, data: bytes, stop_event: threading.Event | None = None) -> None:
        if not data:
            return
        while not self._stop.is_set() and not (stop_event and stop_event.is_set()):
            with self._lock:
                conn = self._conn
            if conn is None:
                time.sleep(0.05)
                continue
            try:
                conn.sendall(data)
                return
            except (BrokenPipeError, ConnectionResetError, OSError):
                self._drop_conn(conn)
                time.sleep(0.05)
        raise BrokenPipeError(f"stream socket closed: {self.label}")

    def close(self) -> None:
        self._stop.set()
        try:
            self._listen.close()
        except OSError:
            pass
        self._drop_conn()
        if self._accept_thread.is_alive():
            self._accept_thread.join(timeout=1.0)


@dataclass
class CameraStream:
    camera: str
    stream_name: str
    rtsp_url: str
    audio_server: StreamSocketServer
    video_server: StreamSocketServer
    work_dir: Path
    publisher: subprocess.Popen
    thread: threading.Thread | None = None
    stop_event: threading.Event | None = None
    current_src: Path | None = None
    desired_src: Path | None = None
    last_still: Path | None = None
    last_error: str | None = None


def slugify(name: str) -> str:
    out = name.strip().lower()
    out = re.sub(r"[^a-z0-9]+", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    return out or "camera"


def _parse_name_timestamp(path: Path | None) -> float:
    if path is None:
        return float("-inf")

    m = TIMESTAMP_IN_NAME_RE.match(path.name)
    if not m:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return float("-inf")

    stamp = m.group("stamp")
    txt = stamp.replace("t", "T")
    txt = re.sub(r"T(\d{2})-(\d{2})-(\d{2})-(\d{1,6})([+-]\d{2})-(\d{2})$", r"T\1:\2:\3.\4\5:\6", txt)
    txt = re.sub(r"T(\d{2})-(\d{2})-(\d{2})([+-]\d{2})-(\d{2})$", r"T\1:\2:\3\4:\5", txt)
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return float("-inf")


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
    return [os.getenv("FFMPEG_BIN", "ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin"]


def ffprobe_bin() -> str:
    return os.getenv("FFPROBE_BIN", "ffprobe")


def wait_for_file_ready(path: Path, *, attempts: int = 12, interval_sec: float = 0.25) -> bool:
    previous: tuple[int, int] | None = None
    stable_hits = 0
    for _ in range(max(1, attempts)):
        try:
            st = path.stat()
        except FileNotFoundError:
            previous = None
            stable_hits = 0
            time.sleep(interval_sec)
            continue
        if st.st_size <= 0:
            previous = (st.st_size, st.st_mtime_ns)
            stable_hits = 0
            time.sleep(interval_sec)
            continue
        current = (st.st_size, st.st_mtime_ns)
        if current == previous:
            stable_hits += 1
            if stable_hits >= 1:
                return True
        else:
            previous = current
            stable_hits = 0
        time.sleep(interval_sec)
    try:
        return path.exists() and path.stat().st_size > 0
    except FileNotFoundError:
        return False


def probe_clip(src: Path) -> ClipProbe:
    cmd = [
        ffprobe_bin(),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "json",
        str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(proc.stdout or "{}")
    has_video = False
    has_audio = False
    for stream in payload.get("streams") or []:
        codec_type = (stream or {}).get("codec_type")
        if codec_type == "video":
            has_video = True
        elif codec_type == "audio":
            has_audio = True
    return ClipProbe(has_video=has_video, has_audio=has_audio)


def run_extract_last_frame(src: Path, out_jpg: Path) -> None:
    tmp_jpg = out_jpg.with_name(f"{out_jpg.stem}.tmp{out_jpg.suffix or '.jpg'}")
    cmd = ffmpeg_base() + [
        "-y",
        "-sseof",
        "-0.2",
        "-i",
        str(src),
        "-frames:v",
        "1",
        str(tmp_jpg),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp_jpg.replace(out_jpg)


def start_publisher(*, audio_url: str, video_url: str, rtsp_url: str, transport: str, video_fps: int, h264_preset: str, h264_crf: str) -> subprocess.Popen:
    gop = max(15, int(video_fps * 2))
    cmd = ffmpeg_base() + [
        "-thread_queue_size",
        "2048",
        "-f",
        "s16le",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-i",
        audio_url,
        "-fflags",
        "+genpts+discardcorrupt",
        "-thread_queue_size",
        "512",
        "-f",
        "mjpeg",
        "-framerate",
        str(video_fps),
        "-i",
        video_url,
        "-map",
        "1:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        h264_preset,
        "-tune",
        "zerolatency",
        "-crf",
        h264_crf,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(video_fps),
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-bf",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-f",
        "rtsp",
        "-rtsp_transport",
        transport,
        rtsp_url,
    ]
    return subprocess.Popen(cmd)


def _pump_stream_to_socket(src, target: StreamSocketServer, stop_event: threading.Event, label: str) -> None:
    try:
        while not stop_event.is_set():
            chunk = src.read(64 * 1024)
            if not chunk:
                return
            target.write(chunk, stop_event)
    except BrokenPipeError:
        return
    except Exception as exc:
        print(f"[rtsp-publisher] pump error {label}: {exc}")
    finally:
        try:
            src.close()
        except Exception:
            pass


def _start_pumped_ffmpeg(cmd: list[str], *, target: StreamSocketServer, label: str) -> PumpedProc:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    assert proc.stdout is not None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_pump_stream_to_socket,
        args=(proc.stdout, target, stop_event, label),
        name=f"pump-{label}",
        daemon=True,
    )
    thread.start()
    return PumpedProc(proc=proc, stop_event=stop_event, threads=[thread])


def start_clip_audio_feeder(*, src: Path, audio_server: StreamSocketServer) -> PumpedProc:
    cmd = ffmpeg_base() + [
        "-y",
        "-re",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-f",
        "s16le",
        "pipe:1",
    ]
    return _start_pumped_ffmpeg(cmd, target=audio_server, label=f"audio-{src.name}")


def start_clip_video_feeder(*, src: Path, video_server: StreamSocketServer, video_fps: int, mjpeg_q: str) -> PumpedProc:
    cmd = ffmpeg_base() + [
        "-y",
        "-re",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-vf",
        f"fps={video_fps},scale=in_range=full:out_range=tv",
        "-q:v",
        mjpeg_q,
        "-f",
        "mjpeg",
        "pipe:1",
    ]
    return _start_pumped_ffmpeg(cmd, target=video_server, label=f"video-{src.name}")


def stop_pumped_proc(state: PumpedProc | None) -> None:
    if state is None:
        return
    state.stop_event.set()
    stop_proc(state.proc)
    for thread in state.threads:
        if thread.is_alive():
            thread.join(timeout=2.0)
    if state.proc.stdout is not None:
        try:
            state.proc.stdout.close()
        except Exception:
            pass


def _sleep_until(deadline: float, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))


def stream_still_frames(still_jpg: Path, video_server: StreamSocketServer, video_fps: int, stop_event: threading.Event) -> None:
    try:
        frame_bytes = still_jpg.read_bytes()
        if not frame_bytes:
            raise ValueError(f"empty still frame: {still_jpg}")
        frame_interval = 1.0 / max(1, video_fps)
        next_deadline = time.monotonic()
        while not stop_event.is_set():
            video_server.write(frame_bytes, stop_event)
            next_deadline += frame_interval
            _sleep_until(next_deadline, stop_event)
    except BrokenPipeError:
        return
    except Exception as exc:
        print(f"[rtsp-publisher] still-writer error path={still_jpg}: {exc}")


def stream_silence(audio_server: StreamSocketServer, stop_event: threading.Event, *, chunk_ms: int = 100) -> None:
    chunk_ms = max(10, chunk_ms)
    bytes_per_second = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * PCM_BYTES_PER_SAMPLE
    chunk_size = max(1, (bytes_per_second * chunk_ms) // 1000)
    silence = b"\x00" * chunk_size
    interval = chunk_ms / 1000.0
    try:
        next_deadline = time.monotonic()
        while not stop_event.is_set():
            audio_server.write(silence, stop_event)
            next_deadline += interval
            _sleep_until(next_deadline, stop_event)
    except BrokenPipeError:
        return
    except Exception as exc:
        print(f"[rtsp-publisher] silence-writer error server={audio_server.label}: {exc}")


def start_threads(*, thread_specs: list[tuple[str, Callable, tuple]]) -> tuple[threading.Event, list[threading.Thread]]:
    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    for name, fn, args in thread_specs:
        thread = threading.Thread(target=fn, name=name, args=(*args, stop_event), daemon=True)
        thread.start()
        threads.append(thread)
    return stop_event, threads


def stop_threads(stop_event: threading.Event | None, threads: list[threading.Thread]) -> None:
    if stop_event is not None:
        stop_event.set()
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=2.0)


def start_still_fillers(stream: CameraStream, video_fps: int) -> tuple[threading.Event, list[threading.Thread]]:
    if stream.last_still is None:
        raise ValueError("last still frame is not available")
    return start_threads(
        thread_specs=[
            (f"rtsp-silence-{stream.camera}", stream_silence, (stream.audio_server,)),
            (f"rtsp-still-{stream.camera}", stream_still_frames, (stream.last_still, stream.video_server, video_fps)),
        ]
    )


def start_silence_only(stream: CameraStream) -> tuple[threading.Event, list[threading.Thread]]:
    return start_threads(
        thread_specs=[(f"rtsp-clip-silence-{stream.camera}", stream_silence, (stream.audio_server,))]
    )


def prepare_clip(stream: CameraStream, desired: Path) -> tuple[ClipProbe, Path]:
    if not wait_for_file_ready(desired):
        raise FileNotFoundError(desired)

    probe = probe_clip(desired)
    if not probe.has_video:
        raise RuntimeError(f"clip has no video stream: {desired}")

    still_path = stream.work_dir / "last.jpg"
    run_extract_last_frame(desired, still_path)
    return probe, still_path


def camera_worker(stream: CameraStream, video_fps: int) -> None:
    stop_event = stream.stop_event
    assert stop_event is not None

    clip_video: PumpedProc | None = None
    clip_audio: PumpedProc | None = None
    clip_aux_stop: threading.Event | None = None
    clip_aux_threads: list[threading.Thread] = []
    clip_expected_audio = False
    filler_stop: threading.Event | None = None
    filler_threads: list[threading.Thread] = []
    last_idle_log: float = 0.0

    def stop_clip_state() -> None:
        nonlocal clip_video, clip_audio, clip_aux_stop, clip_aux_threads, clip_expected_audio
        stop_pumped_proc(clip_video)
        stop_pumped_proc(clip_audio)
        clip_video = None
        clip_audio = None
        stop_threads(clip_aux_stop, clip_aux_threads)
        clip_aux_stop = None
        clip_aux_threads = []
        clip_expected_audio = False

    def stop_filler_state() -> None:
        nonlocal filler_stop, filler_threads
        stop_threads(filler_stop, filler_threads)
        filler_stop = None
        filler_threads = []

    try:
        while not stop_event.is_set():
            desired = stream.desired_src

            if desired is not None and stream.current_src != desired:
                try:
                    probe, still_path = prepare_clip(stream, desired)
                except FileNotFoundError:
                    time.sleep(0.5)
                    continue
                except Exception as exc:
                    stream.last_error = f"prepare clip failed: {exc}"
                    print(f"[rtsp-publisher] cam={stream.camera} {stream.last_error}")
                    time.sleep(1.0)
                    continue

                stop_clip_state()
                stop_filler_state()
                stream.last_still = still_path
                stream.current_src = desired
                stream.last_error = None
                clip_expected_audio = probe.has_audio
                print(f"[rtsp-publisher] cam={stream.camera} switching src={desired.name}")
                if probe.has_audio:
                    print(f"[rtsp-publisher] cam={stream.camera} mode=clip-audio")
                    clip_audio = start_clip_audio_feeder(src=desired, audio_server=stream.audio_server)
                    time.sleep(0.2)
                else:
                    print(f"[rtsp-publisher] cam={stream.camera} mode=clip-no-audio -> silence-fill")
                    clip_aux_stop, clip_aux_threads = start_silence_only(stream)
                    time.sleep(0.1)
                clip_video = start_clip_video_feeder(src=desired, video_server=stream.video_server, video_fps=video_fps, mjpeg_q=mjpeg_q)
                continue

            if clip_video is not None:
                if clip_expected_audio and clip_audio is not None:
                    audio_rc = clip_audio.proc.poll()
                    if audio_rc is not None:
                        if audio_rc != 0:
                            print(f"[rtsp-publisher] cam={stream.camera} clip audio feeder exited code={audio_rc}")
                        stop_pumped_proc(clip_audio)
                        clip_audio = None
                        if not clip_aux_threads and not stop_event.is_set():
                            clip_aux_stop, clip_aux_threads = start_silence_only(stream)

                video_rc = clip_video.proc.poll()
                if video_rc is None:
                    time.sleep(0.25)
                    continue

                stop_clip_state()
                if video_rc != 0:
                    print(f"[rtsp-publisher] cam={stream.camera} clip video feeder exited code={video_rc}")
                if stop_event.is_set():
                    break
                if stream.desired_src != stream.current_src:
                    continue
                if stream.last_still is not None and stream.last_still.exists():
                    print(f"[rtsp-publisher] cam={stream.camera} mode=still-silence-hold")
                    filler_stop, filler_threads = start_still_fillers(stream, video_fps)
                    continue
                time.sleep(0.5)
                continue

            if filler_threads:
                if stream.desired_src != stream.current_src:
                    stop_filler_state()
                    continue
                if any(not t.is_alive() for t in filler_threads):
                    stop_filler_state()
                    if stream.last_still is not None and stream.last_still.exists() and not stop_event.is_set():
                        filler_stop, filler_threads = start_still_fillers(stream, video_fps)
                    continue
                time.sleep(0.5)
                continue

            if stream.current_src is not None and stream.last_still is not None and stream.last_still.exists():
                filler_stop, filler_threads = start_still_fillers(stream, video_fps)
                continue

            now = time.time()
            if now - last_idle_log >= 30.0:
                print(f"[rtsp-publisher] cam={stream.camera} idle waiting for first clip")
                last_idle_log = now
            time.sleep(0.5)
    finally:
        stop_clip_state()
        stop_filler_state()


def teardown_stream(stream: CameraStream) -> None:
    if stream.stop_event is not None:
        stream.stop_event.set()
    stop_proc(stream.publisher)
    stream.audio_server.close()
    stream.video_server.close()
    if stream.thread is not None and stream.thread.is_alive():
        stream.thread.join(timeout=2.0)


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

    try:
        video_fps = int((os.getenv("RTSP_VIDEO_FPS", "15") or "15").strip())
    except ValueError:
        video_fps = 15
    video_fps = max(1, video_fps)

    print(f"[rtsp-publisher] watch_dir={watch_dir} glob={glob_pattern} poll_sec={poll_sec}")
    print(f"[rtsp-publisher] mediamtx=rtsp://{mediamtx_host}:{mediamtx_port} transport={transport}")
    h264_preset = (os.getenv("RTSP_H264_PRESET", "veryfast") or "veryfast").strip()
    h264_crf = (os.getenv("RTSP_H264_CRF", "23") or "23").strip()
    mjpeg_q = (os.getenv("RTSP_MJPEG_Q", "2") or "2").strip()

    print(f"[rtsp-publisher] camera_regex={cam_re.pattern}")
    print(f"[rtsp-publisher] video_fps={video_fps} h264_preset={h264_preset} h264_crf={h264_crf} mjpeg_q={mjpeg_q}")
    if hold_int <= 0:
        print("[rtsp-publisher] still hold=continuous until newer clip arrives")
    else:
        print(f"[rtsp-publisher] still hold compatibility note: RTSP_STILL_HOLD_SEC={hold_int} is ignored; stills now hold until replaced")

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
            newest_key_by_cam: dict[str, float] = {}

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
                key = _parse_name_timestamp(f)
                prev_key = newest_key_by_cam.get(cam, float("-inf"))
                if key > prev_key:
                    newest_key_by_cam[cam] = key
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
                    audio_server = StreamSocketServer(f"{stream_name}-audio")
                    video_server = StreamSocketServer(f"{stream_name}-video")
                    publisher = start_publisher(
                        audio_url=audio_server.url,
                        video_url=video_server.url,
                        rtsp_url=rtsp_url,
                        transport=transport,
                        video_fps=video_fps,
                        h264_preset=h264_preset,
                        h264_crf=h264_crf,
                    )
                    stop_event = threading.Event()
                    stream = CameraStream(
                        camera=cam,
                        stream_name=path,
                        rtsp_url=rtsp_url,
                        audio_server=audio_server,
                        video_server=video_server,
                        work_dir=cam_dir,
                        publisher=publisher,
                        stop_event=stop_event,
                    )
                    stream.desired_src = newest
                    thread = threading.Thread(target=camera_worker, args=(stream, video_fps), daemon=True)
                    stream.thread = thread
                    thread.start()
                    streams[cam] = stream
                    print(f"[rtsp-publisher] starting cam={cam} src={newest.name}")
                    print(f"[rtsp-publisher] cam={cam} url={rtsp_url}")
                else:
                    if not newest.exists():
                        continue
                    current_key = _parse_name_timestamp(existing.desired_src)
                    new_key = _parse_name_timestamp(newest)
                    if new_key >= current_key and existing.desired_src != newest:
                        existing.desired_src = newest
                        print(f"[rtsp-publisher] queued cam={cam} src={newest.name}")

            for cam in list(streams.keys()):
                stream = streams[cam]
                if stream.publisher.poll() is not None:
                    print(f"[rtsp-publisher] cam={cam} publisher exited code={stream.publisher.returncode}")
                    teardown_stream(stream)
                    streams.pop(cam, None)

            time.sleep(max(1.0, poll_sec))

    except KeyboardInterrupt:
        print("[rtsp-publisher] shutting down")
    finally:
        for stream in list(streams.values()):
            teardown_stream(stream)
        shutil.rmtree(work_root, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
