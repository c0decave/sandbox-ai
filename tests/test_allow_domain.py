"""--allow-domain DOMAIN: exactly --allow-host DOMAIN --allow-host '*.DOMAIN', expanded at one place (the launcher's
expand_domains), with the checks --allow-host '*.DOMAIN' has (public suffixes and hosting services refused), at the
start, in a project file, a preset, the security modes, the administrator's policy and `sandbox-ai net allow`
(--keep included). The fixture `m` is the feature matrix's (every agent on both backends)."""
import json
from pathlib import Path

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)
import test_net as tn
from test_net import no_login_runtime, sandbox  # noqa: F401  (fixtures)

s = fm.s
net = tn.net


def parsed(*options):
    return s.parse_args(["claude", "--no-config", *options])


# --- the expansion ----------------------------------------------------------------------------------------------

def test_a_domain_is_its_name_and_the_wildcard_below_it():
    a = parsed("--allow-domain", "example.org")
    b = parsed("--allow-host", "example.org", "--allow-host", "*.example.org")
    assert a.allow_host == b.allow_host == ["example.org", "*.example.org"]
    assert s.allowed_hosts(a) == s.allowed_hosts(b) and "example.org" in s.allowed_hosts(a)
    assert s.host_wildcards(a) == s.host_wildcards(b) == ["*.example.org"]
    assert a.allow_domain == ["example.org"]  # as given, for messages; the hosts carry what it means
    # The short form, repeated, and next to what --allow-host names already: each entry once.
    a = parsed("-ad", "example.org", "-A", "example.org", "-ad", "example.com", "--allow-domain", "example.org")
    assert a.allow_host == ["example.org", "*.example.org", "example.com", "*.example.com"]


@pytest.mark.parametrize("value", ["Example.ORG", "*.example.org", ".example.org", "example.org.", "*.EXAMPLE.org."])
def test_spellings_of_one_domain(value):
    assert s.domain_of(value) == "example.org"
    assert parsed("--allow-domain", value).allow_host == ["example.org", "*.example.org"]


@pytest.mark.parametrize("value, reason", [
    ("co.uk", "refused: it reaches co.uk, where anyone can have a name of their own"),
    ("github.io", "refused: it reaches github.io, where anyone can have a name"),
    ("someone.github.io", "refused: it reaches github.io"),
    ("com", "a whole top-level domain"),
    ("*.com", "a whole top-level domain"),
    ("bücher.de", "xn-- form"),
    ("exa mple.org", "not a domain name"),
    ("a..example.org", "not a domain name"),
    ("*.*.example.org", "not a domain name"),
    ("", "not a domain name"),
    ("*.", "not a domain name"),
    ("x" * 64 + ".example.org", "not a domain name"),
])
def test_refusals_name_the_option(value, reason):
    with pytest.raises(ValueError, match="^--allow-domain .*" + reason.replace("(", r"\(").replace(".", r"\.")):
        s.domain_of(value)
    a = parsed("--allow-domain", value)
    assert a.allow_host == []  # nothing expanded: validate() says why
    with pytest.raises(ValueError, match=r"^--allow-domain .*" + reason.replace(".", r"\.")):
        s.validate(a)


def test_a_domain_below_a_country_suffix_is_one_domain():
    assert s.domain_of("example.co.uk") == "example.co.uk"
    s.validate(parsed("--allow-domain", "example.co.uk"))


def test_the_wildcards_conditions_name_the_domain():
    with pytest.raises(ValueError, match=r"^--allow-domain example\.org needs the sandbox's questions"):
        s.validate(parsed("--allow-domain", "example.org", "--ask", "off"))
    with pytest.raises(ValueError, match=r"^--allow-domain example\.org with --dns or --nameserver"):
        s.validate(parsed("--allow-domain", "example.org", "--dns"))
    # As with --allow-host '*.DOMAIN': every destination is open anyway.
    s.validate(parsed("--allow-domain", "example.org", "--ask", "off", "--egress-all-allow"))
    with pytest.raises(ValueError, match="--offline conflicts"):
        s.validate(parsed("--allow-domain", "example.org", "--offline"))
    with pytest.raises(ValueError, match="--offline conflicts"):
        s.validate(parsed("--allow-domain", "co.uk", "--offline"))


