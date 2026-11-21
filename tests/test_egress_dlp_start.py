"""The secret guard at a start, for every agent on both backends (the feature matrix's fixture `m`: a whole start
through main(), the backend a recorder): what the proxy is given (proxy.json "secrets") - every secret the run is
handed in plain text, in the forms a request may carry it; the token shapes; the decisions of the mode and the
rulesets; the allow-list of the start and of the project - and that no value is printed anywhere."""
import base64
import json
import string
import types

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
rt = s.RUNTIME
guard = s.load_secret_guard_module()
TAIL = (string.ascii_letters + string.digits) * 2
# Assembled here, so that no scanner takes this file for real keys; none of them is one.
API_TOKEN = "tok" + "-" + TAIL[5:41]
GIT_TOKEN = "gh" + "p_" + TAIL[7:43]
SHAPED = "AI" + "za" + TAIL[11:46]


def forms_of(run, ident):
    return {text: encoding for text, value, encoding in run.proxy["secrets"]["forms"] if value == ident}


def test_every_start_with_the_proxy_has_the_guard(m):
    run = m.run()
    secrets = run.proxy["secrets"]
    assert (secrets["on_send"], secrets["on_shape"], secrets["by"]) == ("report", "report", {})
    assert secrets["values"] == [] and secrets["forms"] == [] and secrets["allow"] == []
    assert secrets["providers"] == s.provider_hosts(run.a) and secrets["providers"]
    assert [shape["id"] for shape in secrets["shapes"]] == [ident for ident, _, _ in guard.builtin_shapes()]
    assert m.plan()["secret_guard"]["shapes"] == len(guard.builtin_shapes())  # (their names: sandbox-ai secret-guard)
    # the proxy takes the launcher's settings as they are
    proxy_guard = rt.SecretGuard(secrets)
    assert proxy_guard.settings == rt.SECRET_DEFAULTS and len(proxy_guard.shapes.shapes) == len(secrets["shapes"])
    assert proxy_guard.providers == set(secrets["providers"]) and proxy_guard.active
    # without the proxy there is no guard (and nothing to say about it)
    lone = m.run("--no-proxy")
    assert lone.proxy is None and m.plan("--no-proxy")["secret_guard"] is None and "secret guard" not in lone.err


def test_the_secrets_a_run_is_handed_are_registered_in_every_form(m):
    store = {"api": API_TOKEN, "short": "abc123"}
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get=store.get))
    m.monkeypatch.setenv("GH_PAT", GIT_TOKEN)
    m.monkeypatch.setenv("DB_PASSWORD", "correct horse battery")
    m.monkeypatch.setenv("BUILD_FLAVOUR", "release-candidate-7")
    m.monkeypatch.setenv("PLAIN_NAME", SHAPED)
    options = ["--secret-env", "API_TOKEN=api", "--secret-env", "PIN=short", "--env", "DB_PASSWORD", "--env",
               "BUILD_FLAVOUR", "--env", "PLAIN_NAME", "--git-token-env", "https://github.com=GH_PAT"]
    run = m.run(*options)
    secrets = run.proxy["secrets"]
    assert secrets["values"] == [
        {"id": "env:API_TOKEN", "label": "the value of API_TOKEN (--secret-env)", "hosts": []},
        {"id": "env:DB_PASSWORD", "label": "the value of DB_PASSWORD (--env)", "hosts": []},
        # a variable whose name says nothing, with a value that has a token's shape
        {"id": "env:PLAIN_NAME", "label": "the value of PLAIN_NAME (--env)", "hosts": [], "shape": "google-api-key"},
        # a Git token that enters the sandbox was given for its server (and the names below it)
        {"id": "git-token:github.com", "label": "the Git token for github.com (--git-token-env)",
         "hosts": ["github.com", "*.github.com"], "shape": "github-token"}]
    assert "release-candidate-7" not in json.dumps(secrets)  # a variable that is no secret is none
    found = forms_of(run, "env:API_TOKEN")
    assert found[API_TOKEN] == "plain" and found[API_TOKEN.encode().hex()] == "hex"
    assert found[base64.b64encode(API_TOKEN.encode()[:39]).decode()] == "base64"
    assert forms_of(run, "env:DB_PASSWORD")["correct%20horse%20battery"] == "url"
    assert forms_of(run, "git-token:github.com")[GIT_TOKEN] == "plain"
    # a value too short to tell from ordinary traffic is not looked for, and the start says so - never the value
    assert "note: the secret guard does not look for the value of PIN (--secret-env): a value of fewer than 8 " \
           "characters" in run.err
    assert not forms_of(run, "env:PIN")
    for value in (API_TOKEN, GIT_TOKEN, SHAPED, "correct horse battery", "abc123"):
        assert value not in run.err
    # the proxy finds what it was given: here the Git token on its way to another host than its own
    proxy_guard = rt.SecretGuard(secrets)
    body = json.dumps({"token": GIT_TOKEN}).encode()
    assert [f["id"] for f in rt.SecretScan(proxy_guard, "paste.example", False, []).feed(body)] == ["git-token:github.com"]
    assert rt.SecretScan(proxy_guard, "api.github.com", False, []).feed(body) == []
    # the dry run names what the guard would know, by the options alone (it reads no secret: one too short to be
    # registered is named too), never a value or a form of one
    plan = m.plan(*options)
    assert [value["id"] for value in plan["secret_guard"]["values"]] == [
        "env:API_TOKEN", "env:PIN", "env:DB_PASSWORD", "env:PLAIN_NAME", "git-token:github.com"]
    assert plan["secret_guard"]["values"][0] == {"id": "env:API_TOKEN", "label": "the value of API_TOKEN "
                                                 "(--secret-env)", "hosts": []}
    text = json.dumps(plan)
    for value in (API_TOKEN, GIT_TOKEN, SHAPED, "correct horse battery", API_TOKEN.encode().hex()):
        assert value not in text
    assert "forms" not in plan["secret_guard"]


