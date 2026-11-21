#!/usr/bin/env python3
"""sandbox-ai login: logins for sandboxes, kept apart from the host's own.

  sandbox-ai login claude|codex|opencode|hermes|qwen|pi [launcher options] [-- login command arguments]
  sandbox-ai login --status [--json] [--sessions [--json]] [--host HOST]
  sandbox-ai login claude|codex|opencode|hermes|qwen|pi --logout
  sandbox-ai login claude|codex|opencode|hermes|qwen|pi --from-session NAME [--yes] [--replace] [--dry-run]
                   [--expect FINGERPRINT] [--json] [--host HOST]
  sandbox-ai login give RUN [AGENT] [--from-session NAME] [--yes] [--restart] [--dry-run] [--expect FINGERPRINT]
                   [--json] [--host HOST]

The agent's own login command runs inside a sandbox with a fresh HOME. Its credential file is
then checked and kept in ~/.local/share/sandbox-ai/auth/AGENT (mode 0600). From then on every
sandbox of that agent gets its login from there instead of from the host, and tokens a sandbox
refreshes go back there. The host's login stays untouched, so host and sandboxes never use up
each other's rotating refresh tokens.

Codex's login is its device sign-in (codex login --device-auth; Hermes' Nous Portal login and Qwen OAuth are device
sign-ins too): on a terminal the page and the one-time code are said again on lines of their own, and with a desktop
here the page is opened in your browser after one key (Enter: yes) - SANDBOX_AI_LOGIN_BROWSER=open opens it without
the question, =no never; over SSH nothing opens there (device_signin.py).

--from-session NAME takes the login a session (sandbox-ai list) has in its HOME as the central login: its account is
shown first (as the file says), then asked (or --yes); a central login of another account needs --replace.
--status --sessions shows which login each running or kept session uses (never a token: a fingerprint of it).
give RUN puts the central login of RUN's agent (or, with --from-session NAME, another session's) into the HOME of a
sandbox that runs, as a start does: shown first, then asked (or --yes). Claude Code reads it again by itself; the
other agents are started again inside the sandbox (asked, or --restart: sandbox-ai kits restart-agent RUN).
See session_login.py.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
AGENTS = ("claude", "codex", "opencode", "hermes", "qwen", "pi")
# Not DSH: its DeepSeek account sign-in is its web UI's (OAuth to a server on the sandbox's loopback, in the sandbox's
# own browser); a key goes with --api-key api.deepseek.com=DEEPSEEK_API_KEY.
# The agent's own login command. Codex's device code flow needs no callback to localhost, which
# the browser on the host could not reach inside the sandbox.
# Hermes: the Nous Portal's device login (no callback either; portal.nousresearch.com is Hermes' provider host).
LOGIN_ARGS = {"claude": ["auth", "login"], "codex": ["login", "--device-auth"], "opencode": ["auth", "login"],
              "hermes": ["auth", "add", "nous"],
              # Qwen Code has no login command (qwen auth was removed): started without one, it opens its auth
              # dialog; Qwen OAuth there is a device login (its URL through the login browser). /quit when done.
              "qwen": [],
              # Pi has no login command either: started without one, its terminal UI takes /login (a provider's
              # OAuth with a pasted code or redirect URL, or a key), kept in ~/.pi/agent/auth.json. /quit when done.
              "pi": []}
# OpenCode logs in to other providers (Anthropic, OpenAI, ...); allow their hosts for the login.
LOGIN_OPTIONS = {"opencode": ["--profile", "frontier"], "pi": ["--profile", "frontier"]}
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_login", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def collect(agent, home):
    """{relative path: bytes} to store from the login sandbox's HOME, or {} if it holds no login.
    HOME was the agent's to write: no symlinks are followed, sizes are bounded, contents are checked."""
    l = launcher()
    target = l.AUTH_TARGETS[agent]
    try:
        path = l.check_home_path(home, home / target)
        data = l.read_config_bytes(path)
    except (OSError, ValueError):
        return {}
    if agent == "claude":
        oauth = l.claude_oauth(path, follow=False)
        if oauth is None:
            return {}
        login = {**l.oauth_fields(oauth, l.OAUTH_INFO_FIELDS), **l.oauth_fields(oauth, l.OAUTH_TOKEN_FIELDS)}
        files = {target: json.dumps({"claudeAiOauth": login}, indent=2).encode()}
        try:
            state = json.loads(l.read_config_bytes(l.check_home_path(home, home / ".claude.json"),
                                                   l.CLAUDE_STATE_LIMIT))
        except (OSError, ValueError, RecursionError):
            state = None
        if isinstance(state, dict):
            subset = {key: state[key] for key in l.CLAUDE_STATE_KEYS if key in state}
            files[".claude.json"] = json.dumps(subset, indent=2).encode()
        return files
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        return {}
    return {target: json.dumps(document, indent=2).encode()} if l.login_document_ok(agent, document) else {}


