"""MCP servers' variables (0.1.95): required ones, optional ones, plain values that are no secret, secrets of another
name - in the definition (mcp add/set --env NAME, NAME=VALUE, NAME@SECRET), for one start (--mcp-env), at the start
(run_variables), and every problem of a start's MCP servers checked at once before anything is made (start_problems),
with an instance of the start's own where a server of that name runs with another definition."""
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_mcp_variables_test", "agent_sandbox.py")
m = load("sandbox_mcp_variables_test", "mcp.py")
IMAGE_PRESENT = m.image_present  # (the fixture replaces it on the module)
DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/c0decave/sandbox-ai-mcp-gowitness@{DIGEST}"
# The catalog's entries as sandbox-ai-mcp declares their variables ({name, description, secret, required}).
GOWITNESS = {"id": "mcp:gowitness", "name": "gowitness", "title": "gowitness", "kind": "oci",
             "image": "ghcr.io/c0decave/sandbox-ai-mcp-gowitness", "digest": DIGEST, "network": "full",
             "env": [{"name": "ALLOW_PRIVATE_TARGETS", "description": "1: also private addresses", "secret": False,
                      "required": False}]}
TELEGRAM = {"id": "mcp:telegram", "name": "telegram", "title": "Telegram", "kind": "oci",
            "image": "ghcr.io/c0decave/sandbox-ai-mcp-telegram", "digest": DIGEST, "network": "full",
            "env": [{"name": "TELEGRAM_BOT_TOKEN", "description": "bot token", "secret": True, "required": True},
                    {"name": "TELEGRAM_ALLOWED_CHATS", "description": "chat ids", "secret": False, "required": False}]}


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    for name in ("ALLOW_PRIVATE_TARGETS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHATS", "KEY"):
        monkeypatch.delenv(name, raising=False)
    # No image is pulled here: every image counts as there unless a test says otherwise. The launcher uses this
    # module (one module: what a test changes in it holds for the launcher's checks too).
    monkeypatch.setattr(m, "image_present", lambda image, run=None: True)
    monkeypatch.setattr(s, "load_mcp_module", lambda: m)


def catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": entries}))
    return [("t", str(path))]


def write_servers(servers):
    m.config_file().parent.mkdir(parents=True, exist_ok=True)
    m.config_file().write_text(json.dumps(servers))


def token():
    """A GitHub token's shape, made at run time (no scanner takes the test for a real key)."""
    return "gh" + "p_" + "A1b2C3d4" * 5


# --- one --env value -------------------------------------------------------------------------------------------------

def test_parse_env_spec(scenario):
    if scenario == "happy":
        assert m.parse_env_spec("ALLOW_PRIVATE_TARGETS") == ("ALLOW_PRIVATE_TARGETS", "pass", None)
        assert m.parse_env_spec("ALLOW_PRIVATE_TARGETS=1") == ("ALLOW_PRIVATE_TARGETS", "value", "1")
        assert m.parse_env_spec("TELEGRAM_BOT_TOKEN@MY_BOT") == ("TELEGRAM_BOT_TOKEN", "secret", "MY_BOT")
        assert m.parse_env_spec("URL=https://search.example.org/?q=a=b") == ("URL", "value",
                                                                               "https://search.example.org/?q=a=b")
    elif scenario == "sad":
        for bad in ("1X=1", "A-B=1", "X@1bad", "", "=1", None, "X@"):
            with pytest.raises(ValueError, match="--env NAME"):
                m.parse_env_spec(bad)
        with pytest.raises(ValueError, match="one line"):
            m.parse_env_spec("X=a\nb")
        with pytest.raises(ValueError, match="never kept as a plain value"):
            m.parse_env_spec("GITHUB_TOKEN=abc")
        with pytest.raises(ValueError, match="shape of a credential"):
            m.parse_env_spec(f"HEADER={token()}")
        with pytest.raises(ValueError, match="sets this variable itself"):
            m.parse_env_spec("SANDBOX_AI_MCP_MOUNTS=[]")
    else:
        assert m.parse_env_spec("EMPTY=") == ("EMPTY", "value", "")  # an empty value is a value
        assert m.parse_env_spec("MAX_TOKENS=4096")[2] == "4096"  # TOKENS is no word of a secret
        assert m.parse_env_spec("X=" + "a" * m.ENV_VALUE_LIMIT)[1] == "value"
        with pytest.raises(ValueError, match="longer than"):
            m.parse_env_spec("X=" + "a" * (m.ENV_VALUE_LIMIT + 1))
        with pytest.raises(ValueError, match="one line"):
            m.parse_env_spec("X=a\x00b")


def test_secret_looking(scenario):
    if scenario == "happy":
        assert m.secret_looking("ALLOW_PRIVATE_TARGETS", "1") is None
        assert m.secret_looking("BRIGHTDATA_SERP_ZONE", "serp_api1") is None
    elif scenario == "sad":
        for name in ("GITHUB_TOKEN", "BRAVE_API_KEY", "DB_PASSWORD", "CLIENT_SECRET", "AWS_ACCESS_KEY", "APIKEY"):
            assert "name of a secret" in m.secret_looking(name, "x"), name
        assert "GitHub token" in m.secret_looking("SOMETHING", token())
    else:
        assert m.secret_looking("MAX_TOKENS", "5") is None and m.secret_looking("KEYBOARD", "de") is None
        assert m.secret_looking("PRIVATE_TARGETS", "1") is None  # PRIVATE alone is no key


