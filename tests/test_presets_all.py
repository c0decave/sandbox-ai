"""Every built-in preset with every agent: `presets check` and a launcher dry run of `sandbox-ai AGENT --preset NAME
--preset-value K=V ... --dry-run`, with the values a user would give and what a preset names registered for real in a
private configuration (VPN profiles: WireGuard, Tailscale and Tor; LLM servers of every kind, added with sandbox-ai's
own `vpn add` and `llm add`; the catalog MCP servers of the presets as stand-in definitions). Optional values are left out
once and given once. An agent a preset is not for is refused; Claude Code with a server without the Anthropic API is
refused, by `presets check` and by the start. Each plan's network rules are checked against what the preset names
(network_of): the agents' own provider hosts unless --no-provider-defaults, the hosts of each --allow-PROFILE and
--allow-host, the --allow-tcp and --allow-host-port targets, the LLM server's address and the ranges of a VPN profile;
nothing else may be opened. A preset that a launcher change breaks, or that opens more than it says, fails here.

Nothing is contacted: profiles and endpoints are only written, host names are resolved by a stand-in, /dev and the
CDI directories are fakes (as in test_feature_matrix.py)."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write VPN keys and LLM endpoints: never into the real HOME, also without conftest.py

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_presets_all", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
P = s.load_presets_module()
AGENTS = ("claude", "codex", "opencode")
PRESETS = P.catalog()  # the built-in ones: HOME is a temporary one, there are none of yours
# The LLM servers registered below: (name, kind, URL, extra llm add options). "api" is a raw OpenAI-compatible API,
# "gateway" one with the Anthropic Messages API (--anthropic), "gpu" a vLLM behind the Tailscale profile.
SERVERS = (("ollama", "ollama", "http://127.0.0.1:11434", ["--model", "qwen3-coder:30b"]),
           ("lmstudio", "lmstudio", "http://127.0.0.1:1234", ["--model", "qwen/qwen3-coder-30b"]),
           ("llamacpp", "llamacpp", "http://127.0.0.1:8080", ["--model", "qwen3-coder"]),
           ("vllm", "vllm", "http://192.168.1.20:8000", ["--model", "Qwen/Qwen3-Coder-30B"]),
           ("api", "openai-compatible", "http://192.168.1.21:4000", ["--model", "qwen3"]),
           ("gateway", "openai-compatible", "https://192.168.1.22:4000/litellm", ["--anthropic", "--model", "qwen3"]),
           ("gpu", "vllm", "http://100.100.1.5:8000", ["--vpn", "ts", "--model", "Qwen/Qwen3-Coder-30B"]))
ANTHROPIC = {"ollama", "lmstudio", "llamacpp", "vllm", "gateway", "gpu"}
# The LLM presets and the server each is tried with first (its own kind; for Claude the raw API's Anthropic one).
LLM_PRESETS = {"local-api": "api", "ollama": "ollama", "lm-studio": "lmstudio", "llama-cpp": "llamacpp", "vllm": "vllm"}
WG_PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
# What each VPN profile registered below opens besides the tunnel: addresses allowed in the sandbox and the resolvers
# it gets with --dns (a full WireGuard tunnel: the profile's DNS; Tor: its DNSPort and the .onion range; Tailscale:
# the tailnet, no preset gives it --dns). Without a VPN --dns gives the launcher's DEFAULT_NAMESERVERS.
VPN_OPENS = {"home": {"allow": [], "nameservers": ["10.66.0.1"]},
             "anon": {"allow": ["10.192.0.0/10"], "nameservers": ["127.0.53.1"]},
             "ts": {"allow": ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]}}


def key():
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """A private host: its own configuration (VPN profiles, LLM servers, MCP stand-ins), /dev with fake devices,
    no policy, no DNS."""
    root = tmp_path_factory.mktemp("presets-all")
    patch = pytest.MonkeyPatch()
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
        (root / sub).mkdir()
        patch.setenv(name, str(root / sub))
    patch.setenv("SANDBOX_AI_SECRETS", "file")
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "WAYLAND_DISPLAY", "GIT_CONFIG_GLOBAL"):
        patch.delenv(name, raising=False)
    patch.setattr(s, "load_policy", lambda path=None: {})
    patch.setattr(s, "resolve_hosts", lambda hosts: {h: ["192.0.2.80"] for h in hosts})
    patch.setattr(s, "resolve_pod_hosts", lambda a, hosts: {h: ["192.0.2.80"] for h in hosts})
    patch.setattr(s, "user_manager_running", lambda: True)
    patch.setattr(s, "delegated_controllers", lambda path=None: {"memory", "cpu", "pids"})
    patch.setattr(s.os, "cpu_count", lambda: 8)
    # /dev: regular files presented as character devices (kvm, a serial port, a DRI render node).
    dev = root / "dev"
    for name in ("ttyACM0", "kvm", "dri/renderD128"):
        (dev / name).parent.mkdir(parents=True, exist_ok=True)
        (dev / name).write_text("")
    real_mode = s.device_mode
    patch.setattr(s, "DEV", dev)
    patch.setattr(s, "device_mode", lambda path: stat.S_IFDIR | 0o755 if Path(path).is_dir() else
                  stat.S_IFCHR | 0o666 if dev in Path(path).parents else real_mode(path))
    patch.setattr(s, "CDI_DIRS", (root / "cdi",))
    # VPN profiles and LLM servers, added with sandbox-ai's own commands.
    conf = root / "wg.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {key()}\nAddress = 10.66.0.2/32\nDNS = 10.66.0.1\n\n[Peer]\n"
                    f"PublicKey = {WG_PEER}\nEndpoint = 198.51.100.7:51820\nAllowedIPs = 0.0.0.0/0\n")
    conf.chmod(0o600)
    vpn, llm = s.load_vpn_module(), s.load_llm_module()
    for args in (["add", "home", "--wireguard", str(conf)], ["add", "ts", "--tailscale"],
                 ["add", "anon", "--tor", "--socks"]):
        assert vpn.main(args, out=lambda line: None) == 0, args
    for name, kind, url, extra in SERVERS:
        assert llm.main(["add", name, "--kind", kind, "--url", url, *extra], out=lambda line: None) == 0, name
    # The catalog servers the presets name, as stand-ins without network (sandbox-ai mcp add needs Podman).
    names = {server.split(":", 1)[-1] for preset in PRESETS.values() for server in preset.get("mcp", [])}
    (root / "config/sandbox-ai").mkdir(parents=True, exist_ok=True)
    (root / "config/sandbox-ai/mcp.json").write_text(json.dumps(
        {name: {"command": ["python3", "-c", "pass"], "options": ["--offline"], "description": f"stand-in for {name}"}
         for name in sorted(names)}))
    work = root / "work"
    work.mkdir()
    yield root, dev, work
    patch.undo()


def sample_values(preset, root, dev, agent, full):
    """{name: value} for PRESET as a user would give them: the required ones, and with FULL the optional ones and
    those with a default too."""
    values = {}
    for name, spec in preset.get("values", {}).items():
        if not full and (spec.get("optional") or "default" in spec):
            continue
        kind = spec.get("kind", "text")
        if kind == "folder":
            folder = root / f"value-{name}"
            folder.mkdir(exist_ok=True)
            values[name] = str(folder)
        elif kind == "device":
            values[name] = str(dev / ("kvm" if name == "kvm" else "ttyACM0"))
        elif kind == "vpn":
            values[name] = "anon" if preset["id"] == "tor" else "home"
        elif kind == "llm":
            server = LLM_PRESETS.get(preset["id"], "ollama")
            if agent == "claude" and server not in ANTHROPIC:
                server = "gateway"
            values[name] = f"{server}:some/model-7b" if full else server
        elif name == "gpu":
            values[name] = "dri"
        elif "default" in spec:
            values[name] = spec["default"]
        else:
            values[name] = "a task for the test"
    return values


def dry_run(capsys, work, agent, preset, values, *options):
    capsys.readouterr()
    argv = [agent, "--backend", "podman", "--workspace", str(work), "--no-tmux", "--preset", preset,
            *[token for name, value in values.items() for token in ("--preset-value", f"{name}={value}")], *options,
            "--dry-run"]
    assert s.launch(argv) == 0
    return json.loads(capsys.readouterr().out)


def network_of(preset, values, agent):
    """What the dry-run plan of PRESET with VALUES for AGENT must open, worked out from the options the preset names
    (expanded with its values): {plan field: expected value}. Anything a plan opens beyond it is a failure."""
    options = P.expand(preset, values, extras=False)
    pairs, index = [], 0
    while index < len(options):
        flag, value = options[index], None
        if index + 1 < len(options) and not options[index + 1].startswith("--"):
            value, index = options[index + 1], index + 1
        pairs.append((flag, value))
        index += 1
    team, providers, dns, vpn = [agent], True, False, None
    hosts, tcp, ports, allow = [], [], [], []
    for flag, value in pairs:
        profile = flag[len("--allow-"):] if flag.startswith("--allow-") else None
        if profile in s.PRESETS:
            hosts += s.PRESETS[profile]
        elif flag == "--allow-host":
            hosts.append(value)
        elif flag == "--allow-tcp":
            tcp.append(value)
        elif flag == "--allow-host-port":
            ports.append(int(value))
        elif flag == "--no-provider-defaults":
            providers = False
        elif flag == "--with":
            team.append(value)
        elif flag == "--vpn":
            allow, vpn = allow + VPN_OPENS[value]["allow"], value
        elif flag == "--dns":
            dns = True
        elif flag == "--llm":
            name, url, extra = next((n, u, e) for n, _, u, e in SERVERS if n == value.split(":", 1)[0])
            address = url.split("//", 1)[1].split("/", 1)[0]
            if address.startswith("127.0.0.1:"):
                ports.append(int(address.rsplit(":", 1)[1]))  # the host's loopback: --allow-host-port
            else:
                tcp.append(address)
            if "--vpn" in extra:
                allow += VPN_OPENS[extra[extra.index("--vpn") + 1]]["allow"]
        else:
            # Another option that opens something: this harness must learn what it opens first.
            opening = ("--allow", "--egress", "--frontier", "--offline", "--network", "--profile", "--nameserver",
                       "--host-")
            assert not flag.startswith(opening), f"{preset['id']}: the harness does not know what {flag} opens"
    nameservers = (VPN_OPENS[vpn]["nameservers"] if vpn else s.DEFAULT_NAMESERVERS) if dns else []
    expected_hosts = [host for member in team for host in s.PROVIDERS[member]] if providers else []
    expected_hosts = list(dict.fromkeys(expected_hosts + [host for host in hosts if not host.startswith("*.")]))
    return {"https_hosts": sorted(expected_hosts), "https_networks": sorted(s.host_networks(expected_hosts)),
            "host_wildcards": sorted(host for host in hosts if host.startswith("*.")), "allow_tcp": sorted(tcp),
            "host_ports": sorted(set(ports)), "allow": sorted(allow), "nameservers": nameservers,
            "egress_all_allow": False, "offline": False, "network_profiles": []}


def assert_network(plan, expected):
    found = {key: sorted(plan[key]) if isinstance(plan[key], list) and key != "nameservers" else plan[key]
             for key in expected}
    assert found == expected, {key: (found[key], expected[key]) for key in expected if found[key] != expected[key]}


def test_the_presets_asked_for_are_there():
    assert {"tor", "local-api", "ollama", "lm-studio", "llama-cpp", "vllm"} <= set(PRESETS)
    assert len(PRESETS) >= 30
    for ident in LLM_PRESETS:
        assert PRESETS[ident]["category"] == "llm" and "--llm" in PRESETS[ident]["options"]
        assert "--model" not in PRESETS[ident]["options"]  # the launcher refuses --model with --llm
        assert "--no-provider-defaults" in PRESETS[ident]["options"]  # only the server is opened, no cloud provider
    assert PRESETS["tor"]["category"] == "privacy" and PRESETS["tor"]["values"]["tor"]["kind"] == "vpn"


@pytest.mark.parametrize("full", [False, True], ids=["required-values", "every-value"])
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_with_every_agent(host, capsys, name, agent, full):
    root, dev, work = host
    preset = PRESETS[name]
    values = sample_values(preset, root, dev, agent, full)
    if agent not in preset.get("agents", AGENTS):
        with pytest.raises(ValueError, match=f"preset {name} is for"):
            dry_run(capsys, work, agent, name, values)
        assert any("is for" in line for line in P.missing(preset, values, agent))
        return
    assert P.missing(preset, values, agent) == [], "presets check"
    plan = dry_run(capsys, work, agent, name, values)
    assert plan["agent"] == agent
    assert_network(plan, network_of(preset, values, agent))
    assert [entry["name"] for entry in plan.get("mcp", [])] == [server.split(":", 1)[-1]
                                                                for server in preset.get("mcp", [])]
    if values.get("llm"):
        assert plan["llm"]["name"] == values["llm"].split(":", 1)[0], plan.get("llm")
    vpn = values.get("tor") or values.get("vpn")
    if vpn:
        assert plan["vpn"]["profile"] == vpn
    if name == "tor":
        assert plan["vpn"]["type"] == "tor" and plan["vpn"]["transparent"] and not plan["vpn"]["ipv6"]


@pytest.mark.parametrize("name", sorted(LLM_PRESETS))
@pytest.mark.parametrize("server", [entry[0] for entry in SERVERS])
@pytest.mark.parametrize("agent", AGENTS)
def test_the_llm_presets_with_every_kind_of_server(host, capsys, name, server, agent):
    """Each LLM preset with each kind of server, NAME and NAME:MODEL: OpenCode and Codex with any, Claude Code only
    with a server that has the Anthropic Messages API (`presets check` says why not, the start refuses)."""
    _, _, work = host
    preset = PRESETS[name]
    for value in (server, f"{server}:org/Qwen2.5-Coder-7B:q4" if name == "local-api" else f"{server}:qwen2.5-coder:7b"):
        if agent == "claude" and server not in ANTHROPIC:
            assert any("Anthropic Messages API" in line for line in P.missing(preset, {"llm": value}, agent))
            with pytest.raises(ValueError, match="Anthropic Messages API"):
                dry_run(capsys, work, agent, name, {"llm": value})
            continue
        assert P.missing(preset, {"llm": value}, agent) == []
        plan = dry_run(capsys, work, agent, name, {"llm": value})
        assert plan["llm"]["name"] == server
        # Only the server is opened: no provider host, nothing but its address (and the tailnet of its VPN).
        assert plan["https_hosts"] == [] and plan["https_networks"] == [] and plan["host_wildcards"] == []
        url = next(u for n, _, u, _ in SERVERS if n == server)
        address = url.split("//", 1)[1].split("/", 1)[0]
        assert (plan["allow_tcp"], plan["host_ports"]) == (
            ([], [int(address.rsplit(":", 1)[1])]) if address.startswith("127.0.0.1:") else ([address], []))
        assert plan["allow"] == (VPN_OPENS["ts"]["allow"] if server == "gpu" else [])
        assert_network(plan, network_of(preset, {"llm": value}, agent))
        if server == "gpu":  # registered with --vpn ts: the start goes behind it
            assert plan["vpn"]["profile"] == "ts"


def test_what_presets_check_says_before_a_start(host):
    """A VPN profile or LLM server that is not registered is named by `presets check`, with how to add it."""
    found = P.missing(PRESETS["tor"], {"tor": "nope"}, "codex")
    assert len(found) == 1 and "no VPN profile nope" in found[0] and "vpn add anon --tor" in found[0]
    found = P.missing(PRESETS["ollama"], {}, "opencode")  # the default name, ollama, is registered
    assert found == []
    found = P.missing(PRESETS["ollama"], {"llm": "other"}, "opencode")
    assert len(found) == 1 and "no LLM endpoint other" in found[0] and "llm add ollama --kind ollama" in found[0]
    assert P.missing(PRESETS["local-api"], {"llm": "api:bad model"}, "codex")[0].endswith("must be a llm: "
                                                                                          "'api:bad model'")


def test_the_network_check_catches_what_a_preset_does_not_name(host, capsys):
    """The harness itself: a host the preset does not name, a provider opened despite --no-provider-defaults, or a
    network option it does not know fails it."""
    _, _, work = host
    plan = dry_run(capsys, work, "codex", "ollama", {"llm": "ollama"})
    expected = network_of(PRESETS["ollama"], {"llm": "ollama"}, "codex")
    assert_network(plan, expected)
    with pytest.raises(AssertionError):
        assert_network({**plan, "https_hosts": ["evil.example"]}, expected)
    with pytest.raises(AssertionError):
        assert_network({**plan, "allow_tcp": ["192.0.2.9:22"]}, expected)
    without = {**PRESETS["ollama"],
               "options": [o for o in PRESETS["ollama"]["options"] if o != "--no-provider-defaults"]}
    assert network_of(without, {"llm": "ollama"}, "codex")["https_hosts"] == sorted(s.PROVIDERS["codex"])
    with pytest.raises(AssertionError, match="does not know what --egress-all-allow opens"):
        network_of({**PRESETS["development"], "options": ["--egress-all-allow"]}, {}, "codex")


def test_the_tor_preset_takes_only_a_tor_profile(host, capsys):
    """Finding L8: "Everything through Tor" with a WireGuard (or any other) profile is refused, in presets check and
    at the start (its dry run); with a Tor profile it starts as before."""
    _, _, work = host
    found = P.missing(PRESETS["tor"], {"tor": "home"}, "codex")
    assert len(found) == 1 and "of type wireguard" in found[0] and "needs a tor profile" in found[0]
    assert P.missing(PRESETS["tor"], {"tor": "anon"}, "codex") == []
    capsys.readouterr()
    argv = ["codex", "--backend", "podman", "--workspace", str(work), "--no-tmux", "--preset", "tor",
            "--preset-value", "tor=home", "--dry-run"]
    with pytest.raises(ValueError, match="must name a tor VPN profile; home is of type wireguard"):
        s.launch(argv)  # as the preset's other refusals (the launcher's main says it)
    assert not capsys.readouterr().out.strip(), "no plan"
    plan = dry_run(capsys, work, "codex", "tor", {"tor": "anon"})
    assert plan["vpn"]["type"] == "tor"
    # The value's constraint is a preset's own field: kind vpn only.
    assert any("vpn_type" in problem for problem in P.problems(
        {**PRESETS["tor"], "values": {"tor": {"label": "x", "kind": "text", "vpn_type": "tor"}}}))



def test_the_tor_promise_holds_against_the_command_line(host, capsys):
    """The command line wins over a preset's options; it must not undo "Everything through Tor": a --vpn of another
    type there, or --vpn-check warn (a leak found during the run would only be said), is refused. Another Tor
    profile on the command line is fine."""
    _, _, work = host
    base = ["codex", "--backend", "podman", "--workspace", str(work), "--no-tmux", "--preset", "tor",
            "--preset-value", "tor=anon"]
    with pytest.raises(ValueError, match="--vpn home on the command line is of type wireguard; this preset needs a "
                                         "tor profile"):
        P.apply([*base, "--vpn", "home"], canonical=s.canonical_options)
    with pytest.raises(ValueError, match="needs a tor profile"):
        P.apply([*base, "-vp", "home"], canonical=s.canonical_options)  # the short form too
    with pytest.raises(ValueError, match="drop --vpn-check warn"):
        P.apply([*base, "--vpn-check", "warn"], canonical=s.canonical_options)
    with pytest.raises(ValueError, match="drop --vpn-check warn"):
        P.apply([*base, "--vpn-check=warn"], canonical=s.canonical_options)
    applied = P.apply([*base, "--vpn", "anon", "--vpn-check", "stop"], canonical=s.canonical_options)
    assert applied.count("--vpn") == 2 and P.last_value(applied, "--vpn") == "anon"


def test_an_identity_a_preset_names_is_checked_with_how_to_make_it(host):
    preset = {**PRESETS["tor"], "id": "t", "options": [*PRESETS["tor"]["options"], "--identity", "{who}"],
              "values": {**PRESETS["tor"]["values"], "who": {"label": "identity", "kind": "text", "optional": True}}}
    found = P.missing(preset, {"tor": "anon", "who": "investigation"}, "codex")
    assert len(found) == 1 and "--identity investigation: no such identity yet" in found[0]
    assert "sandbox-ai identities add investigation" in found[0] and "--preset-value who=" in found[0]
    assert P.missing(preset, {"tor": "anon"}, "codex") == []  # left out: no identity, nothing to check
    assert P.missing(preset, {"tor": "anon", "who": "default"}, "codex") == []

# --- Bubblewrap, what a plan keeps beyond the network, the commands the notes name ------------------------------

def loopback_llm(values):
    """True when VALUES name an LLM server on this host's loopback (only Podman's pasta reaches it)."""
    server = (values.get("llm") or "").split(":", 1)[0]
    return any(name == server and "//127.0.0.1:" in url for name, _, url, _ in SERVERS)


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_with_bubblewrap(host, capsys, name, agent):
    """Every preset with --backend bwrap: a plan with the same network as with Podman; or, for a preset that needs the
    host's loopback (--allow-host-port, an LLM server on 127.0.0.1) or a VPN, which only Podman gives, a refusal that
    names the Podman backend; its notes say so ("Podman only")."""
    root, dev, work = host
    preset = PRESETS[name]
    if agent not in preset.get("agents", AGENTS):
        return
    values = sample_values(preset, root, dev, agent, True)
    options = P.expand(preset, values, extras=False)
    if "--allow-host-port" in options or "--vpn" in options or loopback_llm(values):
        assert any("Podman only" in note or "only with Podman" in note for note in preset.get("notes", [])), name
        with pytest.raises(ValueError, match="Podman backend"):
            dry_run(capsys, work, agent, name, values, "--backend", "bwrap")
        return
    plan = dry_run(capsys, work, agent, name, values, "--backend", "bwrap")
    assert plan["backend"] == "bwrap" and plan["agent"] == agent
    assert_network(plan, network_of(preset, values, agent))


def option_pairs(options):
    pairs, index = [], 0
    while index < len(options):
        flag, value = options[index], None
        if index + 1 < len(options) and not options[index + 1].startswith("--"):
            value, index = options[index + 1], index + 1
        pairs.append((flag, value))
        index += 1
    return pairs


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_plan_keeps_what_the_preset_promises(host, capsys, name, agent):
    """Beyond the network (above): each folder mounted where and as the preset says, the virtual screen and its size,
    the audio, the resource limits, the security mode as a floor, a read-only workspace, the privacy check, the
    canaries, the companion agent, the subnet, the devices, and the first task with its values filled in (OpenCode's
    after --prompt)."""
    root, dev, work = host
    preset = PRESETS[name]
    if agent not in preset.get("agents", AGENTS):
        return
    values = sample_values(preset, root, dev, agent, True)
    plan = dry_run(capsys, work, agent, name, values)
    rank = {"lax": 0, "low": 0, "medium": 1, "high": 2, "paranoid": 3}
    for flag, value in option_pairs(P.expand(preset, values, extras=False)):
        if flag == "--mount":
            assert value in plan["mounts"], (value, plan["mounts"])
            assert value.endswith(":ro"), f"{name}: samples and evidence stay read-only"
        elif flag == "--gui":
            assert plan["gui"], name
        elif flag == "--gui-screen":
            assert value in json.dumps(plan["gui"]), plan["gui"]
        elif flag == "--audio":
            assert plan["audio"] == value
        elif flag in ("--memory", "--cpus", "--pids"):
            assert str(plan["limits"].get(flag[2:])) == value, (flag, plan["limits"])
        elif flag == "--mode":
            assert rank[plan["mode"]["name"]] >= rank[value], plan["mode"]
            assert plan["mode"]["name"] == value  # a laxer default (medium) is raised to it
        elif flag == "--workspace-ro":
            assert plan["workspace_mode"] == "ro"
        elif flag == "--privacy-check":
            assert plan["privacy_check"] == value
        elif flag == "--canaries":
            assert plan["canaries"], name
        elif flag == "--with":
            assert value in json.dumps(plan["companions"]), plan["companions"]
        elif flag == "--subnet":
            assert plan["sandbox_network"] and value.rsplit(".", 1)[0] in json.dumps(plan["sandbox_network"])
        elif flag == "--device":
            assert value in [entry["path"] for entry in plan["devices"]], plan["devices"]
        elif flag == "--gpu":
            assert plan["gpu"] == value
    text = preset.get("prompt")
    if text:
        args = plan["agent_args"]
        assert args[:1] == ["--prompt"] if agent == "opencode" else len(args) == 1, args
        assert not P.PLACEHOLDER.search(args[-1]), args[-1]
        for value_name in P.PLACEHOLDER.findall(text):
            assert values[value_name] in args[-1]
    else:
        assert plan["agent_args"] == []


# `sandbox-ai WORD [SUBCOMMAND]` in a preset's notes, hints and labels; the --options of the same note.
COMMAND = re.compile(r"sandbox-ai ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?")
# Options of other programs the notes show (llama-server, vllm serve, nvidia-ctk, python3 -m http.server, curl).
FOREIGN = {"--jinja", "--host", "--port", "--enable-auto-tool-choice", "--tool-call-parser", "--served-model-name",
           "--output", "--bind", "--directory", "--socks5-hostname"}
PROSE = {"opens"}  # "sandbox-ai opens that one port": sandbox-ai as the subject of a sentence
HELP_CACHE = {}


def help_text(words):
    """The --help of `sandbox-ai WORDS...` (python3 -I sandbox/main.py), or None when it is not a command."""
    key = tuple(words)
    if key not in HELP_CACHE:
        result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), *words, "--help"],
                                capture_output=True, text=True, timeout=60)
        HELP_CACHE[key] = (result.stdout + result.stderr) if result.returncode == 0 else None
    return HELP_CACHE[key]


def test_the_commands_and_options_the_presets_name_exist(host):
    """Every command a preset's notes, hints and labels tell you to run is one of sandbox-ai's (with that subcommand),
    and every --option such a text names is an option of a command it names or of the start (`sandbox-ai AGENT`):
    no note sends you to a command or option that is not there."""
    main = importlib.util.spec_from_file_location("sandbox_ai_main_for_presets", ROOT / "sandbox/main.py")
    module = importlib.util.module_from_spec(main)
    main.loader.exec_module(module)
    commands = set(module.PYTHON_COMMANDS) | {"build", "update", "version"}
    launcher = help_text(["codex"])
    checked, wrong = 0, []
    for preset in PRESETS.values():
        texts = list(preset.get("notes", [])) + [spec.get(key, "") for spec in preset.get("values", {}).values()
                                                  for key in ("hint", "label")]
        for text in texts:
            helps = [launcher]
            for word, sub in COMMAND.findall(text):
                if word in PROSE:
                    continue
                if word not in commands:
                    wrong.append((preset["id"], f"no command {word}", text))
                    continue
                if word == "build":
                    helps.append("--variant")  # sandbox-ai build --variant NAME (build_image.sh: no --help)
                if word in ("build", "update", "claude", "codex", "opencode"):
                    continue
                top = help_text([word])
                choices = re.search(r"\{([a-z,-]+)\}", top or "")
                if sub and choices and sub in choices.group(1).split(","):
                    helps.append(help_text([word, sub]))
                else:
                    helps.append(top)
                if not helps[-1]:
                    wrong.append((preset["id"], f"no help for {word} {sub}", text))
                checked += 1
            for option in sorted(set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", text)) - FOREIGN):
                if not any(option in found for found in helps if found):
                    wrong.append((preset["id"], f"no command named here has {option}", text))
    assert wrong == []
    assert checked > 20


def test_your_own_development_preset_with_its_folders(host, capsys, monkeypatch):
    """A preset of your own as a developer writes one (~/.config/sandbox-ai/presets): its folders as workspaces with
    defaults in HOME, GitHub and PyPI, a Git server on this host's loopback, two ports of a machine in the LAN, an MCP
    server and a publishing login by values, the virtual screen and limits. With every folder there; with optional
    ones missing (left out, said); with the main one missing (refused, naming the --preset-value); started from the
    main folder, whose trusted project file names one of the folders again in another spelling (once, not refused as
    overlapping itself); with Bubblewrap (refused for the loopback port; with that value left empty it starts)."""
    root, _, work = host
    home = Path(os.environ["HOME"])
    for name in ("main", "lib", "notes"):
        (home / "code" / name).mkdir(parents=True, exist_ok=True)
    own = {"format": "sandbox-ai preset", "version": 1, "id": "own-dev", "title": "Own dev", "description": "d",
           "category": "development",
           "options": ["--workspace", "{main}", "--workspace", "{lib}", "--workspace", "{notes}", "--workspace", "{gone}",
                       "--allow-github", "--allow-pypi", "--allow-host-port", "{git_port}", "--allow-tcp", "{lan}:1234",
                       "--allow-tcp", "{lan}:22", "--mcp", "{tool}", "--publish", "{publish}", "--gui", "--memory",
                       "{memory}", "--cpus", "{cpus}", "--pids", "8192"],
           "values": {"main": {"label": "Main", "kind": "folder", "default": "~/code/main"},
                      "lib": {"label": "Lib", "kind": "folder", "default": "~/code/lib", "optional": True},
                      "notes": {"label": "Notes", "kind": "folder", "default": "~/code/notes", "optional": True},
                      "gone": {"label": "Gone", "kind": "folder", "default": "~/code/gone", "optional": True},
                      "git_port": {"label": "Git", "kind": "port", "default": "3000", "optional": True},
                      "lan": {"label": "LAN", "kind": "host", "default": "192.0.2.44"},
                      "tool": {"label": "MCP", "default": "capa", "optional": True},
                      "publish": {"label": "Publish", "default": "pypi", "optional": True},
                      "memory": {"label": "Memory", "default": "16g"},
                      "cpus": {"label": "CPUs", "kind": "number", "default": "8"}}}
    presets = Path(os.environ["XDG_CONFIG_HOME"]) / "sandbox-ai" / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    (presets / "own-dev.json").write_text(json.dumps(own))
    monkeypatch.setenv("PYPI_FAKE", "pypi-not-a-token")
    assert s.load_secret_store_module().main(["set", "PYPI_TOKEN", "--from-env", "PYPI_FAKE"], out=lambda line: None) == 0
    assert s.load_publish_logins_module().main(["add", "pypi", "--secret", "PYPI_TOKEN"], out=lambda line: None) == 0
    try:
        assert P.missing(P.load("own-dev"), {}, "codex") == []

        def start(*extra, cwd=None, backend="podman"):
            capsys.readouterr()
            argv = ["codex", "--backend", backend, "--no-tmux", "--preset", "own-dev", *extra, "--dry-run"]
            if cwd:
                monkeypatch.chdir(cwd)
            assert s.launch(argv) == 0
            captured = capsys.readouterr()
            return json.loads(captured.out), captured.err

        plan, err = start()
        assert [w["host"] for w in plan["workspaces"]] == [str(home / "code" / n) for n in ("main", "lib", "notes")]
        assert plan["workspaces"][0]["sandbox"] == "/work"
        assert "code/gone does not exist here, left out (another one: --preset-value gone=PATH)" in err
        assert plan["host_ports"] == [3000] and sorted(plan["allow_tcp"]) == ["192.0.2.44:1234", "192.0.2.44:22"]
        assert [m["name"] for m in plan["mcp"]] == ["capa"] and plan["publish"][0]["kind"] == "pypi"
        assert plan["gui"] and plan["limits"] == {"memory": "16g", "cpus": "8", "pids": 8192}
        # values of yours: another memory limit, folders and servers left out
        plan, _ = start("--preset-value", "memory=32g", "--preset-value", "lib=", "--preset-value", "tool=",
                        "--preset-value", "publish=")
        assert plan["limits"]["memory"] == "32g" and len(plan["workspaces"]) == 2
        assert plan["mcp"] == [] and plan["publish"] == []
        # an optional folder that is gone: left out; the main one gone: refused, with what to give
        (home / "code" / "notes").rmdir()
        plan, err = start()
        assert len(plan["workspaces"]) == 2 and "code/notes does not exist here, left out" in err
        (home / "code" / "main").rename(home / "code" / "main-moved")
        with pytest.raises(ValueError, match=r"main \(Main\): its default ~/code/main does not exist here; create it, "
                                             r"or give another: --preset-value main=PATH"):
            start()
        assert any("its default ~/code/main does not exist" in line for line in P.missing(P.load("own-dev"), {}))
        (home / "code" / "main-moved").rename(home / "code" / "main")
        (home / "code" / "notes").mkdir()
        # started in the main folder, whose trusted project file names a folder of the preset as ~/code/lib
        project = home / "code" / "main" / ".sandbox-ai.json"
        project.write_text(json.dumps({"version": 1, "agent": "codex", "workspaces": ["~/code/lib", str(work)]}))
        s.trust_config(project)
        plan, _ = start(cwd=home / "code" / "main")
        assert plan["project_config"] == str(project)
        assert [w["host"] for w in plan["workspaces"]] == [str(home / "code" / n) for n in ("main", "lib", "notes")] + \
            [str(work)]
        project.unlink()
        s.forget_config(project)
        # Bubblewrap: the host's loopback only with Podman; without the Git server's port it starts
        with pytest.raises(ValueError, match="--allow-host-port needs the Podman backend"):
            start(backend="bwrap")
        plan, _ = start("--preset-value", "git_port=", backend="bwrap")
        assert plan["backend"] == "bwrap" and plan["host_ports"] == []
    finally:
        (presets / "own-dev.json").unlink()
        s.load_publish_logins_module().main(["remove", "pypi"], out=lambda line: None)
