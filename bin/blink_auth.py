#!/usr/bin/env python3
"""Blink auth/state helper backed by SQLite.

Commands:
  status
  save-credentials <username> <password>
  verify-2fa <code>
  test-auth
  pause-fetch
  resume-fetch
"""

import asyncio
import getpass
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import aiohttp
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _print(obj):
    print(json.dumps(obj))


def _db_path():
    return os.getenv("BLINK_DB_FILE", "/app/config/blink-bridge.db").strip()


def _auth_file():
    return os.getenv("BLINK_AUTH_FILE", "/app/config/blink-auth.json").strip()


def _connect():
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
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


def _status(conn):
    r = _row(conn)
    return {
        "ok": True,
        "auth_file": _auth_file(),
        "db_file": _db_path(),
        "authenticated": bool(r["authenticated"]),
        "needs_credentials": bool(r["needs_credentials"]),
        "needs_2fa": bool(r["needs_2fa"]),
        "locked_error": bool(r["locked_error"]),
        "paused_fetch": bool(r["paused_fetch"]),
        "last_error": r["last_error"],
        "last_attempt_at": r["last_attempt_at"],
        "next_allowed_attempt_at": r["next_allowed_attempt_at"],
        "has_credentials": bool(r["username"] and r["password"]),
    }


def _update(conn, **fields):
    fields["updated_at"] = _utc_now()
    keys = sorted(fields.keys())
    sets = ", ".join(f"{k}=?" for k in keys)
    vals = [fields[k] for k in keys]
    conn.execute(f"UPDATE auth_state SET {sets} WHERE id=1", vals)
    conn.commit()


def _err_text(err: Exception) -> str:
    msg = str(err).strip()
    if msg:
        return msg
    return repr(err)


def _needs_2fa(err: Exception) -> bool:
    msg = _err_text(err).lower()
    # blinkpy may raise names like BlinkTwoFARequiredError()
    return (
        "2fa" in msg
        or "twofa" in msg
        or "auth key" in msg
        or "verification" in msg
        or "required" in msg and "blink" in msg and "fa" in msg
    )


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


async def _submit_2fa(auth, blink, code):
    # blinkpy API differs by version; try known call patterns.
    errors = []

    for fn in (
        getattr(auth, "send_auth_key", None),
        getattr(getattr(blink, "auth", None), "send_auth_key", None),
        getattr(blink, "send_auth_key", None),
    ):
        if fn is None:
            continue

        # Most common signatures are (blink, code) or (code)
        try:
            await fn(blink, code)
            return
        except TypeError as e:
            errors.append(repr(e))
        except Exception as e:
            errors.append(repr(e))
            continue

        try:
            await fn(code)
            return
        except Exception as e:
            errors.append(repr(e))

    raise RuntimeError("unable to submit 2FA code; compatible send_auth_key API not found: " + "; ".join(errors))


async def _cleanup_blink_sessions(blink, session):
    # Some blinkpy versions create extra ClientSession objects that are not
    # guaranteed to be cleaned up by our outer context manager.
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


async def _attempt_auth(conn, twofa_code=""):
    r = _row(conn)
    username = (r["username"] or "").strip()
    password = (r["password"] or "").strip()
    now = _utc_now()

    if not username or not password:
        _update(
            conn,
            authenticated=0,
            needs_credentials=1,
            needs_2fa=0,
            locked_error=0,
            paused_fetch=1,
            last_error="missing Blink credentials",
            last_attempt_at=now,
            next_allowed_attempt_at=now,
        )
        return {"ok": False, "error": "missing Blink credentials"}

    _update(conn, last_attempt_at=now, next_allowed_attempt_at=now, last_error=None)

    auth = Auth({"username": username, "password": password}, no_prompt=True)
    session = aiohttp.ClientSession()
    blink = await _new_blink(session, auth)
    try:
        try:
            await blink.start()
        except Exception as exc:
            if _needs_2fa(exc):
                if not twofa_code:
                    _update(
                        conn,
                        authenticated=0,
                        needs_credentials=0,
                        needs_2fa=1,
                        locked_error=0,
                        paused_fetch=1,
                        last_error="Blink 2FA required",
                    )
                    return {"ok": False, "error": "Blink 2FA required", "needs_2fa": True}

                try:
                    await _submit_2fa(auth, blink, twofa_code)
                    if hasattr(blink, "setup_post_verify"):
                        await blink.setup_post_verify()
                except Exception as v_exc:
                    _update(
                        conn,
                        authenticated=0,
                        needs_credentials=0,
                        needs_2fa=1,
                        locked_error=1,
                        paused_fetch=1,
                        last_error=_err_text(v_exc),
                    )
                    return {"ok": False, "error": _err_text(v_exc), "needs_2fa": True}
            else:
                _update(
                    conn,
                    authenticated=0,
                    needs_credentials=0,
                    needs_2fa=0,
                    locked_error=1,
                    paused_fetch=1,
                    last_error=_err_text(exc),
                )
                return {"ok": False, "error": _err_text(exc)}

        try:
            await blink.refresh(force=True)
        except Exception:
            pass

        try:
            await blink.save(_auth_file())
        except Exception as exc:
            _update(
                conn,
                authenticated=0,
                needs_credentials=0,
                needs_2fa=0,
                locked_error=1,
                paused_fetch=1,
                last_error=f"auth succeeded but failed to save auth file: {exc}",
            )
            return {"ok": False, "error": f"failed to save auth file: {exc}"}
    finally:
        await _cleanup_blink_sessions(blink, session)

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
    return {"ok": True}


