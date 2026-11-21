"""Wildcards below a shared service (*.blob.core.windows.net: anyone can rent a name there): no longer always
refused, but allowed only after an explicit confirmation that names the risk (user 2026-11-20: the human decides,
warned, never silently refused). A top-level domain (*.com) and a country's public suffix (*.co.uk, every ICANN-section
suffix) stay refused always; the strict modes and the administrator's policy refuse a shared one too.

Through runtime.py (the classification, the supervisor's control op), the launcher (--allow-shared-wildcard, the
terminal question, --yes, the plan, run.json and config.json), `sandbox-ai net allow` and `sandbox-ai report`."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_runtime_shared", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)

RISK = "could open any server anyone rents below blob.core.windows.net, and send data there"


def psl(tmp_path, private="", icann="", pad=1000):
    filler = "\n".join(f"filler{n}.test" for n in range(pad))
    path = tmp_path / "list.dat"
    path.write_text(f"// header\n// ===BEGIN ICANN DOMAINS===\ncom\nuk\nco.uk\n*.ck\n!www.ck\n{icann}\n{filler}\n"
                    f"// ===END ICANN DOMAINS===\n\n// ===BEGIN PRIVATE DOMAINS===\n{private}\n"
                    "// ===END PRIVATE DOMAINS===\n", encoding="utf-8")
    return path


# --- the classification (runtime.py) -------------------------------------------------------------------------------

def test_a_shared_service_is_named_and_an_own_domain_is_none():
    assert rt.shared_wildcard("*.blob.core.windows.net") == "blob.core.windows.net"
    assert rt.shared_wildcard("*.Blob.Core.Windows.NET") == "blob.core.windows.net"
    assert rt.shared_wildcard("*.github.io") == "github.io"
    assert rt.shared_wildcard("*.mine.github.io") == "github.io"  # SHARED_SUFFIXES: at any depth below
    assert rt.shared_wildcard("*.amazonaws.com") == "amazonaws.com"
    assert rt.shared_wildcard("*.example.com") is None
    assert rt.shared_wildcard("*.shop.co.uk") is None
    assert rt.shared_wildcard("*.actions.githubusercontent.com") is None  # run by the service itself


@pytest.mark.parametrize("value", ["*.com", "*.co.uk", "*.com.au", "*.kyoto.jp", "*.ca.us", "*.org"])
def test_a_top_level_domain_and_a_countrys_public_suffix_stay_refused_always(value):
    with pytest.raises(ValueError) as caught:
        rt.shared_wildcard(value)
    assert not isinstance(caught.value, rt.SharedWildcardError)
    with pytest.raises(ValueError) as caught:
        rt.wildcard_suffix(value, shared_ok=True)
    assert not isinstance(caught.value, rt.SharedWildcardError) and "--allow-shared-wildcard" not in str(caught.value)


def test_unconfirmed_it_is_refused_naming_the_risk_and_the_option():
    with pytest.raises(rt.SharedWildcardError) as caught:
        rt.wildcard_suffix("*.blob.core.windows.net")
    text = str(caught.value)
    assert "anyone can have a name of their own" in text and RISK in text
    assert "--allow-shared-wildcard '*.blob.core.windows.net'" in text
    assert caught.value.wildcard == "*.blob.core.windows.net" and caught.value.shared == "blob.core.windows.net"
    assert isinstance(caught.value, ValueError)  # every caller that refused it still does
    assert rt.wildcard_suffix("*.blob.core.windows.net", shared_ok=True) == "blob.core.windows.net"
    assert rt.wildcard_suffix("*.example.com", shared_ok=True) == "example.com"


def test_the_risk_text_names_the_shared_suffix():
    assert RISK in rt.shared_wildcard_risk("*.blob.core.windows.net")
    assert "below github.io" in rt.shared_wildcard_risk("*.mine.github.io")
    assert rt.shared_wildcard_risk("*.example.com") is None


def test_the_lists_sections_decide(tmp_path):
    path = psl(tmp_path, private="tunnel.example\nshared.cloud.example", icann="*.rent.example\nregion.example")
    assert rt.shared_wildcard("*.tunnel.example", path) == "tunnel.example"
    assert rt.shared_wildcard("*.cloud.example", path) == "cloud.example"  # a private suffix lies below it
    assert rt.shared_wildcard("*.mine.tunnel.example", path) is None  # someone's own name
    for value in ("*.foo.ck", "*.rent.example", "*.x.rent.example", "*.region.example", "*.co.uk"):
        with pytest.raises(ValueError) as caught:  # the ICANN section: a registry's, refused always
            rt.shared_wildcard(value, path)
        assert not isinstance(caught.value, rt.SharedWildcardError), value
    assert rt.shared_wildcard("*.www.ck", path) is None  # !www.ck


def test_a_malformed_list_refuses_every_wildcard_confirmed_or_not(tmp_path):
    path = psl(tmp_path, private="*.*.bad")
    for value in ("*.example.com", "*.blob.core.windows.net"):
        with pytest.raises(ValueError, match="every wildcard is refused") as caught:
            rt.wildcard_suffix(value, path, shared_ok=True)
        assert not isinstance(caught.value, rt.SharedWildcardError)


def test_without_the_list_file_the_built_in_suffixes_are_shared(tmp_path):
    missing = tmp_path / "none.dat"
    assert rt.shared_wildcard("*.ts.net", missing) == "ts.net"
    with pytest.raises(ValueError) as caught:
        rt.shared_wildcard("*.co.uk", missing)
    assert not isinstance(caught.value, rt.SharedWildcardError)


# --- the supervisor's control op (what `sandbox-ai net allow` sends) ---------------------------------------------

@pytest.fixture
def own_set(monkeypatch):
    monkeypatch.setattr(rt, "WILDCARDS", set())
    events = []
    monkeypatch.setattr(rt, "safeguard_event", lambda *args, **fields: events.append((args, fields)))
    return events


def test_the_control_op_takes_it_only_confirmed(own_set):
    with pytest.raises(rt.SharedWildcardError):
        rt.change_wildcards({"add": ["*.blob.core.windows.net"]})
    assert rt.change_wildcards({"add": ["*.blob.core.windows.net"], "shared": ["*.BLOB.core.windows.net"]}) == \
        ["blob.core.windows.net"]
    # Taken back without a confirmation: a revoke only narrows.
    assert rt.change_wildcards({"remove": ["*.blob.core.windows.net"]}) == []
    with pytest.raises(ValueError) as caught:
        rt.change_wildcards({"add": ["*.co.uk"], "shared": ["*.co.uk"]})
    assert not isinstance(caught.value, rt.SharedWildcardError)


def test_the_control_op_holds_the_policy_and_the_mode(own_set):
    request = {"add": ["*.blob.core.windows.net"], "shared": ["*.blob.core.windows.net"]}
    with pytest.raises(ValueError, match="administrator's policy"):
        rt.change_wildcards(request, forbid=["--allow-shared-wildcard"])
    with pytest.raises(ValueError, match="security mode high"):
        rt.change_wildcards(request, mode={"name": "high", "refuses": ["--allow-shared-wildcard"]})
    # A mode record of an older start (without the option): its --allow-host refusal holds.
    with pytest.raises(ValueError, match="security mode paranoid"):
        rt.change_wildcards(request, mode={"name": "paranoid", "refuses": ["--allow-host"]})
    assert rt.WILDCARDS == set()
    assert [args[0] for args, _ in own_set] == ["policy", "mode", "mode"]
    # An own domain is no shared one: only --allow-host's own refusal applies to it.
    assert rt.change_wildcards({"add": ["*.example.com"]}, forbid=["--allow-shared-wildcard"]) == ["example.com"]


# --- the start (the launcher: s.launch is ./sandbox-ai's entry) -----------------------------------------------------

import test_feature_matrix as fm  # noqa: E402

s = fm.s
WILD = "*.blob.core.windows.net"


@pytest.fixture(params=[("claude", "podman"), ("codex", "bwrap")], ids=["claude-podman", "codex-bwrap"])
def m(request, tmp_path, monkeypatch, capsys):
    return fm.make_matrix(*request.param, tmp_path, monkeypatch, capsys)


def test_the_option_is_the_confirmation_and_the_run_marks_the_risk(m):
    plan = m.plan("--allow-shared-wildcard", WILD)
    assert plan["host_wildcards"] == [WILD] and plan["shared_wildcards"] == {WILD: "blob.core.windows.net"}
    s._WARNED.clear()  # (a warning is said once per process: the plan's start said it already)
    seen = m.run("--allow-shared-wildcard", WILD)
    assert seen.run_record["shared_wildcards"] == {WILD: "blob.core.windows.net"}
    assert seen.run_record["allow_wildcards"] == [WILD]
    assert seen.config["wildcards"] == [WILD] and seen.config["shared_wildcards"] == [WILD]
    assert "could open any server anyone rents below blob.core.windows.net" in seen.err  # said, with the risk
    # An own domain's wildcard is no risk of that kind: nothing marked.
    seen = m.run("--allow-host", "*.example.org")
    assert "shared_wildcards" not in seen.run_record and seen.config["shared_wildcards"] == []


def test_allow_host_without_a_terminal_is_refused_naming_the_option(m):
    m.refused("--allow-host", WILD, match=r"--allow-shared-wildcard '\*\.blob\.core\.windows\.net'.*--yes")
    m.refused("--allow-host", WILD, match="could open any server anyone rents below blob.core.windows.net")
    plan = m.plan("--allow-host", WILD, "--yes")  # for scripts
    assert plan["shared_wildcards"] == {WILD: "blob.core.windows.net"}


def test_on_a_terminal_the_start_asks(m, monkeypatch):
    asked = []
    for answer, allowed in (("y", True), ("", False), ("no", False)):
        a = s.parse_args([m.agent, "--no-config", "--allow-host", WILD])
        monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(s.sys.stderr, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda prompt, answer=answer: asked.append(prompt) or answer)
        s.confirm_shared_wildcards(a)
        assert (WILD in a.shared_confirmed) is allowed
    assert asked == ["agent-sandbox: allow anyway? [y/N] "] * 3


def test_a_registrys_suffix_stays_refused_whatever_is_said(m):
    for value in ("*.co.uk", "*.kyoto.jp"):
        m.refused("--allow-shared-wildcard", value, match=r"public suffix of a registry")
        m.refused("--allow-host", value, "--yes", match=r"public suffix of a registry")
    m.refused("--allow-shared-wildcard", "*.com", match="not a host wildcard")


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_the_strict_modes_refuse_it_by_its_name(m, mode):
    m.refused("--mode", mode, "--allow-shared-wildcard", WILD,
              match=rf"^--allow-shared-wildcard \*\.blob\.core\.windows\.net widens the security mode {mode}")
    m.refused("--mode", mode, "--allow-host", WILD, "--yes", match=rf"widens the security mode {mode}")


@pytest.mark.parametrize("policy", [{"forbid_shared_wildcards": True}, {"forbid": ["--allow-shared-wildcard"]}])
def test_the_administrators_policy_forbids_it(m, policy):
    m.policy(**policy)
    m.refused("--allow-shared-wildcard", WILD, match="administrator's policy")
    m.refused("--allow-host", WILD, "--yes", match="administrator's policy")
    assert m.plan("--allow-host", "*.example.org")["host_wildcards"] == ["*.example.org"]
    seen = m.run("--allow-host", "*.example.org")
    assert "--allow-shared-wildcard" in seen.config["policy"]["forbid"]  # the supervisor holds net allow to it


@pytest.mark.skipif(os.geteuid() != 0, reason="a policy counts only when root owns it")
def test_a_policy_value_must_be_a_flag(tmp_path):
    folder = tmp_path / "etc"
    folder.mkdir(mode=0o755)
    path = folder / "policy.json"
    path.write_text(json.dumps({"version": 1, "forbid_shared_wildcards": "yes"}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="forbid_shared_wildcards is true or false"):
        s.load_policy(path)
    path.write_text(json.dumps({"version": 1, "forbid_shared_wildcards": True}))
    assert s.load_policy(path)["forbid_shared_wildcards"] is True


def test_a_trusted_project_file_carries_it_and_the_trust_question_marks_it(m):
    m.project_file({"options": ["--allow-shared-wildcard", WILD]})
    assert m.plan()["shared_wildcards"] == {WILD: "blob.core.windows.net"}
    found = s.MODES.weakening_options(s, ["--allow-shared-wildcard", WILD, "--yes"])
    assert found == [("--allow-shared-wildcard", WILD), ("--yes", None)]


def test_allow_domain_of_a_shared_service_names_the_way(m):
    m.refused("--allow-domain", "blob.core.windows.net",
              match=r"--allow-domain blob\.core\.windows\.net: refused: .*--allow-shared-wildcard "
                    r"'\*\.blob\.core\.windows\.net'")


def test_the_mode_record_names_it_for_net_allow():
    a = s.parse_args(["claude", "--no-config", "--mode", "high"])
    a.mode, a.mode_origin = "high", "--mode"
    assert "--allow-shared-wildcard" in s.MODES.runtime_record(s.launcher_namespace(), a)["refuses"]


# --- sandbox-ai net allow (a registered run with a real control_server thread: test_net's fixture) ----------------

import subprocess  # noqa: E402

import test_net as tn  # noqa: E402
from test_net import no_login_runtime, sandbox  # noqa: E402,F401  (fixtures)

net = tn.net
RUN = "codex-1234abcd"


def allow(*options):
    lines = []
    return net.main(["allow", RUN, *options], out=lines.append, resolver=tn.fake_resolver), lines


def test_net_allow_refuses_it_unconfirmed_and_takes_it_confirmed(sandbox, capsys):  # noqa: F811
    record = tn.wildcard_sandbox(sandbox)
    status, _ = allow("--allow-host", WILD)
    err = capsys.readouterr().err
    assert status == 1 and "--allow-shared-wildcard '*.blob.core.windows.net'" in err and RISK in err
    assert not (sandbox["run"] / "wildcards.json").exists()
    status, lines = allow("--allow-shared-wildcard", WILD)
    assert status == 0, capsys.readouterr().err
    assert tn.rt.WILDCARDS == {"blob.core.windows.net"}
    assert json.loads((sandbox["run"] / "wildcards.json").read_text()) == [WILD]
    assert json.loads((sandbox["run"] / "shared-wildcards.json").read_text()) == [WILD]  # your confirmation
    text = "\n".join(lines)
    assert "below the shared service blob.core.windows.net" in text  # the table marks it
    assert f"--allow-shared-wildcard {WILD}" in text  # how to keep it
    lines = []
    assert net.main(["show", RUN, "--json"], out=lines.append) == 0
    assert json.loads("\n".join(lines))["shared_wildcards"] == {WILD: "blob.core.windows.net"}
    # Taken back without a question: a revoke only narrows.
    assert net.main(["revoke", RUN, "--allow-host", WILD], out=lambda line: None) == 0
    assert tn.rt.WILDCARDS == set()
    assert record["directory"]


def test_net_allow_yes_and_the_terminal_question(sandbox, monkeypatch, capsys):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    assert allow("--allow-host", WILD, "--yes")[0] == 0
    assert net.main(["revoke", RUN, "--allow-host", WILD], out=lambda line: None) == 0
    asked = []
    monkeypatch.setattr(net.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(net.sys.stderr, "isatty", lambda: True, raising=False)
    for answer, status in (("n", 1), ("y", 0)):
        monkeypatch.setattr("builtins.input", lambda prompt, answer=answer: asked.append(prompt) or answer)
        assert allow("--allow-host", WILD)[0] == status
    assert asked == ["sandbox-ai net: allow anyway? [y/N] "] * 2
    assert RISK in capsys.readouterr().err  # the risk is said before the question


def test_net_allow_holds_the_policy_and_the_mode(sandbox, monkeypatch, capsys):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    monkeypatch.setattr(net.launcher(), "load_policy", lambda path=None: {"version": 1, "forbid_shared_wildcards": True})
    assert allow("--allow-shared-wildcard", WILD)[0] == 1
    assert "administrator's policy forbids" in capsys.readouterr().err
    monkeypatch.setattr(net.launcher(), "load_policy", lambda path=None: {})
    path = sandbox["run"] / "run.json"
    record = json.loads(path.read_text())
    for refuses in (["--allow-host", "--allow-shared-wildcard"], ["--allow-host"]):  # (the latter: an older start)
        path.write_text(json.dumps({**record, "mode": {"name": "high", "origin": "--mode", "refuses": refuses}}))
        assert allow("--allow-shared-wildcard", WILD)[0] == 1
        assert "widens the security mode high" in capsys.readouterr().err
    assert tn.rt.WILDCARDS == set()


def test_the_supervisor_refuses_a_request_written_by_hand(sandbox):  # noqa: F811
    with pytest.raises(ValueError, match="without your confirmation"):
        net.ask(str(sandbox["run"]), {"op": "wildcards", "add": [WILD]})
    assert tn.rt.WILDCARDS == set()


def test_keep_keeps_it_as_the_confirmation_only_where_you_gave_it(sandbox):  # noqa: F811
    record = tn.wildcard_sandbox(sandbox)
    (sandbox["run"] / "wildcards.json").write_text(json.dumps([WILD, "*.github.io"]))
    (sandbox["run"] / "shared-wildcards.json").write_text(json.dumps([WILD]))
    tokens, _, notes = net.running_options(record, {"https": [], "tcp": [], "nets": []})
    assert tokens[:2] == ["--allow-shared-wildcard", WILD] and "*.github.io" not in tokens
    assert any("*.github.io: not kept" in note for note in notes)


def test_e2e_through_the_command(sandbox):  # noqa: F811
    tn.wildcard_sandbox(sandbox)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    command = [str(ROOT / "sandbox-ai"), "net", "allow", RUN]
    refused = subprocess.run([*command, "--allow-host", WILD], capture_output=True, text=True, env=env, timeout=120,
                             stdin=subprocess.DEVNULL)
    assert refused.returncode == 1 and "--allow-shared-wildcard" in refused.stderr, refused.stderr
    done = subprocess.run([*command, "--allow-shared-wildcard", WILD], capture_output=True, text=True, env=env,
                          timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    assert tn.rt.WILDCARDS == {"blob.core.windows.net"}
    always = subprocess.run([*command, "--allow-shared-wildcard", "*.co.uk"], capture_output=True, text=True, env=env,
                            timeout=120, stdin=subprocess.DEVNULL)
    assert always.returncode == 1 and "public suffix of a registry" in always.stderr


def test_a_session_keeps_the_terminals_yes_as_the_option(m, monkeypatch):
    """A session's supervisor (in tmux) and its later runs have no terminal to ask on: what was confirmed at the
    start's question (or --yes) is kept as --allow-shared-wildcard in the options the session saves."""
    kept = []
    monkeypatch.setattr(s.load_sessions_module(), "start", lambda a, argv: kept.append(list(argv)) or 0)
    assert s.main(m.argv("--allow-host", WILD, "--yes", "--session", "work")) == 0
    assert kept and kept[0][kept[0].index("--allow-shared-wildcard") + 1] == WILD
