"""The live checks the development sandbox cannot run (no Podman, no user namespaces there): opt-in with --agent-live,
run on a real machine by scripts/live_check.sh (docs/LIVE_CHECK.md), which says per check what it proves.

Each test runs sandbox-ai with folders of its own (HOME, XDG configuration, data, state, cache, runtime; plain-file
secrets): the configuration, sessions, kits and secrets of whoever runs them are never read or changed. Podman alone
sees the real HOME (scripts/live_gaps.py's wrapper first on PATH), so the images built there are used. Every session
a test starts is stopped and deleted at its end. Inputs from the script (all optional, a test without its input is
skipped and says what it needs): SAI_LIVE_TOKEN (a made-up token), SAI_LIVE_DEVICE, SAI_LIVE_HOST +
SAI_LIVE_HOSTS_DIR, SAI_LIVE_LLM_ENDPOINT + SAI_LIVE_LLM_MODELS, SAI_LIVE_REAL_HOME."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
sys.path.insert(0, str(Path(__file__).parent))
from test_agent_sandbox_live import backend  # noqa: E402,F401 - the backend fixture (--agent-backend)

pytestmark = pytest.mark.agent_live
_spec = importlib.util.spec_from_file_location("live_gaps_for_tests", ROOT / "scripts" / "live_gaps.py")
LG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LG)
SLEEP = ["--binary", "/usr/bin/python3", "--", "-I", "-c", "import time; time.sleep(3600)"]


def token():
    return os.environ.get("SAI_LIVE_TOKEN") or "sai-lc-" + secrets.token_hex(16)


def environment(tmp_path, **more):
    """The environment of this test's sandbox-ai: folders of its own below TMP_PATH/e (short: socket paths)."""
    env = LG.isolated_environment(tmp_path / "e", os.environ)
    env.update(more)
    return env


def sai(env, *args, timeout=180, input=None):
    return subprocess.run([str(SAI), *map(str, args)], capture_output=True, text=True, env=env, timeout=timeout,
                          input=input, stdin=None if input is not None else subprocess.DEVNULL)


def said(done):
    return f"exit {done.returncode}\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def once(request, backend, agent, work, options, args, env, timeout=240):
    """A one-shot start (sandbox-ai AGENT ... -- ARGS) in the foreground; (stdout, stderr), exit 0 asserted."""
    command = [str(SAI), agent, "--backend", backend, "--workspace", str(work), *options, "--", *args]
    if backend == "podman" and request.config.getoption("--agent-image"):
        command[2:2] = ["--image", request.config.getoption("--agent-image")]
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        pytest.fail(f"{backend}/{agent} timed out after {timeout} s; stderr: {stderr[-3000:]}")
    assert process.returncode == 0, f"{backend}/{agent} exit {process.returncode}\n{stdout[-3000:]}\n{stderr[-3000:]}"
    return stdout, stderr


class Session:
    """A kept session (sandbox-ai AGENT --session NAME ...) of this test, stopped and deleted at its end."""

    def __init__(self, request, backend, tmp_path, agent="codex", options=(), env=None, work=None):
        self.env = env or environment(tmp_path)
        self.name = "lc-" + uuid.uuid4().hex[:6]
        self.work = work or tmp_path / "w"
        self.work.mkdir(exist_ok=True)
        self.command = [str(SAI), agent, "--backend", backend, "--workspace", str(self.work), "--no-auth",
                        "--session", self.name, *options]
        if backend == "podman" and request.config.getoption("--agent-image"):
            self.command[2:2] = ["--image", request.config.getoption("--agent-image")]
        self.started = False

    def start(self, timeout=300):
        done = subprocess.run(self.command, capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL,
                              timeout=timeout)
        assert done.returncode == 0, "the session did not start: " + said(done)
        self.started = True
        return done

    def sai(self, *args, timeout=180, input=None):
        return sai(self.env, *args, timeout=timeout, input=input)

    def shell(self, script, timeout=180):
        """SCRIPT run by sh in the sandbox (sandbox-ai shell: under exactly the agent's rules)."""
        return self.sai("shell", self.name, "-n", "--", "sh", "-c", script, timeout=timeout)

    def python(self, code, *args, timeout=180):
        return self.sai("shell", self.name, "-n", "--", "python3", "-I", "-c", code, *args, timeout=timeout)

    def close(self):
        if self.started:
            self.sai("session", "stop", self.name, timeout=240)
            self.sai("session", "delete", self.name, "--yes", timeout=120)


@pytest.fixture
def sessions():
    made = []
    yield made
    for session in made:
        session.close()


def session(sessions, *args, **kwargs):
    made = Session(*args, **kwargs)
    sessions.append(made)
    return made


def answer(text, marker):
    line = next((line for line in text.splitlines() if line.startswith(marker + " ")), None)
    assert line is not None, f"no {marker} line in: {text[-3000:]}"
    return json.loads(line[len(marker) + 1:])


# --- Firejail hides the proxy's and the supervisor's folders (credentials-verify A/B: read, not run) ----------------

PRIVATE_VIEW = r'''
import errno, json, os, sys, time
seen = {}
def look(path, kind):
    try:
        if kind == "dir":
            seen[path] = {"entries": len(os.listdir(path))}
        else:
            with open(path, "rb"):
                seen[path] = "readable"
    except OSError as exc:
        seen[path] = errno.errorcode.get(exc.errno, str(exc.errno))
for path in ("/sandbox/proxy", "/sandbox/control", "/sandbox/log", "/run/sandbox-proxy", "/sandbox/session"):
    look(path, "dir")
look("/sandbox/proxy/proxy.json", "file")
text = "VIEW " + json.dumps(seen)
if len(sys.argv) > 1:
    open(sys.argv[1], "w").write(text + "\n")
    time.sleep(3600)
print(text)
'''
HIDDEN = ("EACCES", "EPERM")


def hidden(seen, path):
    return seen[path] in HIDDEN or seen[path] == "ENOENT" or seen[path] == {"entries": 0}


