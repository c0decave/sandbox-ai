"""The Service part's image list (user 2026-11-19: "apparently there are no services in the Board's drop-down of the
service container, remote or local, though they should be built"). What it offers is the board's host's: your
images there (`sandbox-ai images list`), read again when the host changes - a slow host's late answer never stands
for the next one, a host that does not answer (or an older sandbox-ai) is said as such, never as "none of yours" -
and sandbox-ai's own service Containerfiles (`images templates`: showcase-web), built when a start needs one (asked,
in the tab Preparing the start). Agent images and their variants are not service images: the list says why and
links the Images page."""
import json
import os
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board as B, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

RECORD = {"kind": "pulled", "ref": "redis:7-alpine", "image": "docker.io/library/redis@sha256:" + "a" * 64,
          "digest": "sha256:" + "a" * 64, "id": "b" * 64, "user": "999:1000", "ports": [6379],
          "os": {"ID": "alpine"}, "reviewed": False}
TEMPLATE = {"template": "showcase-web", "description": "nginx 1.28.0 for the showcase presets",
            "image": "showcase-web", "built": []}


def row(name, **extra):
    return {**RECORD, "name": name, "unchecked": True, "used_by": [], "present": True, "base_problem": None, **extra}


class Hosts:
    """The answers of the board's hosts to `sandbox-ai ...` over SSH (host_run), per host: a fake SSH host each."""

    def __init__(self):
        self.gate = threading.Event()
        self.asked, self.on_gui = [], []
        self.lists = {"lab": [row("pg", ports=[5432])], "slow": [row("slowimg")], "fast": [row("fastimg")]}

    def __call__(self, name, *args, timeout=120, input=None):
        if threading.current_thread() is threading.main_thread():
            self.on_gui.append((name, args))
            return 255, "", "run on the GUI thread"
        self.asked.append((name, args))
        if "--dry-run" in args:
            return 0, json.dumps({"agent": args[0], "host": name}), ""
        if name == "down":
            return 255, "", "ssh: connect to host down.example port 22: Connection refused"
        if name == "old" and args[:1] == ("images",) and args[1:2] in (("list",), ("templates",)):
            return 2, "", f"sandbox-ai images: error: argument command: invalid choice: '{args[1]}'"
        if args == ("images", "list", "--json"):
            if name == "slow":
                self.gate.wait(20)
            return 0, json.dumps(self.lists.get(name, [])), ""
        if args == ("images", "templates", "--json"):
            return 0, json.dumps([TEMPLATE]), ""
        if args == ("images", "--json"):
            return 0, "[]", ""
        return 1, "", "not asked in this test"


@pytest.fixture
def home(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    path = tmp_path / "config" / "sandbox-ai" / "user-images.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "images": {"redis": RECORD}}))
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def page(home, monkeypatch, qapp):
    from sandbox_ai_app import board_view as BV
    sai = backend.SandboxAI()
    hosts = Hosts()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": n, "kind": "ssh", "ssh": f"me@{n}.example"}
                                                      for n in ("lab", "slow", "fast", "down", "old")])
    monkeypatch.setattr(sai, "host_run", hosts)
    monkeypatch.setattr(BV, "REMOTE_CHECK_DELAY", 20)
    view = BV.BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False,
                                                            "board_check_delay": 20})
    view.hosts = hosts
    view.wanted = []
    view.images_wanted.connect(lambda: view.wanted.append(True))
    yield view
    hosts.gate.set()
    view.check_timer.stop()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()


def combo_of(page):
    page.inspect(page.board.first("service"))
    return next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox)
                if c.accessibleName() == "Image")


def entries(combo):
    """[(text, data, enabled)] of COMBO's items."""
    model = combo.model()
    return [(combo.itemText(i), combo.itemData(i), bool(model.item(i).isEnabled())) for i in range(combo.count())]


def offered(combo):
    return [data for _, data, enabled in entries(combo) if enabled and isinstance(data, str)
            and not data.startswith("\x00")]


def settle(page, wait, qapp):
    assert wait(lambda: page.user_images is not None and not page.user_images_loading, 30)
    qapp.processEvents()


# -- 1: the board's host, read again on a change, a failure said as one --

