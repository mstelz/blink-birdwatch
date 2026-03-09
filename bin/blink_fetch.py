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
    ignore_seen = (os.getenv("BLINK_FETCH_IGNORE_SEEN", "") or "").strip().lower() in ("1", "true", "yes", "on")
    no_save_state = (os.getenv("BLINK_FETCH_NO_SAVE_STATE", "") or "").strip().lower() in ("1", "true", "yes", "on")
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
            f"state_file={state_file} seen={len(seen)} lastDownloadSince={state.get('lastDownloadSince')} updatedAt={state.get('updatedAt')} ignore_seen={ignore_seen} no_save_state={no_save_state}"
        )

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Pass the aiohttp session into Auth so blinkpy doesn't create its own.
        # This prevents "Unclosed client session" warnings.
        auth = Auth(creds, no_prompt=True, session=session)

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

            # Prefer metadata-based clip discovery. This is more reliable than
            # camera.recent_clips (often empty) and avoids depending on blink.download_videos().
            # We emit events with mediaUrl (relative -> absolute), and let the bridge download.
            #
            # Optionally, try download_videos first if explicitly requested.
            fetch_mode = (os.getenv("BLINK_FETCH_MODE", "metadata") or "metadata").strip().lower()

            def _infer_base_url() -> str:
                # Try to derive a stable base URL from any camera thumbnail URL.
                try:
                    for cam in (blink.cameras or {}).values():
                        thumb = getattr(cam, "thumbnail", None)
                        if isinstance(thumb, str) and thumb.startswith("http"):
                            from urllib.parse import urlsplit
                            u = urlsplit(thumb)
                            return f"{u.scheme}://{u.netloc}"
                except Exception:
                    pass
                # Fall back: blinkpy Auth defaults are region-specific; this is best-effort.
                return "https://rest-u037.immedia-semi.com"

            base_url = _infer_base_url()
            dlog(f"base_url={base_url}")

            async def emit_from_metadata() -> None:
                nonlocal events
                # Ask for a limited number; we can tune via env.
                meta_stop = int(os.getenv("BLINK_FETCH_META_STOP", "200") or "200")
                dlog(f"fetching videos metadata stop={meta_stop} since_arg={since_arg!r}")
                md = await blink.get_videos_metadata(since=since_arg, stop=meta_stop)
                dlog(f"metadata_count={len(md) if md else 0}")
                if not md:
                    return

                for m in md:
                    try:
                        if m.get("deleted"):
                            continue
                        if m.get("type") != "video":
                            continue
                        media = (m.get("media") or "").strip()
                        if not media or not media.endswith(".mp4"):
                            continue
                        camera_name = (m.get("device_name") or "").strip() or "unknown"
                        if include and camera_name.lower() not in include:
                            continue

                        # created_at is the most consistent field we've observed.
                        ts = (m.get("created_at") or m.get("updated_at") or _utc_now()).strip()
                        # normalize iso with +00:00 -> Z
                        ts = ts.replace("+00:00", "Z")

                        media_url = base_url.rstrip("/") + media
                        thumb = (m.get("thumbnail") or "").strip()
                        thumb_url = (base_url.rstrip("/") + thumb) if thumb.startswith("/") else (thumb or None)

                        event_id = _event_id(camera_name, ts, media_url)
                        if not ignore_seen and event_id in seen:
                            continue
                        events.append(
                            {
                                "id": event_id,
                                "timestamp": ts,
                                "mediaUrl": media_url,
                                "thumbnailUrl": thumb_url,
                                "source": "blink",
                                "camera": camera_name,
                            }
                        )
                    except Exception as _e:
                        continue

            used_download = False
            if fetch_mode == "download" and hasattr(blink, "download_videos"):
                # Keep the old download mode available, but it's not the default.
                try:
                    dlog("attempting blink.download_videos")
                    before: set[str] = set(
                        os.path.abspath(p)
                        for p in glob.glob(os.path.join(download_dir, "**", "*.mp4"), recursive=True)
                        + glob.glob(os.path.join(download_dir, "**", "*.m4v"), recursive=True)
                        + glob.glob(os.path.join(download_dir, "**", "*.mov"), recursive=True)
                    )
                    await blink.download_videos(download_dir, since=since_arg, delay=2)
                    after: set[str] = set(
                        os.path.abspath(p)
                        for p in glob.glob(os.path.join(download_dir, "**", "*.mp4"), recursive=True)
                        + glob.glob(os.path.join(download_dir, "**", "*.m4v"), recursive=True)
                        + glob.glob(os.path.join(download_dir, "**", "*.mov"), recursive=True)
                    )
                    new_files = after - before
                    used_download = True
                    dlog(f"download_videos completed new_files={len(new_files)}")

                    # emit localFile events
                    files = sorted(new_files)
                    for fpath in files:
                        try:
                            st = os.stat(fpath)
                        except Exception:
                            continue
                        ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                        event_id = _event_id("download", ts, fpath)
                        if not ignore_seen and event_id in seen:
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
                except Exception as dl_exc:
                    dlog(f"download_videos failed: {_err_text(dl_exc)}")

            if not used_download:
                await emit_from_metadata()

            events.sort(key=lambda e: e.get("timestamp") or "")
            if max_events > 0:
                events = events[-max_events:]

            if not no_save_state:
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
            else:
                last_since = state.get("lastDownloadSince") or state.get("updatedAt")

            dlog(f"events_found={len(events)} (before max_events trim) next_lastDownloadSince={last_since}")
            await blink.save(auth_file)
            print(json.dumps(events))
        except Exception as exc:
            print(f"[blink-fetch] {_err_text(exc)}", file=sys.stderr)
            print("[]")
        finally:
            # Close any blinkpy-created session if it didn't reuse ours.
            # With Auth(..., session=session) this should be a no-op.
            try:
                bsession = getattr(blink, "session", None) or getattr(blink, "_session", None)
                if bsession is not None and bsession is not session and not getattr(bsession, "closed", True):
                    await bsession.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(_main())
