#!/usr/bin/env python3
"""PreToolUse hook (Bash): block `git push` outside :05-:50 past the hour,
US Eastern. Every push to master redeploys production.

Reads the hook JSON on stdin and looks only at tool_input.command. Exit 2
(with a message on stderr) blocks the command; exit 0 lets it run. Anything
that isn't a git push, or input it can't parse, is always let through.

Test override: PUSH_WINDOW_NOW="HH:MM" (Eastern) replaces the clock.
"""
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# `git push`, also with options before the subcommand: `git -C "dir" push`.
PUSH = re.compile(r"""\bgit(\s+-{1,2}[\w-]+(\s+("[^"]*"|'[^']*'|[^\s"'-]\S*))?)*\s+push\b""")
OPEN_FROM, OPEN_TO = 5, 50


def now_hh_mm():
    fake = os.environ.get("PUSH_WINDOW_NOW")
    if fake:
        hh, mm = fake.split(":")
        return int(hh), int(mm)
    t = datetime.now(ZoneInfo("America/New_York"))
    return t.hour, t.minute


def main():
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
    except Exception:
        return 0
    if not isinstance(command, str) or not PUSH.search(command):
        return 0
    hh, mm = now_hh_mm()
    if OPEN_FROM <= mm <= OPEN_TO:
        return 0
    next_hh = hh if mm < OPEN_FROM else (hh + 1) % 24
    print(
        f"Push window is :05-:50; wait until {next_hh:02d}:05 Eastern "
        f"(it's {hh:02d}:{mm:02d} now). Each push redeploys production.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
