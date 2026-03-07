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
import logging
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


def _to_download_since(iso_ts, lookback_sec: int | None = None):
    """Convert ISO timestamp (or fallback lookback) to blinkpy since format.

    If iso_ts is missing/invalid, falls back to now - lookback_sec (or 24h).
    """
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except Exception:
        lb = lookback_sec if (lookback_sec is not None and lookback_sec > 0) else 24 * 3600
        dt = datetime.now(timezone.utc) - timedelta(seconds=lb)
    return dt.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M")


async def _new_blink(session, auth):
    # Follow blinkpy docs path explicitly: construct Blink(session=...), then assign auth.
    blink = Blink(session=session)
    if hasattr(blink, "auth"):
        blink.auth = auth
    elif hasattr(blink, "_auth"):
        blink._auth = auth
    return blink


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
    debug = (os.getenv("BLINK_FETCH_DEBUG", "") or "").strip().lower() in ("1", "true", "yes", "on")
    lookback_sec = int(os.getenv("BLINK_FETCH_LOOKBACK_SEC", "0") or "0")
    blinkpy_debug = (os.getenv("BLINKPY_DEBUG", "") or "").strip().lower() in ("1", "true", "yes", "on")
    aiohttp_debug = (os.getenv("AIOHTTP_DEBUG", "") or "").strip().lower() in ("1", "true", "yes", "on")

    def dlog(msg: str):
        if debug:
            print(f"[blink-fetch] {msg}", file=sys.stderr)

    if blinkpy_debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
        # Make sure blinkpy logs are visible even if root logger is configured elsewhere.
        logging.getLogger("blinkpy").setLevel(logging.DEBUG)
        if aiohttp_debug:
            logging.getLogger("aiohttp.client").setLevel(logging.DEBUG)
            logging.getLogger("aiohttp.connector").setLevel(logging.DEBUG)
        dlog(f"enabled blinkpy debug (aiohttp_debug={aiohttp_debug})")

    creds = _load_json(auth_file, {})
    # Prefer token-based operation: after `blink login`, blinkpy may persist tokens but not the raw password.
    # We only require a valid token set to proceed.
    has_tokens = bool(creds.get("access_token") or creds.get("refresh_token") or creds.get("account_id"))
    if debug:
        dlog(
            f"auth_file={auth_file} has_tokens={has_tokens} has_username={bool((creds.get('username') or '').strip())} "
            f"has_password={bool((creds.get('password') or '').strip())}"
        )
    if not has_tokens:
        print("[blink-fetch] missing tokens in BLINK_AUTH_FILE; run: blink login", file=sys.stderr)
        print("[]")
        return

    state = _load_json(state_file, {"seen": []})
    seen = set(state.get("seen") or [])
    events = []

    if debug:
        dlog(
            f"state_file={state_file} seen={len(seen)} lastDownloadSince={state.get('lastDownloadSince')} updatedAt={state.get('updatedAt')}"
        )

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        auth = Auth(creds, no_prompt=True)
        # blinkpy's Auth may create its own aiohttp session if not provided; force reuse
        # our session to avoid "Unclosed client session" warnings.
        if hasattr(auth, "session"):
            auth.session = session

        blink = await _new_blink(session, auth)

        try:
            dlog("starting blink session")
            await blink.start()
            dlog("refreshing blink")
            await blink.refresh(force=True)

            include = set(name.strip().lower() for name in camera_filter.split(",") if name.strip())
            dlog(f"camera_filter={camera_filter!r} include={sorted(include)}")

            os.makedirs(download_dir, exist_ok=True)
            dlog(f"download_dir={download_dir}")
            # If we've never run before, pull a small backlog (default handled by _to_download_since)
            # instead of "since now", which would always yield 0 events on first run.
            since_iso = state.get("lastDownloadSince") or state.get("updatedAt")
            since_arg = _to_download_since(since_iso, lookback_sec=lookback_sec)
            dlog(f"since_iso={since_iso!r} since_arg={since_arg!r} lookback_sec={lookback_sec}")

            used_download = False
            if hasattr(blink, "download_videos"):
                try:
                    dlog("attempting blink.download_videos")
                    await blink.download_videos(download_dir, since=since_arg, delay=2)
                    used_download = True
                    dlog("download_videos completed")
                except Exception as dl_exc:
                    print(
                        f"[blink-fetch] download_videos failed, falling back to recent_clips: {_err_text(dl_exc)}",
                        file=sys.stderr,
                    )
                    dlog(f"download_videos failed: {_err_text(dl_exc)}")
            else:
                dlog("blink.download_videos not available; using recent_clips")

            if used_download:
                patterns = ["*.mp4", "*.m4v", "*.mov"]
                files = []
                for pat in patterns:
                    files.extend(glob.glob(os.path.join(download_dir, "**", pat), recursive=True))
                dlog(f"download scan patterns={patterns} files_found={len(files)}")

                for fpath in sorted(set(files)):
                    try:
                        st = os.stat(fpath)
                    except Exception:
                        continue
                    ts = (
                        datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
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
                try:
                    cam_names = list((blink.cameras or {}).keys())
                except Exception:
                    cam_names = []
                dlog(f"cameras={len(cam_names)} names={cam_names}")

                for camera_name, camera in blink.cameras.items():
                    if include and camera_name.lower() not in include:
                        continue

                    recent = list(camera.recent_clips or [])
                    dlog(f"camera={camera_name} recent_clips={len(recent)}")
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

            # Strategy A:
            # - Always update updatedAt
            # - Only advance lastDownloadSince when we actually observed new events
            #   (prevents "chasing now" and missing backlog when polls return 0)
            now_iso = _utc_now()
            last_since = state.get("lastDownloadSince") or state.get("updatedAt")
            if events:
                # Use the newest event timestamp if available, else now.
                newest_ts = (events[-1].get("timestamp") or "").strip()
                last_since = newest_ts or now_iso
            _save_json(
                state_file,
                {"seen": list(seen)[-1000:], "updatedAt": now_iso, "lastDownloadSince": last_since},
            )

            dlog(f"events_found={len(events)} (before max_events trim) next_lastDownloadSince={last_since}")
            await blink.save(auth_file)
            print(json.dumps(events))
        except Exception as exc:
            print(f"[blink-fetch] {_err_text(exc)}", file=sys.stderr)
            print("[]")
        finally:
            # Close any blinkpy-created session if it didn't reuse ours.
            try:
                bsession = getattr(blink, "session", None) or getattr(blink, "_session", None)
                if bsession is not None and bsession is not session and not getattr(bsession, "closed", True):
                    await bsession.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(_main())
