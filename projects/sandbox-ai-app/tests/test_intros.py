"""The other intro animations - "Glass cube", "Night city" and "Vault" - and the choice between them and the classic
one: their stories over time (pure timelines, no Qt), every frame of each drawn offscreen, their texts fitted into
every window, skipping, reduced motion, and the rotation (a different one at each start, remembered) and the
setting."""
import math

import pytest

from sandbox_ai_app import i18n, intro_choice as IC, intro_timeline as C
from sandbox_ai_app import intro_city_timeline as Y, intro_glass_timeline as G, intro_vault_timeline as V

NEW = {"glass": G, "city": Y, "vault": V}
THEMES = ("light", "dark")


def moments(timeline, step=0.05):
    return [k * step for k in range(int((timeline.DURATION + 0.3) / step) + 1)]


# -- the timelines --

@pytest.mark.parametrize("which", NEW)
def test_the_phases_follow_each_other_and_end_on_the_shared_title_card(which):
    tl = NEW[which]
    assert tl.PHASES[0][1] == 0.0
    for (_, start, end), (_, following, _) in zip(tl.PHASES, tl.PHASES[1:]):
        assert start < end == following
    assert 8.0 <= tl.DURATION <= 9.5, "about as long as the classic one"
    assert tl.PHASES[-2:] == (("title",) + tuple(C.PHASES[-2][1:]), ("out",) + tuple(C.PHASES[-1][1:]))
    seen = [tl.scene(t)["phase"] for t in moments(tl)]
    order = [name for name, _, _ in tl.PHASES]
    assert [name for k, name in enumerate(seen) if k == 0 or seen[k - 1] != name] == order + ["done"]
    assert tl.scene(tl.DURATION)["fade"] == 0.0 and tl.scene(tl.DURATION - 0.6)["fade"] == 1.0
    assert tl.duration(True) == C.STATIC_DURATION and tl.duration() == tl.DURATION
    title = tl.scene(8.1)
    assert title["recede"] == 1 and title["title"] == C.scene(8.1)["title"]
    assert all(v == 1 for v in title["title"].values())
    mark = title["mark"]
    assert (mark["x"], mark["y"], mark["size"]) == C.MARK_TITLE
    assert all(mark[k] == 1 for k in ("square", "shield", "tick", "fill")), "the same mark, complete"
    # three captions, one after the other, inside the story
    assert [index for index, _, _ in tl.CAPTIONS] == [0, 1, 2]
    for (_, start, end), (_, following, _) in zip(tl.CAPTIONS, tl.CAPTIONS[1:]):
        assert start < end <= following
    for index, start, end in tl.CAPTIONS:
        assert tl.scene((start + end) / 2)["caption"]["index"] == index
    assert tl.scene(0.2)["caption"] is None and tl.scene(7.0)["caption"] is None


@pytest.mark.parametrize("which", NEW)
def test_reduced_motion_is_the_same_still_title_card(which):
    tl = NEW[which]
    for t in (0.0, 0.5, 1.2, C.STATIC_DURATION):
        s, classic = tl.scene(t, reduced=True), C.scene(t, reduced=True)
        assert s["reduced"] and s["story"] is None and s["caption"] is None
        for key in ("mark", "title", "recede", "fade", "phase", "progress"):
            assert s[key] == classic[key], (key, t)
    assert tl.scene(0.0, True)["fade"] == 0 and tl.scene(1.0, True)["fade"] == 1
    assert tl.scene(C.STATIC_DURATION, True)["phase"] == "done"


def forward(which, s):
    """The values of intro WHICH's scene S that may only grow."""
    st = s["story"]
    values = [s["progress"], s["recede"], s["title"]["word"], s["mark"]["square"], s["mark"]["fill"]]
    if which == "glass":
        values += [*st["cube"]["edges"], st["cube"]["glass"], st["cube"]["into_mark"], *st["tree"], *st["cross"],
                   *[sig["badge"] for sig in st["signals"]], *st["chips"], st["port"]["in"]]
    elif which == "city":
        values += [*[r["badge"] for r in st["trails"]], *[r["u"] for r in st["trails"]], st["dome"]["draw"],
                   st["dome"]["paused"], st["refused"], st["approval"]["done"], *st["labels"]]
    else:
        values += [*[r["compass"] for r in st["rings"]], *[r["fill"] for r in st["rings"]],
                   st["terminal"]["lines"], st["request"]["u"], st["request"]["badge"], st["chip"]]
    return values


