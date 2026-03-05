#!/usr/bin/env python3
"""Blink auth helper for bridge API/UI.

Commands:
  status
  verify-2fa <code>
"""

import asyncio
import json
import os
import sys

import aiohttp
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _print(obj):
    print(json.dumps(obj))


def _needs_2fa(err: Exception) -> bool:
    msg = str(err).lower()
    return "2fa" in msg or "auth key" in msg or "verification" in msg


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


async def _attempt(auth_file, username, password, twofa_code=""):
    creds = _load_json(auth_file, {})
    if not creds:
        if not username or not password:
            return {
                "ok": False,
                "authenticated": False,
                "needs2fa": False,
                "hasCredentials": False,
                "authFile": auth_file,
                "error": "missing Blink credentials",
            }
        creds = {"username": username, "password": password}

    auth = Auth(creds, no_prompt=True)
    async with aiohttp.ClientSession() as session:
        blink = await _new_blink(session, auth)
        try:
            await blink.start()
        except Exception as exc:
            if _needs_2fa(exc):
                if twofa_code:
                    try:
                        await auth.send_auth_key(blink, twofa_code)
                        await blink.setup_post_verify()
                    except Exception as v_exc:
                        return {
                            "ok": False,
                            "authenticated": False,
                            "needs2fa": True,
                            "hasCredentials": True,
                            "authFile": auth_file,
                            "error": str(v_exc),
                        }
                else:
                    return {
                        "ok": True,
                        "authenticated": False,
                        "needs2fa": True,
                        "hasCredentials": True,
                        "authFile": auth_file,
                    }
            else:
                return {
                    "ok": False,
                    "authenticated": False,
                    "needs2fa": False,
                    "hasCredentials": True,
                    "authFile": auth_file,
                    "error": str(exc),
                }

        try:
            await blink.refresh(force=True)
        except Exception:
            # Start success is enough to consider auth valid.
            pass

        try:
            await blink.save(auth_file)
        except Exception as exc:
            return {
                "ok": False,
                "authenticated": True,
                "needs2fa": False,
                "hasCredentials": True,
                "authFile": auth_file,
                "error": f"auth succeeded but failed to save auth file: {exc}",
            }

        return {
            "ok": True,
            "authenticated": True,
            "needs2fa": False,
            "hasCredentials": True,
            "authFile": auth_file,
        }


async def _main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    auth_file = os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()
    username = os.getenv("BLINK_USERNAME", "").strip()
    password = os.getenv("BLINK_PASSWORD", "").strip()

    if cmd == "status":
        _print(await _attempt(auth_file, username, password, ""))
        return

    if cmd == "verify-2fa":
        code = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
        if not code:
            _print({"ok": False, "error": "2fa code required"})
            return
        _print(await _attempt(auth_file, username, password, code))
        return

    _print({"ok": False, "error": f"unknown command: {cmd}"})


if __name__ == "__main__":
    asyncio.run(_main())
