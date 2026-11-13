#!/usr/bin/env python3
"""What a start does before its agent appears, said as it happens: one line per step on standard error
(`agent-sandbox: starting the MCP server fetch…`), the time of a step that took more than SLOW seconds, a final
`agent-sandbox: ready in N s`; and the output of a Podman build or pull in a bounded, readable form (its steps as
`building the image claude (step 4/12): RUN ...`, a few lines of each step's own output, the whole end of it when it
fails). Nothing secret is ever printed: tokens, keys and passwords in a line are masked (scrub).

  python3 progress.py podman [PODMAN OPTIONS] SUBCOMMAND ...   podman, its build and pull output bounded (every other
                                                              subcommand runs as it is: its output untouched)

Standard library only.
"""
import collections
import os
import re
import subprocess
import sys
import time

PREFIX = "agent-sandbox"
SLOW = 5  # seconds: a step that took longer says how long
LINE = 150  # characters of a line of a command's output, at most
STEP_LINES = 3  # lines of a build step's own output shown (the rest counted)
TAIL = 40  # lines of the output kept, shown whole when the command fails

# Masked wherever they appear: NAME=VALUE / NAME: VALUE of a credential, and tokens of the usual forms.
SECRET_SETTING = re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|"
                            r"credential|private[_-]?key)[A-Za-z0-9_.-]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
SECRET_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|"
                          r"sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
                          r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})")
URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+@")


def scrub(text, secrets=()):
    """TEXT with every value of SECRETS and whatever looks like a credential masked."""
    text = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    text = URL_PASSWORD.sub(r"\1***@", text)
    text = SECRET_SETTING.sub(lambda m: m[1] + m[2] + "***", text)
    return SECRET_TOKEN.sub("***", text)