def test_live_firejail_hides_the_proxy_and_control_folders(request, backend, tmp_path, sessions):
    """The agent's process and `sandbox-ai shell` cannot list /sandbox/proxy (the proxy's logins and keys) nor
    /sandbox/control (the supervisor's), nor read the proxy's proxy.json; /sandbox/session (the agent's own) is
    readable. Before this ran live, Firejail's --blacklist of them (runtime.firejail_command) was only read."""
    # (not --offline: the connection proxy runs, so its folder is there to be hidden; nothing is allowed to leave)
    started = session(sessions, request, backend, tmp_path, options=[
        "--no-provider-defaults", "--binary", "/usr/bin/python3", "--", "-I", "-c", PRIVATE_VIEW, "/work/view.txt"])
    started.start()
    view = started.work / "view.txt"
    for _ in range(60):
        if view.exists() and view.read_text().endswith("\n"):
            break
        time.sleep(1)
    agent = answer(view.read_text(), "VIEW")
    shell = answer(started.python(PRIVATE_VIEW).stdout, "VIEW")
    for seen, who in ((agent, "the agent"), (shell, "sandbox-ai shell")):
        # there (the proxy runs: its folder holds proxy.json), and blacklisted: unreachable, or Firejail's empty
        # read-only stand-in (root's, mode 0400), which the agent - root in the sandbox - may list
        assert hidden(seen, "/sandbox/proxy") and seen["/sandbox/proxy"] != "ENOENT", (who, seen)
        assert hidden(seen, "/sandbox/control"), (who, seen)
        assert hidden(seen, "/sandbox/log") and hidden(seen, "/run/sandbox-proxy"), (who, seen)
        assert seen["/sandbox/proxy/proxy.json"] != "readable", (who, seen)
        assert isinstance(seen["/sandbox/session"], dict), (who, seen)
    print("agent sees:", json.dumps(agent), "\nshell sees:", json.dumps(shell))


# --- a login's server opened, no other host (credentials-verify B: nftables was a list) ----------------------------

REACH = r'''
import json, socket, ssl, sys, urllib.error, urllib.request
found = {}
for host in sys.argv[1:]:
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{host}/", method="HEAD"), timeout=25) as reply:
            found[host] = f"HTTP {reply.status}"
    except urllib.error.HTTPError as exc:
        found[host] = f"HTTP {exc.code}"
    except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
        found[host] = "refused: " + str(getattr(exc, "reason", exc))[:200]
print("REACH " + json.dumps(found))
'''


@pytest.mark.parametrize("proxy", ["proxy", "no-proxy"])
def test_live_a_logins_server_is_opened_and_no_other_host(request, backend, tmp_path, proxy):
    """--git-token-env https://codeberg.org=VAR opens codeberg.org (TCP/443) and nothing else: gitlab.com stays shut,
    with the connection proxy (the default) and with the firewall alone (--no-proxy). The token is made up and never
    sent (only a HEAD of the front page)."""
    work = tmp_path / "w"
    work.mkdir()
    env = environment(tmp_path, SAI_LC_GIT_TOKEN=token())
    options = ["--no-auth", "--no-provider-defaults", "--git-token-env", "https://codeberg.org=SAI_LC_GIT_TOKEN",
               *(["--no-proxy"] if proxy == "no-proxy" else []), "--binary", "/usr/bin/python3"]
    stdout, stderr = once(request, backend, "codex", work, options,
                          ["-I", "-c", REACH, "codeberg.org", "gitlab.com"], env, timeout=240)
    reached = answer(stdout, "REACH")
    print(proxy, json.dumps(reached))
    assert reached["codeberg.org"].startswith("HTTP "), (reached, stderr[-2000:])
    assert reached["gitlab.com"].startswith("refused"), (reached, stderr[-2000:])


# --- a Git login given while the sandbox runs (credentials-verify A/B: no live container) -----------------------------

def git_server(tmp_path, secret):
    """git http-backend on the host's loopback that wants a Basic login whose password is SECRET (any user):
    (port, bare repository, server)."""
    import test_proxy

    class Handler(test_proxy.GitHandler):
        root = tmp_path / "server"

        def handle_git(self):
            header = self.headers.get("Authorization", "")
            given = ""
            if header.startswith("Basic "):
                try:
                    given = base64.b64decode(header[6:]).decode().partition(":")[2]
                except ValueError:
                    given = ""
            if given != secret:
                body = b"login wanted\n"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="live check"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            test_proxy.GitHandler.handle_git(self)

        do_GET = do_POST = handle_git

    bare = Handler.root / "repo.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True)
    server = test_proxy.serve(Handler)
    return server.server_address[1], bare, server


def test_live_a_git_login_given_while_it_runs_reaches_git_until_revoked(request, backend, tmp_path, sessions):
    """git-logins give RUN URL --env VAR: the running sandbox's git push to a server that wants a login fails
    before, works after the give, fails again after the revoke; no listing shows the token."""
    if backend != "podman":
        pytest.skip("the host's loopback is reachable only through Podman's --allow-host-port")
    secret = token()
    port, bare, server = git_server(tmp_path, secret)
    url = f"http://127.0.0.1:{port}"
    env = environment(tmp_path, SAI_LC_GIT_TOKEN=secret)
    started = session(sessions, request, backend, tmp_path, env=env, options=[
        "--no-provider-defaults", "--allow-host-port", str(port), "--ask", "off", *SLEEP])
    push = (f"cd /work && rm -rf r && git init -q -b main r && cd r && git config user.name Tester && "
            f"git config user.email tester@example.org && echo x > f && git add f && git commit -q -m x && "
            f"if GIT_TERMINAL_PROMPT=0 git push -q {url}/repo.git HEAD:refs/heads/STEP </dev/null; "
            f"then echo PUSHED; else echo REFUSED; fi")
    try:
        started.start()
        before = started.shell(push.replace("STEP", "before"))
        assert "REFUSED" in before.stdout, said(before)
        given = started.sai("git-logins", "give", started.name, url, "--env", "SAI_LC_GIT_TOKEN", "--user", "alice")
        assert given.returncode == 0, said(given)
        listed = started.sai("git-logins", "list", "--run", started.name, "--json")
        assert listed.returncode == 0 and secret not in listed.stdout + listed.stderr, said(listed)
        assert secret not in given.stdout + given.stderr
        after = started.shell(push.replace("STEP", "given"))
        assert "PUSHED" in after.stdout, said(after)
        revoked = started.sai("git-logins", "revoke", started.name, url)
        assert revoked.returncode == 0, said(revoked)
        again = started.shell(push.replace("STEP", "revoked"))
        assert "REFUSED" in again.stdout, said(again)
    finally:
        server.shutdown()
    heads = subprocess.run(["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"], capture_output=True,
                           text=True).stdout.split()
    assert heads == ["refs/heads/given"], heads