def write_store(agent, files):
    """Make the stored login of AGENT exactly FILES: each file replaced atomically in place (the store
    never disappears, so a sandbox starting meanwhile never falls back to the host), then files that
    are not part of the new login removed."""
    store = launcher().auth_store(agent)
    if store.is_symlink():
        raise ValueError(f"{store} is a symlink; refusing to replace it")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if agent == "claude":  # running sandboxes compare and swap Claude's login under this lock
        with launcher().claude_login_lock():
            replace_store(agent, store, files)
    else:
        replace_store(agent, store, files)


def replace_store(agent, store, files):
    """write_store's replacement of the files in STORE, once it may write."""
    # Claude's credentials last: a sandbox that sees the new login also sees its account state.
    for relative in sorted(files, key=lambda name: name == launcher().AUTH_TARGETS[agent]):
        path = store / relative
        for parent in reversed(list(Path(relative).parents)[:-1]):  # without '.'
            directory = store / parent
            if directory.is_symlink():
                raise ValueError(f"{directory} is a symlink; refusing to write through it")
            directory.mkdir(mode=0o700, exist_ok=True)
        launcher().write_private_file(path, files[relative])
    for path in sorted(store.rglob("*"), reverse=True):
        relative = str(path.relative_to(store))
        if path.is_symlink() or path.is_file():
            if relative not in files:
                path.unlink()
        elif path.is_dir() and not any(name.startswith(relative + "/") for name in files):
            shutil.rmtree(path, ignore_errors=True)


def login(agent, options=(), login_args=None, out=None):
    """Run the agent's login inside a sandbox and keep the result; 0 on success."""
    out = out or (lambda line: print(line, file=sys.stderr))
    l = launcher()
    base = l.auth_store()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    base.chmod(0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".login-{agent}-", dir=base))
    try:
        home, work = staging / "home", staging / "work"
        for path in (home, work):
            path.mkdir(mode=0o700)
        # --binary keeps the launcher from adding agent options in front of the login subcommand.
        cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), agent, *LOGIN_OPTIONS.get(agent, []), *options,
               "--no-auth", "--no-config", "--no-tmux", "--no-proxy", "--no-default-kit", "--no-memory", "--no-plugins",
               "--binary", agent, "--state-dir", str(home),
               "--workspace", str(work), "--", *(login_args or LOGIN_ARGS[agent])]
        out(f"sandbox-ai login: running `{agent} {' '.join(login_args or LOGIN_ARGS[agent])}` in a sandbox; "
            "open the link it shows in your browser")
        status = run_login(agent, cmd)
        files = collect(agent, home)
        if not files:
            out(f"sandbox-ai login: no {agent} login was created (exit status {status}); nothing stored")
            return 1
        write_store(agent, files)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    out(f"sandbox-ai login: stored the {agent} login in {l.auth_store(agent)}; every {agent} sandbox uses it "
        "from now on (the host's login is not used or changed)")
    return 0


