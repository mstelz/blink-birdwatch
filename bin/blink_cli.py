#!/usr/bin/env python3
"""Interactive Blink helper CLI.

Usage:
  blink login
  blink status
  blink pause
  blink resume
  blink test
"""

import getpass
import json
import os
import subprocess
import sys

AUTH = "/app/bin/blink_auth.py"
PY = "python3"


def run_auth(*args):
    proc = subprocess.run([PY, AUTH, *args], text=True, capture_output=True)
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError((proc.stderr or "").strip() or "blink_auth returned empty output")
    return json.loads(out)


def print_json(data):
    print(json.dumps(data, indent=2))


def cmd_status():
    print_json(run_auth("status"))


def cmd_pause():
    print_json(run_auth("pause-fetch"))


def cmd_resume():
    print_json(run_auth("resume-fetch"))


def cmd_test():
    print_json(run_auth("test-auth"))


def cmd_login():
    print("Blink interactive login")
    print("- credentials are stored in BLINK_DB_FILE")
    print("- session tokens are stored in BLINK_AUTH_FILE")
    print()

    username = input("Blink username/email: ").strip()
    password = getpass.getpass("Blink password: ").strip()
    if not username or not password:
        print("username/password required", file=sys.stderr)
        sys.exit(1)

    saved = run_auth("save-credentials", username, password)
    if not saved.get("ok"):
        print_json(saved)
        sys.exit(1)

    tested = run_auth("test-auth")
    if tested.get("ok") and tested.get("authenticated"):
        print("\nAuthenticated successfully.")
        resumed = run_auth("resume-fetch")
        print_json(resumed)
        return

    if tested.get("needs_2fa"):
        print("\nBlink requires 2FA.")
        code = input("Enter Blink 2FA code: ").strip()
        if not code:
            print("2FA code required", file=sys.stderr)
            sys.exit(1)
        verified = run_auth("verify-2fa", code)
        if not verified.get("ok") or not verified.get("authenticated"):
            print_json(verified)
            sys.exit(1)
        print("\n2FA verified.")
        resumed = run_auth("resume-fetch")
        print_json(resumed)
        return

    print("\nAuthentication failed.")
    print_json(tested)
    print("\nFetch remains paused for safety. Re-run: blink login")
    sys.exit(1)


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "help").strip().lower()

    if cmd == "login":
        cmd_login()
        return
    if cmd == "status":
        cmd_status()
        return
    if cmd == "pause":
        cmd_pause()
        return
    if cmd == "resume":
        cmd_resume()
        return
    if cmd == "test":
        cmd_test()
        return

    print(__doc__.strip())
    sys.exit(1)


if __name__ == "__main__":
    main()
