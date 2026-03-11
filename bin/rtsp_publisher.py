#!/usr/bin/env python3
"""Publish latest downloaded Blink MP4 clips to MediaMTX as RTSP streams.

Design goals
- Keep one long-lived ffmpeg RTSP publisher process per camera.
- Avoid reconnecting MediaMTX or downstream RTSP readers when content changes.
- Play each discovered clip once, in order, then hold on the last frame plus silence.
- Avoid MPEG-TS producer handoff corruption while also removing the extra lossy MJPEG hop.

How it works
- Watches a directory for MP4 files that match a pattern (default: *.mp4).
- Groups files by camera name using RTSP_CAMERA_REGEX.
- Each camera owns an explicit in-memory playback state machine:
  IDLE -> PREPARING -> PLAYING -> HOLDING (or ERROR), with a monotonic discovery watermark.
- Existing files at startup seed each camera with only the newest clip.
- Newly discovered clips with a strictly newer sort key are queued once and played once.
- For each camera, one persistent ffmpeg publisher ingests continuous local TCP inputs:
  * audio: mono 48 kHz s16le PCM
  * video: rawvideo yuv420p at RTSP_VIDEO_FPS
- Short-lived ffmpeg decoders feed clip audio/video into those local sockets.
- After the clip ends, Python keeps sending the last raw video frame and zero-valued PCM silence
  until a newer queued clip arrives.

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
- RTSP_H264_PRESET / RTSP_H264_CRF: persistent publisher encode settings
- RTSP_MJPEG_Q: legacy compatibility knob; ignored now that the transport is rawvideo instead of MJPEG
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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

ClipSortKey = tuple[float, str]


class CameraLifecycleState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    PLAYING = "playing"
    HOLDING = "holding"
    ERROR = "error"


@dataclass(frozen=True, order=True)
class ClipRef:
    """A discovered MP4 plus its monotonic ordering key.

    ``sort_key`` is intentionally separated from filesystem mtime so queue ordering is
    stable even if files are copied, restored, or touched after download.
    """

    sort_key: ClipSortKey
    path: Path = field(compare=False)

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class VideoGeometry:
    """The fixed canvas size used by the persistent publisher for one camera."""

    width: int
    height: int

    @property
    def size_arg(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def raw_frame_size(self) -> int:
        # yuv420p => 1.5 bytes per pixel
        return (self.width * self.height * 3) // 2


@dataclass
class ClipProbe:
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None


@dataclass
class PreparedClip:
    clip: ClipRef
    probe: ClipProbe
    geometry: VideoGeometry
    still_frame: Path


@dataclass
class PumpedProc:
    proc: subprocess.Popen
    stop_event: threading.Event
    threads: list[threading.Thread]


@dataclass
class CameraPlaybackState:
    """Minimal state machine for a single camera stream.

    The important invariant is that ``discovered_highwater`` only moves forward.
    That allows the scanner to re-list the whole watch directory every poll without
    replaying older clips after prune/copy churn.
    """

    lifecycle: CameraLifecycleState = CameraLifecycleState.IDLE
    pending_clips: deque[ClipRef] = field(default_factory=deque)
    discovered_highwater: ClipSortKey | None = None
    preparing_clip: ClipRef | None = None
    active_clip: ClipRef | None = None
    held_clip: ClipRef | None = None
    last_completed_clip: ClipRef | None = None

    def seed_from_existing(self, clips: list[ClipRef]) -> ClipRef | None:
        if not clips:
            return None
        newest = clips[-1]
        self.pending_clips.clear()
        self.pending_clips.append(newest)
        self.discovered_highwater = newest.sort_key
        self.preparing_clip = None
        self.active_clip = None
        self.held_clip = None
        self.last_completed_clip = None
        self.lifecycle = CameraLifecycleState.IDLE
        return newest

    def discover_new_clips(self, clips: list[ClipRef]) -> list[ClipRef]:
        appended: list[ClipRef] = []
        highwater = self.discovered_highwater
        for clip in clips:
            if highwater is None or clip.sort_key > highwater:
                self.pending_clips.append(clip)
                appended.append(clip)
                highwater = clip.sort_key
        self.discovered_highwater = highwater
        return appended

    def begin_prepare(self) -> ClipRef | None:
        if not self.pending_clips:
            return None
        clip = self.pending_clips.popleft()
        self.preparing_clip = clip
        return clip

    def mark_playing(self, clip: ClipRef) -> None:
        self.preparing_clip = None
        self.active_clip = clip
        self.held_clip = None

    def mark_holding(self, clip: ClipRef) -> None:
        self.preparing_clip = None
        self.active_clip = None
        self.held_clip = clip
        self.last_completed_clip = clip

    def mark_idle(self) -> None:
        self.preparing_clip = None
        self.active_clip = None

    def mark_error(self) -> None:
        self.preparing_clip = None
        self.active_clip = None


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
    stop_event: threading.Event
    publisher: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    video_geometry: VideoGeometry | None = None
    last_still_frame: Path | None = None
    last_error: str | None = None
    playback: CameraPlaybackState = field(default_factory=CameraPlaybackState)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def slugify(name: str) -> str:
    out = name.strip().lower()
    out = re.sub(r"[^a-z0-9]+", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    return out or "camera"


def clip_sort_key(path: Path) -> ClipSortKey:
    return (_parse_name_timestamp(path), path.name)


def make_clip_ref(path: Path) -> ClipRef:
    return ClipRef(sort_key=clip_sort_key(path), path=path)


def clip_label(clip: ClipRef | None) -> str:
    return clip.name if clip is not None else "-"


def snapshot_playback(stream: CameraStream) -> tuple[CameraLifecycleState, int, str, str, str]:
    playback = stream.playback
    return (
        playback.lifecycle,
        len(playback.pending_clips),
        clip_label(playback.preparing_clip),
        clip_label(playback.active_clip),
        clip_label(playback.held_clip),
    )


def transition_state(stream: CameraStream, new_state: CameraLifecycleState, *, reason: str, note: str | None = None) -> None:
    with stream.lock:
        prev_state, pending_count, preparing_name, active_name, held_name = snapshot_playback(stream)
        stream.playback.lifecycle = new_state
        pending_count = len(stream.playback.pending_clips)
        preparing_name = clip_label(stream.playback.preparing_clip)
        active_name = clip_label(stream.playback.active_clip)
        held_name = clip_label(stream.playback.held_clip)
        error = stream.last_error
    parts = [
        f"cam={stream.camera}",
        f"state={new_state.value}",
        f"from={prev_state.value}",
        f"reason={reason}",
        f"pending={pending_count}",
        f"preparing={preparing_name}",
        f"active={active_name}",
        f"held={held_name}",
    ]
    if note:
        parts.append(f"note={note}")
    if error:
        parts.append(f"error={error}")
    print("[rtsp-publisher] " + " ".join(parts))


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


def build_silence_chunk(*, chunk_ms: int = 100) -> bytes:
    chunk_ms = max(10, chunk_ms)
    bytes_per_second = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * PCM_BYTES_PER_SAMPLE
    chunk_size = max(1, (bytes_per_second * chunk_ms) // 1000)
    return b"\x00" * chunk_size


def identify_camera_name(path: Path, cam_re: re.Pattern[str]) -> str | None:
    m = cam_re.match(path.name)
    if m:
        return m.group("camera")
    if re.match(r"^blink_\d{4}-\d{2}-\d{2}[Tt]\d{2}-\d{2}-\d{2}", path.name, re.IGNORECASE):
        return "blink"
    if re.match(r"^download-\d{4}-\d{2}-\d{2}[Tt]\d{2}-\d{2}-\d{2}", path.name, re.IGNORECASE):
        return "download"
    return None


def collect_clips_by_camera(files: list[Path], cam_re: re.Pattern[str]) -> tuple[dict[str, list[ClipRef]], list[str]]:
    clips_by_cam: dict[str, list[ClipRef]] = {}
    skipped: list[str] = []
    for path in files:
        cam = identify_camera_name(path, cam_re)
        if cam is None:
            skipped.append(path.name)
            continue
        clips_by_cam.setdefault(cam, []).append(make_clip_ref(path))
    for clips in clips_by_cam.values():
        clips.sort()
    return clips_by_cam, skipped


def probe_clip(src: Path) -> ClipProbe:
    """Read just enough stream metadata to decide how to feed the publisher."""

    cmd = [
        ffprobe_bin(),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height",
        "-of",
        "json",
        str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(proc.stdout or "{}")
    has_video = False
    has_audio = False
    width: int | None = None
    height: int | None = None
    for stream in payload.get("streams") or []:
        codec_type = (stream or {}).get("codec_type")
        if codec_type == "video" and not has_video:
            has_video = True
            width = (stream or {}).get("width")
            height = (stream or {}).get("height")
        elif codec_type == "audio":
            has_audio = True
    return ClipProbe(has_video=has_video, has_audio=has_audio, width=width, height=height)


def video_filter(*, geometry: VideoGeometry, video_fps: int | None = None) -> str:
    """Build a deterministic ffmpeg filter graph for clip and still normalization.

    Every clip is scaled into the camera's fixed canvas so the long-lived publisher can
    stay connected while sources change size or aspect ratio.
    """

    filters: list[str] = []
    if video_fps is not None:
        filters.append(f"fps={video_fps}")
    filters.extend(
        [
            f"scale=w={geometry.width}:h={geometry.height}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={geometry.width}:{geometry.height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "format=yuv420p",
        ]
    )
    return ",".join(filters)


def run_extract_last_frame_raw(src: Path, out_frame: Path, *, geometry: VideoGeometry) -> None:
    """Extract one normalized raw frame near clip end for HOLDING mode.

    The output is raw ``yuv420p`` rather than PNG/JPEG because the filler thread writes
    the bytes directly into the publisher's rawvideo socket at a fixed cadence.
    """

    tmp_frame = out_frame.with_name(f"{out_frame.stem}.tmp{out_frame.suffix or '.yuv'}")
    cmd = ffmpeg_base() + [
        "-y",
        "-sseof",
        "-0.2",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        video_filter(geometry=geometry),
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        str(tmp_frame),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp_frame.replace(out_frame)


def start_publisher(
    *,
    audio_url: str,
    video_url: str,
    rtsp_url: str,
    transport: str,
    video_fps: int,
    geometry: VideoGeometry,
    h264_preset: str,
    h264_crf: str,
) -> subprocess.Popen:
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
        "-thread_queue_size",
        "512",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-video_size",
        geometry.size_arg,
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


def _read_stream_chunk(src, *, chunk_size: int, require_full_chunk: bool) -> tuple[bytes, bool]:
    if chunk_size <= 0:
        chunk_size = 64 * 1024
    if not require_full_chunk:
        return src.read(chunk_size), True

    buf = bytearray()
    while len(buf) < chunk_size:
        part = src.read(chunk_size - len(buf))
        if not part:
            break
        buf.extend(part)
    if not buf:
        return b"", True
    return bytes(buf), len(buf) == chunk_size


def _pump_stream_to_socket(
    src,
    target: StreamSocketServer,
    stop_event: threading.Event,
    label: str,
    *,
    chunk_size: int = 64 * 1024,
    require_full_chunk: bool = False,
) -> None:
    try:
        while not stop_event.is_set():
            chunk, complete = _read_stream_chunk(src, chunk_size=chunk_size, require_full_chunk=require_full_chunk)
            if not chunk:
                return
            if require_full_chunk and not complete:
                print(
                    f"[rtsp-publisher] pump-short-read {label}: dropped trailing partial chunk bytes={len(chunk)} expected={chunk_size}"
                )
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


def _start_pumped_ffmpeg(
    cmd: list[str],
    *,
    target: StreamSocketServer,
    label: str,
    chunk_size: int = 64 * 1024,
    require_full_chunk: bool = False,
) -> PumpedProc:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    assert proc.stdout is not None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_pump_stream_to_socket,
        args=(proc.stdout, target, stop_event, label),
        kwargs={"chunk_size": chunk_size, "require_full_chunk": require_full_chunk},
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


def start_clip_video_feeder(*, src: Path, video_server: StreamSocketServer, video_fps: int, geometry: VideoGeometry) -> PumpedProc:
    cmd = ffmpeg_base() + [
        "-y",
        "-re",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-vf",
        video_filter(geometry=geometry, video_fps=video_fps),
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    return _start_pumped_ffmpeg(
        cmd,
        target=video_server,
        label=f"video-{src.name}",
        chunk_size=geometry.raw_frame_size,
        require_full_chunk=True,
    )


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


def stream_still_frames(still_frame: Path, *, geometry: VideoGeometry, video_server: StreamSocketServer, video_fps: int, stop_event: threading.Event) -> None:
    """Push the last extracted frame repeatedly so RTSP readers stay attached.

    This is the core of HOLDING mode: the persistent publisher continues receiving valid
    raw frames at the expected FPS even when no active clip is playing.
    """

    try:
        frame_bytes = still_frame.read_bytes()
        if len(frame_bytes) != geometry.raw_frame_size:
            raise ValueError(
                f"unexpected still size {len(frame_bytes)} != {geometry.raw_frame_size} for {still_frame}"
            )
        frame_interval = 1.0 / max(1, video_fps)
        next_deadline = time.monotonic()
        while not stop_event.is_set():
            video_server.write(frame_bytes, stop_event)
            next_deadline += frame_interval
            _sleep_until(next_deadline, stop_event)
    except BrokenPipeError:
        return
    except Exception as exc:
        print(f"[rtsp-publisher] still-writer error path={still_frame}: {exc}")


def stream_silence(audio_server: StreamSocketServer, stop_event: threading.Event, *, chunk_ms: int = 100) -> None:
    silence = build_silence_chunk(chunk_ms=chunk_ms)
    interval = max(10, chunk_ms) / 1000.0
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


def start_silence_only(stream: CameraStream) -> tuple[threading.Event, list[threading.Thread]]:
    return start_threads(thread_specs=[(f"rtsp-clip-silence-{stream.camera}", stream_silence, (stream.audio_server,))])


def prepare_clip(stream: CameraStream, clip: ClipRef) -> PreparedClip:
    """Validate and normalize a queued clip before PLAYING begins.

    Preparation is intentionally front-loaded: probe geometry, verify a video stream is
    present, and precompute the hold-frame artifact. That keeps the runtime state machine
    simple once playback transitions from PREPARING to PLAYING.
    """

    if not wait_for_file_ready(clip.path):
        raise FileNotFoundError(clip.path)

    probe = probe_clip(clip.path)
    if not probe.has_video:
        raise RuntimeError(f"clip has no video stream: {clip.path}")
    if not probe.width or not probe.height:
        raise RuntimeError(f"clip video dimensions unavailable: {clip.path}")

    with stream.lock:
        geometry = stream.video_geometry or VideoGeometry(width=int(probe.width), height=int(probe.height))
    still_path = stream.work_dir / "last-frame.yuv"
    run_extract_last_frame_raw(clip.path, still_path, geometry=geometry)
    return PreparedClip(clip=clip, probe=probe, geometry=geometry, still_frame=still_path)


def ensure_publisher(
    stream: CameraStream,
    *,
    transport: str,
    video_fps: int,
    geometry: VideoGeometry,
    h264_preset: str,
    h264_crf: str,
) -> None:
    """Start the long-lived ffmpeg publisher once per camera if needed.

    Clip feeders can come and go, but this publisher should remain stable so MediaMTX and
    downstream readers do not see disconnect/reconnect churn on every clip boundary.
    """

    with stream.lock:
        if stream.publisher is not None and stream.publisher.poll() is None:
            if stream.video_geometry is None:
                stream.video_geometry = geometry
            return

    publisher = start_publisher(
        audio_url=stream.audio_server.url,
        video_url=stream.video_server.url,
        rtsp_url=stream.rtsp_url,
        transport=transport,
        video_fps=video_fps,
        geometry=geometry,
        h264_preset=h264_preset,
        h264_crf=h264_crf,
    )
    with stream.lock:
        stream.publisher = publisher
        stream.video_geometry = geometry
    print(
        f"[rtsp-publisher] cam={stream.camera} publisher-start transport=rawvideo-yuv420p size={geometry.size_arg} url={stream.rtsp_url}"
    )
    # Rawvideo inputs can take a moment longer to initialize than PCM. Wait a bit longer and
    # re-check the publisher process before declaring startup failure.
    if not stream.audio_server.wait_for_connection(timeout_sec=10.0):
        stop_proc(publisher)
        raise RuntimeError("publisher failed to connect audio socket")
    if not stream.video_server.wait_for_connection(timeout_sec=15.0):
        if publisher.poll() is not None:
            raise RuntimeError(f"publisher exited during video socket connect (code={publisher.returncode})")
        stop_proc(publisher)
        raise RuntimeError("publisher failed to connect video socket")


# start_threads accepts positional args only, so wrap keyword-heavy helpers.
def _stream_still_frames_thread(still_frame: Path, stream: CameraStream, video_fps: int, stop_event: threading.Event) -> None:
    if stream.video_geometry is None:
        raise ValueError("video geometry is not available")
    stream_still_frames(
        still_frame,
        geometry=stream.video_geometry,
        video_server=stream.video_server,
        video_fps=video_fps,
        stop_event=stop_event,
    )


def start_still_fillers(stream: CameraStream, video_fps: int) -> tuple[threading.Event, list[threading.Thread]]:
    if stream.last_still_frame is None:
        raise ValueError("last still frame is not available")
    if stream.video_geometry is None:
        raise ValueError("video geometry is not available")
    return start_threads(
        thread_specs=[
            (f"rtsp-silence-{stream.camera}", stream_silence, (stream.audio_server,)),
            (f"rtsp-still-{stream.camera}", _stream_still_frames_thread, (stream.last_still_frame, stream, video_fps)),
        ]
    )


def activate_prepared_clip(
    stream: CameraStream,
    prepared: PreparedClip,
    *,
    transport: str,
    video_fps: int,
    h264_preset: str,
    h264_crf: str,
) -> tuple[bool, PumpedProc | None, PumpedProc, threading.Event | None, list[threading.Thread]]:
    clip_video = start_clip_video_feeder(
        src=prepared.clip.path,
        video_server=stream.video_server,
        video_fps=video_fps,
        geometry=prepared.geometry,
    )
    clip_audio: PumpedProc | None = None
    clip_aux_stop: threading.Event | None = None
    clip_aux_threads: list[threading.Thread] = []
    try:
        clip_expected_audio = prepared.probe.has_audio
        if clip_expected_audio:
            clip_audio = start_clip_audio_feeder(src=prepared.clip.path, audio_server=stream.audio_server)
            time.sleep(0.2)
        else:
            clip_aux_stop, clip_aux_threads = start_silence_only(stream)
            print(f"[rtsp-publisher] cam={stream.camera} clip={prepared.clip.name} audio-fallback=zero-pcm-silence")
            time.sleep(0.1)

        ensure_publisher(
            stream,
            transport=transport,
            video_fps=video_fps,
            geometry=prepared.geometry,
            h264_preset=h264_preset,
            h264_crf=h264_crf,
        )
        return clip_expected_audio, clip_audio, clip_video, clip_aux_stop, clip_aux_threads
    except Exception:
        stop_pumped_proc(clip_video)
        stop_pumped_proc(clip_audio)
        stop_threads(clip_aux_stop, clip_aux_threads)
        raise


def camera_worker(stream: CameraStream, *, transport: str, video_fps: int, h264_preset: str, h264_crf: str) -> None:
    stop_event = stream.stop_event

    clip_video: PumpedProc | None = None
    clip_audio: PumpedProc | None = None
    clip_aux_stop: threading.Event | None = None
    clip_aux_threads: list[threading.Thread] = []
    clip_expected_audio = False
    filler_stop: threading.Event | None = None
    filler_threads: list[threading.Thread] = []
    current_clip: ClipRef | None = None
    preparing_clip: ClipRef | None = None
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
            if preparing_clip is None and clip_video is None:
                with stream.lock:
                    preparing_clip = stream.playback.begin_prepare()
                if preparing_clip is not None:
                    transition_state(stream, CameraLifecycleState.PREPARING, reason="clip-queued", note=f"clip={preparing_clip.name}")

            if preparing_clip is not None and clip_video is None:
                try:
                    prepared = prepare_clip(stream, preparing_clip)
                    stop_clip_state()
                    stop_filler_state()
                    clip_expected_audio, clip_audio, clip_video, clip_aux_stop, clip_aux_threads = activate_prepared_clip(
                        stream,
                        prepared,
                        transport=transport,
                        video_fps=video_fps,
                        h264_preset=h264_preset,
                        h264_crf=h264_crf,
                    )
                except FileNotFoundError:
                    if stop_event.is_set():
                        break
                    if not preparing_clip.path.exists():
                        with stream.lock:
                            stream.last_error = f"clip vanished before ready: {preparing_clip.name}"
                            stream.playback.mark_error()
                        transition_state(stream, CameraLifecycleState.ERROR, reason="prepare-vanished")
                        preparing_clip = None
                        time.sleep(0.5)
                        continue
                    time.sleep(0.5)
                    continue
                except Exception as exc:
                    with stream.lock:
                        stream.last_error = f"prepare clip failed: {exc}"
                        stream.playback.mark_error()
                    transition_state(stream, CameraLifecycleState.ERROR, reason="prepare-failed")
                    preparing_clip = None
                    time.sleep(1.0)
                    continue

                with stream.lock:
                    stream.last_still_frame = prepared.still_frame
                    stream.video_geometry = prepared.geometry
                    stream.last_error = None
                    stream.playback.mark_playing(preparing_clip)
                source_size = f"{prepared.probe.width}x{prepared.probe.height}"
                scale_note = ""
                if source_size != prepared.geometry.size_arg:
                    scale_note = f" source={source_size}->target={prepared.geometry.size_arg}"
                transition_state(
                    stream,
                    CameraLifecycleState.PLAYING,
                    reason="clip-start",
                    note=(
                        f"clip={preparing_clip.name} audio={'source' if prepared.probe.has_audio else 'zero-pcm-silence'} "
                        f"video=rawvideo-yuv420p target={prepared.geometry.size_arg}{scale_note}"
                    ),
                )
                current_clip = preparing_clip
                preparing_clip = None
                continue

            if clip_video is not None:
                if clip_expected_audio and clip_audio is not None:
                    audio_rc = clip_audio.proc.poll()
                    if audio_rc is not None:
                        if audio_rc != 0:
                            print(
                                f"[rtsp-publisher] cam={stream.camera} clip={clip_label(current_clip)} clip-audio-feeder exited code={audio_rc}"
                            )
                        stop_pumped_proc(clip_audio)
                        clip_audio = None
                        if not clip_aux_threads and not stop_event.is_set():
                            clip_aux_stop, clip_aux_threads = start_silence_only(stream)
                            print(
                                f"[rtsp-publisher] cam={stream.camera} clip={clip_label(current_clip)} audio-transition=zero-pcm-silence"
                            )

                video_rc = clip_video.proc.poll()
                if video_rc is None:
                    time.sleep(0.25)
                    continue

                stop_clip_state()
                if video_rc != 0:
                    print(
                        f"[rtsp-publisher] cam={stream.camera} clip={clip_label(current_clip)} clip-video-feeder exited code={video_rc}"
                    )
                if stop_event.is_set():
                    break

                pending_after = 0
                with stream.lock:
                    if current_clip is not None:
                        stream.playback.last_completed_clip = current_clip
                    pending_after = len(stream.playback.pending_clips)

                if current_clip is not None and stream.last_still_frame is not None and stream.last_still_frame.exists() and pending_after == 0:
                    filler_stop, filler_threads = start_still_fillers(stream, video_fps)
                    with stream.lock:
                        stream.playback.mark_holding(current_clip)
                    transition_state(
                        stream,
                        CameraLifecycleState.HOLDING,
                        reason="clip-complete",
                        note=f"clip={current_clip.name} video=still-raw audio=zero-pcm-silence",
                    )
                else:
                    with stream.lock:
                        if current_clip is not None:
                            stream.playback.last_completed_clip = current_clip
                        stream.playback.mark_idle()
                        stream.playback.held_clip = current_clip if pending_after == 0 else None
                    transition_state(
                        stream,
                        CameraLifecycleState.IDLE,
                        reason="clip-complete-next-queued" if pending_after > 0 else "clip-complete-no-still",
                        note=f"clip={clip_label(current_clip)}",
                    )
                current_clip = None
                continue

            if filler_threads:
                pending_after = 0
                with stream.lock:
                    pending_after = len(stream.playback.pending_clips)
                if pending_after > 0:
                    stop_filler_state()
                    with stream.lock:
                        stream.playback.mark_idle()
                    transition_state(stream, CameraLifecycleState.IDLE, reason="newer-clip-arrived")
                    continue
                if any(not t.is_alive() for t in filler_threads):
                    stop_filler_state()
                    if stream.last_still_frame is not None and stream.last_still_frame.exists() and not stop_event.is_set():
                        filler_stop, filler_threads = start_still_fillers(stream, video_fps)
                        transition_state(stream, CameraLifecycleState.HOLDING, reason="hold-restored", note="video=still-raw audio=zero-pcm-silence")
                    continue
                time.sleep(0.5)
                continue

            now = time.time()
            if now - last_idle_log >= 30.0:
                transition_state(stream, CameraLifecycleState.IDLE, reason="waiting-for-clip")
                last_idle_log = now
            time.sleep(0.5)
    finally:
        stop_clip_state()
        stop_filler_state()


def teardown_stream(stream: CameraStream) -> None:
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
    print(
        f"[rtsp-publisher] video_fps={video_fps} h264_preset={h264_preset} h264_crf={h264_crf} "
        f"video_transport=rawvideo-yuv420p legacy_rtsp_mjpeg_q={mjpeg_q}(ignored)"
    )
    if hold_int <= 0:
        print("[rtsp-publisher] still hold=continuous until newer clip arrives")
    else:
        print(
            f"[rtsp-publisher] still hold compatibility note: RTSP_STILL_HOLD_SEC={hold_int} is ignored; stills now hold until replaced"
        )
    print("[rtsp-publisher] hold-audio=zero-valued pcm_s16le silence")

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
            clips_by_cam, skipped = collect_clips_by_camera(files, cam_re)

            cam_summary = tuple(sorted((cam, clips[-1].name) for cam, clips in clips_by_cam.items() if clips))
            if last_file_count != len(files) or last_cam_summary != cam_summary:
                print(f"[rtsp-publisher] scan files={len(files)} matched_cams={len(clips_by_cam)}")
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

            for cam, clips in clips_by_cam.items():
                newest = clips[-1]
                stream_path = f"{stream_prefix}/{slugify(cam)}" if stream_prefix else slugify(cam)
                rtsp_url = f"rtsp://{mediamtx_host}:{mediamtx_port}/{stream_path}"
                existing = streams.get(cam)
                if existing is None:
                    cam_dir = work_root / slugify(cam)
                    cam_dir.mkdir(parents=True, exist_ok=True)
                    audio_server = StreamSocketServer(f"{cam}-audio")
                    video_server = StreamSocketServer(f"{cam}-video")
                    stream = CameraStream(
                        camera=cam,
                        stream_name=stream_path,
                        rtsp_url=rtsp_url,
                        audio_server=audio_server,
                        video_server=video_server,
                        work_dir=cam_dir,
                        stop_event=threading.Event(),
                    )
                    with stream.lock:
                        seed = stream.playback.seed_from_existing(clips)
                    thread = threading.Thread(
                        target=camera_worker,
                        args=(stream,),
                        kwargs={
                            "transport": transport,
                            "video_fps": video_fps,
                            "h264_preset": h264_preset,
                            "h264_crf": h264_crf,
                        },
                        daemon=True,
                    )
                    stream.thread = thread
                    thread.start()
                    streams[cam] = stream
                    print(f"[rtsp-publisher] starting cam={cam} seed_clip={seed.name if seed else '-'}")
                    print(f"[rtsp-publisher] cam={cam} url={rtsp_url}")
                    continue

                with existing.lock:
                    discovered = existing.playback.discover_new_clips(clips)
                if discovered:
                    queued = ", ".join(clip.name for clip in discovered)
                    print(
                        f"[rtsp-publisher] cam={cam} queued_new_clips={len(discovered)} newest={newest.name} clips={queued}"
                    )

            for cam in list(streams.keys()):
                stream = streams[cam]
                publisher = stream.publisher
                if publisher is not None and publisher.poll() is not None:
                    print(f"[rtsp-publisher] cam={cam} publisher exited code={publisher.returncode}")
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
