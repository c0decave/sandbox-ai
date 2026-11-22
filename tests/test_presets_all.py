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
# The catalog servers a preset gives a folder of its own (--mcp-mount): engram and memory at /data, the file-analysis
# servers the samples at /work, smali and frida also a writable /out for what they make.
CATALOG_FOLDERS = {"engram": ["/data"], "memory": ["/data"], "binwalk": ["/work"], "capa": ["/work"], "jadx": ["/work"], "cfr": ["/work"],
                   "radare2": ["/work"], "floss": ["/work"], "yara": ["/work"], "smali": ["/work", "/out"],
                   "frida": ["/work", "/out"], "axross": ["/work", "/axross", "/ssh"]}
# The entries of the two MCP catalogs (sandbox-ai-mcp: "mcp", axross-mcp-hub: "hub") a preset may name; checked against
# the catalogs themselves where a checkout has them (test_the_mcp_servers_the_presets_name_are_in_the_catalogs).
CATALOG_ENTRIES = {
    "mcp": {"axross", "binwalk", "brave-search", "capa", "cfr", "duckduckgo", "engram", "frida", "gowitness", "jadx",
            "memory",
            "openosint", "osint-mcp", "playwright", "radare2", "searxng", "signal", "smali", "telegram",
            "telegram-user", "web-fetch", "whatsapp", "whatweb"},
    "hub": {"binwalk", "capa", "crtsh", "dnstwist", "exiftool", "ffuf", "floss", "gitleaks", "john", "mcpcap", "nmap",
            "nuclei", "radare2", "sleuthkit", "sqlmap", "tshark", "yara"},
}
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
    # The keys a preset's --api-key names (coding-goose: Anthropic's), kept as a user keeps them: a start refuses a
    # key that is not there, the dry run too.
    patch.setenv("SANDBOX_AI_TEST_API_KEY", "sk-ant-not-a-key")
    for secret in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):  # default-dsh: DeepSeek's
        assert s.load_secret_store_module().main(["set", secret, "--from-env", "SANDBOX_AI_TEST_API_KEY"],
                                                 out=lambda line: None) == 0
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
    # The catalog servers the presets name (in "mcp", and by default in an --mcp option), as stand-ins without network
    # (sandbox-ai mcp add needs Podman); those with a folder of their own (engram, memory) as image servers with it, as
    # `sandbox-ai mcp add NAME --mount /data=DIR:rw` writes them, so that a preset's --mcp-mount can change it.
    names = {server.split(":", 1)[-1] for preset in PRESETS.values() for server in preset.get("mcp", [])}
    for preset in PRESETS.values():
        defaults = {name: str(spec["default"]) for name, spec in preset.get("values", {}).items() if "default" in spec}
        names |= {value for value, _ in P.option_values(preset, defaults, "--mcp")}
        names |= set(mcp_values(preset))
    (root / "config/sandbox-ai").mkdir(parents=True, exist_ok=True)
    servers = {}
    for name in sorted(names):
        if name in CATALOG_FOLDERS:
            default = root / "brains" / name
            default.mkdir(parents=True, exist_ok=True)
            servers[name] = {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-{name}@sha256:" + "a" * 64, "network": "none",
                             "mounts": [{"target": target, "host": str(default), "mode": "rw"}
                                        for target in CATALOG_FOLDERS[name]],
                             "description": f"stand-in for {name}"}
        else:
            servers[name] = {"command": ["python3", "-c", "pass"], "options": ["--offline"],
                             "description": f"stand-in for {name}"}
    (root / "config/sandbox-ai/mcp.json").write_text(json.dumps(servers))
    # The images the presets' services name (--service NAME:image=...), as `sandbox-ai images pull --as NAME` records
    # them (pulled, pinned by digest, unchecked).
    images = {}
    for index, (name, user) in enumerate((("postgres", "999:999"), ("redis", "999:1000"), ("web", "101:101"),
                                          ("showcase-web", "101:101"))):
        images[name] = {"kind": "pulled", "image": f"docker.io/library/{name}@sha256:" + str(index) * 64,
                        "id": "sha256:" + str(index + 5) * 64, "digest": "sha256:" + str(index) * 64, "user": user,
                        "ports": [], "os": {}}
    (root / "config/sandbox-ai/user-images.json").write_text(json.dumps({"version": 1, "images": images}))
    # The image variants the presets start (--image-variant): made from their recipe, as the preset's "needs" says
    # (`sandbox-ai images recipe NAME --from AGENT`; a start of another agent takes it over), never built.
    spec = importlib.util.spec_from_file_location("images_for_presets_all", ROOT / "sandbox/images.py")
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    for preset in PRESETS.values():
        defaults = {name: str(spec["default"]) for name, spec in preset.get("values", {}).items() if "default" in spec}
        for variant, _ in P.option_values(preset, defaults, "--image-variant"):
            if variant not in {name for name, _ in images.variants()}:
                images.recipe(variant, "claude")
    work = root / "work"
    work.mkdir()
    yield root, dev, work
    patch.undo()


def mcp_values(preset):
    """The names of PRESET's values that name an MCP server of yours (--mcp {name}): the registered server's name."""
    options = preset.get("options", [])
    return [match for index, option in enumerate(options) if index and options[index - 1] == "--mcp"
            for match in P.PLACEHOLDER.findall(option)]


