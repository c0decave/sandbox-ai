"""sandbox-ai mcp update (the user, 2026-11-23: "add mcp update").

A definition keeps the digest it was added with: the signed catalog of 2026-11-22 pinned rebuilt images of radare2 and
binwalk (with the mount points /work_ro, /work_rw, /app, /srv), and the user's definitions went on running the old
ones - nothing said so, and only `mcp remove` + `mcp add` took the new image (and dropped the folders, variables and
limits chosen). Now every catalog read keeps what it pins; `mcp list`, `mcp show`, doctor and a start say when a
definition runs another image than its catalog pins, the fix plan offers the update, and `mcp update [NAME...]` takes
it: the catalog read again, its signature checked as `mcp add` checks it, everything else of the definition kept,
asked first.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_mcp_update_test", "agent_sandbox.py")
m = load("sandbox_mcp_update_test", "mcp.py")

RADARE2 = {"id": "mcp:radare2", "name": "radare2", "title": "radare2 (r2mcp)", "kind": "oci",
           "image": "ghcr.io/c0decave/sandbox-ai-mcp-radare2", "digest": OLD, "network": "none",
           "mounts": [{"target": "/work", "required": True,
                       "description": "The folder with the binaries (mount it read-only: --mount /work=DIR:ro)"}]}
DDG = {"id": "mcp:duckduckgo", "name": "duckduckgo", "title": "DuckDuckGo", "kind": "oci",
       "image": "ghcr.io/c0decave/sandbox-ai-mcp-duckduckgo", "digest": OLD, "network": "allowlist",
       "hosts": ["html.duckduckgo.com:443"]}


@pytest.fixture(scope="module")
def mcp_key(tmp_path_factory):
    directory = tmp_path_factory.mktemp("mcp-update-key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(directory / "key")],
                   check=True)
    kind, key = (directory / "key.pub").read_text().split()[:2]
    k = m.release_keys()
    (directory / "signers").write_text(k.line(kind, key, k.MCP) + "\n")
    return directory


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, mcp_key):
    for variable, folder in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data"),
                             ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(variable, str(tmp_path / folder))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    monkeypatch.setitem(m.release_keys().MCP, "signers", mcp_key / "signers")


def catalog(tmp_path, entries, key=None, signed=True):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": entries}))
    if signed:
        k = m.release_keys()
        Path(str(path) + ".sig").write_bytes(k.sign(path.read_bytes(), key, k.MCP))
    elif Path(str(path) + ".sig").exists():
        Path(str(path) + ".sig").unlink()
    return str(path)


def added(tmp_path, mcp_key, *entries, signed=True, **options):
    source = catalog(tmp_path, list(entries), mcp_key / "key", signed)
    for entry in entries:
        m.add(entry["name"], catalogs=[("c", source)], pull=False, out=lambda line: None, unsigned=not signed,
              **options)
    return source


def test_a_newer_signed_image_is_said_and_taken_with_everything_else_kept(tmp_path, mcp_key, capsys):
    workspace = tmp_path / "samples"
    workspace.mkdir()
    source = added(tmp_path, mcp_key, RADARE2, mounts=[f"/work={workspace}:ro"], memory="1g", env=())
    m.set_server("radare2", {"tool_timeout": 900}, out=lambda line: None)
    before = m.definitions()["radare2"]
    catalog(tmp_path, [{**RADARE2, "digest": NEW}], mcp_key / "key")
    m.catalog_entries([("c", source)])  # (the catalog read again: mcp catalog, the app's catalog page)
    newer = m.server_list()[[e["name"] for e in m.server_list()].index("radare2")]["newer"]
    assert newer == {"image": [f"{RADARE2['image']}@{OLD}", f"{RADARE2['image']}@{NEW}"], "signed": True}
    assert m.cli(["list"]) == 0
    listed = " ".join(capsys.readouterr().out.split())
    assert "radare2: its signed catalog pins a newer image 111111111111 -> 222222222222: sandbox-ai mcp update " \
           "radare2" in listed, listed
    assert m.cli(["show", "radare2"]) == 0
    assert "sandbox-ai mcp update radare2" in capsys.readouterr().out
    # --check says it and changes nothing
    result = m.update_servers(["radare2"], check=True, out=lambda line: None)
    assert result["updated"] == [] and m.definitions()["radare2"] == before
    pulled = []
    m.pull_image = lambda image, local, out: pulled.append(image)
    result = m.update_servers(["radare2"], yes=True, out=lambda line: None)
    after = m.definitions()["radare2"]
    assert result["updated"] == ["radare2"] and pulled == [f"{RADARE2['image']}@{NEW}"]
    assert after["image"] == f"{RADARE2['image']}@{NEW}"
    assert {k: v for k, v in after.items() if k != "image"} == {k: v for k, v in before.items() if k != "image"}, \
        "its folders, limits, tool timeout, signature and catalog stay"
    assert m.server_list()[[e["name"] for e in m.server_list()].index("radare2")]["newer"] is None


def test_an_unsigned_catalog_never_updates_a_signed_server(tmp_path, mcp_key):
    source = added(tmp_path, mcp_key, RADARE2)
    catalog(tmp_path, [{**RADARE2, "digest": NEW}], signed=False)
    rows = m.update_plan(["radare2"], catalogs=[("c", source)], unsigned=True)
    assert rows[0]["state"] == "refused" and "not signed by a trusted key" in rows[0]["why"]
    assert m.update_servers(["radare2"], yes=True, unsigned=True, out=lambda line: None)["updated"] == []
    assert m.definitions()["radare2"]["image"].endswith(OLD)


def test_a_signature_that_does_not_verify_is_refused(tmp_path, mcp_key):
    source = added(tmp_path, mcp_key, RADARE2)
    path = Path(catalog(tmp_path, [{**RADARE2, "digest": NEW}], mcp_key / "key"))
    path.write_text(path.read_text().replace(NEW, "sha256:" + "3" * 64))  # changed after it was signed
    rows = m.update_plan(["radare2"], catalogs=[("c", source)])
    assert rows[0]["state"] == "refused" and "does not verify" in rows[0]["why"]


def test_one_added_unsigned_takes_an_unsigned_image_only_when_asked_and_never_in_high(tmp_path, mcp_key):
    added(tmp_path, mcp_key, RADARE2, signed=False)
    catalog(tmp_path, [{**RADARE2, "digest": NEW}], signed=False)
    assert m.update_plan(["radare2"])[0]["state"] == "refused"  # (without --unsigned)
    for mode in ("high", "paranoid"):
        row = m.update_plan(["radare2"], unsigned=True, mode=mode)[0]
        assert row["state"] == "refused" and f"security mode {mode}" in row["why"]
    row = m.update_plan(["radare2"], unsigned=True, mode="medium")[0]
    assert row["state"] == "newer" and row["signed_by"] is None
    m.pull_image = lambda image, local, out: None
    assert m.update_servers(["radare2"], yes=True, unsigned=True, out=lambda line: None)["updated"] == ["radare2"]
    assert m.definitions()["radare2"]["signed_by"] is None


def test_an_allowlist_server_takes_its_new_hosts_and_says_them(tmp_path, mcp_key):
    added(tmp_path, mcp_key, DDG)
    catalog(tmp_path, [{**DDG, "digest": NEW, "hosts": ["html.duckduckgo.com:443", "duckduckgo.com:443"]}],
            mcp_key / "key")
    said = []
    m.pull_image = lambda image, local, out: None
    m.update_servers(["duckduckgo"], yes=True, out=said.append)
    assert any("its hosts change: +duckduckgo.com:443" in line for line in said), said
    assert m.definitions()["duckduckgo"]["hosts"] == ["html.duckduckgo.com:443", "duckduckgo.com:443"]
    assert m.definitions()["duckduckgo"]["network"] == "allowlist"


def test_without_a_terminal_and_without_yes_nothing_is_updated(tmp_path, mcp_key, monkeypatch):
    added(tmp_path, mcp_key, RADARE2)
    catalog(tmp_path, [{**RADARE2, "digest": NEW}], mcp_key / "key")
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(ValueError, match="give --yes"):
        m.update_servers(["radare2"], out=lambda line: None)
    asked = []
    m.update_servers(["radare2"], out=lambda line: None, ask=lambda question: asked.append(question) or False)
    assert asked and "Update radare2 now" in asked[0]
    assert m.definitions()["radare2"]["image"].endswith(OLD)


def test_a_running_instance_runs_on_until_stopped(tmp_path, mcp_key, monkeypatch):
    added(tmp_path, mcp_key, RADARE2)
    catalog(tmp_path, [{**RADARE2, "digest": NEW}], mcp_key / "key")
    m.pull_image = lambda image, local, out: None
    monkeypatch.setattr(m, "instances", lambda name, include_stopped=False: ["radare2.abcd1234"])
    stopped = []
    monkeypatch.setattr(m, "stop", lambda instance: stopped.append(instance))
    said = []
    m.update_servers(["radare2"], yes=True, out=said.append)
    assert stopped == [] and any("with the old image until it is stopped" in line for line in said)
    catalog(tmp_path, [{**RADARE2, "digest": "sha256:" + "4" * 64}], mcp_key / "key")
    m.update_servers(["radare2"], yes=True, stop_running=True, out=said.append)
    assert stopped == ["radare2.abcd1234"]


def test_unknown_names_and_servers_of_no_catalog(tmp_path, mcp_key):
    with pytest.raises(ValueError, match="no MCP server nothere"):
        m.update_plan(["nothere"])
    config = m.config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"mine": {"image": f"ghcr.io/me/x@{OLD}", "network": "none"}}))
    row = m.update_plan(["mine"])[0]
    assert row["state"] == "unknown" and "not a catalog server" in row["why"]


def test_the_start_notes_it_and_the_fix_plan_offers_it(tmp_path, mcp_key, monkeypatch, capsys):
    source = added(tmp_path, mcp_key, RADARE2)
    catalog(tmp_path, [{**RADARE2, "digest": NEW}], mcp_key / "key")
    m.catalog_entries([("c", source)])
    monkeypatch.setattr(s, "load_mcp_module", lambda: m)
    workspace = tmp_path / "project"
    workspace.mkdir()
    a = s.parse_args(["codex", "--mcp", "radare2", "--workspace", str(workspace)])
    fixes = s.mcp_update_fixes(a)
    assert [f["id"] for f in fixes] == ["mcp-update:radare2"]
    assert fixes[0]["commands"] == [["sandbox-ai", "mcp", "update", "radare2", "--yes"]]
    assert fixes[0]["needed"] is False and fixes[0]["unsigned"] is False
    assert "mcp-update:radare2" in [f["id"] for f in s.optional_fixes(a)]
    assert any("sandbox-ai mcp update radare2" in note for note in s.mcp_update_notes(a))


# --- end to end through ./sandbox-ai -------------------------------------------------------------------------------

def test_e2e_mcp_update_through_sandbox_ai(tmp_path):
    host = Host(tmp_path / "h")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [RADARE2]}))
    done = host.run("mcp", "add", "radare2", "--catalog", str(path), "--no-pull", "--unsigned", timeout=120)
    assert done.returncode == 0, done.stderr
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1,
                                "entries": [{**RADARE2, "digest": NEW}]}))
    assert host.run("mcp", "catalog", "--catalog", str(path), timeout=120).returncode == 0
    listed = host.run("mcp", "list", timeout=60)
    assert "sandbox-ai mcp update radare2 --unsigned" in " ".join(listed.stdout.split()), listed.stdout
    checked = host.run("mcp", "update", "--check", "--unsigned", "--json", timeout=120)
    assert checked.returncode == 0, checked.stderr
    rows = json.loads(checked.stdout)["servers"]
    assert [(r["server"], r["state"]) for r in rows] == [("radare2", "newer")]
    refused = host.run("mcp", "update", "radare2", "--unsigned", "--mode", "high", "--yes", timeout=120)
    assert "security mode high" in refused.stdout + refused.stderr
    no_terminal = host.run("mcp", "update", "radare2", "--unsigned", timeout=120)
    assert no_terminal.returncode != 0 and "give --yes" in no_terminal.stderr, no_terminal.stderr
    updated = host.run("mcp", "update", "radare2", "--unsigned", "--yes", "--no-pull", timeout=120)
    assert updated.returncode == 0, updated.stderr
    definition = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["radare2"]
    assert definition["image"] == f"{RADARE2['image']}@{NEW}"
    assert definition["mounts"] == [{"target": "/work", "host": "@workspace", "mode": "ro"}]
    assert "sandbox-ai mcp update" not in host.run("mcp", "list", timeout=60).stdout


def test_doctor_says_which_servers_have_a_newer_image():
    doctor = load("doctor_mcp_update_test", "doctor.py")
    newer = {"image": [f"x@{OLD}", f"x@{NEW}"], "signed": True}
    servers = [{"name": "radare2", "own": True, "kind": "image", "definition": {"source": "mcp:radare2"},
                "newer": newer},
               {"name": "time", "own": True, "kind": "image", "definition": {"source": "mcp:time"}, "newer": None}]
    state, name, detail, fix = doctor.check_mcp_images(servers)
    assert (state, name) == ("warn", "MCP images") and "radare2" in detail and fix == "sandbox-ai mcp update radare2"
    assert doctor.check_mcp_images(servers[1:])[0] == "ok"
    assert doctor.check_mcp_images([])[0] == "skip"
