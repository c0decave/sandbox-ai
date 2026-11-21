"""Final review 2 (correctness and user journeys, docs/reviews/final-review-2-journeys.md): each journey that went
wrong, as the user takes it - through ./sandbox-ai (tests/live_fakes.py's user) where a command shows it, the function
behind it otherwise."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/c0decave/sandbox-ai-mcp-web-fetch@sha256:" + "a" * 64


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("sandbox_mcp_final_review_2", "mcp.py")


# --- An MCP server added in 0.1.97 to reach your network (ALLOW_PRIVATE_TARGETS=1, as its README said) -------------
# 0.1.98's "full" is the internet without your local network: such a server no longer reaches the targets it was
# added for, and nothing said so. mcp list, mcp add|set and doctor say it now, with the fix.

def test_a_server_that_asks_for_private_targets_without_reaching_them_is_told_how():
    old = {"image": IMAGE, "network": "full", "env_values": {"ALLOW_PRIVATE_TARGETS": "1"}}
    note = m.egress_note("gowitness", old)
    assert note and "ALLOW_PRIVATE_TARGETS" in note and "not your local network" in note, note
    assert "sandbox-ai mcp set gowitness --allow-private" in note and "--network lan" in note, note
    # opened (a part, or all of it): what is said is what it reaches, as before
    opened = m.egress_note("gowitness", {**old, "allow_private": ["192.168.1.0/24"]})
    assert "reaches 192.168.1.0/24" in opened
    assert m.egress_note("gowitness", {**old, "network": "lan"}).startswith("MCP server gowitness reaches your local")
    # without network, or without the flag (or with it off): nothing to say
    assert m.egress_note("gowitness", {**old, "network": "none"}) is None
    assert m.egress_note("gowitness", {**old, "env_values": {"ALLOW_PRIVATE_TARGETS": "0"}}) is None
    assert m.egress_note("web", {"image": IMAGE, "network": "full"}) is None


@pytest.fixture()
def host(tmp_path):
    found = Host(tmp_path / "h")
    config = found.home / ".config/sandbox-ai"
    config.mkdir(parents=True)
    # (mcp.json as sandbox-ai 0.1.97 wrote it for `mcp add gowitness --network full --env ALLOW_PRIVATE_TARGETS=1`)
    (config / "mcp.json").write_text(json.dumps({
        "gowitness": {"image": IMAGE, "network": "full", "env_values": {"ALLOW_PRIVATE_TARGETS": "1"},
                      "memory": "512m", "pids": 128, "source": "mcp:gowitness"}}))
    return found


def test_e2e_mcp_list_says_an_upgraded_lan_server_no_longer_reaches_your_network(host):
    done = host.run("mcp", "list", timeout=120)
    assert done.returncode == 0, done.stderr
    said = done.stdout + done.stderr
    assert "ALLOW_PRIVATE_TARGETS" in said and "sandbox-ai mcp set gowitness --allow-private" in said, said


# --- mcp add|set --allow-private / --allow-tcp take several in one value, as every other allow-type option ------------

def test_e2e_mcp_set_takes_several_networks_and_addresses_in_one_value(host):
    done = host.run("mcp", "set", "gowitness", "--allow-private", "10.0.0.0/8, 172.16.0.0/12",
                    "--allow-tcp", "192.168.1.2:80 192.168.1.3:80,[fd00::7]:8080", timeout=120)
    assert done.returncode == 0, done.stderr
    definition = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["gowitness"]
    assert definition["allow_private"] == ["10.0.0.0/8", "172.16.0.0/12"]
    assert definition["allow_tcp"] == ["192.168.1.2:80", "192.168.1.3:80", "[fd00::7]:8080"]
    bad = host.run("mcp", "set", "gowitness", "--allow-tcp", "192.168.1.2:80, nonsense", timeout=120)
    assert bad.returncode != 0 and "nonsense" in bad.stderr, bad.stderr
    empty = host.run("mcp", "set", "gowitness", "--allow-private", " , ", timeout=120)
    assert empty.returncode != 0


def test_e2e_mcp_add_and_set_help_say_the_lists(host):
    for sub in ("add", "set"):
        done = host.run("mcp", sub, "--help", timeout=120)
        text = " ".join(done.stdout.split())
        assert done.returncode == 0 and "separated by commas or spaces" in text, (sub, text)


# --- team start --help: the agents, readable (they were one word, broken across lines) -----------------------------

def test_e2e_team_start_help_lists_the_agents_whole(host):
    done = host.run("team", "start", "--help", timeout=120)
    words = done.stdout.replace(",", " ").split()
    assert done.returncode == 0 and "openhands" in words and "opencode2" in words, done.stdout


# --- mcp copy says the network in words ("copied: network the start's rules" read as nonsense) ---------------------

def test_mcp_copy_says_the_network_in_words():
    mc = load("sandbox_mcp_copy_final_review_2", "mcp_copy.py")
    assert mc.network_text({"network": "full", "vpn": None, "options": []}) == "network: internet (no LAN)"
    assert mc.network_text({"network": "the start's rules", "vpn": None, "options": ["--network=none"]}) == \
        "network: its command's own options (the sandbox's rules), --network=none"
    assert mc.network_text({"network": "allowlist", "vpn": "mullvad", "options": ["--allow-tcp 10.0.0.7:80"]}) == \
        "network: only its catalog entry's hosts, VPN mullvad, --allow-tcp 10.0.0.7:80"


# --- defaults show: the values in one column (the keys grew past its width: "desktop_notifications -") ------------

def test_e2e_defaults_show_aligns_every_value(host):
    done = host.run("defaults", "show", timeout=120)
    assert done.returncode == 0, done.stderr
    rows = [line for line in done.stdout.splitlines() if line.startswith("  ")]
    starts = {len(line) - len(line[2:].split(None, 1)[1]) for line in rows}  # where each value begins
    assert len(rows) > 5 and len(starts) == 1, done.stdout


def test_a_copy_that_is_refused_never_says_imported_above_it():
    mc = load("sandbox_mcp_copy_final_review_2b", "mcp_copy.py")
    plan = {"from_name": "notes", "server": "notes", "how": "definition", "replace": False, "copied": [], "paths": [],
            "secrets": {}, "image": None, "changes": [], "notes": [], "warnings": [],
            "problems": [{"text": "this host has a server notes already", "fix": "--as NEWNAME"}]}
    text = mc.plan_text(plan, "lab1")
    assert "would be imported" in text and "definition imported" not in text and "not copied" in text, text
    assert "its definition imported" in mc.plan_text({**plan, "problems": []}, "lab1")
