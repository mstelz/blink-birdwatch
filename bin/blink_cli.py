#!/usr/bin/env python3
"""Interactive Blink helper CLI.

Usage:
  blink login
  blink status
  blink pause
  blink resume
  blink test
"""

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
    # Keep login flow in one Python process so 2FA challenge context is preserved.
    proc = subprocess.run([PY, AUTH, "login"])
    if proc.returncode != 0:
        sys.exit(proc.returncode)


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
