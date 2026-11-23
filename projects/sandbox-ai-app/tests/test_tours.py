"""Tours of single areas: every page has one, in both languages, every step points at a control that exists, and
the window starts them from Help, F1, the navigation and the guided tour.

The guard of every tour against the window as it is now (test_every_step_..._is_shown_on_its_page and below): each
step of the guided tour, an area tour or a task tour shows a page that is there, and the control it points at is
there and visible once the step points at it - in English and in German; what the Help tells you to press on a page
is named by a tour of that page; the features that came after the tours were written (toolchains, an LLM server for
all hosts, an MCP server to another host, the switch Board | Expert) each have their step; no tour says something
was not tried out yet; and the README names the tours and the Start page's views as the app does."""
from pathlib import Path
import re
import string

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, guide, help_content, i18n, start_link, tours, ui, widgets  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests

ROOT = Path(__file__).resolve().parents[3]  # the sandbox-ai checkout this app is part of
VIEWS = start_link.VIEW_KEYS
# Controls the window shows only once there are hosts (sandbox-ai hosts): the guard gives the page one.
WITH_HOSTS = {("launch", "host_choice"), ("board", "host_choice"), ("sandboxes", "host_filter"),
              ("mcp", "host_choice")}


def fields(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def every_step():
    """(tour, index, page, target) of every step of every area tour and task tour."""
    found = [(f"area:{page}", index, page, step.target)
             for page, steps in tours.AREA_TOURS.items() for index, step in enumerate(steps)]
    found += [(tour.id, index, step.page, step.target)
              for tour in tours.TASK_TOURS.values() for index, step in enumerate(tour.steps)]
    return found


def every_text():
    """(where, english, german) of every text of every tour: the guided tour's through the app's translation."""
    for page, steps in tours.AREA_TOURS.items():
        for index, step in enumerate(steps):
            for text in (step.title, step.text, step.try_):
                yield f"area:{page}#{index}", text.en, text.de
    for tour in tours.TASK_TOURS.values():
        yield tour.id, tour.title.en, tour.title.de
        yield tour.id, tour.summary.en, tour.summary.de
        for index, step in enumerate(tour.steps):
            for text in (step.title, step.text, step.try_):
                yield f"{tour.id}#{index}", text.en, text.de
    for step in guide.TOUR:
        for text in (step.title, step.text, step.try_):
            yield f"guide:{step.page}", text, (i18n.TEXTS.get(text) or {}).get("de", "")


def words(text):
    """TEXT as a control's name is written in a sentence: no mnemonic, no ellipsis, no closing colon."""
    return text.replace("&", "").replace("…", "").rstrip(":").strip()


def test_every_page_has_a_tour_in_both_languages():
    assert set(tours.AREA_TOURS) == {key for key, _ in ui.PAGES}
    for page, steps in tours.AREA_TOURS.items():
        assert steps, page
        for step in steps:
            for text in (step.title, step.text, step.try_):
                assert text.en.strip() and text.de.strip(), (page, text)
                assert text.en != text.de or text.en in ("Kits", "Live"), (page, text)  # the same word in both
                assert fields(text.en) == fields(text.de), (page, text)
    i18n.set_language("de")
    try:
        assert tours.say(tours.AREA_TOURS["images"][1].title) == "Bauen"
    finally:
        i18n.set_language("en")


def test_every_text_of_every_tour_is_in_both_languages_and_none_says_it_was_not_tried():
    """The guided tour's texts too (they are translated by i18n.TEXTS); and since 0.1.211 nothing of the app says
    "not live-tested yet": a tour neither."""
    for where, en, de in every_text():
        assert en.strip() and de.strip(), (where, en)
        same = ("Kits", "Live", "Start", "Board", "Terminals", "Teams", "System", "Hosts", "Images")  # in both
        assert en != de or en in same, (where, en)
        for text in (en, de):
            assert not re.search(r"live[- ]tested|not tested|nicht live|ungetestet|noch nicht getestet", text, re.I), \
                (where, text)


@pytest.fixture(params=i18n.LANGUAGES)
def tour_window(request, qapp, env, monkeypatch):  # noqa: F811
    """The window in each of the app's languages, shown at a laptop's size, with one host (sandbox-ai hosts)."""
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    i18n.set_language(request.param)
    try:
        w = ui.MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        if w.tray is not None:
            w.tray.hide()
            w.tray = None
        w.resize(1400, 900)
        w.show()
        qapp.processEvents()
        yield w
        w.shutdown()
        w.close()
        w.deleteLater()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    finally:
        i18n.set_language("en")


def give_a_host(window, page, target):
    """The control TARGET of PAGE that the window shows only with hosts, as with the host build1."""
    widget = getattr(getattr(window, page), target)
    if target == "host_filter":
        window.sandboxes.remote_names = ["build1"]
        window.sandboxes.fill_host_filter()
    elif widget.findData("build1") < 0:
        widget.addItem("build1", "build1")
    widget.setVisible(True)


@pytest.mark.parametrize("page", sorted({page for _tour, _index, page, _target in every_step()}))
def test_every_step_of_every_tour_points_at_a_control_that_is_shown_on_its_page(tour_window, qapp, page):
    """Each step's page is shown as the step asks for it (a view of the Start page as that view, the switch saying
    so), and the control it points at exists and is visible there once the step points at it (its tab brought to
    the front, its panel opened, its fold opened)."""
    window = tour_window
    for tour, index, step_page, target in every_step():
        if step_page != page:
            continue
        where = (i18n.language(), tour, index, target)
        if page in VIEWS:  # reached from the other view, as a tour's next step may
            window.show_page(next(view for view in VIEWS if view != page))
        window.show_page(page)
        qapp.processEvents()
        assert window.current_page() == page, where
        if page in VIEWS:
            assert window.view_switch.isVisible() and window.view_switch.view() == page, where
        if target is None:
            continue
        widget = getattr(getattr(window, page), target, None)
        assert isinstance(widget, QtWidgets.QWidget), f"{where}: the page has no control {target}"
        if (page, target) in WITH_HOSTS:
            give_a_host(window, page, target)
        window.point_at(page, target)
        qapp.processEvents()
        assert widget.isVisible(), f"{where}: {target} is not shown when the step points at it"
        assert window.spotlight.target is widget and window.spotlight.isVisible(), where
        window.point_at(page, "")
    assert window.start_view == start_link.DEFAULT_VIEW, "a tour shows a view; it chooses none"


def test_every_page_of_the_guided_tour_is_there_and_leads_to_its_area_tour(window, qapp):  # noqa: F811
    window.resize(1400, 900)
    window.show()
    for index, step in enumerate(guide.TOUR):
        window.start_tour(index)
        qapp.processEvents()
        assert window.current_page() == step.page, (index, step.page)
        assert step.page in tours.AREA_TOURS, step.page
    window.tour.hide()


def named_in(page, target, en, de):
    """Whether a tour step on PAGE names its control TARGET called EN (DE in German): points at it, or says its
    name."""
    steps = [step for step in tours.AREA_TOURS.get(page, ())]
    steps += [step for tour in tours.TASK_TOURS.values() for step in tour.steps if step.page == page]
    if any(step.target == target for step in steps):
        return True
    said_en = " ".join(f"{step.title.en} {step.text.en} {step.try_.en}" for step in steps).replace("…", "")
    said_de = " ".join(f"{step.title.de} {step.text.de} {step.try_.de}" for step in steps).replace("…", "")
    return en in said_en and de in said_de


def test_what_the_help_tells_you_to_press_on_a_page_a_tour_of_that_page_names(window):  # noqa: F811
    """A button of a page whose name a Help section's "How do I…" uses (the section's screenshot is of that page) is
    named by a tour of that page, in both languages - so a control the Help sends you to is never one no tour
    knows. Names are compared as written in a sentence ("Add…" is "Add")."""
    missing = []
    for section in help_content.SECTIONS:
        page = getattr(window, section.shot, None)
        if page is None:
            continue
        steps_en = " ".join(step.en for task in section.tasks for step in task.steps).replace("…", "")
        steps_de = " ".join(step.de for task in section.tasks for step in task.steps).replace("…", "")
        for name, control in vars(page).items():
            if not isinstance(control, QtWidgets.QAbstractButton) or not control.text():
                continue
            en = words(control.text())
            de = words((i18n.TEXTS.get(control.text()) or {}).get("de", control.text()))
            # a name of two words or more, or one ending in an ellipsis (a dialog's button): never a word such as
            # "Add" or "Check" that the steps use as a verb
            if len(en.split()) < 2 and not control.text().endswith("…"):
                continue
            if re.search(rf"\b{re.escape(en)}\b", steps_en) and re.search(rf"\b{re.escape(de)}\b", steps_de) \
                    and not named_in(section.shot, name, en, de):
                missing.append(f"{section.id}: {section.shot}.{name} ({en} / {de})")
    assert not missing, "the Help names controls no tour of their page names:\n" + "\n".join(sorted(set(missing)))


# What came to the window after the tours were written, and the step (page, control) a user looks for it at.
NEWER = {
    "the switch Board | Expert": (("launch", "view_switch"), ("board", "view_switch")),
    "toolchains on the Board": (("board", "palette"),),
    "toolchains of a running sandbox": (("sandboxes", "toolchain_button"),),
    "an LLM server for all hosts": (("llm", "share_button"),),
    "an MCP server from another host": (("mcp", "copy_button"),),
    "an MCP server to another host": (("mcp", "copy_to_button"),),
}
NEWER_WORDS = {  # what the step's text says (English, German)
    "toolchains on the Board": ("Toolchains", "Toolchains"),
    "toolchains of a running sandbox": ("Add toolchain", "Toolchain hinzufügen"),
    "an LLM server for all hosts": ("For all hosts", "Für alle Hosts"),
    "an MCP server from another host": ("From another host", "Von einem anderen Host"),
    "an MCP server to another host": ("Copy NAME to another host", "NAME auf einen anderen Host kopieren"),
}


def storyboard():
    """docs/video/storyboard.py: the videos of the app's pages (data only)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("video_storyboard", ROOT / "docs" / "video" / "storyboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# What the video of a page says about what came after the tours (English, German): its storyboard names the control
# as the tour does.
NEWER_VIDEOS = {
    "launch": (("Expert", "Experte"), ("F6", "F6")),
    "board": (("Expert", "Experte"), ("Toolchains", "Toolchains")),
    "sandboxes": (("toolchain", "Toolchain"),),
    "terminals": (("While it runs", "Während sie läuft"),),
    "mcp": (("From another host", "Von einem anderen Host"), ("to another host", "auf einen anderen Host")),
    "llm": (("For all hosts", "Für alle Hosts"),),
}


def test_what_came_after_the_tours_has_its_step(window):  # noqa: F811
    for feature, places in NEWER.items():
        for page, target in places:
            assert isinstance(getattr(getattr(window, page), target, None), QtWidgets.QWidget), (feature, target)
            steps = [step for step in tours.AREA_TOURS[page] if step.target == target]
            assert steps, f"{feature}: the tour of {page} has no step at {target}"
            if feature in NEWER_WORDS:
                en, de = NEWER_WORDS[feature]
                assert any(en in step.text.en + step.title.en and de in step.text.de + step.title.de
                           for step in steps), (feature, page, target)
    terminals = " ".join(step.text.en for step in tours.AREA_TOURS["terminals"])
    assert "toolchain" in terminals, "a terminal tab's menu has the running sandbox's actions, toolchains among them"
    videos = {video["page"]: video for video in storyboard().SERIES["pages"]["videos"]}
    for page, said in NEWER_VIDEOS.items():
        texts = list(texts_of(videos[page]))
        for en, de in said:
            assert any(en in text["en"] and de in text["de"] for text in texts), (page, en, de)


def texts_of(value):
    """The texts ({"en", "de"}) anywhere in VALUE, a part of the storyboard."""
    if isinstance(value, dict) and set(value) == {"en", "de"}:
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from texts_of(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from texts_of(item)


def test_the_readme_names_the_tours_and_the_start_pages_views_as_the_app_does():
    """README "A look at the app" and its quick start: the Start page's two views by their titles on the switch and
    its key, and Help → Tours with the task tours it walks through and F1 for the tour of a page."""
    for name, language in (("README.md", "en"), ("README_DE.md", "de")):
        text = (ROOT / name).read_text(encoding="utf-8")
        start = text.index("## A look at the app" if language == "en" else "## Ein Blick in die App")
        look = text[start:text.index("\n## ", start)]
        for key, title, _what in start_link.VIEWS:
            word = (i18n.TEXTS.get(title) or {}).get(language, title)
            assert f"**{word}**" in look or f"als {word}**" in look or f"as {word}**" in look, (name, word)
        assert start_link.SWITCH_KEYS in look, name
        tours_line = "Help → Tours" if language == "en" else "Hilfe → Touren"
        assert tours_line in look, name
        after = look[look.index(tours_line):][:200]
        for en, de in (("first start", "ersten Start"), ("preset", "Vorlage"), ("approvals", "Freigaben"),
                       ("F1", "F1")):
            assert (en if language == "en" else de) in " ".join(after.split()), (name, en)


def test_an_area_tour_outlines_each_control_and_finishes(window, qapp):  # noqa: F811
    window.resize(1280, 860)
    window.show()
    window.start_area_tour("images")
    panel = window.area_tour
    assert window.current_page() == "images" and window.tour_dock.isVisible()
    assert panel.where.text() == "Tour of Images · 1 of 5" and panel.title.text() == "The images"
    qapp.processEvents()
    spot = window.spotlight
    assert spot.isVisible() and spot.target is window.images.images
    panel.go(3)  # the extension's tabs: on the tab widget of the lower half
    qapp.processEvents()
    assert spot.target is window.images.editor_tabs and window.images.tabs.currentIndex() == 1  # its tab shown
    target = window.images.editor_tabs
    corner = target.mapTo(window, target.rect().topLeft())
    assert spot.geometry().contains(corner)  # around the control
    panel.next.click()
    assert panel.next.text() == "Finish"
    panel.next.click()
    qapp.processEvents()
    assert not window.tour_dock.isVisible() and not spot.isVisible()


def test_tours_start_from_help_f1_the_navigation_and_the_guided_tour(window):  # noqa: F811
    window.show_page("connections", raise_window=False)
    window.actions["area-tour"].trigger()  # F1
    assert window.area_tour.page == "connections"
    assert [key.toString() for key in window.actions["area-tour"].shortcuts()] == ["F1"]
    window.actions["area-tour:hosts"].trigger()
    assert window.area_tour.page == "hosts" and window.current_page() == "hosts"
    window.start_tour(2)  # the guided tour's step "Sandboxes & sessions"
    window.tour.area.click()
    assert window.area_tour.page == "sandboxes" and not window.tour.isVisible()
