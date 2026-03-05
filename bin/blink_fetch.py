#!/usr/bin/env python3
"""Blink fetch adapter for BLINK_FETCH_COMMAND.

Outputs a JSON array of events:
[{id,timestamp,mediaUrl,thumbnailUrl,source,camera}]

Lockout-safe behavior:
- Reads auth state from SQLite
- Skips Blink calls when paused/locked/needs credentials or 2FA
- On auth failure, sets paused+locked so polling stops until explicit reauth
"""

import asyncio
import glob
import hashlib
import json
import os
import sqlite3
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


def _to_download_since(iso_ts):
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc) - timedelta(hours=24)
    return dt.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M")


def _err_text(err: Exception) -> str:
    msg = str(err).strip()
    return msg or repr(err)


def _needs_2fa(err: Exception) -> bool:
    msg = _err_text(err).lower()
    return "2fa" in msg or "twofa" in msg or "auth key" in msg or "verification" in msg


def _ensure_blink_ready(blink):
    base_url = getattr(blink, "base_url", None) or getattr(blink, "_base_url", None)
    if base_url is None:
        raise RuntimeError("Cannot setup Blink platform (base_url missing)")


def _connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_state (
          id INTEGER PRIMARY KEY CHECK(id=1),
          username TEXT,
          password TEXT,
          authenticated INTEGER NOT NULL DEFAULT 0,
          needs_credentials INTEGER NOT NULL DEFAULT 1,
          needs_2fa INTEGER NOT NULL DEFAULT 0,
          locked_error INTEGER NOT NULL DEFAULT 0,
          paused_fetch INTEGER NOT NULL DEFAULT 1,
          last_error TEXT,
          last_attempt_at TEXT,
          next_allowed_attempt_at TEXT,
          updated_at TEXT
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO auth_state(id, updated_at) VALUES(1, ?)", (_utc_now(),))
    conn.commit()
    return conn


def _row(conn):
    return conn.execute("SELECT * FROM auth_state WHERE id=1").fetchone()


def _update(conn, **fields):
    fields["updated_at"] = _utc_now()
    keys = sorted(fields.keys())
    sets = ", ".join(f"{k}=?" for k in keys)
    vals = [fields[k] for k in keys]
    conn.execute(f"UPDATE auth_state SET {sets} WHERE id=1", vals)
    conn.commit()


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


async def _start_blink(conn, auth_file):
    r = _row(conn)

    if r["paused_fetch"] or r["locked_error"] or r["needs_credentials"] or r["needs_2fa"]:
        return None, "fetch paused by auth state"

    username = (r["username"] or "").strip()
    password = (r["password"] or "").strip()
    if not username or not password:
        _update(
            conn,
            authenticated=0,
            needs_credentials=1,
            needs_2fa=0,
            locked_error=0,
            paused_fetch=1,
            last_error="missing Blink credentials",
            last_attempt_at=_utc_now(),
            next_allowed_attempt_at=_utc_now(),
        )
        return None, "missing Blink credentials"

    _update(conn, last_attempt_at=_utc_now(), next_allowed_attempt_at=_utc_now(), last_error=None)

    auth = Auth({"username": username, "password": password}, no_prompt=True)
    session = aiohttp.ClientSession()
    blink = await _new_blink(session, auth)

    try:
        await blink.start()
        _ensure_blink_ready(blink)
        await blink.refresh(force=True)
        await blink.save(auth_file)
        _update(
            conn,
            authenticated=1,
            needs_credentials=0,
            needs_2fa=0,
            locked_error=0,
            paused_fetch=0,
            last_error=None,
            next_allowed_attempt_at=None,
        )
        return blink, None
    except Exception as exc:
        await session.close()
        if _needs_2fa(exc):
            _update(
                conn,
                authenticated=0,
                needs_credentials=0,
                needs_2fa=1,
                locked_error=1,
                paused_fetch=1,
                last_error="Blink 2FA required",
            )
            return None, "Blink 2FA required"

        _update(
            conn,
            authenticated=0,
            needs_credentials=0,
            needs_2fa=0,
            locked_error=1,
            paused_fetch=1,
            last_error=_err_text(exc),
        )
        return None, _err_text(exc)


async def _main():
    auth_file = os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()
    state_file = os.getenv("BLINK_FETCH_STATE_FILE", "/app/config/blink-fetch-state.json").strip()
    db_file = os.getenv("BLINK_DB_FILE", "/app/config/blink-bridge.db").strip()
    camera_filter = os.getenv("BLINK_CAMERA_NAMES", "").strip()
    max_events = int(os.getenv("BLINK_FETCH_MAX_EVENTS", "25") or "25")

    conn = _connect(db_file)
    blink, err = await _start_blink(conn, auth_file)
    if err or blink is None:
        if err and "paused" not in err:
            print(f"[blink-fetch] {err}", file=sys.stderr)
        print("[]")
        return

    include = set(name.strip().lower() for name in camera_filter.split(",") if name.strip())

    state = _load_json(state_file, {"seen": []})
    seen = set(state.get("seen") or [])
    events = []

    try:
        # Preferred: download new videos since last successful fetch window.
        download_dir = os.getenv("BLINK_DOWNLOAD_DIR", "/app/work/blink-downloads").strip()
        os.makedirs(download_dir, exist_ok=True)
        since_iso = state.get("lastDownloadSince") or state.get("updatedAt") or _utc_now()
        since_arg = _to_download_since(since_iso)

        used_download = False
        if hasattr(blink, "download_videos"):
            try:
                await blink.download_videos(download_dir, since=since_arg, delay=2)
                used_download = True
            except Exception as dl_exc:
                print(f"[blink-fetch] download_videos failed, falling back to recent_clips: {dl_exc}", file=sys.stderr)

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
            # Fallback path for blinkpy variants without download_videos support.
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
    except Exception as exc:
        _update(
            conn,
            authenticated=0,
            locked_error=1,
            paused_fetch=1,
            last_error=f"fetch failed: {exc}",
            next_allowed_attempt_at=_utc_now(),
        )
        print(f"[blink-fetch] {exc}", file=sys.stderr)
        print("[]")
        try:
            bsession = getattr(blink, "session", None) or getattr(blink, "_session", None)
            if bsession is not None and not getattr(bsession, "closed", True):
                await bsession.close()
        except Exception:
            pass
        return

    events.sort(key=lambda e: e.get("timestamp") or "")
    if max_events > 0:
        events = events[-max_events:]

    for ev in events:
        seen.add(ev["id"])

    seen_list = list(seen)[-1000:]
    now_iso = _utc_now()
    _save_json(state_file, {"seen": seen_list, "updatedAt": now_iso, "lastDownloadSince": now_iso})

    try:
        bsession = getattr(blink, "session", None) or getattr(blink, "_session", None)
        if bsession is not None and not getattr(bsession, "closed", True):
            await bsession.close()
    except Exception:
        pass

    print(json.dumps(events))


if __name__ == "__main__":
    asyncio.run(_main())