# --- definitions -----------------------------------------------------------------------------------------------------

def test_check_variables(scenario):
    base = {"image": IMAGE, "network": "full"}
    if scenario == "happy":
        definition = {**base, "env": ["A"], "env_optional": ["B"], "env_values": {"C": "1"}, "env_secrets": {"D": "S"}}
        assert m.validated({"x": definition}, "t")["x"] == definition
        assert m.validated({"x": {**base, "env": ["KEY"]}}, "t")["x"]["env"] == ["KEY"]  # an older one, unchanged
    elif scenario == "sad":
        for bad in ({"env_optional": "B"}, {"env_values": ["C=1"]}, {"env_values": {"C": 1}},
                    {"env_secrets": {"D": "not a name"}}, {"env": ["A"], "env_values": {"A": "1"}},
                    {"env_values": {"GITHUB_TOKEN": "x"}}, {"env_values": {"X": token()}},
                    {"env_values": {"X": "a\nb"}}, {"env": ["SANDBOX_AI_MCP_MOUNTS"]}):
            with pytest.raises(ValueError):
                m.validated({"x": {**base, **bad}}, "t")
    else:
        many = {f"V{i}": "1" for i in range(m.ENV_LIMIT)}
        assert m.validated({"x": {**base, "env_values": many}}, "t")
        with pytest.raises(ValueError, match="at most"):
            m.validated({"x": {**base, "env_values": many, "env": ["ONE_MORE"]}}, "t")
        with pytest.raises(ValueError, match="each variable once"):
            m.validated({"x": {**base, "env_optional": ["A"], "env_secrets": {"A": "S"}}}, "t")


def test_entry_variables(scenario):
    if scenario == "happy":
        # A non-secret optional variable is not required: NAME is passed where there is one, NAME=VALUE kept.
        assert m.entry_variables(GOWITNESS, ["ALLOW_PRIVATE_TARGETS"]) == {"env": [],
                                                                           "env_optional": ["ALLOW_PRIVATE_TARGETS"]}
        assert m.entry_variables(GOWITNESS, ["ALLOW_PRIVATE_TARGETS=1"]) == {"env": [], "env_values": {
            "ALLOW_PRIVATE_TARGETS": "1"}}
        assert m.entry_variables(GOWITNESS, []) == {"env": []}
        found = m.entry_variables(TELEGRAM, ["TELEGRAM_BOT_TOKEN@MY_BOT", "TELEGRAM_ALLOWED_CHATS=123,-100"])
        assert found == {"env": [], "env_values": {"TELEGRAM_ALLOWED_CHATS": "123,-100"},
                         "env_secrets": {"TELEGRAM_BOT_TOKEN": "MY_BOT"}}
        assert m.entry_variables(TELEGRAM, ["TELEGRAM_BOT_TOKEN"]) == {"env": ["TELEGRAM_BOT_TOKEN"]}  # required
    elif scenario == "sad":
        with pytest.raises(ValueError, match="takes TELEGRAM_BOT_TOKEN as a secret, so its value is never kept"):
            m.entry_variables(TELEGRAM, ["TELEGRAM_BOT_TOKEN=12345:abc"])
        with pytest.raises(ValueError, match="needs --env TELEGRAM_BOT_TOKEN"):
            m.entry_variables(TELEGRAM, ["TELEGRAM_ALLOWED_CHATS=1"])
        with pytest.raises(ValueError, match="does not use OTHER"):
            m.entry_variables(GOWITNESS, ["OTHER=1"])
    else:
        with pytest.raises(ValueError, match="given twice"):
            m.entry_variables(GOWITNESS, ["ALLOW_PRIVATE_TARGETS", "ALLOW_PRIVATE_TARGETS=1"])
        # A value of a secret's shape is refused even for a variable the catalog calls no secret.
        with pytest.raises(ValueError, match="shape of a credential"):
            m.entry_variables(TELEGRAM, ["TELEGRAM_BOT_TOKEN", f"TELEGRAM_ALLOWED_CHATS={token()}"])
        assert m.entry_env(TELEGRAM, ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHATS=1"]) == [
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHATS"]  # (the names, as before)


def test_mcp_catalog_json_names_each_entrys_variables(scenario, tmp_path, capsys):
    """`mcp catalog --json` (the web page's add form): every entry with what the listing says and the variables its
    catalog declares, each {name, description, secret, required}, its texts printable and bounded."""
    odd = {**TELEGRAM, "name": "odd", "title": "Odd\x1b]0;x\x07 title",
           "env": [{"name": "OK_NAME", "description": "line one\nline two" + "x" * 900, "secret": "yes"},
                   {"name": "not a name", "description": "left out"}]}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1,
                                "entries": [GOWITNESS, TELEGRAM, odd, {**GOWITNESS, "name": "later", "digest": None}]}))
    argv = ["catalog", "--json", "-c", str(path)]
    if scenario == "sad":  # a catalog that cannot be read: said in the JSON, the command still answers
        argv = ["catalog", "--json", "-c", str(tmp_path / "missing.json")]
    assert m.cli(argv) == 0
    found = json.loads(capsys.readouterr().out)
    if scenario == "sad":
        assert found["entries"] == [] and "missing.json" in found["problems"][0]
        return
    entries = {entry["name"]: entry for entry in found["entries"]}
    if scenario == "happy":
        assert entries["telegram"]["entry"] == "c0:telegram" and entries["telegram"]["signature"] == "none"
        assert entries["telegram"]["env"] == [
            {"name": "TELEGRAM_BOT_TOKEN", "description": "bot token", "secret": True, "required": True},
            {"name": "TELEGRAM_ALLOWED_CHATS", "description": "chat ids", "secret": False, "required": False}]
        assert entries["gowitness"]["published"] is True and entries["later"]["published"] is False
        assert entries["gowitness"]["network"] == "full" and found["problems"] == []
    else:  # what an (unsigned) catalog says is text only, bounded; a variable without a proper name is left out
        assert entries["odd"]["title"].isprintable() and "\x1b" not in entries["odd"]["title"]
        assert [v["name"] for v in entries["odd"]["env"]] == ["OK_NAME"]
        variable = entries["odd"]["env"][0]
        assert variable["secret"] is True and variable["required"] is False
        assert len(variable["description"]) <= 500 and "\n" not in variable["description"]


