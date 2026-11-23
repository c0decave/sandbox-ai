"""The live checks the development sandbox cannot run (no Podman, no user namespaces there): opt-in with --agent-live,
run on a real machine by scripts/live_check.sh (docs/LIVE_CHECK.md), which says per check what it proves.

Each test runs sandbox-ai with folders of its own (HOME, XDG configuration, data, state, cache, runtime; plain-file
secrets): the configuration, sessions, kits and secrets of whoever runs them are never read or changed. Podman alone
sees the real HOME (scripts/live_gaps.py's wrapper first on PATH), so the images built there are used. Every session
a test starts is stopped and deleted at its end. Inputs from the script (all optional, a test without its input is
skipped and says what it needs): SAI_LIVE_TOKEN (a made-up token), SAI_LIVE_DEVICE, SAI_LIVE_HOST +
SAI_LIVE_HOSTS_DIR, SAI_LIVE_LLM_ENDPOINT + SAI_LIVE_LLM_MODELS, SAI_LIVE_REAL_HOME, SAI_LIVE_HARNESS_DIR (where
harnesses-live and cairn-workers-live note what each harness's image took), SAI_LIVE_CAIRN_SECONDS (how long
cairn-workers-live waits for a worker's fact on Cairn's board; default 900)."""
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


# --- build toolchains mounted into a running sandbox (toolchain-live) ------------------------------------------------

HELLO_GO = 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hello from go") }\n'


def toolchains_added(env, *names):
    """NAMES in the check's own store: from the signed catalog, or from SAI_LIVE_TOOLCHAINS (a toolchains.json of
    your own builds, taken unsigned). Skips when the catalog has none of them yet."""
    own = os.environ.get("SAI_LIVE_TOOLCHAINS")
    more = ["--catalog", own, "--unsigned"] if own else []
    for name in names:
        done = sai(env, "toolchain", "add", name, *more, timeout=1800)
        if done.returncode != 0 and not own and ("not published" in done.stderr or "no toolchain" in done.stderr
                                                 or "not signed" in done.stderr):
            pytest.skip(f"the signed catalog offers no {name} yet ({done.stderr.strip()[-300:]}): build it "
                        "(sandbox-ai-mcp: scripts/toolchain.py build) and give SAI_LIVE_TOOLCHAINS=.../toolchains.json")
        assert done.returncode == 0, said(done)


def test_live_toolchains_attached_to_a_running_session(request, backend, tmp_path, sessions):
    """sandbox-ai toolchain attach RUN go, then python: each mounted read-only (nosuid, nodev) at /opt/toolchains/NAME
    in the running sandbox, its commands found by shells started after it without anything started anew; a hello
    world built and run with go; python3 is the toolchain's, before the image's; toolchain list --run names both;
    detach takes the commands and the mount away again."""
    env = environment(tmp_path)
    toolchains_added(env, "go", "python")
    started = session(sessions, request, backend, tmp_path, env=env, options=["--offline", *SLEEP])
    started.start()
    before = started.shell("test -d /opt/toolchains/bin && echo FOLDER; command -v go || echo NO-GO; echo $PATH")
    assert "FOLDER" in before.stdout and "NO-GO" in before.stdout, said(before)
    assert "/opt/toolchains/bin" in before.stdout.splitlines()[-1].split(":")[0], said(before)
    attached = started.sai("toolchain", "attach", started.name, "go", "--offline", timeout=300)
    assert attached.returncode == 0 and "attached go" in attached.stdout, said(attached)
    (started.work / "hello.go").write_text(HELLO_GO)
    built = started.shell("cd /work && command -v go && go version && go run hello.go && go build -o hello hello.go "
                          "&& ./hello", timeout=600)
    assert built.returncode == 0 and built.stdout.count("hello from go") == 2, said(built)
    assert "/opt/toolchains/bin/go" in built.stdout
    facts = started.shell("grep ' /opt/toolchains/go ' /proc/self/mountinfo; touch /opt/toolchains/go/x 2>&1 || "
                          "echo READ-ONLY; touch /opt/toolchains/bin/x 2>&1 || echo WRAPPERS-READ-ONLY")
    options = facts.stdout.splitlines()[0].split()[5].split(",") if facts.stdout.strip() else []
    assert {"ro", "nosuid", "nodev"} <= set(options), said(facts)
    assert "READ-ONLY" in facts.stdout and "WRAPPERS-READ-ONLY" in facts.stdout, said(facts)
    second = started.sai("toolchain", "attach", started.name, "python", "--offline", timeout=300)
    assert second.returncode == 0, said(second)
    python = started.shell("command -v python3; python3 -c 'import ssl, sys; print(sys.version_info[:2], sys.prefix)'; "
                           "bash -c 'echo BASH_ENV=$BASH_ENV'")
    assert python.stdout.splitlines()[0] == "/opt/toolchains/bin/python3", said(python)
    assert "/opt/toolchains/python" in python.stdout and "BASH_ENV=/opt/toolchains/env.sh" in python.stdout
    listed = started.sai("toolchain", "list", "--run", started.name, "--json")
    assert listed.returncode == 0, said(listed)
    assert sorted((row["name"], row["source"]) for row in json.loads(listed.stdout)) == [("go", "added"),
                                                                                         ("python", "added")]
    for name in ("go", "python"):
        gone = started.sai("toolchain", "detach", started.name, name, timeout=300)
        assert gone.returncode == 0, said(gone)
    after = started.shell("command -v go || echo NO-GO; ls /opt/toolchains/go 2>/dev/null | wc -l; "
                          "command -v python3")
    assert "NO-GO" in after.stdout and "/opt/toolchains" not in after.stdout.splitlines()[-1], said(after)
    assert json.loads(started.sai("toolchain", "list", "--run", started.name, "--json").stdout) == []
    print(f"toolchains under {backend}: go and python attached, used, detached")


