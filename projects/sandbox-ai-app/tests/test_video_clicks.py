"""Every click about the videos visibly does something (user 2026-11-22: "Videos zu dieser Seite anklicken passiert
nichts, und wenn man Videos herunterladen aus dem Release drückt passiert auch nichts") - through the real window, as
an installed app sees it: no videos on this computer, the version of a release that may not be published.

- The windows the clicks open (the offer to download, the player, the player of the system) float over the window
  they were asked from: a window without a transient parent opened behind a full-screen or maximized main window on
  a tiling window manager (spectrwm), and a second click only raised the hidden one - nothing to be seen.
- A release that is not published (yet), or has no videos, is said so, and the newest published release is offered.
- Offline, a missing file, a bad signature: said in the window, in the colour of an error.
- The system's player that does not open: said, the window stays.
- An error while the window is made: said in a message, not only logged.
- After a download from the Help page's list, the list shows the videos.
- The Help section about the videos leads to the list."""
import functools

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from sandbox_ai_app import help_content, help_view, i18n, video_player as P, videos as V, widgets  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests
from test_videos import GOOD, Release, archive_of, load_release_keys, new_key, series  # noqa: E402

VERSION = "0.1.202"  # an installed version whose release is not published where the videos are fetched from


def offers():
    return [w for w in QtWidgets.QApplication.topLevelWidgets()
            if isValid(w) and w.isVisible() and isinstance(w, (P.VideoDownload, P.NoPlayer, P.VideoPlayer))]


def close_offers():
    for widget in list(QtWidgets.QApplication.topLevelWidgets()):
        if isValid(widget) and isinstance(widget, (P.VideoDownload, P.NoPlayer, P.VideoPlayer)):
            widget.close()
    P._downloads[:] = []


@pytest.fixture
def app_window(window, env, monkeypatch, qapp):
    """The main window shown, with no video anywhere and this version not published."""
    monkeypatch.delenv("SANDBOX_AI_VIDEOS", raising=False)
    monkeypatch.setattr(V, "folders", lambda directory=None, environ=None: [V.data_folder(environ)])
    monkeypatch.setattr(window.sai, "version", lambda: VERSION)
    monkeypatch.setattr(P, "_backend", ["no backend in this test"])
    close_offers()
    window.show()
    qapp.processEvents()
    yield window
    close_offers()


def floats_over(shown, owner):
    handle = shown.windowHandle()
    return shown.isWindow() and handle is not None and handle.transientParent() == owner.windowHandle()


def click_link(browser, text):
    """A real mouse click on the link with TEXT in the QTextBrowser BROWSER."""
    cursor = browser.document().find(text)
    assert not cursor.isNull(), text
    point = browser.cursorRect(cursor).center()
    QTest.mouseClick(browser.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)


# -- the windows float over the main window --

def test_every_entry_point_opens_the_offer_over_the_window(app_window, qapp, wait):
    """The film beside the page's (?), Help > Watch the video about this page, the (?)'s Watch the video, the Help
    page's link: the one offer, shown over the main window (its transient parent) - also when it was minimized."""
    window = app_window
    window.show_page("kits", raise_window=False)
    QTest.mouseClick(window.page_video, Qt.MouseButton.LeftButton)
    shown = offers()
    assert [type(w) for w in shown] == [P.VideoDownload] and shown[0].page == "kits"
    offer = shown[0]
    assert floats_over(offer, window), "the offer has the main window as its transient parent"
    offer.showMinimized()
    qapp.processEvents()
    window.actions["page-video"].trigger()
    assert offers() == [offer] and not offer.isMinimized()
    popover = help_view.show_tip("page.kits", window, QPoint(10, 10))
    popover.video.click()
    assert offers() == [offer]
    offer.close()
    assert wait(lambda: not offers())
    window.actions["videos"].trigger()
    assert window.current_page() == "help" and window.help.shown == help_view.VIDEOS
    click_link(window.help.text, P.tr("Download them from the release…"))
    shown = offers()
    assert [type(w) for w in shown] == [P.VideoDownload] and floats_over(shown[0], window)


def test_from_a_modal_dialog_the_offer_floats_over_the_dialog(app_window, qapp):
    """Asked from inside a dialog that blocks the window, the offer floats over that dialog (over the window it
    would be behind the dialog's veil, and blocked with the window)."""
    window = app_window
    dialog = QtWidgets.QDialog(window)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.show()
    qapp.processEvents()
    try:
        help_view.watch_video("files")
        shown = offers()
        assert [type(w) for w in shown] == [P.VideoDownload] and floats_over(shown[0], dialog)
    finally:
        dialog.close()


# -- the download: a release not published, failures said --