async def _interactive_login(conn):
    print("Blink interactive login")
    username = input("Blink username/email: ").strip()
    password = getpass.getpass("Blink password: ").strip()
    if not username or not password:
        _print({"ok": False, "error": "username and password are required"})
        return

    _update(
        conn,
        username=username,
        password=password,
        authenticated=0,
        needs_credentials=0,
        needs_2fa=0,
        locked_error=0,
        paused_fetch=1,
        last_error=None,
        next_allowed_attempt_at=_utc_now(),
    )

    now = _utc_now()
    _update(conn, last_attempt_at=now, next_allowed_attempt_at=now, last_error=None)

    auth = Auth({"username": username, "password": password}, no_prompt=True)
    session = aiohttp.ClientSession()
    blink = await _new_blink(session, auth)
    try:
        try:
            await blink.start()
        except Exception as exc:
            if _needs_2fa(exc):
                _update(
                    conn,
                    authenticated=0,
                    needs_credentials=0,
                    needs_2fa=1,
                    locked_error=0,
                    paused_fetch=1,
                    last_error="Blink 2FA required",
                )

                # Prefer blinkpy's interactive flow when available.
                prompt_fn = getattr(blink, "prompt_2fa", None)
                if callable(prompt_fn):
                    try:
                        await prompt_fn()
                        if hasattr(blink, "setup_post_verify"):
                            await blink.setup_post_verify()
                    except Exception as prompt_exc:
                        # Fallback to manual code entry for blinkpy versions where
                        # prompt_2fa is unavailable/broken in this context.
                        code = input("Blink 2FA code (press Enter to cancel): ").strip()
                        if not code:
                            payload = _status(conn)
                            payload.update({
                                "ok": False,
                                "error": f"2FA prompt failed ({_err_text(prompt_exc)}), and no code entered",
                            })
                            _print(payload)
                            return
                        try:
                            await _submit_2fa(auth, blink, code)
                            if hasattr(blink, "setup_post_verify"):
                                await blink.setup_post_verify()
                        except Exception as v_exc:
                            _update(
                                conn,
                                authenticated=0,
                                needs_credentials=0,
                                needs_2fa=1,
                                locked_error=1,
                                paused_fetch=1,
                                last_error=_err_text(v_exc),
                            )
                            payload = _status(conn)
                            payload.update({"ok": False, "error": _err_text(v_exc)})
                            _print(payload)
                            return
                else:
                    code = input("Blink 2FA code (press Enter to cancel): ").strip()
                    if not code:
                        payload = _status(conn)
                        payload.update({"ok": False, "error": "2FA code required to continue"})
                        _print(payload)
                        return
                    try:
                        await _submit_2fa(auth, blink, code)
                        if hasattr(blink, "setup_post_verify"):
                            await blink.setup_post_verify()
                    except Exception as v_exc:
                        _update(
                            conn,
                            authenticated=0,
                            needs_credentials=0,
                            needs_2fa=1,
                            locked_error=1,
                            paused_fetch=1,
                            last_error=_err_text(v_exc),
                        )
                        payload = _status(conn)
                        payload.update({"ok": False, "error": _err_text(v_exc)})
                        _print(payload)
                        return
            else:
                _update(
                    conn,
                    authenticated=0,
                    needs_credentials=0,
                    needs_2fa=0,
                    locked_error=1,
                    paused_fetch=1,
                    last_error=_err_text(exc),
                )
                payload = _status(conn)
                payload.update({"ok": False, "error": _err_text(exc)})
                _print(payload)
                return

        try:
            await blink.refresh(force=True)
        except Exception:
            pass

        try:
            await blink.save(_auth_file())
        except Exception as save_exc:
            _update(
                conn,
                authenticated=0,
                needs_credentials=0,
                needs_2fa=0,
                locked_error=1,
                paused_fetch=1,
                last_error=f"auth succeeded but failed to save auth file: {save_exc}",
            )
            payload = _status(conn)
            payload.update({"ok": False, "error": f"failed to save auth file: {save_exc}"})
            _print(payload)
            return
    finally:
        await _cleanup_blink_sessions(blink, session)

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
    _print(_status(conn))


async def _main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    conn = _connect()

    if cmd == "status":
        _print(_status(conn))
        return

    if cmd == "login":
        await _interactive_login(conn)
        return

    if cmd == "save-credentials":
        username = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
        password = (sys.argv[3] if len(sys.argv) > 3 else "").strip()
        if not username or not password:
            _print({"ok": False, "error": "username and password are required"})
            return
        _update(
            conn,
            username=username,
            password=password,
            authenticated=0,
            needs_credentials=0,
            needs_2fa=0,
            locked_error=0,
            paused_fetch=1,
            last_error=None,
            next_allowed_attempt_at=_utc_now(),
        )
        _print(_status(conn))
        return

    if cmd == "verify-2fa":
        code = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
        if not code:
            _print({"ok": False, "error": "2fa code required"})
            return
        result = await _attempt_auth(conn, code)
        payload = _status(conn)
        payload["ok"] = bool(result.get("ok"))
        if result.get("error"):
            payload["error"] = result["error"]
        _print(payload)
        return

    if cmd == "test-auth":
        result = await _attempt_auth(conn, "")
        payload = _status(conn)
        payload["ok"] = bool(result.get("ok"))
        if result.get("error"):
            payload["error"] = result["error"]
        _print(payload)
        return

    if cmd == "pause-fetch":
        _update(conn, paused_fetch=1)
        _print(_status(conn))
        return

    if cmd == "resume-fetch":
        s = _status(conn)
        if not s.get("authenticated"):
            _print({
                "ok": False,
                "error": "cannot resume fetching until authentication succeeds",
                **s,
            })
            return
        _update(conn, paused_fetch=0, locked_error=0, last_error=None, next_allowed_attempt_at=None)
        _print(_status(conn))
        return

    _print({"ok": False, "error": f"unknown command: {cmd}"})


if __name__ == "__main__":
    asyncio.run(_main())