def test_help_completion_and_groups():
    text = s.build_parser().format_help()
    assert "-ad, --allow-domain DOMAIN" in text
    assert "--allow-domain" in dict(s.HELP_GROUPS)["Network"] and s.SHORT_OPTIONS["--allow-domain"] == "-ad"
    assert s.parse_args(["claude", "-ad", "example.org"]).allow_domain == ["example.org"]
    assert s.parse_args(["claude", "-a", "10.0.0.0/8"]).allow == ["10.0.0.0/8"]  # -ad is no -a d
    import importlib.util
    spec = importlib.util.spec_from_file_location("completion_for_domains", fm.ROOT / "sandbox" / "completion.py")
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    assert "--allow-domain" in c.complete(["claude", "--allow-d"])
    assert "--allow-domain" in c.complete(["net", "allow", "--allow-d"])


# --- a start: the plan, the project file, a preset, the modes, the policy ----------------------------------------

def test_the_plan_is_that_of_the_two_host_options(m):
    domain = m.plan("--allow-domain", "Example.org")
    hosts = m.plan("--allow-host", "example.org", "--allow-host", "*.example.org")
    for key in ("https_hosts", "host_wildcards", "https_networks"):
        assert domain[key] == hosts[key], key
    assert domain["host_wildcards"] == ["*.example.org"] and "example.org" in domain["https_hosts"]
    m.refused("--allow-domain", "github.io", match="--allow-domain github.io: refused")
    m.refused("--allow-domain", "example.org", "--ask", "off", match="--allow-domain example.org needs")


def test_a_project_file_and_a_preset_take_it(m):
    m.project_file({"options": ["--allow-domain", "example.org"]})
    plan = m.plan()
    assert plan["host_wildcards"] == ["*.example.org"] and "example.org" in plan["https_hosts"]
    m.project_file({"options": ["--allow-domain=co.uk"]})
    m.refused(match="--allow-domain co.uk: refused")
    m.project_file({"options": []})
    presets = Path.home() / ".config" / "sandbox-ai" / "presets"
    presets.mkdir(parents=True)
    (presets / "docs.json").write_text(json.dumps({
        "format": "sandbox-ai preset", "version": 1, "id": "docs", "title": "Docs", "description": "a domain",
        "options": ["--allow-domain", "{domain}"], "values": {"domain": {"label": "Domain", "kind": "host"}}}))
    assert s.load_presets_module().problems(json.loads((presets / "docs.json").read_text()), "docs.json") == []
    plan = m.plan("--preset", "docs", "--preset-value", "domain=example.com")
    assert plan["host_wildcards"] == ["*.example.com"] and "example.com" in plan["https_hosts"]
    m.refused("--preset", "docs", "--preset-value", "domain=github.io", match="--allow-domain github.io: refused")


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_the_strict_modes_refuse_it_by_its_name(m, mode):
    m.refused("--mode", mode, "--allow-domain", "example.org",
              match=rf"^--allow-domain example\.org widens the security mode {mode} \(--mode\): .*--mode medium")
    m.project_file({"mode": mode, "options": ["-ad", "example.org"]})
    m.refused(match=rf"--allow-domain example\.org widens the security mode {mode}")


def test_the_laxer_modes_take_it(m):
    for mode in ("lax", "low", "medium"):
        plan = m.plan("--mode", mode, "--allow-domain", "example.org")
        assert plan["mode"]["name"] == mode
    assert m.plan("--mode", "low", "--allow-domain", "example.org")["host_wildcards"] == ["*.example.org"]
    record = m.run("--mode", "high").run_record["mode"]
    assert "--allow-domain" in record["refuses"] and "--allow-host" in record["refuses"]


def test_the_policy_binds_it_like_the_hosts_it_stands_for(m):
    m.policy(forbid=["--allow-host"])
    m.refused("--allow-domain", "example.org", match="--allow-host is not allowed by the administrator's policy")
    m.policy(forbid=["--allow-domain"])
    m.refused("--allow-domain", "example.org", match="--allow-domain is not allowed by the administrator's policy")
    assert "example.org" in m.plan("--allow-host", "example.org")["https_hosts"]  # --allow-host stays allowed
    m.policy(allow_within=["10.0.0.0/8"], allow_hosts=["example.org"])
    m.refused("--allow-domain", "example.org", match=r"\*\.example\.org is not among the hosts")
    m.policy(allow_within=["10.0.0.0/8"], allow_hosts=["example.org", "*.example.org"])
    assert m.plan("--allow-domain", "example.org")["host_wildcards"] == ["*.example.org"]


# --- during the run: sandbox-ai net allow / revoke ---------------------------------------------------------------

