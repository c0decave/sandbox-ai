"""DeepSeek Harness as a sandbox-ai agent (sandbox/dsh.py, docs/DSH_REVIEW.md): its read-only patch, its command line,
telemetry that is never switchable, the image (the reviewed release, the hash-locked npm install, no pipe into a
shell), its build, sessions and helper mode. The start itself is in tests/test_feature_matrix.py (DSH) and the
launcher's contracts (test_agent_sandbox_contracts.py)."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dsh = load("dsh_tests", "sandbox/dsh.py")
s = load("launcher_dsh_tests", "sandbox/agent_sandbox.py")
llm = load("llm_dsh_tests", "sandbox/llm.py")
sessions = load("sessions_dsh_tests", "sandbox/sessions.py")
presets = load("presets_dsh_tests", "sandbox/presets.py")
kits = load("kits_dsh_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_dsh_tests", "sandbox/agent_mcp.py")
lock_script = load("agent_npm_lock_dsh_tests", "scripts/agent_npm_lock.py")
LOCK = ROOT / "sandbox/Containerfile.dsh.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.dsh"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DSH_HOME", raising=False)
    (tmp_path / "home").mkdir()


def ns(agent="dsh", args=(), **more):
    return argparse.Namespace(**{"agent": agent, "agent_args": list(args), "allow_host": [], "no_inspect": [],
                                 "model": None, **more})


# --- the command line -------------------------------------------------------------------------------------------------

def test_the_patch_comes_right_after_the_profile():
    assert dsh.command_args([]) == ["chat"], "the chat in the terminal: its own command line (dsh.command)"
    assert dsh.command_args(["web"]) == ["web", "--patch", dsh.PATCH, *dsh.WEB_ARGS[1:]]
    assert dsh.command_args(["--profile", "web"]) == dsh.command_args(["web"])
    assert dsh.command_args(["headless", "run the tests"]) == ["headless", "--patch", dsh.PATCH, "run the tests"]
    assert dsh.command_args(["--profile", "acp"]) == ["--profile", "acp", "--patch", dsh.PATCH]
    assert dsh.command_args(["--help"]) == ["--help"]  # the launcher's own flags: no profile, no patch
    assert dsh.WEB_ARGS == ("web", "--no-open", "--host", "127.0.0.1", "--port", "3080")


def test_check_refuses_what_dsh_cannot_do_here():
    dsh.check(ns(args=["headless", "x"]))  # fine
    dsh.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="not in the team image"):
        dsh.check(ns(), companions=["claude"])
    with pytest.raises(ValueError, match="not in the team image"):
        dsh.check(ns("claude"), companions=["dsh"])
    with pytest.raises(ValueError, match="not enforced for DeepSeek Harness"):
        dsh.check(ns(), hooks_active=True)
    for args in (["desktop"], ["plugin", "--profile", "web", "add", "x"], ["--profile", "desktop"]):
        with pytest.raises(ValueError, match="not in a sandbox"):
            dsh.check(ns(args=args))
    for args in (["web", "--patch", "x.yml"], ["headless", "--patch=x.yml", "task"]):
        with pytest.raises(ValueError, match="--patch in DSH's arguments"):
            dsh.check(ns(args=args))
    dsh.check(ns(args=["headless", "--", "--patch is just text"]))  # after "--"


# --- the patch ----------------------------------------------------------------------------------------------------

def test_the_patch_pins_telemetry_last():
    assert dsh.patch(ns()) == dsh.TELEMETRY_PINNED
    assert {row["id"] for row in dsh.TELEMETRY_PINNED} == {"session-telemetry-otel", "otel"}
    sneaky = {"route": {"api": "openai-completions"}, "model": "m", "instructions": None}
    rows = dsh.patch(ns(model="deepseek-pro"), mcp=[{"id": "x"}], llm=sneaky)
    assert rows[-2:] == dsh.TELEMETRY_PINNED, "after everything: never turned on again"


def test_model_rows():
    assert dsh.model_rows(ns()) == []
    assert dsh.model_rows(ns(model="deepseek-pro")) == [
        {"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": "deepseek-pro"}}]
    rows = dsh.model_rows(ns(), {"route": {"api": "openai-completions"}, "model": "q", "instructions": "be brief"})
    assert rows[0] == {"id": "llm-pi-ai", "config": {"providers": {"sandbox-ai": {"api": "openai-completions"}}}}
    assert rows[1]["config"] == {"provider": "sandbox-ai", "model": "q"}
    assert rows[2]["config"]["personaPrefix"] == dsh.PERSONA + "\n\nbe brief"


def test_mcp_rows_go_through_the_bridge():
    rows = dsh.mcp_rows(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name], {"helper-claude-x": 2000})
    assert [row["config"]["serverName"] for row in rows] == ["demo", "helper-claude-x"]
    assert rows[0]["config"] == {"serverName": "demo", "transport": "stdio", "command": "python3",
                                 "args": ["/bridge", "demo"]}
    assert rows[1]["config"]["toolCallTimeoutMs"] == 2000 * 1000
    long = dsh.mcp_rows(["x" * 40], lambda name: [])[0]["config"]["serverName"]
    assert re.fullmatch(dsh.MCP_NAME, long)


def test_telemetry_hosts_are_never_opened():
    assert dsh.telemetry_hosts_in(["*.deepseeksvc.com"]) == list(dsh.TELEMETRY_HOSTS)
    assert dsh.telemetry_hosts_in(["api.deepseek.com", "platform.deepseek.com", "deepseeksvc.com.evil"]) == []
    assert not dsh.telemetry_hosts_in(s.PROVIDERS["dsh"])
    for opened in ({"allow_host": ["dsh-otel-collector.deepseeksvc.com"]},
                   {"no_inspect": ["dsh-otel-collector.deepseeksvc.com"]}, {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            dsh.check_telemetry(argparse.Namespace(**opened))
    assert [rule["host"] for rule in dsh.telemetry_rules()] == dsh.rule_hosts() == list(dsh.TELEMETRY_HOSTS)


def test_environment_and_plan():
    env = dsh.environment(ns())
    assert env["DSH_TELEMETRY_DISABLED"] == "1" and env["DSH_HOME"] == "/root/.dsh"
    assert env["DSH_TELEMETRY_OTLP_URL"].startswith("http://127.0.0.1:9/"), "a closed port, never the collector"
    plan = dsh.plan(ns())
    assert plan["version"] == dsh.VERSION and plan["profile"] == "chat" and plan["web_ui"] is None
    assert dsh.plan(ns(args=["web"]))["profile"] == "web" and dsh.plan(ns(args=["web"]))["web_ui"] == dsh.WEB_URL
    assert plan["telemetry"]["switchable"] is False and plan["repository"] == dsh.REPOSITORY
    assert dsh.plan(ns(args=["headless", "x"]))["web_ui"] is None
    assert dsh.plan(ns(args=["--profile", "acp"]))["profile"] == "acp"


def test_the_launcher_knows_dsh():
    assert s.DANGER_ENV["dsh"] == {"DSH_PERMISSION_MODE": dsh.DANGER_MODE} and s.DANGER_ARGS["dsh"] == []
    assert s.AUTH_TARGETS["dsh"] == ".dsh/.credentials.yaml" and "dsh" in s.NOT_IN_TEAM
    assert s.AGENT_EFFORTS["dsh"] == () and "dsh" in s.AGENT_PROGRAMS
    assert llm.DSH_PATCH == dsh.PATCH and llm.DSH_ROUTE == dsh.LLM_ROUTE and llm.KEY_VARIABLE == dsh.KEY_VARIABLE
    assert agent_mcp.DSH_PATCH == dsh.PATCH


def test_the_hosts_dsh_home(tmp_path, monkeypatch):
    assert s.auth_sources("dsh") == [(tmp_path / "home/.dsh/.credentials.yaml", ".dsh/.credentials.yaml")]
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "d"))
    assert s.auth_sources("dsh")[0][0] == tmp_path / "d/.credentials.yaml"
    assert not s.merge_refreshed("dsh", {"a": 1}, {"a": 2}), "its store is YAML: never merged back"


# --- the image ----------------------------------------------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG DSH_VERSION={dsh.VERSION}" in text and "COPY Containerfile.dsh.lock" in text
    assert "npm ci --ignore-scripts" in text and "npm install" not in text and "pnpm" not in text.split("ARG")[-1]
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert "ENV DSH_TELEMETRY_DISABLED=1" in text and "mkdir -p -m 755 /etc/dsh" in text


def test_the_lock_holds_the_reviewed_package_with_every_integrity():
    lock = json.loads(LOCK.read_text())
    assert lock_script.check(lock, "dsh", dsh.PACKAGE, dsh.VERSION) > 600
    entry = lock["packages"][f"node_modules/{dsh.PACKAGE}"]
    assert entry["integrity"] == dsh.INTEGRITY
    assert all(entry.get("resolved", "").startswith("https://registry.npmjs.org/")
               for path, entry in lock["packages"].items() if path), "only npm's registry"
    ours = [path for path in lock["packages"] if path.startswith("node_modules/@deepseek-ai/dsh-")]
    assert ours and all(lock["packages"][path]["version"] == dsh.VERSION for path in ours
                        if lock["packages"][path]["version"].startswith("0.2.")), "its own plugins at its version"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) "
                      "+ '\\n')\nif sys.argv[1:3] == ['image', 'inspect']:\n    print('[]')\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def build(*args):
    return subprocess.run(["/bin/bash", str(ROOT / "sandbox/build_image.sh"), *args], capture_output=True, text=True)


def test_build_dsh_takes_the_reviewed_release_only(engine):
    result = build("dsh")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"DSH_VERSION={dsh.VERSION}" in calls[0] and any(a.endswith("Containerfile.dsh") for a in calls[0])
    refused = build("--pin", "0.2.2-rc.3", "dsh")
    assert refused.returncode == 2 and "DeepSeek Harness is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("dsh", "--build-arg", "DSH_VERSION=0.2.2-rc.3").returncode == 2


# --- the agent's other places ----------------------------------------------------------------------------------

def test_sessions_and_one_shots():
    assert sessions.CONTINUE_ARGS["dsh"] == [] and "headless" in sessions.ONE_SHOT_ARGS
    assert sessions.SESSION_OPTIONS["dsh"] == {"--session-id"} and "--patch" in sessions.VALUE_OPTIONS["dsh"]


def test_a_presets_first_task_runs_headless():
    assert presets.prompt_args({"prompt": "fix {what}"}, "dsh", {"what": "it"}) == ["headless", "fix it"]
    assert presets.prompt_args({"prompt": "-fix it"}, "dsh") == ["headless", " -fix it"], "never an option"


def test_kits_and_memory_for_dsh():
    assert kits.LAYOUT["dsh"] == {"skills": ".agents/skills"}
    assert kits.MEMORY_FILE["dsh"] == ("AGENTS.md", ".dsh/AGENTS.md")
    skill = (kits.BUILTIN / "dsh/skills/sandbox-ai/SKILL.md").read_text()
    assert "## DeepSeek Harness in this sandbox" in skill and dsh.TELEMETRY_HOSTS[0] in skill
    assert (kits.TEMPLATES / "dsh/AGENTS.md").is_file()


def test_dsh_as_a_helper_reads_its_task_on_standard_input():
    assert agent_mcp.command("dsh", "-rf x") == ["dsh", "headless", "--patch", dsh.PATCH, "-"]
    assert "dsh" in agent_mcp.STDIN_PROMPT and "-rf x" not in agent_mcp.command("dsh", "-rf x", resume=True)


def test_the_review_names_the_release_and_the_official_repository():
    review = (ROOT / "docs/DSH_REVIEW.md").read_text()
    assert dsh.VERSION in review and dsh.COMMIT in review and dsh.INTEGRITY in review
    assert "deepseek-ai/deepseek-harness" in review and "DeepSeek-TUI" in review and "## Go / No-Go" in review
    assert "### Telemetry: always off, never switchable" in review
    assert all(host in review for host in dsh.TELEMETRY_HOSTS) and all(name in review for name in dsh.TELEMETRY_ENV)
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "DeepSeek Harness" in text and dsh.TELEMETRY_HOSTS[0] in text, readme


# --- the web UI (-- web, and beside the chat with --browser): its window script, the session browser ------------------
#
# User (live, 0.1.95): the terminal of `sandbox-ai dsh` showed only "dsh web: http://127.0.0.1:3080/?token=…" -
# "ist das das einzige bei DeepSeek Harness, startet da kein Chat?" and then "wie kann man den Link erreichen?".
# DSH 0.2.1-rc.5 has no terminal UI (its shipped profiles: acp, web, headless, sdk, sdk-minimal); the URL is on the
# sandbox's own loopback, which only the session browser (--browser) reaches.

web = load("dsh_web_tests", "sandbox/dsh_web.py")
approvals = load("approvals_dsh_tests", "sandbox/approvals.py")
browser = load("browser_dsh_tests", "sandbox/browser.py")
browser_runtime = load("browser_runtime_dsh_tests", "sandbox/browser_runtime.py")
launchlog = load("launchlog_dsh_tests", "sandbox/launchlog.py")
TOKEN = "Zk3v_Q-9xYw2LmN8pR4tU6sA1bC0dEfGhIjKlMnOpQr"  # 43 characters of base64url, as DSH's 32 random bytes
URL = f"http://127.0.0.1:3080/?token={TOKEN}"


def test_the_web_ui_says_what_to_do():
    """-- web: the web UI alone (as every interactive start was before the chat)."""
    assert dsh.PROFILES == ("web", "headless", "acp", "sdk", "sdk-minimal"), "no tui among the shipped profiles"
    web_only = ns(args=["web"])
    assert dsh.note(web_only, opens=False) == dsh.WEB_ONLY
    for words in ("web UI", "--browser", "the app's Browser tab", "Browser part", "start without -- web",
                  'sandbox-ai dsh -- headless "TASK"', "no browser outside the sandbox reaches"):
        assert words in dsh.WEB_ONLY, words
    assert dsh.note(web_only, opens=True) == dsh.IN_BROWSER and "opens in the session browser" in dsh.IN_BROWSER
    assert dsh.note(ns(args=["web", "--port", "8080"]), opens=False) == dsh.WEB_ONLY
    assert dsh.note(ns(args=["headless", "run the tests"]), opens=False) is None
    assert dsh.note(ns(args=["--profile", "acp"]), opens=True) is None
    plan = dsh.plan(web_only, opens=True)
    assert plan["terminal_ui"] is False and plan["opens_in_session_browser"] and plan["note"] == dsh.IN_BROWSER
    assert plan["profile"] == "web" and plan["chat"] is None
    assert dsh.plan(ns(args=["headless", "x"]), opens=True)["opens_in_session_browser"] is False


def test_the_web_ui_runs_through_its_window_script():
    web_only = ns(args=["web"])
    args = dsh.command_args(["web"])
    plain = dsh.command(web_only, "dsh", args)
    assert plain == ["python3", "-I", dsh.WEB_SCRIPT, "--notice", dsh.WEB_ONLY, "--", "dsh", *args]
    opened = dsh.command(web_only, "dsh", args, opens=True)
    assert opened == ["python3", "-I", dsh.WEB_SCRIPT, "--notice", dsh.IN_BROWSER, "--open", "--", "dsh", *args]
    assert args[args.index("--host") + 1] == "127.0.0.1" and "--no-open" in args, "the sandbox's loopback only"
    headless = dsh.command_args(["headless", "task"])
    assert dsh.command(ns(args=["headless", "task"]), "dsh", headless, opens=True) == ["dsh", *headless]
    assert dsh.WEB_SCRIPT == "/etc/dsh/web.py" and dsh.WEB_SCRIPT.startswith(dsh.MANAGED_DIR + "/")
    files = dsh.files(web_only, dsh.patch(web_only))
    assert files["dsh/web.py"] == (ROOT / "sandbox/dsh_web.py").read_text() and "dsh/sandbox-ai.patch.yml" in files
    assert "dsh/chat.py" not in files, "the web UI alone: no chat"
    assert "dsh/web.py" not in dsh.files(ns(args=["headless", "x"]), [])


def test_the_web_ui_takes_a_loopback_port_nothing_else_takes():
    """127.0.0.1:3080 of the sandbox is the host's when the host's port 3080 is relayed (--allow-host-port 3080, or a
    git login for a forge at http://localhost:3080): the browser's connection would go to the host, never to DSH."""
    assert dsh.web_port() == 3080 and dsh.web_port([3080]) == 3081 and dsh.web_port([3080, 3081, 22]) == 3082
    assert dsh.command_args(["web"], 3081)[-2:] == ["--port", "3081"] and dsh.command_args(["web"])[-1] == "3080"
    assert dsh.plan(ns(args=["web"]), port=3081)["web_ui"] == "http://127.0.0.1:3081/"
    assert dsh.plan(ns(), opens=True, port=3081)["web_ui"] == "http://127.0.0.1:3081/", "the chat's web UI too"
    chat = dsh.command(ns(), "dsh", ["chat"], opens=True, port=3081)
    assert json.loads(chat[chat.index("--web") + 1])[-2:] == ["--port", "3081"]
    assert dsh.command_args(["web", "--port", "9000"], 3081) == ["web", "--patch", dsh.PATCH, "--port", "9000"]
    a = s.parse_args(["dsh", "--allow-host-port", "3080"])
    a.services = [{"name": "db", "ports": [3081]}]
    assert s.dsh_web_port(a) == 3082


def test_the_session_browser_reaches_the_sandboxs_loopback_without_the_proxy():
    """The browser shares the pod's network namespace (its container joins the pod): its connection to
    127.0.0.1:3080 is loopback, which the firewall accepts both ways, and the proxy's NAT chain leaves alone (no
    approval, no inspection) - unless the host's port is relayed there, which dsh_web_port avoids."""
    rules = s.firewall_rules([], [], proxy={"port": 8080, "loopback": []})
    assert 'iifname "lo" accept' in rules and 'oifname "lo" accept' in rules
    assert "ip daddr 127.0.0.0/8 return" in rules, "loopback is never redirected to the proxy"
    relayed = s.firewall_rules([], [], proxy={"port": 8080, "loopback": [3080]})
    assert "ip daddr 127.0.0.0/8 tcp dport != { 3080 } return" in relayed, "3081 stays the sandbox's own"


def test_only_the_web_uis_launch_url_is_taken_and_its_token_is_masked():
    assert dsh.is_web_url(URL) and dsh.is_web_url(URL.replace("3080", "8080"))
    for other in (URL + "&x=1", URL.replace("127.0.0.1", "127.0.0.2"), URL.replace("http:", "https:"),
                  "http://127.0.0.1:3080/", "http://evil.example/?token=" + TOKEN, URL + "\n", None, 3):
        assert not dsh.is_web_url(other), other
    assert dsh.mask(f"dsh web: {URL}") == "dsh web: http://127.0.0.1:3080/?token=***"
    assert web.mask(f"dsh web: {URL}") == dsh.mask(f"dsh web: {URL}")
    assert web.LINE.pattern.startswith("dsh web: (" + dsh.WEB_TOKEN_URL.pattern + ")")
    assert web.SOCKET == s.RUNTIME.BROWSER_SOCKET and browser.OPEN_REQUEST == browser_runtime.OPEN_REQUEST
    # launcher.log and the app's log never keep the token
    assert TOKEN not in launchlog.redact(f"dsh web: {URL}")
    app = ROOT / "projects/sandbox-ai-app"
    sys.path.insert(0, str(app))
    try:
        applog = pytest.importorskip("sandbox_ai_app.applog")
        assert TOKEN not in applog.redact(f"dsh web: {URL}") and TOKEN not in applog.argv_text(["xdg-open", URL])
    finally:
        sys.path.remove(str(app))


def test_the_session_opener_takes_the_web_ui_only_and_only_a_few_times(tmp_path):
    handed, logged = [], []
    opener = dsh.session_opener(handed.append, opens=2, log=logged.append)
    assert opener("https://example.com/") is None, "not this one's: your browser's question, as before"
    assert opener(URL) == {"ok": True, "message": dsh.OPENED} and opener(URL)["ok"]
    assert opener(URL)["ok"] is False and handed == [URL, URL]
    assert logged and all(TOKEN not in line for line in logged)

    def broken(url):
        raise OSError("no such folder")
    assert "did not take it" in dsh.session_opener(broken)(URL)["message"]
    # browser.request_open: the VNC folder's open.url, written whole, 0600; nothing but an http(s) URL
    browser.request_open(tmp_path, URL)
    path = tmp_path / browser.OPEN_REQUEST
    assert path.read_text() == URL + "\n" and oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        browser.request_open(tmp_path, "javascript:alert(1)")


def test_browser_reply_hands_the_web_ui_to_the_session_browser(tmp_path):
    handed = []
    opener = dsh.session_opener(handed.append)
    line = json.dumps({"url": URL})
    reply = approvals.browser_reply(line, tmp_path / "approvals", iter(range(9)), lambda *a: None, session=opener,
                                    host=False)
    assert reply == {"ok": True, "message": dsh.OPENED} and handed == [URL]
    assert not (tmp_path / "approvals").exists(), "no question: the sandbox's own browser, the sandbox's own loopback"
    other = approvals.browser_reply(json.dumps({"url": "https://example.com/"}), tmp_path / "approvals",
                                    iter(range(9)), lambda *a: None, session=opener, host=False)
    assert other["ok"] is False and "no URL goes to the human's browser" in other["message"]


def fake_dsh(tmp_path, lines, status=0):
    script = tmp_path / "fake-dsh.py"
    script.write_text("import sys\n" + "".join(f"print({line!r}, flush=True)\n" for line in lines)
                      + f"sys.exit({status})\n")
    return [sys.executable, str(script)]


class Stream:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def flush(self):
        pass

    def text(self):
        return self.data.decode()


def serve(path, answers):
    """A browser socket at PATH answering each request with answers(url); returns the URLs it got."""
    import socket
    import threading
    got = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)

    def loop():
        while True:
            connection, _ = server.accept()
            with connection:
                url = json.loads(connection.makefile("r").readline())["url"]
                got.append(url)
                connection.sendall((json.dumps(answers(url)) + "\n").encode())
    threading.Thread(target=loop, daemon=True).start()
    return got


