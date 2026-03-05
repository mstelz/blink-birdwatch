#!/usr/bin/env python3
"""Blink fetch adapter for BLINK_FETCH_COMMAND.

Outputs a JSON array of events:
[{id,timestamp,mediaUrl,localFile,thumbnailUrl,source,camera}]

Simple file-based auth flow:
- credentials/tokens live in BLINK_AUTH_FILE
- no sqlite auth DB/state machine
- dedupe stays in BLINK_FETCH_STATE_FILE
"""

import asyncio
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _event_id(camera_name, clip_time, clip_url):
    raw = f"{camera_name}|{clip_time}|{clip_url}".encode("utf-8")
    return "blink-" + hashlib.sha1(raw).hexdigest()[:16]


def _err_text(err: Exception) -> str:
    msg = str(err).strip()
    return msg or repr(err)


def _to_download_since(iso_ts):
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc) - timedelta(hours=24)
    return dt.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M")


async def _new_blink(session, auth):
    try:
        return Blink(session=session, auth=auth)
    except TypeError:
        blink = Blink(session=session)
        if hasattr(blink, "auth"):
            blink.auth = auth
        elif hasattr(blink, "_auth"):
            blink._auth = auth
        return blink


async def _ensure_blink_ready(blink):
    # Some blinkpy versions need explicit setup before base_url is available.
    setup = getattr(blink, "setup", None)
    if callable(setup):
        await setup()

    base_url = getattr(blink, "base_url", None) or getattr(blink, "_base_url", None)
    if not base_url:
        raise RuntimeError("Cannot setup Blink platform (base_url missing)")


async def _cleanup(blink, session):
    try:
        bsession = getattr(blink, "session", None) or getattr(blink, "_session", None)
        if bsession is not None and bsession is not session and not getattr(bsession, "closed", True):
            await bsession.close()
    except Exception:
        pass
    try:
        if session is not None and not session.closed:
            await session.close()
    except Exception:
        pass


async def _main():
    auth_file = os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()
    state_file = os.getenv("BLINK_FETCH_STATE_FILE", "/app/config/blink-fetch-state.json").strip()
    camera_filter = os.getenv("BLINK_CAMERA_NAMES", "").strip()
    max_events = int(os.getenv("BLINK_FETCH_MAX_EVENTS", "25") or "25")
    download_dir = os.getenv("BLINK_DOWNLOAD_DIR", "/app/work/blink-downloads").strip()

    creds = _load_json(auth_file, {})
    username = (creds.get("username") or "").strip()
    password = (creds.get("password") or "").strip()
    if not username or not password:
        print("[blink-fetch] missing username/password in BLINK_AUTH_FILE; run: blink login", file=sys.stderr)
        print("[]")
        return

    state = _load_json(state_file, {"seen": []})
    seen = set(state.get("seen") or [])
    events = []

    auth = Auth(creds, no_prompt=True)
    session = aiohttp.ClientSession()
    blink = await _new_blink(session, auth)

    try:
        await blink.start()
        await blink.refresh(force=True)

        include = set(name.strip().lower() for name in camera_filter.split(",") if name.strip())

        os.makedirs(download_dir, exist_ok=True)
        since_iso = state.get("lastDownloadSince") or state.get("updatedAt") or _utc_now()
        since_arg = _to_download_since(since_iso)

        used_download = False
        if hasattr(blink, "download_videos"):
            try:
                await blink.download_videos(download_dir, since=since_arg, delay=2)
                used_download = True
            except Exception as dl_exc:
                print(f"[blink-fetch] download_videos failed, falling back to recent_clips: {_err_text(dl_exc)}", file=sys.stderr)

        if used_download:
            for fpath in sorted(glob.glob(os.path.join(download_dir, "**", "*.mp4"), recursive=True)):
                try:
                    st = os.stat(fpath)
                except Exception:
                    continue
                ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                event_id = _event_id("download", ts, fpath)
                if event_id in seen:
                    continue
                events.append(
                    {
                        "id": event_id,
                        "timestamp": ts,
                        "mediaUrl": None,
                        "localFile": fpath,
                        "thumbnailUrl": None,
                        "source": "blink",
                        "camera": "download",
                    }
                )
        else:
            for camera_name, camera in blink.cameras.items():
                if include and camera_name.lower() not in include:
                    continue

                recent = list(camera.recent_clips or [])
                for clip in recent:
                    clip_url = clip.get("clip")
                    clip_time = clip.get("time") or _utc_now()
                    if not clip_url:
                        continue

                    event_id = _event_id(camera_name, clip_time, clip_url)
                    if event_id in seen:
                        continue

                    events.append(
                        {
                            "id": event_id,
                            "timestamp": clip_time,
                            "mediaUrl": clip_url,
                            "thumbnailUrl": camera.thumbnail,
                            "source": "blink",
                            "camera": camera_name,
                        }
                    )

        events.sort(key=lambda e: e.get("timestamp") or "")
        if max_events > 0:
            events = events[-max_events:]

        for ev in events:
            seen.add(ev["id"])

        now_iso = _utc_now()
        _save_json(state_file, {"seen": list(seen)[-1000:], "updatedAt": now_iso, "lastDownloadSince": now_iso})

        await blink.save(auth_file)
        print(json.dumps(events))
    except Exception as exc:
        print(f"[blink-fetch] {_err_text(exc)}", file=sys.stderr)
        print("[]")
    finally:
        await _cleanup(blink, session)


if __name__ == "__main__":
    asyncio.run(_main())