def test_mcp_add_and_set_take_values_and_secrets(scenario, tmp_path, capsys):
    sources = catalog(tmp_path, [GOWITNESS, TELEGRAM])
    if scenario == "happy":
        definition = m.add("gowitness", env=["ALLOW_PRIVATE_TARGETS=1"], catalogs=sources, pull=False,
                           out=lambda line: None, unsigned=True)
        assert definition["env_values"] == {"ALLOW_PRIVATE_TARGETS": "1"} and definition["env"] == []
        assert json.loads(m.config_file().read_text())["gowitness"]["env_values"] == {"ALLOW_PRIVATE_TARGETS": "1"}
        # mcp set changes them (all of them): back to "passed where there is one", then none.
        result = m.set_server("gowitness", {"env": ["ALLOW_PRIVATE_TARGETS"]}, catalogs=sources, out=lambda t: None)
        assert result["definition"]["env_optional"] == ["ALLOW_PRIVATE_TARGETS"]
        assert "env_values" not in result["definition"] and "env_optional" in result["changed"]
        result = m.set_server("gowitness", {"env": []}, catalogs=sources, out=lambda t: None)
        assert m.variables_of(result["definition"]) == []
    elif scenario == "sad":
        with pytest.raises(ValueError, match="value is never kept in the definition"):
            m.add("telegram", env=["TELEGRAM_BOT_TOKEN=1:x"], catalogs=sources, pull=False, out=lambda line: None,
                  unsigned=True)
        assert not m.config_file().exists()  # nothing written
    else:
        m.add("telegram", env=["TELEGRAM_BOT_TOKEN@BOT_A", "TELEGRAM_ALLOWED_CHATS=1"], catalogs=sources, pull=False,
              out=lambda line: None, unsigned=True)
        listed = {entry["name"]: entry for entry in m.server_list()}
        # mcp list --json says where each value comes from - the secret's name, never its value.
        assert listed["telegram"]["variables"] == [
            {"name": "TELEGRAM_ALLOWED_CHATS", "source": "value", "value": "1"},
            {"name": "TELEGRAM_BOT_TOKEN", "source": "secret", "secret": "BOT_A"}]