# --- --hide for a workspace added while the sandbox runs (knostic-gaps 5) ---------------------------------------------

READ_FILES = r'''
import errno, json, sys
seen = {}
for path in sys.argv[1:]:
    try:
        seen[path] = open(path).read()
    except OSError as exc:
        seen[path] = errno.errorcode.get(exc.errno, str(exc.errno))
    try:
        with open(path, "a") as stream:
            stream.write("changed")
        seen[path + " (write)"] = "written"
    except OSError as exc:
        seen[path + " (write)"] = errno.errorcode.get(exc.errno, str(exc.errno))
print("FILES " + json.dumps(seen))
'''


def test_live_hide_covers_a_workspace_added_while_it_runs(request, backend, tmp_path, sessions):
    """--hide .env --hide '*.pem': a folder given with `sandbox-ai workspace add` while the sandbox runs has its
    .env and key.pem empty and read-only in the sandbox, ok.txt as it is; the host's files keep their text. The
    start's own workspace is the control."""
    started = session(sessions, request, backend, tmp_path, options=[
        "--offline", "--hide", ".env", "--hide", "*.pem", *SLEEP])
    (started.work / ".env").write_text("START_FIXTURE=1\n")
    extra = tmp_path / "extra"
    (extra / "sub").mkdir(parents=True)
    (extra / ".env").write_text("ADDED_FIXTURE=1\n")
    (extra / "sub" / "key.pem").write_text("PEM FIXTURE\n")
    (extra / "ok.txt").write_text("visible\n")
    started.start()
    added = started.sai("workspace", "add", started.name, str(extra), "--name", "extra")
    assert added.returncode == 0, said(added)
    paths = ["/work/.env", "/workspaces/extra/.env", "/workspaces/extra/sub/key.pem"]
    probe = started.python(READ_FILES, *paths)
    seen = answer(probe.stdout, "FILES")
    for path in paths:
        assert seen[path] == "", (path, seen)
        assert seen[path + " (write)"] in ("EROFS", "EACCES", "EPERM"), (path, seen)
    readable = started.python(r"print('OK ' + open('/workspaces/extra/ok.txt').read().strip())")
    assert "OK visible" in readable.stdout, said(readable)
    assert (extra / ".env").read_text() == "ADDED_FIXTURE=1\n" and (extra / "sub" / "key.pem").read_text() == \
        "PEM FIXTURE\n" and (started.work / ".env").read_text() == "START_FIXTURE=1\n"


# --- --untrusted (Landlock) with a real HOME (v0.1.99: a crowded or linked HOME) -------------------------------------

CROWD = r'''
import os, pathlib
home = pathlib.Path.home()
for index in range(10500):
    (home / f"lc-{index:05d}").touch()
(home / "elsewhere" / "glab").mkdir(parents=True, exist_ok=True)
(home / "elsewhere" / "glab" / "config.yml").write_text("FIXTURE-GLAB-LOGIN\n")
(home / ".config").mkdir(exist_ok=True)
if not os.path.lexists(home / ".config" / "glab-cli"):
    os.symlink(home / "elsewhere" / "glab", home / ".config" / "glab-cli")
print("LINKED", os.path.islink(home / ".config" / "glab-cli"))
(home / ".codex").mkdir(exist_ok=True)
(home / ".codex" / "auth.json").write_text("FIXTURE-CODEX-LOGIN\n")
print("MADE", home)
'''


def test_live_untrusted_with_a_crowded_and_linked_home_in_a_sandbox(request, backend, tmp_path, sessions):
    """In a real sandbox: a HOME of more than 10000 entries, ~/.config/glab-cli a link to ~/elsewhere/glab, ~/.codex
    with a made-up login (not ~/.config/gh: a canary of the sandbox lies there, a folder, so no link could be made). A command run with --untrusted reads neither login (by the link nor where it really lies), runs
    at all (it once refused a crowded HOME), and does so in reasonable time; --check names the real folder."""
    started = session(sessions, request, backend, tmp_path, options=["--offline", *SLEEP])
    started.start()
    made = started.python(CROWD, timeout=300)
    assert "MADE" in made.stdout and "LINKED True" in made.stdout, said(made)
    check = started.shell("python3 -I /sandbox/runtime.py --untrusted --check")
    assert check.returncode == 0, said(check)
    plan = json.loads(check.stdout.strip().splitlines()[-1])
    home = made.stdout.split("MADE", 1)[1].strip().splitlines()[0]
    assert plan["landlock"] >= 1, plan
    assert f"{home}/elsewhere/glab" in plan["protected"] and f"{home}/.config/glab-cli" in plan["protected"], plan
    begin = time.monotonic()
    confined = started.shell("python3 -I /sandbox/runtime.py --untrusted -- sh -c 'cat ~/.config/glab-cli/config.yml; "
                             "cat ~/elsewhere/glab/config.yml; cat ~/.codex/auth.json; echo CONFINED_DONE'", timeout=300)
    spent = time.monotonic() - begin
    text = confined.stdout + confined.stderr
    assert "CONFINED_DONE" in confined.stdout, said(confined)
    assert "FIXTURE-GLAB-LOGIN" not in text and "FIXTURE-CODEX-LOGIN" not in text, said(confined)
    assert spent < 120, f"--untrusted took {spent:.0f} s with a crowded HOME"
    print(f"--untrusted with a crowded HOME: {spent:.1f} s; unreadable: {plan.get('unreadable')}")


HOST_PROBE = r'''
import errno, json, os, sys
seen = {}
for path in sys.argv[1:]:
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            os.close(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY))  # opened, never read
        seen[path] = "opened"
    except OSError as exc:
        seen[path] = errno.errorcode.get(exc.errno, str(exc.errno))
print("PROBE " + json.dumps(seen))
'''


