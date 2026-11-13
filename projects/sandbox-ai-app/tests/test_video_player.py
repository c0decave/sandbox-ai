"""The videos in the app (video_player.py, help_view.py, ui.py): the player's controls with a stand-in for
QtMultimedia (play and pause, seek, volume and mute, speed, subtitles from the .vtt, full screen, another video,
the system's player when one cannot be played), the player without a backend, the offer to download the videos
when they are not here (end to end against a local fake release), the Help page's list, the (?)'s "Watch the
video", the button beside the page's (?) and the Help menu; and, where QtMultimedia can play here, a real video
with its sound track and subtitles."""
import functools

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QObject, QPoint, Qt, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from sandbox_ai_app import help_view, video_player as P, videos as V  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests
from test_videos import GOOD, REAL, Release, archive_of, load_release_keys, new_key, series  # noqa: E402


class FakePlayer(QObject):
    positionChanged = Signal(int)  # noqa: N815 - QMediaPlayer's names
    durationChanged = Signal(int)  # noqa: N815
    playbackStateChanged = Signal(object)  # noqa: N815
    errorOccurred = Signal(object, str)  # noqa: N815

    def __init__(self):
        super().__init__()
        self.source, self.state, self.at, self.length, self.rate, self.calls = None, "stopped", 0, 0, 1.0, []

    def setSource(self, url):  # noqa: N802
        self.source = url.toLocalFile()
        self.at, self.length = 0, 106840
        self.durationChanged.emit(self.length)

    def play(self):
        self.state = P.PLAYING
        self.playbackStateChanged.emit(self.state)

    def pause(self):
        self.state = "paused"
        self.playbackStateChanged.emit(self.state)

    def stop(self):
        self.state = "stopped"

    def playbackState(self):  # noqa: N802
        return self.state

    def position(self):
        return self.at

    def duration(self):
        return self.length

    def setPosition(self, ms):  # noqa: N802
        self.at = ms
        self.positionChanged.emit(ms)

    def setPlaybackRate(self, rate):  # noqa: N802
        self.rate = rate


class FakeAudio:
    def __init__(self):
        self.volume, self.muted = 1.0, False

    def setVolume(self, value):  # noqa: N802
        self.volume = value

    def setMuted(self, muted):  # noqa: N802
        self.muted = muted


class FakeMedia:
    def __init__(self):
        self.player, self.audio, self.view = FakePlayer(), FakeAudio(), QtWidgets.QWidget()


VTT = ("WEBVTT\n\n00:00:00.500 --> 00:00:10.000\nDie Seite Board zeigt den Plan.\n\n"
       "00:00:30.000 --> 00:00:40.000\nJeder Teil hat ein Fragezeichen.\n")


@pytest.fixture
def made(tmp_path, monkeypatch, qapp):
    folder = series(tmp_path / "videos", pages=(("launch", 1), ("board", 2), ("help", 18)))
    (folder / "sandbox-ai-app-02-board-de.vtt").write_text(VTT)
    (folder / "sandbox-ai-app-18-help-de.vtt").unlink()
    found = V.catalog([folder], "en")
    window = P.VideoPlayer(V.for_page(found, "board"), found, media=FakeMedia())
    window.show()
    yield window, found
    if isValid(window):
        window.close()


def test_the_player_plays_pauses_seeks_and_shows_the_time(made):
    window, _found = made
    player = window.player
    assert player.source.endswith("sandbox-ai-app-02-board-de.mp4") and player.state == P.PLAYING
    assert window.windowTitle() == "Video: Part 2: Board"
    assert window.play_button.accessibleName() == "Pause"
    window.play_button.click()
    assert player.state == "paused" and window.play_button.accessibleName() == "Play"
    QTest.keyClick(window.view, Qt.Key.Key_Space)
    assert player.state == P.PLAYING
    window.position.setValue(30000)
    window.slider_released()
    assert player.at == 30000 and window.time.text() == "0:30 / 1:47"
    QTest.keyClick(window.view, Qt.Key.Key_Right)
    assert player.at == 35000
    QTest.keyClick(window.view, Qt.Key.Key_Left)
    QTest.keyClick(window.view, Qt.Key.Key_Left)
    assert player.at == 25000
    window.seek_by(-100000)
    assert player.at == 0
    window.seek_by(10 ** 7)
    assert player.at == player.length


