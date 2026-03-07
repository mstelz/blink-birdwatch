#!/usr/bin/env python3
"""Simple Blink auth helper (file-based).

Commands:
  status
  login
"""

import asyncio
import json
import logging
import os
import sys

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink

# blinkpy moved this exception between versions; support both.
try:
    from blinkpy.auth import BlinkTwoFARequiredError  # type: ignore
except Exception:  # pragma: no cover
    from blinkpy.exceptions import BlinkTwoFARequiredError  # type: ignore


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


async def _patched_oauth_signin(auth, email, password, csrf_token):
    """Wrap blinkpy's oauth_signin to surface 429 rate-limit errors clearly."""
    from blinkpy import api as blink_api
    from blinkpy.api import OAUTH_USER_AGENT, OAUTH_SIGNIN_URL

    headers = {
        "User-Agent": OAUTH_USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://api.oauth.blink.com",
        "Referer": OAUTH_SIGNIN_URL,
    }
    data = {
        "username": email,
        "password": password,
        "csrf-token": csrf_token,
    }
    response = await auth.session.post(
        OAUTH_SIGNIN_URL, headers=headers, data=data, allow_redirects=False
    )
    if response.status == 429:
        body = await response.json(content_type=None)
        cause = body.get("error_cause", "")
        wait = body.get("next_time_in_secs", 0)
        hours = round(wait / 3600, 1)
        msg = body.get("error_description", "rate limit exceeded")
        raise RuntimeError(
            f"Blink login blocked: {msg}"
            + (f" Try again in {hours}h." if hours else "")
            + (f" (cause: {cause})" if cause else "")
        )
    if response.status == 412:
        return "2FA_REQUIRED"
    if response.status in (301, 302, 303, 307, 308):
        return "SUCCESS"
    return None


async def _interactive_login(debug=False):
    if debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                            format="%(levelname)s %(name)s: %(message)s")
    import blinkpy.api as blink_api
    blink_api.oauth_signin = _patched_oauth_signin

    auth_file = _auth_file()
    async with ClientSession() as session:
        blink = Blink(session=session)
        try:
            started = await blink.start()
        except BlinkTwoFARequiredError:
            await blink.prompt_2fa()  # prompts interactively, then calls start() again
            started = True

        if not started:
            raise RuntimeError(
                "Blink login failed — check your username/password, or run with --debug for details"
            )

        await blink.save(auth_file)

    print(json.dumps(_status_payload()))
    return 0


async def _main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()

    if cmd == "status":
        print(json.dumps(_status_payload()))
        return 0

    if cmd == "login":
        debug = "--debug" in sys.argv
        try:
            return await _interactive_login(debug=debug)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc) or repr(exc)}))
            return 1

    print(json.dumps({"ok": False, "error": f"unknown command: {cmd}"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