def test_cli_set_env_value(scenario, tmp_path, capsys, monkeypatch):
    """`sandbox-ai mcp set NAME --env NAME=VALUE` as the command line takes it."""
    sources = catalog(tmp_path, [GOWITNESS])
    m.add("gowitness", catalogs=sources, pull=False, out=lambda line: None, unsigned=True)
    monkeypatch.setattr(m, "catalog_entry_of", lambda name, definition, fetch=None, catalogs=None: GOWITNESS)
    if scenario == "happy":
        assert m.cli(["set", "gowitness", "--env", "ALLOW_PRIVATE_TARGETS=1", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["definition"]["env_values"] == {"ALLOW_PRIVATE_TARGETS": "1"}
    elif scenario == "sad":
        with pytest.raises(ValueError, match="does not use NOPE"):
            m.cli(["set", "gowitness", "--env", "NOPE=1"])
    else:
        with pytest.raises(ValueError, match="--no-env takes all of them away"):
            m.cli(["set", "gowitness", "--env", "ALLOW_PRIVATE_TARGETS=1", "--no-env"])


# --- at the start ----------------------------------------------------------------------------------------------------

def test_run_variables(scenario):
    definition = {"image": IMAGE, "env": ["REQ"], "env_optional": ["OPT"], "env_values": {"FLAG": "1"},
                  "env_secrets": {"BOT": "MY_BOT"}}
    secrets = {"MY_BOT": "s3"}
    if scenario == "happy":
        found, problems = m.run_variables("x", definition, {"REQ": "r", "OPT": "o"}, secrets.get)
        assert found == {"REQ": "r", "OPT": "o", "FLAG": "1", "BOT": "s3"} and problems == []
        assert m.run_definition(definition, found) == {"image": IMAGE, "env": ["REQ", "OPT", "FLAG", "BOT"]}
    elif scenario == "sad":
        found, problems = m.run_variables("x", definition, {}, {}.get)
        assert found == {"FLAG": "1"}  # the optional one is left out, the value is the definition's
        assert len(problems) == 2 and "needs the variable REQ" in problems[0] and \
            "sandbox-ai mcp set x --env REQ=VALUE" in problems[0]
        assert "takes BOT from the secret MY_BOT, which is not there: sandbox-ai secrets set MY_BOT" in problems[1]
        assert all("s3" not in problem for problem in problems)
    else:
        # The secret of the variable's name where the environment has none; the definition's value over the
        # environment's.
        found, problems = m.run_variables("x", definition, {"FLAG": "0"}, {"REQ": "from-secret", **secrets}.get)
        assert found["REQ"] == "from-secret" and found["FLAG"] == "1" and problems == []


# A definition of before 0.1.95: `mcp add --env ALLOW_PRIVATE_TARGETS` made the variable required although the
# catalog says "required": false (the user's gowitness-tor and whatweb-tor).
OLD_DEFINITION = {"image": IMAGE, "network": "full", "env": ["ALLOW_PRIVATE_TARGETS"], "source": "mcp:gowitness"}
SECRET_OPTIONAL = {**GOWITNESS, "id": "mcp:keyed", "name": "keyed", "env": [
    {"name": "API_KEY", "description": "an optional key", "secret": True, "required": False}]}


def remembered(tmp_path, entries, state="unsigned"):
    """The catalog read once (mcp catalog, mcp add): what it declares kept for the starts. Returns its source."""
    sources = catalog(tmp_path, entries)
    real = m.catalog_signature
    m.catalog_signature = lambda source, data, fetch=None: {"state": state, "reason": "test", "signer": "SHA256:x"}
    try:
        assert m.catalog_entries(sources)[1] == []
    finally:
        m.catalog_signature = real
    return sources[0][1]


def test_remember_catalog_env(scenario, tmp_path):
    if scenario == "happy":
        source = remembered(tmp_path, [GOWITNESS, TELEGRAM])
        known = json.loads(m.catalog_env_file().read_text())
        assert known[source]["mcp:gowitness"] == {"signature": "unsigned", "env": {
            "ALLOW_PRIVATE_TARGETS": {"required": False, "secret": False}},
            # (its network and hosts too: mcp list and doctor tell a full server of an allowlist entry to switch)
            "network": "full", "hosts": [],
            # (what it pins: mcp list, show, doctor and a start say a newer image - tests/test_mcp_update.py)
            "image": f"{GOWITNESS['image']}@{GOWITNESS['digest']}"}
        assert known[source]["mcp:telegram"]["env"]["TELEGRAM_BOT_TOKEN"] == {"required": True, "secret": True}
        assert oct(m.catalog_env_file().stat().st_mode & 0o777) == "0o600"
    elif scenario == "sad":  # a catalog whose signature does not verify: what it said before is forgotten
        source = remembered(tmp_path, [GOWITNESS])
        remembered(tmp_path, [GOWITNESS], state="bad")
        assert "mcp:gowitness" not in json.loads(m.catalog_env_file().read_text())[source]
    else:  # nothing said: required and a secret (only an explicit false makes a variable optional at a start)
        odd = {**GOWITNESS, "env": [{"name": "VAGUE"}, {"name": "not a name", "required": False, "secret": False}]}
        source = remembered(tmp_path, [odd])
        assert json.loads(m.catalog_env_file().read_text())[source]["mcp:gowitness"]["env"] == {
            "VAGUE": {"required": True, "secret": True}}


def test_catalog_optional(scenario, tmp_path):
    source = remembered(tmp_path, [GOWITNESS, SECRET_OPTIONAL])
    definition = {**OLD_DEFINITION, "catalog": source}
    if scenario == "happy":
        assert m.catalog_optional(definition, "ALLOW_PRIVATE_TARGETS")
    elif scenario == "sad":
        assert not m.catalog_optional({**definition, "env": ["API_KEY"], "source": "mcp:keyed"}, "API_KEY"), \
            "a secret is never left out on its own"
        assert not m.catalog_optional({**definition, "signed_by": "SHA256:x"}, "ALLOW_PRIVATE_TARGETS"), \
            "added signed: only what a signed catalog said"
        assert not m.catalog_optional({key: v for key, v in definition.items() if key != "catalog"},
                                      "ALLOW_PRIVATE_TARGETS"), "no catalog of origin: not known"
        assert not m.catalog_optional(definition, "OTHER")
    else:
        m.catalog_env_file().write_text("[damaged")
        assert not m.catalog_optional(definition, "ALLOW_PRIVATE_TARGETS")
        m.catalog_env_file().unlink()
        assert not m.catalog_optional(definition, "ALLOW_PRIVATE_TARGETS"), "never read here: not known"


def test_an_old_definition_leaves_out_what_its_catalog_declares_optional(scenario, tmp_path, monkeypatch, capsys):
    """Regression (the user's gowitness-tor and whatweb-tor): a variable the catalog declares optional, made required
    by an `mcp add` of before 0.1.95, stopped every start ("needs the variable ALLOW_PRIVATE_TARGETS"). Without a
    value it is left out now, with a note that says how to set it; with one it is passed as before."""
    source = remembered(tmp_path, [GOWITNESS])
    write_servers({"gowitness-tor": {**OLD_DEFINITION, "catalog": source}})
    a = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp", "gowitness-tor"])
    if scenario == "happy":
        problems, _instances, notes = m.start_problems(a, ["gowitness-tor"])
        assert problems == [] and notes == [
            "MCP server gowitness-tor: ALLOW_PRIVATE_TARGETS not set: left out (optional in the catalog); set it with "
            "sandbox-ai mcp set gowitness-tor --env ALLOW_PRIVATE_TARGETS=VALUE (with its other variables), or make "
            "it optional in the definition: sandbox-ai mcp set gowitness-tor --env ALLOW_PRIVATE_TARGETS"]
        s.check_mcp_names(a)
        assert "ALLOW_PRIVATE_TARGETS not set: left out (optional in the catalog)" in capsys.readouterr().err
        found, problems = m.run_variables("gowitness-tor", {**OLD_DEFINITION, "catalog": source}, {}, {}.get)
        assert found == {} and problems == [], "the server's containers do not get it"
    elif scenario == "sad":  # not known as optional (its catalog never read here): a problem as before, with the way out
        m.catalog_env_file().unlink()
        problems = m.start_problems(a, ["gowitness-tor"])[0]
        assert len(problems) == 1 and "needs the variable ALLOW_PRIVATE_TARGETS" in problems[0]
        assert "sandbox-ai mcp set gowitness-tor --env ALLOW_PRIVATE_TARGETS (with its other variables) makes it " \
               "optional" in problems[0]
    else:  # a value there: passed, nothing said
        monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "1")
        problems, _instances, notes = m.start_problems(a, ["gowitness-tor"])
        assert problems == [] and notes == []
        assert m.run_variables("gowitness-tor", {**OLD_DEFINITION, "catalog": source})[0] == {
            "ALLOW_PRIVATE_TARGETS": "1"}


def test_set_makes_an_old_required_variable_optional(tmp_path):
    """The way to fix such a definition for good: `mcp set NAME --env VAR` takes the catalog's word (env_optional)."""
    sources = catalog(tmp_path, [GOWITNESS])
    write_servers({"gowitness-tor": {**OLD_DEFINITION, "catalog": sources[0][1]}})
    result = m.set_server("gowitness-tor", {"env": ["ALLOW_PRIVATE_TARGETS"]}, catalogs=sources, out=lambda t: None)
    assert result["definition"]["env_optional"] == ["ALLOW_PRIVATE_TARGETS"] and result["definition"]["env"] == []


def test_image_server_gets_its_values_in_the_bridge_environment(tmp_path, monkeypatch):
    write_servers({"gowitness": {"image": IMAGE, "network": "full", "env_values": {"ALLOW_PRIVATE_TARGETS": "1"},
                                 "env_optional": ["OPTIONAL_ONE"]}})
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    monkeypatch.setattr(m, "verified_or_stopped", lambda *a, **kw: None)
    monkeypatch.delenv("OPTIONAL_ONE", raising=False)
    a = s.parse_args(["codex"])
    assert m.ensure_running("gowitness", a) == "started"
    cmd, kwargs = [(c, k) for c, k in started if "--internal-mcp-serve" in c][-1]
    assert "--env=ALLOW_PRIVATE_TARGETS" in cmd and "--env=OPTIONAL_ONE" not in cmd
    assert kwargs["env"]["ALLOW_PRIVATE_TARGETS"] == "1" and "ALLOW_PRIVATE_TARGETS=1" not in " ".join(cmd)


# --- a start's own variables (--mcp-env) -----------------------------------------------------------------------------

def test_start_env(scenario):
    if scenario == "happy":
        a = s.parse_args(["codex", "--mcp", "gowitness", "--mcp-env", "gowitness:ALLOW_PRIVATE_TARGETS=1",
                          "-mv", "gowitness:BOT@MY_BOT"])
        assert m.start_env(a) == {"gowitness": {"ALLOW_PRIVATE_TARGETS": ("value", "1"), "BOT": ("secret", "MY_BOT")}}
    elif scenario == "sad":
        for bad in ("gowitness", "gowitness:ALLOW", "bad name:X=1", "gowitness:GITHUB_TOKEN=x", ":X=1"):
            with pytest.raises(ValueError):
                m.start_env(s.parse_args(["codex", "--mcp-env", bad]))
    else:
        a = s.parse_args(["codex", "--mcp-env", "g:X=1", "--mcp-env", "g:X=2"])
        assert m.start_env(a) == {"g": {"X": ("value", "2")}}  # the later one


def test_instance_for_variables(scenario):
    definition = m.validated_image({"image": IMAGE, "network": "full"}, "t")
    if scenario == "happy":
        instance, changed = m.instance_for("gowitness", definition, None, None, {"ALLOW_PRIVATE_TARGETS": ("value", "1")})
        assert re.fullmatch(r"gowitness\.[0-9a-f]{8}", instance) and changed["env_values"] == {
            "ALLOW_PRIVATE_TARGETS": "1"}
        again, _ = m.instance_for("gowitness", definition, None, None, {"ALLOW_PRIVATE_TARGETS": ("value", "1")})
        other, _ = m.instance_for("gowitness", definition, None, None, {"ALLOW_PRIVATE_TARGETS": ("value", "0")})
        both, _ = m.instance_for("gowitness", definition, {"memory": "1g"}, None,
                                 {"ALLOW_PRIVATE_TARGETS": ("value", "1")})
        assert again == instance and len({instance, other, both}) == 3  # one instance per set of values
    elif scenario == "sad":
        with pytest.raises(ValueError, match="defined with a command"):
            m.instance_for("cmd", {"command": ["x"]}, None, None, {"X": ("value", "1")})
        with pytest.raises(ValueError, match="takes no variables of its own"):
            m.instance_for("team-a", {"image": IMAGE}, None, None, {"X": ("value", "1")})
    else:
        # What the definition says already: the server itself, shared.
        own = {**definition, "env": [], "env_values": {"ALLOW_PRIVATE_TARGETS": "1"}}
        assert m.instance_for("gowitness", own, None, None, {"ALLOW_PRIVATE_TARGETS": ("value", "1")}) == \
            ("gowitness", own)


# --- every problem before anything is made ---------------------------------------------------------------------------

def test_start_problems(scenario, tmp_path, monkeypatch, capsys):
    write_servers({"gowitness": {"image": IMAGE, "network": "full", "env": ["ALLOW_PRIVATE_TARGETS"]},
                   "files": {"image": IMAGE, "mounts": [{"target": "/data", "host": str(tmp_path / "gone"),
                                                         "mode": "ro"}]},
                   "telegram": {"image": IMAGE, "network": "full", "env_secrets": {"TELEGRAM_BOT_TOKEN": "BOT"}}})
    if scenario == "happy":
        monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "1")
        a = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp", "gowitness", "--mcp-env",
                          "gowitness:EXTRA=1"])
        assert m.start_problems(a, ["gowitness"])[0] == []
        s.check_mcp_names(a)
        assert re.fullmatch(r"gowitness\.[0-9a-f]{8}", s.mcp_instance(a, "gowitness"))  # its own: the extra value
    elif scenario == "sad":
        monkeypatch.setattr(m, "image_present", lambda image, run=None: False)
        a = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp", "gowitness", "--mcp", "files", "--mcp",
                          "telegram", "--mcp", "unknown", "--dry-run"])
        with pytest.raises(ValueError) as caught:
            s.check_mcp_names(a)
        text = str(caught.value)
        # All of them at once, each with its fix - a dry run too, before any workspace, session or server.
        assert text.startswith("7 problems with the MCP servers of this start (nothing was started or made")
        for part in ("Unknown MCP server: unknown", "needs the variable ALLOW_PRIVATE_TARGETS",
                     "sandbox-ai mcp set gowitness --env ALLOW_PRIVATE_TARGETS=VALUE",
                     f"the folder {tmp_path / 'gone'} is not there", "sandbox-ai secrets set BOT",
                     f"podman pull {IMAGE}"):
            assert part in text, part
        assert text.count("\n  - ") == 7
    else:
        # A bad --mcp-limits value and a bad --mcp-env value are said together with the rest.
        a = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp", "gowitness", "--mcp-limits",
                          "gowitness:memory=lots", "--mcp-env", "gowitness:GITHUB_TOKEN=x"])
        problems = m.start_problems(a, ["gowitness"])[0]
        assert any("memory must be like" in p for p in problems) and any("name of a secret" in p for p in problems)
        assert any("needs the variable ALLOW_PRIVATE_TARGETS" in p for p in problems)
        assert m.problems_text(["one"]) == "one"