def test_volume_mute_and_speed(made):
    window, _found = made
    window.volume.setValue(40)
    assert window.audio.volume == pytest.approx(0.4)
    QTest.keyClick(window.view, Qt.Key.Key_Up)
    assert window.volume.value() == 50 and window.audio.volume == pytest.approx(0.5)
    QTest.keyClick(window.view, Qt.Key.Key_M)
    assert window.audio.muted and window.mute_button.isChecked()
    window.mute_button.click()
    assert not window.audio.muted
    window.speed.setCurrentIndex(P.SPEEDS.index(1.5))
    assert window.player.rate == 1.5
    window.load(window.catalog[0])  # another video keeps the speed chosen
    assert window.player.rate == 1.5


def test_subtitles_from_the_vtt_on_and_off(made, qapp):
    window, found = made
    assert window.subtitles_box.isEnabled() and not window.subtitles_box.isChecked()  # off by default
    assert not window.caption.isVisible()
    window.subtitles_box.setChecked(True)
    window.seek(31000)
    assert window.caption.isVisible() and window.caption.text() == "Jeder Teil hat ein Fragezeichen."
    window.seek(20000)
    assert window.caption.text() == ""
    QTest.keyClick(window.view, Qt.Key.Key_C)
    assert not window.subtitles_box.isChecked() and not window.caption.isVisible()
    window.subtitles_box.setChecked(True)
    window.load(V.for_page(found, "help"))  # no .vtt beside it
    assert not window.subtitles_box.isEnabled() and not window.caption.isVisible()
    assert "no subtitles" in window.subtitles_box.toolTip()


def test_full_screen_and_esc(made, qapp):
    window, _found = made
    QTest.keyClick(window.view, Qt.Key.Key_F)
    qapp.processEvents()
    assert window.isFullScreen()
    QTest.keyClick(window.view, Qt.Key.Key_Escape)  # first Esc: full screen ends, the window stays
    qapp.processEvents()
    assert not window.isFullScreen() and window.isVisible()
    window.fullscreen_button.click()
    qapp.processEvents()
    assert window.isFullScreen()
    window.toggle_fullscreen()
    qapp.processEvents()
    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not isValid(window) or not window.isVisible()  # (closed: deleted on close)


def test_another_video_of_the_series_and_the_system_player(made):
    from conftest import OPENED_URLS
    window, found = made
    assert [window.chooser.itemText(i) for i in range(window.chooser.count())] == [
        "Part 1: Start (1:01) - German", "Part 2: Board (1:02) - German", "Part 18: Help (1:18) - German"]
    assert window.chooser.currentIndex() == 1
    window.chooser.activated.emit(0)
    assert window.player.source.endswith("sandbox-ai-app-01-launch-de.mp4") and window.video.page == "launch"
    window.player.errorOccurred.emit("format", "Unsupported codec")
    assert "cannot be played here: Unsupported codec" in window.bar.text()
    before = len(OPENED_URLS)
    window.bar.choose("Open with the system player")
    assert OPENED_URLS[before].toLocalFile().endswith("sandbox-ai-app-01-launch-de.mp4")
    assert window.player.state == "paused"
    before = len(OPENED_URLS)
    window.external.click()
    assert len(OPENED_URLS) == before + 1


def test_without_a_backend_the_system_player_is_offered(tmp_path, monkeypatch, qapp):
    from conftest import OPENED_URLS
    folder = series(tmp_path / "videos")
    monkeypatch.setattr(P, "_backend", ["Qt Multimedia has no media backend here (its FFmpeg plugin is missing)"])
    found = V.catalog([folder], "en")
    shown = P.open_video("board", found=found)
    try:
        assert isinstance(shown, P.NoPlayer) and "FFmpeg plugin is missing" in shown.text.text()
        before = len(OPENED_URLS)
        shown.open_button.click()
        assert OPENED_URLS[before].toLocalFile() == str(folder / "sandbox-ai-app-02-board-de.mp4")
        assert not shown.isVisible()
    finally:
        shown.close()