def test_live_untrusted_with_this_hosts_real_home(tmp_path):
    """runtime.py --untrusted on this host with your real HOME (its size, its links such as /home -> /var/home):
    --check works, and a confined command can open none of the credential and agent folders that exist there. Only
    whether each one opens is printed - never a name inside, never a byte."""
    if sys.platform != "linux":
        pytest.skip("Landlock is Linux")
    home = os.environ.get("SAI_LIVE_REAL_HOME") or os.path.expanduser("~")
    spec = importlib.util.spec_from_file_location("runtime_for_live_gaps", ROOT / "sandbox" / "runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    there = [os.path.join(home, name) for name in runtime.UNTRUSTED_HOME if os.path.lexists(os.path.join(home, name))]
    env = {"HOME": home, "PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    command = [sys.executable, "-I", str(ROOT / "sandbox" / "runtime.py"), "--untrusted"]
    begin = time.monotonic()
    check = subprocess.run([*command, "--check"], capture_output=True, text=True, env=env, timeout=300)
    assert check.returncode == 0, said(check)
    plan = json.loads(check.stdout.strip().splitlines()[-1])
    probe = subprocess.run([*command, "--", sys.executable, "-I", "-c", HOST_PROBE, *there], capture_output=True,
                           text=True, env=env, timeout=300, cwd=str(tmp_path))
    spent = time.monotonic() - begin
    assert probe.returncode == 0, said(probe)
    seen = answer(probe.stdout, "PROBE")
    print(f"Landlock ABI {plan['landlock']}; {len(there)} credential or agent folders in HOME; {spent:.1f} s; "
          f"unreadable: {len(plan.get('unreadable') or {})} folder(s) cut")
    opened = [path for path, state in seen.items() if state == "opened"]
    assert not opened, f"a confined command opened: {opened}"


# --- kits attach and plugins attach into a running Claude Code sandbox (runtime-kits) -----------------------------

def test_live_kits_attach_reaches_a_running_claude_sandbox(request, backend, tmp_path, sessions):
    """sandbox-ai kits attach --skill / --command to a running claude sandbox: the files are at the agent's place
    at once (Claude's layout), cannot be changed from inside, and kits detach takes the skill away. Claude Code's
    own take-up of them needs a model: not part of this check."""
    started = session(sessions, request, backend, tmp_path, agent="claude", options=["--offline", *SLEEP])
    skill = tmp_path / "skills" / "lc-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lc-skill\ndescription: a skill of the live check\n---\nSay hello.\n")
    command = tmp_path / "lc-command.md"
    command.write_text("---\ndescription: a command of the live check\n---\nSay hello.\n")
    started.start()
    given = started.sai("kits", "attach", started.name, "--skill", str(skill), "--json")
    assert given.returncode == 0, said(given)
    places = {entry["name"]: entry["path"] for entry in json.loads(given.stdout)["attached"]}
    assert "lc-skill" in places, given.stdout
    given = started.sai("kits", "attach", started.name, "--command", str(command), "--json")
    assert given.returncode == 0, said(given)
    places.update({entry["name"]: entry["path"] for entry in json.loads(given.stdout)["attached"]})
    command_place = next(path for name, path in places.items() if name != "lc-skill")
    inside = started.shell(f"cat {places['lc-skill']}/SKILL.md && cat {command_place} && "
                           f"(touch {places['lc-skill']}/new 2>/dev/null && echo WRITABLE || echo READ_ONLY) && "
                           f"(rm -f {places['lc-skill']}/SKILL.md 2>/dev/null && echo REMOVED || echo KEPT)")
    assert "name: lc-skill" in inside.stdout and "a command of the live check" in inside.stdout, said(inside)
    assert "READ_ONLY" in inside.stdout and "KEPT" in inside.stdout, said(inside)
    taken = started.sai("kits", "detach", started.name, "lc-skill", "--json")
    assert taken.returncode == 0, said(taken)
    gone = started.shell(f"test -e {places['lc-skill']}/SKILL.md && echo STILL_THERE || echo GONE")
    assert "GONE" in gone.stdout, said(gone)


def test_live_plugins_attach_enables_a_plugin_in_a_running_claude(request, backend, tmp_path, sessions):
    """sandbox-ai plugins attach RUN sandcastle@sandbox-ai (the built-in marketplace, from this checkout: no
    network): installed into the plugin store by its own sandbox, enabled in the running claude sandbox's settings,
    its files there; plugins detach turns it off. The store exists at the start (a marketplace in it), as it does
    for anyone who installed a plugin before."""
    env = environment(tmp_path)
    for step in (["kits", "new", "lcplugins", "--agent", "claude"],
                 ["kits", "add-marketplace", "lcplugins", "sandbox-ai", "sandbox-ai", "--agent", "claude"]):
        done = sai(env, *step)
        assert done.returncode == 0, (step, said(done))
    fetched = sai(env, "plugins", "claude", "--marketplaces-only", timeout=900)
    assert fetched.returncode == 0, said(fetched)
    # (no plugin installed yet, so the store has no cache folder: the start makes it, else attach could not work)
    started = session(sessions, request, backend, tmp_path, agent="claude", env=env,
                      options=["--offline", "--kit", "lcplugins", *SLEEP])
    started.start()
    given = started.sai("plugins", "attach", started.name, "sandcastle@sandbox-ai", "--json", timeout=900)
    assert given.returncode == 0, said(given)
    result = json.loads(given.stdout)
    assert result["attached"] == "live", result
    inside = started.shell("cat ~/.claude/settings.json")
    assert inside.returncode == 0, said(inside)
    settings = json.loads(inside.stdout)
    assert settings["enabledPlugins"].get("sandcastle@sandbox-ai") is True, settings
    files = started.shell("ls ~/.claude/plugins/cache")
    assert "sandbox-ai" in files.stdout.split(), said(files)
    taken = started.sai("plugins", "detach", started.name, "sandcastle@sandbox-ai")
    assert taken.returncode == 0, said(taken)
    after = started.shell("cat ~/.claude/settings.json")
    settings = json.loads(after.stdout)
    assert settings["enabledPlugins"].get("sandcastle@sandbox-ai") is False, settings


# --- devices (board-devices): the scan on real hardware, a device added and removed live ------------------------------

def test_live_devices_scan_on_this_machine(tmp_path):
    """sandbox-ai devices scan on this machine: JSON with the documented fields, every node it lists exists,
    every refused entry says why; the text form works too. What it found is printed (kinds and counts)."""
    env = environment(tmp_path)
    done = sai(env, "devices", "scan", "--json", timeout=120)
    assert done.returncode == 0, said(done)
    scan = json.loads(done.stdout)
    assert isinstance(scan["devices"], list) and isinstance(scan["notes"], list)
    kinds = {}
    for entry in scan["devices"]:
        assert {"kind", "value", "label", "nodes", "option", "refused"} <= set(entry), entry
        assert entry["option"] in ("--device", "--gpu"), entry
        for node in entry["nodes"]:
            assert os.path.exists(node), (node, entry)
        assert entry["refused"] is None or (isinstance(entry["refused"], str) and entry["refused"]), entry
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    text = sai(env, "devices", "scan", timeout=120)
    assert text.returncode == 0, said(text)
    print("devices scan:", json.dumps(kinds, sort_keys=True), "- devices-live would take:",
          LG.pick_device(scan) or "none")


def test_live_a_device_added_and_removed_while_it_runs(request, backend, tmp_path, sessions):
    """sandbox-ai devices add RUN DEVICE: the device's node appears in the running sandbox and devices list says
    attached; devices remove takes it away again. DEVICE: SAI_LIVE_DEVICE (the script's --device), else the first
    serial port, camera, render node or /dev/kvm devices scan offers."""
    env = environment(tmp_path)
    device = os.environ.get("SAI_LIVE_DEVICE")
    if not device:
        done = sai(env, "devices", "scan", "--json", timeout=120)
        device = LG.pick_device(json.loads(done.stdout)) if done.returncode == 0 else None
    if not device:
        pytest.skip("no device to hand over: give scripts/live_check.sh --device PATH")
    node = os.path.realpath(device) if device.startswith("/dev/") else None
    started = session(sessions, request, backend, tmp_path, env=env, options=["--offline", *SLEEP])
    started.start()
    probe = f"test -e {node} && echo PRESENT || echo ABSENT" if node else "echo PRESENT"
    if node:
        assert "ABSENT" in started.shell(probe).stdout, f"{node} was in the sandbox before it was added"
    added = started.sai("devices", "add", started.name, device)
    assert added.returncode == 0, said(added)
    assert "PRESENT" in started.shell(probe).stdout, f"{device} not in the sandbox after devices add"
    listed = started.sai("devices", "list", started.name, "--json")
    assert listed.returncode == 0 and "attached" in listed.stdout, said(listed)
    removed = started.sai("devices", "remove", started.name, device)
    assert removed.returncode == 0, said(removed)
    if node:
        assert "ABSENT" in started.shell(probe).stdout, f"{device} still in the sandbox after devices remove"
    print(f"device {device}: added, seen, removed")


# --- signed updates from the real GitHub (update-live) --------------------------------------------------------------

GITHUB_RELEASES = "https://api.github.com/repos/c0decave/sandbox-ai/releases/latest"


def github_get(url, limit=64 << 20):
    """URL's bytes from GitHub, read-only and without a token (a public repository needs none)."""
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "sandbox-ai-live-check",
                                                   "Accept": "application/octet-stream" if "/download/" in url
                                                   else "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read(limit)


def test_live_update_check_against_github(tmp_path):
    """update-live: the newest release on GitHub is signed by a key of this checkout's sandbox/release-signers, lists
    every kind's file in its signed SHA256SUMS (and the bundle has the SHA-256 listed there); this checkout's
    `sandbox-ai update --check` finds it and offers it only when it is newer (never a downgrade); a copy of this
    checkout one version older than the release is offered it, signed by that key. Nothing is installed."""
    sys.path.insert(0, str(ROOT / "tests"))
    import release_kit as kit
    assets = kit.assets
    keys = kit.load(ROOT / "sandbox" / "release_keys.py", "release_keys_for_update_live")
    document = json.loads(github_get(GITHUB_RELEASES))
    latest = document["tag_name"].lstrip("v")
    links = {asset["name"]: asset["browser_download_url"] for asset in document["assets"]}
    assert {"SHA256SUMS", "SHA256SUMS.sig", assets.bundle(latest)} <= set(links), sorted(links)
    sums, signature = github_get(links["SHA256SUMS"], 1 << 20), github_get(links["SHA256SUMS.sig"], 1 << 16)
    trusted = keys.load(ROOT / "sandbox" / "release-signers")
    verdict, signer = keys.verify(sums, signature, trusted)
    assert verdict == "good", f"SHA256SUMS of {latest}: {signer}"
    listed = assets.checksums(sums.decode())
    assert assets.bundle_version(listed) == latest
    for kind, machine in (("user", None), ("pip", None), ("pyz", None), ("appimage", "x86_64"),
                          ("appimage", "aarch64"), ("macos-app", "arm64"), ("macos-app", "x86_64")):
        name = assets.asset_for(kind, latest, machine)
        assert name in links and name in listed, (kind, machine, name)
    bundle = github_get(links[assets.bundle(latest)])
    assert hashlib.sha256(bundle).hexdigest() == listed[assets.bundle(latest)], "the bundle is the one signed"
    # this checkout: what `update --check` says, and update mode's verdict
    env = environment(tmp_path)
    env.pop("SANDBOX_AI_TOKEN", None)
    done = sai(env, "update", "--check", timeout=300)
    assert done.returncode == 0, said(done)
    assert f"available: {latest} (https://github.com/c0decave/sandbox-ai)" in done.stdout, said(done)
    assert f"signature: good, by the trusted key {signer}" in done.stdout, said(done)
    status = json.loads(sai(env, "update", "mode", "--json").stdout)
    installed = status["installed"]
    newer = kit.load(ROOT / "sandbox" / "updates.py", "updates_for_update_live").newer(latest, installed)
    assert status["result"]["newer"] is newer, status["result"]
    # a copy one version before the release: offered it
    major, minor, patch = (int(part) for part in re.match(r"(\d+)\.(\d+)\.(\d+)", latest).groups())
    before = f"{major}.{minor}.{patch - 1}" if patch else f"{major}.{minor - 1}.999" if minor else "0.0.1"
    tree = kit.tree_copy(tmp_path / "older", before, [])
    shutil.copy2(ROOT / "sandbox" / "release-signers", tree / "sandbox" / "release-signers")
    kit.commit(tree, "the release keys of this checkout")
    older = subprocess.run([str(tree / "sandbox-ai"), "update", "--check"], capture_output=True, text=True,
                           env=environment(tmp_path / "o"), timeout=300, stdin=subprocess.DEVNULL)
    assert older.returncode == 0, said(older)
    assert f"installed: {before}" in older.stdout and f"available: {latest} " in older.stdout, said(older)
    assert f"signature: good, by the trusted key {signer}" in older.stdout, said(older)
    print(f"GitHub's newest release: {latest}, signed by {signer}; this checkout {installed}: "
          + ("offered" if newer else "up to date (not offered)") + f"; a copy of {before}: offered {latest}")


# --- a bridge between two real sessions (the bridge check of 2026-11-22: "check in-depth if bridge is now working") ---

BRIDGE_CLIENT = r"""
import json, subprocess, sys
server, calls = sys.argv[1], json.loads(sys.argv[2])
bridge = subprocess.Popen(["python3", "-I", "/sandbox/runtime.py", "--internal-mcp-bridge",
                           f"/run/sandbox-mcp/{server}/server.sock"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True)
def call(message):
    bridge.stdin.write(json.dumps(message) + "\n"); bridge.stdin.flush()
    return json.loads(bridge.stdout.readline()) if "id" in message else None
call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
call({"jsonrpc": "2.0", "method": "notifications/initialized"})
results = []
for number, (tool, arguments) in enumerate(calls, 2):
    reply = call({"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": tool,
                                                                                    "arguments": arguments}})
    result = reply.get("result") or {}
    results.append({"error": bool(result.get("isError")) or "error" in reply,
                    "text": "".join(part.get("text", "") for part in result.get("content") or [])
                    or json.dumps(reply.get("error"))})
bridge.stdin.close(); bridge.wait(timeout=30)
print("BRIDGE_RESULT " + json.dumps(results))
"""


def bridge_calls(started, server, *calls, timeout=300):
    """CALLS ((tool, arguments) ...) made by an agent of STARTED through its bridge's real relay (bridge-PEER, as its
    agent's configuration reaches it: /sandbox/runtime.py --internal-mcp-bridge in the sandbox); their results."""
    done = started.python(BRIDGE_CLIENT, server, json.dumps([list(call) for call in calls]), timeout=timeout)
    assert done.returncode == 0, said(done)
    return answer(done.stdout, "BRIDGE_RESULT")


def bridge_calls_later(started, server, *calls):
    """bridge_calls in a process of its own (a write waits for the human): (process, read) - read() its results."""
    command = [str(SAI), "shell", started.name, "-n", "--", "python3", "-I", "-c", BRIDGE_CLIENT, server,
               json.dumps([list(call) for call in calls])]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               stdin=subprocess.DEVNULL, env=started.env)

    def read(timeout=300):
        out, err = process.communicate(timeout=timeout)
        assert process.returncode == 0, f"exit {process.returncode}\n{out[-3000:]}\n{err[-3000:]}"
        return answer(out, "BRIDGE_RESULT")
    return process, read