def test_a_missing_agent_image_is_said_with_the_mcp_problems_before_anything_starts(tmp_path, monkeypatch):
    """Regression (the user's Qwen start from the app): its MCP servers were started, then Podman failed with "image
    not known". The agent's image is asked for with the MCP servers' checks - every problem at once, with its build -
    and without MCP servers too."""
    monkeypatch.setattr(s, "start_image_problems", lambda a: [
        f"The image of {a.agent} (localhost/{a.agent}-sandbox:latest) is not built on this host (nothing is pulled at "
        f"a start): sandbox-ai build {a.agent}"])
    write_servers({"gowitness": {"image": IMAGE, "network": "full", "env": ["ALLOW_PRIVATE_TARGETS"]}})
    a = s.parse_args(["qwen", "--workspace", str(tmp_path), "--mcp", "gowitness", "--dry-run"])
    with pytest.raises(ValueError) as caught:
        s.check_mcp_names(a)
    text = str(caught.value)
    assert text.startswith("2 problems with this start (nothing was started or made")
    assert "\n  - The image of qwen (localhost/qwen-sandbox:latest) is not built on this host" in text
    assert "sandbox-ai build qwen" in text and "needs the variable ALLOW_PRIVATE_TARGETS" in text
    a = s.parse_args(["qwen", "--workspace", str(tmp_path)])
    with pytest.raises(ValueError, match="^The image of qwen .*: sandbox-ai build qwen$"):
        s.check_mcp_names(a)