def test_live_a_start_with_a_toolchain(request, backend, tmp_path):
    """sandbox-ai AGENT --toolchain go: the toolchain is there from the first command, read-only."""
    env = environment(tmp_path)
    toolchains_added(env, "go")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "hello.go").write_text(HELLO_GO)
    out, err = once(request, backend, "codex", work, ["--toolchain", "go", "--toolchain-offline", "--binary", "/bin/sh",
                                                      "--no-tmux"],
                    ["-c", "cd /work && go run hello.go && touch /opt/toolchains/go/x 2>/dev/null || echo READ-ONLY"],
                    env, timeout=900)
    assert "hello from go" in out and "READ-ONLY" in out, (out[-2000:], err[-2000:])


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
LLM_AGENTS = ("claude", "codex", "opencode")  # local-llm's (the research's three); harnesses-live: LG.HARNESSES


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


def llm_server(tmp_path, model):
    """(entry, env) of the server SAI_LIVE_LLM_ENDPOINT names (its entry, without secrets) in a configuration of this
    test's own, MODEL on offer there (it is unloaded after, when these runs loaded it); skipped without them."""
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
    return entry, env


def smoke_run(request, backend, tmp_path, agent, entry, model, env):
    """AGENT with --llm SERVER:MODEL and nothing else open, in a workspace of its own: it writes live.txt with a nonce
    through its own tool and replies with it (asserted). A server that could not load MODEL, or one that only the
    Podman backend reaches (this host's loopback), is a skip with that reason, not a failure."""
    work = tmp_path / "w"
    work.mkdir()
    nonce = "LIVE" + secrets.token_hex(4).upper()
    prompt = (f"Create a file named live.txt in the current directory that contains exactly {nonce}. Then reply with "
              f"exactly {nonce} and nothing else.")
    begin = time.monotonic()
    try:
        stdout, stderr = once(request, backend, agent, work,
                              ["--no-auth", "--no-provider-defaults", "--dangerously-skip-permissions", "--llm",
                               f"{entry['name']}:{model}"], LG.one_shot(agent, prompt), env, timeout=1800)
    except AssertionError as failed:
        # Live check 2026-11-22: LM Studio refused to load the 122b model ("Model loading was stopped due to
        # insufficient system resources") - the server's memory, not the sandbox: said as such, not as a failure.
        text = str(failed)
        if "insufficient system resources" in text or f"LM Studio did not load {model}" in text:
            pytest.skip(f"{entry['name']} could not load {model} (its server's resources): "
                        + next((line.strip() for line in text.splitlines()
                                if "insufficient system resources" in line or "did not load" in line), "")[:300])
        if "which only the Podman backend" in text:
            pytest.skip(f"{entry['name']} is on this host's loopback, which only the Podman backend reaches")
        raise
    spent = time.monotonic() - begin
    print(f"{backend}/{agent} x {model}: {spent:.0f} s")
    written = (work / "live.txt").read_text().strip() if (work / "live.txt").is_file() else None
    assert written == nonce, (written, stdout[-2000:], stderr[-2000:])
    assert nonce in stdout, (stdout[-2000:], stderr[-2000:])


@pytest.mark.parametrize("agent", LLM_AGENTS)
@pytest.mark.parametrize("model", MODELS)
def test_live_local_model_smoke_run(request, backend, tmp_path, agent, model, models_unloaded_after):
    """AGENT with --llm SERVER:MODEL (SAI_LIVE_LLM_ENDPOINT: the server's entry, without secrets) and nothing else
    open: it writes live.txt with a nonce through its own tool and replies with it."""
    entry, env = llm_server(tmp_path, model)
    smoke_run(request, backend, tmp_path, agent, entry, model, env)


# --- every harness through the real launcher (optional; the user, 2026-11-22) ----------------------------------------

def harness_image(agent, env):
    """AGENT's image for Podman: there, or built now with `sandbox-ai build AGENT` (how long it took said, and kept
    in SAI_LIVE_HARNESS_DIR/AGENT.json for the report); a skip with the reason when it cannot be built here."""
    image = f"localhost/{agent}-sandbox:latest"
    facts = Path(os.environ["SAI_LIVE_HARNESS_DIR"]) / f"{agent}.json" if os.environ.get("SAI_LIVE_HARNESS_DIR") \
        else None
    known = json.loads(facts.read_text()) if facts and facts.is_file() else {}
    if known.get("problem"):
        pytest.skip(f"its image cannot be built here: {known['problem']}")
    if subprocess.run(["podman", "image", "exists", image], env=env, capture_output=True).returncode == 0:
        if facts and not known:
            facts.write_text(json.dumps({"present": True}) + "\n")
        return image
    begin = time.monotonic()
    built = sai(env, "build", agent, timeout=3600)
    seconds = time.monotonic() - begin
    problem = None if built.returncode == 0 else \
        (next((line for line in reversed((built.stderr + built.stdout).splitlines()) if line.strip()), "")
         or f"exit {built.returncode}")[-300:]
    if facts:
        facts.write_text(json.dumps({"built": problem is None, "seconds": round(seconds, 1), "problem": problem}) + "\n")
    print(f"sandbox-ai build {agent}: {'built' if problem is None else 'failed'} in {seconds:.0f} s")
    if problem:
        pytest.skip(f"its image cannot be built here (sandbox-ai build {agent}, {seconds:.0f} s): {problem}")
    return image


@pytest.mark.parametrize("agent", list(LG.HARNESSES))
def test_live_harness_smoke_run(request, backend, tmp_path, agent, models_unloaded_after):
    """Every harness the launcher knows, through the real launcher with --llm SERVER:MODEL (the first of
    SAI_LIVE_LLM_MODELS) and nothing else open: it writes live.txt with a nonce through its own tool and replies with
    it. Podman: its image, built first when missing; Bubblewrap: this host's own installation of it."""
    entry, env = llm_server(tmp_path, MODELS[0])
    if backend == "podman":
        harness_image(agent, env)
    elif not shutil.which(agent, path=env.get("PATH")):
        pytest.skip(f"Bubblewrap runs this host's own `{agent}`, which is not on PATH here (Podman runs its image)")
    smoke_run(request, backend, tmp_path, agent, entry, MODELS[0], env)