def bridge_question(env, ident, seconds=60):
    """The id of the open question of bridge IDENT (`sandbox-ai approvals list --json`), once there is one."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        done = sai(env, "approvals", "list", "--json")
        assert done.returncode == 0, said(done)
        mine = [row for row in json.loads(done.stdout) if ident in json.dumps(row)]
        if mine:
            return mine[0]["id"]
        time.sleep(0.5)
    pytest.fail(f"no question of bridge {ident} within {seconds} s")


def app_python():
    """A Python with PySide6 for the app's part (SANDBOX_AI_APP_PYTHON, as sandbox-ai-app takes it, else this one),
    or None."""
    for python in (os.environ.get("SANDBOX_AI_APP_PYTHON"), sys.executable):
        if python and subprocess.run([python, "-I", "-c", "import PySide6.QtWidgets"], capture_output=True,
                                     timeout=60).returncode == 0:
            return python
    return None


def two_sessions(request, backend, tmp_path, sessions):
    """(env, A, B) - two sessions that run, A with BACKEND, B with the other backend when both are selected (one
    Podman, one Bubblewrap); B's workspace has src/app.py and out/."""
    selected = request.config.getoption("--agent-backend")
    if selected == "both" and backend == "bwrap":
        pytest.skip("the podman run bridges a Podman session with a Bubblewrap one")
    other = "bwrap" if selected == "both" else backend
    env = environment(tmp_path)
    one, two = tmp_path / "wa", tmp_path / "wb"
    for work in (one, two):
        work.mkdir()
    (two / "src").mkdir()
    (two / "src" / "app.py").write_text("print('from b')\n")
    (two / "out").mkdir()
    a = session(sessions, request, backend, tmp_path, env=env, work=one, options=["--offline", *SLEEP])
    b = session(sessions, request, other, tmp_path, env=env, work=two, options=["--offline", *SLEEP])
    a.start()
    b.start()
    return env, a, b