def test_a_dry_run_reads_no_secret(m):
    """Found by the whole suite: the plan of a start whose secret is not kept (Hermes' gateway token, an MCP server's
    variable) failed, because the guard's part of the plan read the secret store. A dry run asks no keyring."""
    def never(name):
        raise AssertionError(f"the dry run read the secret {name}")
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get=never))
    m.monkeypatch.setenv("GH_PAT", GIT_TOKEN)
    plan = m.plan("--secret-env", "API_TOKEN=not_kept", "--secret-env", "OTHER")
    assert [value["id"] for value in plan["secret_guard"]["values"]] == ["env:API_TOKEN", "env:OTHER"]
    # the start itself still says that the secret is missing
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get={}.get))
    m.refused("--secret-env", "API_TOKEN=not_kept", match="no secret not_kept")


def test_a_token_the_proxy_adds_never_enters_the_sandbox_and_is_not_registered(m):
    m.monkeypatch.setenv("GH_PAT", GIT_TOKEN)
    run = m.run("--git-token-env", "https://github.com=GH_PAT", "--token-proxy")
    assert run.proxy["secrets"]["values"] == [] and run.proxy["secrets"]["forms"] == []
    assert run.proxy["git_tokens"][0]["token"] == GIT_TOKEN


def test_a_well_known_variable_was_given_for_its_service(m):
    m.monkeypatch.setenv("OPENAI_API_KEY", "sk" + "-proj-" + TAIL[:40])
    m.monkeypatch.setenv("HF_TOKEN", "hf" + "_" + TAIL[:34])
    values = {value["id"]: value for value in m.run("--env", "OPENAI_API_KEY", "--env", "HF_TOKEN"
                                                    ).proxy["secrets"]["values"]}
    assert values["env:OPENAI_API_KEY"]["hosts"] == ["api.openai.com"]
    assert values["env:HF_TOKEN"]["hosts"] == ["huggingface.co", "*.huggingface.co", "*.hf.co"]
    assert values["env:HF_TOKEN"]["shape"] == "huggingface-token"


def test_the_strict_modes_block_a_known_value_and_ask_or_block_a_shape(m):
    for mode, expected in (("lax", ("report", "report")), ("low", ("report", "report")),
                           ("medium", ("report", "report")), ("high", ("block", "ask")),
                           ("paranoid", ("block", "block"))):
        secrets = m.run("--mode", mode).proxy["secrets"]
        assert (secrets["on_send"], secrets["on_shape"]) == expected, mode
        assert secrets["by"] == ({} if expected == ("report", "report") else
                                 {"on_send": f"mode {mode}", "on_shape": f"mode {mode}"})
    assert m.plan("--mode", "high")["secret_guard"]["on_send"] == "block"