def device_signin():
    """device_signin.py (a device sign-in read from the login's output), loaded once."""
    loaded = sys.modules.get("sandbox_ai_device_signin")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_device_signin", HERE / "device_signin.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        loaded = sys.modules.setdefault("sandbox_ai_device_signin", loaded)
    return loaded


def run_login(agent, cmd):
    """The login sandbox CMD of AGENT, its exit status. A device sign-in (Codex's, Hermes', Qwen Code's) on a
    terminal runs with its output watched (device_signin.run): the page and the one-time code are said on lines of
    their own and the page is opened in the human's browser, asked first. In a script, in a terminal tab of the
    desktop app (which watches the output itself) and for every other agent: as it always ran."""
    signin = device_signin()
    if agent not in signin.AGENTS or os.name != "posix" or not (sys.stdin.isatty() and sys.stdout.isatty()) \
            or os.environ.get(signin.APP_TAB):
        return subprocess.run(cmd).returncode
    hosts = launcher().PROVIDERS.get(agent, [])
    try:
        status, _ = signin.run(cmd, hosts)
    except (ImportError, OSError) as exc:  # no pseudo-terminal here: the login itself, as before
        print(f"sandbox-ai login: the sign-in's output is not watched ({exc}); open the link it shows yourself",
              file=sys.stderr)
        return subprocess.run(cmd).returncode
    return status if status >= 0 else 128 - status


def logout(agent, out=print):
    store = launcher().auth_store(agent)
    if store.is_symlink():
        store.unlink()
    elif store.exists():
        shutil.rmtree(store)
    else:
        out(f"No stored {agent} login; sandboxes use the host's login")
        return 1
    out(f"Removed the stored {agent} login; sandboxes use the host's login again")
    return 0


def status(out=print):
    l = launcher()
    for agent in AGENTS:
        where, problem = l.login_state(agent)
        source = l.auth_sources(agent)[0][0]
        place = "login store" if where == "store" else "host"
        detail = {"missing": "no login", "expired": "expired, log in again"}.get(problem, "logged in")
        if problem is None and agent == "claude":
            oauth = l.claude_oauth(source, follow=where == "host")
            hours = (oauth["expiresAt"] / 1000 - time.time()) / 3600
            detail += f", access token valid for {hours:.1f} h" if hours > 0 else ", access token refreshes on use"
            if "refreshTokenExpiresAt" in oauth:  # the provider's own figure, if Claude stored one
                days = (oauth["refreshTokenExpiresAt"] / 1000 - time.time()) / 86400
                detail += f", renewable for {days:.0f} more days" if days >= 1 else ", renewable for less than a day"
        out(f"{agent:9} {place:12} {detail}  ({l.printable(source)})")
    return 0


def status_data():
    """`sandbox-ai login --status --json`: {"agents": {AGENT: {"where": "store" (sandbox-ai's login store) or "host"
    (this computer's own login), "problem": None, "missing" or "expired", "logged_in", "source": the file}}} - where
    the sandboxes of each agent get their login from and whether there is one. Paths only, never a token."""
    l = launcher()
    agents = {}
    for agent in AGENTS:
        where, problem = l.login_state(agent)
        agents[agent] = {"where": where, "problem": problem, "logged_in": problem is None,
                         "source": str(l.auth_sources(agent)[0][0])}
    return {"agents": agents}