@pytest.mark.parametrize("which", NEW)
def test_progressions_only_go_forward(which):
    tl = NEW[which]
    previous = None
    for t in moments(tl, 0.02):
        s = tl.scene(t)
        if s["story"] is None:
            continue
        now = forward(which, s)
        if previous is not None:
            assert all(b >= a - 1e-9 for a, b in zip(previous, now)), (which, t)
        previous = now
        assert 0 <= s["fade"] <= 1 and 0 <= s["enter"] <= 1 and 0 <= s["skip"] <= 1


def test_glass_a_network_grows_inside_the_cube_and_one_port_lets_out():
    assert all(max(abs(c) for c in node) < 0.8 for node in G.NODES), "the network stays inside"
    assert G.scene(1.2)["story"]["nodes"] == [0.0] * G.NODE_COUNT, "nothing before the cube"
    assert all(f == 1 for f in G.scene(3.5)["story"]["nodes"]), "grown"
    assert all(abs(abs(max(end, key=abs)) - 0.98) < 1e-9 for end, *_ in G.TENDRILS), "each tendril ends at a wall"
    cube = G.cube_state(2.0)
    x, y, _ = G.project((0, 0, 0), cube)
    assert (round(x, 6), round(y, 6)) == G.CENTRE and G.facing("+x", cube) > 0 and G.facing("+z", cube) > 0
    # a touch ripples the wall, the wall holds: the tendril draws back
    face, u, v, when = G.TOUCHES[0]
    touched = G.scene(when + 0.25)["story"]["tendrils"][0]
    assert touched["ripple"] is not None and touched["back"] > 0 and not touched["port"]
    end = G.scene(6.2)["story"]
    states = {sig["host"]: (sig["state"], sig["badge"]) for sig in end["signals"]}
    assert states == {"api.anthropic.com": ("arrived", 1), "pypi.org": ("arrived", 1),
                      "tracker.example.net": ("blocked", 1)}
    blocked = next(k for k, (_, verdict, _) in enumerate(G.SIGNALS) if verdict == "block")
    at_port = G.SIGNALS[blocked][2] + G.TO_PORT
    for t in (at_port + 0.1, at_port + 0.4, at_port + 0.9):
        sig = G.scene(t)["story"]["signals"][blocked]
        assert sig["u"] <= 1 and sig["back"] > 0, "it never passes the port: it bounces back"
    assert G.scene(at_port + 0.1)["story"]["port"]["kind"] == "bad"
    assert G.scene(6.7)["story"]["cube"]["into_mark"] == 1
    assert (G.scene(6.7)["story"]["cube"]["cx"], G.scene(6.7)["story"]["cube"]["cy"]) == C.MARK_TITLE[:2]


def test_city_trails_reach_the_allowed_towers_and_the_canary_stops_the_decoy_key():
    assert all(r == 1 for r in Y.scene(1.75)["story"]["towers"]), "the city stands"
    for k in range(len(Y.HOSTS)):
        assert Y.TOWERS[Y.host_tower(k)][3] == k
    end = Y.scene(6.2)["story"]
    assert [(r["host"], r["state"], r["badge"]) for r in end["trails"]] == [(0, "arrived", 1), (1, "arrived", 1),
                                                                         (2, "arrived", 1)]
    assert all(Y.HOSTS[r["host"]][1] != "canary" for r in end["trails"]), "nothing is sent to the decoy's host"
    ask = next(r for r in end["trails"] if r["verdict"] == "ask")
    waiting = Y.scene(ask["at_beacon"] + 0.3)["story"]
    assert waiting["trails"][2]["state"] == "waiting" and waiting["approval"]["in"] > 0.5
    assert waiting["approval"]["done"] == 0 and waiting["trails"][2]["u"] == 1
    assert Y.scene(ask["at_beacon"] + sum(Y.ASK) + 0.1)["story"]["approval"]["done"] == 1
    appear, walk, take, run, wire = Y.SCRIPT
    before = Y.scene(take - 0.05)["story"]
    assert before["canary"]["key"] == 1 and before["canary"]["alarm"] == 0 and before["dome"]["paused"] == 0
    taken = Y.scene(take + 0.25)["story"]
    assert taken["canary"]["key"] == 0 and taken["script"]["carry"] and taken["canary"]["alarm"] == 1
    caught = Y.scene(wire + 0.15)["story"]
    assert caught["script"]["caught"] and caught["wire"]["flash"] > 0.5 and caught["refused"] > 0
    assert caught["script"]["y"] >= Y.TRIPWIRE_Y - 1e-9, "stopped at the wire, under the beacon"
    assert end["dome"]["paused"] == 1 and end["refused"] == 1 and end["script"]["alpha"] == 0


