"""The newest lines of a log, read from its end: a command that shows the last N records of a JSON-lines log
(activity.jsonl, safeguards.jsonl) reads only as far back as it needs instead of the whole file, which grows for
as long as the log directory is used (every run of the project). Standard library only."""
import json
import os

CHUNK = 1 << 20        # read backwards in pieces of this size
LINE_LIMIT = 4 << 20   # a line longer than this is skipped (never held whole)


DECODER = json.JSONDecoder()


def loads(line):
    """json.loads(LINE), a line of bytes: one that starts with "{" (every record of the logs) is decoded as UTF-8 at
    once - what json.loads makes of it too - without json.loads's look for UTF-16 or UTF-32, a quarter of the time a
    line took. ValueError for a line that is no JSON (or no UTF-8), as json.loads."""
    if line[:1] == b"{" and line[1:2] != b"\x00":  # json.detect_encoding: UTF-8 (no BOM starts with "{")
        return DECODER.decode(line.decode("utf-8", "surrogatepass"))
    return json.loads(line)


def complete_end(fd, size, chunk=CHUNK):
    """The offset just after the last newline before SIZE (0: none): a line still being written is not read."""
    position = size
    while position > 0:
        step = min(chunk, position)
        position -= step
        data = os.pread(fd, step, position)
        index = data.rfind(b"\n")
        if index >= 0:
            return position + index + 1
    return 0


def lines_from_end(fd, end, chunk=CHUNK, line_limit=LINE_LIMIT):
    """The complete lines (bytes, without the newline) of FD before offset END, the newest first."""
    position, partial, skipping = end, b"", False
    while position > 0:
        step = min(chunk, position)
        position -= step
        data = os.pread(fd, step, position) + partial
        pieces = data.split(b"\n")
        partial = pieces[0]
        for line in reversed(pieces[1:]):
            if skipping:  # the rest of an overlong line ends here
                skipping = False
                continue
            if line:
                yield line
        if len(partial) > line_limit:
            partial, skipping = b"", True
    if partial and not skipping:
        yield partial


def newest(fd, end, parse, keep=None, count=1, chunk=CHUNK):
    """The last COUNT records before END that PARSE(line) makes (None: not a record) and KEEP wants, oldest first."""
    found = []
    for line in lines_from_end(fd, end, chunk):
        record = parse(line)
        if record is None or (keep is not None and not keep(record)):
            continue
        found.append(record)
        if len(found) >= count:
            break
    found.reverse()
    return found
