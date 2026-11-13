"""The app's video player: the videos about its pages (videos.py), with sound, in a window of their own.

QtMultimedia plays them (QMediaPlayer, QAudioOutput, QVideoWidget): play and pause, seek, volume and mute, speed,
full screen, the subtitles of the .vtt beside the video on or off (read by videos.parse_vtt, shown below the
picture), another video of the series. Keys: Space, Left/Right (5 s), Up/Down (volume), M (mute), C (subtitles),
F or F11 (full screen), Esc (full screen off, else the window closes).

Where QtMultimedia cannot be loaded or has no media backend (its FFmpeg plugin; the wheels bring it, a trimmed Qt
may not), or a video cannot be played, it is said so and the file can be opened with the system's player instead.

open_video(page) is what the pages and the Help page call: the video about PAGE when it is on this computer; when
the videos are not, the offer to download them once from the release of this version (VideoDownload: checked
against the release's signed SHA256SUMS before anything is used - videos.install_release).
"""
from __future__ import annotations

import threading
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSlider, QStyle, QToolButton, QVBoxLayout,
    QWidget,
)

from . import videos as V
from .i18n import tr
from .widgets import background, plain_label, safe_tooltip

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaFormat, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MISSING = None
    PLAYING, NO_ERROR = QMediaPlayer.PlaybackState.PlayingState, QMediaPlayer.Error.NoError
except ImportError as exc:  # no QtMultimedia (or a library it needs, such as libpulse, is missing)
    MISSING = str(exc)
    PLAYING, NO_ERROR = "playing", "no error"  # (what a stand-in player of the tests says)

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
SEEK_STEP = 5000  # ms: Left and Right
VOLUME_STEP = 10
SUBTITLES_KEY, VOLUME_KEY = "video_subtitles", "video_volume"  # the app's state: kept between runs
_players = weakref.WeakSet()
_downloads = []  # the one download running (VideoDownload)
_backend = []  # the answer of backend_problem, once


def backend_problem():
    """Why no video can be played in the app ("" when one can): QtMultimedia missing, or without a media backend."""
    if _backend:
        return _backend[0]
    if MISSING:
        why = tr("Qt Multimedia cannot be loaded ({error})", error=MISSING)
    else:
        try:
            formats = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
        except (RuntimeError, AttributeError):
            formats = []
        why = "" if formats else tr("Qt Multimedia has no media backend here (its FFmpeg plugin is missing)")
    _backend.append(why)
    return why