def test_the_list_is_the_boards_hosts_and_follows_a_host_change(page, wait, qapp):
    page.add_node("service", **{**B.default_service("db"), "image": ""})
    settle(page, wait, qapp)
    assert "redis" in offered(combo_of(page)) and "pg" not in offered(combo_of(page))
    assert page.host_choice.choose("lab")
    assert wait(lambda: page.user_images is not None and any(r["name"] == "pg" for r in page.user_images), 30)
    qapp.processEvents()
    combo = combo_of(page)
    assert "pg" in offered(combo) and "redis" not in offered(combo), entries(combo)
    assert ("lab", ("images", "list", "--json")) in page.hosts.asked
    assert page.hosts.on_gui == []


def test_a_slow_hosts_late_answer_never_holds_up_or_stands_for_the_next_host(page, wait, qapp):
    page.add_node("service", **{**B.default_service("db"), "image": ""})
    settle(page, wait, qapp)
    assert page.host_choice.choose("slow")
    assert wait(lambda: ("slow", ("images", "list", "--json")) in page.hosts.asked, 20)
    assert page.host_choice.choose("fast")
    # fast's list comes while slow's read still hangs (it was read at once, not after slow's answer)
    assert wait(lambda: page.user_images is not None and [r["name"] for r in page.user_images] == ["fastimg"], 10), \
        page.user_images
    page.hosts.gate.set()  # slow answers now: its list is not fast's
    widgets.ThreadCall.wait_all(30)
    qapp.processEvents()
    assert [r["name"] for r in page.user_images] == ["fastimg"]
    assert "fastimg" in offered(combo_of(page)) and "slowimg" not in offered(combo_of(page))


@pytest.mark.parametrize("host, said", [("down", "Connection refused"), ("old", "update")])
def test_a_host_that_does_not_answer_is_said_never_as_none_of_yours(page, wait, qapp, host, said):
    page.add_node("service", **{**B.default_service("db"), "image": ""})
    settle(page, wait, qapp)
    assert page.host_choice.choose(host)
    assert wait(lambda: page.user_images is not None and page.user_images_failed, 30)
    qapp.processEvents()
    texts = [text for text, _, _ in entries(combo_of(page))]
    assert not any("No images of yours" in t or "No service images" in t for t in texts), texts
    assert any("could not be read" in t and said in t for t in texts), texts
    assert not any(t.startswith("Reading") for t in texts), "never Reading… once the read has ended"
    labels = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("could not be read" in t for t in labels), labels


def test_opening_the_list_reads_it_again(page, wait, qapp, home):
    page.add_node("service", **{**B.default_service("db"), "image": ""})
    settle(page, wait, qapp)
    combo = combo_of(page)
    path = home / "config" / "sandbox-ai" / "user-images.json"
    path.write_text(json.dumps({"version": 1, "images": {"redis": RECORD, "added": RECORD}}))
    page.user_images_read_at = 0.0  # (as a while later)
    page.reread_user_images(combo)
    assert wait(lambda: any(r["name"] == "added" for r in page.user_images or []), 30)
    qapp.processEvents()
    assert "added" in offered(combo_of(page))


# -- 2: sandbox-ai's own templates, built when needed --

@pytest.mark.parametrize("host", ["local", "lab"])
def test_sandbox_ais_own_templates_are_offered_and_pass_the_boards_check(page, wait, qapp, host):
    page.add_node("service", **{**B.default_service("web"), "image": ""})
    if host != "local":
        assert page.host_choice.choose(host)
    assert wait(lambda: page.user_images is not None and page.image_templates is not None
                and not page.user_images_loading, 30)
    qapp.processEvents()
    combo = combo_of(page)
    item = next((text for text, data, enabled in entries(combo) if data == "showcase-web" and enabled), None)
    assert item is not None and "sandbox-ai's own, built when needed" in item, entries(combo)
    combo.setCurrentIndex(combo.findData("showcase-web"))
    combo.activated.emit(combo.currentIndex())
    node = page.board.first("service")
    assert node.props["image"] == "showcase-web"
    page.refresh()
    assert not any("none of your images" in p.text for p in page.problems), [p.text for p in page.problems]
    assert not any("showcase-web" in p.text for p in page.errors())
    page.inspect(node)
    labels = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("built" in t and "when a start needs it" in t for t in labels), labels


