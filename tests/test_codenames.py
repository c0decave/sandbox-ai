"""Codenames for starts (sandbox/codenames.py): three words such as brisk-otter-lantern for every start without
--name, unless "name_style": "project" (defaults.json) or --name-style project keep AGENT-PROJECT-xxxx; a fresh one
for an existing start with `sandbox-ai rename NAME` (or --codename). The words are chosen with secrets, a name in
use is skipped, and every codename passes the launcher's name rules. Nothing here writes a login (HOME is moved for
the session by conftest.py and again for each test below)."""
import importlib.util
import inspect
import json
from pathlib import Path
import re
import secrets

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


cn = load("codenames_for_tests", "codenames.py")
st = load("starts_for_codename_tests", "starts.py")
launcher = st.launcher()
sessions = st.module("sessions")
LISTS = {"ADJECTIVES": cn.ADJECTIVES, "ANIMALS": cn.ANIMALS, "THINGS": cn.THINGS}
CODENAME = re.compile(r"[a-z]{3,8}-[a-z]{3,8}-[a-z]{3,8}")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_DATA_HOME", "data")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sessions, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    return tmp_path


def chooser(*names):
    """A choice() that gives the words of NAMES ("a-b-c" ...) in turn, as secrets.choice would pick them."""
    words = iter(word for name in names for word in name.split("-"))
    return lambda options: next(words)


# --- the words -------------------------------------------------------------------------------------------------

def test_the_lists_are_short_plain_and_apart():
    for name, words in LISTS.items():
        assert 150 <= len(words) <= 250, name
        assert len(set(words)) == len(words), f"{name}: a word twice"
        assert all(re.fullmatch(r"[a-z]{3,8}", word) for word in words), name
        # A word of hex digits only would look like a run id's or the -xxxx part.
        assert not [word for word in words if re.fullmatch(r"[0-9a-f]+", word)], name
    every = [word for words in LISTS.values() for word in words]
    assert len(set(every)) == len(every), "a word in two lists"


def test_no_word_is_a_command_an_agent_a_mode_a_state_or_a_brand():
    main = load("main_for_codename_tests", "main.py")
    commands = {part for command in main.PYTHON_COMMANDS for part in command.split("-")}
    reserved = commands | set(launcher.AUTH_TARGETS) | set(launcher.MODES.NAMES) | set(launcher.TELEMETRY_LEVELS) \
        | set(st.STATE_ORDER) | {"mcp", "team", "helper", "run", "agent", "session", "kept", "live", "paused", "ready",
                                 "on", "off", "own", "true", "false", "none", "default", "codename", "project"}
    brands = {"apple", "amazon", "jaguar", "puma", "python", "mustang", "beetle", "oracle", "shell", "rust", "ruby",
              "swift", "camel", "dove", "lynx", "gecko", "chrome", "safari", "opera", "ubuntu", "debian", "fedora",
              "docker", "podman", "kitty", "foot", "gnome", "mint", "java", "cobra", "nebula", "meteor", "ember",
              "crystal", "lotus", "harbor", "spring", "cocoa", "turkey", "gnu", "cat", "ram"}
    every = {word for words in LISTS.values() for word in words}
    assert not every & reserved and not every & brands


def test_every_codename_passes_the_name_rules_and_fits():
    """Each word in each place, beside the longest words of the other places and with the -xxxx a crowded machine
    adds: the launcher's name rule (SESSION_NAME, 40 characters) and starts.check_name take all of them; with
    words of [a-z]{3,8} only, that covers every combination."""
    longest = [max(words, key=len) for words in LISTS.values()]
    assert len("-".join(longest)) + len("-ffff") <= 40
    for place, words in enumerate(LISTS.values()):
        for word in words:
            parts = list(longest)
            parts[place] = word
            for name in ("-".join(parts), "-".join(parts) + "-0a1b"):
                st.check_name(name)
                assert re.fullmatch(launcher.SESSION_NAME, name) and not name.startswith("mcp-")
    for _ in range(200):
        name = cn.codename()
        assert CODENAME.fullmatch(name) and st.valid_name(name)


