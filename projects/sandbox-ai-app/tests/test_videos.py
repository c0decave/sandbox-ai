"""The videos about the app's pages without Qt (videos.py): the WebVTT parser and the subtitle shown at a time, the
length of an MP4, which video belongs to which page, where they are looked for, and the one download from the
release - against a local fake release signed with a key made here: a good signature, the archive SHA256SUMS
lists and the series' files only, or nothing is installed."""
import hashlib
import http.server
import importlib.util
import io
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import threading

import pytest

from sandbox_ai_app import videos as V

ROOT = Path(__file__).resolve().parents[3]  # the sandbox-ai checkout
REAL = Path("/work/videos-0.1.94")  # the rendered series, where it was made (not in Git)


# -- WebVTT --

SAMPLE = ("\ufeffWEBVTT - the narration\r\n\r\n"
          "NOTE made by make_video.py\r\nover two lines\r\n\r\n"
          "STYLE\r\n::cue { color: yellow }\r\n\r\n"
          "1\r\n00:00:00.500 --> 00:00:10.085\r\n"
          "Sandboxen &amp; Sitzungen listet <b>jede</b> Sandbox.\r\n\r\n"
          "intro-2\r\n00:10.785 --> 00:20.822 align:start position:10%\r\n"
          "<v Sprecher>Jeder Host ist ein Zweig,</v>\r\n<c.yellow>darunter seine Starts</c>.\r\n\r\n"
          "01:02:03.000 --> 01:02:04.250\r\nLate &lt;cue&gt;&nbsp;here\r\n\r\n"
          "00:00:30.000 --> 00:00:29.000\r\nends before it begins\r\n\r\n"
          "00:00:31.000 -> 00:00:32.000\r\nnot a timing\r\n\r\n"
          "00:00:33.000 --> 00:00:34.000\r\n\r\n")


def test_webvtt_cues_their_times_and_text_without_markup():
    cues = V.parse_vtt(SAMPLE)
    assert [(c.start, c.end) for c in cues] == [(0.5, 10.085), (10.785, 20.822), (3723.0, 3724.25)]
    assert cues[0].text == "Sandboxen & Sitzungen listet jede Sandbox."
    assert cues[1].text == "Jeder Host ist ein Zweig,\ndarunter seine Starts."
    assert cues[2].text == "Late <cue> here"


def test_webvtt_needs_its_header_and_takes_any_line_end():
    assert V.parse_vtt("1\n00:00:01.000 --> 00:00:02.000\nno header\n") == []
    assert V.parse_vtt("WEBVTTX\n\n00:00:01.000 --> 00:00:02.000\nx\n") == []
    assert V.parse_vtt("") == []
    lone_cr = "WEBVTT\r\r00:00:01.000 --> 00:00:02.000\rone\r"
    assert [c.text for c in V.parse_vtt(lone_cr)] == ["one"]
    unsorted = "WEBVTT\n\n00:00:05.000 --> 00:00:06.000\nb\n\n00:00:01.000 --> 00:00:02.000\na\n"
    assert [c.text for c in V.parse_vtt(unsorted)] == ["a", "b"]
    assert V.parse_vtt("WEBVTT\n\n00:61.000 --> 00:62.000\nbad seconds\n") == []


def test_the_subtitle_shown_at_a_time():
    subtitles = V.Subtitles(V.parse_vtt(
        "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nfirst\n\n00:00:04.000 --> 00:00:06.000\nsecond\n\n"
        "00:00:10.000 --> 00:00:11.000\nthird\n"))
    assert subtitles.at(0.0) == "" and subtitles.at(1.0) == "first"
    assert subtitles.at(4.5) == "first\nsecond"
    assert subtitles.at(5.0) == "second"  # (an end is the first moment it is gone)
    assert subtitles.at(7.0) == "" and subtitles.at(10.5) == "third" and subtitles.at(99) == ""
    assert V.Subtitles([]).at(3) == ""


def test_the_real_subtitles_of_the_series_parse():
    files = sorted(REAL.glob("*.vtt")) if REAL.is_dir() else []
    if not files:
        pytest.skip(f"the rendered series is not in {REAL}")
    for path in files:
        cues = V.read_vtt(path)
        assert len(cues) >= 3, path
        assert all(c.text and "<" not in c.text and "-->" not in c.text for c in cues), path
        assert cues[-1].end < 600


# -- the length of an MP4 --