def test_vault_rings_lock_inside_out_each_stops_something_and_the_key_is_added_outside():
    times = [V.ring_times(k)[3] for k in range(len(V.RINGS))]
    assert times == sorted(times) and V.RINGS == tuple(sorted(V.RINGS)), "inside out"
    for k, lock in enumerate(times):
        before, after = V.scene(lock - 0.02)["story"], V.scene(lock + 0.25)["story"]
        assert not before["rings"][k]["locked"] and after["rings"][k]["locked"]
        assert int(after["terminal"]["lines"]) == k + 2, "the terminal ticks off each check as its ring locks"
    assert V.scene(4.4)["story"]["terminal"]["lines"] == 7
    for k, (_what, ring, angle, _leave) in enumerate(V.PROBES):
        probe = V.probe_state(k, 5.0)
        assert probe["stopped"] and probe["r"] < V.RINGS[ring] - V.BAND / 2, "stopped at the inner edge of its ring"
        rest = V.angle_of(ring, 4.5)
        assert abs(math.remainder(rest - angle, 2 * math.pi)) > V.NOTCH, "it meets the ring, not its notch"
        assert V.scene(probe["arrive"] + 0.1)["story"]["rings"][ring]["hit"] > 0
        assert V.probe_state(k, 6.0)["alpha"] == 0
    leave, at_ring, at_host = V.REQUEST
    for k in range(len(V.RINGS)):
        assert abs(math.remainder(V.angle_of(k, leave) - V.EXIT, 2 * math.pi)) < 1e-9, "the notches line up"
    assert any(abs(math.remainder(V.angle_of(k, 4.8) - V.EXIT, 2 * math.pi)) > V.NOTCH for k in range(5))
    assert V.scene(at_ring - 0.05)["story"]["request"]["key"] == 0, "no key inside the sandbox"
    assert V.scene(at_ring + 0.3)["story"]["request"]["key"] > 0.9
    assert V.scene(6.45)["story"]["request"]["badge"] == 1


# -- the choice --

def test_the_rotation_shows_every_one_and_never_the_same_twice_in_a_row():
    played, last = [], None
    for _ in range(3 * len(IC.IDS)):
        last = IC.pick({}, last)
        played.append(last)
    assert set(played[:len(IC.IDS)]) == set(IC.IDS), "every one is seen, one round each"
    assert all(a != b for a, b in zip(played, played[1:]))
    assert played[0] != "classic", "the first start after the update shows a new one"
    assert IC.following("not-an-intro") == IC.following(None)


@pytest.mark.parametrize("value", [None, "", "fireworks", 3, True, ["vault"], "Rotate"])
def test_an_unknown_setting_rotates(value):
    assert IC.chosen({IC.SETTING: value}) == IC.ROTATE
    assert IC.pick({IC.SETTING: value}, "city") == "vault"


def test_a_chosen_intro_plays_every_time():
    assert IC.chosen({}) == IC.ROTATE
    for which in IC.IDS:
        assert IC.pick({IC.SETTING: which}, which) == which and IC.pick({IC.SETTING: which}, None) == which
    assert [IC.timeline(which) for which in IC.IDS] == [C, G, Y, V] and IC.timeline("nope") is C
    assert IC.CHOICES == (IC.ROTATE, "classic", "glass", "city", "vault")


