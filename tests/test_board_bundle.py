"""sandbox-ai board export / import (0.1.99): a board or a session as one self-sufficient file, taken in elsewhere.

User 2026-11-17: "also we need an export funktion on the board/sessions ... which allows to export a complete running
board or config of a board but to be selfsustainable". End to end through the real ./sandbox-ai of two machines (two
HOMEs of tests/live_fakes.py, their fake Podman), and in-process for what an import refuses.

The permanent regression tests: a secret's value never goes into a bundle (canary scan); an imported bundle is
passed on (a login, a private folder that is not a placeholder, --llm-traffic all, a way past the VPN, hidden
characters are refused); archive members are data (no traversal, no link, no member it does not describe); nothing
is written before the plan is confirmed; a name there is reused only when it means the same."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

import live_fakes as F

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
OWN_IMAGE = f"registry.example.org/me/own-server@{DIGEST}"
CANARIES = {
    "secret": "canary-secret-value-7f3a9c1e5b",
    "llm_key": "canary-llm-key-0d8e2b4f6a",
    "login": "canary-login-file-3c5e7a9b1d",
    "git": "canary-git-credential-9e1c3a5d7f",
    "auth_file": "canary-auth-file-2b4d6f8a0c",
    "cookie": "canary-cookie-6a8c0e2f4b",
    "home": "canary-home-content-1a2b3c4d5e",
    "vpn": "canary-vpn-sealed-8f0b2d4a6c",
    "env": "canary-environment-5d7f9b1c3e",
    "service": "canary-service-password-4e6a8c0f2d",
    "token": "ghp_" + "C4n4ry" * 6,
}


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bb = load("sandbox_board_bundle_test", "board_bundle.py")


def machine(tmp_path, name):
    host = F.Host(tmp_path / name)
    host.env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1")
    return host


def given(host, *args, text):
    """`sandbox-ai ARGS` on HOST with TEXT on its standard input (a secret's value: never an argument)."""
    return subprocess.run(host.command(*args), cwd=host.workspace, env=host.env, input=text, capture_output=True,
                          text=True, timeout=120)


def ok(done):
    assert done.returncode == 0, (done.stdout, done.stderr)
    return done


def config(host):
    return host.home / ".config/sandbox-ai"


def data(host):
    return host.home / ".local/share/sandbox-ai"


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


RULESET = {"version": 1, "defaults": {"http": "ask"}, "http": [{"id": "pypi", "host": "pypi.org", "decision": "allow"}]}


def furnish(host, tmp_path):
    """HOST with one of each part a board names: a project and data folder, an MCP server of its own image (a
    folder, a secret), an LLM server (its key a secret), a ruleset file, a kit, an image variant, a preset of its own,
    secrets with canary values. The board file that names them all."""
    project, extra = tmp_path / f"{host.directory.name}-proj", tmp_path / f"{host.directory.name}-data"
    project.mkdir()
    extra.mkdir()
    ok(given(host, "secrets", "set", "MY_TOKEN", text=CANARIES["secret"] + "\n"))
    ok(given(host, "secrets", "set", "GPU_KEY", text=CANARIES["llm_key"] + "\n"))
    ok(host.run("llm", "add", "gpu", "--kind", "vllm", "--url", "http://100.100.1.5:8000", "--model", "qwen3",
                "--api-key-secret", "GPU_KEY"))
    host.add_image(OWN_IMAGE)
    write_json(config(host) / "mcp.json", {"own": {"image": OWN_IMAGE, "network": "full",
                                                  "env_secrets": {"TOKEN": "MY_TOKEN"},
                                                  "mounts": [{"target": "/data", "host": str(extra), "mode": "rw"}]}})
    rules = write_json(tmp_path / f"{host.directory.name}-rules.json", RULESET)
    skill = data(host) / "kits/mykit/claude/skills/hello/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hello\ndescription: says hello\n---\nSay hello.\n")
    ok(host.run("images", "new", "web", "--from", "claude"))
    ok(host.run("images", "extend", "web", "--add-os", "curl"))
    write_json(config(host) / "presets/mine.json", {"format": "sandbox-ai preset", "version": 1, "id": "mine",
                                                    "title": "Mine", "description": "my own preset",
                                                    "options": ["--allow-pypi"]})
    options = ["--workspace", str(project), "--mount", f"{extra}:/mnt/data:ro", "--mcp", "own", "--llm", "gpu:qwen3",
               "--rules", str(rules), "--kit", "mykit", "--image-variant", "web", "--preset", "mine",
               "--secret-env", "GH_TOKEN=MY_TOKEN", "--allow-pypi", "--memory", "4g"]
    board = write_json(tmp_path / f"{host.directory.name}-board.json",
                       {"schema": "sandbox-ai-board", "version": 1, "name": "Demo Board", "agent": "claude",
                        "options": options, "agent_args": ["--model", "sonnet"],
                        "state": {"nodes": [{"id": 1, "kind": "workspace", "props": {"path": str(project)},
                                             "x": 40.0, "y": 80.0}], "edges": [], "extra": [],
                                  "document_extra": {}}})
    return board, project, extra, rules


# --- end to end: export on one machine, import on a fresh one -------------------------------------------------------