def test_a_starts_images_are_asked_of_podman_at_once(tmp_path, monkeypatch):
    """0.1.95, performance round 1: the agent's image and its MCP servers' images were asked one after another (a
    Podman start each, about 0.1 s): now all at once - each check waits here until all four run, which one after
    another they never would - with the same answers, a missing one said as before."""
    import threading
    together = threading.Barrier(4, timeout=10)
    asked = []

    def present(image, run=None):
        asked.append(image)
        together.wait()
        return not image.startswith("ghcr.io/x/gone")

    def agent_image(a):
        together.wait()
        return []
    monkeypatch.setattr(m, "image_present", present)
    monkeypatch.setattr(s, "start_image_problems", agent_image)
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)  # (the tests' switch: nothing asked at all)
    monkeypatch.setattr(s.shutil, "which", lambda name, *rest, **options: "/usr/bin/" + name)
    gone = f"ghcr.io/x/gone@{DIGEST}"
    write_servers({"one": {"image": IMAGE, "network": "none"}, "two": {"image": f"ghcr.io/x/two@{DIGEST}",
                                                                       "network": "none"},
                   "three": {"image": gone, "network": "none"}})
    a = s.parse_args(["qwen", "--workspace", str(tmp_path), "--mcp", "one", "--mcp", "two", "--mcp", "three",
                      "--dry-run"])
    with pytest.raises(ValueError, match=f"MCP server three: its image {re.escape(gone)} is not there"):
        s.check_mcp_names(a)
    assert sorted(asked) == sorted([IMAGE, f"ghcr.io/x/two@{DIGEST}", gone])  # each once


