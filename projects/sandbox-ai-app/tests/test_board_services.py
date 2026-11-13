"""Services (--service) and your own images in the app: the board's Service part (options -> part -> options, its
problems and its score), its properties in the inspector, the engine's variant on a base image of yours, and the
Images page's Your images (list, pull, build, remove, a variant on your image)."""
import json
import os

import pytest

from sandbox_ai_app import backend, board as B

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SPECS = ["db:image=postgres,port=5432,env=POSTGRES_PASSWORD=dev,env=HOST_VAR,secret=PGPASS=pg,data=/var/lib/pg,"
         "keep=yes,user=999:999,memory=1g",
         "cache:image=redis,port=6379,ready=none,read-only=no,cpus=0.5,pids=512",
         "search"]


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


@pytest.fixture
def images(tmp_path, monkeypatch):
    """Your images recorded in a temporary configuration (no Podman is asked)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    record = {"kind": "pulled", "ref": "redis:7-alpine", "image": "docker.io/library/redis@sha256:" + "a" * 64,
              "digest": "sha256:" + "a" * 64, "id": "b" * 64, "user": "999:1000", "ports": [6379],
              "os": {"ID": "alpine"}, "reviewed": False}
    path = tmp_path / "config" / "sandbox-ai" / "user-images.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "images": {"redis": record, "postgres": {**record, "ports": [5432]}}}))
    return path


# -- the board's model --

@pytest.mark.parametrize("spec", SPECS)
def test_a_service_survives_the_board(sai, spec):
    board = B.from_argv(sai, "claude", ["--service", spec, "--memory", "4g"])
    node = board.first("service")
    assert node is not None and node.props["name"] == spec.partition(":")[0]
    back = B.to_argv(sai, board)
    assert back[back.index("--service") + 1] == sai.module("services").spec_text(sai.module("services").parse_spec(spec))
    assert B.to_argv(sai, B.from_argv(sai, "claude", back)) == back, "a second trip changes nothing"
    # the board's own text of a part is sandbox-ai's own
    assert B.service_spec(node.props) == sai.module("services").spec_text(node.props)


def test_services_are_parts_of_their_own(sai):
    board = B.from_argv(sai, "claude", [w for spec in SPECS for w in ("--service", spec)])
    services = board.of_kind("service")
    assert [n.props["name"] for n in services] == ["db", "cache", "search"]
    assert services[0].title() == "Service (container): db" and services[0].key() == "service:db"
    agent, proxy = board.first("agent"), board.first("proxy")
    assert all((agent.id, n.id) in board.edges and (n.id, proxy.id) in board.edges for n in services)
    board.disconnect(agent.id, services[1].id)  # not connected: not started
    assert "cache:" not in " ".join(B.to_argv(sai, board))
    assert B.project_document(sai, board)[0]["options"].count("--service") == 2  # in the project file's options


def test_a_service_sandbox_ai_refuses_keeps_its_text_and_is_a_problem(sai):
    board = B.from_argv(sai, "claude", ["--service", "db:port=nope", "--service", "Bad"])
    first, second = board.of_kind("service")
    assert first.props["raw"] == "db:port=nope" and "TCP port" in first.props["error"]
    assert B.to_argv(sai, board)[-4:] == ["--service", "db:port=nope", "--service", "Bad"]  # nothing lost
    texts = [p.text for p in B.problems(sai, board) if p.level == "error"]
    assert any("cannot be read" in t and "TCP port" in t for t in texts)
    assert any("Bad" in t for t in texts)


def test_service_problems(sai):
    board = B.from_argv(sai, "claude", ["--service", "db:image=postgres,port=5432",
                                        "--service", "web:image=nginx,port=5432"])
    texts = [p.text for p in B.problems(sai, board, user_images={"postgres"})]
    assert any("nginx is none of your images" in t for t in texts)
    assert any("Port 5432 is taken by the service db" in t for t in texts)
    assert not any("none of your images" in p.text for p in B.problems(sai, board))  # not known: not checked
    board = B.from_argv(sai, "claude", ["--service", "db:image=postgres", "--backend", "bwrap"])
    assert any("Podman backend" in p.text for p in B.problems(sai, board))
    board = B.from_argv(sai, "claude", ["--service", "db:image=postgres", "--podman-url", "unix:///x.sock"])
    assert any("--podman-url" in p.text for p in B.problems(sai, board))
    node = board.first("service")
    node.props["ports"] = [70000]  # an edit the parser refuses: its own words
    assert any("TCP port" in p.text and p.node == node.id for p in B.problems(sai, board))


def test_services_and_bases_cost_points(sai):
    plain = B.badges(B.from_argv(sai, "claude", []))[0]
    one = B.from_argv(sai, "claude", ["--service", "db:image=postgres,secret=PW,read-only=no"])
    score, found = B.badges(one)
    assert score == plain - 9
    assert any("UNCHECKED" in b.text and b.level == "warn" for b in found)
    assert any("writable root" in b.text for b in found) and any("Secret PW" in b.text for b in found)
    variant = B.from_argv(sai, "claude", ["--image-variant", "py"])
    assert B.badges(variant)[0] == plain - 5  # a variant on the agent's own base
    score, found = B.badges(variant, bases={"py": "pyimg"})
    assert score == plain - 20 and any(b.level == "bad" and "pyimg" in b.text for b in found)


def test_the_backend_reads_your_images_without_podman(sai, images, monkeypatch):
    monkeypatch.setattr(sai, "_modules", {})  # read with this test's configuration
    monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: pytest.fail("Podman was asked"))
    rows = sai.user_images()
    assert sorted(r["name"] for r in rows) == ["postgres", "redis"] and all(r["unchecked"] for r in rows)
    assert sai.variant_bases() == {}


# -- the inspector --

@pytest.fixture
def page(qapp, tmp_path, monkeypatch, images):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import widgets
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


def test_the_service_part_is_set_up_in_the_inspector(page, wait):
    from PySide6 import QtWidgets
    from sandbox_ai_app.board_view import LinesEdit
    page.load_user_images()
    assert wait(lambda: page.user_images is not None, 30)
    page.add_node("service")
    node = page.board.first("service")
    assert node.props["name"] == "service" and node.props["image"] in ("postgres", "redis")
    page.inspect(node)
    labels = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any(text.startswith("UNCHECKED") for text in labels)
    combo = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if c.findData("redis") >= 0)
    combo.setCurrentIndex(combo.findData("redis"))
    combo.activated.emit(combo.currentIndex())
    node = page.board.first("service")
    assert node.props["image"] == "redis"
    page.inspect(node)
    edits = page.inspector_holder.findChildren(QtWidgets.QLineEdit)
    ports = next(e for e in edits if "localhost:5432" in e.placeholderText())
    ports.setText("6379, 6380")
    ports.editingFinished.emit()
    page.inspect(page.board.first("service"))
    lines = page.inspector_holder.findChildren(LinesEdit)
    secrets = next(e for e in lines if "SECRET" in e.placeholderText())
    secrets.setPlainText("REDIS_PASSWORD=redis_pw")
    secrets.changed.emit(secrets.values())
    node = page.board.first("service")
    assert node.props["ports"] == [6379, 6380]
    assert node.props["secrets"] == [{"name": "REDIS_PASSWORD", "secret": "redis_pw"}]
    assert "--service service:image=redis,port=6379,port=6380,secret=REDIS_PASSWORD=redis_pw" in \
        page.preview.toPlainText()
    assert any("UNCHECKED" in page.findings.item(i).text() for i in range(page.findings.count()))
    page.undo.undo()
    assert page.board.first("service").props["secrets"] == []


def test_an_unknown_image_is_a_problem_once_your_images_are_known(page, wait):
    page.add_node("service", **{**B.default_service("web"), "image": "nginx"})
    page.load_user_images()
    assert wait(lambda: page.user_images is not None, 30)
    page.refresh()
    assert any("nginx is none of your images" in p.text for p in page.problems)


def test_the_engine_says_when_its_variant_is_on_your_image(page, wait, monkeypatch):
    from PySide6 import QtWidgets
    monkeypatch.setattr(page.sai, "variant_bases", lambda host="local": {"py": "pyimg"})
    page.add_node("engine", **{"--image-variant": "py"})
    page.load_user_images()
    assert wait(lambda: page.image_bases is not None, 30)
    page.inspect(page.board.first("engine"))
    labels = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("built on your image pyimg" in text for text in labels)
    assert any("pyimg" in page.findings.item(i).text() for i in range(page.findings.count()))


# -- the Images page: Your images --

def test_your_images_on_the_images_page(qapp, sai, monkeypatch):
    from sandbox_ai_app import images_page
    page = images_page.ImagesPage(sai, lambda argv, title, **kw: None)
    started, tasks = [], []
    monkeypatch.setattr(page.list_command, "start", lambda argv, input=None: True)
    monkeypatch.setattr(page.user_command, "start", lambda argv, input=None: started.append(argv) or True)
    monkeypatch.setattr(page.task, "run", lambda argv, done, text, source=None, input=None, **kw:
                        tasks.append((argv, done, kw)))
    page.refresh()
    assert started[-1] == sai.command("images", "list", "--json")
    rows = [{"name": "redis", "kind": "pulled", "ref": "redis:7-alpine", "image": "docker.io/library/redis@sha256:"
             + "a" * 64, "id": "sha256:" + "b" * 64, "user": "999:1000", "ports": [6379], "os": {"ID": "alpine"},
             "unchecked": True, "used_by": [], "present": True, "base_problem": "it is alpine"},
            {"name": "py", "kind": "pulled", "ref": "python:3.12-bookworm", "image": "docker.io/library/python@sha256:"
             + "c" * 64, "id": "d" * 64, "user": "0:0", "ports": [],
             "os": {"PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)"}, "unchecked": True, "used_by": ["pyv"],
             "present": False, "base_problem": None}]
    page.user_images_listed(0, json.dumps(rows), "")
    assert page.user_table.rowCount() == 2
    cells = [page.user_table.item(0, c).text() for c in range(page.user_table.columnCount())]
    assert "UNCHECKED" in cells and "999:1000" in cells
    assert "1 missing from Podman" in page.user_status.text()
    # pull: the dialog's command, run where the page is, with a long time limit
    dialog = images_page.PullDialog(page, ["redis"])
    dialog.reference.setText("docker.io/library/postgres:16")
    assert dialog.chosen_name() == "postgres" and dialog.ok.isEnabled()
    assert dialog.argv() == ["images", "pull", "docker.io/library/postgres:16", "--as", "postgres", "--json"]
    dialog.reference.setText("redis:8")
    assert not dialog.ok.isEnabled()  # the name is taken
    dialog.replace.setChecked(True)
    assert dialog.ok.isEnabled() and "--replace" in dialog.argv()
    monkeypatch.setattr(images_page.PullDialog, "exec", lambda self: (self.reference.setText("postgres:16"), 1)[1])
    page.pull_image()
    assert tasks[-1][0] == sai.command("images", "pull", "postgres:16", "--as", "postgres", "--json")
    assert tasks[-1][2]["timeout"] >= 3600
    build = images_page.BuildImageDialog(page, [], local=True)
    build.name.setText("tools")
    build.file.setText("/src/Containerfile")
    build.context.setText("/src")
    assert build.argv() == ["images", "build-image", "tools", "--file", "/src/Containerfile", "--context", "/src",
                            "--json"]
    # a variant on one of your images: only one that can be a base is offered
    variant = images_page.NewVariantDialog(page, sai.agents, "claude", lambda name: bool(name),
                                           page.user_entries)
    offered = [variant.base.itemData(i) for i in range(variant.base.count())]
    assert offered == ["", "", "py"] and not variant.base.model().item(1).isEnabled()
    monkeypatch.setattr(images_page, "ask_first", lambda *a, **k: True)
    page.user_table.selectRow(0)
    page.remove_user_image()
    assert tasks[-1][0] == sai.command("images", "remove", "redis")
    page.create_variant("pyv", "claude", base="py")
    assert tasks[-1][0] == sai.command("images", "new", "pyv", "--from", "claude", "--base", "py", "--json")
    page.user_image_added(0, "{}", "", "postgres")
    assert started[-1] == sai.command("images", "list", "--json")  # read again
    page.user_images_listed(2, "", "argument command: invalid choice: 'list'")
    assert "update it" in page.user_status.text()
    page.deleteLater()


def test_a_containerfile_build_asks_for_network_and_for_a_review_of_an_agent_written_file(qapp, sai, monkeypatch,
                                                                                           tmp_path):
    """build-image gives RUN steps no network unless --build-network and refuses a Containerfile or context where a
    sandbox can write unless --trust-agent-written: the dialog has a box for the network (off, with what it means)
    and - only for such a file - "I reviewed it", without which it builds nothing; a failed build names the box that
    would have let it. English and German."""
    from sandbox_ai_app import i18n, images_page
    page = images_page.ImagesPage(sai, lambda argv, title, **kw: None)
    exposed = tmp_path / "workspace"

    def writable(path, what):
        resolved = os.path.abspath(path)
        if resolved == str(exposed) or resolved.startswith(str(exposed) + os.sep):
            raise ValueError(f"{what} {resolved} lies inside {exposed}, which a sandbox can write")
    build = images_page.BuildImageDialog(page, [], local=True, writable=writable)
    build.show()
    build.name.setText("tools")
    build.file.setText("/src/Containerfile")
    assert not build.network.isChecked() and "not through the sandbox's firewall" in build.network_note.text()
    assert not build.trust.isVisibleTo(build) and build.ok.isEnabled()
    build.network.setChecked(True)
    assert build.argv() == ["images", "build-image", "tools", "--file", "/src/Containerfile", "--build-network",
                            "--json"]
    # An agent-written Containerfile (its folder is the context): said, and built only after the review is ticked.
    build.file.setText(str(exposed / "Containerfile"))
    assert build.trust.isVisibleTo(build) and "a sandbox can write" in build.agent_note.text()
    assert not build.ok.isEnabled() and "--trust-agent-written" not in build.argv()
    build.trust.setChecked(True)
    assert build.ok.isEnabled() and "--trust-agent-written" in build.argv()
    build.file.setText("/src/Containerfile")  # another file: its review is not this one's
    assert not build.trust.isVisibleTo(build) and not build.trust.isChecked()
    build.context.setText(str(exposed))  # the context alone where a sandbox writes
    assert build.trust.isVisibleTo(build) and not build.ok.isEnabled()
    # On another host it is not known here: no box; the host's refusal names the box.
    remote = images_page.BuildImageDialog(page, [], local=False, writable=writable)
    remote.name.setText("tools")
    remote.file.setText(str(exposed / "Containerfile"))
    assert remote.ok.isEnabled() and "--trust-agent-written" not in remote.argv()
    shown = []
    monkeypatch.setattr(images_page, "warn", lambda parent, title, text, **kw: shown.append(text))
    page.user_image_added(1, "", "sandbox-ai images: podman build of /src/Containerfile failed (exit status 1); its RUN "
                          "steps had no network: --build-network gives them this host's", "tools",
                          argv=["images", "build-image", "tools", "--file", "/src/Containerfile", "--json"])
    assert "RUN steps may use this computer's network" in shown[-1]
    page.user_image_added(1, "", "sandbox-ai images: the Containerfile ... then build with --trust-agent-written",
                          "tools", argv=["images", "build-image", "tools", "--file", "/x", "--json"])
    assert "I reviewed this agent-written Containerfile" in shown[-1]
    page.user_image_added(1, "", "failed (exit status 1); its RUN steps had no network", "tools",
                          argv=["images", "build-image", "tools", "--build-network", "--json"])
    assert "RUN steps may use" not in shown[-1], "nothing to tick: it had the network"
    try:
        i18n.set_language("de")
        german = images_page.BuildImageDialog(page, [], local=True, writable=writable)
        assert "Netzwerk dieses Computers" in german.network.text() and "kein Netzwerk" in german.network_note.text()
        german.deleteLater()
    finally:
        i18n.set_language("en")
    for dialog in (build, remote):
        dialog.deleteLater()
    page.deleteLater()


def test_a_variant_on_your_image_is_marked_in_the_list(qapp, sai, monkeypatch):
    from sandbox_ai_app import images_page
    page = images_page.ImagesPage(sai, lambda argv, title, **kw: None)
    monkeypatch.setattr(page.list_command, "start", lambda argv, input=None: True)
    page.show_images([{"agent": "claude", "variant": True, "target": "pyv", "present": True,
                       "image": "localhost/claude-sandbox-pyv:latest", "base_image": "py"}])
    shown = [(page.images.item(0, c).text(), page.images.item(0, c).toolTip()) for c in range(page.images.columnCount())]
    assert any("on your image py (UNCHECKED)" in text or "on your image py (UNCHECKED)" in tip for text, tip in shown)
    page.deleteLater()