def wait_for(condition, seconds=10):
    import time
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        time.sleep(0.02)
    return condition()


def test_the_window_script_says_what_to_do_and_masks_the_url(tmp_path, monkeypatch):
    monkeypatch.setattr(web.signal, "signal", lambda *args: None)
    out = Stream()
    status = web.main(["--notice", dsh.WEB_ONLY, "--", *fake_dsh(tmp_path, [f"dsh web: {URL}", "ready"], 3)],
                      stream=out)
    text = out.text()
    assert status == 3 and TOKEN not in text
    assert text.startswith("sandbox-ai: " + dsh.WEB_ONLY + "\n")
    assert "dsh web: http://127.0.0.1:3080/?token=*** (reachable only inside the sandbox)" in text
    assert text.count(dsh.WEB_ONLY) == 2 and text.rstrip().endswith("ready")


def test_the_window_script_hands_the_url_to_the_session_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(web.signal, "signal", lambda *args: None)
    path = tmp_path / "open.sock"
    got = serve(path, lambda url: {"ok": True, "message": dsh.OPENED})
    out = Stream()
    assert web.main(["--notice", dsh.IN_BROWSER, "--open", "--socket", str(path), "--",
                     *fake_dsh(tmp_path, [f"dsh web: {URL}", "dsh web: ready"])], stream=out) == 0
    assert wait_for(lambda: "open in the session browser" in out.text()), out.text()
    assert got == [URL] and TOKEN not in out.text() and "handed to the session browser" in out.text()
    # refused, or no launcher: the URL whole, to open it there by hand
    refused = tmp_path / "refused.sock"
    serve(refused, lambda url: {"ok": False, "message": "sandbox-ai: too often"})
    out = Stream()
    web.main(["--open", "--socket", str(refused), "--", *fake_dsh(tmp_path, [f"dsh web: {URL}"])], stream=out)
    assert wait_for(lambda: "yourself" in out.text())
    assert "not opened in the session browser (too often); open this URL there yourself (the app's Browser tab): " \
           f"{URL}" in out.text()