def test_net_allow_opens_the_name_and_the_names_below_it(sandbox):  # noqa: F811
    record = tn.wildcard_sandbox(sandbox)
    status, lines = tn.run_net(["allow", "--allow-domain", "Example.org"])
    assert status == 0, lines
    assert "192.0.2.50 example.org" in sandbox["hosts"].read_text()
    assert net.run_wildcards(record) == ["*.example.org"]
    assert lines[-1].endswith("start with: --allow-domain example.org (or add --keep: into the project's "
                              ".sandbox-ai.json)"), lines[-1]
    status, lines = tn.run_net(["show", "--json"])
    shown = json.loads("\n".join(lines))
    # What it expanded to: the name under HTTPS, the wildcard among the wildcards.
    assert shown["host_wildcards"] == ["*.example.org"]
    assert shown["https"] == [{"address": "192.0.2.50", "names": ["example.org"]}]
    status, lines = tn.run_net(["revoke", "-ad", "example.org"])
    assert status == 0, lines
    assert net.run_wildcards(record) == [] and "example.org" not in sandbox["hosts"].read_text()


def test_net_allow_refuses_what_the_start_refuses(sandbox, capsys):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    for value, reason in (("github.io", "refused: it reaches github.io"), ("co.uk", "refused: it reaches co.uk"),
                          ("com", "a whole top-level domain"), ("bücher.de", "a name outside ASCII is given in its xn-- form")):
        assert tn.run_net(["allow", "--allow-domain", value])[0] == 1
        assert f"--allow-domain {value}: {reason}" in capsys.readouterr().err
    assert sandbox["applied"] == []
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (Path(record["session_dir"]) / "resolv.conf").write_text("nameserver 1.1.1.1\n")
    assert tn.run_net(["allow", "--allow-domain", "example.org"])[0] == 1
    assert "past its DNS stub" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_net_allow_keeps_to_the_mode_of_the_run(sandbox, capsys, mode):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "mode": tn.mode_record(mode)}))
    assert tn.run_net(["allow", "--allow-domain", "example.org"])[0] == 1
    assert f"--allow-domain widens the security mode {mode} of codex-1234abcd" in capsys.readouterr().err
    # A run whose record predates --allow-domain: the --allow-host it stands for is refused.
    older = {**tn.mode_record(mode)}
    older["refuses"] = [o for o in older["refuses"] if o != "--allow-domain"]
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "mode": older}))
    assert tn.run_net(["allow", "--allow-domain", "example.org"])[0] == 1
    assert f"--allow-host widens the security mode {mode}" in capsys.readouterr().err
    assert sandbox["applied"] == [] and "example.org" not in sandbox["hosts"].read_text()


def test_net_allow_keep_writes_the_domain_as_given(sandbox, tmp_path, monkeypatch):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tn.project_file(tmp_path)
    status, lines = tn.run_net(["allow", "--allow-domain", "*.Example.org", "--allow-host", "api.osv.dev", "--keep"])
    assert status == 0 and "kept in" in lines[-1], lines
    assert json.loads(path.read_text())["options"] == ["--allow-host", "api.osv.dev", "--allow-domain", "example.org"]
    status, lines = tn.run_net(["allow", "--allow-domain", "example.org", "--keep"])
    assert status == 0 and "already has it" in lines[-1], lines


def test_kept_options_and_the_parser():
    args = net.parser().parse_args(["allow", "r", "-ad", "example.org", "-A", "x.example", "-A", "example.org"])
    args.domain_hosts = net.launcher().expand_domains(args)
    assert args.allow_host == ["x.example", "example.org", "*.example.org"] and args.domain_hosts == ["*.example.org"]
    # example.org was named by --allow-host as well: kept as both, the domain once.
    assert net.kept_options(args, []) == ["--allow-host", "x.example", "--allow-host", "example.org",
                                          "--allow-domain", "example.org"]
    request, names, hosts = net.request_for(args, "allow", resolver=tn.fake_resolver)
    assert hosts == ["x.example", "example.org"] and request["wildcards"] == ["*.example.org"]
    args = net.parser().parse_args(["allow", "r", "-ad", "github.io"])
    args.domain_hosts = net.launcher().expand_domains(args)
    assert args.allow_host == []
    with pytest.raises(ValueError, match="--allow-domain github.io: refused"):
        net.request_for(args, "allow", resolver=tn.fake_resolver)


def test_a_project_file_with_allow_domain_is_marked_before_it_is_trusted():
    """Bug hunt: the trust question marks every option of a project file that widens what the sandbox reaches
    (modes.WEAKENING_OPTIONS) - --allow-host, but not --allow-domain, which opens a domain and every name below it;
    nor --mcp-limits, an MCP server's limits raised as --memory raises the agent's."""
    found = s.MODES.weakening_options(s, ["--allow-domain", "evil.example", "-ad", "x.example",
                                          "--mcp-limits", "demo:memory=8g"])
    assert found == [("--allow-domain", "evil.example"), ("--allow-domain", "x.example"),
                     ("--mcp-limits", "demo:memory=8g")]