# --- every built-in preset through the real launcher (optional; the user, 2026-11-22) --------------------------------

PRESETS = LG.builtin_presets()


def preset_record(name, backend, **found):
    """What presets-live found for preset NAME with BACKEND, for the report (SAI_LIVE_PRESET_DIR/NAME.BACKEND.json):
    outcome (passed, failed, skipped), why, parts [(part, seen)], agent, seconds."""
    folder = os.environ.get("SAI_LIVE_PRESET_DIR")
    if folder:
        Path(folder, f"{name}.{backend}.json").write_text(json.dumps({"preset": name, "backend": backend, **found}))


def preset_environment(tmp_path):
    """(env, the --llm value "SERVER:MODEL" or None, provider_login) of a preset's run: --llm's server in a
    configuration of this run's own (llm_server), and with --provider-login (SAI_LIVE_PROVIDER_LOGIN) your HOME and
    data folder for the agents' logins - sandbox-ai's configuration, state, cache and runtime stay this run's own."""
    login = os.environ.get("SAI_LIVE_PROVIDER_LOGIN") == "1"
    if os.environ.get("SAI_LIVE_LLM_ENDPOINT"):
        entry, env = llm_server(tmp_path, MODELS[0])
        value = f"{entry['name']}:{MODELS[0]}"
    elif login:
        env, value = environment(tmp_path), None
    else:
        pytest.skip("give scripts/live_check.sh --llm NAME (or --provider-login)")
    if login:
        env["HOME"] = os.environ.get("SAI_LIVE_REAL_HOME") or os.path.expanduser("~")
        env["XDG_DATA_HOME"] = os.environ.get("SAI_LIVE_REAL_DATA_HOME") or str(Path(env["HOME"]) / ".local/share")
    return env, value, login


def preset_setup(plan, env, tmp_path):
    """What PLAN needs in this run's own configuration: its folders (with one small file), a made-up key for each
    --api-key (its host stays closed: --no-provider-defaults), a Tor profile for its VPN value, each MCP server it
    names added from the catalog with folders of this run's own (by the catalog the preset names: mcp:binwalk, which
    axross-mcp-hub has too), each toolchain it brings added to this run's store (the signed catalog, or
    SAI_LIVE_TOOLCHAINS's own builds, unsigned), your images (user-images.json, read only: no secret in it) for its
    services. A problem (the command and its last line) or None."""
    for folder in plan["folders"]:
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, "README.txt").write_text("a folder of the sandbox-ai live check\n")
    for name in plan["api_keys"]:
        done = sai({**env, "SAI_LIVE_MADE_UP": token()}, "secrets", "set", name, "--from-env", "SAI_LIVE_MADE_UP")
        if done.returncode != 0:
            return f"sandbox-ai secrets set {name}: {said(done)[-300:]}"
    config = Path(env["XDG_CONFIG_HOME"]) / "sandbox-ai"
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    if plan.get("vpn"):  # the VPN value's Tor profile, of this run's own (no key in it)
        done = sai(env, "vpn", "add", plan["vpn"], "--tor", timeout=300)
        if done.returncode != 0 and "exists" not in (done.stderr + done.stdout):
            return f"sandbox-ai vpn add {plan['vpn']} --tor: {said(done)[-300:]}"
    mine = Path(os.environ.get("SAI_LIVE_REAL_CONFIG", "/nonexistent")) / "user-images.json"
    if plan["services"] and mine.is_file() and not (config / "user-images.json").exists():
        shutil.copy(mine, config / "user-images.json")
    for server, targets in plan["mcp"]:
        entry = next((item for item in PRESETS[plan["preset"]].get("mcp") or [] if item.split(":")[-1] == server),
                     server)
        mounts = []
        for target in targets:
            host = tmp_path / "mcp" / server / target.strip("/").replace("/", "-")
            host.mkdir(parents=True, exist_ok=True)
            mounts += ["--mount", f"{target}={host}:rw"]
        done = sai(env, "mcp", "add", entry, *mounts, timeout=1800)
        if done.returncode != 0:
            last = next((line for line in reversed((done.stderr + done.stdout).splitlines()) if line.strip()), "")
            return f"sandbox-ai mcp add {entry}: {last[-300:] or f'exit {done.returncode}'}"
    own = os.environ.get("SAI_LIVE_TOOLCHAINS")
    for name in plan.get("toolchains") or []:
        done = sai(env, "toolchain", "add", name, *(["--catalog", own, "--unsigned"] if own else []), timeout=1800)
        if done.returncode != 0:
            last = next((line for line in reversed((done.stderr + done.stdout).splitlines()) if line.strip()), "")
            return f"sandbox-ai toolchain add {name}: {last[-300:] or f'exit {done.returncode}'}"
    return None