def test_check_mcp_names_gives_an_instance_of_its_own_where_the_server_runs_otherwise(tmp_path, monkeypatch, capsys):
    """Regression ("MCP server playwright is running with a different definition or other rules; stop it first", said
    after the workspace and session were made): the start gets an instance of its own, said before anything starts."""
    write_servers({"playwright": {"image": IMAGE, "network": "full"}})
    sock = s.mcp_socket_dir("playwright")
    sock.mkdir(parents=True)
    (sock / "server.sock").touch()
    entry = tmp_path / "proc" / "91"
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"python3\0" + os.fsencode(str(sock)) + b"\0")
    m.save_state("playwright", {"pid": 91, "definition": "an older definition"})
    assert m.status("playwright") == "running"
    a = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp", "playwright"])
    s.check_mcp_names(a)
    instance = s.mcp_instance(a, "playwright")
    assert re.fullmatch(r"playwright\.[0-9a-f]{8}", instance) and m.instance_of(a, "playwright") == instance
    assert "this start gets an instance of its own" in capsys.readouterr().err
    # Its socket is the instance's: the agent never reaches the one that runs with the other definition.
    assert s.mcp_socket_dir(instance) != sock
    # The same definition running: shared, as before.
    m.save_state("playwright", {"pid": 91, "definition": m.start_definition("playwright", a)[2]})
    a = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp", "playwright"])
    s.check_mcp_names(a)
    assert s.mcp_instance(a, "playwright") == "playwright"


def test_image_present(scenario, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return type("Done", (), {"returncode": 0 if scenario == "happy" else 1})()
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/podman")
    if scenario == "happy":
        assert IMAGE_PRESENT(IMAGE, run) is True and calls == [["podman", "image", "exists", IMAGE]]
    elif scenario == "sad":
        assert IMAGE_PRESENT(IMAGE, run) is False
    else:
        monkeypatch.setattr(m.shutil, "which", lambda name: None)
        assert IMAGE_PRESENT(IMAGE, run) is None and calls == []  # no Podman: nothing to say
        monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
        assert IMAGE_PRESENT(IMAGE, run) is None and calls == []


def test_mcp_catalog_json_names_the_folders_each_entry_takes(tmp_path, capsys):
    """`mcp catalog --json` says the folders an entry takes and which it requires (the app asks for one before `mcp
    add` instead of failing with "needs --mount /work=HOST")."""
    folders = {**GOWITNESS, "name": "exiftool", "mounts": [
        {"target": "/work", "required": True, "description": "the files to read"}, {"target": "/out"}]}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [folders, GOWITNESS]}))
    assert m.cli(["catalog", "--json", "-c", str(path)]) == 0
    entries = {entry["name"]: entry for entry in json.loads(capsys.readouterr().out)["entries"]}
    assert entries["exiftool"]["mounts"] == [  # (0.1.212: each with its default and the mode its entry declares)
        {"target": "/work", "required": True, "description": "the files to read", "default": "workspace", "mode": "ro"},
        {"target": "/out", "required": False, "description": "", "default": "own", "mode": "ro"}]
    assert entries["gowitness"]["mounts"] == []


# --- a variable that changes what code runs (LD_PRELOAD, NODE_OPTIONS, PYTHONSTARTUP, ...) ---------------------------
# The human's own --mcp-env is set, with a warning that names the risk (stderr and launcher.log); a preset passed on
# (built-in, an extra directory's: someone else's text) may not set one - "give it yourself if you mean it".

