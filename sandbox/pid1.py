#!/usr/bin/env python3
"""sandbox-ai's own init: PID 1 of the containers sandbox-ai runs from its own images (the browser's, Cairn's).

    python3 -I pid1.py PROGRAM [ARGUMENT...]

Podman's --init puts the HOST's catatonit into a container as its PID 1, so a host without that package could not
start them ("lookup init binary: exec: "catatonit": executable file not found in $PATH"). These containers bring
their Python with their image and get this file mounted read-only beside their runtime at every start, so nothing of
the host is needed and no image has to be rebuilt for it. What an init does, and nothing else: it starts PROGRAM as
its one child, passes the signals it gets on to it (PID 1 has no default actions: without this `podman stop` would
wait for its timeout), reaps every process that ends - also those handed to PID 1 when their parent went, which would
stay as zombies otherwise - and ends with PROGRAM's exit status (128 + N for one ended by signal N) as soon as PROGRAM
ends; the kernel then ends what is left in the container.

Standard library only.
"""
import os
import signal
import sys

# Passed on to the program: what ends, interrupts or tells a process something. Never SIGCHLD (ours: a child ended),
# SIGKILL and SIGSTOP (not catchable), nor the faults of this process itself.
FORWARDED = tuple(getattr(signal, name) for name in (
    "SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGWINCH", "SIGCONT", "SIGALRM", "SIGPIPE")
    if hasattr(signal, name))


def exit_status(status):
    """A shell's exit status of a wait status: the program's own, or 128 + N when signal N ended it."""
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1


def main(argv):
    if not argv:
        print("pid1: usage: pid1.py PROGRAM [ARGUMENT...]", file=sys.stderr)
        return 2
    child = [None]

    def forward(number, _frame):
        if child[0] is not None:
            try:
                os.kill(child[0], number)
            except ProcessLookupError:
                pass
    for number in FORWARDED:  # (before the fork: a signal in between is not lost, it reaches the child when it is known)
        signal.signal(number, forward)
    pid = os.fork()
    if pid == 0:
        for number in FORWARDED:
            signal.signal(number, signal.SIG_DFL)
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            print(f"pid1: {argv[0]}: {exc.strerror}", file=sys.stderr)
            os._exit(127 if isinstance(exc, FileNotFoundError) else 126)
    child[0] = pid
    while True:
        try:
            ended, status = os.wait()
        except ChildProcessError:  # (nothing left at all: the program was reaped by someone else)
            return 1
        if ended == pid:
            return exit_status(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
