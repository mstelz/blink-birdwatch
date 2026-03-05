#!/usr/bin/env python3
"""Simple Blink auth helper (file-based).

Commands:
  status
  login
"""

import asyncio
import json
import os
import sys

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink


def _auth_file():
    return os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


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
    auth_file = _auth_file()
    async with ClientSession() as session:
        blink = Blink(session=session)
        await blink.start()  # native interactive prompt flow (username/password/2FA)
        if hasattr(blink, "refresh"):
            await blink.refresh(force=True)
        await blink.save(auth_file)

    print(json.dumps(_status_payload()))
    return 0


async def _main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()

    if cmd == "status":
        print(json.dumps(_status_payload()))
        return 0

    if cmd == "login":
        try:
            return await _interactive_login()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc) or repr(exc)}))
            return 1

    print(json.dumps({"ok": False, "error": f"unknown command: {cmd}"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