def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mp4(seconds, scale=1000, version=0, large=False):
    if version == 0:
        mvhd = bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, scale, int(seconds * scale)) + bytes(80)
    else:
        mvhd = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", 0, 0, scale, int(seconds * scale)) + bytes(80)
    moov = box(b"moov", box(b"trak", b"\0" * 12) + box(b"mvhd", mvhd))
    free = struct.pack(">I4sQ", 1, b"free", 16 + 5) + b"xxxxx" if large else box(b"free", b"x")
    return box(b"ftyp", b"isom\0\0\0\0isom") + free + box(b"mdat", b"\0" * 100) + moov


def test_the_length_of_an_mp4_from_its_movie_header(tmp_path):
    for name, data, wanted in (("v0.mp4", mp4(65.4), 65.4), ("v1.mp4", mp4(3725, 90000, 1), 3725),
                               ("large.mp4", mp4(12, large=True), 12)):
        (tmp_path / name).write_bytes(data)
        assert V.duration(tmp_path / name) == pytest.approx(wanted), name
    for name, data in (("empty.mp4", b""), ("text.mp4", b"not a video at all"),
                       ("cut.mp4", mp4(5)[:-60]), ("nomoov.mp4", box(b"ftyp", b"isom"))):
        (tmp_path / name).write_bytes(data)
        assert V.duration(tmp_path / name) is None, name
    assert V.duration(tmp_path / "missing.mp4") is None
    assert [V.duration_text(s) for s in (None, 0, 5.4, 65.4, 600, 3725)] == ["", "0:00", "0:05", "1:05", "10:00",
                                                                              "1:02:05"]


def test_the_real_videos_have_a_length():
    files = sorted(REAL.glob("*.mp4")) if REAL.is_dir() else []
    if not files:
        pytest.skip(f"the rendered series is not in {REAL}")
    for path in files:
        seconds = V.duration(path)
        assert seconds and 20 < seconds < 600, (path, seconds)
        assert abs(seconds - V.subtitles_end(path.with_suffix(".vtt"))) < 15, path


# -- which video, where --

def series(folder, pages=(("launch", 1), ("board", 2), ("help", 18)), languages=("de",), subtitles=True):
    folder.mkdir(parents=True, exist_ok=True)
    for page, number in pages:
        for language in languages:
            base = folder / f"sandbox-ai-app-{number:02d}-{page}-{language}"
            base.with_suffix(".mp4").write_bytes(mp4(60 + number))
            if subtitles:
                base.with_suffix(".vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nx\n")
    return folder


def test_one_video_per_page_in_the_series_order_in_the_language_wanted(tmp_path):
    series(tmp_path / "a", languages=("de", "en"))
    (tmp_path / "a" / "notes.mp4").write_bytes(b"x")  # not one of the series
    (tmp_path / "a" / "sandbox-ai-app-03-sandboxes-de.vtt").write_text("WEBVTT\n")  # subtitles without a video
    chosen = V.catalog([tmp_path / "a"], "en")
    assert [(v.page, v.number, v.language) for v in chosen] == [("launch", 1, "en"), ("board", 2, "en"),
                                                                 ("help", 18, "en")]
    assert set(chosen[0].others) == {"de", "en"}
    assert chosen[1].seconds == pytest.approx(62) and chosen[1].length() == "1:02"
    assert chosen[1].subtitles == tmp_path / "a" / "sandbox-ai-app-02-board-en.vtt"
    assert chosen[0].title() == "Part 1: Start" and chosen[2].title() == "Part 18: Help"
    german_only = V.catalog([series(tmp_path / "b")], "en")
    assert [v.language for v in german_only] == ["de", "de", "de"]  # there is none in English: the German one
    assert V.for_page(german_only, "board").number == 2 and V.for_page(german_only, "kits") is None


def test_the_first_folder_wins_and_a_video_without_subtitles_is_listed(tmp_path):
    first = series(tmp_path / "first", pages=(("board", 2),), subtitles=False)
    second = series(tmp_path / "second")
    chosen = V.catalog([tmp_path / "missing", first, second], "de")
    assert [(v.page, v.path.parent.name) for v in chosen] == [("launch", "second"), ("board", "first"),
                                                              ("help", "second")]
    assert V.for_page(chosen, "board").subtitles is None


def test_where_the_videos_are_looked_for(tmp_path):
    environ = {"XDG_DATA_HOME": str(tmp_path / "data")}
    tree = tmp_path / "sandbox-ai-0.1.94" / "sandbox"
    assert V.folders(tree, environ) == [tmp_path / "sandbox-ai-0.1.94" / "videos",
                                        tmp_path / "data" / "sandbox-ai" / "videos"]
    environ["SANDBOX_AI_VIDEOS"] = str(tmp_path / "mine")
    assert V.folders(None, environ) == [tmp_path / "mine", tmp_path / "data" / "sandbox-ai" / "videos"]
    assert V.data_folder({"XDG_DATA_HOME": "/d"}) == Path("/d/sandbox-ai/videos")
    assert V.data_folder({}) == Path.home() / ".local" / "share" / "sandbox-ai" / "videos"