def test_the_words_are_chosen_with_secrets():
    assert inspect.signature(cn.codename).parameters["choice"].default is secrets.choice
    assert inspect.signature(cn.free_codename).parameters["choice"].default is secrets.choice
    assert inspect.signature(cn.free_codename).parameters["token"].default is secrets.token_hex
    picked = []
    assert cn.codename(lambda options: picked.append(options) or options[0]) == \
        f"{cn.ADJECTIVES[0]}-{cn.ANIMALS[0]}-{cn.THINGS[0]}"
    assert picked == [cn.ADJECTIVES, cn.ANIMALS, cn.THINGS]
    assert len({cn.codename() for _ in range(50)}) > 45, "about 11 million of them"


# --- a free one ------------------------------------------------------------------------------------------------

def test_a_name_in_use_is_skipped():
    taken = {"brave-otter-tulip"}
    assert cn.free_codename(taken, chooser("brave-otter-tulip", "calm-heron-maple")) == "calm-heron-maple"


def test_after_a_few_tries_a_short_hex_is_added():
    taken = {"brave-otter-tulip", "brave-otter-tulip-aaaa"}
    names = ["brave-otter-tulip"] * (cn.TRIES + 2)
    tokens = iter(["aaaa", "0b1c"])
    found = cn.free_codename(taken, chooser(*names), lambda n: next(tokens))
    assert found == "brave-otter-tulip-0b1c" and st.valid_name(found)


def test_no_free_codename_at_all_is_said():
    everything = type("Everything", (), {"__contains__": lambda self, name: True})()
    with pytest.raises(ValueError, match="choose a name with --name"):
        cn.free_codename(everything)


# --- the default name of a start -------------------------------------------------------------------------------

def test_the_default_name_is_a_codename_or_agent_project(monkeypatch):
    assert CODENAME.fullmatch(st.default_name("claude", "shop", taken=set()))
    assert re.fullmatch(r"claude-shop-[0-9a-f]{4}", st.default_name("claude", "shop", "project", taken=set()))
    with pytest.raises(ValueError, match="codename, project"):
        st.default_name("claude", "shop", "fancy")
    assert st.NAME_STYLES == launcher.NAME_STYLES
    # Without TAKEN: what names_taken() gives.
    monkeypatch.setattr(st.module("codenames"), "free_codename", lambda taken: sorted(taken))
    st.record("old-name", "start", "old-id")
    assert st.default_name("claude", "shop") == ["old-name"]


def test_what_a_codename_must_not_be(isolated):
    """Names and ids of what runs and is kept, the teams' names and the names with a history."""
    directory = sessions.session_dir("kept-id")
    (directory / "home").mkdir(parents=True)
    sessions.save("kept-id", {"agent": "codex", "options": [], "agent_args": [], "backend": "podman",
                              "workspace": "/srv/app", "cwd": "/", "created": "2026-11-01T10:00:00Z",
                              "name": "brave-otter-tulip"})
    st.record("calm-heron-maple", "start", "ended-id", "ended-run")
    (isolated / "state" / "sandbox-ai" / "teams" / "review").mkdir(parents=True)
    taken = st.names_taken()
    assert {"brave-otter-tulip", "kept-id", "calm-heron-maple", "review"} <= taken
    assert cn.free_codename(taken, chooser("brave-otter-tulip", "calm-heron-maple", "keen-wren-moss")) == \
        "keen-wren-moss"


def test_the_launcher_names_a_start_by_the_style(isolated):
    workspace = str(isolated / "shop")
    a = launcher.parse_args(["claude", "--workspace", workspace])
    assert CODENAME.fullmatch(launcher.name_start(a))
    a = launcher.parse_args(["claude", "--workspace", workspace, "-nv", "project"])
    assert re.fullmatch(r"claude-shop-[0-9a-f]{4}", launcher.name_start(a))
    # "name_style" of your defaults, checked when it is read; the option wins over it.
    defaults = isolated / "config" / "sandbox-ai" / "defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"version": 1, "name_style": "project"}))
    assert launcher.load_user_defaults(launcher.parse_args(["claude", "--workspace", workspace]))["name_style"] == \
        "project"
    defaults.write_text(json.dumps({"version": 1, "name_style": "words"}))
    with pytest.raises(ValueError, match="name_style is one of codename, project"):
        launcher.load_user_defaults(launcher.parse_args(["claude", "--workspace", workspace]))
    with pytest.raises(SystemExit):
        launcher.parse_args(["claude", "--name-style", "words"])
    # --name wins over both; a session's own name too.
    a = launcher.parse_args(["claude", "--workspace", workspace, "--name", "review", "--name-style", "project"])
    assert launcher.name_start(a) == "review"