def system_player(path):
    """Open the file PATH with the system's player."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def remembered(key, default, kind):
    from . import state
    kept = state.current()
    return kept.get(key, default, kind) if kept is not None else default


def remember(key, value):
    from . import state
    kept = state.current()
    if kept is not None:
        kept.set(key, value)


def time_text(ms):
    return V.duration_text(max(0, ms) / 1000)


class Media:
    """What plays: QMediaPlayer with its QAudioOutput and a QVideoWidget (tests give one of their own)."""

    def __init__(self):
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.view = QVideoWidget()
        self.player.setVideoOutput(self.view)


class VideoPlayer(QWidget):
    """A video of the series in a window: VIDEO (videos.Video), CATALOG the series to choose another from."""

    def __init__(self, video, catalog=(), media=None):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName("videoPlayer")
        self.media = media or Media()
        self.player, self.audio = self.media.player, self.media.audio
        self.catalog, self.video, self.subtitles, self.seeking = list(catalog) or [video], None, V.Subtitles([]), False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        self.chooser = QComboBox()
        self.chooser.setAccessibleName(tr("Video"))
        self.set_catalog(self.catalog)
        self.chooser.activated.connect(lambda index: self.load(self.catalog[index]))
        top.addWidget(self.chooser, 1)
        self.external = QPushButton(tr("Open with the system player"))
        self.external.setToolTip(tr("Play this video in the program the system plays videos with"))
        self.external.clicked.connect(lambda _=False: self.open_externally())
        top.addWidget(self.external)
        layout.addLayout(top)
        from .terminal_tools import TerminalBar
        self.bar = TerminalBar(self)
        layout.addWidget(self.bar)
        self.view = self.media.view
        self.view.setMinimumSize(320, 180)
        self.view.setAccessibleName(tr("Video"))
        layout.addWidget(self.view, 1)
        self.caption = QLabel()
        self.caption.setObjectName("videoSubtitles")
        self.caption.setTextFormat(Qt.TextFormat.PlainText)  # a subtitle is text, never HTML
        self.caption.setWordWrap(True)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setStyleSheet("QLabel#videoSubtitles { background: #000; color: #fff; padding: 4px; "
                                   "font-size: 15px; }")
        self.caption.setMinimumHeight(self.caption.fontMetrics().lineSpacing() * 2 + 12)
        layout.addWidget(self.caption)
        controls = QHBoxLayout()
        self.play_button = QToolButton()
        self.play_button.clicked.connect(lambda _=False: self.toggle_play())
        controls.addWidget(self.play_button)
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setAccessibleName(tr("Position"))
        self.position.setRange(0, 0)
        self.position.sliderPressed.connect(lambda: setattr(self, "seeking", True))
        self.position.sliderReleased.connect(self.slider_released)
        self.position.sliderMoved.connect(self.seek)
        self.position.actionTriggered.connect(self.slider_action)
        controls.addWidget(self.position, 1)
        self.time = QLabel("0:00 / 0:00")
        self.time.setAccessibleName(tr("Time"))
        controls.addWidget(self.time)
        self.mute_button = QToolButton()
        self.mute_button.setCheckable(True)
        self.mute_button.toggled.connect(self.set_muted)
        controls.addWidget(self.mute_button)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setAccessibleName(tr("Volume"))
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(self.set_volume)
        controls.addWidget(self.volume)
        self.speed = QComboBox()
        self.speed.setAccessibleName(tr("Speed"))
        self.speed.setToolTip(tr("Speed"))
        for value in SPEEDS:
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(SPEEDS.index(1.0))
        self.speed.currentIndexChanged.connect(lambda index: self.set_speed(SPEEDS[index]))
        controls.addWidget(self.speed)
        self.subtitles_box = QCheckBox(tr("Subtitles"))
        self.subtitles_box.setToolTip(tr("Show the narration as text below the picture (C)"))
        self.subtitles_box.setChecked(remembered(SUBTITLES_KEY, False, bool))
        self.subtitles_box.toggled.connect(self.set_subtitles)
        controls.addWidget(self.subtitles_box)
        self.fullscreen_button = QToolButton()
        self.fullscreen_button.setText(tr("Full screen"))
        self.fullscreen_button.setToolTip(tr("Full screen (F); Esc ends it"))
        self.fullscreen_button.clicked.connect(lambda _=False: self.toggle_fullscreen())
        controls.addWidget(self.fullscreen_button)
        layout.addLayout(controls)
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.playbackStateChanged.connect(lambda _state: self.show_state())
        self.player.errorOccurred.connect(self.failed)
        self.volume.setValue(remembered(VOLUME_KEY, 80, int))
        self.set_volume(self.volume.value())
        self.mute_button.setChecked(False)
        self.show_mute()
        from .widgets import close_controls
        close_controls(self)  # an [x] at its top right; Esc closes it (after this window's own keys: KeyFilter)
        self.keys = KeyFilter(self)
        self.resize(980, 640)
        _players.add(self)
        self.load(video)

    def set_catalog(self, found):
        """The series to choose from (the chooser filled again)."""
        self.catalog = list(found)
        self.chooser.blockSignals(True)
        self.chooser.clear()
        for item in self.catalog:
            self.chooser.addItem(self.entry(item), item.page)
        self.chooser.blockSignals(False)

    def entry(self, video):
        text = video.title()
        if video.length():
            text += f" ({video.length()})"
        if video.language != "en" and video.language in V.LANGUAGE_NAMES:
            text += f" - {tr(V.LANGUAGE_NAMES[video.language])}"
        return text

    # -- what plays --

    def load(self, video, play=True):
        self.video = video
        self.setWindowTitle(tr("Video: {title}", title=video.title()))
        index = next((i for i, item in enumerate(self.catalog) if item.page == video.page), -1)
        if index < 0:
            self.catalog.append(video)
            self.chooser.addItem(self.entry(video), video.page)
            index = len(self.catalog) - 1
        self.chooser.setCurrentIndex(index)
        self.subtitles = V.Subtitles(V.read_vtt(video.subtitles) if video.subtitles else [])
        has = bool(self.subtitles.cues)
        self.subtitles_box.setEnabled(has)
        self.subtitles_box.setToolTip(tr("Show the narration as text below the picture (C)") if has else
                                      tr("This video has no subtitles (.vtt) beside it"))
        self.caption.setVisible(self.subtitles_box.isChecked() and has)
        self.caption.setText("")
        self.bar.dismiss()
        self.player.setSource(QUrl.fromLocalFile(str(video.path)))
        self.player.setPlaybackRate(self.speed.currentData() or 1.0)
        if play:
            self.player.play()
        self.show_state()

    def toggle_play(self):
        if self.player.playbackState() == PLAYING:
            self.player.pause()
        else:
            self.player.play()

    def show_state(self):
        playing = self.player.playbackState() == PLAYING
        pixmap = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(pixmap))
        self.play_button.setToolTip(tr("Pause (Space)") if playing else tr("Play (Space)"))
        self.play_button.setAccessibleName(tr("Pause") if playing else tr("Play"))

    def seek(self, ms):
        self.player.setPosition(max(0, int(ms)))
        self.show_position(int(ms))

    def seek_by(self, ms):
        self.seek(min(max(0, self.player.position() + ms), max(0, self.player.duration())))

    def slider_released(self):
        self.seeking = False
        self.seek(self.position.value())

    def slider_action(self, _action):
        if not self.position.isSliderDown():  # a click beside the handle, Page Up/Down, the arrow keys
            QTimer.singleShot(0, lambda: self.seek(self.position.value()))

    def position_changed(self, ms):
        if not self.seeking:
            self.position.blockSignals(True)
            self.position.setValue(ms)
            self.position.blockSignals(False)
        self.show_position(ms)

    def show_position(self, ms):
        self.time.setText(f"{time_text(ms)} / {time_text(self.player.duration())}")
        if self.caption.isVisible():
            self.caption.setText(self.subtitles.at(ms / 1000))

    def duration_changed(self, ms):
        self.position.setRange(0, max(0, ms))
        self.position.setPageStep(max(1000, ms // 20))
        self.show_position(self.player.position())

    def set_volume(self, value):
        self.audio.setVolume(max(0, min(100, int(value))) / 100)
        remember(VOLUME_KEY, int(value))

    def set_muted(self, muted):
        self.audio.setMuted(bool(muted))
        self.show_mute()

    def show_mute(self):
        muted = self.mute_button.isChecked()
        pixmap = QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume
        self.mute_button.setIcon(self.style().standardIcon(pixmap))
        self.mute_button.setToolTip(tr("Sound on (M)") if muted else tr("Mute (M)"))
        self.mute_button.setAccessibleName(tr("Mute"))

    def set_speed(self, rate):
        self.player.setPlaybackRate(float(rate))

    def set_subtitles(self, on):
        remember(SUBTITLES_KEY, bool(on))
        self.caption.setVisible(bool(on) and bool(self.subtitles.cues))
        self.show_position(self.player.position())

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def failed(self, error, text=""):
        if error == NO_ERROR:
            return
        self.bar.ask(tr("This video cannot be played here: {why}", why=text or str(error)),
                     [(tr("Open with the system player"), self.open_externally)], level="error")

    def open_externally(self):
        self.player.pause()
        system_player(self.video.path)

    def closeEvent(self, event):  # noqa: N802 - Qt's name
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)


class KeyFilter(QObject):
    """The player's keys, wherever in its window the focus is (a combo box keeps its own arrows)."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        window.installEventFilter(self)
        for child in window.findChildren(QWidget):
            if not isinstance(child, QComboBox):
                child.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() != QEvent.Type.KeyPress or event.modifiers() not in (Qt.KeyboardModifier.NoModifier,
                                                                             Qt.KeyboardModifier.KeypadModifier):
            return False
        window, key = self.window, event.key()
        if key == Qt.Key.Key_Escape and window.isFullScreen():
            window.showNormal()
        elif key == Qt.Key.Key_Space and not isinstance(watched, (QPushButton, QToolButton, QCheckBox)):
            window.toggle_play()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and not isinstance(watched, QSlider):
            window.seek_by(SEEK_STEP if key == Qt.Key.Key_Right else -SEEK_STEP)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not isinstance(watched, QSlider):
            window.volume.setValue(window.volume.value() + (VOLUME_STEP if key == Qt.Key.Key_Up else -VOLUME_STEP))
        elif key == Qt.Key.Key_M:
            window.mute_button.toggle()
        elif key == Qt.Key.Key_C and window.subtitles_box.isEnabled():
            window.subtitles_box.toggle()
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            window.toggle_fullscreen()
        else:
            return False
        return True