@pytest.mark.parametrize("name", ["LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "NODE_OPTIONS", "PYTHONSTARTUP",
                                  "PYTHONPATH", "PERL5OPT", "RUBYOPT", "BASH_ENV", "JAVA_TOOL_OPTIONS", "PATH",
                                  "DYLD_INSERT_LIBRARIES", "LD_BIND_NOW"])
def test_a_variable_that_changes_what_code_runs_is_known(name):
    assert m.code_variable(name)


@pytest.mark.parametrize("name", ["ALLOW_PRIVATE_TARGETS", "TELEGRAM_ALLOWED_CHATS", "LOG_LEVEL", "NODE_ENV",
                                  "PYTHONUNBUFFERED", "LANG", "OLD_PRELOAD"])
def test_an_ordinary_variable_is_not(name):
    assert not m.code_variable(name)


def test_code_env_in_options():
    options = ["--mcp", "g", "--mcp-env", "g:NODE_OPTIONS=--require /work/x.js", "--mcp-env=g:LD_PRELOAD=/w/x.so",
               "--mcp-env", "g:ALLOW_PRIVATE_TARGETS=1", "--mcp-env", "g:PYTHONPATH@SECRET_ONE", "--mcp-env", "bad"]
    assert m.code_env_in(options) == ["--mcp-env g:NODE_OPTIONS", "--mcp-env g:LD_PRELOAD", "--mcp-env g:PYTHONPATH"]
    # A whole value the human gives (--preset-value) is theirs.
    assert m.code_env_in(options, ["g:LD_PRELOAD=/w/x.so"]) == ["--mcp-env g:NODE_OPTIONS", "--mcp-env g:PYTHONPATH"]
    assert m.code_env_in(["--mcp-env"]) == [] and m.code_env_in([]) == []


def test_code_env_warnings():
    a = s.parse_args(["codex", "--mcp", "g", "--mcp-env", "g:LD_PRELOAD=/work/x.so", "--mcp-env", "g:X=1"])
    said = m.code_env_warnings(a)
    assert len(said) == 1 and "--mcp-env g:LD_PRELOAD" in said[0] and "changes what code runs" in said[0]
    assert "MCP server g" in said[0] and "--mcp-mount" in said[0]
    assert m.code_env_warnings(s.parse_args(["codex", "--mcp-env", "g:X=1"])) == []
    assert m.code_env_warnings(s.parse_args(["codex", "--mcp-env", "bad"])) == []  # (start_problems says it)


@pytest.fixture
def mcp_host(tmp_path):
    from live_fakes import Host
    host = Host(tmp_path)
    host.env.pop("SANDBOX_AI_RUN", None)
    added = host.run("mcp", "add", "mcp:duckduckgo", "--network", "full", "--no-pull")
    assert added.returncode == 0, added.stderr
    image = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["duckduckgo"]["image"]
    host.add_image(image, "localhost/claude-sandbox:latest")
    return host


def test_mcp_env_ld_preload_of_yours_is_set_with_a_warning(mcp_host):
    """End to end: `--mcp-env duckduckgo:LD_PRELOAD=...` was taken without a word."""
    done = mcp_host.run("claude", "--mcp", "duckduckgo", "--mcp-env", "duckduckgo:LD_PRELOAD=/work/x.so", "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "warning: --mcp-env duckduckgo:LD_PRELOAD" in done.stderr and "changes what code runs" in done.stderr
    log = (mcp_host.home / ".local/state/sandbox-ai/launcher.log").read_text()
    assert "--mcp-env duckduckgo:LD_PRELOAD" in log
    # A session's start: its supervisor says it, relayed whole to the terminal - once.
    session = mcp_host.run("claude", "--session", "s1", "--mcp", "duckduckgo", "--mcp-env",
                           "duckduckgo:LD_PRELOAD=/work/x.so")
    assert session.stderr.count("changes what code runs") == 1, session.stderr
    assert "set as you chose it" in session.stderr
    plain = mcp_host.run("claude", "--mcp", "duckduckgo", "--mcp-env", "duckduckgo:SAFESEARCH=off", "--dry-run")
    assert plain.returncode == 0 and "changes what code runs" not in plain.stderr, plain.stderr


def test_mcp_env_node_options_in_a_preset_passed_on_is_refused(mcp_host, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    document = {"format": "sandbox-ai preset", "version": 1, "id": "theirs", "title": "t", "description": "d",
                "options": ["--mcp", "duckduckgo", "--mcp-env", "duckduckgo:NODE_OPTIONS=--require=/work/x.js"]}
    (shared / "theirs.json").write_text(json.dumps(document))
    assert mcp_host.run("presets", "dirs", "add", shared).returncode == 0
    refused = mcp_host.run("claude", "--preset", "theirs", "--dry-run")
    assert refused.returncode != 0
    assert "--mcp-env duckduckgo:NODE_OPTIONS is not allowed in a preset that is not yours" in refused.stderr, \
        refused.stderr
    own = mcp_host.home / ".config/sandbox-ai/presets"
    own.mkdir(parents=True, exist_ok=True)
    (own / "mine.json").write_text(json.dumps({**document, "id": "mine"}))
    mine = mcp_host.run("claude", "--preset", "mine", "--dry-run")
    assert mine.returncode == 0 and "warning: --mcp-env duckduckgo:NODE_OPTIONS" in mine.stderr, mine.stderr
