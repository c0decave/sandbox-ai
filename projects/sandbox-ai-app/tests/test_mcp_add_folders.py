"""A catalog MCP server that must have a folder, added for a start (user report from live use, 2026-11-14: the Board's
exiftool failed with "sandbox-ai mcp: exiftool needs --mount /work=HOST"): the folders its catalog entry requires are
read first; one the start does not give it (--mcp-mount) is asked for - the start's workspace offered, another folder
chosen, or Cancel (then it is not added, and said) - and passed to `mcp add` as --mount."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import prerequisites, state, terminal, widgets  # noqa: E402
from test_prepare_progress import FakeSai, Page  # noqa: E402

EXIFTOOL = {"name": "exiftool", "mounts": [{"target": "/work", "required": True}, {"target": "/out"}]}
BINWALK = {"name": "binwalk", "mounts": [{"target": "/work", "required": True}]}
FETCH = {"name": "fetch"}


class Sai(FakeSai):
    def __init__(self, script, entries, terminal_setting="app"):
        super().__init__(script, terminal_setting)
        self.entries, self.asked_hosts = entries, []

    def mcp_catalog(self):
        return [("mcp", entry) for entry in self.entries], []

    def host_json(self, host, *args, timeout=None):
        self.asked_hosts.append((host, args))
        return {"entries": [{"prefix": "mcp", **entry} for entry in self.entries], "problems": []}


class StartPage(Page):
    def __init__(self, sai, argv):
        super().__init__(sai)
        self.argv = argv

    def prerequisite_argv(self):
        return self.argv


@pytest.fixture
def make(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))

    def at_once(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(prerequisites, "background", at_once)
    script = tmp_path / "sandbox-ai"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    pages = []

    def made(argv, entries=(EXIFTOOL, BINWALK, FETCH), terminal_setting="window"):
        page = StartPage(Sai(script, list(entries), terminal_setting), argv)
        pages.append(page)
        return page
    yield made
    for page in pages:
        page.deleteLater()


def added(page):
    return [args for _, args in page.sai.ran if args[:2] == ("mcp", "add")]


def test_the_folder_a_catalog_server_needs_is_asked_for_with_the_workspace_offered(make, tmp_path, monkeypatch):
    work = tmp_path / "project"
    page = make(["--workspace", str(work), "--mcp", "exiftool", "--mcp", "fetch"])
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "workspace")
    page.provide({"--mcp": ["exiftool", "fetch"]}, [], "local", ("local", "{}"), None, "start", context="local",
                 agent="claude")
    assert added(page) == [("mcp", "add", "exiftool", "--mount", f"/work={work}"), ("mcp", "add", "fetch")]
    question = next(text for title, text, _ in widgets.SHOWN if title == "MCP server exiftool")
    assert "/work" in question and "needs a folder" in question
    assert not any(title == "MCP server fetch" for title, *_ in widgets.SHOWN), "a server without folders: no question"


def test_the_starts_own_folder_is_taken_without_a_question(make, tmp_path):
    page = make(["--workspace", "/srv/w", "--mcp", "exiftool", "--mcp-mount", f"exiftool:/work={tmp_path}"])
    page.provide({"--mcp": ["exiftool"]}, [], "local", ("local", "{}"), None, "start", context="local", agent="claude")
    assert added(page) == [("mcp", "add", "exiftool", "--mount", f"/work={tmp_path}")]
    assert not any(title == "MCP server exiftool" for title, *_ in widgets.SHOWN)


def test_another_folder_or_cancel(make, tmp_path, monkeypatch):
    other = tmp_path / "photos"
    page = make(["--workspace", "/srv/w", "--mcp", "exiftool"])
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    monkeypatch.setattr(state, "run_dialog", lambda dialog: [str(other)])
    page.provide({"--mcp": ["exiftool"]}, [], "local", ("local", "{}"), None, "start", context="local", agent="claude")
    assert added(page) == [("mcp", "add", "exiftool", "--mount", f"/work={other}")]
    # Cancel: not added, and said (never a silent failure of mcp add).
    page = make(["--workspace", "/srv/w", "--mcp", "binwalk", "--mcp", "fetch"])
    monkeypatch.setattr(widgets, "AUTO_CHOICE", None)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    real_choose = widgets.choose
    monkeypatch.setattr(widgets, "choose", lambda *a, **k: None if "binwalk" in str(a[1]) else real_choose(*a, **k))
    page.provide({"--mcp": ["binwalk", "fetch"]}, [], "local", ("local", "{}"), None, "start", context="local",
                 agent="claude")
    assert added(page) == [("mcp", "add", "fetch")]
    said = "\n".join(text for _, text, _ in widgets.SHOWN)
    assert "MCP server binwalk: not added - it needs a folder for /work" in said


def test_another_host_reads_its_catalog_and_the_folder_is_typed(make, monkeypatch):
    page = make(["--workspace", "/home/me/w", "--mcp", "exiftool"])
    page.host = lambda: "box"
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    # The folder: chosen from box's folders (remote_chooser), never this machine's
    from sandbox_ai_app import remote_chooser
    monkeypatch.setattr(remote_chooser, "choose", lambda parent, host, *a, **k: "/data/pics" if host == "box" else "")
    page.provide({"--mcp": ["exiftool"]}, [], "box", ("box", "{}"), None, "start", context="box", agent="claude")
    assert page.sai.asked_hosts == [("box", ("mcp", "catalog", "--json"))]
    assert page.sai.ran[-1] == ("box", ("mcp", "add", "exiftool", "--mount", "/work=/data/pics"))


def test_every_required_folder_of_the_catalog_is_known(make):
    """required_folders: the targets an entry requires, by its name or prefix:name; None when no catalog has it."""
    entries = [("mcp", EXIFTOOL), ("hub", BINWALK), ("mcp", FETCH)]
    assert prerequisites.required_folders(entries, "exiftool") == ["/work"]
    assert prerequisites.required_folders(entries, "hub:binwalk") == ["/work"]
    assert prerequisites.required_folders(entries, "mcp:binwalk") is None
    assert prerequisites.required_folders(entries, "fetch") == []
    assert prerequisites.required_folders([{"prefix": "hub", **BINWALK}], "binwalk") == ["/work"]
    assert prerequisites.start_workspace(["--workspace=/a/b:rw", "--workspace", "/c"]) == "/a/b"
    assert prerequisites.start_workspace(["--mcp", "x"]) is None
