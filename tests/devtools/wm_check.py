#!/usr/bin/env python3
"""Run a program on a virtual screen with a real window manager (spectrwm, the tiling window manager the app is used
with) and check that every top-level window it maps is managed: not override-redirect (a window that bypasses the
window manager draws over every workspace and cannot be moved: the app's floating properties panel did, 0.1.93),
and with the window manager's WM_STATE. Standard library; needs Xvfb, the window manager, xwininfo and xprop
(x11-utils), optionally xwd for a screenshot - the dev image variant has them (sandbox-ai images recipe dev).

  python3 tests/devtools/wm_check.py [--display :97] [--screen 1600x900x24] [--wm spectrwm] [--settle 6]
                                     [--screenshot FILE] [--json] -- COMMAND [ARG...]

Prints a JSON report ({"windows": [...], "unmanaged": [...]}) and ends with 1 when a window of COMMAND (by
_NET_WM_PID, its process and its children) is unmanaged, 3 when COMMAND mapped no window at all (it ended, or
never showed one: nothing was checked), 2 when the display or the window manager did not start.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

CHILD = re.compile(r"^\s+(0x[0-9a-f]+)\s+(.*?)\s*$")


def parse_children(text):
    """[window id] of `xwininfo -root -children`."""
    return [m.group(1) for m in map(CHILD.match, text.splitlines()) if m and not m.group(2).startswith("child")]


def parse_xwininfo(text):
    """{"map_state", "override_redirect"} of `xwininfo -id W -all`."""
    found = {"map_state": None, "override_redirect": None}
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "Map State":
            found["map_state"] = value.strip()
        elif key == "Override Redirect State":
            found["override_redirect"] = value.strip().lower() == "yes"
    return found


def parse_xprop(text):
    """{"pid", "wm_state", "name"} of `xprop -id W _NET_WM_PID WM_STATE WM_NAME`."""
    found = {"pid": None, "wm_state": None, "name": None}
    for line in text.splitlines():
        if line.startswith("_NET_WM_PID(CARDINAL) = "):
            value = line.split("=", 1)[1].strip()
            found["pid"] = int(value) if value.isdigit() else None
        elif line.strip().startswith("window state:"):
            found["wm_state"] = line.split(":", 1)[1].strip()
        elif line.startswith("WM_NAME("):
            found["name"] = line.split("=", 1)[1].strip().strip('"')
    return found


def descendants(pid, proc=Path("/proc")):
    """PID and every process below it (by the parent PIDs in /proc)."""
    parents = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
                parents.setdefault(int(stat.rsplit(")", 1)[1].split()[1]), []).append(int(entry.name))
            except (OSError, IndexError, ValueError):
                continue
    found, todo = set(), [pid]
    while todo:
        current = todo.pop()
        if current not in found:
            found.add(current)
            todo += parents.get(current, [])
    return found


def verdict(windows, pids):
    """The windows of PIDS that are mapped and unmanaged: override-redirect, or without WM_STATE."""
    return [w for w in windows if w["pid"] in pids and w["map_state"] == "IsViewable"
            and (w["override_redirect"] or not w["wm_state"])]


def outcome(report):
    """The exit status of REPORT: 1 with an unmanaged window, 3 without any mapped window of the program."""
    if report["unmanaged"]:
        return 1
    if not any(w["map_state"] == "IsViewable" for w in report["windows"]):
        return 3
    return 0


def inspect(display, run=subprocess.run):
    env = {**os.environ, "DISPLAY": display}
    listing = run(["xwininfo", "-root", "-children"], env=env, capture_output=True, text=True, timeout=30).stdout
    windows = []
    for window in parse_children(listing):
        info = parse_xwininfo(run(["xwininfo", "-id", window, "-all"], env=env, capture_output=True, text=True,
                                  timeout=30).stdout)
        props = parse_xprop(run(["xprop", "-id", window, "_NET_WM_PID", "WM_STATE", "WM_NAME"], env=env,
                                capture_output=True, text=True, timeout=30).stdout)
        windows.append({"id": window, **info, **props})
    return windows


def wait_for(path, seconds):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def main(argv=None):
    p = argparse.ArgumentParser(prog="wm_check.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--display", default=":97")
    p.add_argument("--screen", default="1600x900x24")
    p.add_argument("--wm", default="spectrwm")
    p.add_argument("--settle", type=float, default=6.0, help="seconds the program gets to open its windows")
    p.add_argument("--screenshot", metavar="FILE")
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        p.error("a command to run after --")
    for tool in ("Xvfb", a.wm, "xwininfo", "xprop"):
        if not shutil.which(tool):
            print(json.dumps({"error": f"{tool} is missing"}))
            return 2
    started = []
    try:
        socket = f"/tmp/.X11-unix/X{a.display.lstrip(':').split('.')[0]}"
        started.append(subprocess.Popen(["Xvfb", a.display, "-screen", "0", a.screen, "-nolisten", "tcp"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not wait_for(socket, 15):
            print(json.dumps({"error": "Xvfb did not start"}))
            return 2
        env = {**os.environ, "DISPLAY": a.display}
        started.append(subprocess.Popen([a.wm], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.5)
        if started[-1].poll() is not None:
            print(json.dumps({"error": f"{a.wm} ended at once"}))
            return 2
        program = subprocess.Popen(command, env=env, start_new_session=True)
        started.append(program)
        time.sleep(a.settle)
        windows = inspect(a.display)
        pids = descendants(program.pid)
        unmanaged = verdict(windows, pids)
        if a.screenshot and shutil.which("xwd"):
            subprocess.run(["xwd", "-root", "-silent", "-out", a.screenshot], env=env, timeout=30, check=False)
        report = {"windows": [w for w in windows if w["pid"] in pids], "unmanaged": unmanaged,
                  "program_running": program.poll() is None}
        print(json.dumps(report, indent=1))
        return outcome(report)
    finally:
        for index, process in reversed(list(enumerate(started))):
            if process.poll() is not None:
                continue
            try:
                if index == 2:  # the program: its whole process group (it may have started others)
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()


if __name__ == "__main__":
    sys.exit(main())