@pytest.mark.parametrize("name", list(PRESETS))
def test_live_preset_smoke_run(request, backend, tmp_path, name, models_unloaded_after):
    """Every built-in preset, started for real (sandbox-ai AGENT --preset NAME, its first agent) with --llm's model
    (SAI_LIVE_LLM_ENDPOINT) and nothing else open, or with the agent's own provider (SAI_LIVE_PROVIDER_LOGIN): it
    writes live.txt with a nonce through its own tool and replies with it (a read-only workspace: it reads the nonce
    from a file there instead), and each MCP server it names starts, each service it runs is ready. A value only you
    can give comes from SAI_LIVE_PRESET_VALUES (--preset-values FILE); without it the preset is skipped with the
    reason and the command to run it yourself."""
    chosen = [n for n in (os.environ.get("SAI_LIVE_PRESETS") or "").split(",") if n]
    if chosen and name not in chosen:
        pytest.skip("not among --presets")
    given = LG.preset_values(os.environ["SAI_LIVE_PRESET_VALUES"]).get(name) \
        if os.environ.get("SAI_LIVE_PRESET_VALUES") else None
    env, value, login = preset_environment(tmp_path)
    plan = LG.preset_plan(PRESETS[name], given, llm=value, provider_login=login, folders=tmp_path / "v")
    record = {"agent": plan["agent"], "parts": [], "by_hand": plan["by_hand"]}
    if plan["skip"]:
        preset_record(name, backend, outcome="skipped", why=plan["skip"], **record)
        pytest.skip(plan["skip"])
    if backend not in plan["backends"]:
        why = f"Podman only: {', '.join(plan['podman_only'])} (Bubblewrap has no pod for it)"
        preset_record(name, backend, outcome="skipped", why=why, **record)
        pytest.skip(why)
    agent = plan["agent"]
    if backend == "podman":
        harness_image(agent, env)
    elif not shutil.which(agent, path=env.get("PATH")):
        why = f"Bubblewrap runs this host's own `{agent}`, which is not on PATH here (Podman runs its image)"
        preset_record(name, backend, outcome="skipped", why=why, **record)
        pytest.skip(why)
    problem = preset_setup(plan, env, tmp_path)
    if problem:
        preset_record(name, backend, outcome="failed", why=problem, **record)
        pytest.fail(problem)
    work = tmp_path / "w"
    work.mkdir()
    nonce = "LIVE" + secrets.token_hex(4).upper()
    read_only = "--workspace-ro" in plan["options"] or "--workspace-ro" in PRESETS[name].get("options", [])
    if read_only:  # the agent cannot write the workspace: it reads the nonce there, which the prompt never names
        (work / "nonce.txt").write_text(nonce + "\n")
        prompt = "Read the file nonce.txt in the current directory and reply with exactly its content and nothing else."
    else:
        prompt = (f"Create a file named live.txt in the current directory that contains exactly {nonce}. Then reply "
                  f"with exactly {nonce} and nothing else.")
    command = [str(SAI), agent, "--backend", backend, "--workspace", str(work), *plan["options"],
               "--dangerously-skip-permissions", "--", *LG.one_shot(agent, prompt)]
    # What this host lacks for it (your service image, say) the launcher's dry run says first: skipped with its line
    dry = subprocess.run([*command[:command.index("--")], "--dry-run", *command[command.index("--"):]],
                         capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=300)
    if dry.returncode != 0:
        line = next((text for text in reversed(dry.stderr.splitlines()) if text.strip()), f"exit {dry.returncode}")
        why = f"the launcher refuses this start here: {line[-300:]}; run: {plan['by_hand']}"
        preset_record(name, backend, outcome="skipped", why=why, **record)
        pytest.skip(why)
    begin = time.monotonic()
    process = subprocess.run(command, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                             timeout=3600)
    record["seconds"] = round(time.monotonic() - begin, 1)
    out, err = process.stdout, process.stderr
    last = next((line for line in reversed((err + out).splitlines()) if line.strip()), f"exit {process.returncode}")
    if process.returncode != 0:
        if "which only the Podman backend" in err:
            why = "the LLM server is on this host's loopback, which only the Podman backend reaches"
            preset_record(name, backend, outcome="skipped", why=why, **record)
            pytest.skip(why)
        preset_record(name, backend, outcome="failed", why=last[-300:], **record)
        pytest.fail(f"{name} ({backend}/{agent}): exit {process.returncode}\n{out[-3000:]}\n{err[-3000:]}")
    written = (work / "live.txt").read_text().strip() if (work / "live.txt").is_file() else None
    if (not read_only and written != nonce) or nonce not in out:
        why = (f"the smoke task: live.txt {'missing' if written is None else 'wrong'}" if not read_only and
               written != nonce else "the smoke task: the nonce is not in its reply") + f" ({last[-200:]})"
        preset_record(name, backend, outcome="failed", why=why, **record)
        pytest.fail(f"{name}: {why}\n{out[-2000:]}\n{err[-2000:]}")
    record["parts"] = LG.parts_seen(plan, err)
    missing = [part for part, seen in record["parts"] if not seen]
    if missing:
        why = f"started, but did not come up: {', '.join(missing)}"
        preset_record(name, backend, outcome="failed", why=why, **record)
        pytest.fail(f"{name}: {why}\n{err[-3000:]}")
    preset_record(name, backend, outcome="passed", why="", **record)
    print(f"{backend}/{agent} --preset {name}: {record['seconds']:.0f} s" +
          (f"; came up: {', '.join(part for part, _ in record['parts'])}" if record["parts"] else ""))


# --- every MCP server of the signed catalog through a start's path (optional; the user, 2026-11-23) ------------------

