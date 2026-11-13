"""The videos about the app's pages: where they are, which page each is about, how long it runs, its subtitles, and
their one download from the release of this version. No Qt (video_player.py plays them, help_view.py lists them).

The series "The desktop app, page by page" (docs/video/make_video.py --series pages) has one video per page of the
app, in the order of its navigation: sandbox-ai-app-NN-PAGE-LANG.mp4 (H.264 and AAC), its narration as WebVTT
subtitles beside it (the same name, .vtt). They are not kept in Git nor in the bundle (about 40 MB); the app finds
them in

- $SANDBOX_AI_VIDEOS, a folder named by hand;
- videos/ in sandbox-ai's tree (next to its sandbox/ folder: an unpacked bundle, a checkout);
- the app's data folder, ~/.local/share/sandbox-ai/videos (XDG_DATA_HOME): where a download puts them.

The download (install_release) takes sandbox-ai-videos.tar from the release of this version (VERSION's tag on the
repository of $SANDBOX_AI_SOURCE, else GitHub's) with the release's SHA256SUMS and SHA256SUMS.sig. Nothing of it is
used before the signature is good - by a key of the release-signers of the sandbox-ai installed here, as
`sandbox-ai update` checks a release - and the archive is the one SHA256SUMS lists; its members must be the videos
and subtitles of the series (plain files with those names), or nothing is installed. Only https (no redirect to
anything else), sizes bounded; nothing is ever played from the network.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
import hashlib
import html
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from .i18n import N_, tr

NAME = re.compile(r"sandbox-ai-app-(\d{2})-([a-z]+)-([a-z]{2})\.(mp4|vtt)")  # also scripts/make_dist.py VIDEO_NAME
ARCHIVE = "sandbox-ai-videos.tar"  # the release file (scripts/make_dist.py --videos DIR makes it)
DEFAULT_SOURCE = "https://github.com/c0decave/sandbox-ai"  # install.sh's DEFAULT_SOURCE
SUMS_LIMIT, SIGNATURE_LIMIT = 1 << 20, 64 << 10  # bytes of SHA256SUMS and of its signature, at most
ARCHIVE_LIMIT = 2 << 30  # bytes of the videos' archive, at most
MEMBER_LIMIT = 1 << 30  # bytes of one video in it, at most
LANGUAGE_NAMES = {"en": N_("English"), "de": N_("German")}
# The page each video is about: the navigation's titles (ui.SECTIONS; a test keeps the two the same).
PAGE_TITLES = {"launch": N_("Start"), "board": N_("Board"), "sandboxes": N_("Sandboxes & sessions"),
               "approvals": N_("Approvals"), "terminals": N_("Terminals"), "files": N_("File manager"),
               "kits": N_("Kits & plugins"), "mcp": N_("MCP servers"), "llm": N_("LLM servers"),
               "images": N_("Images"), "credentials": N_("Credentials"), "team": N_("Teams"),
               "connections": N_("Connections"), "activity": N_("Activity"), "statistics": N_("Statistics"),
               "system": N_("System"), "hosts": N_("Hosts"), "help": N_("Help")}
LOOPBACK_OK = False  # http to 127.0.0.1 allowed (the tests' fake release server alone; nothing in the app sets it)


class VideoError(Exception):
    """Why the videos could not be fetched or installed (said to the human as it is)."""


@dataclass
class Video:
    page: str
    number: int
    language: str
    path: Path
    subtitles: Path | None = None
    seconds: float | None = None
    others: dict = field(default_factory=dict)  # {language: Video} of the same page in other languages

    def title(self):
        return tr("Part {number}: {page}", number=self.number, page=page_title(self.page))

    def length(self):
        return duration_text(self.seconds)


def page_title(page):
    return tr(PAGE_TITLES[page]) if page in PAGE_TITLES else page


# -- where they are --

def data_folder(environ=None):
    """The app's own folder of videos (a download goes there)."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "sandbox-ai" / "videos"


def folders(directory=None, environ=None):
    """The folders looked in, first first: $SANDBOX_AI_VIDEOS, videos/ beside sandbox-ai's sandbox/ folder
    (DIRECTORY, SandboxAI.directory), the app's data folder."""
    environ = os.environ if environ is None else environ
    found = []
    if environ.get("SANDBOX_AI_VIDEOS"):
        found.append(Path(environ["SANDBOX_AI_VIDEOS"]).expanduser())
    if directory:
        found.append(Path(directory).parent / "videos")
    found.append(data_folder(environ))
    return list(dict.fromkeys(found))