def test_a_template_built_as_one_of_yours_is_listed_as_yours(page, wait, qapp, home):
    path = home / "config" / "sandbox-ai" / "user-images.json"
    built = {**RECORD, "kind": "built", "image": "localhost/sandbox-ai-image-showcase-web:latest",
             "builtin": "showcase-web", "ports": [8080], "user": "101:101"}
    path.write_text(json.dumps({"version": 1, "images": {"showcase-web": built, "web": {
        **built, "image": "localhost/sandbox-ai-image-web:latest"}}}))
    page.add_node("service", **{**B.default_service("web"), "image": ""})
    settle(page, wait, qapp)
    combo = combo_of(page)
    texts = {data: text for text, data, _ in entries(combo) if isinstance(data, str)}
    assert "when needed" not in texts["showcase-web"], "built already: one of yours"
    assert "sandbox-ai's showcase-web" in texts["web"]
    assert [d for d in offered(combo)].count("showcase-web") == 1


# -- 3: why agent images are none, and the Images page --

def test_the_list_says_why_agent_images_are_not_service_images_and_links_the_images_page(page, wait, qapp):
    page.add_node("service", **{**B.default_service("db"), "image": "redis"})
    settle(page, wait, qapp)
    combo = combo_of(page)
    notes = [text for text, _, enabled in entries(combo) if not enabled]
    assert any("Agent images and their variants are not service images" in t for t in notes), notes
    add = next(i for i, (text, data, enabled) in enumerate(entries(combo)) if enabled and "Images page" in text)
    combo.setCurrentIndex(add)
    combo.activated.emit(add)
    assert page.wanted == [True]
    assert page.board.first("service").props["image"] == "redis", "the image chosen stays"
    combo = combo_of(page)  # a name typed in stays too
    combo.setEditText("typedimg")
    combo.lineEdit().editingFinished.emit()
    assert page.board.first("service").props["image"] == "typedimg"
    combo = combo_of(page)
    add = next(i for i, (text, data, enabled) in enumerate(entries(combo)) if enabled and "Images page" in text)
    combo.setCurrentIndex(add)
    combo.activated.emit(add)
    assert page.wanted == [True, True] and page.board.first("service").props["image"] == "typedimg"
    assert combo.currentText() == "typedimg"


# -- the start: a template not built is built first (asked), in the tab Preparing the start --

def test_a_start_builds_the_template_first_on_the_boards_host(page, wait, qapp, monkeypatch):
    from sandbox_ai_app import prerequisites
    page.add_node("service", **{**B.default_service("web"), "image": "showcase-web", "ports": [8080]})
    assert page.host_choice.choose("lab")
    assert wait(lambda: page.image_templates is not None and not page.user_images_loading, 30)
    assert page.board_needs().get("--service") == ["showcase-web"]
    tabs = []
    monkeypatch.setattr(page.sai, "host_terminal", lambda host, *args: ["ssh", "-tt", host, "sandbox-ai", *args])
    monkeypatch.setattr(page, "prerequisite_terminal",
                        lambda: lambda argv, title, ended: tabs.append((argv, title, ended)) or True)
    went_on = []
    assert page.check_prerequisites(then=lambda: went_on.append(True))
    assert wait(lambda: tabs, 30)
    argv, title, ended = tabs[0]
    assert title == "Preparing the start"
    steps = json.loads(argv[-1])
    assert [s[0] for s in steps] == ["building the service image showcase-web (sandbox-ai's own Containerfile)"]
    assert steps[0][1] == ["ssh", "-tt", "lab", "sandbox-ai", "images", "build-image", "showcase-web", "--builtin",
                           "showcase-web"], steps
    assert went_on == []
    page.hosts.lists["lab"].append(row("showcase-web", builtin="showcase-web"))  # (built now)
    ended(0)
    assert wait(lambda: went_on == [True], 30), "the start goes on once it is built"