# The agent's side of an MCP connection, run in the session's sandbox (sandbox-ai shell): the bridge the agent's own
# configuration runs (/sandbox/runtime.py --internal-mcp-bridge) to the socket the supervisor's relay serves there;
# initialize, notifications/initialized, tools/list and each call, every answer awaited for its own time at most.
MCP_CLIENT = r"""
import json, os, select, subprocess, sys, time
server, payload = sys.argv[1], json.loads(sys.argv[2])
bridge = subprocess.Popen(["python3", "-I", "/sandbox/runtime.py", "--internal-mcp-bridge",
                           f"/run/sandbox-mcp/{server}/server.sock"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
buffer = b""
def answer(ident, seconds):
    global buffer
    deadline = time.monotonic() + seconds
    while True:
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == ident:
                return message
        left = deadline - time.monotonic()
        if left <= 0:
            return {"timeout": seconds}
        ready, _, _ = select.select([bridge.stdout], [], [], left)
        if ready:
            chunk = os.read(bridge.stdout.fileno(), 65536)
            if not chunk:
                return {"ended": bridge.stderr.read().decode(errors="replace")[-1500:]}
            buffer += chunk
def send(message):
    bridge.stdin.write((json.dumps(message) + "\n").encode()); bridge.stdin.flush()
found = {"calls": []}
begin = time.monotonic()
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
      "capabilities": {}, "clientInfo": {"name": "sandbox-ai-live-check", "version": "1"}}})
found["initialize"] = answer(1, payload.get("start_timeout", 60) + 60)
found["handshake_seconds"] = round(time.monotonic() - begin, 1)
if "result" in found["initialize"]:
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    found["tools"] = answer(2, 120)
    for number, call in enumerate(payload["calls"], 3):
        started = time.monotonic()
        send({"jsonrpc": "2.0", "id": number, "method": "tools/call",
              "params": {"name": call["tool"], "arguments": call.get("arguments") or {}}})
        found["calls"].append({"answer": answer(number, call.get("timeout", 120) + 30),
                               "seconds": round(time.monotonic() - started, 1)})
bridge.stdin.close()
try:
    bridge.wait(timeout=30)
except subprocess.TimeoutExpired:
    bridge.kill()
print("MCP_LIVE_RESULT " + json.dumps(found))
"""
MCP_NAMES = [name for name in (os.environ.get("SAI_LIVE_MCP_SERVERS") or "").split(",") if name] or ["(none)"]


def mcp_record(name, backend, **found):
    """What mcp-live found for server NAME with BACKEND, for the report (SAI_LIVE_MCP_DIR/NAME.BACKEND.json)."""
    folder = os.environ.get("SAI_LIVE_MCP_DIR")
    if folder:
        Path(folder, f"{name}.{backend}.json").write_text(json.dumps({"server": name, "backend": backend, **found}))


def mcp_environment():
    """The sandbox-ai of every mcp-live test (its HOME and data folders: SAI_LIVE_MCP_HOME): a server is added there
    once and every test reaches it; never yours."""
    return LG.isolated_environment(Path(os.environ["SAI_LIVE_MCP_HOME"]), os.environ)