def test_live_a_bridge_between_two_real_sessions(request, backend, tmp_path, sessions):
    """sandbox-ai bridge between two real sessions - with both backends, one of each (Podman and Bubblewrap): A
    may message, ask, read src and write out of B, and watch it. Every call is made by A's or B's agent side
    through its real relay in its sandbox; the write is asked and answered with `sandbox-ai approvals` (its diff
    shown there); watch sees B's activity, changes and terminal; the observer alerts and hints; revoke ends it for
    both."""
    env, a, b = two_sessions(request, backend, tmp_path, sessions)
    two = b.work
    done = sai(env, "bridge", "add", a.name, b.name, "--allow", "message,ask,files:ro:src,files:rw:out,watch",
               "--observer-actions", "alert,hint", "--ask", "never", "--json", timeout=240)
    assert done.returncode == 0, said(done)
    ident = json.loads(done.stdout)["id"]
    shown = json.loads(sai(env, "bridge", "show", ident, "--json").stdout)
    to_b, to_a = shown["a"]["server"], shown["b"]["server"]
    assert shown["state"] == "active", shown
    # message: A's agent sends, B's agent reads it.
    sent, = bridge_calls(a, to_b, ("send_message", {"text": "hello from a"}))
    assert not sent["error"], sent
    inbox, = bridge_calls(b, to_a, ("inbox", {"wait_seconds": 10}))
    assert "hello from a" in inbox["text"], inbox
    # ask: A waits while B's agent answers.
    asking, asked = bridge_calls_later(a, to_b, ("ask", {"question": "which port?", "timeout_seconds": 120}))
    question = None
    deadline = time.monotonic() + 90
    while question is None and time.monotonic() < deadline:
        seen, = bridge_calls(b, to_a, ("inbox", {"wait_seconds": 5}))
        found = re.search(r"question ([0-9a-f]{8}) \(", seen["text"])
        question = found.group(1) if found else None
    assert question, "B's inbox showed A's question"
    answered, = bridge_calls(b, to_a, ("answer", {"question_id": question, "text": "8080"}))
    assert not answered["error"], answered
    assert "8080" in asked()[0]["text"]
    # files:ro below the grant only; files:rw asked, with its diff, answered with sandbox-ai approvals.
    read, outside = bridge_calls(a, to_b, ("read_file", {"path": "src/app.py"}), ("read_file", {"path": "../wa"}))
    assert "print('from b')" in read["text"] and outside["error"], (read, outside)
    writing, wrote = bridge_calls_later(a, to_b, ("write_file", {"path": "out/live.txt", "content": "live\n"}))
    number = bridge_question(env, ident)
    detail = sai(env, "approvals", "show", number)
    assert "+live" in detail.stdout, said(detail)
    allowed = sai(env, "approvals", "allow", number)
    assert allowed.returncode == 0, said(allowed)
    assert not wrote()[0]["error"] and (two / "out" / "live.txt").read_text() == "live\n"
    # watch, and the observer's alert and hint.
    activity, changes, screen, alerted, hinted = bridge_calls(
        a, to_b, ("watch_activity", {"limit": 50}), ("watch_changes", {}), ("watch_terminal", {"lines": 0}),
        ("alert", {"message": "the live check's alert", "severity": "info"}), ("hint", {"text": "a live hint"}))
    assert not activity["error"] and "live.txt" in changes["text"], (activity, changes)
    assert not screen["error"] or "terminal" in screen["text"], screen
    assert not alerted["error"] and not hinted["error"], (alerted, hinted)
    assert "a live hint" in bridge_calls(b, to_a, ("inbox", {"wait_seconds": 5}))[0]["text"]
    shown = json.loads(sai(env, "bridge", "show", ident, "--json").stdout)
    assert any(x.get("text") == "the live check's alert" for x in shown.get("alerts") or []), shown
    # revoke: both lose it at once.
    revoked = sai(env, "bridge", "revoke", ident)
    assert revoked.returncode == 0, said(revoked)
    rows = json.loads(sai(env, "bridge", "list", "--all", "--json").stdout)
    assert next(row for row in rows if row["id"] == ident)["state"] == "revoked"
    gone = a.python(BRIDGE_CLIENT, to_b, json.dumps([["info", {}]]), timeout=120)
    assert gone.returncode != 0 or "BRIDGE_RESULT" not in gone.stdout or '"error": true' in gone.stdout, said(gone)


