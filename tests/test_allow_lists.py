"""Several items in one value of an allow-type option: --allow-host "pypi.org, files.pythonhosted.org *.example.com",
--allow-tcp 10.0.0.5:22,10.0.0.6:22, --allow "10.0.0.0/8 192.168.1.0/24", --allow-host-port 8080,11434 - commas
and/or whitespace between them, empty items (",,") ignored, duplicates dropped, each item checked exactly as one
given alone (one bad item refuses the whole option, naming the item). The launcher's LIST_OPTIONS, at a start (the
parser, canonical_options: project files, presets, MCP definitions, kept starts), in `sandbox-ai net allow|revoke`,
and through the real ./sandbox-ai entry point (live_fakes' Podman; net against a real control socket). Options whose
values hold a comma or a space by meaning (--api-key HOST=NAME=SECRET, --allow-http rules, paths) are not split.
One value per option everywhere: a second word after it is never taken as another item."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)
import test_net as tn
from test_net import no_login_runtime, sandbox  # noqa: F401  (fixtures)
from live_fakes import Host

s = fm.s
net = tn.net
ROOT = Path(__file__).resolve().parents[1]


def parsed(*options):
    return s.parse_args(["claude", "--no-config", *options])


# --- the items of one value -------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, items", [
    ("pypi.org, files.pythonhosted.org *.example.com", ["pypi.org", "files.pythonhosted.org", "*.example.com"]),
    ("a,b", ["a", "b"]), ("a b", ["a", "b"]), (" a ,, b ,", ["a", "b"]), ("\ta\n b", ["a", "b"]),
    ("a a,a", ["a"]), ("a", ["a"]), ("", []), (",, ,", []),
    ("[2001:db8::1]:443, 10.0.0.5:22", ["[2001:db8::1]:443", "10.0.0.5:22"]),
])
def test_list_items(value, items):
    assert s.list_items(value) == items


def test_the_list_options():
    assert set(s.LIST_OPTIONS) == {"--allow", "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow-tcp",
                                   "--allow-host-port",
                                   "--no-inspect", "--nameserver", "--vpn-bypass-tcp"}
    actions = s.option_actions()
    for option in s.LIST_OPTIONS:
        assert isinstance(actions[option], s.ListOption), option
        assert "one or more, separated by commas or spaces" in actions[option].help, option
    # Values with a comma or a space by meaning stay one value each.
    for option in ("--api-key", "--allow-http", "--mount", "--copy", "--rules", "--env", "--git-token-env",
                   "--secret-env", "--preset-value", "--service", "--profile"):
        assert not isinstance(actions[option], s.ListOption), option


# --- a start ------------------------------------------------------------------------------------------------------

def test_every_list_option_takes_several_items_in_every_spelling():
    a = parsed("--allow-host", "PyPI.org, files.pythonhosted.org example.com", "-A", "pypi.org",
               "--allow-domain=example.net,example.org", "--allow-tcp", "10.0.0.5:22,[2001:db8::1]:443",
               "-a", "10.0.0.0/8 192.168.1.0/24", "--allow-host-port", "8080,11434", "-P8080",
               "--no-inspect", "a.example.org, *.b.example.org", "--nameserver", "192.0.2.53 192.0.2.54",
               "--vpn-bypass-tcp", "192.168.1.5:11434,,192.168.1.6:11434")
    assert a.allow_host[:3] == ["pypi.org", "files.pythonhosted.org", "example.com"]  # lower case, each once
    assert a.allow_domain == ["example.net", "example.org"]
    assert a.allow_tcp == ["10.0.0.5:22", "[2001:db8::1]:443"]
    assert s.tcp_targets(a) == [("10.0.0.5", 22), ("2001:db8::1", 443)]
    assert a.allow == ["10.0.0.0/8", "192.168.1.0/24"]
    assert s.host_ports(a) == [8080, 11434] and a.allow_host_port == ["8080", "11434"]
    assert a.no_inspect == ["a.example.org", "*.b.example.org"]
    assert a.nameserver == ["192.0.2.53", "192.0.2.54"]
    assert a.vpn_bypass_tcp == ["192.168.1.5:11434", "192.168.1.6:11434"]
    a.allow_host = [host for host in a.allow_host if not host.startswith("*.")]  # (wildcards: not with --nameserver)
    a.vpn_bypass_tcp = []  # (it goes with a Tor profile)
    s.validate(a)


def test_one_list_is_the_options_one_by_one():
    one = parsed("--allow-host", "a.example.org,b.example.org", "--allow-tcp", "192.0.2.9:22 192.0.2.10:22")
    many = parsed("--allow-host", "a.example.org", "--allow-host", "b.example.org", "--allow-tcp", "192.0.2.9:22",
                  "--allow-tcp", "192.0.2.10:22")
    assert vars(one) == vars(many)
    assert s.allowed_hosts(one) == s.allowed_hosts(many)


@pytest.mark.parametrize("options, match", [
    (["--allow-host", "pypi.org, *.co.uk, example.org"], r"^--allow-host \*\.co\.uk: "),
    (["--allow-domain", "example.org co.uk"], r"^--allow-domain co\.uk: refused"),
    (["--allow-host", "pypi.org,a..example.org"], r"Invalid hostname: 'a\.\.example\.org'"),
    (["--allow-tcp", "10.0.0.5:22,nonsense"], r"--allow-tcp expects IP:PORT or \[IPv6\]:PORT: 'nonsense'"),
    (["--allow-tcp", "10.0.0.5:22 [2001:db8::1]"], r"--allow-tcp expects .*'\[2001:db8::1\]'"),
    (["--allow", "10.0.0.0/8,example.org"], r"--allow takes an address .*'example\.org'"),
    (["--allow", "10.0.0.0/8 fe80::1%eth0"], r"--allow does not accept an IPv6 scope ID: 'fe80::1%eth0'"),
    (["--allow-host-port", "8080,99999"], r"--allow-host-port expects a TCP port 1-65535: '99999'"),
    (["--nameserver", "192.0.2.53,example.org"], r"--nameserver expects a single IP address: 'example\.org'"),
    (["--no-inspect", "a.example.org,github.com"], r"--no-inspect github\.com would let pushes"),
])
def test_one_bad_item_refuses_the_option_naming_it(options, match):
    with pytest.raises(ValueError, match=match):
        s.validate(parsed(*options))


@pytest.mark.parametrize("value", ["", ",,", " , "])
def test_a_value_without_any_item_is_refused(value, capsys):
    with pytest.raises(SystemExit):
        parsed("--allow-host", value)
    assert "--allow-host: no value (one or more, separated by commas or spaces)" in capsys.readouterr().err


def test_values_with_a_comma_or_space_by_meaning_stay_whole():
    a = parsed("--allow-http", "api.github.com GET,HEAD /repos/**", "--api-key", "api.example.com=EX_KEY",
               "--env", "PATH_X", "--preset-value", "hosts=a.example.org,b.example.org")
    assert a.allow_http == ["api.github.com GET,HEAD /repos/**"]
    assert a.api_key == ["api.example.com=EX_KEY"]
    assert a.preset_value == ["hosts=a.example.org,b.example.org"]


def test_a_second_word_is_never_another_item(capsys):
    """One value per option: `--allow-host a b` takes a; b is no item of it (the launcher refuses it as an
    argument of its own, as before). Quoted, "a b" is the list."""
    with pytest.raises(SystemExit):
        parsed("--allow-host", "a.example.org", "b.example.org")
    assert "unrecognized arguments: b.example.org" in capsys.readouterr().err
    assert parsed("--allow-host", "a.example.org b.example.org").allow_host == ["a.example.org", "b.example.org"]
    # Agent arguments after -- are the agent's, never split nor taken.
    assert s.canonical_options(["claude", "-A", "a,b", "--", "--allow-host", "x,y"]) == \
        ["claude", "--allow-host", "a", "--allow-host", "b", "--", "--allow-host", "x,y"]


def test_canonical_options_spread_the_items():
    """What reads options by name (project files, presets, MCP definitions, kept starts, the modes, the app) sees one
    item per option."""
    assert s.canonical_options(["claude", "-A", "a.example, b.example", "--allow-tcp=10.0.0.5:22,10.0.0.6:22",
                                "-Pa8080", "-P8080,11434", "-x", "*.example.org", "--name", "--allow-host",
                                "--api-key", "h.example=K", "--allow-http", "h.example GET,HEAD /x"]) == [
        "claude", "--allow-host", "a.example", "--allow-host", "b.example", "--allow-tcp=10.0.0.5:22",
        "--allow-tcp=10.0.0.6:22", "--allow-host-port", "a8080", "--allow-host-port", "8080", "--allow-host-port", "11434",
        "--no-inspect", "*.example.org", "--name", "--allow-host", "--api-key", "h.example=K", "--allow-http",
        "h.example GET,HEAD /x"]
    # A value without any item stays as it was given: the parser refuses it.
    assert s.canonical_options(["claude", "--allow-host", ",,"]) == ["claude", "--allow-host", ",,"]
    # Duplicates within one value go.
    assert s.canonical_options(["claude", "-A", "a,a"]) == ["claude", "--allow-host", "a"]


def test_the_plan_project_file_and_preset_take_lists(m):
    plan = m.plan("--allow-host", "pypi.org, files.pythonhosted.org *.example.com", "--allow-tcp",
                  "10.0.0.5:22,[2001:db8::1]:443")
    assert {"pypi.org", "files.pythonhosted.org"} <= set(plan["https_hosts"])
    assert plan["host_wildcards"] == ["*.example.com"]
    m.refused("--allow-host", "pypi.org,*.co.uk", match=r"--allow-host \*\.co\.uk")
    m.project_file({"options": ["--allow-host", "a.example.org b.example.org", "--allow=10.0.0.0/8,192.168.1.0/24"]})
    plan = m.plan()
    assert {"a.example.org", "b.example.org"} <= set(plan["https_hosts"])
    m.project_file({"options": ["-ad", "example.org,co.uk"]})
    m.refused(match=r"--allow-domain co\.uk: refused")
    m.project_file({"options": []})
    presets = Path.home() / ".config" / "sandbox-ai" / "presets"
    presets.mkdir(parents=True)
    (presets / "hosts.json").write_text(json.dumps({
        "format": "sandbox-ai preset", "version": 1, "id": "hosts", "title": "Hosts", "description": "hosts",
        "options": ["--allow-host", "{hosts}"], "values": {"hosts": {"label": "Hosts", "kind": "text"}}}))
    plan = m.plan("--preset", "hosts", "--preset-value", "hosts=c.example.org, d.example.org")
    assert {"c.example.org", "d.example.org"} <= set(plan["https_hosts"])


@pytest.mark.parametrize("mode", ["high"])
def test_the_strict_modes_name_the_option_of_a_list(m, mode):
    m.refused("--mode", mode, "--allow-host", "a.example.org,b.example.org",
              match=rf"^--allow-host a\.example\.org --allow-host b\.example\.org widens the security mode {mode}")


def test_the_modes_and_the_trust_question_see_each_item():
    assert s.MODES.weakening_options(s, ["--allow-host", "a.example,b.example"]) == [
        ("--allow-host", "a.example"), ("--allow-host", "b.example")]


def test_a_kept_starts_list_option_is_one_of_lists():
    assert s.load_sessions_module().is_list(s.option_actions()["--allow-host"])


# --- sandbox-ai net allow / revoke --------------------------------------------------------------------------------

def test_net_parser_takes_lists():
    args = net.parser().parse_args(["allow", "r", "-A", "A.example, b.example", "-t", "192.0.2.9:22,[2001:db8::5]:443",
                                    "-P", "8080 11434", "-a", "10.0.0.0/8,192.168.1.0/24", "-ns", "192.0.2.53,,192.0.2.54",
                                    "-ad", "example.org example.net", "--allow-host=b.example"])
    assert args.allow_host == ["a.example", "b.example"]
    assert args.allow_tcp == ["192.0.2.9:22", "[2001:db8::5]:443"]
    assert args.allow_host_port == [8080, 11434]
    assert args.allow == ["10.0.0.0/8", "192.168.1.0/24"]
    assert args.nameserver == ["192.0.2.53", "192.0.2.54"]
    assert args.allow_domain == ["example.org", "example.net"]
    args = net.parser().parse_args(["revoke", "r", "-A", "a.example b.example"])
    assert args.allow_host == ["a.example", "b.example"]


def test_net_parser_refuses_a_bad_port_naming_it(capsys):
    with pytest.raises(SystemExit):
        net.parser().parse_args(["allow", "r", "-P", "8080,abc"])
    assert "'abc'" in capsys.readouterr().err


def test_net_a_second_word_is_the_run_not_an_item():
    args = net.parser().parse_args(["allow", "-A", "a.example", "codex-1234abcd"])
    assert args.allow_host == ["a.example"] and args.run == "codex-1234abcd"


def test_net_allow_and_revoke_lists(sandbox, tmp_path, monkeypatch):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tn.project_file(tmp_path)
    status, lines = tn.run_net(["allow", "--allow-host", "a.example, b.example *.wild.example", "--allow-tcp",
                                "192.0.2.9:22,[2001:db8::5]:443", "--allow", "10.9.0.0/16 10.8.0.0/16", "--keep"])
    assert status == 0, lines
    hosts = sandbox["hosts"].read_text()
    assert "192.0.2.50 a.example" in hosts and "192.0.2.50 b.example" in hosts
    allowed = json.loads(sandbox["allow"].read_text())
    assert {"10.9.0.0/16", "10.8.0.0/16"} <= set(allowed["nets"])
    assert json.loads(path.read_text())["options"] == [
        "--allow-host", "a.example", "--allow-host", "b.example", "--allow-host", "*.wild.example",
        "--allow-tcp", "192.0.2.9:22", "--allow-tcp", "[2001:db8::5]:443", "--allow", "10.9.0.0/16",
        "--allow", "10.8.0.0/16"]
    status, lines = tn.run_net(["revoke", "-A", "a.example,b.example"])
    assert status == 0, lines
    assert "a.example" not in sandbox["hosts"].read_text() and "b.example" not in sandbox["hosts"].read_text()


def test_net_allow_refuses_the_whole_list_for_one_bad_item(sandbox, capsys):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    assert tn.run_net(["allow", "--allow-host", "a.example,*.co.uk,b.example"])[0] == 1
    assert "--allow-host *.co.uk" in capsys.readouterr().err
    assert tn.run_net(["allow", "--allow-domain", "example.org co.uk"])[0] == 1
    assert "--allow-domain co.uk: refused" in capsys.readouterr().err
    assert sandbox["applied"] == [] and "a.example" not in sandbox["hosts"].read_text()


# --- end to end: the real entry point -------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/claude-sandbox:latest")
    return made


def test_e2e_a_start_takes_lists(host):
    result = host.run("claude", "--dry-run", "--no-tmux", "--allow-host", "pypi.org, files.pythonhosted.org *.example.com",
                      "--allow-tcp", "10.0.0.5:22,[2001:db8::1]:443", "--allow", "10.0.0.0/8 192.168.1.0/24",
                      "--allow-host-port", "8080,11434")
    assert result.returncode == 0, result.stderr[-3000:]
    plan = json.loads(result.stdout)
    assert {"pypi.org", "files.pythonhosted.org"} <= set(plan["https_hosts"])
    assert plan["host_wildcards"] == ["*.example.com"]
    assert plan["host_ports"] == [8080, 11434]
    text = json.dumps(plan)
    for value in ("10.0.0.0/8", "192.168.1.0/24", "2001:db8::1", "10.0.0.5"):
        assert value in text, value


def test_e2e_a_start_refuses_a_list_with_one_bad_item(host):
    result = host.run("claude", "--dry-run", "--no-tmux", "--allow-host", "pypi.org,*.co.uk,example.org")
    assert result.returncode != 0
    assert "--allow-host *.co.uk" in result.stderr
    result = host.run("claude", "--dry-run", "--no-tmux", "--allow-host", "a.example.org", "b.example.org")
    assert result.returncode != 0 and "b.example.org" in result.stderr


def test_e2e_net_allow_takes_lists(sandbox, tmp_path):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([str(ROOT / "sandbox-ai"), "net", "allow", "codex-1234abcd", "--allow-tcp",
                           "192.0.2.9:22, [2001:db8::5]:443", "--allow", "10.9.0.0/16,10.8.0.0/16",
                           "--allow-host", "*.wild.example"], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    allowed = json.loads(sandbox["allow"].read_text())
    assert {"10.9.0.0/16", "10.8.0.0/16"} <= set(allowed["nets"])
    assert {"192.0.2.9", "2001:db8::5"} <= {target[0] for target in allowed["tcp"]}
    refused = subprocess.run([str(ROOT / "sandbox-ai"), "net", "allow", "codex-1234abcd", "--allow-host",
                              "*.ok.example,*.co.uk"], capture_output=True, text=True, env=env, timeout=120)
    assert refused.returncode == 1 and "--allow-host *.co.uk" in refused.stderr, refused.stderr


# --- help and completion ------------------------------------------------------------------------------------------

def test_help_and_completion_say_how_several_are_given():
    text = " ".join(s.build_parser().format_help().split())
    assert text.count("one or more, separated by commas or spaces") >= len(s.LIST_OPTIONS)
    for name in ("allow", "revoke"):
        command = net.parser()._subparsers._group_actions[0].choices[name]
        for option in ("--allow-host", "--allow-domain", "--allow-tcp", "--allow", "--allow-host-port", "--nameserver"):
            action = command._option_string_actions[option]
            assert isinstance(action, net.launcher().ListOption), (name, option)
            assert "one or more, separated by commas or spaces" in action.help, (name, option)
    import importlib.util
    spec = importlib.util.spec_from_file_location("completion_for_lists", ROOT / "sandbox" / "completion.py")
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    for option in s.LIST_OPTIONS:
        assert "one or more, separated by commas or spaces" in c.FREE_TEXT[option], option
    # A list in one (quoted) word: the next word is completed as before.
    assert "--allow-domain" in c.complete(["claude", "--allow-host", "a.example,b.example", "--allow-d"])
    assert "--allow-domain" in c.complete(["net", "allow", "r", "-A", "a.example b.example", "--allow-d"])


def test_a_rebirth_adds_each_item_once(tmp_path):
    """sandbox-ai resume NAME --allow-host "a, b": each item is a list option's value of its own; one saved already
    is kept, the others are added."""
    sessions = s.load_sessions_module()
    data = {"name": "web", "agent": "claude", "cwd": str(tmp_path), "options": ["--no-config", "--allow-host", "a.example"]}
    options, changes = sessions.changed_options(data, given=["-A", "a.example, b.example", "--allow-tcp",
                                                             "192.0.2.9:22,192.0.2.10:22"])
    assert options == ["--no-config", "--allow-host", "a.example", "--allow-host", "b.example", "--allow-tcp",
                       "192.0.2.9:22", "--allow-tcp", "192.0.2.10:22"]
    assert "kept: --allow-host a.example (saved already)" in changes and "added: --allow-host b.example" in changes
