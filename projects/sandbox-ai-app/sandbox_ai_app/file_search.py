"""The file manager's search (user 2026-11-19: "eine generelle Suche mit Wildcard-Support"): every entry BELOW a folder
of a pane - this machine's, a sandbox's, a remote system's - whose name, or with a / in the pattern whose path below
that folder, matches a pattern, and (optional) whose text holds what is typed in the second field. No Qt.

Patterns (Matcher): * any characters but /, ? one character, [abc] [a-z] one of them, [!abc] none of them, ** any number
of folders (src/**/test_*.py; **/x at any depth, the start folder's own entries too). Letter case is ignored unless
asked; names are compared in NFC (a macOS name in NFD matches what was typed). A pattern without / is matched against
the name alone, at any depth (as find -name); a pattern without any wildcard is a part of the name (as the panes' quick
filter takes it: "report" finds "Q3 report.txt"); an empty pattern takes every name; a / at its end takes folders only.

Content (optional, Search.text): looked for in regular files of at most CONTENT_LIMIT bytes that are text - a NUL byte
makes a file binary, not looked into further; larger files, binary files, links and special files are counted
(Search.too_large, .binary, .links), never read beyond that. Letter case is ignored there too unless asked.

The walk never goes through a symbolic link (a link is an entry - shown, matched by its name -, never entered or read)
and stays on the start folder's file system (a folder another one is mounted in - a network share, a stick, a hung
mount - is named and not entered). Nothing is skipped silently: a folder or file that cannot be read is counted and
named (Search.unreadable), each folder not entered too (Search.not_entered). Bounded by MAX_RESULTS results,
MAX_DEPTH folders below the start, MAX_SECONDS, MAX_ENTRIES entries looked at and CONTENT_TOTAL bytes read; every
limit reached is said (Search.summary). Cancel is looked at between any two entries.

This machine's folders (files.Root) are walked as files.py walks them: relative to open descriptors (O_NOFOLLOW |
O_DIRECTORY, each folder checked to lie below the root), files read with O_NOFOLLOW | O_NONBLOCK and only when regular.
A remote system's folder (remote.RemoteRoot) is walked over its own SFTP connection by remote.TreeScan: the listings of
many folders and the reads of many files on their way at once (remote.WALK_WINDOW), so a tree of thousands of folders
takes about as many round trips as it is deep, never one per folder; no program runs there (SFTP alone, as everything
of remote.py: no shell, no find). The connection is held a slice at a time, so the panes' listings go between.

Search is filled in the search's thread and read by the page's timer (lists only appended to).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import stat
import time
import unicodedata

from . import files
from .files import Cancelled, Refused, printable, printable_text
from .i18n import tr

MAX_RESULTS = 5000  # results listed: more, and the search stops and says so
MAX_DEPTH = files.MAX_DEPTH  # folders below the start folder that are entered
MAX_SECONDS = 300  # s a search may take
MAX_ENTRIES = 2_000_000  # entries looked at
CONTENT_LIMIT = 2 << 20  # bytes: larger files are not looked into for the text
CONTENT_TOTAL = 512 << 20  # bytes read for the text in one search
LINE_SHOWN = 200  # characters of the line a text was found in
WILDCARDS = "*?["
mount_of = files._mount_of  # (the tests: a folder that is a mount point)


class Matcher:
    """PATTERN (see the module) compiled; CASE: letter case counts. Not a pattern: ValueError, said in words."""

    def __init__(self, pattern, case=False):
        text = unicodedata.normalize("NFC", str(pattern or "").strip())
        self.case = case
        self.folders_only = text.endswith("/") and text.strip("/") != ""
        text = text.strip("/") if "/" in text else text
        self.path = "/" in text
        self.part = None  # a part of the name (no wildcard), as it is compared
        self.regex = None
        if not text or text == "**":
            return
        if not self.path and not any(c in text for c in WILDCARDS):
            self.part = text if case else text.casefold()
            return
        try:
            self.regex = re.compile(translate(text), 0 if case else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(tr("Not a pattern: {pattern} ({error})", pattern=printable(pattern),
                                error=printable_text(exc))) from None

    def match(self, relative, name, kind="file"):
        """Whether the entry RELATIVE (below the start folder; NAME its last part; KIND its Entry kind) matches."""
        if self.folders_only and kind != "dir":
            return False
        if self.part is not None:
            name = unicodedata.normalize("NFC", name)
            return self.part in (name if self.case else name.casefold())
        if self.regex is None:
            return True
        return self.regex.fullmatch(unicodedata.normalize("NFC", relative if self.path else name)) is not None


def translate(pattern):
    """The regular expression of the glob PATTERN: * and ? never cross a /, ** does ("**/" also none)."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            if j - i >= 2:
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    j += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
            i = j
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:  # (no ]: a [ as it is)
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j]
                negate = body[:1] in ("!", "^")
                body = body[1:] if negate else body
                body = body.replace("\\", "\\\\").replace("[", "\\[").replace("&&", "&\\&").replace("~~", "~\\~") \
                    .replace("||", "|\\|")
                out.append(f"[^/{body}]" if negate else f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass
class Hit:
    """One entry found: RELATIVE below the start folder, its kind (files.Entry's), size, time and mode; LINE and TEXT:
    where the text was found (a content search)."""
    relative: str
    kind: str
    size: int
    mtime: float
    mode: int = 0
    line: int = 0
    text: str = ""

    @property
    def name(self):
        return self.relative.rsplit("/", 1)[-1]

    @property
    def folder(self):
        """The folder it is in, below the start folder ("" the start folder)."""
        return self.relative.rsplit("/", 1)[0] if "/" in self.relative else ""


class Search:
    """One search: PATTERN and TEXT (see the module), CASE: letter case counts; its limits; CANCELLED(): stop now;
    CLOCK: time.monotonic (tests). The walks fill it (seen, found, content, folder, unreadable_folder, ...); the page
    reads hits, the counts and summary()."""

    def __init__(self, pattern="", text="", case=False, cancelled=None, max_results=MAX_RESULTS, max_depth=MAX_DEPTH,
                 seconds=MAX_SECONDS, max_entries=MAX_ENTRIES, content_limit=CONTENT_LIMIT,
                 content_total=CONTENT_TOTAL, clock=time.monotonic):
        self.pattern, self.text, self.case = str(pattern or ""), str(text or ""), bool(case)
        self.matcher = Matcher(self.pattern, self.case)
        self.needle = self.text if self.case else self.text.casefold()
        self.cancelled = cancelled or (lambda: False)
        self.max_results, self.max_depth, self.seconds = max_results, max_depth, seconds
        self.max_entries, self.content_limit, self.content_total = max_entries, content_limit, content_total
        self.clock = clock
        self.began = clock()
        self.hits = []
        self.unreadable = []  # [(relative, why)] folders and files that could not be read
        self.not_entered = []  # [(relative, why)] folders not walked (too deep, another file system)
        self.folders = self.entries = self.links = 0
        self.binary = self.too_large = self.looked_into = self.content_bytes = self.not_looked_into = 0
        self.reserved = 0  # bytes of the files whose text is being read (CONTENT_TOTAL)
        self.stopped = None  # why it ended early: "cancelled", "results", "time", "entries"
        self.done = False
        self.error = None  # what made it fail as a whole (the start folder could not be read)
        self.ended_after = None  # s it took

    @property
    def halted(self):
        return self.stopped is not None

    def stop(self, why):
        if self.stopped is None:
            self.stopped = why

    def check(self):
        """Whether to stop now (Cancel, the time limit, a limit reached before)."""
        if self.stopped is None:
            if self.cancelled():
                self.stopped = "cancelled"
            elif self.clock() - self.began > self.seconds:
                self.stopped = "time"
        return self.stopped is not None

    # -- what the walks tell --

    def folder(self):
        self.folders += 1

    def seen(self, relative, name, link=False):
        """An entry RELATIVE (NAME; LINK: a symbolic link, never followed) was met: whether its name or path matches
        (then found() follows, with what it is)."""
        self.entries += 1
        if link:
            self.links += 1
        if self.entries > self.max_entries:
            self.stop("entries")
            return False
        if self.check():
            return False
        return self.matcher.match(relative, name, "dir")  # (a folder-only pattern: its kind is looked at in found())

    def found(self, relative, info):
        """INFO (lstat) of an entry whose name matched: a result - or, with a TEXT to look for, the bytes of it to read
        (then content() follows), 0 for none."""
        kind = files.kind_of(info.st_mode)
        if self.matcher.folders_only and kind != "dir":
            return 0
        if not self.needle:
            self.add(Hit(relative, kind, int(info.st_size or 0), float(info.st_mtime or 0), int(info.st_mode)))
            return 0
        if kind != "file":
            return 0
        size = int(info.st_size or 0)
        if size > self.content_limit:
            self.too_large += 1
            return 0
        if self.reserved + size > self.content_total:
            self.not_looked_into += 1
            return 0
        self.reserved += size
        return max(size, 1)

    def content(self, relative, info, data):
        """DATA: the bytes read of the file RELATIVE (None: it was no regular file when it was opened - a link put in
        its place)."""
        if data is None:
            self.links += 1
            return
        self.looked_into += 1
        self.content_bytes += len(data)
        if b"\0" in data:
            self.binary += 1
            return
        text = data.decode("utf-8", "replace")
        hay = unicodedata.normalize("NFC", text if self.case else text.casefold())
        needle = unicodedata.normalize("NFC", self.needle)
        at = hay.find(needle)
        if at < 0:
            return
        line = hay.count("\n", 0, at) + 1
        lines = text.split("\n")
        shown = lines[line - 1] if line <= len(lines) else ""
        self.add(Hit(relative, "file", int(info.st_size or len(data)), float(info.st_mtime or 0), int(info.st_mode),
                     line, printable(shown.strip()[:LINE_SHOWN])))

    def add(self, hit):
        if len(self.hits) >= self.max_results:
            self.stop("results")
            return
        self.hits.append(hit)

    def unreadable_folder(self, relative, why):
        self.unreadable.append((relative or ".", reason(why)))

    unreadable_file = unreadable_folder

    def not_entered_folder(self, relative, why):
        self.not_entered.append((relative, str(why)))

    # -- what is said --

    def progress(self):
        return tr("{folders} folder(s), {entries} entries looked at, {count} found", folders=self.folders,
                  entries=self.entries, count=len(self.hits))

    def summary(self):
        """The lines that say how it ended: every limit reached, what was not searched and why."""
        lines = []
        if self.stopped == "cancelled":
            lines.append(tr("Cancelled: what was found until then is listed."))
        elif self.stopped == "results":
            lines.append(tr("Stopped at {count} results: there are more. Narrow the pattern or search a smaller "
                            "folder.", count=self.max_results))
        elif self.stopped == "time":
            lines.append(tr("Stopped after {seconds} s: what was found until then is listed. Search a smaller "
                            "folder.", seconds=self.seconds))
        elif self.stopped == "entries":
            lines.append(tr("Stopped after {count} entries looked at: search a smaller folder.",
                            count=self.max_entries))
        if self.unreadable:
            lines.append(tr("{count} folder(s) or file(s) could not be read: {names}", count=len(self.unreadable),
                            names=named(rel for rel, _why in self.unreadable)))
        deep = [rel for rel, why in self.not_entered if why == "deep"]
        mounted = [rel for rel, why in self.not_entered if why == "mount"]
        if deep:
            lines.append(tr("{count} folder(s) more than {depth} deep were not searched: {names}", count=len(deep),
                            depth=self.max_depth, names=named(deep)))
        if mounted:
            lines.append(tr("{count} folder(s) with another file system mounted in them were not searched: {names}",
                            count=len(mounted), names=named(mounted)))
        if self.links:
            lines.append(tr("{count} symbolic link(s) met, not followed.", count=self.links))
        if self.needle:
            if self.too_large:
                lines.append(tr("{count} file(s) larger than {size} were not looked into.", count=self.too_large,
                                size=files.size_text(self.content_limit)))
            if self.binary:
                lines.append(tr("{count} file(s) are not text (binary) and were not looked into.", count=self.binary))
            if self.not_looked_into:
                lines.append(tr("{count} file(s) were not looked into: {size} of files were read already.",
                                count=self.not_looked_into, size=files.size_text(self.content_total)))
        return lines

    def not_searched(self):
        """Every folder and file not searched, one a line, with why (the dialog's Not searched…)."""
        why = {"deep": tr("more than {depth} folders deep", depth=self.max_depth),
               "mount": tr("another file system is mounted there")}
        return [f"{printable(rel)}: {printable(text)}" for rel, text in self.unreadable] + \
               [f"{printable(rel)}: {why.get(kind, kind)}" for rel, kind in self.not_entered]


def named(relatives, limit=5):
    """The first LIMIT of RELATIVES, printable, then how many more."""
    relatives = list(relatives)
    shown = ", ".join(printable(rel) for rel in relatives[:limit])
    return shown + (" " + tr("and {count} more", count=len(relatives) - limit) if len(relatives) > limit else "")


def reason(error):
    """The words of ERROR (an OSError: its strerror, never the path it may carry twice)."""
    if isinstance(error, OSError) and error.strerror:
        return printable_text(error.strerror)
    return printable_text(error)


# -- the walks --

def run(root, relative, search):
    """Walk the folder RELATIVE of ROOT (files.Root or remote.RemoteRoot) for SEARCH; it is done afterwards (an error of
    the start folder itself - gone, a link, unreadable - is raised and kept in SEARCH.error)."""
    try:
        if getattr(root, "remote", False):
            from . import remote
            top = root.walk(relative, last=True) if files.parts(relative) else root.path
            remote.TreeScan(root.sftp, top, search).run()
        else:
            fd = root.open_dir(relative)
            try:
                walk_local(root, fd, "", 0, search, mount_of(fd))
            finally:
                os.close(fd)
    except Cancelled:
        search.stop("cancelled")
    except BaseException as exc:
        search.error = exc
        raise
    finally:
        search.done = True
        search.ended_after = search.clock() - search.began


def walk_local(root, fd, prefix, depth, search, here):
    """The folder FD (PREFIX below the start folder, DEPTH deep, on the mount HERE): its entries in name order, then
    its folders, never through a link."""
    search.folder()
    try:
        with os.scandir(fd) as listed:
            entries = sorted(listed, key=lambda entry: entry.name)
    except OSError as exc:
        search.unreadable_folder(prefix, exc)
        return
    for entry in entries:
        if search.check():
            return
        name = entry.name
        relative = f"{prefix}/{name}" if prefix else name
        try:
            link = entry.is_symlink()
            folder = not link and entry.is_dir(follow_symlinks=False)
        except OSError:
            link = folder = False
        if search.seen(relative, name, link):
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # (gone meanwhile)
            except OSError as exc:
                search.unreadable_file(relative, exc)
                continue
            want = search.found(relative, info)
            if want:
                try:
                    data = read_local(fd, name, want)
                except Refused:
                    data = None  # (no regular file any more: a link put in its place)
                except FileNotFoundError:
                    data = b""
                except OSError as exc:
                    search.unreadable_file(relative, exc)
                    data = b""
                search.content(relative, info, data)
            if search.halted:
                return
        if not folder:
            continue
        if depth + 1 > search.max_depth:
            search.not_entered_folder(relative, "deep")
            continue
        try:
            child = root._open_child_dir(fd, name)
        except FileNotFoundError:
            continue
        except (OSError, Refused) as exc:
            search.unreadable_folder(relative, exc)
            continue
        try:
            mount = mount_of(child)
            if mount != here:
                search.not_entered_folder(relative, "mount")
                continue
            walk_local(root, child, relative, depth + 1, search, here)
        finally:
            os.close(child)
        if search.halted:
            return


def read_local(dir_fd, name, limit):
    """Up to LIMIT bytes of the regular file NAME in DIR_FD (never through a link, never a FIFO or device); stops at
    the first chunk with a NUL byte (binary)."""
    fd, info = files._open_regular(name, dir_fd)
    try:
        if not stat.S_ISREG(info.st_mode):
            raise Refused(tr("{name} is not a regular file.", name=printable(name)))
        chunks, read = [], 0
        while read < limit:
            chunk = os.read(fd, min(1 << 20, limit - read))
            if not chunk:
                break
            chunks.append(chunk)
            read += len(chunk)
            if b"\0" in chunk:
                break
        return b"".join(chunks)
    finally:
        os.close(fd)
