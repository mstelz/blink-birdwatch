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

from aiohttp import ClientSession
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink

try:
    from blinkpy.exceptions import BlinkTwoFARequiredError
except Exception:  # blinkpy 0.25.x moved/changed exceptions export
    class BlinkTwoFARequiredError(Exception):
        pass


def _auth_file():
    return os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _needs_2fa(err: Exception) -> bool:
    msg = (str(err) or repr(err)).lower()
    return "2fa" in msg or "twofa" in msg or "verification" in msg or "auth key" in msg


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
    username = input("Blink username/email: ").strip()
    password = input("Blink password: ").strip()
    if not username or not password:
        print(json.dumps({"ok": False, "error": "username and password are required"}))
        return 1

    async with ClientSession() as session:
        blink = Blink(session=session)
        blink.auth = Auth({"username": username, "password": password}, no_prompt=True)
        try:
            await blink.start()
        except Exception as exc:
            if not _needs_2fa(exc):
                raise
            await blink.prompt_2fa()

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