def players():
    from shiboken6 import isValid
    return [p for p in list(_players) if isValid(p)]


# -- what the pages and the Help page call --

def catalog(sai=None, language=None):
    """The videos on this computer (videos.catalog over videos.folders), in the app's language where there is one."""
    from . import help_view, i18n
    sai = sai if sai is not None else (help_view.VIDEO_SAI[0] if help_view.VIDEO_SAI else None)
    return V.catalog(V.folders(getattr(sai, "directory", None)), language or i18n.language())


def open_video(page=None, video=None, sai=None, parent=None, found=None):
    """Play the video about PAGE (or VIDEO) in the player; FOUND: the catalog (else looked up). Without the videos
    on this computer: the offer to download them (VideoDownload); without a player: the system's (NoPlayer).
    Returns the window shown."""
    from . import help_view
    sai = sai if sai is not None else (help_view.VIDEO_SAI[0] if help_view.VIDEO_SAI else None)
    found = catalog(sai) if found is None else found
    video = video or (V.for_page(found, page) if page else (found[0] if found else None))
    if video is None:
        return offer_download(page, sai, parent)
    why = backend_problem()
    if why:
        return show(NoPlayer(video, why))
    window = next((p for p in players() if p.isVisible()), None)
    if window is not None:
        if found:
            window.set_catalog(found)
        window.load(video)
    else:
        window = VideoPlayer(video, found)
    return show(window)