def test_every_page_of_the_series_is_a_page_of_the_app():
    """The storyboard's series (docs/video/storyboard.py): one video per page, in the order of the app's
    navigation, every page with its title here."""
    spec = importlib.util.spec_from_file_location("storyboard", ROOT / "docs" / "video" / "storyboard.py")
    storyboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(storyboard)
    pages = [video["page"] for video in storyboard.SERIES["pages"]["videos"]]
    assert pages == list(V.PAGE_TITLES)
    if REAL.is_dir():
        names = sorted(p.name for p in REAL.glob("*.mp4"))
        assert names == [f"sandbox-ai-app-{n:02d}-{page}-de.mp4" for n, page in enumerate(pages, 1)]


# -- the download from the release --

def test_release_addresses():
    assert V.release_url("https://github.com/c0decave/sandbox-ai", "0.1.94", "SHA256SUMS") == \
        "https://github.com/c0decave/sandbox-ai/releases/download/v0.1.94/SHA256SUMS"
    assert V.release_url("https://git.example.org/forge/me/sandbox-ai.git/", "0.1.94", V.ARCHIVE) == \
        "https://git.example.org/forge/me/sandbox-ai/releases/download/v0.1.94/sandbox-ai-videos.tar"
    for bad in ("file:///x/y", "https://github.com/onlyowner", "ftp://h/o/r"):
        with pytest.raises(V.VideoError):
            V.release_url(bad, "0.1.94", "x")
    with pytest.raises(V.VideoError):
        V.release_url("https://github.com/o/r", "0.1.94/../../x", "x")
    assert V.source({}) == V.DEFAULT_SOURCE and V.source({"SANDBOX_AI_SOURCE": "https://h/o/r"}) == "https://h/o/r"


def test_only_https_is_fetched(monkeypatch):
    with pytest.raises(V.VideoError, match="not https"):
        V.fetch("http://example.org/x", 10)
    with pytest.raises(V.VideoError, match="not https"):
        V.fetch("http://127.0.0.1:1/x", 10)  # (not even this machine, but for the tests' server)
    with pytest.raises(V.VideoError):
        V.fetch("file:///etc/passwd", 10)


def load_release_keys():
    spec = importlib.util.spec_from_file_location("release_keys_for_tests", ROOT / "sandbox" / "release_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def new_key(folder, name):
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    (folder / "keys").mkdir(exist_ok=True)
    key = folder / "keys" / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True)
    kind, blob = key.with_suffix(".pub").read_text().split()[:2]
    return key, (kind, blob)


