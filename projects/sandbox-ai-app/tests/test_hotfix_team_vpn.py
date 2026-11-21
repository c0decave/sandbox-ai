"""Hotfix 0.1.96 in the app (offscreen): the Teams page's VPN, and the MCP servers of an imported preset.

A) The approved task: the Teams page had only a free "Options" field, and `-- --vpn PROFILE` gave each member its own
   VPN container, one tunnel of the same profile per member. The team dialog now has a "VPN profile" picker (the
   host's profiles with their type) for every member and, under "Per member (advanced)", another one per member;
   one profile in several members is checked as sandbox-ai checks it (Tor said, WireGuard/Mullvad and a persistent
   Tailscale node refused with the reason, OpenVPN only with its box ticked). Members may be any of the seven agents.
B) The user: "beim Import einzeln fragen" - a preset that is not your own and brings MCP servers with network access
   shows each such server at import (what it runs, its network, its VPN) and includes it only after a yes for it
   (default no; else it is left out, and that is said).

Then the real ./sandbox-ai (tests/live_fakes.py) runs what the dialog built."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QDialog  # noqa: E402

from sandbox_ai_app import backend, presets as PR, team_page, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
PROFILES = [{"profile": "tor", "type": "tor", "provider": "tor"},
            {"profile": "mv1", "type": "wireguard", "provider": "mullvad"},
            {"profile": "mv2", "type": "wireguard", "provider": "mullvad"},
            {"profile": "wg", "type": "wireguard", "provider": "generic"},
            {"profile": "ovpn", "type": "openvpn", "provider": "generic"},
            {"profile": "ts", "type": "tailscale", "provider": "tailscale", "ephemeral": False},
            {"profile": "tse", "type": "tailscale", "provider": "tailscale", "ephemeral": True}]


def synchronous(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(team_page, "background", synchronous)
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "vpn_profiles", lambda: PROFILES)
    return sai, tmp_path


def dialog_for(members, lead="claude", vpn=None):
    dialog = team_page.StartDialog(None, "local", False, "/srv/w", PROFILES)
    dialog.name.setText("t")
    dialog.lead.setCurrentIndex(dialog.lead.findData(lead))
    for agent, box in dialog.members.items():
        box.setChecked(agent in members)
    if vpn:
        dialog.vpn.setCurrentIndex(dialog.vpn.findData(vpn))
    return dialog


# --- A: the team dialog ------------------------------------------------------------------------------------------

def test_the_apps_rule_is_sandbox_ais_rule(env):
    sai, _ = env
    vpn = sai.module("vpn")
    for entry in PROFILES:
        core = {"type": entry["type"], **({"tailscale": {"ephemeral": entry.get("ephemeral", False)}}
                                          if entry["type"] == "tailscale" else {})}
        assert team_page.vpn_sharing(entry) == vpn.sharing(core), entry["profile"]


def test_the_picker_lists_the_hosts_profiles_with_their_type_and_every_agent_can_be_a_member(env):
    dialog = dialog_for(["codex"])
    labels = [dialog.vpn.itemText(i) for i in range(dialog.vpn.count())]
    assert labels[0] == "(none)" and "mv1 (mullvad)" in labels and "ovpn (openvpn)" in labels and "tor (tor)" in labels
    assert list(dialog.members) == list(team_page.AGENTS)
    # sandbox-ai's own team.AGENTS: every agent of the app since 0.1.98 (OpenCode 2, OpenHands, nanobot, Letta Code and
    # Pi are team members too)
    assert team_page.AGENTS == tuple(env[0].module("team").AGENTS) and set(backend.AGENTS) == set(team_page.AGENTS)
    assert dialog.options.isVisibleTo(dialog), "the free Options field stays"
    dialog.deleteLater()


def test_tor_for_every_member_is_allowed_and_said(env):
    dialog = dialog_for(["codex", "goose"], vpn="tor")
    assert "each member gets a Tor of its own" in dialog.vpn_notes.text() and dialog.problem.text() == ""
    assert dialog.request()[4:] == ("tor", {}, False)
    dialog.deleteLater()


@pytest.mark.parametrize("profile, label", [("mv1", "mv1 (mullvad)"), ("wg", "wg (wireguard)")])
def test_one_wireguard_or_mullvad_profile_for_several_members_is_refused_with_reason_and_suggestion(env, profile,
                                                                                                    label):
    dialog = dialog_for(["codex"], vpn=profile)
    text = dialog.problem.text()
    assert f"{label} would run in 2 members at once (claude, codex)" in text
    assert "same tunnel address" in text and "whichever tunnel spoke last" in text
    assert "Mullvad allows several devices" in text and "Tor" in text
    with pytest.raises(ValueError, match="same tunnel address"):
        dialog.request()
    # Per member (advanced): one Mullvad device per member is fine.
    dialog.per_member.setChecked(True)
    assert dialog.member_vpn["codex"][1].isVisibleTo(dialog) and not dialog.member_vpn["qwen"][1].isVisibleTo(dialog)
    dialog.vpn.setCurrentIndex(dialog.vpn.findData("mv1"))
    dialog.member_vpn["codex"][1].setCurrentIndex(dialog.member_vpn["codex"][1].findData("mv2"))
    assert dialog.problem.text() == ""
    assert dialog.request()[4:] == ("mv1", {"codex": "mv2"}, False)
    dialog.deleteLater()


def test_openvpn_for_several_members_only_after_the_box_is_ticked(env):
    dialog = dialog_for(["codex"])
    assert not dialog.shared_openvpn.isVisibleTo(dialog), "no box while it is not needed"
    dialog.vpn.setCurrentIndex(dialog.vpn.findData("ovpn"))
    assert dialog.shared_openvpn.isVisibleTo(dialog) and "duplicate-cn" in dialog.problem.text()
    with pytest.raises(ValueError, match="duplicate-cn"):
        dialog.request()
    dialog.shared_openvpn.setChecked(True)
    assert dialog.problem.text() == "" and "as you confirmed" in dialog.vpn_notes.text()
    assert dialog.request()[4:] == ("ovpn", {}, True)
    dialog.deleteLater()


def test_tailscale_once_an_ephemeral_profile_for_each(env):
    dialog = dialog_for(["codex"], vpn="ts")
    assert "a Tailscale node runs once at a time" in dialog.problem.text()
    dialog.vpn.setCurrentIndex(dialog.vpn.findData("tse"))
    assert dialog.problem.text() == "" and "ephemeral Tailscale node" in dialog.vpn_notes.text()
    dialog.deleteLater()


def test_start_runs_team_start_with_the_vpn_options(env, monkeypatch):
    sai, _ = env
    opened = []
    page = team_page.TeamPage(sai, lambda argv, title, **kw: opened.append(argv))

    def start(dialog):
        dialog.name.setText("trio")
        dialog.members["hermes"].setChecked(True)
        dialog.members["dsh"].setChecked(True)
        dialog.workspace.setText("/srv/w")
        dialog.vpn.setCurrentIndex(dialog.vpn.findData("tor"))
        dialog.per_member.setChecked(True)
        dialog.member_vpn["dsh"][1].setCurrentIndex(dialog.member_vpn["dsh"][1].findData("mv1"))
        dialog.accept()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(team_page.StartDialog, "exec", start)
    page.start()
    argv = opened[-1]
    index = argv.index("start")
    assert argv[index:index + 10] == ["start", "--agents", "claude,codex,hermes,dsh", "--workspace", "/srv/w",
                                      "--vpn", "tor", "--member-vpn", "dsh=mv1", "trio"]
    page.deleteLater()


def test_team_list_lines_with_the_other_agents_are_read(env):
    sai, _ = env
    sai.host_run = lambda host, *args, timeout=60: (0, "mix   hermes, qwen, dsh, goose   /srv/w\n", "")
    assert sai.teams() == [{"name": "mix", "members": ["hermes", "qwen", "dsh", "goose"], "workspace": "/srv/w"}]
    with pytest.raises(ValueError):
        sai.team_start_argv("local", "t", ["claude", "codex"], "/w", vpn="../x")
    with pytest.raises(ValueError):
        sai.team_start_argv("local", "t", ["claude", "codex"], "/w", member_vpn={"goose": "tor"})


def test_e2e_the_dialogs_command_runs_and_sandbox_ai_refuses_one_wireguard_key_twice(env, tmp_path):
    """The real ./sandbox-ai runs the argv the app built: refused with the reason; nothing started."""
    sai, _ = env
    root = Path(os.environ["SANDBOX_AI_HOME"])
    sys.path.insert(0, str(root / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.env["SANDBOX_AI_SECRETS"] = "file"
    conf = host.directory / "wg.conf"
    conf.write_text("[Interface]\nPrivateKey = yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=\nAddress = 10.66.1.2/32\n"
                    "DNS = 10.66.0.1\n\n[Peer]\nPublicKey = xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg=\n"
                    "AllowedIPs = 0.0.0.0/0\nEndpoint = 198.51.100.7:51820\n")
    conf.chmod(0o600)
    assert host.run("vpn", "add", "wg", "--wireguard", str(conf)).returncode == 0
    argv = sai.team_start_argv("local", "e2e", ["claude", "codex"], str(host.workspace), [], vpn="wg")
    start = argv.index("team")
    done = subprocess.run(host.command(*argv[start:]), env=host.env, cwd=host.workspace, capture_output=True,
                          text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert done.returncode == 1 and "would run in 2 members at once (claude, codex)" in done.stderr, done.stderr
    assert "same tunnel address" in done.stderr and host.calls() == []


# --- B: "beim Import einzeln fragen" ----------------------------------------------------------------------------

def foreign_preset(path):
    document = {"format": PR.FORMAT, "version": 1, "id": "shared-research", "title": "Shared research",
                "category": "simple", "options": ["--mcp", "fetch", "--mcp=search", "--mcp-limits",
                                                  "fetch:memory=1g", "--mcp", "calc", "--mcp-env", "search:X=1"],
                "mcp_servers": {
                    "fetch": {"command": ["uvx", "mcp-server-fetch"], "options": ["--allow-pypi",
                                                                                  "--egress-all-allow"]},
                    "search": {"command": ["uvx", "duckduckgo-mcp-server"],
                               "options": ["--vpn", "<vpn:search>", "--allow-duckduckgo"]},
                    "calc": {"command": ["python3", "calc.py"], "options": ["--offline"]}},
                "placeholders": [{"marker": "<vpn:search>", "label": "VPN of the search", "hint": "",
                                  "optional": False, "follows": ""}]}
    path.write_text(json.dumps(document))
    return path


@pytest.fixture
def gallery(env):
    from sandbox_ai_app.preset_gallery import PresetGallery
    sai, root = env
    (root / "project").mkdir()
    dialog = PresetGallery(sai, str(root / "project"), "claude", {}, None)
    yield dialog
    dialog.deleteLater()


def test_each_networked_server_is_shown_and_asked_for_offline_ones_are_not(env, tmp_path):
    sai, _ = env
    preset = PR.import_file(sai, foreign_preset(tmp_path / "p.json"))
    found = dict(PR.networked_servers(sai, preset))
    assert sorted(found) == ["fetch", "search"], "calc runs offline: nothing to ask"
    assert found["fetch"] == {"what": "uvx mcp-server-fetch", "network": "--allow-pypi --egress-all-allow",
                              "vpn": None}
    assert found["search"]["vpn"] == "<vpn:search>"


def test_at_import_the_default_is_no_and_what_is_left_out_is_said(gallery, env, tmp_path, monkeypatch):
    sai, _ = env
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # every question answered with its default: Leave it out
    preset = gallery.import_preset(str(foreign_preset(tmp_path / "p.json")))
    asked = [(title, text) for title, text, _ in widgets.SHOWN if title.startswith("MCP server")]
    assert [title for title, _ in asked] == ["MCP server fetch with network", "MCP server search with network"]
    assert "Runs: uvx mcp-server-fetch" in asked[0][1] and "Network access: --allow-pypi --egress-all-allow" in \
        asked[0][1] and "VPN: none of its own" in asked[0][1]
    assert "VPN: <vpn:search>" in asked[1][1]
    assert [name for name, _ in preset.servers] == ["calc"]
    assert list(preset.options) == ["--mcp", "calc"], "their --mcp and per-server options went with them"
    assert preset.placeholders == (), "the search's VPN placeholder went with it"
    kept = PR.load_mine(sai)[0]
    assert [name for name, _ in kept[0].servers] == ["calc"]


def test_a_yes_for_one_server_includes_exactly_that_one(gallery, env, tmp_path, monkeypatch):
    sai, _ = env
    answers = iter([False, True])  # fetch: no; search: yes
    monkeypatch.setattr(widgets, "choose", lambda *args, **kw: "include" if next(answers) else None)
    preset = gallery.import_preset(str(foreign_preset(tmp_path / "p.json")))
    assert sorted(name for name, _ in preset.servers) == ["calc", "search"]
    assert list(preset.options) == ["--mcp=search", "--mcp", "calc", "--mcp-env", "search:X=1"]
    assert [p.marker for p in preset.placeholders] == ["<vpn:search>"]