def show(window):
    window.show()
    window.raise_()
    window.activateWindow()
    return window


class NoPlayer(QWidget):
    """No player in the app: why, and the video in the system's player."""

    def __init__(self, video, why):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(tr("Video: {title}", title=video.title()))
        self.video = video
        layout = QVBoxLayout(self)
        self.text = plain_label(tr("The app cannot play videos here: {why}. Open it with the system's player instead?",
                                   why=why), selectable=True)
        layout.addWidget(self.text)
        row = QHBoxLayout()
        row.addStretch(1)
        self.open_button = QPushButton(tr("Open with the system player"))
        self.open_button.setProperty("role", "primary")
        self.open_button.clicked.connect(lambda _=False: self.open_externally())
        close = QPushButton(tr("Close"))
        close.clicked.connect(lambda _=False: self.close())
        row.addWidget(self.open_button)
        row.addWidget(close)
        layout.addLayout(row)
        from .widgets import close_controls
        close_controls(self)
        self.resize(520, 160)

    def open_externally(self):
        system_player(self.video.path)
        self.close()


def offer_download(page, sai, parent=None):
    if _downloads and _downloads[0].alive():
        return show(_downloads[0])
    window = VideoDownload(sai, page)
    _downloads[:] = [window]
    return show(window)


class VideoDownload(QWidget):
    """The videos are not on this computer: where the app looks for them, and their one download from the release of
    this version - checked before anything is used; then the video about PAGE plays."""

    def __init__(self, sai, page=None, installer=None):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(tr("Videos"))
        self.sai, self.page = sai, page
        self.installer = installer or V.install_release  # (tests: a fake release)
        self.version = sai.version() if sai is not None and hasattr(sai, "version") else ""
        self.target = V.data_folder()
        self.progress_state = {"done": 0, "total": None}
        self.stop = threading.Event()
        layout = QVBoxLayout(self)
        places = "\n".join(f"- {folder}" for folder in V.folders(getattr(sai, "directory", None)))
        self.text = plain_label(tr(
            "The videos about the app's pages are not on this computer. The app looks for them in:\n{folders}\n\n"
            "Download them once from the release {version} (about 40 MB)? They are used only after the release's "
            "signed SHA256SUMS checked out: its signature by a release key this sandbox-ai trusts, the archive's "
            "SHA-256, its files.", folders=places, version=self.version or "?"), selectable=True)
        layout.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.message = plain_label(role="secondary", selectable=True)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        row.addStretch(1)
        self.download_button = QPushButton(tr("Download and check"))
        self.download_button.setProperty("role", "primary")
        self.download_button.setToolTip(safe_tooltip(tr("From {source}, into {folder}", source=V.source(),
                                                        folder=self.target)))
        self.download_button.clicked.connect(lambda _=False: self.download())
        self.download_button.setEnabled(bool(self.version))
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(lambda _=False: self.close())
        row.addWidget(self.download_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.show_progress)
        self.running = False
        from .widgets import close_controls
        close_controls(self)
        self.resize(620, 300)

    def alive(self):
        from shiboken6 import isValid
        return isValid(self) and self.isVisible()

    def download(self):
        if self.running:
            return
        self.running = True
        self.stop.clear()
        self.message.setText(tr("Downloading and checking…"))
        self.bar.setRange(0, 0)
        self.bar.show()
        self.download_button.setEnabled(False)
        self.close_button.setText(tr("Cancel"))
        self.timer.start()
        sai, version, target, state = self.sai, self.version, self.target, self.progress_state

        def progress(done, total):
            state["done"], state["total"] = done, total

        def work():
            keys = V.release_keys(getattr(sai, "directory", None))
            return self.installer(target, version, keys, V.source(), progress=progress, cancelled=self.stop.is_set)
        background(work, done=self.finished, owner=self, work=True)

    def show_progress(self):
        done, total = self.progress_state["done"], self.progress_state["total"]
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            self.bar.setFormat(tr("{done} of {total} MB", done=f"{done / 1e6:.1f}", total=f"{total / 1e6:.1f}"))
        elif done:
            self.bar.setFormat(tr("{done} MB", done=f"{done / 1e6:.1f}"))

    def finished(self, names, error):
        self.running = False
        self.timer.stop()
        self.close_button.setText(tr("Close"))
        if error is not None:
            self.bar.hide()
            self.download_button.setEnabled(bool(self.version))
            self.message.setText(tr("Not downloaded: {why}", why=error))
            self.message.setProperty("role", "error")
            return
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.message.setText(tr("{count} files checked and installed in {folder}.", count=len(names),
                                folder=self.target))
        found = catalog(self.sai)
        if self.page and V.for_page(found, self.page) is not None:
            open_video(self.page, sai=self.sai, found=found)
            self.close()

    def closeEvent(self, event):  # noqa: N802 - Qt's name: a download running is cancelled
        self.stop.set()
        super().closeEvent(event)
