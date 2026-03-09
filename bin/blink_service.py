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

        stamp = f"blink_{self._stamp(event.get('timestamp'))}"
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        mp4_path = self.cfg.work_dir / f"{stamp}.mp4"
        wav_tmp = self.cfg.work_dir / f"{stamp}.wav"
        wav_out = self.cfg.output_dir / f"{stamp}.wav"

        try:
            if local_file:
                self.dlog(f"copy local_file={local_file} -> {mp4_path}")
                shutil.copy2(local_file, mp4_path)
            else:
                self.dlog(f"download mediaUrl={media_url} -> {mp4_path}")
                await self._download_file(str(media_url), mp4_path)

            # Optionally persist the MP4 for RTSP publishing / debugging.
            if self.cfg.persist_mp4:
                try:
                    self.cfg.persist_mp4_dir.mkdir(parents=True, exist_ok=True)
                    persist_name = mp4_path.name
                    persist_path = self.cfg.persist_mp4_dir / persist_name
                    shutil.copy2(mp4_path, persist_path)
                    self.dlog(f"persisted mp4 -> {persist_path}")
                except Exception as persist_exc:
                    self.dlog(f"persist mp4 failed: {persist_exc}")

            self.dlog(f"extract wav {mp4_path} -> {wav_tmp}")
            await self._extract_wav(mp4_path, wav_tmp)
            # wav_tmp and wav_out may be on different filesystems (e.g., separate Docker bind mounts).
            # Path.replace() uses os.rename() which fails cross-device with EXDEV.
            shutil.move(str(wav_tmp), str(wav_out))
            print(f"[bridge] emitted {wav_out.name}")

            # Optionally clean up downloaded source mp4s to save space.
            if self.cfg.cleanup_mp4 and local_file:
                try:
                    src = Path(local_file).resolve()
                    if self.cfg.download_dir in src.parents:
                        src.unlink(missing_ok=True)
                        self.dlog(f"cleaned up source mp4 {src}")
                except Exception as cleanup_exc:
                    self.dlog(f"cleanup mp4 failed: {cleanup_exc}")

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