def scan(places):
    """{page: {language: Video}} of the videos in PLACES (the first folder that has one wins), with their subtitles
    and length."""
    found = {}
    for folder in places:
        try:
            names = sorted(entry.name for entry in os.scandir(folder) if entry.is_file())
        except OSError:
            continue
        for name in names:
            match = NAME.fullmatch(name)
            if not match or match[4] != "mp4":
                continue
            number, page, language = int(match[1]), match[2], match[3]
            if language in found.get(page, {}):
                continue
            path = Path(folder) / name
            subtitles = path.with_suffix(".vtt")
            found.setdefault(page, {})[language] = Video(page, number, language, path,
                                                         subtitles if subtitles.is_file() else None)
    for languages in found.values():
        for video in languages.values():
            video.seconds = duration(video.path)
            if video.seconds is None and video.subtitles is not None:
                video.seconds = subtitles_end(video.subtitles)
    return found


def catalog(places, language="en"):
    """One Video per page, in the order of the series: in LANGUAGE where there is one, else in another (others has
    every language of that page)."""
    chosen = []
    for page, languages in scan(places).items():
        video = languages.get(language) or languages[sorted(languages)[0]]
        video.others = dict(languages)
        chosen.append(video)
    return sorted(chosen, key=lambda v: (v.number, v.page))


def for_page(videos, page):
    """The video about PAGE (a key of the app's navigation), or None."""
    return next((video for video in videos if video.page == page), None)


# -- how long --

