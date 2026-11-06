#!/usr/bin/env python3
"""A fake `claude` CLI for tests, using the standard library only."""
from __future__ import annotations

import json
import os
import sys

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
    signed_in_path = os.path.join(state_dir, "signed-in")
    if os.path.exists(signed_in_path):
        os.remove(signed_in_path)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args[:2] == ["auth", "login"]:
        return cmd_auth_login()
    elif args[:2] == ["auth", "status"]:
        return cmd_auth_status()
    elif args[:2] == ["auth", "logout"]:
        return cmd_auth_logout()
    else:
        return 64


if __name__ == "__main__":
    sys.exit(main())