def fake_release(tmp_path, monkeypatch, published=("0.1.94",), latest="0.1.94", trusted_key=True):
    """GitHub as the installed app sees it: the releases PUBLISHED (0.1.94: signed, with the videos), the newest
    LATEST; a release not published answers 404 (VideoError missing)."""
    key, trusted = new_key(tmp_path, "release")
    _other, other = new_key(tmp_path, "other")
    module = load_release_keys()
    release = Release(tmp_path / "release", key, archive_of(GOOD))
    asked = []

    def fetcher(url, limit, **kwargs):
        version = url.split("/releases/download/v", 1)[1].split("/", 1)[0]
        asked.append((version, url.rsplit("/", 1)[-1]))
        if version not in published:
            raise V.VideoError(f"The release has no {url.rsplit('/', 1)[-1]} ({url}).", missing=True)
        return release.fetch(url.replace(f"/v{version}/", "/v0.1.94/"), limit, **kwargs)
    monkeypatch.setattr(V, "release_keys", lambda directory: (module, [trusted if trusted_key else other]))
    monkeypatch.setattr(V, "install_release", functools.partial(V.install_release, fetcher=fetcher))
    monkeypatch.setattr(V, "latest_release", lambda repository=None, **kwargs: latest)
    return asked


def download(offer, wait):
    QTest.mouseClick(offer.download_button, Qt.MouseButton.LeftButton)
    assert wait(lambda: not offer.running, 20), offer.message.text()


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_release_not_published_is_said_and_the_newest_is_offered(app_window, tmp_path, monkeypatch, qapp, wait,
                                                                  language):
    i18n.set_language(language)
    window = app_window
    asked = fake_release(tmp_path, monkeypatch)
    window.show_page("board", raise_window=False)
    QTest.mouseClick(window.page_video, Qt.MouseButton.LeftButton)
    offer = offers()[0]
    assert VERSION in offer.text.text()
    download(offer, wait)
    said = offer.message.text()
    if language == "de":
        assert f"Das Release {VERSION} gibt es auf {V.source()} nicht" in said and "(noch) nicht veröffentlicht" in said
        assert offer.other_button.text() == "Die Videos von 0.1.94 herunterladen"
    else:
        assert f"The release {VERSION} is not on {V.source()}" in said and "not published there (yet)" in said
        assert offer.other_button.text() == "Download the videos of 0.1.94"
    assert offer.message.property("role") == "notice-error" and offer.message.isVisible()
    assert offer.other_button.isVisible() and offer.isVisible()
    assert asked == [(VERSION, "SHA256SUMS")]
    QTest.mouseClick(offer.other_button, Qt.MouseButton.LeftButton)
    assert wait(lambda: not isValid(offer) or not offer.isVisible(), 20), offer.message.text()
    assert asked[1:] == [("0.1.94", "SHA256SUMS"), ("0.1.94", "SHA256SUMS.sig"), ("0.1.94", V.ARCHIVE)]
    assert (V.data_folder() / "sandbox-ai-app-02-board-de.mp4").is_file()
    played = offers()
    assert [type(w) for w in played] == [P.NoPlayer] and played[0].video.page == "board"
    assert floats_over(played[0], window)


def test_without_a_newer_release_only_the_reason_is_said(app_window, tmp_path, monkeypatch, wait):
    fake_release(tmp_path, monkeypatch, published=(), latest=VERSION)
    app_window.watch_video("board")
    offer = offers()[0]
    download(offer, wait)
    assert "not published there (yet)" in offer.message.text() and not offer.other_button.isVisible()
    assert offer.download_button.isEnabled()


@pytest.mark.parametrize("case", ["offline", "no videos", "bad signature"])
def test_a_failure_is_said_in_the_window(app_window, tmp_path, monkeypatch, wait, case):
    asked = fake_release(tmp_path, monkeypatch, published=(VERSION,), latest=VERSION,
                         trusted_key=case != "bad signature")
    if case == "offline":
        def offline(*args, **kwargs):
            raise V.VideoError("Cannot download https://github.com/...: [Errno -3] Temporary failure in name "
                               "resolution")
        monkeypatch.setattr(V, "install_release", offline)
    elif case == "no videos":
        def no_videos(target, version, keys, repository=None, **kwargs):
            raise V.VideoError(V.tr("The release {version} has no videos (its SHA256SUMS lists no {name}).",
                                    version=version, name=V.ARCHIVE), missing=True)
        monkeypatch.setattr(V, "install_release", no_videos)
    app_window.watch_video("board")
    offer = offers()[0]
    download(offer, wait)
    said = offer.message.text()
    assert said.startswith("Not downloaded:") and offer.message.isVisible() and offer.isVisible()
    assert offer.message.property("role") == "notice-error" and not offer.bar.isVisible()
    assert offer.download_button.isEnabled() and offer.close_button.text() == "Close"
    expected = {"offline": "name resolution", "no videos": "has no videos", "bad signature": "not a trusted"}[case]
    assert expected in said
    assert not offer.other_button.isVisible()
    if case == "bad signature":
        assert asked == [(VERSION, "SHA256SUMS"), (VERSION, "SHA256SUMS.sig")]