def duration(path):
    """The length of an MP4 file in seconds (its movie header, mvhd), or None when it has none that can be read."""
    try:
        with open(path, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            moov = _box(stream, 0, size, b"moov")
            if moov is None:
                return None
            mvhd = _box(stream, moov[0], moov[1], b"mvhd")
            if mvhd is None:
                return None
            stream.seek(mvhd[0])
            head = stream.read(32)
    except OSError:
        return None
    if len(head) < 20:
        return None
    if head[0] == 1:
        if len(head) < 32:
            return None
        scale, length = struct.unpack(">IQ", head[20:32])
    else:
        scale, length = struct.unpack(">II", head[12:20])
    return length / scale if scale else None


def _box(stream, start, end, kind):
    """(the start of its content, its end) of the first box KIND between START and END, or None."""
    offset = start
    while offset + 8 <= end:
        stream.seek(offset)
        header = stream.read(8)
        if len(header) < 8:
            return None
        size, name = struct.unpack(">I4s", header)
        content = offset + 8
        if size == 1:
            large = stream.read(8)
            if len(large) < 8:
                return None
            size, content = struct.unpack(">Q", large)[0], offset + 16
        elif size == 0:
            size = end - offset
        if size < content - offset or offset + size > end:
            return None
        if name == kind:
            return content, offset + size
        offset += size
    return None


def duration_text(seconds):
    """1:05, 12:00, 1:02:03; "" when not known."""
    if seconds is None:
        return ""
    whole = int(round(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# -- subtitles (WebVTT) --

@dataclass(frozen=True)
class Cue:
    start: float  # seconds
    end: float
    text: str


TIMING = re.compile(r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d+:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?")
TAG = re.compile(r"</?[^>\n]*>")  # <b>, <i>, <c.yellow>, <v Speaker>, <00:00:01.000>, <ruby> ...


def timestamp(text):
    """Seconds of a WebVTT timestamp: [HH:]MM:SS.mmm."""
    parts = text.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) == 3 else 0
    if minutes > 59 or seconds >= 60:
        raise ValueError(text)
    return hours * 3600 + minutes * 60 + seconds


def cue_text(lines):
    """The text of a cue as shown: its lines, without markup and with its character references read."""
    return "\n".join(html.unescape(TAG.sub("", line)).replace("\u00a0", " ").strip() for line in lines).strip()


def parse_vtt(text):
    """The cues of a WebVTT text, by start time. Blocks that are no cue (NOTE, STYLE, REGION, a broken timing) are
    left out; a text that does not begin with WEBVTT has none."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", text)
    first = blocks[0].split("\n", 1)[0]
    if not (first == "WEBVTT" or first.startswith(("WEBVTT ", "WEBVTT\t"))):
        return []
    cues = []
    for block in blocks[1:]:
        lines = [line for line in block.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")) and "-->" not in lines[0]:
            continue
        if "-->" not in lines[0]:
            lines = lines[1:]  # its identifier
        if not lines:
            continue
        match = TIMING.fullmatch(lines[0].strip())
        if not match:
            continue
        try:
            start, end = timestamp(match[1]), timestamp(match[2])
        except ValueError:
            continue
        shown = cue_text(lines[1:])
        if end > start and shown:
            cues.append(Cue(start, end, shown))
    return sorted(cues, key=lambda cue: (cue.start, cue.end))


def read_vtt(path, limit=4 << 20):
    try:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        return []
    if len(data) > limit:
        return []
    return parse_vtt(data.decode("utf-8", errors="replace"))


def subtitles_end(path):
    cues = read_vtt(path)
    return max((cue.end for cue in cues), default=None)


class Subtitles:
    """The cues of one video, and the text shown at a time (cues that overlap: one below the other)."""

    def __init__(self, cues):
        self.cues = list(cues)
        self.starts = [cue.start for cue in self.cues]
        self.longest = max((cue.end - cue.start for cue in self.cues), default=0)

    def at(self, seconds):
        upto = bisect.bisect_right(self.starts, seconds)
        shown = []
        index = upto - 1
        while index >= 0 and self.cues[index].start >= seconds - self.longest:
            cue = self.cues[index]
            if cue.start <= seconds < cue.end:
                shown.append(cue)
            index -= 1
        return "\n".join(cue.text for cue in sorted(shown, key=lambda c: c.start))


# -- the download from the release --

def source(environ=None):
    environ = os.environ if environ is None else environ
    return (environ.get("SANDBOX_AI_SOURCE") or DEFAULT_SOURCE).strip()


def release_url(repository, version, name):
    """The address of the file NAME of the release VERSION of REPOSITORY (GitHub, Gitea and Forgejo serve it so)."""
    parts = urllib.parse.urlsplit(re.sub(r"\.git$", "", repository.strip().rstrip("/")))
    path = [p for p in parts.path.split("/") if p]
    if parts.scheme not in ("https", "http") or len(path) < 2 or not parts.hostname:
        raise VideoError(tr("Not a repository address: {source}", source=repository))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.+-][0-9A-Za-z.+-]+)?", str(version)):
        raise VideoError(tr("Not a version: {version}", version=version))
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/".join([""] + path), "", ""))
    return f"{base}/releases/download/v{version}/{urllib.parse.quote(name)}"


def allowed_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    return LOOPBACK_OK and parts.scheme == "http" and parts.hostname == "127.0.0.1"


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise VideoError(tr("Refused: the download was sent on to {address}, which is not https.",
                                address=newurl[:200]))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit, sink=None, progress=None, cancelled=None, timeout=60):
    """The bytes at URL (at most LIMIT); with SINK (a file open for writing) they go there and (bytes written,
    sha256) is returned. PROGRESS(done, total or None) after each piece; CANCELLED() true stops it (VideoError)."""
    if not allowed_url(url):
        raise VideoError(tr("Refused: {address} is not https.", address=url[:200]))
    request = urllib.request.Request(url, headers={"User-Agent": "sandbox-ai-app", "Accept": "application/octet-stream"})
    opener = urllib.request.build_opener(_HttpsOnly)
    digest, done, kept = hashlib.sha256(), 0, io.BytesIO()
    try:
        with opener.open(request, timeout=timeout) as response:
            total = response.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            if total is not None and total > limit:
                raise VideoError(tr("Refused: {name} is larger than {size} MB.", name=_name(url),
                                    size=limit >> 20))
            while True:
                if cancelled is not None and cancelled():
                    raise VideoError(tr("Cancelled."))
                piece = response.read(1 << 16)
                if not piece:
                    break
                done += len(piece)
                if done > limit:
                    raise VideoError(tr("Refused: {name} is larger than {size} MB.", name=_name(url),
                                        size=limit >> 20))
                digest.update(piece)
                (sink or kept).write(piece)
                if progress is not None:
                    progress(done, total)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise VideoError(tr("The release has no {name} ({address}).", name=_name(url), address=url)) from None
        raise VideoError(tr("{address}: HTTP {code}", address=url, code=exc.code)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise VideoError(tr("Cannot download {address}: {why}", address=url,
                            why=getattr(exc, "reason", exc))) from None
    return (done, digest.hexdigest()) if sink is not None else kept.getvalue()


def _name(url):
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])


def release_keys(directory):
    """(sandbox-ai's release_keys module, its trusted keys) of the installation in DIRECTORY (its sandbox/ folder)."""
    path = Path(directory) / "release_keys.py" if directory else None
    if path is None or not path.is_file():
        raise VideoError(tr("This sandbox-ai has no release keys to check the videos with."))
    spec = importlib.util.spec_from_file_location("sandbox_release_keys_for_videos", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        keys = module.load()
    except ValueError as exc:
        raise VideoError(str(exc)) from None
    if not keys:
        raise VideoError(tr("This sandbox-ai trusts no release key: the videos cannot be checked, so they are not "
                            "downloaded."))
    return module, keys


def checksums(text):
    return {parts[1].lstrip("*"): parts[0].lower() for parts in (line.split() for line in text.splitlines())
            if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0])}


def install_release(target, version, keys, repository=None, fetcher=fetch, progress=None, cancelled=None):
    """Download the videos of release VERSION into the folder TARGET, checked first: SHA256SUMS signed by one of
    KEYS ((release_keys module, keys)), the archive listed there with its SHA-256, its members the series' files.
    Returns the names installed; VideoError says why nothing was."""
    module, trusted = keys
    repository = repository or source()
    sums = fetcher(release_url(repository, version, "SHA256SUMS"), SUMS_LIMIT)
    try:
        signature = fetcher(release_url(repository, version, "SHA256SUMS.sig"), SIGNATURE_LIMIT)
    except VideoError as exc:
        raise VideoError(tr("The release {version} is not signed: the videos are not downloaded ({why}).",
                            version=version, why=exc)) from None
    try:
        verdict, detail = module.verify(sums, signature, trusted)
    except (RuntimeError, OSError) as exc:  # no ssh-keygen
        raise VideoError(tr("The signature cannot be checked: {why}", why=exc)) from None
    if verdict != "good":
        raise VideoError(tr("The release's signature is not good ({why}): nothing is downloaded.", why=detail))
    wanted = checksums(sums.decode("utf-8", errors="replace")).get(ARCHIVE)
    if wanted is None:
        raise VideoError(tr("The release {version} has no videos (its SHA256SUMS lists no {name}).", version=version,
                            name=ARCHIVE))
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    handle, partial = tempfile.mkstemp(prefix=".videos-download-", dir=target)
    try:
        with os.fdopen(handle, "wb") as sink:
            _size, got = fetcher(release_url(repository, version, ARCHIVE), ARCHIVE_LIMIT, sink=sink,
                                 progress=progress, cancelled=cancelled)
        if got != wanted:
            raise VideoError(tr("{name} is not the file the release's SHA256SUMS lists: nothing is installed.",
                                name=ARCHIVE))
        names = extract(partial, target)
    finally:
        try:
            os.unlink(partial)
        except OSError:
            pass
    (target / "videos.json").write_text(json.dumps({"version": version, "source": repository, "sha256": wanted,
                                                    "signed_by": detail, "files": names}, indent=1) + "\n")
    return names


def extract(archive, target):
    """The series' files of the tar ARCHIVE into TARGET: every member must be a plain file named as one of them (no
    folder, no link, no path), else nothing is installed (VideoError). Written beside first, then moved in."""
    target = Path(target)
    staging = Path(tempfile.mkdtemp(prefix=".videos-unpack-", dir=target))
    names = []
    try:
        try:
            with tarfile.open(archive, mode="r:") as tar:
                for member in tar:
                    if not member.isreg() or not NAME.fullmatch(member.name) or member.name in names:
                        raise VideoError(tr("The videos' archive holds {name}, which is not a video of the series: "
                                            "nothing is installed.", name=member.name[:200]))
                    if member.size > MEMBER_LIMIT:
                        raise VideoError(tr("Refused: {name} is larger than {size} MB.", name=member.name,
                                            size=MEMBER_LIMIT >> 20))
                    source_file = tar.extractfile(member)
                    with open(staging / member.name, "wb") as out:
                        shutil.copyfileobj(source_file, out, 1 << 16)
                    names.append(member.name)
        except (tarfile.TarError, EOFError) as exc:
            raise VideoError(tr("The videos' archive cannot be read: {why}", why=exc)) from None
        if not any(name.endswith(".mp4") for name in names):
            raise VideoError(tr("The videos' archive holds no video."))
        for name in names:
            os.replace(staging / name, target / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return names