def test_round_trip_a_board_on_a_fresh_machine_gives_the_same_start(tmp_path):
    a, b = machine(tmp_path, "laptop"), machine(tmp_path, "fresh")
    board, project, extra, rules = furnish(a, tmp_path)
    bundle = tmp_path / "demo.bundle.json"
    done = ok(a.run("board", "export", "--board", str(board), str(bundle)))
    assert "secrets by name only (never a value): GPU_KEY, MY_TOKEN" in done.stderr + done.stdout
    document = json.loads(bundle.read_text())
    assert document["schema"] == "sandbox-ai-bundle" and document["version"] == 1 and document["name"] == "demo-board"
    assert {f["default"] for f in document["folders"]} == {str(project), str(extra)}
    assert str(project) not in json.dumps(document["start"]) and "<folder:" in json.dumps(document["start"])
    assert [p["name"] for p in document["parts"]["mcp"]] == ["own"]
    assert document["parts"]["llm"][0]["record"]["api_key_secret"] == "GPU_KEY"
    assert [k["name"] for k in document["parts"]["kits"]] == ["mykit"]
    assert [v["name"] for v in document["parts"]["variants"]] == ["web"]
    assert [p["document"]["id"] for p in document["parts"]["presets"]] == ["mine"]
    assert document["parts"]["rules"][0]["ruleset"]["http"][0]["host"] == "pypi.org"
    # The fresh machine: its plan first (nothing written), then the import with its folders of its own.
    b.add_image(OWN_IMAGE)
    there = tmp_path / "fresh-proj"
    there.mkdir()
    checked = b.run("board", "import", str(bundle), "--check", "--map", f"{project}={there}", "--json")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    plan = json.loads(checked.stdout)
    assert not plan["problems"] and plan["written"] is False
    assert {(c["kind"], c["name"]) for c in plan["create"]} >= {("mcp", "own"), ("llm", "gpu"), ("kit", "mykit"),
                                                                 ("variant", "web@claude"), ("preset", "mine"),
                                                                 ("rules", "rules-1")}
    assert {m["name"] for m in plan["missing"]["secrets"]} == {"GPU_KEY", "MY_TOKEN"}
    assert not (config(b) / "mcp.json").exists() and not (config(b) / "boards").exists()
    # Without --yes and no terminal: asked first, nothing written.
    refused = b.run("board", "import", str(bundle), "--map", f"{project}={there}")
    assert refused.returncode == 1 and "it asks first" in refused.stderr
    assert not (config(b) / "mcp.json").exists()
    ok(b.run("board", "import", str(bundle), "--map", f"{project}={there}", "--yes"))
    imported = json.loads((config(b) / "boards/demo-board.json").read_text())
    expected = [str(there) if t == str(project) else t for t in json.loads(board.read_text())["options"]]
    expected[expected.index("--rules") + 1] = str(config(b) / "boards/demo-board/rules-1.rules.json")
    assert imported["options"] == expected and imported["agent_args"] == ["--model", "sonnet"]
    assert imported["state"]["nodes"][0]["props"]["path"] == str(there)
    assert json.loads(Path(expected[expected.index("--rules") + 1]).read_text()) == RULESET
    mcp = json.loads((config(b) / "mcp.json").read_text())["own"]
    assert mcp["mounts"] == [{"target": "/data", "host": str(extra), "mode": "rw"}] and mcp["image"] == OWN_IMAGE
    assert json.loads((config(b) / "llm.json").read_text())["endpoints"]["gpu"]["api_key_secret"] == "GPU_KEY"
    assert (data(b) / "kits/mykit/claude/skills/hello/SKILL.md").read_text().endswith("Say hello.\n")
    assert json.loads((config(b) / "images/variants/claude/web/image.json").read_text())["apt"] == ["curl"]
    assert json.loads((config(b) / "presets/mine.json").read_text())["id"] == "mine"
    # The start it imported is the launcher's: its dry run passes there (secrets kept first: they are named only).
    for name, value in (("MY_TOKEN", "x" * 12), ("GPU_KEY", "y" * 12)):
        ok(given(b, "secrets", "set", name, text=value + "\n"))
    shown = ok(b.run("board", "show", "demo-board", "--check", "--json"))
    result = json.loads(shown.stdout)
    assert result["check"]["status"] == 0 and result["options"] == expected
    listed = json.loads(ok(b.run("board", "list", "--json")).stdout)
    assert [row["name"] for row in listed] == ["demo-board"] and listed[0]["from"]["kind"] == "board"
    # Again: the same parts are reused, the board's name is refused unless replaced or renamed.
    again = b.run("board", "import", str(bundle), "--map", f"{project}={there}", "--yes", "--json")
    assert again.returncode == 1
    plan = json.loads(again.stdout)
    assert [p["kind"] for p in plan["problems"]] == ["name"]
    assert "--replace board:demo-board" in plan["problems"][0]["fix"]
    assert {(r["kind"], r["name"]) for r in plan["reuse"]} >= {("mcp", "own"), ("llm", "gpu"), ("kit", "mykit")}
    ok(b.run("board", "import", str(bundle), "--map", f"{project}={there}", "--yes", "--as", "demo-2"))
    ok(b.run("board", "delete", "demo-2"))
    assert not (config(b) / "boards/demo-2.json").exists()


def test_a_part_defined_otherwise_there_is_refused_unless_renamed_or_replaced(tmp_path):
    a, b = machine(tmp_path, "laptop"), machine(tmp_path, "other")
    board, project, _extra, _rules = furnish(a, tmp_path)
    bundle = tmp_path / "b.json"
    ok(a.run("board", "export", "--board", str(board), str(bundle)))
    write_json(config(b) / "mcp.json", {"own": {"command": ["python3", "-m", "http.server"]}})
    done = b.run("board", "import", str(bundle), "--check", "--json")
    plan = json.loads(done.stdout)
    assert done.returncode == 1 and any("has an MCP server own already" in p["text"] for p in plan["problems"])
    assert any("--rename mcp:own=NEWNAME" in p["fix"] for p in plan["problems"])
    renamed = json.loads(b.run("board", "import", str(bundle), "--check", "--json", "--rename", "mcp:own=own2").stdout)
    assert not [p for p in renamed["problems"] if p["kind"] == "mcp"]
    options = renamed["start"]["options"]
    assert options[options.index("--mcp") + 1] == "own2"
    replaced = json.loads(b.run("board", "import", str(bundle), "--check", "--json", "--replace", "mcp:own").stdout)
    assert {"kind": "mcp", "name": "own"} in replaced["replace"]