def cli(argv):
    if argv[:1] == ["give"]:  # a login for a sandbox that runs (session_login.cli_give: its own options)
        return session_login().cli_give(argv[1:])
    if "--" in argv:
        position = argv.index("--")
        argv, login_args = argv[:position], argv[position + 1:]
    else:
        login_args = None
    # The agent may come before or after launcher options (sandbox-ai login --backend bwrap claude).
    agents = [arg for arg in argv if arg in AGENTS]
    if agents:
        argv = list(argv)
        argv.remove(agents[0])
        argv.insert(0, agents[0])
    parser = argparse.ArgumentParser(prog="sandbox-ai login", description=__doc__.split("\n\n")[0],
                                     epilog="Launcher options (e.g. --backend bwrap, --podman-url URL) are "
                                            "passed on to the login sandbox. A login for a sandbox that runs "
                                            "already: sandbox-ai login give RUN [AGENT] [--from-session NAME] "
                                            "(sandbox-ai login give --help).")
    parser.add_argument("-V", "--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    parser.add_argument("agent", nargs="?", choices=AGENTS)
    parser.add_argument("-s", "--status", action="store_true", help="Show which login each agent's sandboxes get")
    parser.add_argument("-L", "--logout", action="store_true", help="Forget the stored login of AGENT")
    parser.add_argument("--sessions", action="store_true",
                        help="With --status: which login each running or kept session uses (central, this "
                             "computer's, a file, from a session), its account as the file says, kind, expiry, "
                             "last refresh and fingerprint - never a token")
    parser.add_argument("--from-session", metavar="NAME",
                        help="Import the login of this session (sandbox-ai list) as the central login of AGENT: its "
                             "account is shown first, then asked (or --yes)")
    parser.add_argument("--yes", action="store_true", help="With --from-session: import without asking")
    parser.add_argument("--replace", action="store_true",
                        help="With --from-session: replace a central login of another account (or of one that "
                             "cannot be told)")
    parser.add_argument("--dry-run", action="store_true", help="With --from-session: show what would be imported, "
                                                               "import nothing")
    parser.add_argument("--expect", metavar="FINGERPRINT",
                        help="With --from-session: import only the login with this fingerprint (the one shown)")
    parser.add_argument("--json", action="store_true",
                        help="With --status, --sessions or --from-session: as JSON (--status --json: for each agent "
                             "where its sandboxes' login comes from and whether there is one)")
    parser.add_argument("--host", metavar="HOST",
                        help="With --status or --from-session: on this host of sandbox-ai hosts (over SSH)")
    a, options = parser.parse_known_args(argv)
    extra = [name for name in ("yes", "replace", "dry_run", "expect") if getattr(a, name) not in (False, None)]
    if extra and not a.from_session:
        parser.error(f"--{extra[0].replace('_', '-')} goes with --from-session")
    if a.sessions and not a.status:
        parser.error("--sessions goes with --status")
    if a.json and not (a.status or a.from_session):
        parser.error("--json goes with --status, --status --sessions or --from-session")
    if a.status:
        if a.sessions:
            return session_login().cli_sessions(a)
        if a.host not in (None, "", "local"):
            return remote_status(a.host, as_json=a.json)
        if a.json:
            print(json.dumps(status_data(), indent=2))
            return 0
        return status()
    if a.host not in (None, "", "local") and not a.from_session:
        parser.error("--host goes with --status or --from-session (a login on another host: sandbox-ai hosts run "
                     "HOST -- login AGENT)")
    if not a.agent:
        parser.error("name an agent, or use --status")
    if a.from_session:
        if a.logout or options or login_args is not None:
            parser.error("--from-session imports a session's login: no --logout, launcher options or login "
                         "arguments with it")
        return session_login().cli_import(a)
    if a.logout:
        return logout(a.agent)
    return login(a.agent, options, login_args)


def session_login():
    """session_login.py (a session's login: listed, imported), sharing this command's launcher."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_session_login", HERE / "session_login.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    loaded._modules["launcher"] = launcher()
    return loaded


def remote_status(host, out=print, as_json=False):
    """sandbox-ai login --status [--json] on HOST (over SSH), its lines as they are (escaped)."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_hosts_for_login", HERE / "hosts.py")
    hosts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hosts)
    code, said, err = hosts.run(host, ["login", "--status", *(["--json"] if as_json else [])], timeout=90)
    for line in (said if code == 0 else err or said).splitlines():
        out(launcher().printable(line))
    return code


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai login: {exc}", file=sys.stderr)
        sys.exit(1)