def test_the_browser_runtime_opens_the_handed_url(tmp_path, monkeypatch):
    for kind in ("chromium", "firefox"):
        mount = tmp_path / kind
        (mount / "vnc").mkdir(parents=True)
        (mount / "config.json").write_text(json.dumps({"kind": kind, "control": "human", "screen": "800x600x24",
                                                       "display": ":42", "cdp_port": None, "profile": "fresh"}))
        started, stopped, handlers, ticks = [], [], {}, [0]

        class Process:
            def __init__(self, argv):
                self.argv, self.ended = argv, None

            def poll(self):
                return self.ended

        class Runtime:
            VNC_DIR = None

            @staticmethod
            def start_gui(cfg):
                return []

            @staticmethod
            def stop_process(process):
                process.ended = -15
                stopped.append((process.argv[-1], ticks[0]))
        monkeypatch.setattr(browser_runtime.signal, "signal", lambda n, h: handlers.__setitem__(n, h))
        monkeypatch.setattr(browser_runtime.os, "environ", dict(os.environ))

        def popen(argv, **kwargs):
            started.append(argv[-1])
            return Process(argv)

        def sleep(_):
            ticks[0] += 1
            if ticks[0] == 1:
                browser.request_open(mount / "vnc", URL)
            elif ticks[0] == 3:
                handlers[signal.SIGTERM]()
        assert browser_runtime.main(mount, tmp_path / f"home-{kind}", Runtime, popen, sleep) == 0
        assert not (mount / "vnc" / browser.OPEN_REQUEST).exists()
        assert started == ["about:blank", URL]
        if kind == "chromium":  # its singleton hands it to the running one (a new tab): the first one stays
            assert sorted(stopped) == [("about:blank", 3), (URL, 3)]
        else:  # --no-remote: started again on it (not counted as a crash)
            assert stopped == [("about:blank", 1), (URL, 3)]