def test_the_backend_check_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(P, "_backend", [])
    if P.MISSING:
        assert "Qt Multimedia cannot be loaded" in P.backend_problem()
    else:
        assert P.backend_problem() in ("", P.tr("Qt Multimedia has no media backend here (its FFmpeg plugin is "
                                                "missing)"))


class Sai:
    def __init__(self, directory):
        self.directory = directory

    def version(self):
        return "0.1.94"


def test_without_the_videos_they_are_downloaded_checked_and_the_page_plays(tmp_path, monkeypatch, qapp, wait):
    """End to end: no video here - the offer names where the app looks; Download and check takes them from a local
    fake release signed with a key the test trusts, installs them in the data folder, and the page's video plays."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SANDBOX_AI_VIDEOS", raising=False)
    sai = Sai(tmp_path / "tree" / "sandbox")
    key, trusted = new_key(tmp_path, "release")
    module = load_release_keys()
    release = Release(tmp_path / "release", key, archive_of(GOOD))
    monkeypatch.setattr(V, "release_keys", lambda directory: (module, [trusted]))
    monkeypatch.setattr(V, "install_release", functools.partial(V.install_release, fetcher=release.fetch))
    monkeypatch.setattr(P, "_backend", ["no backend in this test"])
    shown = P.open_video("board", sai=sai)
    try:
        assert isinstance(shown, P.VideoDownload)
        assert str(tmp_path / "data" / "sandbox-ai" / "videos") in shown.text.text()
        assert str(tmp_path / "tree" / "videos") in shown.text.text() and "0.1.94" in shown.text.text()
        assert P.open_video("board", sai=sai) is shown  # one offer at a time
        shown.download_button.click()
        assert wait(lambda: not shown.isVisible(), 20), shown.message.text()
        assert release.asked == ["SHA256SUMS", "SHA256SUMS.sig", V.ARCHIVE]
        assert (tmp_path / "data" / "sandbox-ai" / "videos" / "sandbox-ai-app-02-board-de.mp4").is_file()
        played = [w for w in qapp.topLevelWidgets() if isinstance(w, P.NoPlayer) and w.isVisible()]
        assert played and played[0].video.page == "board"
    finally:
        for widget in qapp.topLevelWidgets():
            if isinstance(widget, (P.NoPlayer, P.VideoDownload)):
                widget.close()


def test_a_download_that_does_not_check_out_installs_nothing_and_says_why(tmp_path, monkeypatch, qapp, wait):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    key, _trusted = new_key(tmp_path, "release")
    _other_key, other = new_key(tmp_path, "other")
    module = load_release_keys()
    release = Release(tmp_path / "release", key, archive_of(GOOD))
    monkeypatch.setattr(V, "release_keys", lambda directory: (module, [other]))
    window = P.VideoDownload(Sai(tmp_path / "tree" / "sandbox"), "board",
                             installer=functools.partial(V.install_release, fetcher=release.fetch))
    window.show()
    try:
        window.download()
        assert wait(lambda: not window.running, 20)
        assert "Not downloaded" in window.message.text() and "not a trusted" in window.message.text()
        assert window.download_button.isEnabled() and window.isVisible()
        folder = tmp_path / "data" / "sandbox-ai" / "videos"
        assert not folder.exists() or not any(folder.iterdir())
    finally:
        window.close()


def test_the_help_page_lists_the_videos_and_plays_one(tmp_path, monkeypatch, qapp):
    folder = series(tmp_path / "videos", pages=(("launch", 1), ("board", 2)))
    monkeypatch.setenv("SANDBOX_AI_VIDEOS", str(folder))
    asked = []
    monkeypatch.setattr(help_view, "VIDEO_OPENER", asked.append)
    page = help_view.HelpPage()
    assert page.show_section(help_view.VIDEOS)
    text = page.text.toPlainText()
    assert "Videos about the pages" in text and "Board" in text and "1:02" in text and "German" in text
    assert 'href="video:board"' in page.text.toHtml() and 'href="section:page-board"' in page.text.toHtml()
    page.link("video:board")
    assert asked == ["board"]
    page.search.setText("Statistik")  # the page titles in both languages find the list
    assert not page.items[help_view.VIDEOS].isHidden()
    page.search.setText("")
    monkeypatch.setenv("SANDBOX_AI_VIDEOS", str(tmp_path / "empty"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    page.show_section(help_view.VIDEOS)
    assert "not on this computer" in page.text.toPlainText() and "download:videos" in page.text.toHtml()
    page.link("download:videos")
    assert asked == ["board", None]
    page.deleteLater()


def test_a_pages_question_mark_offers_its_video(qapp, monkeypatch):
    asked = []
    monkeypatch.setattr(help_view, "VIDEO_OPENER", asked.append)
    owner = QtWidgets.QWidget()
    popover = help_view.show_tip("page.files", owner, QPoint(10, 10))
    assert popover.video.isVisibleTo(popover) and popover.video.text() == "Watch the video"
    popover.video.click()
    assert asked == ["files"]
    other = help_view.show_tip("board.canvas", owner, QPoint(10, 10))
    assert not other.video.isVisibleTo(other)
    other.close()
    owner.deleteLater()


def test_the_window_has_the_video_button_beside_the_pages_question_mark_and_the_menu(window, monkeypatch, qapp):
    asked = []
    monkeypatch.setattr(P, "open_video", lambda page=None, **kw: asked.append(page))
    window.show_page("kits", raise_window=False)
    assert window.page_video.isVisibleTo(window)
    from sandbox_ai_app.widgets import safe_tooltip
    assert window.page_video.toolTip() == safe_tooltip("Watch the video about this page: Kits & plugins")
    window.page_video.click()
    window.actions["page-video"].trigger()
    assert asked == ["kits", "kits"]
    window.actions["videos"].trigger()
    assert window.current_page() == "help" and window.help.shown == help_view.VIDEOS
    assert help_view.VIDEO_SAI == [window.sai] and help_view.VIDEO_OPENER == window.watch_video
    from sandbox_ai_app import ui
    assert [key for key, _ in ui.PAGES] == list(V.PAGE_TITLES)  # every page of the navigation has its video
    assert {key: title for key, title in ui.PAGES} == V.PAGE_TITLES


def test_a_real_video_plays_with_its_sound_track_and_subtitles(qapp, wait):
    """Where QtMultimedia and its FFmpeg backend load (here: a stand-in libpulse may be needed), the series' video
    about the Board plays: its position moves on, it has an audio track, a seek lands, the subtitle of that moment
    is shown, the speed is taken."""
    if P.MISSING or P.backend_problem():
        pytest.skip(f"QtMultimedia cannot play here: {P.MISSING or P.backend_problem()}")
    path = REAL / "sandbox-ai-app-02-board-de.mp4"
    if not path.is_file():
        pytest.skip(f"the rendered series is not in {REAL}")
    found = V.catalog([REAL], "de")
    window = P.VideoPlayer(V.for_page(found, "board"), found)
    window.show()
    try:
        assert wait(lambda: window.player.position() > 300, 20), window.bar.text()
        assert window.player.duration() > 60000 and len(window.player.audioTracks()) == 1
        assert window.position.maximum() == window.player.duration()
        window.subtitles_box.setChecked(True)
        cue = window.subtitles.cues[2]
        window.seek(int((cue.start + 0.5) * 1000))
        assert wait(lambda: window.player.position() >= cue.start * 1000, 10)
        window.show_position(window.player.position())
        assert window.caption.text() == cue.text
        window.speed.setCurrentIndex(P.SPEEDS.index(2.0))
        assert window.player.playbackRate() == 2.0
        window.toggle_play()
        assert wait(lambda: window.player.playbackState() != P.PLAYING, 5)
    finally:
        window.close()


def test_another_page_plays_in_the_player_already_open(made, monkeypatch, tmp_path):
    window, found = made
    monkeypatch.setattr(P, "_backend", [""])
    fewer = [video for video in found if video.page != "help"]
    assert P.open_video("launch", found=fewer) is window
    assert window.video.page == "launch" and window.chooser.count() == 2 and window.chooser.currentIndex() == 0
    window.chooser.activated.emit(1)
    assert window.video.page == "board"
