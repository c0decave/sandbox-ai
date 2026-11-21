"""The newest lines of a log, read from its end: a command that shows the last N records of a JSON-lines log
(activity.jsonl, safeguards.jsonl) reads only as far back as it needs instead of the whole file, which grows for
as long as the log directory is used (every run of the project). Standard library only."""
import collections
import gzip
import json
import os
import zlib

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


def compressed(path):
    """Whether PATH is a log the compaction compressed (sandbox-ai gc: NAME.gz)."""
    return str(path).endswith(".gz")


def open_compressed(path):
    """The compressed log PATH as a stream of its lines (never through a link); OSError when it cannot be opened."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        return gzip.GzipFile(fileobj=os.fdopen(fd, "rb"), mode="rb")
    except BaseException:
        os.close(fd)
        raise


def unpacked_size(path):
    """About how many bytes the compressed log PATH holds unpacked, without unpacking it: gzip's trailer (the size
    modulo 4 GiB of its last member; a file of several members counts its last), else the file's size; 0 when it
    cannot be read. For what a reader whose budget is spent leaves out."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size < 18:
                return size
            stream.seek(size - 4)
            return int.from_bytes(stream.read(4), "little") or size
    except OSError:
        return 0


def compressed_tail(path, keep_bytes, keep_lines=None, line_limit=LINE_LIMIT):
    """The newest complete lines (bytes, each with its newline) of the compressed log PATH, oldest first: at most
    KEEP_BYTES of them, and KEEP_LINES. A compressed log can only be read from its start: it is decompressed as a
    stream, only what is kept held. (lines, bytes left out before them, overlong lines skipped); a damaged or cut
    file gives what was read up to the damage, a missing one nothing."""
    lines, held, skipped, long = collections.deque(), 0, 0, 0
    try:
        stream = open_compressed(path)
    except OSError:
        return [], 0, 0
    with stream:
        try:
            while True:
                line = stream.readline(line_limit + 1)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) <= line_limit:
                        break  # the last line, cut: not a record
                    long += 1
                    while line and not line.endswith(b"\n"):  # the rest of an overlong line
                        skipped += len(line)
                        line = stream.readline(line_limit)
                    skipped += len(line)
                    continue
                lines.append(line)
                held += len(line)
                while lines and (held > keep_bytes or (keep_lines is not None and len(lines) > keep_lines)):
                    dropped = lines.popleft()
                    held -= len(dropped)
                    skipped += len(dropped)
        except (OSError, EOFError, zlib.error):
            pass
    return list(lines), skipped, long