def mcp_workspace(path, plan, marker):
    """A workspace for PLAN's /work: the smoke files (SAI_LIVE_MCP_FIXTURES) or its seed, and its own MARKER file
    (the other workspace's must never show)."""
    path.mkdir(parents=True, exist_ok=True)
    if plan["work"] == "fixtures":
        shutil.copytree(os.environ["SAI_LIVE_MCP_FIXTURES"], path, symlinks=True, dirs_exist_ok=True)
    for item in plan.get("seed") or []:
        target = path.joinpath(*str(item["path"]).split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if "symlink" in item:
            target.symlink_to(item["symlink"])
        else:
            target.write_text(item["text"])
    (path / marker).write_text("the sandbox-ai live check's own file of this workspace\n")
    return path


def supervisor_tail(env, name, lines=40):
    """The last lines of the newest supervisor.log of server NAME's instances (where a server that died says why)."""
    logs = sorted((Path(env["XDG_STATE_HOME"]) / "sandbox-ai" / "mcp").glob(f"{name}*/supervisor.log"),
                  key=lambda path: path.stat().st_mtime)
    return "\n".join(logs[-1].read_text(errors="replace").splitlines()[-lines:]) if logs else ""


def mcp_added(env, name, plan, backend):
    """`sandbox-ai mcp add NAME` with PLAN's options (its defaults otherwise), once for every test (a marker in
    SAI_LIVE_MCP_HOME): (said, problem)."""
    marker = Path(os.environ["SAI_LIVE_MCP_HOME"]) / f"added-{name}.json"
    if marker.is_file():
        known = json.loads(marker.read_text())
        return known["said"], known.get("problem")
    folders = []
    for target, content in plan["folders"].items():  # a folder its smoke spec fills: one of the check's own
        host = Path(os.environ["SAI_LIVE_MCP_HOME"]) / "folders" / name / target.strip("/").replace("/", "-")
        mcp_workspace(host, {"work": "fixtures" if content == "fixtures" else None,
                             "seed": content if isinstance(content, list) else []}, "README.txt")
        folders += ["--mount", f"{target}={host}:rw"]
    begin = time.monotonic()
    # The signed catalog's entry by its prefix: a server of the same name in another catalog (axross's hub: binwalk,
    # radare2) made a bare NAME ambiguous - `mcp add` asks which one (the user's first mcp-live run, 2026-11-23).
    done = sai(env, "mcp", "add", f"{LG.MCP_PREFIX}:{name}", *plan["add"], *folders, timeout=3600)
    seconds = time.monotonic() - begin
    problem = None if done.returncode == 0 else (next((line for line in reversed(
        (done.stderr + done.stdout).splitlines()) if line.strip()), "") or f"exit {done.returncode}")[-300:]
    said = f"yes ({seconds:.0f} s)" if problem is None else "no"
    marker.write_text(json.dumps({"said": said, "problem": problem}))
    return said, problem


def mcp_calls(started, plan):
    """PLAN's calls made by the agent side of STARTED through its relay (MCP_CLIENT): its MCP_LIVE_RESULT."""
    payload = {"calls": plan["calls"], "start_timeout": plan.get("start_timeout", 60)}
    timeout = 300 + sum(int(call.get("timeout", 120)) + 30 for call in plan["calls"])
    done = started.python(MCP_CLIENT, plan["server"], json.dumps(payload), timeout=timeout)
    assert done.returncode == 0, said(done)
    return answer(done.stdout, "MCP_LIVE_RESULT")


def mcp_verdict(plan, found):
    """(handshake, tools, calls {passed, failed, skipped, first_failure}, problems) of a session's FOUND."""
    problems = []
    init = found.get("initialize") or {}
    if "result" not in init:
        why = init.get("error", {}).get("message") if isinstance(init.get("error"), dict) else \
            ("no answer within its time" if "timeout" in init else f"the bridge ended: {init.get('ended', '')[-300:]}")
        return f"no: {why}", "-", {}, [f"no handshake: {why}"]
    handshake = f"yes ({found.get('handshake_seconds', 0):.0f} s)"
    names = [tool.get("name") for tool in ((found.get("tools") or {}).get("result") or {}).get("tools") or []
             if isinstance(tool, dict)]
    missing = [tool for tool in plan["expect_tools"] if tool not in names]
    recorded = plan.get("tools")
    added = sorted(set(names) - set(recorded)) if recorded is not None else []
    gone = sorted(set(recorded) - set(names)) if recorded is not None else []
    tools = f"{len(names)}" + (" = tools.json" if recorded is not None and not added and not gone else "")
    if missing or added or gone:
        tools += "; " + "; ".join(part for part in (f"missing {', '.join(missing or gone)}" if missing or gone else "",
                                                     f"not recorded {', '.join(added)}" if added else "") if part)
        problems.append(f"tools/list: {tools}")
    counts = {"passed": 0, "failed": 0, "skipped": len(plan["skipped_calls"])}
    for call, made in zip(plan["calls"], found.get("calls") or []):
        passed, text, limited = LG.mcp_call_passed(call, made.get("answer"))
        if limited:
            counts["skipped"] += 1
        elif passed:
            counts["passed"] += 1
        else:
            counts["failed"] += 1
            counts.setdefault("first_failure", f"{call.get('tool')}: {' '.join(text.split())[:200] or made}")
            problems.append(f"{call.get('tool')}: wanted {call.get('expect')!r}, got {' '.join(text.split())[:300]}")
    return handshake, tools, counts, problems


@pytest.mark.parametrize("name", MCP_NAMES)
def test_live_mcp_server(request, backend, tmp_path, name, sessions):
    """Every server of the signed MCP catalog through the path a start uses: `sandbox-ai mcp add NAME` with its
    defaults (the catalog's current digest), a kept session with --mcp NAME, the server started by sandbox-ai (its
    supervisor, bridge and each connection's container or Bubblewrap sandbox), the agent side's connection through
    the session's relay: initialize, tools/list against the MCP repository's tools.json, and its smoke calls. A server
    on the start's workspace gets a second session in a second workspace: neither instance sees the other's files."""
    if name == "(none)":
        pytest.skip("give scripts/live_check.sh --only mcp-live (it reads the signed catalog and asks first)")
    catalog = json.loads(Path(os.environ["SAI_LIVE_MCP_CATALOG"]).read_text())
    entry = next(e for e in catalog["entries"] if e.get("prefix") == LG.MCP_PREFIX and e.get("name") == name)
    repo = os.environ.get("SAI_LIVE_MCP_REPO") or "/nonexistent"
    plan = LG.mcp_plan(entry, LG.mcp_spec(repo, name), backend, LG.mcp_tools(repo, name))
    record = {"by_hand": plan["by_hand"]}
    if plan["skip"]:
        mcp_record(name, backend, outcome="skipped", why=plan["skip"], **record)
        pytest.skip(plan["skip"])
    env = mcp_environment()
    if backend == "podman":
        harness_image("codex", env)
    begin = time.monotonic()
    record["added"], problem = mcp_added(env, name, plan, backend)
    if problem:
        mcp_record(name, backend, outcome="failed", why=f"sandbox-ai mcp add {name}: {problem}", **record)
        pytest.fail(f"sandbox-ai mcp add {name}: {problem}")
    options = ["--mcp", name]
    for var, value, secret in plan["env"]:  # (the smoke spec's own made-up values: a secret of the check's own)
        if secret:
            key = f"LIVE_{name.upper().replace('-', '_')}_{var}"
            done = sai({**env, "SAI_LIVE_MADE_UP": value}, "secrets", "set", key, "--from-env", "SAI_LIVE_MADE_UP")
            assert done.returncode == 0, said(done)
            options += ["--mcp-env", f"{name}:{var}@{key}"]
        else:
            options += ["--mcp-env", f"{name}:{var}={value}"]
    works = [mcp_workspace(tmp_path / "wa", plan, LG.MCP_MARKERS[0])]
    if plan["isolation"]:
        works.append(mcp_workspace(tmp_path / "wb", plan, LG.MCP_MARKERS[1]))
    found = []
    for index, work in enumerate(works):
        started = session(sessions, request, backend, tmp_path, env=env, work=work, options=[*options, *SLEEP])
        done = subprocess.run(started.command, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                              timeout=1800)
        if done.returncode != 0:
            last = next((line for line in reversed(done.stderr.splitlines()) if line.strip()), f"exit {done.returncode}")
            mcp_record(name, backend, outcome="failed", started="no", why=last[-300:],
                       supervisor=supervisor_tail(env, name), seconds=round(time.monotonic() - begin, 1), **record)
            pytest.fail(f"{name} ({backend}): the session did not start: {said(done)}")
        started.started = True
        record["started"] = "yes"
        found.append(mcp_calls(started, plan))
    handshake, tools, counts, problems = mcp_verdict(plan, found[0])
    record.update(handshake=handshake, tools=tools, calls=counts)
    if handshake.startswith("no"):
        record["supervisor"] = supervisor_tail(env, name)
    if plan["isolation"]:
        # Each workspace its own instance, mounting its own workspace only; neither's answers name the other's file.
        listed = json.loads(sai(env, "mcp", "list", "--json").stdout or "[]")
        mounts = [[m.get("host") for m in i.get("mounts") or []] for e in listed if e.get("name") == name
                  for i in e.get("instances") or []]
        a, b = (str(work.resolve()) for work in works)
        crossed = [hosts for hosts in mounts if a in hosts and b in hosts]
        seen = [LG.MCP_MARKERS[1 - index] for index, result in enumerate(found)
                if LG.MCP_MARKERS[1 - index] in json.dumps(result)]
        apart = sum(1 for hosts in mounts if a in hosts) >= 1 and sum(1 for hosts in mounts if b in hosts) >= 1
        if crossed or seen or not apart:
            record["isolation"] = "failed: " + ("an instance mounts both workspaces" if crossed else
                                                f"an answer names {seen[0]}" if seen else
                                                "not one instance per workspace")
            problems.append(f"isolation: {record['isolation']}")
        else:
            record["isolation"] = "ok (one instance per workspace)"
    record["seconds"] = round(time.monotonic() - begin, 1)
    if problems:
        mcp_record(name, backend, outcome="failed", why="; ".join(problems)[:600], **record)
        pytest.fail(f"{name} ({backend}): " + "\n".join(problems))
    mcp_record(name, backend, outcome="passed", **record)
    print(f"{backend}/{name}: handshake {handshake}, tools {tools}, calls {counts}, isolation "
          f"{record.get('isolation', '-')}")


# --- every harness as Cairn's worker (optional; the user, 2026-11-22) ------------------------------------------------

CAIRN_SCRIPT = r"""
set -u
python3 -I - <<'WATCH'
import json, os, time, urllib.request
base = "http://127.0.0.1:8000"
nonce = "__NONCE__"


def get(path):
    with urllib.request.urlopen(base + path, timeout=30) as answer:
        return json.loads(answer.read())


def put(path, body):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="PUT",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as answer:
        return answer.status


project = None
for _ in range(300):
    try:
        found = [p for p in get("/projects") if nonce in (p.get("title") or "")]
        if found:
            project = found[0]["id"]
            break
    except OSError:
        pass
    time.sleep(1)
print("RESULT project", project or "none", flush=True)
detail, deadline = {}, time.monotonic() + __SECONDS__
while project and time.monotonic() < deadline:
    try:
        detail = get(f"/projects/{project}")
    except OSError:
        time.sleep(5)
        continue
    facts = [f.get("description") or "" for f in detail.get("facts") or []]
    if any(nonce.lower() in text for text in facts) or (detail.get("project") or {}).get("status") == "completed":
        break
    time.sleep(5)
print("RESULT facts", json.dumps([f.get("description") for f in detail.get("facts") or []])[:4000], flush=True)
print("RESULT intents", json.dumps([{"worker": i.get("worker"), "creator": i.get("creator"),
                                     "concluded": bool(i.get("concluded_at"))} for i in detail.get("intents") or []]),
      flush=True)
print("RESULT status", (detail.get("project") or {}).get("status"), flush=True)
if project:
    try:  # nothing more spent: the project stops here whatever it reached
        put(f"/projects/{project}/status", {"status": "stopped"})
    except OSError:
        pass
try:
    print("RESULT relay", json.dumps(open("/sandbox/cairn/relay.log").read()[-4000:]), flush=True)
except OSError as exc:
    print("RESULT relay", json.dumps(f"no relay log: {exc}"), flush=True)
WATCH
exit 0
"""


def part_image(name, env):
    """sandbox-ai's own image NAME (cairn, browser) for Podman: there, or built now (as harness_image does)."""
    image = f"localhost/sandbox-ai-{name}:latest"
    if subprocess.run(["podman", "image", "exists", image], env=env, capture_output=True).returncode == 0:
        return image
    begin = time.monotonic()
    built = sai(env, "build", name, timeout=3600)
    print(f"sandbox-ai build {name}: {'built' if built.returncode == 0 else 'failed'} in "
          f"{time.monotonic() - begin:.0f} s")
    if built.returncode != 0:
        pytest.skip(f"its image cannot be built here (sandbox-ai build {name}): "
                    + (next((line for line in reversed((built.stderr + built.stdout).splitlines()) if line.strip()),
                            "") or f"exit {built.returncode}")[-300:])
    return image


def cairn_results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


@pytest.mark.parametrize("agent", list(LG.CAIRN_WORKERS))
def test_live_cairn_worker(request, tmp_path, agent, models_unloaded_after):
    """Every harness as one of Cairn's workers, through real Cairn in a real pod: the harness is the lead with --llm
    SERVER:MODEL (the first of SAI_LIVE_LLM_MODELS) and nothing else open, --cairn --cairn-workers AGENT, a project
    made at the start whose goal is a code word in lower case. Cairn's dispatcher hands its first task through the
    relay to the harness's own one-shot run (the lead's process is a script that watches the board); the board must
    get a fact with the code word in lower case, from the worker sandbox-AGENT. The project is stopped at the end."""
    if agent in LG.CAIRN_SKIPPED:
        pytest.skip(f"{agent}: {LG.CAIRN_SKIPPED[agent]}")
    entry, env = llm_server(tmp_path, MODELS[0])
    harness_image(agent, env)
    part_image("cairn", env)
    part_image("browser", env)
    work = tmp_path / "w"
    work.mkdir()
    nonce = "LIVE" + secrets.token_hex(4).upper()
    seconds = int(os.environ.get("SAI_LIVE_CAIRN_SECONDS") or 900)
    script = CAIRN_SCRIPT.replace("__NONCE__", nonce).replace("__SECONDS__", str(seconds))
    begin = time.monotonic()
    stdout, stderr = once(request, "podman", agent, work,
                          ["--no-auth", "--no-provider-defaults", "--llm", f"{entry['name']}:{MODELS[0]}",
                           "--cairn", "--cairn-workers", agent, "--cairn-origin", f"The code word is {nonce}.",
                           "--cairn-goal", f"A fact on the board states the code word {nonce} in lower case.",
                           "--cairn-hint", "Answer at once with the fact and complete the project; nothing to "
                           "explore.", "--binary", "/bin/bash"],
                          ["-c", script], env, timeout=seconds + 900)
    print(f"cairn worker {agent} x {MODELS[0]}: {time.monotonic() - begin:.0f} s")
    found = cairn_results(stdout)
    text = f"{json.dumps(found, indent=1)[:6000]}\n{stderr[-2000:]}"
    assert found.get("project", "none") != "none", f"the start's project is not on Cairn's board\n{text}"
    relay_log = json.loads(found.get("relay") or '""')
    assert f"started {agent} task" in relay_log, f"the relay never ran {agent} for a task\n{text}"
    facts = json.loads(found.get("facts") or "[]")
    assert any(nonce.lower() in (fact or "") for fact in facts), \
        f"Cairn took no answer of {agent}'s as a fact with the code word\n{text}"
    intents = json.loads(found.get("intents") or "[]")
    assert any(i.get("worker") == f"sandbox-{agent}" or i.get("creator") == f"sandbox-{agent}" for i in intents) or \
        found.get("status") == "completed", f"no task of the worker sandbox-{agent} on the board\n{text}"


# --- ARTEX beside the agent (optional; the user, 2026-11-22: "check for backdoors ... we still have to be careful") --

ARTEX_PROBE = r'''
import json, os
found = {"uid": os.getuid(), "listeners": [], "artex_env_password": None}
for name in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(name).read().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        fields = line.split()
        if fields[3] == "0A":  # LISTEN
            address, port = fields[1].rsplit(":", 1)
            found["listeners"].append([address, int(port, 16)])
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        command = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        if command and command[0] == b"/opt/artex/bin/artex":
            found["artex_env_password"] = b"SANDBOX_AI_ARTEX_PASSWORD" in open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        pass
print("ARTEX " + json.dumps(found))
'''
LOOPBACK_V4 = "0100007F"  # 127.0.0.1 as /proc/net/tcp writes it
LOOPBACK_V6 = "00000000000000000000000001000000"  # ::1


def artex_request(url, path, method="GET", body=None, headers=None, timeout=30):
    """(status, body) of one request to the forwarded console."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def test_live_artex_beside_the_agent(request, backend, tmp_path, sessions):
    """ARTEX's image built from the reviewed source with sandbox-ai's patches (sandbox-ai build artex, when missing),
    started beside an agent (--artex): its console forwarded to this computer's 127.0.0.1 only, the password of
    sandbox-ai artex console logs in, the first-caller setup and the self-update routes are gone, another Host or
    Origin and a token in the URL are refused; in its container it runs as user 1000, listens on the loopback only
    (PostgreSQL on no TCP port at all), and ARTEX's own environment holds no password."""
    if backend != "podman":
        pytest.skip("--artex runs with Podman only")
    env = environment(tmp_path)
    image = "localhost/sandbox-ai-artex:latest"
    if subprocess.run(["podman", "image", "exists", image], env=env, capture_output=True).returncode != 0:
        begin = time.monotonic()
        built = sai(env, "build", "artex", timeout=3 * 3600)
        print(f"sandbox-ai build artex: exit {built.returncode} in {time.monotonic() - begin:.0f} s")
        assert built.returncode == 0, "sandbox-ai build artex: " + said(built)
    started = session(sessions, request, backend, tmp_path, agent="codex", env=env,
                      options=["--artex", "--allow-tcp", "192.0.2.1:80", *SLEEP])
    started.start(timeout=900)
    console = None
    for _ in range(300):
        done = started.sai("artex", "console", started.name, "--json")
        if done.returncode == 0:
            console = json.loads(done.stdout)
            break
        time.sleep(2)
    assert console, "no console: " + said(done)
    url, password = console["url"], console["password"]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+", url), url
    status, body = artex_request(url, "/api/health")
    assert status == 200 and json.loads(body)["service"] == "artex", body
    status, _ = artex_request(url, "/api/auth/init", "POST", {"password": "someone-else-1"})
    assert status in (403, 404, 405), "the first caller never becomes the admin"
    assert artex_request(url, "/api/auth/login", "POST", {"username": "ARTEX", "password": "wrong-password"})[0] == 401
    status, body = artex_request(url, "/api/auth/login", "POST", {"username": "ARTEX", "password": password})
    assert status == 200, body
    bearer = {"Authorization": "Bearer " + json.loads(body)["token"]}
    assert artex_request(url, "/api/stats", headers=bearer)[0] == 200
    assert artex_request(url, "/api/update/check", headers=bearer)[0] == 404, "no self-update"
    assert artex_request(url, "/api/stats", headers={**bearer, "Origin": "http://evil.example"})[0] == 403
    assert artex_request(url, "/api/stats", headers={**bearer, "Host": "evil.example"})[0] == 403
    token = json.loads(body)["token"]
    assert artex_request(url, f"/api/stats?token={token}")[0] == 401, "a token in the URL only for event streams"
    # On this computer: the forward listens on 127.0.0.1 only.
    port = int(url.rsplit(":", 1)[1])
    listening = [line.split()[1] for name in ("/proc/net/tcp", "/proc/net/tcp6") if Path(name).exists()
                 for line in Path(name).read_text().splitlines()[1:]
                 if line.split()[3] == "0A" and int(line.split()[1].rsplit(":", 1)[1], 16) == port]
    assert listening and all(entry.startswith(LOOPBACK_V4 + ":") for entry in listening), listening
    # In its container.
    probe = subprocess.run(["podman", "exec", console["container"], "/usr/bin/python3", "-I", "-c", ARTEX_PROBE],
                           env=env, capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    found = answer(probe.stdout, "ARTEX")
    assert found["uid"] == 1000 and found["artex_env_password"] is False, found
    ports = {port for _, port in found["listeners"]}
    assert {8787, 8788} <= ports and 5432 not in ports, found
    assert all(address in (LOOPBACK_V4, LOOPBACK_V6) for address, port in found["listeners"]
               if port in (8787, 8788)), found
    print("ARTEX live:", json.dumps({"url": url, "listeners": found["listeners"]}))
