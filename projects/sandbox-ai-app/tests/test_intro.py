"""The intro animation: its story over time (intro_timeline.py, no Qt) and the overlay that draws it, fits its
texts into every window, and is skipped by a click or any key (intro.py)."""
import pytest

from sandbox_ai_app import i18n, intro_timeline as T

SIZES = ((800, 560), (1280, 800), (2560, 1440))
THEMES = ("light", "dark")


def moments(step=0.05, until=T.DURATION + 0.3):
    return [k * step for k in range(int(until / step) + 1)]


# -- the timeline --

def test_the_phases_follow_each_other_and_it_ends():
    assert T.PHASES[0][1] == 0.0
    for (_, start, end), (_, following, _) in zip(T.PHASES, T.PHASES[1:]):
        assert start < end == following
    assert 6.0 <= T.DURATION <= 9.0, "about 6 to 9 seconds"
    order = [name for name, _, _ in T.PHASES]
    seen = [T.phase_at(t) for t in moments()]
    assert [name for k, name in enumerate(seen) if k == 0 or seen[k - 1] != name] == order + ["done"]
    assert T.scene(T.DURATION)["fade"] == 0.0 and T.scene(T.DURATION - 0.6)["fade"] == 1.0
    assert 1.5 <= T.STATIC_DURATION <= 3.0 and T.duration(True) == T.STATIC_DURATION and T.duration() == T.DURATION


def test_progressions_only_go_forward():
    previous = None
    for t in moments(0.02):
        s = T.scene(t)
        now = (s["progress"], s["mark"]["square"], s["mark"]["shield"], s["mark"]["tick"], s["mark"]["fill"],
               s["panel"]["open"], s["code"]["lines"], s["routes"], s["recede"], s["title"]["word"],
               *[r["badge"] for r in s["requests"]], *[r["u"] for r in s["requests"]])
        if previous is not None:
            assert all(b >= a - 1e-9 for a, b in zip(previous, now)), t
        previous = now
        assert 0 <= s["fade"] <= 1 and 0 <= s["enter"] <= 1 and 0 <= s["recede"] <= 1 and 0 <= s["skip"] <= 1
        assert all(0 <= value <= 1 for value in s["chips"]) and 0 <= s["pan"] <= T.PAN


def test_the_easings():
    for ease in (T.ease_out_cubic, T.ease_in_out_cubic, T.ease_in_out_expo):
        assert ease(0) == pytest.approx(0) and ease(1) == pytest.approx(1)
        values = [ease(k / 100) for k in range(101)]
        assert values == sorted(values), ease.__name__
    assert T.spring(0) == 0 and T.spring(1) == 1 and max(T.spring(k / 100) for k in range(101)) > 1, "it overshoots"
    assert T.window(5, 4, 6) == 0.5 and T.window(3, 4, 6) == 0 and T.window(9, 4, 6) == 1


def test_the_story():
    drawn = T.scene(1.75)
    assert all(drawn["mark"][k] == 1 for k in ("square", "shield", "tick", "fill")), "the mark is complete"
    assert drawn["panel"]["open"] < 0.1 and drawn["caption"] is None
    working = T.scene(3.3)
    assert working["panel"]["open"] == 1 and working["agent"]["in"] > 0.9 and working["code"]["lines"] > 2
    assert working["caption"]["index"] == 0 and working["mark"]["header"] == 1 and working["pan"] == T.PAN
    assert T.scene(4.3)["pan"] == 0, "the sandbox makes room for the gate and the destinations"
    end = T.scene(6.4)
    states = {r["host"]: (r["verdict"], r["state"], r["badge"]) for r in end["requests"]}
    assert states["api.anthropic.com"] == ("allow", "arrived", 1) and states["pypi.org"] == ("allow", "arrived", 1)
    assert states["tracker.example.net"] == ("block", "blocked", 1)
    assert states["github.com"] == ("ask", "arrived", 1)
    blocked = next(r for r in end["requests"] if r["verdict"] == "block")
    assert blocked["u"] == 1 and blocked["alpha"] == 0, "it never gets past the gate"
    # the one that asks waits at the gate until it is allowed
    ask = next(i for i, r in enumerate(T.REQUESTS) if r[1] == "ask")
    at_gate = T.REQUESTS[ask][2] + T.LEG[0]
    waiting = T.scene(at_gate + 0.3)
    assert waiting["requests"][ask]["state"] == "waiting" and waiting["approval"]["in"] > 0.5
    assert waiting["approval"]["done"] == 0 and any(kind == "warn" for kind, _ in waiting["gate"]["flash"])
    assert T.scene(at_gate + sum(T.ASK) + 0.15)["approval"]["done"] == 1
    assert [T.scene(t)["caption"]["index"] for t in (3.0, 4.5, 6.0)] == [0, 1, 2]
    title = T.scene(8.1)
    assert title["recede"] == 1 and all(v == 1 for v in title["title"].values())
    assert (title["mark"]["x"], title["mark"]["y"], title["mark"]["size"]) == T.MARK_TITLE


