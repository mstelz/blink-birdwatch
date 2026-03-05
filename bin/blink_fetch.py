#!/usr/bin/env python3
"""Blink fetch adapter for BLINK_FETCH_COMMAND.

Outputs a JSON array of events:
[{id,timestamp,mediaUrl,thumbnailUrl,source,camera}]
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink


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


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_id(camera_name, clip_time, clip_url):
    raw = f"{camera_name}|{clip_time}|{clip_url}".encode("utf-8")
    return "blink-" + hashlib.sha1(raw).hexdigest()[:16]


async def _start_blink(auth_file, username, password, twofa_code):
    creds = _load_json(auth_file, {})
    if not creds:
        if not username or not password:
            raise RuntimeError("missing Blink credentials: set BLINK_USERNAME and BLINK_PASSWORD")
        creds = {"username": username, "password": password}

    auth = Auth(creds, no_prompt=True)
    async with aiohttp.ClientSession() as session:
        blink = Blink(session=session, auth=auth)
        try:
            await blink.start()
        except Exception as exc:
            msg = str(exc).lower()
            # blinkpy raises specific 2FA exceptions; keep this generic for compatibility.
            if "2fa" in msg or "auth key" in msg or "verification" in msg:
                if not twofa_code:
                    raise RuntimeError(
                        "blink 2FA required. set BLINK_2FA_CODE to the email code and rerun"
                    ) from exc
                await auth.send_auth_key(blink, twofa_code)
                await blink.setup_post_verify()
            else:
                raise

        await blink.refresh(force=True)
        await blink.save(auth_file)
        return blink


async def _main():
    username = os.getenv("BLINK_USERNAME", "").strip()
    password = os.getenv("BLINK_PASSWORD", "").strip()
    twofa_code = os.getenv("BLINK_2FA_CODE", "").strip()
    auth_file = os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()
    state_file = os.getenv("BLINK_FETCH_STATE_FILE", "/app/config/blink-fetch-state.json").strip()
    camera_filter = os.getenv("BLINK_CAMERA_NAMES", "").strip()
    max_events = int(os.getenv("BLINK_FETCH_MAX_EVENTS", "25") or "25")

    try:
        blink = await _start_blink(auth_file, username, password, twofa_code)
    except Exception as exc:
        print(f"[blink-fetch] {exc}", file=sys.stderr)
        print("[]")
        return

    include = set(
        name.strip().lower() for name in camera_filter.split(",") if name.strip()
    )

    state = _load_json(state_file, {"seen": []})
    seen = set(state.get("seen") or [])
    events = []

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

    # Keep state bounded
    seen_list = list(seen)[-1000:]
    _save_json(state_file, {"seen": seen_list, "updatedAt": _utc_now()})

    print(json.dumps(events))


if __name__ == "__main__":
    asyncio.run(_main())