def test_rulesets_tighten_the_guard_and_never_loosen_it(m):
    strict = m.private_file("strict.json", json.dumps({"version": 1, "secrets": {"on_send": "ask", "on_shape": "block"}}))
    lax = m.private_file("lax.json", json.dumps({"version": 1, "secrets": {"on_send": "report", "on_shape": "report"}}))
    secrets = m.run("--rules", str(lax), "--rules", str(strict)).proxy["secrets"]
    assert (secrets["on_send"], secrets["on_shape"]) == ("ask", "block")
    assert secrets["by"] == {"on_send": "--rules strict.json", "on_shape": "--rules strict.json"}
    # the mode's and a ruleset's: the stricter of each
    secrets = m.run("--mode", "high", "--rules", str(strict)).proxy["secrets"]
    assert (secrets["on_send"], secrets["on_shape"]) == ("block", "block")
    assert secrets["by"] == {"on_send": "mode high", "on_shape": "--rules strict.json"}
    broken = m.private_file("broken.json", json.dumps({"version": 1, "secrets": {"on_send": "stop"}}))
    m.refused("--rules", str(broken), match=r'"secrets" takes "on_send" and "on_shape" \(report, ask, block\)')
    trial = m.private_file("trial.json", json.dumps({"version": 1, "mode": "audit", "secrets": {"on_send": "block"}}))
    m.refused("--rules", str(trial), match='"secrets" acts on the run')


def test_what_is_allowed_for_the_start_and_for_the_project(m):
    assert guard.allow(m.work, ["google-api-key@*.example.com", "env:API_TOKEN@api.service.example"]) == [
        "google-api-key@*.example.com", "env:API_TOKEN@api.service.example"]
    run = m.run("--allow-send", "GitHub-Token@Paste.Example", "-sd", "private-key@intra.test", "--allow-send",
                "github-token@paste.example")
    assert run.proxy["secrets"]["allow"] == ["github-token@paste.example", "private-key@intra.test",
                                             "google-api-key@*.example.com", "env:API_TOKEN@api.service.example"]
    assert rt.SecretGuard(run.proxy["secrets"]).allow == [
        ("github-token", "paste.example"), ("private-key", "intra.test"), ("google-api-key", "*.example.com"),
        ("env:API_TOKEN", "api.service.example")]
    assert m.plan("--allow-send", "*@intra.test")["secret_guard"]["allow"][0] == "*@intra.test"
    m.refused("--allow-send", "github-token", match=r"--allow-send 'github-token': WHAT@HOST")
    m.refused("--allow-send", "nope@host.example", match=r"--allow-send 'nope@host.example': WHAT@HOST")
    # another project's list is not this one's
    other = m.tmp / "elsewhere"
    other.mkdir()
    guard.allow(other, ["npm-token@only.there"])
    assert "npm-token@only.there" not in m.run().proxy["secrets"]["allow"]
    # taken back
    assert guard.forget(m.work, ["google-api-key@*.example.com"]) == ["google-api-key@*.example.com"]
    assert m.run().proxy["secrets"]["allow"] == ["env:API_TOKEN@api.service.example"]


def test_the_strict_modes_refuse_an_allow_for_anything_or_anywhere(m):
    for mode in ("high", "paranoid"):
        for spec in ("*@intra.test", "github-token@*", "*@*"):
            m.refused("--mode", mode, "--allow-send", spec,
                      match=rf"--allow-send {spec.replace('*', '[*]')} widens the security mode {mode} \(--mode\): a \* "
                            r"there lets any secret out, or a secret go to any host")
        assert m.run("--mode", mode, "--allow-send", "github-token@api.example.com").proxy["secrets"]["allow"] == [
            "github-token@api.example.com"]
    assert m.run("--mode", "medium", "--allow-send", "*@*").proxy["secrets"]["allow"] == ["*@*"]


def test_a_start_that_hands_out_secrets_says_where_the_guard_sees_nothing(m):
    store = {"api": API_TOKEN}
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get=store.get))
    assert "secret guard" not in m.run("--secret-env", "API_TOKEN=api").err
    err = m.run("--secret-env", "API_TOKEN=api", "--allow-host", "api.example.org", "--no-inspect", "api.example.org").err
    assert "note: the secret guard does not see requests to api.example.org: their TLS passes unopened" in err
    assert "secret guard" not in m.run("--allow-host", "api.example.org", "--no-inspect", "api.example.org").err