# --- rename to a fresh codename --------------------------------------------------------------------------------

def make_kept(key, name=None):
    directory = sessions.session_dir(key)
    (directory / "home").mkdir(parents=True)
    sessions.save(key, {"agent": "codex", "options": [], "agent_args": [], "backend": "podman",
                        "workspace": "/srv/app", "cwd": "/", "created": "2026-11-01T10:00:00Z",
                        **({"name": name} if name else {})})


def test_rename_without_new_gives_a_fresh_codename(monkeypatch):
    make_kept("codex-app-1a2b")
    make_kept("other", "brave-otter-tulip")
    st.record("codex-app-1a2b", "start", "codex-app-1a2b")
    monkeypatch.setattr(st.module("codenames"), "free_codename",
                        lambda taken: cn.free_codename(taken, chooser("brave-otter-tulip", "calm-heron-maple")))
    lines = []
    assert st.main(["rename", "codex-app-1a2b"], out=lines.append) == 0
    assert lines == ["codex-app-1a2b is now calm-heron-maple"]
    assert sessions.load("codex-app-1a2b")["name"] == "calm-heron-maple"
    assert [entry["event"] for entry in st.history("calm-heron-maple")] == ["start", "rename"]
    monkeypatch.setattr(st.module("codenames"), "free_codename",
                        lambda taken: cn.free_codename(taken, chooser("calm-heron-maple", "keen-wren-moss")))
    assert st.main(["rename", "calm-heron-maple", "--codename"], out=lines.append) == 0
    assert sessions.load("codex-app-1a2b")["name"] == "keen-wren-moss"


def test_rename_codename_with_a_new_name_is_refused(capsys):
    make_kept("web")
    assert st.main(["rename", "web", "frontend", "--codename"]) == 1
    assert "give NEW or --codename, not both" in capsys.readouterr().err
    assert st.main(["rename", "nothing", "--codename"]) == 1
    assert "nothing named nothing" in capsys.readouterr().err
    assert sessions.load("web").get("name") is None


# --- defaults, completion --------------------------------------------------------------------------------------

def test_the_defaults_command_knows_name_style(isolated):
    d = load("defaults_for_codename_tests", "defaults_cmd.py")
    lines = []
    assert d.cli(["set", "name_style", "project"], out=lines.append) == 0
    path = isolated / "config" / "sandbox-ai" / "defaults.json"
    assert json.loads(path.read_text()) == {"version": 1, "name_style": "project"}
    with pytest.raises(ValueError, match="name_style is one of"):
        d.cli(["set", "name_style", "monkey"])
    assert json.loads(path.read_text())["name_style"] == "project"
    lines.clear()
    assert d.cli(["show", "--json"], out=lines.append) == 0
    shown = json.loads(lines[0])
    assert "name_style" in shown["keys"] and shown["name_styles"] == ["codename", "project"]


def test_completion_offers_the_style_and_codename():
    c = load("completion_for_codename_tests", "completion.py")
    assert {"codename", "project"} <= set(c.complete(["claude", "--name-style", ""]))
    assert "--name-style" in set(c.complete(["claude", "--name-s"]))
    assert "--codename" in set(c.complete(["rename", "web", "--"]))


def test_the_option_has_its_short_form_and_help():
    parser = launcher.build_parser()
    action = next(a for a in parser._actions if "--name-style" in a.option_strings)
    assert action.option_strings == ["-nv", "--name-style"] and list(action.choices) == ["codename", "project"]
    assert "--name-style" in dict(launcher.HELP_GROUPS)["Sessions, terminal and display"]
    assert "--name-style" not in launcher.CONFIG_FORBIDDEN  # a project may keep AGENT-PROJECT names
