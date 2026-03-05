#!/usr/bin/env python3
"""Simple Blink auth helper (file-based, no DB).

Commands:
  status
  login
"""

import asyncio
import json
import os
import sys

import aiohttp
from blinkpy.blinkpy import Blink


def _auth_file():
    return os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _err_text(err: Exception) -> str:
    msg = str(err).strip()
    return msg or repr(err)


async def _cleanup(blink, session, auth=None):
    candidates = []
    if session is not None:
        candidates.append(session)

    for obj in (blink, auth):
        if obj is None:
            continue
        for name in ("session", "_session", "http_session", "_http_session"):
            s = getattr(obj, name, None)
            if s is not None:
                candidates.append(s)

    seen = set()
    for s in candidates:
        try:
            sid = id(s)
            if sid in seen:
                continue
            seen.add(sid)
            if not getattr(s, "closed", True):
                await s.close()
        except Exception:
            pass


def _status_payload():
    auth_file = _auth_file()
    creds = _load_json(auth_file, {})
    has_credentials = bool((creds.get("username") or "").strip() and (creds.get("password") or "").strip())
    has_tokens = bool(creds.get("access_token") or creds.get("refresh_token") or creds.get("account_id"))
    return {
        "ok": True,
        "auth_file": auth_file,
        "has_credentials": has_credentials,
        "authenticated": has_tokens,
    }


async def _interactive_login():
    print("Blink interactive login")

    auth_file = _auth_file()
    session = aiohttp.ClientSession()
    blink = Blink(session=session)

    try:
        # Use blinkpy-native interactive login path (prompts for credentials + MFA as needed).
        await blink.start()
        if hasattr(blink, "refresh"):
            await blink.refresh(force=True)
        await blink.save(auth_file)
        print(json.dumps(_status_payload()))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": _err_text(exc)}))
        return 1
    finally:
        await _cleanup(blink, session)


async def _main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()

    if cmd == "status":
        print(json.dumps(_status_payload()))
        return 0

    if cmd == "login":
        return await _interactive_login()

    print(json.dumps({"ok": False, "error": f"unknown command: {cmd}"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