def archive_of(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


GOOD = [("sandbox-ai-app-01-launch-de.mp4", mp4(61), "file"),
        ("sandbox-ai-app-01-launch-de.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHallo\n", "file"),
        ("sandbox-ai-app-02-board-de.mp4", mp4(62), "file")]


class Release:
    """A fake release in a folder: its files by name, SHA256SUMS over them, SHA256SUMS.sig by KEY."""

    def __init__(self, folder, key, archive, listed=None, signed=True):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        (folder / V.ARCHIVE).write_bytes(archive)
        digest = hashlib.sha256(listed if listed is not None else archive).hexdigest()
        (folder / "SHA256SUMS").write_text(f"{'0' * 64}  install.sh\n{digest}  {V.ARCHIVE}\n")
        if signed:
            (folder / "SHA256SUMS.sig").write_bytes(load_release_keys().sign((folder / "SHA256SUMS").read_bytes(), key))
        self.asked = []

    def fetch(self, url, limit, sink=None, progress=None, cancelled=None):
        assert url.startswith("https://github.com/c0decave/sandbox-ai/releases/download/v0.1.94/"), url
        self.asked.append(url.rsplit("/", 1)[-1])
        path = self.folder / url.rsplit("/", 1)[-1]
        if not path.is_file():
            raise V.VideoError(f"The release has no {path.name}")
        data = path.read_bytes()[:limit + 1]
        if len(data) > limit:
            raise V.VideoError("larger")
        if sink is None:
            return data
        sink.write(data)
        if progress:
            progress(len(data), len(data))
        return len(data), hashlib.sha256(data).hexdigest()


@pytest.fixture
def signing(tmp_path):
    module = load_release_keys()
    key, trusted = new_key(tmp_path, "release")
    other, _ = new_key(tmp_path, "other")
    return module, key, other, [trusted]


def test_a_signed_release_installs_its_videos_and_they_are_listed(tmp_path, signing):
    module, key, _other, trusted = signing
    release = Release(tmp_path / "release", key, archive_of(GOOD))
    target = tmp_path / "data" / "sandbox-ai" / "videos"
    seen = []
    names = V.install_release(target, "0.1.94", (module, trusted), V.DEFAULT_SOURCE, release.fetch,
                              progress=lambda done, total: seen.append((done, total)))
    assert names == [m[0] for m in GOOD]
    assert release.asked == ["SHA256SUMS", "SHA256SUMS.sig", V.ARCHIVE]
    assert sorted(p.name for p in target.iterdir()) == sorted(names + ["videos.json"])
    assert seen and seen[-1][0] == len(archive_of(GOOD))
    chosen = V.catalog([target], "de")
    assert [(v.page, round(v.seconds)) for v in chosen] == [("launch", 61), ("board", 62)]
    assert [c.text for c in V.read_vtt(chosen[0].subtitles)] == ["Hallo"]


@pytest.mark.parametrize("case", ["other key", "unsigned", "changed sums", "not listed", "other archive"])
def test_nothing_is_installed_without_a_good_signature_and_the_listed_archive(tmp_path, signing, case):
    module, key, other, trusted = signing
    archive = archive_of(GOOD)
    release = Release(tmp_path / "release", other if case == "other key" else key, archive,
                      listed=archive + b"x" if case == "other archive" else None, signed=case != "unsigned")
    if case == "changed sums":
        sums = release.folder / "SHA256SUMS"
        sums.write_text(sums.read_text().replace("install.sh", "evil.sh"))
    if case == "not listed":
        sums = release.folder / "SHA256SUMS"
        sums.write_text(sums.read_text().replace(V.ARCHIVE, "something-else.tar"))
        (release.folder / "SHA256SUMS.sig").write_bytes(module.sign(sums.read_bytes(), key))
    target = tmp_path / "videos"
    with pytest.raises(V.VideoError) as caught:
        V.install_release(target, "0.1.94", (module, trusted), V.DEFAULT_SOURCE, release.fetch)
    words = {"other key": "not a trusted", "unsigned": "not signed", "changed sums": "does not match",
             "not listed": "has no videos", "other archive": "is not the file"}[case]
    assert words in str(caught.value)
    assert not target.exists() or [p.name for p in target.iterdir()] == []
    if case in ("other key", "unsigned", "changed sums", "not listed"):
        assert V.ARCHIVE not in release.asked  # (not even downloaded)


@pytest.mark.parametrize("member", [("../sandbox-ai-app-01-launch-de.mp4", b"x", "file"),
                                    ("sub/sandbox-ai-app-01-launch-de.mp4", b"x", "file"),
                                    ("sandbox-ai-app-03-sandboxes-de.mp4", b"", "link"),
                                    ("sandbox-ai-app-03-sandboxes-de.mp4", b"", "dir"),
                                    (".bashrc", b"x", "file"), ("sandbox-ai-app-01-launch-de.mp4", b"x", "file")])
def test_an_archive_with_anything_but_the_series_files_installs_nothing(tmp_path, signing, member):
    module, key, _other, trusted = signing
    release = Release(tmp_path / "release", key, archive_of(GOOD + [member]))
    target = tmp_path / "videos"
    target.mkdir()
    (target / "kept.txt").write_text("mine")
    with pytest.raises(V.VideoError, match="not a video of the series"):
        V.install_release(target, "0.1.94", (module, trusted), V.DEFAULT_SOURCE, release.fetch)
    assert sorted(p.name for p in target.iterdir()) == ["kept.txt"]
    assert not (tmp_path / "sandbox-ai-app-01-launch-de.mp4").exists()


def test_an_archive_that_is_no_tar_or_holds_no_video_installs_nothing(tmp_path, signing):
    module, key, _other, trusted = signing
    for archive, words in ((b"not a tar file" * 100, "cannot be read"), (archive_of(GOOD[1:2]), "no video")):
        release = Release(tmp_path / "release", key, archive)
        target = tmp_path / "videos"
        with pytest.raises(V.VideoError, match=words):
            V.install_release(target, "0.1.94", (module, trusted), V.DEFAULT_SOURCE, release.fetch)
        assert [p.name for p in target.iterdir()] == []


def test_the_installed_release_keys_are_the_ones_checked(tmp_path):
    module, keys = V.release_keys(ROOT / "sandbox")
    assert keys == module.load() and keys  # sandbox/release-signers lists the release key
    empty = tmp_path / "sandbox"
    empty.mkdir()
    shutil.copy(ROOT / "sandbox" / "release_keys.py", empty / "release_keys.py")
    with pytest.raises(V.VideoError, match="trusts no release key"):
        V.release_keys(empty)
    with pytest.raises(V.VideoError, match="no release keys"):
        V.release_keys(tmp_path / "nothing")


class Server:
    """The fake release over http on 127.0.0.1 (LOOPBACK_OK), as GitHub serves it: a redirect to the file."""

    def __init__(self, folder, redirect_to=None):
        outer = self
        self.hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                name = self.path.rsplit("/", 1)[-1]
                if "/releases/download/v0.1.94/" in self.path or "/releases/download/v1.2.3/" in self.path:
                    self.send_response(302)
                    self.send_header("Location", (redirect_to or f"http://127.0.0.1:{outer.port}") + f"/files/{name}")
                    self.end_headers()
                    return
                path = folder / name
                if not self.path.startswith("/files/") or not path.is_file():
                    self.send_error(404)
                    return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_download_end_to_end_over_http_with_a_redirect(tmp_path, signing, monkeypatch):
    module, key, _other, trusted = signing
    Release(tmp_path / "release", key, archive_of(GOOD))
    server = Server(tmp_path / "release")
    monkeypatch.setattr(V, "LOOPBACK_OK", True)
    try:
        repository = f"http://127.0.0.1:{server.port}/c0decave/sandbox-ai"
        names = V.install_release(tmp_path / "videos", "0.1.94", (module, trusted), repository)
        assert len(names) == 3 and (tmp_path / "videos" / GOOD[0][0]).read_bytes() == GOOD[0][1]
        assert "/c0decave/sandbox-ai/releases/download/v0.1.94/sandbox-ai-videos.tar" in server.hits
        with pytest.raises(V.VideoError, match="has no"):
            V.install_release(tmp_path / "other", "0.1.95", (module, trusted), repository)
        with pytest.raises(V.VideoError, match="Cancelled"):
            V.fetch(f"{repository}/releases/download/v0.1.94/{V.ARCHIVE}", 1 << 30, sink=io.BytesIO(),
                    cancelled=lambda: True)
        with pytest.raises(V.VideoError, match="larger than"):
            V.fetch(f"{repository}/releases/download/v0.1.94/{V.ARCHIVE}", 100)
    finally:
        server.close()


def test_a_redirect_to_anything_but_https_is_refused(tmp_path, monkeypatch):
    server = Server(tmp_path, redirect_to="http://example.org")
    monkeypatch.setattr(V, "LOOPBACK_OK", True)
    try:
        with pytest.raises(V.VideoError, match="not https"):
            V.fetch(f"http://127.0.0.1:{server.port}/o/r/releases/download/v1.2.3/SHA256SUMS", 1000)
    finally:
        server.close()


def test_make_dist_packs_the_videos_the_app_installs(tmp_path, signing):
    """scripts/make_dist.py --videos DIR: the archive of the series' files (reproducible, its names the app's), which
    install_release takes."""
    spec = importlib.util.spec_from_file_location("make_dist_for_tests", ROOT / "scripts" / "make_dist.py")
    make_dist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_dist)
    assert make_dist.VIDEO_NAME.pattern == V.NAME.pattern and make_dist.VIDEOS == V.ARCHIVE
    source = series(tmp_path / "rendered")
    (source / "notes.txt").write_text("left out")
    first = make_dist.videos_archive(source, tmp_path / "one", mtime=1234)
    second = make_dist.videos_archive(source, tmp_path / "two", mtime=1234)
    assert first.name == V.ARCHIVE and first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as tar:
        assert [m.name for m in tar] == sorted(p.name for p in source.iterdir() if V.NAME.fullmatch(p.name))
        assert all(m.mtime == 1234 and m.uid == 0 and m.mode == 0o644 for m in tar)
    module, key, _other, trusted = signing
    release = Release(tmp_path / "release", key, first.read_bytes())
    names = V.install_release(tmp_path / "videos", "0.1.94", (module, trusted), V.DEFAULT_SOURCE, release.fetch)
    assert len(names) == 6
    with pytest.raises(SystemExit):
        make_dist.videos_archive(tmp_path / "nothing-here", tmp_path / "three")