def test_every_new_text_is_translated():
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import intro_city, intro_glass, intro_vault
    keys = [IC.ROTATE_TITLE, *IC.TITLES.values(), "Intro animation", "Run paused", "Decoy key", "Firejail namespaces",
            "Firewall · proxy", "Seccomp filter", "capabilities dropped, NoNewPrivs", "mount and PID namespaces",
            "rootless Podman or Bubblewrap", "default-deny network, proxy", "agent started",
            "key added by the proxy"]
    english = {}
    for module in (intro_glass, intro_city, intro_vault):
        english[module] = module.captions()
        keys += list(english[module])
    for key in keys:
        assert set(i18n.TEXTS.get(key, {})) == {"en", "de"} and i18n.TEXTS[key]["de"].strip(), key
    i18n.set_language("de")
    try:
        for module in (intro_glass, intro_city, intro_vault):
            german = module.captions()
            assert all(a != b for a, b in zip(english[module], german)), module.__name__
        assert i18n.tr(IC.TITLES["city"]) == "Nachtstadt"
    finally:
        i18n.set_language("en")


# -- drawing --

def renderers(which, mode):
    from sandbox_ai_app import intro
    return intro.renderer_class(which)(mode)


@pytest.mark.parametrize("mode", THEMES)
@pytest.mark.parametrize("size", [(800, 500), (1920, 1080)])
@pytest.mark.parametrize("which", IC.IDS)
def test_every_frame_draws_in_every_size_and_theme(qapp, which, size, mode):
    """Every 1/30 s of the whole run, offscreen, into one image (as the widget draws into its window)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    renderer = renderers(which, mode)
    tl = renderer.TIMELINE
    width, height = size
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    for k in range(int(tl.DURATION * 30) + 2):
        t = k / 30
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            renderer.paint(painter, width, height, tl.scene(t), 1.0)
        finally:
            painter.end()
        if 0.5 < t < 8.0:  # covered: the app is not seen through it
            assert image.pixelColor(width // 2, height // 3).alpha() == 255, (which, t)
    assert image.pixelColor(width // 2, height // 2).alpha() == 0, "at the end the app is seen again"


@pytest.mark.parametrize("which", NEW)
def test_reduced_motion_draws_the_still_card_and_hidpi_is_sharp(qapp, which):
    from sandbox_ai_app import intro
    still = intro.render_image(900, 600, 1.0, "dark", reduced=True, which=which)
    assert still.pixelColor(450, 300).alpha() == 255
    sharp = intro.render_image(900, 600, 5.0, "light", ratio=2.0, which=which)
    assert sharp.width() == 1800 and sharp.devicePixelRatio() == 2.0


@pytest.mark.parametrize("language", i18n.LANGUAGES)
@pytest.mark.parametrize("size", [(800, 500), (800, 560), (1280, 800), (1920, 1080), (3840, 2160), (2560, 1000),
                                  (900, 1200)])
@pytest.mark.parametrize("which", NEW)
def test_every_text_fits_the_window_and_none_overlaps(qapp, which, size, language):
    from PySide6.QtCore import QRectF
    i18n.set_language(language)
    try:
        layout = renderers(which, "dark").LAYOUT(*size)
        canvas = QRectF(0, 0, *size)
        for name, text in layout.texts.items():
            assert canvas.contains(text.rect), (name, text.rect, size)
            assert text.font.pixelSize() >= (14 if name in ("word", "tag", "caption0") else 8), (name, size)
        for group in layout.GROUPS:
            for k, a in enumerate(group):
                for b in group[k + 1:]:
                    assert not layout.texts[a].rect.intersects(layout.texts[b].rect), (a, b, size, language)
        for k in range(3):
            assert layout.texts[f"caption{k}"].font.pixelSize() >= 14, (k, size, language)
        sizes = [layout.texts[name].font.pixelSize() for name in ("word", "tag", "agents")]
        assert sizes == sorted(sizes, reverse=True) and sizes[0] > 2 * sizes[1]
        if which == "glass":
            cx, cw, ch = G.CHIP
            for k in range(len(G.SIGNALS)):
                assert layout.rect(cx, G.CHIP_Y[k] - ch / 2, cw, ch).contains(layout.texts[f"host{k}"].rect)
        elif which == "city":
            card = layout.rect(*Y.CARD)
            for name in ("ask", "deny", "allow"):
                assert card.contains(layout.texts[name].rect), name
            assert layout.rect(*layout.button(1)).contains(layout.texts["allow"].rect)
        else:
            tx, ty, tw, th = V.TERMINAL
            for k in range(7):
                assert layout.rect(tx, ty, tw, th).contains(layout.texts[f"line{k}"].rect), k
            assert layout.rect(*V.CHIP).contains(layout.texts["host"].rect)
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


@pytest.mark.parametrize("which", NEW)
@pytest.mark.parametrize("reduced", [False, True])
def test_it_can_be_skipped_at_every_moment(qapp, window, which, reduced):
    tl = NEW[which]
    for t in [k * 0.5 for k in range(int(tl.duration(reduced) * 2) + 1)]:
        played, done = started(window, reduced=reduced, which=which)
        assert played.which == which and played.timeline is tl and played.duration == tl.duration(reduced)
        played.elapsed = lambda t=t: t
        played.grab()
        played.skip()
        played.skip()
        assert done == [True], (which, t)


@pytest.mark.parametrize("which", NEW)
def test_a_key_or_a_click_skips_it_and_it_ends_by_itself(qapp, window, which):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    played, done = started(window, which=which)
    qapp.sendEvent(played, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert done == [True] and not played.isVisible()
    played, done = started(window, which=which)
    qapp.sendEvent(played, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                       Qt.KeyboardModifier.NoModifier))
    assert done == [True]
    played, done = started(window, which=which)
    played.elapsed = lambda: NEW[which].DURATION
    played.tick()
    assert done == [True]


@pytest.mark.parametrize("which", NEW)
def test_a_broken_frame_ends_it_instead_of_blocking_the_app(qapp, window, monkeypatch, capsys, which):
    played, done = started(window, which=which)

    def broken(*args, **kwargs):
        raise RuntimeError("no paint today")
    monkeypatch.setattr(played.renderer, "paint", broken)
    played.grab()
    qapp.processEvents()
    assert done == [True] and "no paint today" in capsys.readouterr().err


def test_an_unknown_intro_plays_the_classic_one(qapp, window):
    played, done = started(window, which="fireworks")
    assert played.which == "classic" and played.timeline is C
    played.skip()


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None, kind=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def test_each_start_plays_the_next_one_and_remembers_it(qapp, window):
    from sandbox_ai_app import intro
    state, seen = FakeState(), []
    for _ in range(len(IC.IDS) + 1):
        played = intro.play(window, {}, state=state)
        seen.append(played.which)
        assert state.data[IC.STATE_KEY] == played.which
        played.skip()
    assert seen == ["glass", "city", "vault", "classic", "glass"]
    played = intro.play(window, {IC.SETTING: "city"}, state=state)
    assert played.which == "city" and state.data[IC.STATE_KEY] == "city"
    played.skip()
    played = intro.play(window, {IC.SETTING: "nonsense"}, state=state)
    assert played.which == "vault", "an unknown setting rotates on from the last one"
    played.skip()
    assert intro.play(window, {"intro": False}, state=state) is None and state.data[IC.STATE_KEY] == "vault", \
        "switched off: nothing plays, nothing changes"
    played = intro.play(window, {}, state=state, which="glass")
    assert played.which == "glass"
    played.skip()


def test_the_rotation_survives_a_restart(qapp, window, tmp_path):
    """Kept in the window's state.json: the next start (a new State read from the file) goes on from there."""
    from sandbox_ai_app import intro
    from sandbox_ai_app.state import State
    path = tmp_path / "state.json"
    state = State(path)
    first = intro.play(window, {}, state=state)
    first.skip()
    assert state.flush()
    again = State(path)
    assert again.get(IC.STATE_KEY) == first.which
    second = intro.play(window, {}, state=again)
    assert second.which == IC.following(first.which) != first.which
    second.skip()
