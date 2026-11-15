#!/usr/bin/env python3
"""DeepSeek Harness' web UI in the agent's terminal window (sandbox-ai dsh -- web; dsh.py: command), or in the
background beside sandbox-ai's chat (sandbox-ai dsh --browser; dsh_chat.py runs it with --open, its output in a log).

DSH 0.2.1-rc.5 has no chat in the terminal of its own: its shipped profiles are web, headless (one task), acp, sdk and
sdk-minimal (docs/DSH_REVIEW.md); sandbox-ai's chat (dsh_chat.py) is the default. The web UI runs on the sandbox's
loopback, which only the sandbox's own browser reaches (--browser: the same pod, the same loopback; the app's Browser
tab shows it). This wrapper runs `dsh web ...` and:

  - says first what DSH offers (the launcher's NOTICE): with --browser that its web UI opens in the session browser,
    without it how to reach it (start with --browser) or how to give it a task (headless);
  - passes DSH's output through, but never its launch URL with the token as such: the line
    `dsh web: http://127.0.0.1:PORT/?token=...` is shown masked;
  - with --open hands that URL to the session browser: one request on the sandbox's browser socket
    (/sandbox/browser/open.sock, the same the sandbox's xdg-open uses), which the launcher answers for exactly this
    URL by opening it in the session browser (approvals.browser_reply, dsh.session_opener); if that fails, the URL
    is shown whole so that it can be opened there by hand.

The launcher writes this file into the session directory; it runs read-only as /etc/dsh/web.py. Standard library
only: it runs in the agent's container with the image's python3.
"""
import json
import re
import signal
import socket
import subprocess
import sys
import threading

SOCKET = "/sandbox/browser/open.sock"  # runtime.BROWSER_SOCKET
LINE = re.compile(r"dsh web: (http://127\.0\.0\.1:[0-9]{1,5}/\?token=[A-Za-z0-9_-]{16,128})\s*")
TOKEN = re.compile(r"(\?token=)[A-Za-z0-9_-]+")
PREFIX = "sandbox-ai: "


def mask(text):
    """TEXT with the value of every ?token= masked."""
    return TOKEN.sub(r"\1***", text)


def request_open(url, socket_path=SOCKET, timeout=15):
    """(ok, message): URL handed to the launcher on SOCKET_PATH, its answer."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall((json.dumps({"url": url}) + "\n").encode())
            answer = json.loads(client.makefile("r").readline() or "{}")
    except (OSError, ValueError, RecursionError) as exc:
        return False, f"sandbox-ai: the launcher was not reached ({exc})"
    if not isinstance(answer, dict):
        return False, "sandbox-ai: no answer"
    return bool(answer.get("ok")), str(answer.get("message") or "")[:300]


class Output:
    """Lines to the terminal, one at a time (the reader and the hand-over thread both write)."""

    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()

    def line(self, text):
        with self.lock:
            self.stream.write((text.rstrip("\n") + "\n").encode("utf-8", "replace"))
            self.stream.flush()


def hand_over(url, out, socket_path):
    ok, message = request_open(url, socket_path)
    if ok:
        out.line(PREFIX + "DeepSeek Harness' web UI is open in the session browser (the app's Browser tab)")
    else:
        out.line(PREFIX + f"not opened in the session browser ({message.removeprefix(PREFIX)}); open this URL "
                 f"there yourself (the app's Browser tab): {url}")


def parse(argv):
    """(notice, open, command) from [--notice TEXT] [--open] [--socket PATH] -- COMMAND..."""
    notice, opens, socket_path, rest = None, False, SOCKET, list(argv)
    while rest and rest[0] != "--":
        option = rest.pop(0)
        if option == "--notice" and rest:
            notice = rest.pop(0)
        elif option == "--socket" and rest:
            socket_path = rest.pop(0)
        elif option == "--open":
            opens = True
        else:
            raise SystemExit(f"{PREFIX}dsh_web.py: unknown option {option!r}")
    if not rest[1:]:
        raise SystemExit(f"{PREFIX}dsh_web.py: no command after --")
    return notice, opens, socket_path, rest[1:]


def main(argv=None, stream=None, popen=subprocess.Popen):
    notice, opens, socket_path, command = parse(sys.argv[1:] if argv is None else argv)
    out = Output(stream if stream is not None else sys.stdout.buffer)
    if notice:
        out.line(PREFIX + notice)
    try:
        child = popen(command, stdout=subprocess.PIPE)
    except OSError as exc:
        out.line(f"{PREFIX}{command[0]} did not start: {exc}")
        return 127

    def forward(number, _frame):
        if child.poll() is None:
            child.send_signal(number)
    # Ctrl-C reaches DSH from the terminal itself (its process group): this one only stays to pass on its output.
    signal.signal(signal.SIGINT, lambda *_: None)
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, forward)
    handed = False
    for raw in child.stdout:
        text = raw.decode("utf-8", "replace")
        found = LINE.fullmatch(text.rstrip("\r\n"))
        if found is None:
            out.line(mask(text))
            continue
        shown = mask(text.rstrip("\r\n"))
        if not opens:
            out.line(shown + " (reachable only inside the sandbox)")
            if notice:
                out.line(PREFIX + notice)
        elif not handed:
            handed = True
            out.line(shown + " (sandbox-ai: handed to the session browser)")
            threading.Thread(target=hand_over, args=(found.group(1), out, socket_path), daemon=True).start()
        else:
            out.line(shown)
    status = child.wait()
    return 128 - status if status < 0 else status


if __name__ == "__main__":
    sys.exit(main())
