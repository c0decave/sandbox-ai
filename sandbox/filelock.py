#!/usr/bin/env python3
"""One lock for a read-modify-write of one of sandbox-ai's settings files (llm.json, git-logins.json,
publish-logins.json, identities.json): an flock on FILE.lock beside it, so that commands running at the same time
(the CLI, and the app and the web interface, which run it as subprocesses side by side) never lose each other's
change. Standard library only; the modules load it by path."""
import contextlib
import fcntl
import os
from pathlib import Path
import stat
import threading
import time

TIMEOUT = 30.0  # seconds a writer waits for another one
_held = threading.local()


@contextlib.contextmanager
def locked(path, timeout=TIMEOUT):
    """Hold the lock of the settings file PATH: an flock on PATH.lock (0600, this user's, never through a symbolic
    link), taken by every process and thread that reads the file to write it back. Reentrant within a thread; waits
    at most TIMEOUT seconds, then a ValueError. Yields PATH."""
    path = Path(path)
    key = str(path.absolute())
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    if key in held:
        yield path
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"{lock}: not a regular file of yours; remove it")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError(f"{path} is being changed by another command; try again") from None
                time.sleep(0.02)
        held.add(key)
        try:
            yield path
        finally:
            held.discard(key)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