def sample_values(preset, root, dev, agent, full):
    """{name: value} for PRESET as a user would give them: the required ones, and with FULL the optional ones and
    those with a default too."""
    values = {}
    for name, spec in preset.get("values", {}).items():
        kind = spec.get("kind", "text")
        if kind == "device" and "default" in spec:
            # A device by default (android-development: /dev/kvm) is this computer's - here always the fake one.
            values[name] = str(dev / ("kvm" if name == "kvm" else "ttyACM0"))
            continue
        if not full and (spec.get("optional") or "default" in spec):
            continue
        if kind == "folder":
            folder = root / f"value-{name}"
            folder.mkdir(exist_ok=True)
            values[name] = str(folder)
        elif kind == "device":
            values[name] = str(dev / ("kvm" if name == "kvm" else "ttyACM0"))
        elif kind == "vpn":
            values[name] = "anon" if preset["id"] == "tor" else "home"
        elif kind == "session":
            values[name] = "coder"  # (the session a preset's bridge joins: proposed once the start runs, never here)
        elif kind == "llm":
            server = LLM_PRESETS.get(preset["id"], "ollama")
            if agent == "claude" and server not in ANTHROPIC:
                server = "gateway"
            values[name] = f"{server}:some/model-7b" if full else server
        elif name == "gpu":
            values[name] = "dri"
        elif "default" in spec:
            values[name] = spec["default"]
        elif name in mcp_values(preset):
            values[name] = name  # (android-development: jadx, smali, frida - the servers of that name)
        elif kind == "host" and name == "domain":
            values[name] = "lab.example.org"  # --allow-domain: a name, never an address
        elif kind == "host":
            values[name] = "192.0.2.44"
        elif kind == "port":
            values[name] = "8443"
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
        elif flag == "--allow-domain":
            hosts += [value, "*." + value]  # the domain and every name below it
        elif flag == "--api-key":
            hosts.append(value.split("=", 1)[0])  # the key's host, on TCP/443
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


# Every agent of the launcher has a basic preset: the agent with sandbox-ai's defaults, nothing opened beyond its model,
# nothing to fill in (only a login or a key in the keyring) - usable at once.


