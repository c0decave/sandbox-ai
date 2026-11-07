"""sandbox-ai presets: the format, the built-in presets, values, expansion, the CLI. Starting with each preset is
tested in test_feature_matrix.py (test_presets: every built-in preset passes the checks of a dry run)."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_presets_test", ROOT / "sandbox/presets.py")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "sandbox-ai" / "presets"


def minimal(**fields):
    return {"format": "sandbox-ai preset", "version": 1, "id": "mine", "title": "Mine", "description": "d", **fields}


def test_every_builtin_preset_is_valid_and_named_by_its_file():
    files = sorted((ROOT / "sandbox" / "presets").glob("*.json"))
    assert len(files) >= 24
    for path in files:
        document = json.loads(path.read_text())
        assert p.problems(document, path.name) == [], path.name
        assert document["id"] == path.stem
        assert document["category"] in p.CATEGORIES
        # Placeholders in the first task are declared too (the launcher fills them in).
        for name in p.PLACEHOLDER.findall(document.get("prompt", "")):
            assert name in document.get("values", {}), (path.name, name)
        # No preset leaves the agent without its provider, nor copies a login in.
        assert "--offline" not in document["options"] and "--copy-auth" not in document["options"]


def test_the_presets_the_user_asked_for_are_there():
    ids = set(p.catalog())
    asked = {"hardware-analysis", "software-analysis", "network-analysis", "development",
             "time-capsule", "commit-censor", "chaos-monkey", "docs-lie-detector", "onboarding-buddy", "mini-ctf",
             "canary-course", "network-maze", "memory-hog", "archivist-1995", "radio-sandbox-ai", "agent-race",
             "pixel-art-factory", "sand-chess"}
    assert asked <= ids, asked - ids


def test_problems_name_what_is_wrong():
    assert p.problems(minimal()) == []
    for document, text in ((minimal(format="x"), '"format"'), (minimal(id="Bad Id"), "id is lower-case"),
                           (minimal(options=["--copy-auth"]), "--copy-auth is not allowed"),
                           (minimal(options=["--rules=x"]), "--rules is not allowed"),
                           (minimal(options=["{undeclared}"]), "not declared"),
                           (minimal(values={"x": {"kind": "weird"}}), "value 'x'"),
                           (minimal(rules="../escape.json"), "rules names a JSON file"),
                           (minimal(agents=["gpt"]), "agents is a list"), (minimal(extra=1), "unknown keys"),
                           (minimal(category="other"), "category"), ("[]", "not a JSON object")):
        assert any(text in problem for problem in p.problems(document)), (document, p.problems(document))


def test_values_are_checked_filled_in_and_optional_ones_drop_their_flag(tmp_path):
    preset = {**minimal(options=["--mount", "{folder}:/mnt/x:ro", "--gpu", "{gpu}", "--allow-host-port", "{port}"],
                        values={"folder": {"label": "F", "kind": "folder"}, "gpu": {"label": "G", "optional": True},
                                "port": {"label": "P", "kind": "port", "default": "3000"}}),
              "_path": str(tmp_path / "mine.json")}
    assert p.expand(preset, {"folder": "/data"}) == ["--mount", "/data:/mnt/x:ro", "--allow-host-port", "3000"]
    assert p.expand(preset, {"folder": "/data", "gpu": "dri", "port": "8080"}) == \
        ["--mount", "/data:/mnt/x:ro", "--gpu", "dri", "--allow-host-port", "8080"]
    with pytest.raises(ValueError, match="needs: --preset-value folder=... \\(F\\)"):
        p.expand(preset, {})
    for given, text in (({"folder": "rel"}, "absolute path"), ({"folder": "/x", "port": "70000"}, "port from 1"), ({"folder": "/x", "port": "0"}, "must be a port"),
                        ({"folder": "/x", "port": "abc"}, "must be a port"), ({"folder": "/x\n"}, "control"),
                        ({"folder": "/x", "nope": "1"}, "has no value nope")):
        with pytest.raises(ValueError, match=text):
            p.expand(preset, given)
    assert p.expand(preset, {"folder": "~/samples"})[1].startswith(str(Path.home()))


def test_apply_puts_the_preset_first_and_the_task_last(own_config):
    own_config.mkdir(parents=True)
    (own_config / "mine.json").write_text(json.dumps(minimal(
        options=["--memory", "2g", "--mount", "{dir}:/mnt/d:ro"], mcp=["hub:capa"], rules="mine.rules.json",
        values={"dir": {"label": "D", "kind": "folder"}}, prompt="look at {dir}")))
    argv = p.apply(["claude", "--memory", "4g", "--preset", "mine", "--preset-value=dir=/data", "--gui"])
    assert argv == ["claude", "--memory", "2g", "--mount", "/data:/mnt/d:ro", "--mcp", "capa",
                    "--rules", str(own_config / "mine.rules.json"), "--memory", "4g", "--gui", "--", "look at /data"]
    # The agent's own arguments replace the task; opencode takes it as --prompt.
    assert p.apply(["claude", "--preset", "mine", "--preset-value", "dir=/d", "--", "mine"])[-2:] == ["--", "mine"]
    assert p.apply(["opencode", "--preset", "mine", "--preset-value", "dir=/d"])[-3:] == ["--", "--prompt", "look at /d"]
    assert p.apply(["claude", "--gui"]) == ["claude", "--gui"]  # no preset: unchanged
    for argv, text in ((["claude", "--preset"], "needs a value"), (["claude", "--preset", "a", "--preset", "b"],
                                                                     "only one --preset"),
                       (["claude", "--preset", "absent"], "no preset 'absent'")):
        with pytest.raises(ValueError, match=text):
            p.apply(argv)


def test_yours_win_and_damaged_ones_are_skipped(own_config):
    own_config.mkdir(parents=True)
    (own_config / "development.json").write_text(json.dumps(minimal(id="development", title="My development")))
    (own_config / "broken.json").write_text("{")
    (own_config / "notes.rules.json").write_text("{}")  # a ruleset beside the presets is not a preset
    damaged = []
    presets = p.catalog(damaged)
    assert presets["development"]["title"] == "My development" and presets["development"]["_origin"] == "yours"
    assert len(damaged) == 1 and "broken.json" in damaged[0] and "notes" not in presets


def write_defaults(own_config, **values):
    path = own_config.parent / "defaults.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, **values}))
    path.chmod(0o600)
    return path


@pytest.fixture
def no_sandboxes(monkeypatch):
    """Where sandboxes write: nothing but what a test puts here."""
    trees = []
    monkeypatch.setattr(p, "EXPOSED_BEFORE", lambda: list(trees))
    return trees


def test_extra_preset_directories_in_their_order_between_yours_and_the_built_in(own_config, tmp_path, no_sandboxes):
    first, second = tmp_path / "first", tmp_path / "second"
    for directory, title in ((first, "First"), (second, "Second")):
        directory.mkdir()
        (directory / "shared.json").write_text(json.dumps(minimal(id="shared", title=title)))
        (directory / "development.json").write_text(json.dumps(minimal(id="development", title=title)))
    (second / "only-second.json").write_text(json.dumps(minimal(id="only-second")))
    write_defaults(own_config, preset_dirs=[str(first), str(second)])
    presets = p.catalog()
    assert presets["shared"]["title"] == "First" and presets["shared"]["_origin"] == "extra"
    assert presets["shared"]["_dir"] == str(first) and presets["only-second"]["_dir"] == str(second)
    assert presets["development"]["title"] == "First", "an extra directory wins over a built-in preset"
    assert presets["software-analysis"]["_origin"] == "built-in"
    own_config.mkdir(parents=True)
    (own_config / "shared.json").write_text(json.dumps(minimal(id="shared", title="Mine")))
    assert p.catalog()["shared"]["_origin"] == "yours", "yours win over every extra directory"
    assert p.public(p.catalog()["only-second"])["directory"] == str(second)


def test_an_extra_directory_where_sandboxes_write_is_skipped(own_config, tmp_path, no_sandboxes):
    workspace = tmp_path / "project"
    inside = workspace / "presets"
    inside.mkdir(parents=True)
    (inside / "planted.json").write_text(json.dumps(minimal(id="planted")))
    write_defaults(own_config, preset_dirs=[str(inside)])
    assert "planted" in p.catalog()
    no_sandboxes.append(workspace)
    problems = []
    assert "planted" not in p.catalog(problems)
    assert any("skipped" in problem and str(workspace) in problem for problem in problems)
    # Through a symlink to it: the same.
    link = tmp_path / "link"
    link.symlink_to(inside)
    write_defaults(own_config, preset_dirs=[str(link)])
    assert "planted" not in p.catalog()
    # Not knowing where sandboxes write: no extra directory is trusted.
    p.EXPOSED_BEFORE = lambda: (_ for _ in ()).throw(OSError("unreadable registry"))
    problems = []
    assert "planted" not in p.catalog(problems) and "cannot tell where sandboxes write" in problems[0]


def test_symlinked_presets_and_rulesets_in_an_extra_directory_are_not_followed(own_config, tmp_path, no_sandboxes):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "target.json").write_text(json.dumps(minimal(id="linked")))
    (elsewhere / "rules.json").write_text("{}")
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "linked.json").symlink_to(elsewhere / "target.json")
    (extra / "ruled.json").write_text(json.dumps(minimal(id="ruled", rules="ruled.rules.json")))
    (extra / "ruled.rules.json").symlink_to(elsewhere / "rules.json")
    (extra / "fine.json").write_text(json.dumps(minimal(id="fine")))
    write_defaults(own_config, preset_dirs=[str(extra)])
    problems = []
    presets = p.catalog(problems)
    assert "fine" in presets and "linked" not in presets and "ruled" not in presets
    assert any("a symlink, not followed" in problem for problem in problems)
    assert any("ruleset ruled.rules.json is a symlink" in problem for problem in problems)


def test_a_damaged_preset_dirs_value_is_said_not_fatal(own_config):
    write_defaults(own_config, preset_dirs="/not/a/list")
    problems = []
    assert "software-analysis" in p.catalog(problems) and "preset_dirs is a list" in problems[0]
    with pytest.raises(ValueError, match="twice"):
        p.check_dirs(["/a", "/a/"])
    with pytest.raises(ValueError, match="absolute"):
        p.check_dirs(["relative"])


def test_the_dirs_commands(capsys, own_config, tmp_path, no_sandboxes):
    team = tmp_path / "team"
    team.mkdir()
    (team / "team-tool.json").write_text(json.dumps(minimal(id="team-tool")))
    assert p.main(["dirs"]) == 0
    assert "no extra preset directories" in capsys.readouterr().out
    assert p.main(["dirs", "add", str(team)]) == 0
    assert f"preset directory {team} added (1 preset file(s))" in capsys.readouterr().out
    document = json.loads((own_config.parent / "defaults.json").read_text())
    assert document == {"version": 1, "preset_dirs": [str(team)]}
    assert p.main(["dirs", "add", str(team)]) == 1 and "listed already" in capsys.readouterr().err
    assert p.main(["list"]) == 0 and f"(from {team})" in capsys.readouterr().out
    assert p.main(["show", "team-tool"]) == 0 and f"from {team}" in capsys.readouterr().out
    assert p.main(["dirs", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["dirs"] == [{"path": str(team), "state": "used", "why": None, "presets": 1}]
    no_sandboxes.append(tmp_path)
    assert p.main(["dirs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dirs"][0]["state"] == "skipped"
    other = tmp_path / "other"
    other.mkdir()
    assert p.main(["dirs", "add", str(other)]) == 1
    assert "cannot hold presets: it lies in" in capsys.readouterr().err
    assert p.main(["dirs", "add", str(tmp_path / "missing")]) == 1 and "not a directory" in capsys.readouterr().err
    assert p.main(["dirs", "add", str(p.BUILTIN)]) == 1
    assert p.main(["dirs", "remove", str(team)]) == 0 and "removed" in capsys.readouterr().out
    assert json.loads((own_config.parent / "defaults.json").read_text()) == {"version": 1}
    assert p.main(["dirs", "remove", str(team)]) == 1 and "not listed" in capsys.readouterr().err


def test_missing_names_what_to_add():
    preset = p.load("software-analysis")
    found = p.missing(preset, {}, "claude", mcp_names={"capa"})
    assert any("needs: --preset-value samples=" in line for line in found)
    assert "MCP server hub:floss is not added yet: sandbox-ai mcp add hub:floss" in found
    assert not any("hub:capa" in line for line in found)
    assert p.missing(p.load("sand-chess"), {}, "codex", set())[0].startswith("preset sand-chess is for")


def test_the_cli(capsys, own_config):
    assert p.main(["list"]) == 0
    text = capsys.readouterr().out
    assert "analysis:" in text and "software-analysis" in text and "fun:" in text
    assert p.main(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {entry["origin"] for entry in listed} == {"built-in"} and all("_path" not in entry for entry in listed)
    assert p.main(["show", "mini-ctf"]) == 0
    assert "value port: Port of the CTF service on the host [port] (default 8099)" in capsys.readouterr().out
    assert p.main(["check", "time-capsule", "--agent", "claude"]) == 1
    assert "missing:" in capsys.readouterr().out
    assert p.main(["check", "time-capsule", "--agent", "claude", "--value", "question=why"]) == 0
    assert "ready; start with: sandbox-ai claude --preset time-capsule --preset-value question=why" in \
        capsys.readouterr().out
    assert p.main(["show", "nope"]) == 1
    assert "no preset 'nope'" in capsys.readouterr().err