def test_live_regression_dsh_start_says_there_is_no_chat_and_opens_its_web_ui_in_the_session_browser(
        tmp_path, monkeypatch):
    """User: "ist das das einzige bei DeepSeek Harness, startet da kein Chat?" and "wie kann man den Link erreichen?".
    A real start of `sandbox-ai dsh --browser` (the pod replaced): its window's command is the script that says what
    DSH offers, the launcher serves the sandbox's browser socket (with --ask off too: no question), the script - the
    copy in the session directory - hands DSH's launch URL over it, the launcher puts exactly that URL into the
    session browser's folder, from where its runtime opens it; no port is published on the host (the web UI stays on
    the sandbox's loopback) and no log keeps the token."""
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    monkeypatch.setattr(s, "resolve_pod_hosts", lambda a, hosts: {host: ["203.0.113.5"] for host in hosts})
    monkeypatch.setattr(s, "on_machine", lambda a: False)
    b = s.load_browser_module()
    monkeypatch.setattr(s, "load_browser_module", lambda: b)
    monkeypatch.setattr(b, "prepare", lambda a, l=None, run=None: None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    seen = {}

    def run(a, session, home):
        seen["config"] = json.loads((session / "config.json").read_text())
        create, command = s.pod_commands(a, "agent-dsh-x", session, home)
        seen["podman"] = " ".join(create) + " " + " ".join(command)
        command = seen["config"]["command"]
        assert command[:3] == ["python3", "-I", dsh.WEB_SCRIPT]
        seen["dsh"] = command[command.index("--") + 1:]
        agent = load("dsh_web_in_session", str(session / "dsh" / "web.py"))  # the agent's side: that copy
        monkeypatch.setattr(agent.signal, "signal", lambda *args: None)
        out = Stream()
        short = Path(tempfile.mkdtemp(prefix="s")) / "o"  # (/sandbox/browser/open.sock in the sandbox: short)
        short.symlink_to(a.browser_dir / "open.sock")
        seen["status"] = agent.main(["--notice", command[command.index("--notice") + 1], "--open", "--socket",
                                     str(short), "--", *fake_dsh(tmp_path, [f"dsh web: {URL}"])], stream=out)
        shutil.rmtree(short.parent)
        request = a.browser_vnc_dir / b.OPEN_REQUEST
        seen["url"] = request.read_text().strip() if wait_for(request.exists) else None
        wait_for(lambda: "open in the session browser" in out.text())
        seen["out"] = out.text()
        return 0
    monkeypatch.setattr(s, "run_pod", run)
    work = tmp_path / "work"
    assert s.main(["dsh", "--workspace", str(work), "--no-auth", "--no-tmux", "--no-keep", "--browser",
                   "--ask", "off", "--", "web"]) == 0
    assert seen["dsh"][1:3] == ["web", "--patch"] and seen["dsh"][seen["dsh"].index("--host") + 1] == "127.0.0.1"
    assert seen["status"] == 0 and seen["url"] == URL, seen["out"]
    assert seen["out"].startswith("sandbox-ai: " + dsh.IN_BROWSER) and TOKEN not in seen["out"]
    assert "DeepSeek Harness' web UI is open in the session browser (the app's Browser tab)" in seen["out"]
    assert " -p " not in seen["podman"] and "--publish" not in seen["podman"] and "3080" not in seen["podman"]
    logs = "".join(p.read_text(errors="replace") for p in Path(os.environ["XDG_STATE_HOME"]).rglob("*.log"))
    assert TOKEN not in logs


def test_live_regression_e2e_the_real_entry_point_says_how_to_reach_dsh_and_publishes_no_port(tmp_path):
    """User: "ist das das einzige bei DeepSeek Harness, startet da kein Chat?" / "wie kann man den Link erreichen?".
    The real ./sandbox-ai (Podman faked, tests/live_fakes.py): the web UI (-- web) without --browser says how to
    reach it; with --browser that it opens in the session browser; the pod forwards no port of the host (pasta: none
    in either direction, no --publish), so the web UI is the sandbox's loopback only. (Since 0.1.96 a plain start is
    the chat in the terminal: test_live_regression_e2e_dsh_chats_in_the_terminal.)"""
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from live_fakes import Host
    finally:
        sys.path.remove(str(ROOT / "tests"))
    host = Host(tmp_path)
    plain = host.run("dsh", "--dry-run", "--", "web")
    assert plain.returncode == 0, plain.stderr
    assert f"agent-sandbox: note: {dsh.WEB_ONLY}" in plain.stderr
    assert json.loads(plain.stdout)["dsh"]["opens_in_session_browser"] is False
    seen = host.run("dsh", "--browser", "--dry-run", "--", "web")
    assert f"agent-sandbox: note: {dsh.IN_BROWSER}" in seen.stderr and dsh.WEB_ONLY not in seen.stderr
    assert json.loads(seen.stdout)["dsh"]["opens_in_session_browser"] is True
    headless = host.run("dsh", "--browser", "--dry-run", "--", "headless", "run the tests")
    assert dsh.IN_BROWSER not in headless.stderr and dsh.WEB_ONLY not in headless.stderr
    assert dsh.CHAT_AND_BROWSER not in headless.stderr
    host.add_image("localhost/sandbox-ai-browser:latest", "localhost/dsh-sandbox:latest",
                   labels={"sandbox-ai.role": "browser"})
    host.run("dsh", "--browser")
    create = host.stopped_at()
    assert create and create[:2] == ["pod", "create"], "the start got to its pod"
    network = create[create.index("--network") + 1]
    assert network.startswith("pasta:") and all(f"-{flag},none" in network for flag in "tuTU"), network
    assert not any(arg in ("-p", "--publish") or arg.startswith("--publish=") for call in host.calls() for arg in call)


# --- the chat in the terminal (sandbox-ai's, over DSH's headless profile) --------------------------------------------
#
# User (live, 0.1.96): "Hast du den DeepSeek Harness so gebaut, dass er jetzt auch erreichbar ist im Terminal?"
# DSH 0.2.1-rc.5 ships no terminal chat, but its headless profile keeps a conversation going from run to run
# (@deepseek-ai/dsh-headless, checked in its npm tarball against the lock's integrity: `--session-id ID` adopts the
# persisted Session, an unknown id fails; `--json` streams `session`, `status`, `thinking`, `text`, `tool_call`,
# `tool_result` as each step commits, then `final` or `error`; the task on standard input with "-"). sandbox-ai's
# chat (sandbox/dsh_chat.py, read-only at /etc/dsh/chat.py) runs it once per message: the default of `sandbox-ai dsh`.

chat_script = load("dsh_chat_tests", "sandbox/dsh_chat.py")
USER_ASKED = "Hast du den DeepSeek Harness so gebaut, dass er jetzt auch erreichbar ist im Terminal?"

FAKE_HEADLESS = r'''
import json, os, signal, sys, time
args = sys.argv[1:]
log, store = os.environ["FAKE_DSH_LOG"], os.environ["FAKE_DSH_SESSIONS"]
task = sys.stdin.read()
with open(log, "a") as stream:
    stream.write(json.dumps({"argv": args, "task": task, "pgid": os.getpgid(0), "pid": os.getpid(),
                             "mode": os.environ.get("DSH_PERMISSION_MODE")}) + "\n")
known = open(store).read().split() if os.path.exists(store) else []
session = args[args.index("--session-id") + 1] if "--session-id" in args else None
def say(**event):
    print(json.dumps(event), flush=True)
if session is not None and session not in known:
    message = f'session "{session}" does not exist; omit --session-id to start a new Session'
    say(type="error", message=message)
    print("dsh: " + message, file=sys.stderr, flush=True)
    sys.exit(1)
if session is None:
    session = f"session-{len(known) + 1}"
    with open(store, "a") as stream:
        stream.write(session + "\n")
say(type="session", sessionId=session, cwd=os.getcwd())
say(type="status", phase="turn_start", turn=1)
if task == "slow":
    def stop(*_):
        with open(log, "a") as stream:
            stream.write(json.dumps({"interrupted": session}) + "\n")
        sys.exit(130)
    signal.signal(signal.SIGINT, stop)
    say(type="text", text="working on it")
    for _ in range(600):
        time.sleep(0.05)
    sys.exit(0)
say(type="thinking", text="the user wrote " + task)
say(type="tool_call", callId="c1", tool="bash", input={"command": "touch /etc/x"})
say(type="tool_result", callId="c1", status="error", result="approval unavailable: refused")
answer = "echo: " + task.replace("\n", " | ")
say(type="text", text=answer)
say(type="status", phase="turn_end", turn=1, reason={"kind": "completed"})
say(type="final", text=answer)
'''


def fake_headless(tmp_path):
    """A stand-in `dsh` (its headless --json as dsh-headless 0.2.1-rc.5 writes it); its calls in calls.jsonl."""
    script = tmp_path / "fake-headless.py"
    script.write_text(FAKE_HEADLESS)
    binary = tmp_path / "dsh"
    binary.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    binary.chmod(0o755)
    env = {"FAKE_DSH_LOG": str(tmp_path / "calls.jsonl"), "FAKE_DSH_SESSIONS": str(tmp_path / "sessions.txt")}
    return binary, env


def calls(tmp_path):
    path = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r")


class Terminal:
    """A program in a pseudo-terminal of its own, as tmux runs it: its screen's text, keys sent to it."""

    def __init__(self, argv, env, cwd):
        import pty
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child: the program, the pty its controlling terminal (Ctrl+C reaches it)
            try:
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        self.seen, self.status = "", None

    def read(self, seconds=0.1):
        import select
        ready, _, _ = select.select([self.fd], [], [], seconds)
        if not ready:
            return False
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        self.seen += ANSI.sub("", data.decode("utf-8", "replace"))
        return bool(data)

    def expect(self, text, seconds=20, since=0):
        import time
        end = time.monotonic() + seconds
        while text not in self.seen[since:]:
            if time.monotonic() > end:
                raise AssertionError(f"{text!r} not on the screen:\n{self.seen[-3000:]}")
            self.read()
        return self.seen.index(text, since) + len(text)

    def send(self, keys):
        os.write(self.fd, keys.encode())

    def wait(self, seconds=20):
        import time
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.read(0.05)
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                while self.read(0.05):
                    pass
                self.status = os.waitstatus_to_exitcode(status)
                return self.status
        raise AssertionError(f"still running:\n{self.seen[-2000:]}")

    def close(self):
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except OSError:
                pass
        os.close(self.fd)


def chat_files(tmp_path, a=None):
    """The session directory's dsh/ as the launcher writes it (dsh.files), where /etc/dsh is in the sandbox."""
    a = a or ns()
    folder = tmp_path / "etc-dsh"
    folder.mkdir(exist_ok=True)
    for name, content in dsh.files(a, dsh.patch(a)).items():
        (folder / name.split("/", 1)[1]).write_text(content)
    return folder


def chat_env(tmp_path, env):
    home = tmp_path / "root"
    (home / ".dsh").mkdir(parents=True, exist_ok=True)
    return {"PATH": os.environ["PATH"], "HOME": str(home), "DSH_HOME": str(home / ".dsh"), "TERM": "dumb",
            "LC_ALL": "C.UTF-8", **env}


def test_dsh_chats_in_the_terminal_by_default():
    """No arguments (or chat): the chat's script runs DSH's headless profile for each message, with the chat's patch
    (DSH's credential store on, as in the web UI) and sandbox-ai's own last; --browser: the web UI beside it."""
    assert dsh.command_args([]) == ["chat"] and dsh.chat_profile([]) and dsh.chat_profile(["chat"])
    assert not dsh.chat_profile(["web"]) and not dsh.chat_profile(["headless", "x"])
    plain = dsh.command(ns(), "/usr/local/bin/dsh", dsh.command_args([]))
    assert plain == ["python3", "-I", dsh.CHAT_SCRIPT, "--notice", dsh.CHAT_NOTE, "--", "/usr/local/bin/dsh",
                     "headless", "--patch", dsh.CHAT_PATCH, "--patch", dsh.PATCH]
    both = dsh.command(ns(), "dsh", ["chat"], opens=True)
    assert both[both.index("--notice") + 1] == dsh.CHAT_AND_BROWSER
    assert json.loads(both[both.index("--web") + 1]) == ["dsh", "web", "--patch", dsh.PATCH, "--no-open", "--host",
                                                          "127.0.0.1", "--port", "3080"]
    assert both[both.index("--") + 1:] == ["dsh", *dsh.chat_args()]
    resumed = dsh.command(ns(args=["chat", "--session-id", "session-7"]), "dsh", ["chat", "--session-id", "session-7"])
    assert resumed[resumed.index("--session-id") + 1] == "session-7"
    assert dsh.chat_session(["chat", "--session-id=session-8"]) == "session-8"
    for wrong in (["chat", "--json"], ["chat", "hello"], ["chat", "--session-id"], ["chat", "--session-id", " "]):
        with pytest.raises(ValueError, match="not an option of the chat"):
            dsh.check(ns(args=wrong))
    for words in ("chat in this terminal", "headless", "session id", "/help", "Ctrl+C", "Approvals",
                  "refused", "-D", "--browser"):
        assert words in dsh.CHAT_NOTE, words
    assert "web UI in the session browser" in dsh.CHAT_AND_BROWSER and "of its own" in dsh.CHAT_AND_BROWSER
    assert dsh.note(ns(), opens=False) == dsh.CHAT_NOTE and dsh.note(ns(), opens=True) == dsh.CHAT_AND_BROWSER
    plan = dsh.plan(ns())
    assert plan["terminal_ui"] is True and plan["profile"] == "chat" and plan["web_ui"] is None
    assert plan["chat"] == {"script": dsh.CHAT_SCRIPT, "patch": dsh.CHAT_PATCH, "runs": dsh.chat_args(),
                            "session_id": None}
    assert dsh.plan(ns(), opens=True)["opens_in_session_browser"] is True
    files = dsh.files(ns(), dsh.patch(ns()))
    assert files["dsh/chat.py"] == (ROOT / "sandbox/dsh_chat.py").read_text()
    assert files["dsh/web.py"] == (ROOT / "sandbox/dsh_web.py").read_text(), "the web UI beside it (--browser)"
    assert json.loads(files["dsh/sandbox-ai-chat.patch.yml"]) == dsh.CHAT_ROWS
    assert dsh.CHAT_PATCH == dsh.MANAGED_DIR + "/sandbox-ai-chat.patch.yml" == "/etc/dsh/sandbox-ai-chat.patch.yml"
    assert dsh.chat_args().index(dsh.PATCH) == len(dsh.chat_args()) - 1, "sandbox-ai's patch stays the last word"
    assert {row["id"] for row in dsh.CHAT_ROWS}.isdisjoint(row["id"] for row in dsh.TELEMETRY_PINNED)


def test_the_chat_keeps_one_conversation_and_survives_ctrl_c(tmp_path):
    """The chat in a terminal against a stand-in dsh: every message one headless run with the message on standard
    input, the first run's session id passed on to every later one, tool calls and refused approvals shown, a longer
    text (a trailing backslash, a \"\"\" block), Ctrl+C cancels the answer (SIGINT to DSH's own process group) and the
    chat goes on, /new starts another conversation, Ctrl+D ends it; the history is in DSH's home."""
    binary, env = fake_headless(tmp_path)
    folder = chat_files(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    argv = [sys.executable, "-I", str(folder / "chat.py"), "--notice", dsh.CHAT_NOTE, "--", str(binary),
            *dsh.chat_args()]
    term = Terminal(argv, chat_env(tmp_path, env), work)
    try:
        term.expect(dsh.CHAT_NOTE[:60])
        mark = term.expect("dsh> ")
        term.send("hello there\r")
        mark = term.expect("echo: hello there", since=mark)
        assert "-> bash: touch /etc/x" in term.seen and "<- bash failed:" in term.seen
        assert "approval unavailable: refused" in term.seen and "the user wrote hello there" in term.seen
        mark = term.expect("conversation session-1", since=mark)
        mark = term.expect("dsh> ", since=mark)
        assert term.seen.count("echo: hello there") == 1, "the final event repeats the text: shown once"
        term.send("first line \\\r")
        term.expect("...> ", since=mark)
        term.send("-second line\r")
        mark = term.expect("echo: first line  | -second line", since=mark)
        mark = term.expect("dsh> ", since=mark)
        term.send('"""\r')
        term.send("a\r")
        term.send("--b\r")
        term.send('"""\r')
        mark = term.expect("echo: a | --b", since=mark)
        mark = term.expect("dsh> ", since=mark)
        term.send("slow\r")
        mark = term.expect("working on it", since=mark)
        term.send("\x03")
        mark = term.expect("answer cancelled; the conversation goes on", since=mark)
        mark = term.expect("dsh> ", since=mark)
        term.send("still here?\r")
        mark = term.expect("echo: still here?", since=mark)
        mark = term.expect("dsh> ", since=mark)
        term.send("\x03")
        mark = term.expect("/exit or Ctrl+D ends the chat", since=mark)
        term.send("/new\r")
        mark = term.expect("a new conversation starts", since=mark)
        term.send("fresh\r")
        mark = term.expect("conversation session-2", since=mark)
        mark = term.expect("dsh> ", since=mark)
        term.send("\x04")
        assert term.wait() == 0
        assert "sandbox-ai dsh -- chat --session-id session-2" in term.seen[mark:]
    finally:
        term.close()
    ran = calls(tmp_path)
    runs = [call for call in ran if "argv" in call]
    head = ["headless", "--patch", dsh.CHAT_PATCH, "--patch", dsh.PATCH, "--json"]
    assert [call["task"] for call in runs] == ["hello there", "first line \n-second line", "a\n--b", "slow",
                                               "still here?", "fresh"], "each message whole, on standard input"
    assert runs[0]["argv"] == [*head, "-"]
    for call in runs[1:5]:
        assert call["argv"] == [*head, "--session-id", "session-1", "-"], call
    assert runs[5]["argv"] == [*head, "-"], "/new: no session id"
    assert {"interrupted": "session-1"} in ran, "Ctrl+C reached DSH as SIGINT (its clean shutdown)"
    assert all(call["pgid"] == call["pid"] for call in runs), "DSH in a process group of its own"
    history = (tmp_path / "root/.dsh/sandbox-ai-chat-history").read_text()
    assert "hello there" in history and "still here?" in history


def test_the_chat_starts_anew_when_dsh_had_not_kept_the_conversation(tmp_path, capsys, monkeypatch):
    """A run that failed before DSH saved its Session: the next message's --session-id is unknown to DSH, which says
    so without running anything; the chat says it and sends the message to a new conversation, once. An error DSH
    writes only as an event (a usage error) is shown; one its stderr says already is not shown twice."""
    binary, env = fake_headless(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    chat = chat_script.Chat([str(binary), *dsh.chat_args()], chat_script.Out(sys.stdout), session="session-9")
    assert chat.send("x") == 0 and chat.session == "session-1"
    out = capsys.readouterr().out
    assert "conversation session-9 is not there" in out and out.count('session "session-9" does not exist') == 1
    first, second = (call["argv"] for call in calls(tmp_path))
    assert first[-3:] == ["--session-id", "session-9", "-"] and "--session-id" not in second
    turn = chat_script.Turn(chat_script.Out(sys.stdout))
    turn.render(b'{"type": "error", "message": "unknown option \'--x\'"}\n')
    turn.finish()
    assert "dsh: unknown option '--x'" in capsys.readouterr().out


def test_the_chat_opens_the_web_ui_beside_it_with_the_browser(tmp_path):
    """--browser: the chat starts the web UI in the background through its window script (beside it in /etc/dsh),
    which hands the launch URL to the session browser; its notices reach the chat, its log (token masked) goes to
    DSH's home; ending the chat ends the web UI."""
    binary, env = fake_headless(tmp_path)
    folder = chat_files(tmp_path)
    path = tmp_path / "open.sock"
    got = serve(path, lambda url: {"ok": True, "message": dsh.OPENED})
    web_ui = tmp_path / "fake-web.py"
    web_ui.write_text("import os, sys, time\n"
                      f"open({str(tmp_path / 'web.pid')!r}, 'w').write(str(os.getpid()))\n"
                      f"print('dsh web: {URL}', flush=True)\n"
                      "time.sleep(60)\n")
    argv = [sys.executable, "-I", str(folder / "chat.py"), "--notice", dsh.CHAT_AND_BROWSER, "--web",
            json.dumps([sys.executable, str(web_ui)]), "--socket", str(path), "--", str(binary), *dsh.chat_args()]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=chat_env(tmp_path, env), cwd=tmp_path)
    log = tmp_path / "root/.dsh/sandbox-ai-web.log"
    assert wait_for(lambda: got == [URL] and log.exists() and "?token=***" in log.read_text()), got
    out, err = process.communicate("/session\n", timeout=60)
    assert process.returncode == 0, err
    assert "web UI starts as well and opens in the session browser" in out and "no conversation yet" in out
    assert TOKEN not in log.read_text() and TOKEN not in out
    pid = int((tmp_path / "web.pid").read_text())

    def gone():
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except OSError:
            return True
    assert wait_for(gone), "the web UI ended with the chat"


def test_live_regression_e2e_dsh_chats_in_the_terminal(tmp_path):
    """User: "Hast du den DeepSeek Harness so gebaut, dass er jetzt auch erreichbar ist im Terminal?"
    The real ./sandbox-ai (Podman faked, tests/live_fakes.py): a plain `sandbox-ai dsh` says that its chat is in the
    terminal and starts its window with the chat's script, which the session directory holds with the chat's
    patch; that very copy, run as the sandbox runs it (the session's dsh/ as /etc/dsh, a stand-in dsh), answers a
    message in a terminal and continues the conversation with the next."""
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from live_fakes import Host
    finally:
        sys.path.remove(str(ROOT / "tests"))
    host = Host(tmp_path / "host")
    plain = host.run("dsh", "--dry-run")
    assert plain.returncode == 0, plain.stderr
    assert f"agent-sandbox: note: {dsh.CHAT_NOTE}" in plain.stderr
    plan = json.loads(plain.stdout)["dsh"]
    assert plan["terminal_ui"] is True and plan["profile"] == "chat" and plan["opens_in_session_browser"] is False
    seen = host.run("dsh", "--browser", "--dry-run")
    assert f"agent-sandbox: note: {dsh.CHAT_AND_BROWSER}" in seen.stderr
    assert json.loads(seen.stdout)["dsh"]["opens_in_session_browser"] is True
    host.add_image("localhost/dsh-sandbox:latest")
    done = host.run("dsh", "--no-tmux", "--no-auth", "-D",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
    assert done.returncode == 0, done.stderr
    session = tmp_path / "kept/session"
    config = json.loads((session / "config.json").read_text())
    command = config["command"]
    assert command[:5] == ["python3", "-I", dsh.CHAT_SCRIPT, "--notice", dsh.CHAT_NOTE]
    sandbox_env = {name: config["env"][name] for name in ("DSH_HOME", "DSH_PERMISSION_MODE", *dsh.TELEMETRY_ENV)}
    assert sandbox_env["DSH_PERMISSION_MODE"] == "danger-full-access" and sandbox_env["DSH_TELEMETRY_DISABLED"] == "1"
    tail = command[command.index("--") + 1:]
    assert tail[0].endswith("dsh") and tail[1:] == dsh.chat_args()
    assert (session / "dsh/chat.py").read_text() == (ROOT / "sandbox/dsh_chat.py").read_text()
    assert json.loads((session / "dsh/sandbox-ai-chat.patch.yml").read_text()) == dsh.CHAT_ROWS
    assert json.loads((session / "dsh/sandbox-ai.patch.yml").read_text())[-2:] == dsh.TELEMETRY_PINNED
    # The window's command as the sandbox runs it: /etc/dsh is the session's dsh/, dsh a stand-in.
    binary, env = fake_headless(tmp_path)
    argv = [sys.executable if word == "python3" else str(session / "dsh/chat.py") if word == dsh.CHAT_SCRIPT
            else str(binary) if word == tail[0] else word for word in command]
    work = tmp_path / "work"
    work.mkdir()
    term = Terminal(argv, {**chat_env(tmp_path, env), **{name: value for name, value in sandbox_env.items()
                                                         if name != "DSH_HOME"}}, work)
    try:
        mark = term.expect("dsh> ")
        term.send(USER_ASKED + "\r")
        mark = term.expect("echo: " + USER_ASKED, since=mark)
        mark = term.expect("dsh> ", since=mark)
        term.send("und weiter\r")
        mark = term.expect("echo: und weiter", since=mark)
        term.expect("dsh> ", since=mark)
        term.send("/exit\r")
        assert term.wait() == 0
    finally:
        term.close()
    runs = calls(tmp_path)
    assert runs[0]["argv"] == [*dsh.chat_args(), "--json", "-"] and runs[0]["task"] == USER_ASKED
    assert runs[1]["argv"] == [*dsh.chat_args(), "--json", "--session-id", "session-1", "-"]
    assert all(run["mode"] == "danger-full-access" for run in runs), "-D reaches every message's DSH"