def test_reduced_motion_is_the_static_title_card():
    s = [T.scene(t, reduced=True) for t in (0.0, 0.5, 1.2, T.STATIC_DURATION)]
    assert all(x["reduced"] and x["recede"] == 1 and x["title"] == {"word": 1, "tag": 1, "agents": 1} for x in s)
    assert all(x["mark"]["x"] == T.MARK_TITLE[0] and x["mark"]["fill"] == 1 for x in s), "nothing moves"
    assert s[0]["fade"] == 0 and s[1]["fade"] == 1 and s[-1]["fade"] == 0 and s[-1]["phase"] == "done"
    assert not s[1]["requests"] and s[1]["caption"] is None


def test_the_setting_and_the_environment():
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import intro
    assert intro.wanted({}, {}) and intro.wanted({"intro": True}, {})
    assert not intro.wanted({"intro": False}, {}) and not intro.wanted({}, {"SANDBOX_AI_APP_NO_INTRO": "1"})
    assert intro.reduced_motion({}, {"SANDBOX_AI_APP_REDUCED_MOTION": "1"})
    assert not intro.reduced_motion({}, {}) and not intro.reduced_motion({}, {"SANDBOX_AI_APP_REDUCED_MOTION": "0"})
    assert intro.reduced_motion({"reduced_motion": True}, {})


# -- drawing --