def test_live_the_apps_new_bridge_between_two_real_sessions(request, backend, tmp_path, sessions):
    """The app's New bridge… between two real sessions (one Podman, one Bubblewrap when both are selected),
    offscreen: tests/live_bridge_app_driver.py clicks it in the app's real window, fills in the dialog, the
    launcher's real dry run is asked about in the real question and the bridge made; A's agent then messages B
    through it. Needs a Python with PySide6 (SANDBOX_AI_APP_PYTHON, as sandbox-ai-app takes it, else this one)."""
    python = app_python()
    if python is None:
        pytest.skip("needs a Python with PySide6 for the app (SANDBOX_AI_APP_PYTHON, or pip install "
                    "PySide6-Essentials)")
    env, a, b = two_sessions(request, backend, tmp_path, sessions)
    driver = subprocess.run([python, "-I", str(ROOT / "tests" / "live_bridge_app_driver.py"), str(ROOT), a.name,
                             b.name], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
                            env={**env, "QT_QPA_PLATFORM": "offscreen", "SANDBOX_AI_HOME": str(ROOT)})
    result = answer(driver.stdout, "APP_RESULT") if "APP_RESULT " in driver.stdout else None
    assert result and not result.get("problem"), said(driver)
    assert result["opened"] and result["id"], result
    rows = json.loads(sai(env, "bridge", "list", "--json").stdout)
    assert any(row["id"] == result["id"] and row["state"] == "active" for row in rows), rows
    shown = json.loads(sai(env, "bridge", "show", result["id"], "--json").stdout)
    sent, = bridge_calls(a, shown["a"]["server"], ("send_message", {"text": "made by the app"}))
    assert not sent["error"], sent
    inbox, = bridge_calls(b, shown["b"]["server"], ("inbox", {"wait_seconds": 10}))
    assert "made by the app" in inbox["text"], inbox
    revoked = sai(env, "bridge", "revoke", result["id"])
    assert revoked.returncode == 0, said(revoked)


# --- remote MCP on a real host of sandbox-ai hosts (optional) --------------------------------------------------------