def test_a_session_is_exported_with_its_project_files_servers_and_ruleset(tmp_path):
    """A session (running or not): its saved options with what its trusted project file adds - the project's own MCP
    servers and ruleset go along as parts (on the other machine: yours, no project file needed)."""
    a, b = machine(tmp_path, "laptop"), machine(tmp_path, "fresh")
    sessions = load("sandbox_sessions_for_bundle_test", "sessions.py")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({
        "version": 1, "agent": "codex", "options": ["--allow-pypi"],
        "mcp_servers": {"files": {"command": ["python3", "-m", "files_server"], "options": ["--offline"]}},
        "rules": RULESET}))
    ok(a.run("wizard", "trust", str(project / ".sandbox-ai.json"), "--yes"))
    old = dict(os.environ)
    os.environ.update({k: v for k, v in a.env.items() if k.startswith(("XDG_", "HOME"))})
    try:
        key = "codex-web-1"
        (sessions.root() / key).mkdir(parents=True)
        # (as a running session's record has it: the export reads the record only, running or not)
        sessions.save(key, {"agent": "codex", "name": "web", "options": ["--workspace", str(project), "--mcp", "files",
                                                                        "--memory", "2g"],
                            "agent_args": [], "cwd": str(project), "backend": "podman", "pid": os.getpid(),
                            "run": "codex-web-1-run", "pod": "agent-session-web"})
    finally:
        os.environ.clear()
        os.environ.update(old)
    bundle = tmp_path / "s.json"
    done = a.run("board", "export", "--session", "web", str(bundle))
    assert done.returncode == 0, done.stderr
    document = json.loads(bundle.read_text())
    assert document["source"] == {"kind": "session", "name": "web"} and document["start"]["agent"] == "codex"
    files = next(p for p in document["parts"]["mcp"] if p["name"] == "files")
    assert files["project"] is True and files["kind"] == "command"
    assert [r["id"] for r in document["parts"]["rules"]] == ["project"]
    assert "--rules" in document["start"]["options"]
    plan = json.loads(b.run("board", "import", str(bundle), "--check", "--json").stdout)
    assert not plan["problems"], plan["problems"]
    options = plan["start"]["options"]
    assert options[options.index("--mcp") + 1] == "files" and "--allow-pypi" in options
    assert options[options.index("--rules") + 1].endswith("boards/web/project.rules.json")


def test_e2e_images_go_along_and_are_loaded_only_as_what_they_are(tmp_path):
    a, b = machine(tmp_path, "laptop"), machine(tmp_path, "offline")
    board, project, _extra, _rules = furnish(a, tmp_path)
    bundle = tmp_path / "demo.bundle.tar"
    ok(a.run("board", "export", "--board", str(board), str(bundle), "--images"))
    assert ["save", "--format", "oci-archive", OWN_IMAGE] in a.calls()
    with tarfile.open(bundle) as tar:
        assert tar.getnames() == ["bundle.json", "images/1.tar"]
        document = json.loads(tar.extractfile("bundle.json").read())
    assert document["images"][0]["ref"] == OWN_IMAGE and document["images"][0]["for"] == "mcp:own"
    # Without --images the import names the image as missing; with it the image is loaded (and checked first).
    plan = json.loads(b.run("board", "import", str(bundle), "--check", "--json").stdout)
    assert any(item["name"] == OWN_IMAGE for item in plan["missing"]["images"])
    ok(b.run("board", "import", str(bundle), "--images", "--yes"))
    assert b.images()[OWN_IMAGE].get("loaded")
    assert any(call[:2] == ["load", "--input"] for call in b.calls())


def test_e2e_a_hostile_archive_cannot_rename_this_machines_images(tmp_path):
    a, b = machine(tmp_path, "laptop"), machine(tmp_path, "target")
    board, *_ = furnish(a, tmp_path)
    bundle = tmp_path / "hostile.tar"
    a.env["FAKE_PODMAN_SAVE_NAMES"] = json.dumps(["localhost/claude-sandbox:latest"])
    ok(a.run("board", "export", "--board", str(board), str(bundle), "--images"))
    done = b.run("board", "import", str(bundle), "--images", "--yes")
    assert done.returncode == 1 and "names other images" in done.stderr
    assert "localhost/claude-sandbox:latest" not in b.images() and not (config(b) / "mcp.json").exists()


# --- a remote host (sandbox-ai hosts), through a fake ssh ------------------------------------------------------------

