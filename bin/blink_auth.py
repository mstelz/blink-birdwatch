#!/usr/bin/env python3
"""Simple Blink auth helper (file-based, no DB).

Commands:
  status
  login
"""

import asyncio
import getpass
import json
import os
import sys

import aiohttp
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink


def _auth_file():
    return os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()


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


def _err_text(err: Exception) -> str:
    msg = str(err).strip()
    return msg or repr(err)


def _needs_2fa(err: Exception) -> bool:
    msg = _err_text(err).lower()
    return "2fa" in msg or "twofa" in msg or "auth key" in msg or "verification" in msg


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
    username = input("Blink username/email: ").strip()
    password = getpass.getpass("Blink password: ").strip()
    if not username or not password:
        print(json.dumps({"ok": False, "error": "username and password are required"}))
        return 1

    auth_file = _auth_file()
    creds = _load_json(auth_file, {})
    creds["username"] = username
    creds["password"] = password
    _save_json(auth_file, creds)

    auth = Auth(creds, no_prompt=True)
    session = aiohttp.ClientSession()
    blink = await _new_blink(session, auth)

    try:
        # Prefer native interactive login path when exposed by blinkpy.
        login_fn = getattr(blink, "login", None)
        start_fn = getattr(blink, "start", None)

        if callable(login_fn):
            await login_fn()
        elif callable(start_fn):
            await start_fn()
        else:
            raise RuntimeError("blinkpy object has no login/start method")

        setup_fn = getattr(blink, "setup", None)
        if callable(setup_fn):
            await setup_fn()

        refresh_fn = getattr(blink, "refresh", None)
        if callable(refresh_fn):
            await refresh_fn(force=True)

        await blink.save(auth_file)
        print(json.dumps(_status_payload()))
        return 0
    except Exception as exc:
        if _needs_2fa(exc):
            try:
                prompt_fn = getattr(blink, "prompt_2fa", None)
                if callable(prompt_fn):
                    await prompt_fn()
                else:
                    code = input("Blink 2FA code: ").strip()
                    if not code:
                        raise RuntimeError("2FA code is required")
                    auth.key = code

                if hasattr(blink, "setup_post_verify"):
                    await blink.setup_post_verify()
                if hasattr(blink, "refresh"):
                    await blink.refresh(force=True)
                await blink.save(auth_file)
                print(json.dumps(_status_payload()))
                return 0
            except Exception as twofa_exc:
                print(json.dumps({"ok": False, "error": _err_text(twofa_exc)}))
                return 1

        print(json.dumps({"ok": False, "error": _err_text(exc)}))
        return 1
    finally:
        await _cleanup(blink, session, auth)


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