def every_agent():
    import ast
    tree = ast.parse((ROOT / "sandbox/agent_mcp.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "AGENTS":
            return ast.literal_eval(node.value)
    raise AssertionError("agent_mcp.AGENTS not found")


@pytest.mark.parametrize("agent", every_agent())
def test_every_agent_has_a_basic_default_preset(agent):
    preset = PRESETS.get(f"default-{agent}")
    assert preset, f"no preset default-{agent}"
    assert preset["agents"] == [agent] and preset["category"] == "agents"
    assert preset["translations"]["de"]["title"].endswith("(Standard)")
    allowed = {"--api-key", "--model"}
    flags = {option for option in preset["options"] if option.startswith("-")}
    assert flags <= allowed, f"default-{agent} opens more than its model: {flags - allowed}"
    assert not preset.get("values") and not preset.get("mcp")  # nothing to fill in, no servers


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
    filled = {**{key: str(spec["default"]) for key, spec in preset.get("values", {}).items() if "default" in spec},
              **values}
    # The MCP servers: the --mcp ones, the catalog's, and a --helper's (the agent as an MCP server of this run, one per
    # agent and main workspace).
    assert [entry["name"] for entry in plan.get("mcp", [])] == [
        *[value for value, _ in P.option_values(preset, filled, "--mcp")],
        *[server.split(":", 1)[-1] for server in preset.get("mcp", [])],
        *[s.helper_name(value, plan["workspaces"][0]["host"]) for value, _ in P.option_values(preset, filled, "--helper")]]
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


# --- what the presets name: categories, catalog servers, rulesets -------------------------------------------------

def test_every_preset_is_in_a_category_of_the_one_list():
    """The categories are defined once (presets.CATEGORY_INFO, with English and German titles and descriptions);
    every built-in preset names one of them (no older one, no fallback), and each category but the fallback has a
    preset."""
    assert set(P.CATEGORIES) == set(P.CATEGORY_INFO) and P.DEFAULT_CATEGORY in P.CATEGORIES
    for key, info in P.CATEGORY_INFO.items():
        for field in ("title", "description"):
            assert set(info[field]) == {"en", "de"} and all(text.strip() for text in info[field].values()), key
    used = {}
    for name, preset in PRESETS.items():
        assert preset["category"] in P.CATEGORIES and preset["category"] != P.DEFAULT_CATEGORY, name
        used.setdefault(preset["category"], []).append(name)
    assert set(used) == set(P.CATEGORIES) - {P.DEFAULT_CATEGORY}, set(P.CATEGORIES) - set(used)
    assert set(P.LEGACY_CATEGORIES.values()) <= set(P.CATEGORIES)


def test_the_analysis_presets_give_their_servers_the_samples():
    """MCP review 2026-11-17 (group A): firmware-iot and software-analysis told the agent to analyse /mnt/firmware or
    /mnt/samples with binwalk, capa, radare2, FLOSS and YARA, but gave those servers nothing: each read whatever folder
    `sandbox-ai mcp add` had once given it at /work (`mcp add hub:binwalk`, the note said, without the --mount the
    entry requires), and the agent's paths were not theirs. The start now mounts the preset's folder at the servers'
    /work (read-only), the first task says how the paths map, and binwalk, capa and radare2 are this project's
    reviewed entries (mcp:)."""
    for preset_id, value, servers in (("firmware-iot", "firmware", ["binwalk"]),
                                      ("software-analysis", "samples", ["capa", "floss", "radare2", "yara", "binwalk", "jadx", "cfr"])):
        preset = PRESETS[preset_id]
        mounts = dict(value.split("=", 1) for value, _ in P.option_values(preset, {value: "/x"}, "--mcp-mount"))
        assert mounts == {f"{server}:/work": "/x:ro" for server in servers}, (preset_id, mounts)
        assert all(f"mcp:{name}" in preset["mcp"] for name in ("binwalk", "capa", "radare2") if name in servers)
        assert "as /work" in preset["prompt"], preset_id
        hint = P.missing(preset, {value: str(ROOT)}, "claude", mcp_names=set())
        assert any(f"sandbox-ai mcp add mcp:binwalk --mount /work={ROOT}:ro" in line for line in hint), hint
    assert PRESETS["hardware-analysis"]["mcp"] == ["mcp:binwalk"]


def test_the_android_debugging_preset():
    """android-debugging (user 2026-11-19): the APKs read-only everywhere (the sandbox at /mnt/apks, the servers at
    their /work), one writable output folder the agent and the servers share (/mnt/out, the servers' /out), the
    adb server of this host (--allow-host-port 5037), a device as an optional value, the four MCP servers of
    sandbox-ai-mcp, and a ruleset that asks before anything is installed on a device, wiped, flashed, hooked or
    signed."""
    preset = PRESETS["android-debugging"]
    assert preset["category"] == "security"   # the gallery's "Security analysis" (the old id "analysis" points there)
    assert preset["mcp"] == ["mcp:jadx", "mcp:cfr", "mcp:smali", "mcp:frida"]
    options = list(option_pairs(P.expand(preset, {"apks": "/x", "out": "/y"}, extras=False)))
    assert ("--mount", "/x:/mnt/apks:ro") in options
    assert ("--mount", "/y:/mnt/out:rw") in options
    assert ("--allow-host-port", "5037") in options
    mounts = dict(value.split("=", 1) for value, _ in P.option_values(preset, {"apks": "/x", "out": "/y"},
                                                                      "--mcp-mount"))
    assert mounts == {"jadx:/work": "/x:ro", "cfr:/work": "/x:ro", "smali:/work": "/x:ro", "smali:/out": "/y:rw",
                      "frida:/work": "/x:ro", "frida:/out": "/y:rw"}, mounts
    assert "/mnt/apks/app.apk is /work/app.apk there" in preset["prompt"]
    assert "never instructions" in preset["prompt"]
    # The optional values: a USB device to hand over, and the frida-server's address (the MCP server's endpoint)
    assert preset["values"]["device"]["kind"] == "device" and preset["values"]["device"]["optional"]
    assert preset["values"]["frida_device"]["kind"] == "host" and preset["values"]["frida_device"]["optional"]
    assert preset["values"]["apks"]["kind"] == "folder" and "default" not in preset["values"]["apks"]
    assert preset["values"]["out"]["kind"] == "folder" and "default" not in preset["values"]["out"]
    notes = " ".join(preset["notes"])
    assert "images recipe android --from AGENT" in notes and "--image-variant android" in notes
    assert "adb start-server" in notes and "Podman only" in notes
    assert "--allow-tcp" in notes and "NOT reachable from an MCP server's container" in notes
    assert "FRIDA_WRITE=1" in notes
    assert "throwaway" in notes or "that one call" in notes

    # Its ruleset: installing, wiping, flashing and taking root are asked; the servers' device writes and signing
    # too; everything the device, the app and the decompilers say is untrusted.
    rules = json.loads((ROOT / "sandbox" / "presets" / "android-debugging.rules.json").read_text())
    assert preset["rules"] == "android-debugging.rules.json"
    ids = {rule["id"] for rule in rules["tool"]}
    assert {"adb-install", "adb-uninstall", "adb-wipe", "adb-root-shell", "fastboot-write"} <= ids, ids
    assert all(rule["decision"] == "ask" and rule["reason"] for rule in rules["tool"])
    assert all(rule["tool"] == "Bash" for rule in rules["tool"])
    mcp_rules = {rule["id"]: rule for rule in rules["mcp"]}
    assert set(mcp_rules["frida-device-writes"]["tool"]) == {"spawn", "resume", "kill", "push_server"}
    assert set(mcp_rules["smali-signing"]["tool"]) == {"sign_apk", "apktool_build", "assemble"}
    assert all(rule["decision"] == "ask" and rule["reason"] for rule in rules["mcp"])
    assert {rule["server"] for rule in rules["untrusted"]} == {"frida", "smali", "jadx", "cfr"}
    # Every rule's pattern compiles, and the ones that matter match what they are meant to catch
    for rule in rules["tool"]:
        pattern = rule["args"]["command"]
        for part in (pattern.get("allOf") or [pattern]):
            if "pattern" in part:
                re.compile(part["pattern"])
    install = next(r for r in rules["tool"] if r["id"] == "adb-install")["args"]["command"]["pattern"]
    assert re.fullmatch(install, "adb -s emulator-5554 install /mnt/out/patched.apk")
    assert not re.fullmatch(install, "adb shell pm list packages")
    wipe = next(r for r in rules["tool"] if r["id"] == "adb-wipe")["args"]["command"]["pattern"]
    assert re.fullmatch(wipe, "adb shell pm clear com.example.app")
    assert re.fullmatch(wipe, "adb shell rm /data/local/tmp/x")
    assert re.fullmatch(wipe, "adb shell settings put global x 1")
    assert not re.fullmatch(wipe, "adb shell getprop ro.product.cpu.abi")
    assert not re.fullmatch(wipe, "adb shell ls /data/local/tmp")
    root_shell = next(r for r in rules["tool"] if r["id"] == "adb-root-shell")["args"]["command"]["pattern"]
    assert re.fullmatch(root_shell, "adb shell su 0 id") and not re.fullmatch(root_shell, "adb shell id")
    spawn = next(r for r in rules["tool"] if r["id"] == "frida-cli-spawn")["args"]["command"]["pattern"]
    assert re.fullmatch(spawn, "frida -U -f com.example.app -l hooks.js")
    assert not re.fullmatch(spawn, "frida-ps -U")
    flash = next(r for r in rules["tool"] if r["id"] == "fastboot-write")["args"]["command"]["pattern"]
    assert re.fullmatch(flash, "fastboot flashing unlock")
    assert re.fullmatch(flash, "fastboot flash boot patched.img")
    assert not re.fullmatch(flash, "fastboot devices")


ANDROID_BUILD_HOSTS = ["dl.google.com", "maven.google.com", "services.gradle.org", "downloads.gradle.org",
                       "plugins.gradle.org", "plugins-artifacts.gradle.org", "repo.maven.apache.org", "repo1.maven.org"]


def test_the_android_development_preset():
    """android-development (user 2026-11-21): the image variant android-dev (one image: everything of android and the
    Android SDK, NDK, CMake, emulator, Gradle and Kotlin), exactly the hosts Gradle and the Android Gradle plugin
    fetch from (no wildcard), the host's adb server (port 5037) or a USB device, /dev/kvm for the emulator as a value
    with that default (left out where it is missing, never refused), the MCP servers of android-debugging as
    optional values, a first task with the layout and the commands, and a ruleset that asks before installing on a
    device, wiping or flashing."""
    preset = PRESETS["android-development"]
    assert preset["category"] == "development" and preset["agents"] == ["claude", "codex", "opencode"]
    assert preset["translations"]["de"]["title"] == "Android-Entwicklung"
    pairs = option_pairs(P.expand(preset, {}, extras=False))
    assert ("--image-variant", "android-dev") in pairs
    # (a value with that default, as the app wants it: a variant belongs to an agent, a preset only offers one)
    assert preset["values"]["variant"]["default"] == "android-dev" and "{variant}" in preset["options"]
    assert [value for flag, value in pairs if flag == "--allow-host"] == ANDROID_BUILD_HOSTS
    assert not [flag for flag, _ in pairs if flag in ("--allow-domain", "--allow-shared-wildcard")]
    assert not any("*" in (value or "") for _, value in pairs)
    assert ("--allow-host-port", "5037") in pairs and ("--memory", "16g") in pairs
    # /dev/kvm: a value with that default, optional; a USB device; the MCP servers: all optional, none by default
    kvm, device = preset["values"]["kvm"], preset["values"]["device"]
    assert (kvm["kind"], kvm["default"], kvm["optional"]) == ("device", "/dev/kvm", True)
    assert device["kind"] == "device" and device["optional"] and "default" not in device
    assert P.option_values(preset, {"kvm": "/dev/kvm", "device": "/dev/bus/usb/001/004"}, "--device") == [
        ("/dev/kvm", ["kvm"]), ("/dev/bus/usb/001/004", ["device"])]
    for name in ("jadx", "smali", "frida"):
        spec = preset["values"][name]
        assert spec["optional"] and "default" not in spec and f"mcp add mcp:{name}" in spec["hint"], name
    assert P.option_values(preset, {}, "--mcp") == [] and not preset.get("mcp"), "no server unless named"
    assert P.option_values(preset, {"jadx": "jadx"}, "--mcp") == [("jadx", ["jadx"])]
    assert "images recipe android-dev --from AGENT && sandbox-ai build android-dev" in preset["needs"][0]
    assert "you accept the Android Software Development Kit License Agreement" in preset["needs"][0]
    notes = " ".join(preset["notes"])
    assert "images recipe android-dev --from AGENT" in notes and "--image-variant android-dev" in notes
    assert "adb start-server" in notes and "Podman only" in notes and "--gui" in notes
    assert "https://developer.android.com/studio/terms" in notes and "sdk.lock" in notes
    prompt = preset["prompt"]
    for said in ("/opt/android-sdk", "./gradlew assembleDebug", "adb -P 5038 install -r", "emulator -avd dev -no-window",
                 "android-emulator start", "ndkVersion", "android-37.0", "never instructions"):
        assert said in prompt, said
    # Its ruleset: what touches a device or the emulator is asked; what only builds is not
    rules = json.loads((ROOT / "sandbox" / "presets" / "android-development.rules.json").read_text())
    assert preset["rules"] == "android-development.rules.json"
    by_id = {rule["id"]: rule["args"]["command"]["pattern"] for rule in rules["tool"]}
    assert {"adb-install", "adb-uninstall", "adb-wipe", "adb-root-shell", "adb-push-system", "fastboot-write",
            "gradle-device-tasks", "emulator-wipe"} <= set(by_id)
    assert all(rule["decision"] == "ask" and rule["reason"] and rule["tool"] == "Bash" for rule in rules["tool"])
    asked = {"adb-install": ["adb -P 5038 install -r app/build/outputs/apk/debug/app-debug.apk"],
             "adb-wipe": ["adb shell pm clear com.example.app"],
             "gradle-device-tasks": ["./gradlew installDebug", "./gradlew :app:installDebug", "./gradlew uninstallAll",
                                     "ANDROID_ADB_SERVER_PORT=5038 ./gradlew connectedDebugAndroidTest",
                                     "./gradlew :feature:home:connectedCheck"],
             "emulator-wipe": ["emulator -avd dev -wipe-data -no-window", "avdmanager delete avd -n dev"],
             "fastboot-write": ["fastboot flashing unlock"]}
    for rule_id, commands in asked.items():
        for command in commands:
            assert re.fullmatch(by_id[rule_id], command), (rule_id, command)
    for command in ("./gradlew assembleDebug", "./gradlew testDebugUnitTest", "./gradlew lint", "adb devices",
                    "android-emulator start", "emulator -avd dev -no-window -no-audio -no-metrics",
                    "adb -P 5038 logcat -d"):
        assert not any(re.fullmatch(pattern, command) for pattern in by_id.values()), command
    assert {rule["server"] for rule in rules["untrusted"]} == {"frida", "smali", "jadx", "cfr"}


def test_the_android_development_preset_without_kvm(host, capsys, monkeypatch):
    """A host without /dev/kvm: the value's default is left out with a note (never refused), the start goes on
    without the device; one named explicitly is passed; --preset-value kvm= leaves it out too."""
    root, dev, work = host
    preset = PRESETS["android-development"]
    real = os.path.lexists
    monkeypatch.setattr(P.os.path, "lexists", lambda path: False if path == "/dev/kvm" else real(path))
    notes = []
    values, found = P.checked_values(preset, {}, notes)
    assert found == [] and "kvm" not in values
    assert any("kvm: /dev/kvm does not exist here, left out" in note for note in notes), notes
    assert "--device" not in P.expand(preset, {}, extras=False)
    assert P.missing(preset, {}, "claude") == []
    plan = dry_run(capsys, work, "claude", "android-development", {})
    assert plan["devices"] == [] and plan["image"] == "localhost/claude-sandbox-android-dev:latest"
    plan = dry_run(capsys, work, "codex", "android-development", {"kvm": str(dev / "kvm")})
    assert [entry["path"] for entry in plan["devices"]] == [str(dev / "kvm")]
    plan = dry_run(capsys, work, "opencode", "android-development", {"kvm": ""})
    assert plan["devices"] == []


def test_presets_check_names_the_recipe_of_a_missing_image_variant(host, tmp_path, monkeypatch):
    """`presets check` for a preset that starts an image variant not made yet (android-development without
    android-dev): it names the recipe and the build, for the agent asked about (AGENT without one); a variant of
    another agent is enough (the start takes it over)."""
    preset = PRESETS["android-development"]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    found = P.missing(preset, {"kvm": ""}, "codex")
    assert found == ["the image variant android-dev is not made yet: sandbox-ai images recipe android-dev --from "
                     "codex && sandbox-ai build android-dev"], found
    assert "--from AGENT" in P.missing(preset, {"kvm": ""})[0]
    spec = importlib.util.spec_from_file_location("images_for_check", ROOT / "sandbox/images.py")
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    images.recipe("android-dev", "claude")
    assert P.missing(preset, {"kvm": ""}, "codex") == []


def test_the_mcp_servers_the_presets_name_are_in_the_catalogs():
    """Every catalog server a preset names (hub:capa, mcp:engram; one by default in an --mcp option) is an entry of
    that catalog: `sandbox-ai mcp add` the note names finds it. Checked against the catalogs themselves where this
    checkout has them (projects/sandbox-ai-mcp, projects/axross-mcp-hub)."""
    for prefix, folder in (("mcp", "sandbox-ai-mcp"), ("hub", "axross-mcp-hub")):
        path = ROOT / "projects" / folder / "catalog.json"
        if path.is_file():
            have = {entry["name"] for entry in json.loads(path.read_text())["entries"]}
            # A checkout of the catalog that is older than a preset here: only the servers whose branch is not
            # merged yet may be missing, and they are named (smali and frida: sandbox-ai-mcp feat/android).
            assert CATALOG_ENTRIES[prefix] - have <= {"smali", "frida"}, CATALOG_ENTRIES[prefix] - have
    named = []
    for name, preset in PRESETS.items():
        for server in preset.get("mcp", []):
            prefix, _, entry = server.partition(":")
            assert prefix in CATALOG_ENTRIES and entry in CATALOG_ENTRIES[prefix], (name, server)
            named.append(entry)
        defaults = {key: str(spec["default"]) for key, spec in preset.get("values", {}).items() if "default" in spec}
        for value, _ in P.option_values(preset, defaults, "--mcp"):
            assert value in CATALOG_ENTRIES["mcp"] | CATALOG_ENTRIES["hub"], (name, value)
        for value, _ in P.option_values(preset, {key: "/x" for key in preset.get("values", {})}, "--mcp-mount"):
            server, _, target = value.partition(":")
            assert server in CATALOG_FOLDERS and target.split("=", 1)[0] in CATALOG_FOLDERS[server], (name, value)
            assert any(entry.endswith(":" + server) for entry in preset.get("mcp", [])), (name, server)
    assert {"engram", "memory", "duckduckgo", "web-fetch"} <= set(named)


def test_every_ruleset_beside_a_preset_is_valid():
    """A preset's "rules" file is a ruleset the launcher takes (RUNTIME.Ruleset.validated, as --rules reads it), and
    every ruleset file in the folder belongs to a preset."""
    folder = ROOT / "sandbox" / "presets"
    named = {preset["rules"] for preset in PRESETS.values() if preset.get("rules")}
    assert named == {path.name for path in folder.glob("*.rules.json")}
    for name in named:
        checked = s.RUNTIME.Ruleset.validated(json.loads((folder / name).read_text()), name)
        assert checked["untrusted"] or checked["mcp"] or checked["http"], name


@pytest.mark.parametrize("name,server", [("coding-memory", "engram"), ("research-memory", "memory")])
def test_a_memory_preset_asks_for_its_folder_and_gives_it_to_its_server(host, capsys, name, server, tmp_path):
    """The memory presets: the brain folder is a value of kind folder without a default (asked for, never guessed);
    a folder that is not there yet is fine (the start makes it, a dry run makes nothing); one inside the workspace is
    refused; `presets check` names the exact `mcp add` with the folder for a server not added yet; the agent works
    without questions (-D) on its workspace only."""
    _, _, work = host
    preset = PRESETS[name]
    folder_value = next(key for key, spec in preset["values"].items() if spec.get("kind") == "folder")
    assert "default" not in preset["values"][folder_value] and not preset["values"][folder_value].get("optional")
    assert any(f"--preset-value {folder_value}=" in line for line in P.missing(preset, {}, "claude"))
    values = {key: "the question" for key in preset["values"] if key != folder_value}
    brain = tmp_path / "brains" / "project"
    notes = []
    assert P.missing(preset, {**values, folder_value: str(brain)}, "codex", notes=notes) == []
    assert any("the start creates it for its MCP server" in note for note in notes), notes
    plan = dry_run(capsys, work, "claude", name, {**values, folder_value: str(brain)})
    entry = next(e for e in plan["mcp"] if e["name"] == server)
    assert entry["own_mounts"] == [{"target": "/data", "host": str(brain), "mode": "rw"}] and not brain.exists()
    assert plan["dangerously_skip_permissions"] and plan["workspace_mode"] == "rw"
    # Inside the workspace: given (you decide), with the warning that the agent can change the memory there.
    said = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(s, "warner", lambda warn=None: warn or said.append)
        dry_run(capsys, work, "claude", name, {**values, folder_value: str(work / ".brain")})
    assert any("which the agent can write" in text for text in said), said
    found = P.missing(preset, {**values, folder_value: "/srv/brains/app"}, "codex", mcp_names=set())
    assert f"sandbox-ai mcp add mcp:{server} --mount /data=/srv/brains/app:rw" in "\n".join(found)


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


# --- the showcase presets (2026-11-12: "3-6 more showcase presets and the Containerfiles they need") -------------------

SHOWCASES = {"showcase", "showcase-full-stack", "showcase-vpn", "showcase-local-llm", "showcase-second-opinion"}


def test_the_showcase_presets_each_show_something_else_and_write_a_report():
    """Every showcase is a demo (category fun) for every agent, in English and German, with a first task that checks
    what the start gives and writes a short report into the workspace, and notes that say what to add once."""
    assert SHOWCASES <= set(PRESETS)
    reports = set()
    for name in sorted(SHOWCASES):
        preset = PRESETS[name]
        assert preset["category"] == "fun" and tuple(preset["agents"]) == AGENTS, name
        german = preset["translations"]["de"]
        assert german["title"].startswith("Schaufenster:") and preset["title"].startswith("Showcase:"), name
        assert german["description"] and german["description"] != preset["description"], name
        report = re.search(r"report (?:to|in) (/work/[a-z-]+\.md)", preset["prompt"])
        assert report, name
        reports.add(report.group(1))
        assert preset["notes"], name
    assert len(reports) == len(SHOWCASES), "a report file of its own each"
    # each shows another capability
    options = {name: P.expand(PRESETS[name], sample_values(PRESETS[name], Path("/x"), Path("/dev"), "codex", True),
                              extras=False) for name in SHOWCASES - {"showcase"}}
    assert options["showcase-full-stack"].count("--service") == 2
    assert "--vpn" in options["showcase-vpn"] and "--vpn-check" in options["showcase-vpn"]
    assert "--llm" in options["showcase-local-llm"] and "loop_guard=on" in options["showcase-local-llm"]
    assert "--helper" in options["showcase-second-opinion"]


def test_the_full_stack_showcase_names_what_it_needs_pinned(host, capsys):
    """showcase-full-stack: PostgreSQL and nginx as services (Podman), development hosts open, the development image
    from the recipe django; the images it tells you to add are exact releases (pinned by digest when added), the web
    server is sandbox-ai's own Containerfile, the recipe is sandbox-ai's; and since neither MCP catalog has a reviewed
    PostgreSQL or design server, it names none and says so."""
    _, _, work = host
    preset = PRESETS["showcase-full-stack"]
    plan = dry_run(capsys, work, "codex", "showcase-full-stack", {})
    services = {service["name"]: service for service in plan["services"]}
    assert set(services) == {"db", "web"}
    assert services["db"]["reachable_at"] == ["localhost:5432"] and services["db"]["user"] == "999:999"
    assert services["web"]["reachable_at"] == ["localhost:8080"] and services["web"]["user"] == "101:101"
    assert all(service["read_only"] and service["unchecked"] for service in services.values())
    text = "\n".join(preset["notes"] + [spec["hint"] for spec in preset["values"].values()])
    pulled = re.findall(r"images pull (\S+) --as postgres", text)
    assert pulled and all(re.fullmatch(r"docker\.io/library/postgres:\d+\.\d+-bookworm", ref) for ref in pulled)
    assert "images build-image showcase-web --builtin showcase-web" in text
    # (2026-11-19: its default is the template's own name - the app's start builds it when it is not built yet)
    assert preset["values"]["web"]["default"] == "showcase-web"
    assert "showcase-web" in s.load_services_module().builtin_images()
    assert "images recipe django --from AGENT" in text and "--image-variant django" in text
    assert "django" in s.load_services_module().images_module().RECIPES
    assert "mcp" not in preset and "--mcp" not in preset["options"]
    assert "has a reviewed PostgreSQL or design MCP server yet, so this preset names none" in " ".join(preset["notes"])
    for folder in ("sandbox-ai-mcp", "axross-mcp-hub"):
        path = ROOT / "projects" / folder / "catalog.json"
        if path.is_file():  # still none there: the note holds
            entries = json.loads(path.read_text())["entries"]
            assert not [e["name"] for e in entries if re.search(r"postgres|figma|design", e["name"], re.I)], folder


def test_the_vpn_showcase_takes_any_kind_of_profile(host, capsys):
    """showcase-vpn: a WireGuard, a Tailscale and a Tor profile alike, the check stopping the run, the tunnel's
    resolver, one echo host besides the provider."""
    _, _, work = host
    for profile, kind in (("home", "wireguard"), ("ts", "tailscale"), ("anon", "tor")):
        plan = dry_run(capsys, work, "codex", "showcase-vpn", {"vpn": profile})
        assert plan["vpn"]["profile"] == profile and plan["vpn"]["type"] == kind
        assert plan["vpn"]["checks"]["on_failure"] == "stop"
        assert "api.ipify.org" in plan["https_hosts"]


def test_the_local_llm_showcase_sets_the_sampling_and_a_strict_loop_guard(host, capsys):
    _, _, work = host
    plan = dry_run(capsys, work, "codex", "showcase-local-llm", {"llm": "ollama", "temperature": "0.7"})
    params = plan["llm"]["params"]
    assert params["values"] == {"temperature": 0.7, "max_tokens": 8192}
    assert set(params["forced"]) == {"temperature", "max_tokens"}
    assert params["loop_guard"]["enabled"] is True
    assert (params["loop_guard"]["warn"], params["loop_guard"]["block"]) == (2, 4)
    assert plan["https_hosts"] == [], "nothing but the server"


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("helper", AGENTS)
def test_the_second_opinion_showcase_gives_any_agent_any_helper(host, capsys, agent, helper):
    _, _, work = host
    plan = dry_run(capsys, work, agent, "showcase-second-opinion", {"helper": helper})
    assert [entry["name"] for entry in plan["mcp"]] == [s.helper_name(helper, plan["workspaces"][0]["host"])]
    assert_network(plan, network_of(PRESETS["showcase-second-opinion"], {"helper": helper}, agent))


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
    host's loopback (--allow-host-port, an LLM server on 127.0.0.1), a VPN or a service (--service), which only Podman
    gives, a refusal that names the Podman backend; its notes say so ("Podman only")."""
    root, dev, work = host
    preset = PRESETS[name]
    if agent not in preset.get("agents", AGENTS):
        return
    values = sample_values(preset, root, dev, agent, True)
    options = P.expand(preset, values, extras=False)
    if "--allow-host-port" in options or "--vpn" in options or "--service" in options or loopback_llm(values):
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


def test_the_axross_mcp_server_preset():
    """axross-mcp-server (user 2026-11-20): the MCP server axross on a folder of yours (its /work; the agent sees it
    read-only at /mnt/files), read-only unless the values say rw and write=1, axross's saved connections and the ssh
    folder read-only and optional, the host of a connection as a value for the server's network (the preset cannot
    open it: sandbox-ai mcp set does), and a ruleset that lets the reads run and asks before every write, delete,
    rename, copy, mode change and link."""
    preset = PRESETS["axross-mcp-server"]
    assert preset["category"] == "development" and preset["mcp"] == ["mcp:axross"]
    assert set(preset["translations"]["de"]) == {"title", "description"}
    values = {"files": "/x", "connections": "/c", "ssh": "/s"}
    options = list(option_pairs(P.expand(preset, values, extras=False)))   # the defaults filled in: ro, write=0
    assert ("--mount", "/x:/mnt/files:ro") in options
    assert ("--mcp-mount", "axross:/work=/x:ro") in options and ("--mcp-env", "axross:AXROSS_WRITE=0") in options
    values.update(files_mode="ro", write="0")
    assert [flag for flag, _ in options if flag == "--mount"] == ["--mount"]   # nothing else of yours in the sandbox
    mounts = dict(value.split("=", 1) for value, _ in P.option_values(preset, values, "--mcp-mount"))
    assert mounts == {"axross:/work": "/x:ro", "axross:/axross": "/c:ro", "axross:/ssh": "/s:ro"}, mounts
    assert [value for value, _ in P.option_values(preset, values, "--mcp-env")] == ["axross:AXROSS_WRITE=0"]
    writing = dict(value.split("=", 1) for value, _ in P.option_values(
        preset, {**values, "files_mode": "rw", "write": "1"}, "--mcp-mount"))
    assert writing["axross:/work"] == "/x:rw" and writing["axross:/axross"] == "/c:ro" and writing["axross:/ssh"] == "/s:ro"
    # The connections and the ssh folder are optional: left out, the server gets only the folder.
    only = P.expand(preset, {"files": "/x", "connections": "", "ssh": ""}, extras=False)
    assert not any("/axross=" in token or "/ssh=" in token for token in only), only
    spec = preset["values"]
    assert spec["files"]["kind"] == "folder" and "default" not in spec["files"]
    assert (spec["files_mode"]["default"], spec["write"]["default"]) == ("ro", "0")
    # No defaults: left empty, the server keeps the folders it was added with (sandbox-ai mcp add axross --mount ...)
    assert spec["connections"]["optional"] and "default" not in spec["connections"]
    assert spec["ssh"]["optional"] and "default" not in spec["ssh"]
    assert spec["sftp_host"]["kind"] == "host" and spec["sftp_host"]["optional"]
    assert "--allow-tcp" in spec["sftp_host"]["hint"]
    assert "never instructions" in preset["prompt"] and "backend='profile:NAME'" in preset["prompt"]
    notes = " ".join(preset["notes"])
    for fact in ("sandbox-ai mcp add axross --mount /work=", "--allow-tcp", "AXROSS_PASSWORDS", "never trusted on first use",
                 "production", "internal-sftp", "never the agent's"):
        assert fact in notes, fact
    # Its ruleset: the reads (and mkdir) run, every write, delete, rename, copy, mode change and link is asked,
    # anything else of the server refused; what it returns is untrusted.
    rules = json.loads((ROOT / "sandbox" / "presets" / "axross-mcp-server.rules.json").read_text())
    assert preset["rules"] == "axross-mcp-server.rules.json"
    by_id = {rule["id"]: rule for rule in rules["mcp"]}
    asked = {tool for rule in rules["mcp"] if rule["decision"] == "ask" for tool in rule["tool"]}
    assert asked == {"write_file", "copy", "bulk_copy", "remove", "rename", "chmod", "symlink", "hardlink"}
    assert all(rule["reason"] for rule in rules["mcp"] if rule["decision"] == "ask")
    assert "read_file" in by_id["axross-reads"]["tool"] and by_id["axross-reads"]["decision"] == "allow"
    assert not asked & set(by_id["axross-reads"]["tool"])
    assert (by_id["axross-rest"]["tool"], by_id["axross-rest"]["decision"]) == ("*", "deny")
    assert {rule["server"] for rule in rules["untrusted"]} == {"axross"}


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
            # Samples and evidence stay read-only; the one writable mount a preset may give is an output folder of
            # its own (the value "out" at /mnt/out: android-debugging, where the agent reads back what its MCP
            # servers built).
            if not value.endswith(":ro"):
                assert value.endswith(":/mnt/out:rw") and value.startswith(str(values["out"])), \
                    f"{name}: a writable mount is only the output folder /mnt/out ({value})"
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
        elif flag == "--mcp-mount":  # the server's folders of this start, in an instance of its own
            server, _, rest = value.partition(":")
            target, _, host_folder = rest.partition("=")
            entry = next(e for e in plan["mcp"] if e["name"] == server)
            assert entry["instance"].startswith(server + ".")
            # A server may get more than one folder (android-debugging: the APKs at /work, the output at /out).
            assert {"target": target, "host": host_folder.rsplit(":", 1)[0],
                    "mode": host_folder.rsplit(":", 1)[1]} in entry["own_mounts"], entry["own_mounts"]
        elif flag == "--dangerously-skip-permissions":
            assert plan["dangerously_skip_permissions"] is True
        elif flag == "--service":
            assert value.split(":", 1)[0] in [service["name"] for service in plan["services"]], plan["services"]
    # A preset that turns the agent's questions off keeps it to its workspace: no folder of this computer but the
    # workspace is writable (no writable --mount), and the workspace itself is not read-only by surprise.
    if "--dangerously-skip-permissions" in P.expand(preset, values, extras=False):
        assert not [m for m in plan["mounts"] if not str(m).endswith(":ro")], plan["mounts"]
        assert plan["workspace_mode"] == "rw"
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


# --- the agents' own presets: "Coding with AGENT" and "AGENT with a local model" for every harness ------------------

def test_every_agent_has_its_coding_and_its_local_model_preset():
    """Each harness of the launcher (claude, codex, opencode, opencode2, hermes, dsh, qwen, goose, openhands, nanobot, letta, pi) has both, for it alone; the
    local one opens only the server (--no-provider-defaults) with the loop guard on. Hermes' local one is the older
    hermes-local-llm (category llm), not a second copy."""
    assert set(s.AUTH_TARGETS) == {"claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"}
    for agent in s.AUTH_TARGETS:
        coding, local = PRESETS[f"coding-{agent}"], PRESETS[f"{agent}-local-llm"]
        assert coding["agents"] == [agent] and local["agents"] == [agent], agent
        assert coding["category"] == "agents" and local["category"] == ("llm" if agent == "hermes" else "agents")
        assert {"--allow-pypi", "--allow-npm", "--allow-github"} <= set(coding["options"])
        assert "--no-memory" not in coding["options"] and "--no-provider-defaults" not in coding["options"]
        assert {"--llm", "--no-provider-defaults", "loop_guard=on"} <= set(local["options"])
        assert local["values"]["llm"]["kind"] == "llm"
        for preset in (coding, local):
            assert preset["translations"]["de"]["title"] and preset["translations"]["de"]["description"]
    assert P.CATEGORY_INFO["agents"]["title"] == {"en": "Agents", "de": "Agenten"}


@pytest.mark.parametrize("agent", ["opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"])
def test_the_presets_of_the_experimental_agents(host, capsys, monkeypatch, agent):
    """The ones test_every_preset_with_every_agent leaves out (its agents are claude, codex and opencode): presets
    check, a dry run (no opt-in: they are only not live-tested yet), and only the network the preset names."""
    root, dev, work = host
    for name in sorted(n for n, preset in PRESETS.items() if preset.get("agents") == [agent]):
        preset = PRESETS[name]
        for full in (False, True):
            values = sample_values(preset, root, dev, agent, full)
            assert P.missing(preset, values, agent) == [], name
            plan = dry_run(capsys, work, agent, name, values)
            assert plan["agent"] == agent and plan[agent]["experimental"], name
            assert_network(plan, network_of(preset, values, agent))
            if values.get("llm") or "--llm" in preset["options"]:
                # Read-only: a managed file (/etc/...) or, for Goose, variables that win over its own files.
                assert plan["https_hosts"] == [] and plan["llm"]["config"].startswith(("/etc/", "environment")), name
        for other in ("claude", "codex"):
            with pytest.raises(ValueError, match=f"preset {name} is for"):
                dry_run(capsys, work, other, name, sample_values(preset, root, dev, agent, False))
