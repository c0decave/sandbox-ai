"""The network proposals the user approved on 2026-11-24 (docs/design/network-allowances.md, section 5), as far as the
command line goes:
- A: --allow-github, --allow-pypi ... are "host sets" in every help text; "preset" means only a start template.
- F: --allow-net IP_OR_CIDR is the documented name of --allow (every port and protocol of an address or network);
  --allow stays an alias everywhere (the start, net allow|revoke, project files, presets, saved option lists, board
  export/import, completion).
- G: an MCP server's network in the agents' words (offline, egress-all, allowlist, lan); the old values (none, full)
  stay accepted and are shown in the new words."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import test_feature_matrix as fm
import test_net as tn

s = fm.s
net = tn.net
ROOT = Path(__file__).resolve().parents[1]
SAI = str(ROOT / "sandbox-ai")


def run(*argv):
    return subprocess.run([SAI, *argv], capture_output=True, text=True, timeout=120)


# --- F: --allow-net, --allow its alias ----------------------------------------------------------------------------

def test_allow_net_is_the_name_and_allow_the_alias_at_a_start():
    for spelling in ("--allow-net", "--allow", "-a"):
        a = s.parse_args(["claude", "--no-config", spelling, "192.0.2.0/24, 198.51.100.7"])
        assert a.allow == ["192.0.2.0/24", "198.51.100.7"], spelling
    action = s.option_actions()["--allow-net"]
    assert action is s.option_actions()["--allow"] and action.option_strings == ["-a", "--allow-net", "--allow"]
    assert "every port and protocol" in action.help.lower() and "--allow is the same" in action.help


def test_saved_option_lists_read_allow_net_as_allow():
    """Project files, presets, kept starts, MCP definitions, board bundles: everything that looks options up by name
    reads canonical_options - --allow-net is --allow there, so no reader misses it."""
    assert s.canonical_options(["--allow-net", "192.0.2.0/24"]) == ["--allow", "192.0.2.0/24"]
    assert s.canonical_options(["--allow-net=192.0.2.0/24,192.0.2.9"]) == ["--allow=192.0.2.0/24", "--allow=192.0.2.9"]
    assert s.canonical_options(["-a", "192.0.2.1"]) == ["--allow", "192.0.2.1"], "the short form: the old name"
    assert s.given_by(["--allow-net", "192.0.2.0/24"])["nets"] == {"192.0.2.0/24"}


def test_the_help_names_allow_net_first():
    out = run("claude", "--help").stdout
    line = next(line for line in out.splitlines() if "--allow-net" in line)
    assert line.strip().startswith("-a, --allow-net, --allow IP_OR_CIDR"), line
    allow = run("net", "allow", "--help").stdout
    assert "--allow-net, --allow IP_OR_CIDR" in allow


def test_net_allow_and_revoke_take_allow_net():
    for command in ("allow", "revoke"):
        args = net.parser().parse_args([command, "run-1", "--allow-net", "192.0.2.0/24"])
        assert args.allow == ["192.0.2.0/24"], command


def test_completion_offers_allow_net_and_allow():
    import test_completion as tc
    for words in (["claude", "--allow"], ["net", "allow", "run-1", "--allow"]):
        found = set(tc.c.complete(words))
        assert {"--allow-net", "--allow"} <= found, words


# --- A: host sets -------------------------------------------------------------------------------------------------

def test_the_host_sets_are_called_host_sets_in_the_help():
    actions = s.option_actions()
    for name, hosts in s.PRESETS.items():
        assert actions[f"--allow-{name}"].help.startswith("Host set: "), name
        assert hosts[0] in actions[f"--allow-{name}"].help
    start = run("claude", "--help").stdout
    allow = run("net", "allow", "--help").stdout
    for text in (start, allow):
        assert "--allow-PRESET" not in text and "host set" in text.lower()
        assert "preset" not in " ".join(line for line in text.splitlines() if "--allow-github" in line).lower()
    assert "--allow-SET" in run("net", "--help").stdout


# --- G: an MCP server's network in the agents' words ------------------------------------------------------------

import test_mcp_egress as me  # noqa: E402
from test_mcp_egress import home  # noqa: E402,F401  (fixture)

m = me.m


def test_the_network_words_and_their_aliases():
    assert m.NETWORK_WORDS == {"none": "offline", "full": "egress-all", "allowlist": "allowlist", "lan": "lan"}
    for word, stored in (("offline", "none"), ("egress-all", "full"), ("allowlist", "allowlist"), ("lan", "lan"),
                         ("none", "none"), ("full", "full")):
        assert m.network_value(word) == stored, word
    # Stored as before (older sandbox-ai and the catalogs read them), shown in the new words.
    assert m.network_label({"image": me.IMAGE, "network": "full"}) == "egress-all (the internet, not your LAN)"
    assert m.network_label({"image": me.IMAGE}) == "offline"
    assert m.network_label({"image": me.IMAGE, "network": "lan"}) == "lan (the internet and your LAN)"
    assert m.network_label({"image": me.IMAGE, "network": "allowlist", "hosts": ["a.example.com:443"]}) == \
        "allowlist (only a.example.com:443)"


def test_mcp_add_and_set_take_the_new_words_and_the_old_ones(home):
    path = home / "catalog.json"
    egress_entry = {**me.FULL, "network": "egress-all"}  # a catalog in the new words is read too
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [me.ALLOWLIST, egress_entry]}))
    assert m.definition_from_entry(egress_entry)["network"] == "full"
    assert m.definition_from_entry(me.ALLOWLIST, network="egress-all")["network"] == "full"
    assert m.definition_from_entry(me.ALLOWLIST, network="offline")["network"] == "none"
    m.write_definition("duckduckgo", {**m.definition_from_entry(me.ALLOWLIST), "catalog": str(path)})
    _, new = m.set_definition("duckduckgo", {"network": "egress-all"}, entry=me.ALLOWLIST)
    assert new["network"] == "full" and "hosts" not in new
    _, new = m.set_definition("duckduckgo", {"network": "offline"}, entry=me.ALLOWLIST)
    assert new["network"] == "none"
    with pytest.raises(ValueError, match="offline, egress-all, allowlist or lan"):
        m.set_definition("duckduckgo", {"network": "internet"}, entry=me.ALLOWLIST)
    # A definition written by hand in the new words.
    assert m.validated({"x": {"image": me.IMAGE, "network": "egress-all"}}, "test")["x"]["network"] == "full"


def test_the_command_line_offers_the_new_words_and_takes_the_old():
    parser = m.parser()
    for command in (["add", "web"], ["set", "web"]):
        for word, stored in (("offline", "none"), ("egress-all", "full"), ("none", "none"), ("full", "full")):
            args = parser.parse_args([*command, "--network", word])
            assert m.network_value(args.network) == stored, (command, word)
    out = run("mcp", "add", "--help").stdout + run("mcp", "set", "--help").stdout
    assert "offline" in out and "egress-all" in out
    assert "{none,full" not in out and "full (the internet" not in out
    import test_completion as tc
    found = set(tc.c.complete(["mcp", "add", "web", "--network", ""]))
    assert {"offline", "egress-all", "allowlist", "lan"} <= found and "full" not in found