def test_the_progress_is_shown_while_it_downloads(app_window, monkeypatch, wait, qapp):
    import threading
    go = threading.Event()

    def slow(target, version, keys, repository=None, progress=None, cancelled=None, **kwargs):
        progress(5_000_000, 40_000_000)
        go.wait(10)
        raise V.VideoError("stopped by the test")
    monkeypatch.setattr(V, "install_release", slow)
    monkeypatch.setattr(V, "release_keys", lambda directory: (None, []))
    app_window.watch_video("board")
    offer = offers()[0]
    QTest.mouseClick(offer.download_button, Qt.MouseButton.LeftButton)
    try:
        assert offer.message.text() == "Downloading and checking…" and offer.bar.isVisible()
        assert wait(lambda: offer.bar.format() == "5.0 of 40.0 MB", 5), offer.bar.format()
        assert not offer.download_button.isEnabled() and offer.close_button.text() == "Cancel"
    finally:
        go.set()
    assert wait(lambda: not offer.running, 10)


# -- the system's player --

def test_a_system_player_that_does_not_open_is_said(app_window, tmp_path, monkeypatch, qapp):
    folder = series(tmp_path / "videos", pages=(("launch", 1), ("board", 2)))
    monkeypatch.setattr(V, "folders", lambda directory=None, environ=None: [folder])
    monkeypatch.setattr(P.QDesktopServices, "openUrl", lambda url: False)
    app_window.watch_video("board")
    shown = offers()
    assert [type(w) for w in shown] == [P.NoPlayer] and floats_over(shown[0], app_window)
    QTest.mouseClick(shown[0].open_button, Qt.MouseButton.LeftButton)
    assert shown[0].isVisible(), "the window stays: nothing was opened"
    assert "sandbox-ai-app-02-board-de.mp4" in shown[0].text.text()
    assert "No program to play videos with could be started" in shown[0].text.text()


def test_the_players_system_player_that_does_not_open_is_said(tmp_path, monkeypatch, qapp):
    from test_video_player import FakeMedia
    folder = series(tmp_path / "videos", pages=(("launch", 1), ("board", 2)))
    found = V.catalog([folder], "en")
    monkeypatch.setattr(P.QDesktopServices, "openUrl", lambda url: False)
    player = P.VideoPlayer(V.for_page(found, "board"), found, media=FakeMedia())
    player.show()
    try:
        player.external.click()
        assert "No program to play videos with could be started" in player.bar.label.text()
    finally:
        player.close()


# -- an error while the window is made --

def test_an_error_on_the_way_is_said_not_only_logged(app_window, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("the data folder cannot be read")
    monkeypatch.setattr(V, "catalog", broken)
    QTest.mouseClick(app_window.page_video, Qt.MouseButton.LeftButton)
    assert [(title, text) for title, text, _ in widgets.SHOWN] == [
        ("Videos", "The video cannot be shown: the data folder cannot be read")]


# -- the Help page --

def test_after_a_download_from_the_help_page_its_list_shows_the_videos(app_window, tmp_path, monkeypatch, wait):
    fake_release(tmp_path, monkeypatch, published=(VERSION,), latest=VERSION)
    window = app_window
    window.actions["videos"].trigger()
    click_link(window.help.text, P.tr("Download them from the release…"))
    offer = offers()[0]
    assert offer.page is None
    download(offer, wait)
    assert "checked and installed" in offer.message.text() and offer.isVisible()
    assert 'href="video:board"' in window.help.text.toHtml(), "the list was read again"


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_help_section_about_the_videos_leads_to_the_list(app_window, language):
    i18n.set_language(language)
    window = app_window
    window.show_help("app-videos")
    html = window.help.text.toHtml()
    assert 'href="section:videos"' in html
    title = "Videos zu den Seiten" if language == "de" else "Videos about the pages"
    words = "Die Liste der Videos" if language == "de" else "The list of the videos"
    assert words in window.help.text.toPlainText()
    click_link(window.help.text, words)
    assert window.help.shown == help_view.VIDEOS and title in window.help.text.toPlainText()
    assert help_content.BY_ID["app-videos"]


def test_a_version_that_cannot_be_read_is_said(qapp):
    class Broken:
        directory = None

        def version(self):
            raise ImportError("the launcher does not load")
    offer = P.VideoDownload(Broken(), "board")
    try:
        assert not offer.download_button.isEnabled()
        assert offer.message.text() == ("The version of this sandbox-ai is not known, so there is no release to "
                                        "download the videos from.")
        assert offer.message.property("role") == "notice-error"
    finally:
        offer.close()