SSH = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
at = args.index("--")
host = args[at + 1].rpartition("@")[2].split(".")[0]
env = json.load(open(os.path.join(os.environ["FAKE_HOSTS"], host + ".json")))
os.chdir(env["HOME"])
os.execve("/bin/sh", ["sh", "-c", args[at + 2] if len(args) > at + 2 else "true"], env)
'''


def test_e2e_a_hosts_session_is_exported_here_and_a_bundle_imported_there(tmp_path):
    laptop, host1 = machine(tmp_path, "laptop"), machine(tmp_path, "host1")
    registry = tmp_path / "hosts"
    registry.mkdir()
    (laptop.bin / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1))
    laptop.env.update(FAKE_HOSTS=str(registry), SANDBOX_AI_SSH_MULTIPLEX="0")
    wrapper = host1.bin / "sandbox-ai"
    wrapper.write_text(f'#!/bin/sh\nexec "{ROOT / "sandbox-ai"}" "$@"\n')
    wrapper.chmod(0o755)
    (registry / "host1.json").write_text(json.dumps(host1.env))
    write_json(config(laptop) / "hosts.json", {"version": 1, "hosts": {"host1": {"ssh": "me@host1.example",
                                                                                 "port": 22}}})
    board, project, *_ = furnish(host1, tmp_path)
    # host1's board, exported by this machine: the bundle is written here, checked as one first.
    bundle = tmp_path / "from-host1.json"
    ok(laptop.run("board", "export", "--host", "host1", "--board", str(board), str(bundle)))
    document = json.loads(bundle.read_text())
    assert [p["name"] for p in document["parts"]["mcp"]] == ["own"]
    assert CANARIES["secret"] not in bundle.read_text()
    # And imported on host1 again, as another board: planned there, confirmed here, written there.
    done = laptop.run("board", "import", str(bundle), "--host", "host1", "--as", "copy", "--yes", "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (config(host1) / "boards/copy.json").exists() and not (config(laptop) / "boards").exists()


# --- never a secret's value ----------------------------------------------------------------------------------------

def test_no_secret_value_ever_goes_into_a_bundle(tmp_path):
    """Regression (canary scan): a board that uses every kind of secret-bearing part - secrets, an LLM server's key,
    the agent's login files, a Git credentials file, an auth file, a VPN profile, a cookie and other HOME contents in
    its state folder, a host variable, a service's password, a token in the task - exported: none of the values is in
    the bundle (JSON or archive), each secret named instead."""
    a = machine(tmp_path, "laptop")
    board, project, extra, rules = furnish(a, tmp_path)
    (a.home / ".claude").mkdir()
    (a.home / ".claude/.credentials.json").write_text(json.dumps({"token": CANARIES["login"]}))
    (a.home / ".git-credentials").write_text(f"https://me:{CANARIES['git']}@github.com\n")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"key": CANARIES["auth_file"]}))
    state = tmp_path / "state-home"
    (state / ".config").mkdir(parents=True)
    (state / "cookies.sqlite").write_text(CANARIES["cookie"])
    (state / ".config/notes.txt").write_text(CANARIES["home"])
    write_json(config(a) / "vpn/mv.json", {"name": "mv", "type": "wireguard", "secret": CANARIES["vpn"]})
    a.env["MY_VARIABLE"] = CANARIES["env"]
    document = json.loads(board.read_text())
    document["options"] += ["--copy-auth", "--auth-file", str(auth), "--git-credentials",
                            str(a.home / ".git-credentials"), "--copy", f"{a.home}/.claude/.credentials.json:"
                            ".claude/.credentials.json", "--state-dir", str(state), "--env", "MY_VARIABLE",
                            "--vpn", "mv", "--git-token-secret", "https://github.com=MY_TOKEN",
                            "--service", f"db:image=postgres,env=POSTGRES_PASSWORD={CANARIES['service']}"]
    document["agent_args"] = [f"use {CANARIES['token']} to push"]
    board.write_text(json.dumps(document))
    for kind in ("json", "tar"):
        bundle = tmp_path / f"canary.{kind}"
        ok(a.run("board", "export", "--board", str(board), str(bundle), *(["--images"] if kind == "tar" else [])))
        raw = bundle.read_bytes()
        for what, value in CANARIES.items():
            assert value.encode() not in raw, f"{what}'s value went into the {kind} bundle"
    exported = json.loads((tmp_path / "canary.json").read_text())
    assert {s["name"] for s in exported["secrets"]} == {"MY_TOKEN", "GPU_KEY"}
    assert {e["name"] for e in exported["env"]} == {"MY_VARIABLE"}
    assert exported["by_name"]["vpn"] == ["mv"]
    withheld = " ".join(item["text"] for item in exported["withheld"])
    for option in ("--copy-auth", "--auth-file", "--git-credentials", "--copy of a login file"):
        assert option in withheld
    assert {v["label"] for v in exported["values"]} >= {"db POSTGRES_PASSWORD", "agent argument"}
    assert str(state) in [f["default"] for f in exported["folders"]]


# --- an imported bundle is passed on ---------------------------------------------------------------------------

@pytest.fixture
def here(tmp_path, monkeypatch):
    for key, folder in (("HOME", "home"), ("XDG_CONFIG_HOME", "home/.config"), ("XDG_DATA_HOME", "home/.local/share"),
                        ("XDG_STATE_HOME", "home/.local/state"), ("XDG_CACHE_HOME", "home/.cache")):
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
    (tmp_path / "home").mkdir()
    return tmp_path


def bundle_of(options, agent_args=(), **more):
    document = {"schema": "sandbox-ai-bundle", "version": 1, "name": "given", "source": {"kind": "board", "name": "x"},
                "start": {"agent": "claude", "options": list(options), "agent_args": list(agent_args)},
                "folders": [], "values": [], "secrets": [], "env": [], "by_name": {}, "withheld": [], "images": [],
                "parts": {"presets": [], "mcp": [], "llm": [], "rules": [], "kits": [], "variants": [], "images": [],
                          "settings": {}}, "notes": []}
    document.update(more)
    return document


def planned(document, **choices):
    received = bb.read_bundle_bytes(io.BytesIO(json.dumps(document).encode()))
    return bb.plan_import(received, bb.Choices(**choices), secrets=set())


@pytest.mark.parametrize("options,said", [
    (["--auth-file", "/home/u/auth.json"], "a login or token of a file is never taken"),
    (["--git-token-file", "https://github.com=/home/u/token"], "a login or token of a file is never taken"),
    (["--copy-auth"], "a login or token of a file is never taken"),
    (["--copy", "/home/u/.claude/.credentials.json:.claude/.credentials.json"], "a login or token of a file"),
    (["--llm-traffic", "all"], "--llm-traffic all"),
    (["--vpn-bypass-host-ports"], "past its VPN or Tor"),
    (["--vpn-bypass-tcp", "10.0.0.1:22"], "past its VPN or Tor"),
    (["--mount", "/home/u/.ssh:/mnt/ssh:ro"], "as it is: a bundle's folders are placeholders"),
    (["--workspace", "/etc"], "as it is: a bundle's folders are placeholders"),
    (["--rules", "/etc/sandbox-ai/rules.json"], "a ruleset of a bundle comes with it"),
    (["--preset-value", "dir=/home/u"], "an option of one start only"),
    (["--start-id", "x"], "an option of one start only"),
    (["--no-config"], "an option of one start only"),
])
def test_what_a_passed_on_preset_may_not_carry_is_refused(here, options, said):
    plan = planned(bundle_of(["--allow-pypi", *options]))
    assert not plan["ok"] and any(said in p["text"] for p in plan["problems"]), plan["problems"]
    assert plan["start"] is None and not (here / "home/.config/sandbox-ai/boards").exists()


def test_hidden_characters_and_a_private_default_folder_are_refused(here):
    plan = planned(bundle_of(["--allow-pypi"], name="given", notes=["safe‮gnp.exe"]))
    assert any("control, direction or zero-width" in p["text"] for p in plan["problems"])
    ssh = str(here / "home/.ssh")
    os.makedirs(ssh)
    document = bundle_of(["--mount", "<folder:keys>:/mnt/keys:ro"],
                         folders=[{"marker": "<folder:keys>", "default": ssh, "label": "extra folder"}])
    plan = planned(document)
    problem = next(p for p in plan["problems"] if p["kind"] == "folder")
    assert "private location" in problem["text"] and f"--map <folder:keys>=FOLDER" in problem["fix"]
    # Chosen by the human (mounts: the human decides): warned about, never refused.
    plan = planned(document, maps=[f"<folder:keys>={ssh}"])
    assert plan["ok"] and any("your choice" in w for w in plan["warnings"])
    assert plan["start"]["options"] == ["--mount", f"{ssh}:/mnt/keys:ro"]


def test_withheld_values_and_missing_secrets_are_listed_with_how_to_give_them(here):
    document = bundle_of(["--secret-env", "GH=GH_PUSH", "--env", "LANG_X", "--identity", "work", "--vpn", "mv",
                          "--service", "db:image=postgres,env=POSTGRES_PASSWORD=<text:db-postgres-password>"],
                         values=[{"marker": "<text:db-postgres-password>", "label": "db POSTGRES_PASSWORD"}],
                         secrets=[{"name": "GH_PUSH", "for": ["--secret-env GH"]}],
                         env=[{"name": "LANG_X", "for": ["--env"]}],
                         by_name={"identities": ["work"], "vpn": ["mv"]})
    plan = planned(document)
    assert [p["fix"] for p in plan["problems"]] == ["--value db-postgres-password=VALUE"]
    assert plan["missing"]["secrets"] == [{"name": "GH_PUSH", "for": "--secret-env GH",
                                           "fix": "sandbox-ai secrets set GH_PUSH"}]
    assert [m["name"] for m in plan["missing"]["env"]] == ["LANG_X"]
    assert [m["name"] for m in plan["missing"]["identities"]] == ["work"]
    assert [m["name"] for m in plan["missing"]["vpn"]] == ["mv"]
    plan = planned(document, values=["db-postgres-password=dev-only"])
    assert plan["ok"] and "db:image=postgres,env=POSTGRES_PASSWORD=dev-only" in plan["start"]["options"]


# --- archive members are data ------------------------------------------------------------------------------------

def archive(members, document=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, content
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                data = content if isinstance(content, bytes) else json.dumps(content).encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    return stream


IMAGE = b"an image archive"
ENTRY = {"member": "images/1.tar", "ref": OWN_IMAGE, "for": "mcp:own", "size": len(IMAGE),
         "sha256": __import__("hashlib").sha256(IMAGE).hexdigest()}


@pytest.mark.parametrize("members,said", [
    ([("../evil.json", {}, "file")], "does not start with bundle.json"),
    ([("bundle.json", "/etc/passwd", "symlink")], "no regular file"),
    ([("bundle.json", None, "doc"), ("images/../../x.tar", IMAGE, "file")], "only bundle.json and images/N.tar"),
    ([("bundle.json", None, "doc"), ("/tmp/abs.tar", IMAGE, "file")], "only bundle.json and images/N.tar"),
    ([("bundle.json", None, "doc"), ("images/1.tar", "../../etc/shadow", "symlink")], "no regular file"),
    ([("bundle.json", None, "doc"), ("images/2.tar", IMAGE, "file")], "does not name"),
    ([("bundle.json", None, "doc"), ("images/1.tar", b"an image archivX", "file")], "digest"),
    ([("bundle.json", None, "doc"), ("images", None, "dir")], "no regular file"),
    ([("bundle.json", None, "doc")], "lacks images/1.tar"),
])
def test_archive_members_are_data_no_traversal_no_link(here, members, said):
    document = bundle_of(["--allow-pypi"], images=[ENTRY])
    members = [(name, document if kind == "doc" else content, "file" if kind == "doc" else kind)
               for name, content, kind in members]
    with pytest.raises(ValueError, match=said):
        bb.read_bundle_bytes(archive(members))
    assert not list(Path(bb.mcp_copy().spool_folder() or "/tmp").glob("sandbox-ai-bundle-in-*/1.tar"))


def test_a_good_archive_is_read_with_its_images_spooled_and_checked(here):
    received = bb.read_bundle_bytes(archive([("bundle.json", bundle_of(["--allow-pypi"], images=[ENTRY]), "file"),
                                             ("images/1.tar", IMAGE, "file")]))
    try:
        assert Path(received.images["images/1.tar"]).read_bytes() == IMAGE
    finally:
        received.close()
    assert not Path(received.folder).exists()


def test_kit_files_of_a_bundle_stay_inside_their_folder(here):
    for path in ("../outside.md", "/etc/x", "a/../../b", "", "a//b"):
        files = [{"path": path, "mode": 0o644, "data": base64.b64encode(b"x").decode()}]
        with pytest.raises(ValueError, match="no path inside its folder|without a path"):
            bb.checked_files(files, "kit k")
    secret = [{"path": "claude/x.md", "mode": 0o644, "data": base64.b64encode(f"token = {CANARIES['token']}".encode())
               .decode()}]
    with pytest.raises(ValueError, match="secret-looking"):
        bb.checked_files(secret, "kit k")


def test_a_bundle_that_is_not_one_is_refused(here):
    for text in (b"[]", b"{}", b'{"schema": "sandbox-ai-bundle", "version": 2}', b"not json at all"):
        with pytest.raises(ValueError):
            bb.read_bundle_bytes(io.BytesIO(text))


def test_the_command_is_listed_and_its_help_is_docker_like(tmp_path):
    main = load("sandbox_main_for_bundle_test", "main.py")
    assert main.PYTHON_COMMANDS["board"] == ("board_bundle.py",) and "\n  board " in main.help_text()
    done = subprocess.run([str(ROOT / "sandbox-ai"), "board", "--help"], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.startswith("Usage:  sandbox-ai board COMMAND")


def test_a_server_that_cannot_go_along_is_named_and_listed_as_missing_there(tmp_path):
    """An MCP or LLM server the start names that is not defined here (any more) is never guessed: named as withheld in
    the bundle, listed as missing by the import with how to add it."""
    a, b = machine(tmp_path, "laptop"), machine(tmp_path, "other")
    board = write_json(tmp_path / "b.json", {"schema": "sandbox-ai-board", "version": 1, "name": "x", "agent": "claude",
                                             "options": ["--mcp", "gone", "--llm", "nowhere:qwen", "--mcp", "demo"]})
    bundle = tmp_path / "x.json"
    ok(a.run("board", "export", "--board", str(board), str(bundle)))
    document = json.loads(bundle.read_text())
    withheld = " ".join(item["text"] for item in document["withheld"])
    assert "MCP server gone" in withheld and "LLM server nowhere" in withheld and not document["parts"]["mcp"]
    plan = json.loads(b.run("board", "import", str(bundle), "--check", "--json").stdout)
    assert [m["name"] for m in plan["missing"]["mcp"]] == ["gone"], "the built-in demo is on every machine"
    assert [m["name"] for m in plan["missing"]["llm"]] == ["nowhere"]
    assert "sandbox-ai llm add nowhere" in plan["missing"]["llm"][0]["fix"]


def test_an_mcp_server_of_a_bundle_never_mounts_a_folder_it_does_not_ask_for(here):
    """Regression (review of the import): a bundle whose MCP server names a host folder its "folders" do not list is
    refused - never mounted unasked."""
    server = {"schema": "sandbox-ai-mcp-export", "version": 1, "name": "own", "kind": "image",
              "definition": {"image": OWN_IMAGE, "mounts": [{"target": "/data", "host": "/home/u/.ssh", "mode": "rw"}]},
              "secrets": [], "paths": [], "network": {}}
    document = bundle_of(["--mcp", "own"], parts={"presets": [], "mcp": [server], "llm": [], "rules": [], "kits": [],
                                                  "variants": [], "images": [], "settings": {}})
    plan = planned(document)
    assert any("is none the bundle asks for" in p["text"] for p in plan["problems"]), plan["problems"]


def test_an_import_that_fails_half_way_leaves_nothing_behind(here, monkeypatch):
    """What an import wrote before a later step failed is taken back (a kit it replaced comes back as it was)."""
    kit = here / "home/.local/share/sandbox-ai/kits/mykit/claude"
    kit.mkdir(parents=True)
    (kit / "OLD.md").write_text("old\n")
    files = [{"path": "claude/NEW.md", "mode": 0o644, "data": base64.b64encode(b"new\n").decode()}]
    document = bundle_of(["--kit", "mykit"], parts={"presets": [], "mcp": [], "llm": [], "rules": [],
                                                    "kits": [{"name": "mykit", "files": files}], "variants": [],
                                                    "images": [], "settings": {}})
    received = bb.read_bundle_bytes(io.BytesIO(json.dumps(document).encode()))
    choices = bb.Choices(replaces=["kit:mykit"])
    plan = bb.plan_import(received, choices, secrets=set())
    assert plan["ok"] and {"kind": "kit", "name": "mykit"} in plan["replace"]
    monkeypatch.setattr(bb, "imported_state", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        bb.apply_import(plan, received, choices, out=lambda line: None)
    assert sorted(p.name for p in kit.iterdir()) == ["OLD.md"], "the kit as it was"
    assert not (here / "home/.config/sandbox-ai/boards/given.json").exists()
    assert not [p for p in kit.parent.parent.iterdir() if p.name.startswith(".")], "nothing left beside it"


@pytest.mark.parametrize("kind,old", [("preset", "mine"), ("variant", "v")])
def test_a_rename_to_a_name_that_is_no_name_is_refused_never_a_path(here, kind, old):
    """--rename KIND:OLD=NEW names a file or folder here: a NEW such as ../x would be written outside its folder."""
    files = [{"path": "image.json", "mode": 0o644, "data": base64.b64encode(json.dumps(
        {"version": 1, "agent": "claude"}).encode()).decode()}]
    parts = {"presets": [{"document": {"format": "sandbox-ai preset", "version": 1, "id": "mine", "title": "Mine",
                                     "description": "a preset of mine", "options": ["--allow-pypi"]}}]} \
        if kind == "preset" else {"variants": [{"name": "v", "agent": "claude", "files": files}]}
    document = bundle_of(["--allow-pypi"])
    document["parts"].update(parts)
    for bad in ("../escape", "a/b", "/abs", "UPPER", ".hidden"):
        plan = planned(document, renames=[f"{kind}:{old}={bad}"])
        assert not plan["ok"] and any(p["kind"] == kind and "no" in p["text"] and "name" in p["text"]
                                      for p in plan["problems"]), (bad, plan["problems"])
        assert not any(item["name"].startswith(bad.partition("@")[0]) for item in plan["create"]), plan["create"]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_an_import_that_fails_half_way_takes_back_the_image_records_it_wrote(here, monkeypatch):
    """Review 3x-3: the records of your images an import wrote (services.record_entry) stayed when a later step
    failed, and so did an LLM server's CA file - the parts of an import not taken back."""
    services = bb.module("services")
    record = {"kind": "pulled", "ref": "docker.io/library/redis:7", "image": "docker.io/library/redis@sha256:" + "a" * 64,
              "digest": "sha256:" + "a" * 64, "id": "b" * 64, "reviewed": False, "ports": [6379], "os": {}}
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-subj", "/CN=lab", "-days", "2", "-keyout", str(here / "lab.key"), "-out", str(here / "lab.crt")],
                   check=True, capture_output=True)
    lab = {"name": "lab", "record": {"kind": "openai-compatible", "url": "https://lab.example.org", "ca": "lab.pem"},
           "ca_text": (here / "lab.crt").read_text()}
    document = bundle_of([], parts={"presets": [], "mcp": [], "llm": [lab], "rules": [], "kits": [], "variants": [],
                                    "images": [{"name": "cache", "record": record}], "settings": {}})
    received = bb.read_bundle_bytes(io.BytesIO(json.dumps(document).encode()))
    choices = bb.Choices()
    plan = bb.plan_import(received, choices, secrets=set())
    assert plan["ok"], plan["problems"]
    monkeypatch.setattr(bb, "image_id", lambda image, run=None: "b" * 64)  # the image is here
    monkeypatch.setattr(bb, "imported_state", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        bb.apply_import(plan, received, choices, out=lambda line: None)
    assert "cache" not in services.read_registry(), "the record the failed import wrote is gone again"
    llm = bb.module("llm")
    assert "lab" not in llm.load_store()["endpoints"]
    assert not (llm.ca_dir() / "lab.pem").exists(), "the LLM server's CA file it wrote is gone again too"
    monkeypatch.undo()
    monkeypatch.setattr(bb, "image_id", lambda image, run=None: "b" * 64)
    bb.apply_import(bb.plan_import(received, choices, secrets=set()), received, choices, out=lambda line: None)
    assert services.read_registry()["cache"]["id"] == "b" * 64  # (and kept when the import went well)


# --- review 3x-1 (security): what a bundle someone passes on must not do to this machine -----------------------------

def oci_archive(names):
    """An image archive as `podman save --format oci-archive` writes one, naming NAMES."""
    index = {"schemaVersion": 2, "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                                "digest": DIGEST, "size": 2, "annotations": {
                                                    "org.opencontainers.image.ref.name": name}} for name in names]}
    return archive([("index.json", index, "file"), ("oci-layout", {"imageLayoutVersion": "1.0.0"}, "file")]).read()


def image_entry(ref, what, data, member="images/1.tar"):
    return {"member": member, "ref": ref, "for": what, "size": len(data),
            "sha256": __import__("hashlib").sha256(data).hexdigest()}


def own_server(image=OWN_IMAGE):
    definition = {"image": image, "network": "none"}
    return {"schema": "sandbox-ai-mcp-export", "version": 1, "name": "own", "kind": "image", "definition": definition,
            "secrets": [], "paths": [], "network": bb.mcp_copy().network_of(definition),
            "image": {"ref": image, "present": None, "size": None}}


def received_with(document, data):
    return bb.read_bundle_bytes(archive([("bundle.json", document, "file"), ("images/1.tar", data, "file")]))


@pytest.mark.parametrize("ref,what,names", [
    # an image of sandbox-ai's own (every later start of that agent would run it), tied to no part of the bundle
    ("localhost/claude-sandbox:latest", "mcp:own", ["localhost/claude-sandbox:latest"]),
    ("localhost/claude-sandbox:latest", "the agent", ["localhost/claude-sandbox:latest"]),
    ("localhost/sandbox-ai-browser:latest", "image:browser", ["localhost/sandbox-ai-browser:latest"]),
    # a part's own image whose archive also names one of sandbox-ai's own in "its repository"
    (f"localhost/claude-sandbox@{DIGEST}", "mcp:own", ["localhost/claude-sandbox:latest"]),
])
def test_an_image_of_a_bundle_is_loaded_only_for_the_part_that_uses_it_never_as_sandbox_ais_own(here, ref, what,
                                                                                                 names):
    """Review 3x-1: `board import --images` gave every archive of the bundle to `podman load` under whatever
    reference its entry said - also one no part of the bundle uses, and localhost/claude-sandbox:latest: the image
    every later start of that agent runs. An image is loaded only as the image of a part of the bundle (an MCP
    server's pinned reference, your image's record), and never under a name of sandbox-ai's own images."""
    data = oci_archive(names)
    parts = bundle_of([])["parts"]
    if ref.startswith("localhost/claude-sandbox@"):
        parts["mcp"] = [own_server(ref)]
    document = bundle_of(["--allow-pypi"], images=[image_entry(ref, what, data)], parts=parts)
    received = received_with(document, data)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    try:
        plan = bb.plan_import(received, bb.Choices(images=True), secrets=set())
        assert not plan["ok"] and any(p["kind"] == "image" for p in plan["problems"]), plan["problems"]
        with pytest.raises(ValueError):
            bb.load_images(plan, received, lambda *_: None, run)
    finally:
        received.close()
    assert not [call for call in calls if call[:2] == ["podman", "load"]], calls


def test_the_image_of_a_part_is_still_loaded(here):
    data = oci_archive([OWN_IMAGE])
    parts = bundle_of([])["parts"]
    parts["mcp"] = [own_server()]
    document = bundle_of(["--mcp", "own"], images=[image_entry(OWN_IMAGE, "mcp:own", data)], parts=parts)
    received = received_with(document, data)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    try:
        plan = bb.plan_import(received, bb.Choices(images=True), secrets=set())
        assert not [p for p in plan["problems"] if p["kind"] == "image"], plan["problems"]
        bb.load_images(plan, received, lambda *_: None, run)
    finally:
        received.close()
    assert [call[:3] for call in calls] == [["podman", "load", "--input"]]


def test_mcp_image_load_never_takes_a_name_of_sandbox_ais_own_images(here, tmp_path):
    """The same archive check for `mcp image-load` (mcp copy --copy-image): a pinned reference in the repository of
    one of sandbox-ai's own images let the archive name localhost/claude-sandbox:latest."""
    mc = bb.mcp_copy()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    with pytest.raises(ValueError, match="sandbox-ai's own"):
        mc.load_image(f"localhost/claude-sandbox@{DIGEST}", io.BytesIO(oci_archive(["localhost/claude-sandbox:latest"])),
                      run, lambda *_: None)
    assert not calls
    for name in ("localhost/claude-sandbox:latest", "localhost/codex-sandbox-gpu:latest", "localhost/team-sandbox",
                 "localhost/team-claude-qwen-sandbox:latest", "localhost/sandbox-ai-tor:latest",
                 "localhost/sandbox-ai-image-db:latest", f"localhost/sandbox-ai-cairn@{DIGEST}"):
        assert mc.own_image_name(name), name
    for name in (OWN_IMAGE, "docker.io/library/postgres:16", "localhost/my-server:latest", "ghcr.io/x/claude-sandbox"):
        assert not mc.own_image_name(name), name


def sparse_member(name, data, real_size):
    """One tar member of the old GNU sparse format: DATA at offset 0 of a file of REAL_SIZE bytes (the rest a hole)."""
    info = tarfile.TarInfo(name)
    info.type, info.size = tarfile.GNUTYPE_SPARSE, len(data)
    header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    header[386:410] = b"%011o\0%011o\0" % (0, len(data))
    header[483:495] = b"%011o\0" % real_size
    header[148:156] = b" " * 8
    header[148:156] = b"%06o\0 " % tarfile.calc_chksums(bytes(header))[0]
    return bytes(header) + data + b"\0" * (-len(data) % 512)


def test_a_sparse_member_of_an_archive_is_refused_never_blown_up_on_this_disk(here):
    """Review 3x-1: a member of GNU's sparse type counts as a regular file to tarfile and is read as its full size:
    a bundle of a few KiB wrote up to 64 archives of 64 GiB of zeros into /var/tmp before anything was asked."""
    data, real = b"x" * 512, 1 << 20
    blown = data + b"\0" * (real - len(data))
    entry = {"member": "images/1.tar", "ref": OWN_IMAGE, "for": "mcp:own", "size": real,
             "sha256": __import__("hashlib").sha256(blown).hexdigest()}
    document = json.dumps(bundle_of(["--allow-pypi"], images=[entry])).encode()
    info = tarfile.TarInfo("bundle.json")
    info.size = len(document)
    stream = io.BytesIO(info.tobuf(format=tarfile.GNU_FORMAT) + document + b"\0" * (-len(document) % 512)
                        + sparse_member("images/1.tar", data, real) + b"\0" * 1024)
    with pytest.raises(ValueError, match="no regular file"):
        bb.read_bundle_bytes(stream)
    assert not list(Path(bb.mcp_copy().spool_folder() or "/tmp").glob("sandbox-ai-bundle-in-*/1.tar"))


def test_a_bundles_start_sets_no_variable_that_changes_what_code_runs_in_an_mcp_server(here):
    """Review 3x-1: a preset passed on may not set --mcp-env NAME:LD_PRELOAD/NODE_OPTIONS/... (code of a folder the
    agent writes would run as the server, with its secrets); a bundle's start could - for a server of this machine
    too (github, with your token)."""
    for value in ("github:NODE_OPTIONS=--require=/work/x.js", "github:LD_PRELOAD=/work/x.so",
                  "github:PYTHONSTARTUP=<text:startup>"):
        plan = planned(bundle_of(["--mcp", "github", "--mcp-env", value],
                                 values=[{"marker": "<text:startup>", "label": "x"}]), values=["startup=/work/x.py"])
        assert not plan["ok"] and any("changes what code runs" in p["text"] for p in plan["problems"]), value
    assert planned(bundle_of(["--mcp", "github", "--mcp-env", "github:LOG_LEVEL=debug"]))["start"] is not None


def test_a_default_folder_with_a_mode_after_it_is_still_a_private_location(here):
    """Review 3x-1: the default "/home/u/.ssh:rw" is no folder that exists, so it was no private location - and
    --mcp-mount NAME:/data=<folder:keys> became --mcp-mount NAME:/data=/home/u/.ssh:rw."""
    ssh = str(here / "home/.ssh")
    os.makedirs(ssh)
    for default in (ssh + ":rw", ssh + ":ro"):
        document = bundle_of(["--mcp-mount", "srv:/data=<folder:keys>"],
                             folders=[{"marker": "<folder:keys>", "default": default, "label": "a folder"}])
        plan = planned(document)
        assert not plan["ok"] and any(p["kind"] == "folder" and "private location" in p["text"]
                                      for p in plan["problems"]), plan["problems"]
        with pytest.raises(ValueError, match="private location"):
            bb.apply_import(plan, None, bb.Choices())


def test_board_show_reads_the_board_kept_here_never_a_file_of_this_folder(here, monkeypatch, capsys):
    kept = {"schema": "sandbox-ai-board", "version": 1, "name": "demo", "agent": "claude", "options": ["--offline"],
            "agent_args": []}
    bb.boards_dir().mkdir(parents=True)
    bb.board_path("demo").write_text(json.dumps(kept))
    workspace = here / "workspace"
    workspace.mkdir()
    (workspace / "demo").write_text(json.dumps({**kept, "options": ["--mount", "/:/host:rw"]}))
    monkeypatch.chdir(workspace)
    shown = []
    assert bb.main(["show", "demo"], out=shown.append) == 0
    assert "--offline" in shown[0] and "--mount" not in shown[0]
    assert bb.source_of_board("demo")["options"] == ["--offline"], "an export by name: the board kept here"
    assert bb.source_of_board("./demo")["options"] == ["--mount", "/:/host:rw"], "./NAME: the file"
    assert bb.main(["show", "./demo"], out=shown.append) == 1 and "no board" in capsys.readouterr().err


def test_the_plan_says_where_a_bundles_llm_server_sends_the_conversation_and_what_its_mcp_server_runs(here):
    """Review 3x-1: the plan named a bundle's LLM server ("creates: llm fast") but not its address - where every
    prompt and file of a start with --llm fast goes - nor the image or command of an MCP server it brings. What is
    confirmed says both."""
    parts = bundle_of([])["parts"]
    parts["llm"] = [{"name": "fast", "record": {"kind": "openai-compatible", "url": "https://llm.elsewhere.example/v1"}}]
    parts["mcp"] = [own_server()]
    plan = planned(bundle_of(["--llm", "fast", "--mcp", "own"], parts=parts))
    said = "\n".join(plan["notes"])
    assert "https://llm.elsewhere.example" in said and "LLM server fast" in said, plan
    assert OWN_IMAGE in said and "MCP server own" in said
    assert "https://llm.elsewhere.example" in bb.plan_text(plan)


def test_a_builtin_ruleset_travels_by_its_name():
    """--rules builtin:NAME is sandbox-ai's own ruleset (every installation has it): exported as it is, no file read."""
    e = bb.Export()
    assert bb.export_option(e, "--rules", "builtin:destructive", False, set(), None) == ["--rules", "builtin:destructive",
                                                                                         False]
    assert e.parts["rules"] == []