@pytest.mark.parametrize("mode", THEMES)
@pytest.mark.parametrize("size", SIZES)
def test_frames_draw_in_every_size_and_theme(qapp, size, mode):
    from sandbox_ai_app import intro
    renderer = intro.Renderer(mode)
    width, height = size
    for t in (0.0, 0.6, 1.5, 2.1, 3.0, 3.8, 4.6, 5.3, 6.0, 6.6, 7.2, 8.0, 8.5, T.DURATION):
        image = intro.render_image(width, height, t, mode, renderer=renderer)
        assert image.width() == width and image.height() == height
        if 0.5 < t < 8.0:  # covered: the app is not seen through it
            assert image.pixelColor(width // 2, height // 3).alpha() == 255, t
    assert image.pixelColor(width // 2, height // 2).alpha() == 0, "at the end the app is seen again"
    still = intro.render_image(width, height, 1.0, mode, reduced=True, renderer=renderer)
    assert still.pixelColor(width // 2, height // 2).alpha() == 255


def test_frames_are_sharp_on_hidpi(qapp):
    from sandbox_ai_app import intro
    image = intro.render_image(900, 600, 5.0, "dark", ratio=2.0)
    assert image.width() == 1800 and image.devicePixelRatio() == 2.0


def test_the_palette_follows_the_theme(qapp):
    from sandbox_ai_app import intro, theme
    light, dark = intro.Palette("light"), intro.Palette("dark")
    assert not light.dark and dark.dark
    assert light.bg.name() == theme.THEMES_DATA["light"]["colors"]["window"]
    assert dark.accent.name() == theme.THEMES_DATA["dark"]["colors"]["accent"]
    for pal in (light, dark):
        assert theme.contrast(pal.fg, pal.bg) > 7 and theme.contrast(pal.muted, pal.bg) > 3.5
    assert intro.Palette("hacker").dark, "any theme of the app gives a palette"


@pytest.mark.parametrize("language", i18n.LANGUAGES)
@pytest.mark.parametrize("size", SIZES + ((1024, 768), (1920, 1080), (3840, 2160), (2560, 1000), (900, 1200)))
def test_every_text_fits_the_window_and_none_overlaps(qapp, size, language):
    from PySide6.QtCore import QRectF
    from sandbox_ai_app import intro
    i18n.set_language(language)
    layout = intro.Layout(*size)
    canvas = QRectF(0, 0, *size)
    for name, text in layout.texts.items():
        assert canvas.contains(text.rect), (name, text.rect, size)
        assert text.font.pixelSize() >= (14 if name in ("word", "tag", "caption0") else 10), (name, size)
    for group in layout.GROUPS:
        for k, a in enumerate(group):
            for b in group[k + 1:]:
                assert not layout.texts[a].rect.intersects(layout.texts[b].rect), (a, b, size, language)
    # each text inside what holds it
    cx, cw, ch = T.CHIP
    for k in range(len(T.REQUESTS)):
        assert layout.rect(cx, T.CHIP_Y[k] - ch / 2, cw, ch).contains(layout.texts[f"host{k}"].rect)
    card = layout.rect(*T.CARD)
    for name in ("ask", "deny", "allow"):
        assert card.contains(layout.texts[name].rect), name
    assert layout.rect(*layout.button(1)).contains(layout.texts["allow"].rect)
    assert layout.rect(*T.ALLOW).contains(layout.texts["allowlist"].rect)
    assert layout.skip.contains(layout.texts["skip"].rect) and layout.skip.contains(layout.texts["esc"].rect)
    assert canvas.contains(layout.skip)
    # the title's hierarchy: name, tagline, the agents
    sizes = [layout.texts[name].font.pixelSize() for name in ("word", "tag", "agents")]
    assert sizes == sorted(sizes, reverse=True) and sizes[0] > 2 * sizes[1]
    i18n.set_language("en")


def test_every_visible_text_is_translated():
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import intro
    keys = ["Your AI agent works inside a sandbox.", "Every connection goes through the proxy and its allow-list.",
            "Anything else is blocked, or waits for your approval.",
            "Let AI agents work freely. Your machine stays protected.", "Allow-list", "Allow {host}?", "Skip",
            "Watch the intro again", "Sandbox", "Agent", "Proxy", "Allow", "Deny", "Press Esc to skip"]
    for key in keys:
        assert set(i18n.TEXTS[key]) == {"en", "de"} and i18n.TEXTS[key]["de"].strip(), key
    i18n.set_language("de")
    try:
        assert intro.captions()[0] == "Ihr KI-Agent arbeitet in einer Sandbox." and "Rechner" in intro.tagline()
        assert intro.Layout(1280, 800).texts["skip"].text == "Überspringen"
    finally:
        i18n.set_language("en")


# -- the overlay --

@pytest.fixture
def window(qapp):
    from PySide6.QtWidgets import QWidget
    window = QWidget()
    window.resize(900, 600)
    window.show()
    yield window
    window.close()
    window.deleteLater()


def started(window, **options):
    from sandbox_ai_app import intro
    played = intro.Intro(window, **options)
    done = []
    played.finished.connect(lambda: done.append(True))
    played.start()
    return played, done


def test_it_covers_the_window_and_follows_its_size(qapp, window):
    from sandbox_ai_app import intro
    played = intro.play(window, {"intro": True})
    assert played.geometry() == window.rect() and played.isVisible()
    window.resize(1000, 700)
    qapp.processEvents()
    assert played.geometry() == window.rect()
    assert not played.grab().isNull(), "a frame drawn by the widget itself"
    played.skip()
    assert intro.play(window, {"intro": False}) is None
    again = intro.play(window, {"intro": False}, force=True)  # watched again from the menu or the settings
    assert again is not None and again.isVisible()
    again.skip()


@pytest.mark.parametrize("how", ["Escape", "Space", "A", "Return", "click"])
def test_a_click_or_any_key_skips_it_once(qapp, window, how):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    played, done = started(window)
    shift = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.ShiftModifier)
    qapp.sendEvent(played, shift)
    assert done == [], "a modifier alone does not skip it"
    if how == "click":
        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(10, 10), QPointF(10, 10), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    else:
        event = QKeyEvent(QEvent.Type.KeyPress, getattr(Qt.Key, f"Key_{how}"), Qt.KeyboardModifier.NoModifier)
    qapp.sendEvent(played, event)
    assert done == [True] and not played.isVisible()
    played.skip()
    played.tick()
    assert done == [True], "finished only once"


@pytest.mark.parametrize("reduced", [False, True])
def test_it_can_be_skipped_at_every_moment(qapp, window, reduced):
    for t in [k * 0.5 for k in range(int(T.duration(reduced) * 2) + 1)]:
        played, done = started(window, reduced=reduced)
        played.elapsed = lambda t=t: t
        played.grab()  # that moment's frame
        played.skip()
        played.skip()
        assert done == [True], t


def test_it_ends_by_itself(qapp, window):
    played, done = started(window)
    played.tick()
    assert done == []
    played.elapsed = lambda: T.DURATION
    played.tick()
    assert done == [True]
    played, done = started(window, reduced=True)
    assert played.duration == T.STATIC_DURATION
    played.elapsed = lambda: T.STATIC_DURATION
    played.tick()
    assert done == [True]


def test_reduced_motion_from_the_environment(qapp, window, monkeypatch):
    from sandbox_ai_app import intro
    monkeypatch.setenv("SANDBOX_AI_APP_REDUCED_MOTION", "1")
    played = intro.play(window, {})
    assert played.reduced and played.duration == T.STATIC_DURATION
    played.skip()


def test_a_broken_frame_ends_it_instead_of_blocking_the_app(qapp, window, monkeypatch, capsys):
    from sandbox_ai_app import intro
    played, done = started(window)

    def broken(*args, **kwargs):
        raise RuntimeError("no paint today")
    monkeypatch.setattr(played.renderer, "paint", broken)
    played.grab()
    qapp.processEvents()
    assert done == [True] and "no paint today" in capsys.readouterr().err


def test_without_the_intro_the_app_starts_all_the_same(qapp, window, monkeypatch, capsys):
    from sandbox_ai_app import intro

    def broken(*args, **kwargs):
        raise RuntimeError("no intro today")
    monkeypatch.setattr(intro, "Renderer", broken)
    assert intro.play(window, {}) is None and "no intro today" in capsys.readouterr().err
