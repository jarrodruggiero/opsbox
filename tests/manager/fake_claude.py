#!/usr/bin/env python3
"""A fake `claude` CLI for tests, using the standard library only."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

# State lives in CLAUDE_CONFIG_DIR if set, else FAKE_CLAUDE_HOME.
def _state_dir() -> str:
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    if d is None:
        d = os.environ.get("FAKE_CLAUDE_HOME", "/tmp/fake_claude")
    return d


def cmd_auth_login() -> int:
    """Simulate `claude auth login`."""
    state_dir = _state_dir()
    print(
        "If the browser didn't open, visit: \x1b]8;;https://claude.com/cai/oauth/authorize?code=true&client_id=fake&state=abc\x1b\\https://claude.com/cai/oauth/authorize?code=true&client_id=fake&state=abc\x1b]8;;\x1b\\",
        flush=True,
    )
    print("Paste code here if prompted > ", end="", flush=True)
    line = sys.stdin.readline()
    if line.strip() == "good-code-123":
        signed_in_path = os.path.join(state_dir, "signed-in")
        with open(signed_in_path, "w") as f:
            f.write(os.environ.get("FAKE_CLAUDE_EMAIL", "me@example.com"))
        print("Logged in")
        return 0
    else:
        print("Invalid code")
        return 1


def cmd_auth_status() -> int:
    """Simulate `claude auth status`."""
    state_dir = _state_dir()
    signed_in_path = os.path.join(state_dir, "signed-in")
    if os.path.exists(signed_in_path):
        with open(signed_in_path) as f:
            email = f.read().strip()
        print(
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "email": email,
                    "orgId": "org-1",
                    "orgName": "Org",
                    "subscriptionType": "max",
                    "apiKeySource": os.environ.get("FAKE_API_KEY_SOURCE", "none"),
                }
            )
        )
        return 0
    else:
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        return 1


def cmd_auth_logout() -> int:
    """Simulate `claude auth logout`."""
    state_dir = _state_dir()
    # Logged outside the state dir, so a test can see a logout even after the
    # directory itself has been deleted.
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(f"logout {state_dir}\n")
    signed_in_path = os.path.join(state_dir, "signed-in")
    if os.path.exists(signed_in_path):
        os.remove(signed_in_path)
    return 0


def repaint_forever(period: float) -> int:
    """Draw the real CLI's status block as it does (2.1.308), every *period* seconds.

    Every piece is a write of its own: cursor up, erase below, then each line.
    SIGTERM gets its last words the same way, and they move relative to the
    cursor, so anything else printed in the meantime throws them off.
    FAKE_RC_REPAINT_CHANGES=1 changes a line each time.
    """
    changes = os.environ.get("FAKE_RC_REPAINT_CHANGES") == "1"
    drawn = 0
    tick = 0

    def write(text: str) -> None:
        os.write(1, text.encode())

    def clear() -> None:
        nonlocal drawn
        if drawn:
            write(f"\x1b[{drawn}A")
            write("\x1b[J")
            drawn = 0

    def draw() -> None:
        nonlocal drawn
        clear()
        lines = [
            "\u00b7\u2714\ufe0e\u00b7 Connected \u00b7 workspace",
            f"    Capacity: 1/32 \u00b7 tick {tick if changes else 0}",
            "    \x1b]8;;https://claude.ai/code/session\x07opsbox-iterative-nebula\x1b]8;;\x07",
            "",
            "space to show QR code",
        ]
        for line in lines:
            write(line + "\n")
            drawn += 1

    def last_words(signum, frame) -> None:
        clear()
        write("Shutting down 1 active session(s)\n")
        draw()
        time.sleep(0.1)
        clear()
        write("Environment preserved.\n")
        draw()
        clear()
        os._exit(0)

    signal.signal(signal.SIGTERM, last_words)
    while True:
        draw()
        tick += 1
        time.sleep(period)


def cmd_remote_control() -> int:
    """Simulate `claude remote-control`.

    If FAKE_TRUST=1 and <state>/trusted does not exist, ask the user to
    trust the directory. If FAKE_ENABLE=1, ask for the one-time consent the
    same way. Then print a connected message and the env
    variable FAKE_SEEN so tests can verify the env file was applied.
    If FAKE_RC_EXIT_AFTER is set, sleep that many seconds and exit 3.
    If FAKE_RC_REPAINT is set, repaint a status block that often (see above).
    If FAKE_RC_LEAVE_BEHIND is set, leave a job that long holding the terminal.
    Otherwise sleep in a loop until killed.
    """
    state_dir = _state_dir()
    trusted_path = os.path.join(state_dir, "trusted")

    if os.environ.get("FAKE_TRUST") == "1" and not os.path.exists(trusted_path):
        print("Trust /home/opsbox/workspace? [y/N] ", end="", flush=True)
        line = sys.stdin.readline()
        if line.strip() == "y":
            with open(trusted_path, "w") as f:
                f.write("1")
        else:
            print("Not trusted")
            return 1

    # The real CLI's one-time consent, word for word (2.1.311): "y" saves
    # remoteDialogSeen, anything else exits 0.
    seen_path = os.path.join(state_dir, "remote-dialog-seen")
    if os.environ.get("FAKE_ENABLE") == "1" and not os.path.exists(seen_path):
        print("Enable Remote Control? (y/n) ", end="", flush=True)
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            return 0
        with open(seen_path, "w") as f:
            f.write("1")

    print("Remote Control connected", flush=True)
    print(f"env FAKE_SEEN={os.environ.get('FAKE_SEEN', '')}", flush=True)
    print(f"env TERM={os.environ.get('TERM', '')}", flush=True)

    leave_behind = os.environ.get("FAKE_RC_LEAVE_BEHIND")
    if leave_behind is not None:
        subprocess.Popen(["sleep", leave_behind], start_new_session=True)

    repaint = os.environ.get("FAKE_RC_REPAINT")
    if repaint is not None:
        return repaint_forever(float(repaint))

    exit_after = os.environ.get("FAKE_RC_EXIT_AFTER")
    if exit_after is not None:
        time.sleep(float(exit_after))
        return 3

    # Sleep in a loop until killed.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args[:2] == ["auth", "login"]:
        return cmd_auth_login()
    elif args[:2] == ["auth", "status"]:
        return cmd_auth_status()
    elif args[:2] == ["auth", "logout"]:
        return cmd_auth_logout()
    elif args[:2] == ["remote-control"]:
        return cmd_remote_control()
    else:
        return 64


if __name__ == "__main__":
    sys.exit(main())