def test_live_remote_mcp_on_a_real_host(tmp_path):
    """On the host SAI_LIVE_HOST (scripts/live_check.sh --host NAME): hosts test, its MCP servers listed over SSH
    (JSON), and - when no instance of it runs there - its demo server started, seen running, and stopped."""
    host = os.environ.get("SAI_LIVE_HOST")
    if not host:
        pytest.skip("give scripts/live_check.sh --host NAME")
    # Your HOME: the host's key (its identity, ~/.ssh/...) lies there, and it is never copied
    env = environment(tmp_path, HOME=os.environ.get("SAI_LIVE_REAL_HOME") or os.path.expanduser("~"))
    config = Path(env["XDG_CONFIG_HOME"]) / "sandbox-ai"
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("hosts.json", "known_hosts"):
        source = Path(os.environ.get("SAI_LIVE_HOSTS_DIR", "/nonexistent")) / name
        if source.is_file():
            shutil.copy(source, config / name)
            (config / name).chmod(0o600)
    tested = sai(env, "hosts", "test", host, timeout=300)
    assert tested.returncode == 0, said(tested)

    def servers():
        listed = sai(env, "hosts", "run", host, "mcp", "list", "--json", timeout=300)
        assert listed.returncode == 0, said(listed)
        return {entry["name"]: entry for entry in json.loads(listed.stdout)}
    found = servers()
    print(f"{host}: MCP servers {sorted(found)}")
    demo = found.get("demo")
    if demo is None:
        pytest.skip(f"{host} has no demo MCP server (an older sandbox-ai there?)")
    if demo.get("instances") or demo.get("status") != "stopped":
        print(f"{host}: demo runs already ({demo.get('status')}); left as it is")
        return
    started = sai(env, "hosts", "run", host, "mcp", "start", "demo", timeout=600)
    try:
        assert started.returncode == 0, said(started)
        assert servers()["demo"]["status"] == "running"
    finally:
        stopped = sai(env, "hosts", "run", host, "mcp", "stop", "demo", timeout=300)
    assert stopped.returncode == 0, said(stopped)
    assert servers()["demo"]["status"] == "stopped"


# --- local models through the real launcher (optional; LOCAL_LLM_REPORT.md section 7) --------------------------------

MODELS = [model for model in (os.environ.get("SAI_LIVE_LLM_MODELS") or ",".join(LG.LLM_MODELS)).split(",") if model]
ONE_SHOT = {"claude": lambda prompt: ["-p", prompt],
            "codex": lambda prompt: ["exec", "--skip-git-repo-check", prompt],
            "opencode": lambda prompt: ["run", prompt]}


# Models these runs loaded on the server (not loaded before): {model: (server, environment)}. LM Studio refuses a
# large model while another is loaded ("insufficient system resources": the live check 2026-11-21, the 122b after
# the 35b), so the one loaded here is unloaded before the next model and at the end - never one that was loaded
# before (someone else's).
LOADED_HERE = {}


def unload_loaded_here(keep=None):
    for model in [name for name in LOADED_HERE if name != keep]:
        server, env = LOADED_HERE.pop(model)
        done = sai(env, "llm", "unload", server, model, timeout=300)
        print(f"{server}: unloaded {model}" if done.returncode == 0 else f"{server}: {model} not unloaded: "
              f"{said(done)}")


@pytest.fixture(scope="module")
def models_unloaded_after():
    yield
    unload_loaded_here()


@pytest.mark.parametrize("agent", list(ONE_SHOT))
@pytest.mark.parametrize("model", MODELS)
def test_live_local_model_smoke_run(request, backend, tmp_path, agent, model, models_unloaded_after):
    """AGENT with --llm SERVER:MODEL (SAI_LIVE_LLM_ENDPOINT: the server's entry, without secrets) and nothing else
    open: it writes live.txt with a nonce through its own tool and replies with it."""
    path = os.environ.get("SAI_LIVE_LLM_ENDPOINT")
    if not path:
        pytest.skip("give scripts/live_check.sh --llm NAME")
    entry = json.loads(Path(path).read_text())
    env = environment(tmp_path)
    config = Path(env["XDG_CONFIG_HOME"]) / "sandbox-ai"
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = dict(entry["record"])
    if entry.get("ca"):
        (config / "llm").mkdir(mode=0o700, exist_ok=True)
        shutil.copy(entry["ca"], config / "llm" / Path(entry["ca"]).name)
        record["ca"] = Path(entry["ca"]).name if not Path(record["ca"]).is_absolute() else record["ca"]
    LG.write_private(config / "llm.json", json.dumps({"version": 1, "endpoints": {entry["name"]: record}}) + "\n")
    offered = sai(env, "llm", "models", entry["name"], "--json", timeout=120)
    assert offered.returncode == 0, said(offered)
    listed = [m for m in json.loads(offered.stdout) if isinstance(m, dict)]
    names = {m.get("name") or m.get("key") or m.get("id") for m in listed}
    if model not in names:
        pytest.skip(f"{entry['name']} does not offer {model}")
    unload_loaded_here(keep=model)
    if model not in LOADED_HERE and not any((m.get("name") or m.get("key") or m.get("id")) == model and m.get("loaded")
                                            for m in listed):
        LOADED_HERE[model] = (entry["name"], env)
    work = tmp_path / "w"
    work.mkdir()
    nonce = "LIVE" + secrets.token_hex(4).upper()
    prompt = (f"Create a file named live.txt in the current directory that contains exactly {nonce}. Then reply with "
              f"exactly {nonce} and nothing else.")
    begin = time.monotonic()
    try:
        stdout, stderr = once(request, backend, agent, work,
                              ["--no-auth", "--no-provider-defaults", "--dangerously-skip-permissions", "--llm",
                               f"{entry['name']}:{model}"], ONE_SHOT[agent](prompt), env, timeout=1800)
    except AssertionError as failed:
        # Live check 2026-11-22: LM Studio refused to load the 122b model ("Model loading was stopped due to
        # insufficient system resources") - the server's memory, not the sandbox: said as such, not as a failure.
        text = str(failed)
        if "insufficient system resources" in text or f"LM Studio did not load {model}" in text:
            pytest.skip(f"{entry['name']} could not load {model} (its server's resources): "
                        + next((line.strip() for line in text.splitlines()
                                if "insufficient system resources" in line or "did not load" in line), "")[:300])
        raise
    spent = time.monotonic() - begin
    print(f"{agent} x {model}: {spent:.0f} s")
    written = (work / "live.txt").read_text().strip() if (work / "live.txt").is_file() else None
    assert written == nonce, (written, stdout[-2000:], stderr[-2000:])
    assert nonce in stdout, (stdout[-2000:], stderr[-2000:])
