"""Live 0.1.96: "only added a service as a test, which apparently does not exist; there was no drop-down in the service's
properties either - wherever we choose images/containers there should be a drop-down of the existing ones beside the
text". The launcher then refused the start ("no image service of yours"). Here: a service whose image is none of
yours is refused by the app before the launcher - on the Board (its image read first when not read yet), on the Start
page (--service, a sandbox-ai preset's image values) - naming the service and offering the Images page; and every
field that names an image, a variant, an agent's image or a model server is an editable list of what there is (read
again when opened, a greyed "None yet" saying where one is added): the Service part's image, the engine's
--image-variant and --image, the LLM part's --llm, the guide's questions, a preset's image values (the gallery's form
and the Board's fill-in), the Start page's --service, --image-variant and --image, a variant's base image."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board as B, name_fields as NF, widgets  # noqa: E402

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

RECORD = {"kind": "pulled", "ref": "redis:7-alpine", "image": "docker.io/library/redis@sha256:" + "a" * 64,
          "digest": "sha256:" + "a" * 64, "id": "b" * 64, "user": "999:1000", "ports": [6379],
          "os": {"ID": "alpine"}, "reviewed": False}


def record_images(config, names):
    """Your images NAMES recorded in CONFIG (XDG_CONFIG_HOME): read as sandbox-ai reads them, no Podman."""
    path = config / "sandbox-ai" / "user-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "images": {name: dict(RECORD) for name in names}}))


@pytest.fixture
def home(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    NF._kept.clear()
    NF._waiting.clear()
    yield tmp_path
    NF._kept.clear()
    NF._waiting.clear()


@pytest.fixture
def board(home, monkeypatch):
    (home / "cwd").mkdir()
    monkeypatch.chdir(home / "cwd")
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False})
    view.started = []
    monkeypatch.setattr(view, "run_start", lambda *a, **k: view.started.append(a) or True)
    view.wanted = []
    if hasattr(view, "images_wanted"):  # (an app before it had none: its start was not refused at all)
        view.images_wanted.connect(lambda: view.wanted.append(True))
    yield view
    view.deleteLater()


def said(title):
    return [text for shown_title, text, _ in widgets.SHOWN if shown_title == title]


# -- the reported case: refused in the app, before the launcher --

@needs_sai
@pytest.mark.parametrize("mine", [[], ["redis"]])
def test_the_reported_service_of_no_image_of_yours_is_refused_on_the_board(board, home, wait, mine):
    """The Service part as `--service service` reads it (its name is its image), your images not read yet: the start
    reads them first, then refuses it - naming the service, the images there are - and offers the Images page."""
    record_images(home / "config", mine)
    board.add_node("workspace", path=str(home / "work"), readonly=False)
    board.user_images = None  # not read yet (the user_images check was skipped then)
    board.add_node("service", **B.service_props(board.sai, "service"))
    assert board.board.first("service").props["image"] == "service"
    assert board.start() is False
    assert wait(lambda: said("Not started"), 30), "refused once your images are read"
    text = said("Not started")[-1]
    assert "The service service has no image of yours" in text and "its name is its image" in text
    assert ("none yet" in text) if not mine else ("your images: redis" in text)
    assert "Images page" in text and board.wanted == [True], "the Images page offered (and opened on yes)"
    assert board.started == [], "never handed to the launcher"


@needs_sai
def test_a_service_part_with_no_image_or_an_unknown_one_is_refused(board, home, wait):
    record_images(home / "config", ["redis"])
    board.add_node("workspace", path=str(home / "work"), readonly=False)
    board.add_node("service", **{**B.default_service("db"), "image": ""})
    board.start()
    assert wait(lambda: said("Not started"), 30)
    assert "Choose the image of the service db" in said("Not started")[-1] and not board.started
    widgets.SHOWN.clear()
    node = board.board.first("service")
    board.set_prop(node.id, "image", "postgres")
    board.start()
    assert wait(lambda: said("Not started"), 30)
    text = said("Not started")[-1]
    assert "The service db: postgres is none of your images (your images: redis)" in text and not board.started
    assert [p.text for p in board.errors() if "postgres" in p.text], "an error of the board, not only a finding"


@needs_sai
def test_your_images_not_read_is_an_error_not_a_start_unchecked(board, home):
    board.add_node("service", **{**B.default_service("cache"), "image": "redis"})
    board.user_images, board.user_images_failed = [], True  # the host did not answer
    texts = [p.text for p in board.errors()]
    assert any("could not be read" in t and "cache" in t for t in texts)


@needs_sai
def test_a_start_after_a_failed_read_reads_your_images_again(board, home, wait):
    record_images(home / "config", ["redis"])
    board.add_node("service", **{**B.default_service("cache"), "image": "redis"})
    board.user_images, board.user_images_failed = [], True
    assert board.start() is False, "it waits for the read"
    assert wait(lambda: not board.user_images_failed and board.user_image_names() == {"redis"}, 30)
    assert not [t for t in said("Not started") if "could not be read" in t or "cache" in t]


@needs_sai
def test_an_image_of_yours_passes_the_boards_check(board, home, wait):
    record_images(home / "config", ["redis"])
    board.add_node("service", **{**B.default_service("cache"), "image": "redis"})
    board.load_user_images()
    assert wait(lambda: board.user_images is not None, 30)
    assert not [p for p in board.errors() if "cache" in p.text]


@needs_sai
def test_the_start_page_refuses_a_service_of_no_image_of_yours(home, wait):
    from sandbox_ai_app.ui import LaunchPage
    record_images(home / "config", ["redis"])
    started = []
    page = LaunchPage(backend.SandboxAI(), lambda *a, **k: started.append(a))
    wanted = []
    page.images_wanted.connect(lambda: wanted.append(True))
    page.set_option("--workspace", [str(home)])
    page.set_option("--service", ["service"])
    assert page.start() is False
    assert wait(lambda: said("Not started"), 30)
    text = said("Not started")[-1]
    assert "The service service has no image of yours" in text and "your images: redis" in text
    assert wanted == [True] and not started
    went_on = []
    page.set_option("--service", ["cache:image=redis"])
    assert page.services_unchecked("Not started", lambda: went_on.append(True)) is True, "read first"
    assert wait(lambda: went_on == [True], 30), "an image of yours: the start goes on"
    assert page.services_unchecked("Not started", lambda: None) is False, "checked just now: on at once"
    widgets.SHOWN.clear()
    page.set_option("--service", [])
    page.preset_images = lambda: [("Your PostgreSQL image (sandbox-ai images list)", "postgres")]
    page.services_unchecked("Not started", lambda: went_on.append(True))
    assert wait(lambda: said("Not started"), 30), "a sandbox-ai preset's image value is checked too"
    assert "postgres, none of your images" in said("Not started")[-1] and went_on == [True]
    page.deleteLater()


def test_service_image_problems_say_the_service_and_the_images(qapp):
    sai = backend.SandboxAI() if os.environ.get("SANDBOX_AI_HOME") else None
    texts = B.service_image_problems(sai, ["service", "db:image=postgres", "cache:image=redis"], {"redis"})
    assert len(texts) == 2
    assert texts[0].startswith("The service service has no image of yours") and "your images: redis" in texts[0]
    assert texts[1].startswith("The service db: postgres is none of your images")
    assert B.service_image_problems(sai, ["service"], None) == [], "not known: the launcher says it"
    assert "none yet" in B.service_image_problems(sai, ["x:image=y"], set())[0]


# -- the drop-downs --

class FakeSai:
    def __init__(self, images=("redis", "postgres"), variants=("py",), agent_images=("localhost/claude-sandbox:latest",),
                 servers=("home",)):
        self.images, self.variants, self.agent_images, self.servers = list(images), list(variants), \
            list(agent_images), list(servers)
        self.calls = []

    def user_images(self, host="local"):
        self.calls.append(("image", host))
        return [{"name": name, "kind": "pulled"} for name in self.images]

    def host_image_variants(self, host):
        self.calls.append(("variant", host))
        return list(self.variants)

    def host_agent_images(self, host):
        self.calls.append(("agent_image", host))
        return list(self.agent_images)

    def host_llm_endpoints(self, host):
        self.calls.append(("llm", host))
        return [{"name": name} for name in self.servers]


@pytest.mark.parametrize("kind, wanted", [("image", ["postgres", "redis"]), ("variant", ["py"]),
                                          ("agent_image", ["localhost/claude-sandbox:latest"]), ("llm", ["home"])])
def test_a_name_field_lists_what_there_is_editable_and_read_again_when_opened(home, wait, monkeypatch, kind, wanted):
    sai = FakeSai()
    field = NF.NameField(kind, sai, "local", placeholder="name")
    assert field.isEditable() and wait(lambda: field.offered() == wanted)
    field.setText("typed-one")
    assert field.text() == "typed-one" and field.offered() == wanted, "free text stays possible"
    getattr(sai, {"image": "images", "variant": "variants", "agent_image": "agent_images", "llm": "servers"}[kind]) \
        .append("zz-new")
    monkeypatch.setattr(NF, "OPEN_SECONDS", 0.0)
    field.showPopup()
    assert wait(lambda: "zz-new" in field.offered()), "opened: read again"
    field.hidePopup()
    assert field.text() == "typed-one"
    field.deleteLater()


def test_with_none_yet_the_list_says_where_one_is_added(home, wait):
    field = NF.NameField("image", FakeSai(images=()), "local")
    assert wait(lambda: not field.reading)
    assert field.offered() == []
    hint = field.itemText(field.count() - 1)
    assert hint.startswith("None yet") and "Images page" in hint and "Your images" in hint
    assert not field.model().item(field.count() - 1).isEnabled(), "greyed: never chosen"
    assert field.completer().model().stringList() == [], "nor completed"
    field.deleteLater()


def test_the_images_page_listing_reaches_every_image_field(home, wait):
    sai = FakeSai(images=("redis",))
    field = NF.NameField("image", sai, "local")
    assert wait(lambda: field.offered() == ["redis"])
    NF.publish(sai, "image", "local", ["redis", "web"])  # the Images page listed them (one added there)
    assert field.offered() == ["redis", "web"]
    field.deleteLater()


@needs_sai
def test_the_service_parts_image_list_is_read_again_when_opened(board, home, wait):
    record_images(home / "config", [])
    board.add_node("service", **{**B.default_service("db"), "image": ""})
    board.load_user_images()
    assert wait(lambda: board.user_images == [], 30)
    board.inspect(board.board.first("service"))
    combo = next(c for c in board.inspector_holder.findChildren(QtWidgets.QComboBox) if c.findData("\x00other") >= 0)
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert combo.isEditable() and any("No images of yours yet" in t for t in texts), texts
    record_images(home / "config", ["redis"])
    board.user_images_read_at = 0.0
    combo.showPopup()
    assert wait(lambda: combo.findData("redis") >= 0, 30), "an image added meanwhile is offered when opened"
    combo.hidePopup()


@needs_sai
@pytest.mark.parametrize("kind, option, field_kind", [("engine", "--image-variant", "variant"),
                                                      ("engine", "--image", "agent_image"),
                                                      ("llm", "--llm", "llm")])
def test_the_boards_options_that_name_one_offer_what_there_is(board, home, kind, option, field_kind):
    board.add_node(kind, **({"--llm": "home"} if kind == "llm" else {}))
    board.inspect(board.board.first(kind))
    fields = [f for f in board.inspector_holder.findChildren(NF.NameField) if f.accessibleName() == option]
    assert fields and fields[0].kind == field_kind and fields[0].isEditable()


@needs_sai
def test_the_guides_image_variant_question_offers_the_variants(board, home):
    from sandbox_ai_app import guide, guide_view
    board.add_node("engine")
    node = board.board.first("engine")
    dialog = guide_view.NodeGuide(node, parent=board)
    question = next(q for q in guide.questions("engine") if q.key == "--image-variant")
    _, widget = dialog.inputs[question.key]
    assert isinstance(widget, NF.NameField) and widget.kind == "variant"
    widget.setText("gpu")
    assert dialog.answers()["--image-variant"] == "gpu"
    dialog.deleteLater()


@needs_sai
def test_a_presets_image_values_are_offered_from_your_images(home, wait):
    from sandbox_ai_app import presets as PR
    from sandbox_ai_app.preset_gallery import PlaceholderForm
    sai = backend.SandboxAI()
    record_images(home / "config", ["pg17"])
    document = next(d for d in PR.local_documents(sai)[0] if d.get("id") == "database-services")
    preset = PR.from_sandbox_ai(sai, document)
    images = [p for p in preset.placeholders if p.names == "image"]
    assert len(images) == 2, "postgres and redis stand as a service's image"
    form = PlaceholderForm(sai, preset)
    field = form.fields[images[0].marker]
    assert isinstance(field, NF.NameField) and field.kind == "image"
    assert wait(lambda: field.offered() == ["pg17"], 30) and field.text() == "postgres", "the default offered first"
    field.setText("pg17")
    assert form.values()[images[0].marker] == "pg17"
    form.deleteLater()
    # the Board's fill-in: the placeholders that stand as a service's image are known there too
    board_ = PR.build(sai, preset, None, agent="claude")
    assert B.board_image_markers(board_) == {p.marker for p in images}


def test_the_start_pages_service_chooser_adds_a_service_of_your_image(qapp):
    from sandbox_ai_app import ui
    field = ui.ListField(choices=True, option="--service")
    opened = []
    field.opening.connect(lambda: opened.append(True))
    field.set_choices([], "this computer")
    texts = [a.text() for a in field.pick.menu().actions()]
    assert texts == ["None on this computer - the Images page, Your images, adds one"]
    field.set_choices(["redis", "Pg_17"], "this computer")
    field.pick.menu().aboutToShow.emit()
    assert opened == [True], "opened: the host's lists are read again"
    field.pick.menu().actions()[0].trigger()
    field.pick.menu().actions()[0].trigger()
    field.pick.menu().actions()[1].trigger()
    assert field.value() == ["redis:image=redis", "redis-1:image=redis", "pg-17:image=Pg_17"]
    assert "--service" in ui.OptionsForm.HOST_CHOICES and "--image" in ui.OptionsForm.HOST_CHOICES
    field.deleteLater()


@needs_sai
def test_the_start_pages_value_choosers_say_none_yet_and_read_again_when_opened(home, wait, monkeypatch):
    from sandbox_ai_app.ui import LaunchPage
    page = LaunchPage(backend.SandboxAI(), lambda *a, **k: None)
    read = []
    monkeypatch.setattr(page, "read_host_choices", lambda keep=False: read.append(keep))
    page.form.set_choices("local", {"--image-variant": []}, {})
    combo = page.form.widgets["--image-variant"]
    assert combo.isEditable()
    last = combo.itemText(combo.count() - 1)
    assert last == "None on this computer - the Images page, New variant, makes one"
    assert not combo.model().item(combo.count() - 1).isEnabled()
    assert combo.completer().model().stringList() == [], "never completed to the greyed entry"
    page.form.set_choices("local", {"--image-variant": ["py", "web"]}, {})
    assert [combo.itemText(i) for i in range(combo.count())] == ["", "py", "web"]
    assert combo.completer().model().stringList() == ["py", "web"]
    page.choices_read_at = 0.0
    combo.showPopup()
    assert read == [True], "opened: read again, what it offers kept meanwhile"
    combo.hidePopup()
    page.deleteLater()


def test_a_variants_base_is_an_editable_list_of_your_images(qapp):
    from sandbox_ai_app.images_page import NewVariantDialog
    dialog = NewVariantDialog(None, ["claude"], "claude", lambda name: bool(name),
                              [{"name": "deb12", "base_problem": None}])
    assert dialog.base.isEditable() and dialog.base_name() == ""
    dialog.base.setCurrentIndex(dialog.base.findData("deb12"))
    assert dialog.base_name() == "deb12"
    dialog.base.setEditText("typed")
    assert dialog.base_name() == "typed"
    dialog.deleteLater()
    empty = NewVariantDialog(None, ["claude"], "claude", lambda name: bool(name), [])
    last = empty.base.itemText(empty.base.count() - 1)
    assert last.startswith("None yet") and not empty.base.model().item(empty.base.count() - 1).isEnabled()
    assert empty.base_name() == ""
    empty.deleteLater()