def printable(text):
    """TEXT safe for a terminal: control, bidi and other non-printable characters escaped."""
    return "".join(c if c == "\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text))


def shown(text, limit=LINE):
    text = printable(scrub(text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def seconds(value):
    return f"{value:.0f} s" if value >= 10 else f"{value:.1f} s"


class Progress:
    """The steps of one start (or one command): step() says the next one (and how long the last one took, when that
    was more than SLOW seconds), ready() the end. OUT: where the lines go (default standard error)."""

    def __init__(self, out=None, clock=time.monotonic, prefix=PREFIX, enabled=True):
        self.out, self.clock, self.prefix, self.enabled = out, clock, prefix, enabled
        self.begun = clock()
        self.current, self.since, self.said = None, None, 0

    def say(self, text):
        if not self.enabled:
            return
        line = f"{self.prefix}: {text}"
        if self.out is not None:
            self.out(line)
        else:
            print(line, file=sys.stderr, flush=True)
        self.said += 1

    def finish(self):
        """The step that runs is over: how long it took, when that was more than SLOW seconds."""
        if self.current is not None:
            took = self.clock() - self.since
            if took > SLOW:
                self.say(f"{self.current}: done in {seconds(took)}")
        self.current = None

    def step(self, text):
        self.finish()
        self.current, self.since = shown(text, 200), self.clock()
        self.say(f"{self.current}…")

    def ready(self, what="ready"):
        """The end: "ready in N s" (said when a step was said or it took a second or more)."""
        self.finish()
        took = self.clock() - self.begun
        if self.said or took >= 1:
            self.say(f"{what} in {seconds(took)}")


class Output:
    """A Podman build's or pull's output, line by line, as Progress lines: what WHAT stands for ("building the image
    claude", "pulling the image of fetch"); its steps, at most STEP_LINES lines of each step's output (the rest
    counted), the layers of a pull counted; the last TAIL lines kept for a failure."""

    STEP = re.compile(r"^(?:\[(\d+)/(\d+)\]\s+)?STEP\s+(\d+)/(\d+):\s*(.*)$")

    def __init__(self, what, progress):
        self.what, self.progress = what, progress
        self.tail = collections.deque(maxlen=TAIL)
        self.lines_of_step, self.hidden, self.layers, self.cached = 0, 0, 0, 0

    def line(self, raw):
        text = raw.rstrip("\r\n")
        if not text.strip():
            return
        self.tail.append(text)
        step = self.STEP.match(text.strip())
        if step:
            self.flush_hidden()
            stage = f"stage {step[1]}/{step[2]}, " if step[1] else ""
            self.progress.step(f"{self.what} ({stage}step {step[3]}/{step[4]}): {shown(step[5], 90)}")
            self.lines_of_step = 0
            return
        stripped = text.strip()
        if stripped.startswith(("Copying blob", "Copying config")):
            self.layers += stripped.startswith("Copying blob")
            if stripped.startswith("Copying config"):
                self.progress.say(f"{self.what}: {self.layers} layer{'s' if self.layers != 1 else ''} fetched")
            return
        if stripped.startswith(("Using cache", "--> ")) or re.fullmatch(r"[0-9a-f]{64}", stripped):
            self.cached += "Using cache" in stripped
            return
        if stripped.startswith("COMMIT"):
            self.flush_hidden()
            self.progress.step(f"{self.what}: saving the image")
            return
        if stripped.startswith(("Trying to pull", "Getting image source signatures", "Writing manifest",
                                "Storing signatures", "Successfully tagged")):
            self.progress.say(f"{self.what}: {shown(stripped, 120)}")
            return
        if self.lines_of_step < STEP_LINES:
            self.lines_of_step += 1
            self.progress.say(f"  {shown(stripped)}")
        else:
            self.hidden += 1

    def flush_hidden(self):
        if self.hidden:
            self.progress.say(f"  (… {self.hidden} more line{'s' if self.hidden != 1 else ''})")
        self.hidden = 0

    def end(self, status):
        self.flush_hidden()
        if status != 0:
            self.progress.say(f"{self.what} failed (exit status {status}); its last lines:")
            for text in self.tail:
                self.progress.say(f"  {shown(text, 400)}")
        else:
            self.progress.finish()


def stream(argv, what, progress=None, popen=subprocess.Popen, env=None):
    """Run ARGV (a podman build or pull), its output (both streams) shown as Output does; its exit status."""
    progress = progress or Progress()
    output = Output(what, progress)
    began = progress.clock()
    progress.step(what)
    process = popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    text=True, errors="replace", bufsize=1, env=env)
    try:
        for raw in process.stdout:
            output.line(raw)
    finally:
        process.stdout.close()
    status = process.wait()
    output.end(status)
    if status == 0:
        took = progress.clock() - began
        progress.say(f"{what}: done in {seconds(took)}" + (f" ({output.cached} step{'s' if output.cached != 1 else ''} from "
                                                          "the cache)" if output.cached else ""))
    return status


# podman's options that take a value as the next word (before its subcommand)
PODMAN_VALUE_OPTIONS = {"--url", "--connection", "-c", "--identity", "--root", "--runroot", "--storage-driver",
                        "--cgroup-manager", "--log-level", "--events-backend", "--network-cmd-path", "--tmpdir",
                        "--runtime", "--module"}


def subcommand(podman):
    """(index, word) of the subcommand of the podman command line PODMAN (after its own options), or (None, None)."""
    index = 1
    while index < len(podman):
        word = podman[index]
        if word.startswith("-"):
            index += 2 if word in PODMAN_VALUE_OPTIONS else 1
            continue
        return index, word
    return None, None


def what_of(podman, index, word):
    """What a build or pull is about, for its lines: the image it tags or pulls."""
    rest = podman[index + 1:]
    if word == "pull":
        image = next((w for w in rest if not w.startswith("-")), "")
        return f"pulling the image {image}".strip()
    for option in ("--tag", "-t"):
        if option in rest and rest.index(option) + 1 < len(rest):
            return f"building the image {rest[rest.index(option) + 1]}"
        found = next((w.split("=", 1)[1] for w in rest if w.startswith(option + "=")), None)
        if found:
            return f"building the image {found}"
    return "building an image"


def main(argv):
    if not argv or argv[0] != "podman" or len(argv) < 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    podman = argv[1:]
    index, word = subcommand(podman)
    if word not in ("build", "pull") or os.environ.get("SANDBOX_AI_PODMAN_OUTPUT") == "full":
        os.execvp(podman[0], podman)
    return stream(podman, what_of(podman, index, word), Progress(prefix=os.environ.get("SANDBOX_AI_PROGRESS_PREFIX",
                                                                                         "sandbox-ai build")))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