def test_a_start_whose_service_images_are_there_reads_nothing_more(page, wait, qapp):
    page.add_node("service", **{**B.default_service("db"), "image": "redis"})
    assert page.board_needs().get("--service") is None, "only sandbox-ai's own templates are made for a start"


def test_the_start_page_takes_sandbox_ais_own_templates_and_builds_them_first(home, wait, monkeypatch):
    """The Start page's own check of --service (and a preset's image values) takes sandbox-ai's own templates as the
    Board does, and its preparation builds one that is not built (the same Prerequisites)."""
    from sandbox_ai_app.ui import LaunchPage
    page = LaunchPage(backend.SandboxAI(), lambda *a, **k: None)
    page.set_option("--workspace", [str(home)])
    page.set_option("--service", ["web:image=showcase-web,port=8080"])
    went_on = []
    assert page.services_unchecked("Not started", lambda: went_on.append(True)) is True, "read first"
    assert wait(lambda: went_on == [True], 30), [t for _, t, _ in widgets.SHOWN]
    page.set_option("--service", [])
    page.preset_images = lambda: [("Your web server image (sandbox-ai images list)", "showcase-web")]
    page.services_unchecked("Not started", lambda: went_on.append(True))
    assert wait(lambda: went_on == [True, True], 30), [t for _, t, _ in widgets.SHOWN]
    page.set_option("--service", ["web:image=showcase-web,port=8080"])
    assert page.board_needs().get("--service") == ["showcase-web"]
    made = []
    monkeypatch.setattr(page, "provide", lambda missing, unbuilt, host, *a, **k: made.append((missing, host)))
    assert page.check_prerequisites(then=lambda: None)
    assert wait(lambda: made, 30)
    assert made[0] == ({"--service": ["showcase-web"]}, "local")
    page.deleteLater()


def test_board_start_e2e_builds_sandbox_ais_own_image_then_starts(page, wait, qapp, home, monkeypatch):
    """The whole way through the Board's Start: a service of sandbox-ai's own showcase-web, not built here - the
    board's own check lets it pass (no "none of your images"), the preparation asks and builds it in the tab
    Preparing the start (images build-image showcase-web --builtin showcase-web), and the start goes on once it is
    built - with exactly that service."""
    (home / "work").mkdir()
    started = []
    monkeypatch.setattr(page.sai, "host_dry_run", lambda host, agent, argv: ({"agent": agent}, ""))
    monkeypatch.setattr(page, "run_start", lambda argv, *a, **k: started.append(argv) or True)
    tabs = []
    monkeypatch.setattr(page, "prerequisite_terminal",
                        lambda: lambda argv, title, ended: tabs.append((argv, title, ended)) or True)
    page.add_node("workspace", path=str(home / "work"), readonly=False)
    page.add_node("service", **{**B.default_service("web"), "image": "showcase-web", "ports": [8080]})
    page.check()
    assert wait(lambda: page.launcher_ok is True, 30)
    assert page.start() is False, "it waits for the images, then for the build"
    assert wait(lambda: tabs, 30), [t for _, t, _ in widgets.SHOWN]
    argv, title, ended = tabs[0]
    steps = json.loads(argv[-1])
    assert steps[0][1][-5:] == ["images", "build-image", "showcase-web", "--builtin", "showcase-web"]
    # (asked first: the one question about everything the start lacks, sandbox-ai's default fix_questions all)
    asked = [text for title, text, _ in widgets.SHOWN if title == "What the start lacks"]
    assert asked and "the service image showcase-web is not built" in asked[0] and "UNCHECKED" in asked[0]
    assert started == []
    path = home / "config" / "sandbox-ai" / "user-images.json"  # (the build recorded it)
    path.write_text(json.dumps({"version": 1, "images": {"redis": RECORD, "showcase-web": {
        **RECORD, "kind": "built", "image": "localhost/sandbox-ai-image-showcase-web:latest",
        "builtin": "showcase-web", "user": "101:101", "ports": [8080]}}}))
    ended(0)
    assert wait(lambda: started, 30), [t for _, t, _ in widgets.SHOWN]
    assert "web:image=showcase-web,port=8080" in started[0]
    assert not [t for title_, t, _ in widgets.SHOWN if title_ == "Not started"]
