#!/usr/bin/env python3
"""Blink BirdWatch bridge service (Python-native).

Features:
- GET /health
- POST /bridge/blink/event
- periodic BLINK_FETCH_COMMAND polling
- persisted dedupe state
- mp4 -> mono 48k wav via ffmpeg
- writes wavs to BIRDNET_GO_INPUT_DIR
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re

from aiohttp import web


class Config:
    port = int(os.getenv("PORT", "8787") or "8787")
    poll_interval_sec = int(os.getenv("POLL_INTERVAL_SEC", "180") or "180")
    blink_poll_interval_sec = int(os.getenv("BLINK_POLL_INTERVAL_SEC", "180") or "180")
    work_dir = Path(os.getenv("WORK_DIR", "./work"))
    output_dir = Path(os.getenv("BIRDNET_GO_INPUT_DIR", "/app/output"))
    fetch_command = os.getenv("BLINK_FETCH_COMMAND", "python3 /app/bin/blink_fetch.py").strip()
    seen_ids_file = Path(os.getenv("SEEN_IDS_FILE", "./work/.seen-motion-ids.json"))
    max_seen_ids = int(os.getenv("MAX_SEEN_IDS", "10000") or "10000")
    debug = (os.getenv("BRIDGE_DEBUG", "") or "").strip().lower() in ("1", "true", "yes", "on")

    # If an event comes from the downloaded mp4 folder, optionally delete the mp4 after successful processing.
    cleanup_mp4 = (os.getenv("CLEANUP_MP4", "") or "").strip().lower() in ("1", "true", "yes", "on")
    download_dir = Path(os.getenv("BLINK_DOWNLOAD_DIR", str(work_dir / "blink-downloads"))).resolve()

    # Optionally persist a copy of each processed MP4 into a directory for RTSP publishing.
    persist_mp4 = (os.getenv("PERSIST_MP4", "") or "").strip().lower() in ("1", "true", "yes", "on")
    persist_mp4_dir = Path(os.getenv("PERSIST_MP4_DIR", str(download_dir))).resolve()

    # If the source MP4 already lives in the persistent clip directory, avoid making a duplicate copy.
    persist_existing_local_mp4 = (os.getenv("PERSIST_EXISTING_LOCAL_MP4", "") or "").strip().lower() in ("1", "true", "yes", "on")

    # Toggle BirdNET audio generation. Disable for RTSP-only mode.
    generate_wav = (os.getenv("GENERATE_WAV", "1") or "1").strip().lower() in ("1", "true", "yes", "on")

    # After a successful WAV extraction, optionally prune older MP4s for the same camera and keep only the newest.
    prune_old_mp4 = (os.getenv("PRUNE_OLD_MP4", "") or "").strip().lower() in ("1", "true", "yes", "on")

def _slugify_filename_part(value: str | None, default: str = "camera") -> str:
    txt = (value or "").strip().lower()
    txt = re.sub(r"[^a-z0-9]+", "-", txt)
    txt = re.sub(r"-+", "-", txt).strip("-")
    return txt or default


def _camera_slug_from_filename(path: Path) -> str | None:
    name = path.name
    m = re.match(r"^(?P<camera>.+?)-\d{4}-\d{2}-\d{2}t\d{2}-\d{2}-\d{2}(?:-\d{1,6})?(?:[+-]\d{2}-\d{2})?\.mp4$", name, re.IGNORECASE)
    if not m:
        return None
    return _slugify_filename_part(m.group("camera"), default="camera")


class BridgeService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.seen_ids: list[str] = []
        self.seen_set: set[str] = set()
        self.processing: set[str] = set()
        self.poll_task: asyncio.Task | None = None

    def dlog(self, msg: str) -> None:
        if self.cfg.debug:
            print(f"[bridge][debug] {msg}")

    def load_seen_ids(self) -> None:
        path = self.cfg.seen_ids_file
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, str) and item:
                            self.seen_ids.append(item)
                            self.seen_set.add(item)
            if self.seen_ids:
                print(f"[bridge] loaded {len(self.seen_ids)} seen IDs")
        except Exception as exc:
            print(f"[bridge] failed to load seen IDs: {exc}")

    def persist_seen_ids(self) -> None:
        path = self.cfg.seen_ids_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.seen_ids[-self.cfg.max_seen_ids :], indent=2) + "\n", encoding="utf-8")

    async def add_seen(self, event_id: str) -> bool:
        async with self.lock:
            if event_id in self.seen_set or event_id in self.processing:
                return False
            self.processing.add(event_id)
            return True

    async def mark_done(self, event_id: str, success: bool) -> None:
        async with self.lock:
            self.processing.discard(event_id)
            if success and event_id not in self.seen_set:
                self.seen_set.add(event_id)
                self.seen_ids.append(event_id)
                if len(self.seen_ids) > self.cfg.max_seen_ids:
                    drop = len(self.seen_ids) - self.cfg.max_seen_ids
                    dropped = self.seen_ids[:drop]
                    self.seen_ids = self.seen_ids[drop:]
                    for sid in dropped:
                        if sid not in self.seen_ids:
                            self.seen_set.discard(sid)
                self.persist_seen_ids()

    @staticmethod
    def _stamp(ts: str | None) -> str:
        dt = datetime.now(timezone.utc)
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                pass
        return dt.isoformat().replace(":", "-").replace(".", "-")

    def _candidate_local_paths(self, local_file: str) -> list[Path]:
        raw = Path(local_file).expanduser()
        candidates: list[Path] = []
        seen: set[str] = set()
        for candidate in (raw, self.cfg.download_dir / raw.name, self.cfg.persist_mp4_dir / raw.name):
            resolved = candidate.resolve()
            key = str(resolved)
            if key not in seen:
                candidates.append(resolved)
                seen.add(key)
        return candidates

    async def _copy_local_file(self, local_file: str, out: Path) -> Path:
        last_exc: Exception | None = None
        previous: dict[Path, tuple[int, int] | None] = {}
        stable_hits: dict[Path, int] = {}
        tmp_out = out.with_name(out.name + ".part")

        for _ in range(12):
            for candidate in self._candidate_local_paths(local_file):
                try:
                    st = candidate.stat()
                except FileNotFoundError as exc:
                    last_exc = exc
                    previous.pop(candidate, None)
                    stable_hits.pop(candidate, None)
                    continue

                sig = (st.st_size, st.st_mtime_ns)
                if st.st_size <= 0:
                    previous[candidate] = sig
                    stable_hits[candidate] = 0
                    continue

                if previous.get(candidate) == sig:
                    stable_hits[candidate] = stable_hits.get(candidate, 0) + 1
                else:
                    previous[candidate] = sig
                    stable_hits[candidate] = 0
                    continue

                try:
                    if tmp_out.exists():
                        tmp_out.unlink(missing_ok=True)
                    shutil.copy2(candidate, tmp_out)
                    tmp_out.replace(out)
                    return candidate
                except FileNotFoundError as exc:
                    last_exc = exc
                finally:
                    tmp_out.unlink(missing_ok=True)

            await asyncio.sleep(0.25)

        raise last_exc or FileNotFoundError(local_file)

    async def process_event(self, event: dict[str, Any]) -> tuple[bool, str | None]:
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            return False, "id is required"

        self.dlog(f"process_event id={event_id} keys={sorted(list(event.keys()))}")

        admitted = await self.add_seen(event_id)
        if not admitted:
            self.dlog(f"skip duplicate/inflight id={event_id}")
            return True, None

        media_url = event.get("mediaUrl")
        local_file = event.get("localFile")
        if not media_url and not local_file:
            await self.mark_done(event_id, False)
            return False, "mediaUrl or localFile required"

        stamp = self._stamp(event.get('timestamp'))
        clip_stem = f"blink_{stamp}"
        local_src_path = Path(local_file).resolve() if local_file else None
        camera_slug = _slugify_filename_part(event.get("camera"), default="camera")
        if camera_slug == "download" and local_src_path is not None:
            inferred = _camera_slug_from_filename(local_src_path)
            if inferred:
                camera_slug = inferred
        persisted_stem = f"{camera_slug}-{stamp}"
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        mp4_path = self.cfg.work_dir / f"{clip_stem}.mp4"
        wav_tmp = self.cfg.work_dir / f"{clip_stem}.wav"
        wav_out = self.cfg.output_dir / f"{clip_stem}.wav"

        try:
            if local_file:
                self.dlog(f"copy local_file={local_file} -> {mp4_path}")
                try:
                    local_src_path = await self._copy_local_file(local_file, mp4_path)
                except FileNotFoundError:
                    if media_url:
                        self.dlog(f"local_file vanished; falling back to mediaUrl={media_url}")
                        await self._download_file(str(media_url), mp4_path)
                    else:
                        raise
            else:
                self.dlog(f"download mediaUrl={media_url} -> {mp4_path}")
                await self._download_file(str(media_url), mp4_path)

            # Optionally persist the MP4 for RTSP publishing / debugging.
            if self.cfg.persist_mp4:
                try:
                    self.cfg.persist_mp4_dir.mkdir(parents=True, exist_ok=True)
                    persist_name = f"{persisted_stem}.mp4"
                    persist_path = self.cfg.persist_mp4_dir / persist_name
                    src_already_persistent = (
                        local_src_path is not None
                        and self.cfg.persist_mp4_dir == local_src_path.parent
                        and local_src_path.name == persist_name
                    )
                    if src_already_persistent and not self.cfg.persist_existing_local_mp4:
                        self.dlog(f"persist skip existing local mp4 {local_src_path}")
                    else:
                        shutil.copy2(mp4_path, persist_path)
                        self.dlog(f"persisted mp4 -> {persist_path}")
                except Exception as persist_exc:
                    self.dlog(f"persist mp4 failed: {persist_exc}")

            if self.cfg.generate_wav:
                self.dlog(f"extract wav {mp4_path} -> {wav_tmp}")
                await self._extract_wav(mp4_path, wav_tmp)
                # wav_tmp and wav_out may be on different filesystems (e.g., separate Docker bind mounts).
                # Path.replace() uses os.rename() which fails cross-device with EXDEV.
                shutil.move(str(wav_tmp), str(wav_out))
                print(f"[bridge] emitted {wav_out.name}")
            else:
                self.dlog(f"skip wav extraction for {mp4_path} (GENERATE_WAV disabled)")

            # Optionally clean up downloaded source mp4s to save space.
            if self.cfg.cleanup_mp4 and local_file:
                try:
                    src = Path(local_file).resolve()
                    if self.cfg.download_dir in src.parents:
                        src.unlink(missing_ok=True)
                        self.dlog(f"cleaned up source mp4 {src}")
                except Exception as cleanup_exc:
                    self.dlog(f"cleanup mp4 failed: {cleanup_exc}")

            if self.cfg.prune_old_mp4:
                try:
                    self._prune_old_mp4(camera_slug=camera_slug)
                except Exception as prune_exc:
                    self.dlog(f"prune old mp4 failed: {prune_exc}")

            await self.mark_done(event_id, True)
            return True, None
        except Exception as exc:
            await self.mark_done(event_id, False)
            print(f"[bridge] failed id={event_id}: {exc}")
            return False, str(exc)
        finally:
            for p in (mp4_path, wav_tmp):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    async def _download_file(self, url: str, out: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-fsSL",
            "--retry",
            "3",
            "--retry-delay",
            "1",
            "-o",
            str(out),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError((stderr or b"").decode("utf-8", errors="ignore").strip() or "download failed")

    async def _extract_wav(self, mp4_path: Path, wav_path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(mp4_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(wav_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError((stderr or b"").decode("utf-8", errors="ignore").strip() or "ffmpeg failed")

    def _prune_old_mp4(self, *, camera_slug: str) -> None:
        keep: Path | None = None
        matches: list[Path] = []
        for pattern in (
            f"{camera_slug}-*.mp4",
            f"{camera_slug}_*.mp4",
        ):
            matches.extend(self.cfg.persist_mp4_dir.glob(pattern))

        # Back-compat / generic fallback names. If we still have old duplicated download/blink files
        # for a single-camera setup, keep only the newest of those too.
        if camera_slug in ("blink", "download"):
            matches.extend(self.cfg.persist_mp4_dir.glob("download-*.mp4"))
            matches.extend(self.cfg.persist_mp4_dir.glob("blink_*.mp4"))

        seen: dict[Path, float] = {}
        for path in matches:
            try:
                seen[path] = path.stat().st_mtime
            except FileNotFoundError:
                continue

        if not seen:
            return

        keep = max(seen, key=seen.get)
        for path in seen:
            if path == keep:
                continue
            try:
                path.unlink(missing_ok=True)
                self.dlog(f"pruned old mp4 {path}")
            except Exception as exc:
                self.dlog(f"failed pruning {path}: {exc}")

    async def fetch_loop(self) -> None:
        if not self.cfg.fetch_command:
            print("[bridge] BLINK_FETCH_COMMAND empty; fetch loop disabled")
            return
        while True:
            try:
                events = await self.run_fetch_command()
                emitted = 0
                failed = 0
                for ev in events:
                    ok, err = await self.process_event(ev)
                    if ok and not err:
                        emitted += 1
                    else:
                        failed += 1
                        if err:
                            self.dlog(f"event failed: ok={ok} err={err}")
                print(f"[bridge] poll complete events={len(events)} emitted={emitted} failed={failed}")
            except Exception as exc:
                print(f"[bridge] poll failed: {exc}")
            await asyncio.sleep(max(5, self.cfg.blink_poll_interval_sec))

    async def run_fetch_command(self) -> list[dict[str, Any]]:
        proc = await asyncio.create_subprocess_shell(
            self.cfg.fetch_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/bash",
        )
        stdout, stderr = await proc.communicate()
        err = (stderr or b"").decode("utf-8", errors="ignore").strip()
        if err:
            print(f"[bridge] fetch stderr: {err}")
        if proc.returncode != 0:
            raise RuntimeError(f"fetch command failed with code {proc.returncode}")
        txt = (stdout or b"[]").decode("utf-8", errors="ignore").strip() or "[]"
        parsed = json.loads(txt)
        if not isinstance(parsed, list):
            raise RuntimeError("BLINK_FETCH_COMMAND output must be a JSON array")
        return [ev for ev in parsed if isinstance(ev, dict)]


def create_app() -> web.Application:
    cfg = Config()
    service = BridgeService(cfg)
    service.load_seen_ids()

    app = web.Application()
    app["service"] = service

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "port": cfg.port,
                "workDir": str(cfg.work_dir),
                "outputDir": str(cfg.output_dir),
                "seenEvents": len(service.seen_ids),
                "pollIntervalSec": cfg.blink_poll_interval_sec,
            }
        )

    async def post_event(request: web.Request) -> web.Response:
        body = await request.json()
        ok, err = await service.process_event(body if isinstance(body, dict) else {})
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        return web.json_response({"ok": True, "added": err is None})

    app.router.add_get("/health", health)
    app.router.add_post("/bridge/blink/event", post_event)

    async def on_startup(_app: web.Application) -> None:
        service.poll_task = asyncio.create_task(service.fetch_loop())

    async def on_cleanup(_app: web.Application) -> None:
        task = service.poll_task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    import contextlib

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    app = create_app()
    cfg = Config()
    web.run_app(app, host="0.0.0.0", port=cfg.port)


if __name__ == "__main__":
    main()
