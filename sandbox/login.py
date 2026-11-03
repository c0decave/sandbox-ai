#!/usr/bin/env python3
"""sandbox-ai login: logins for sandboxes, kept apart from the host's own.

  sandbox-ai login claude|codex|opencode [launcher options] [-- login command arguments]
  sandbox-ai login --status
  sandbox-ai login claude|codex|opencode --logout

The agent's own login command runs inside a sandbox with a fresh HOME. Its credential file is
then checked and kept in ~/.local/share/sandbox-ai/auth/AGENT (mode 0600). From then on every
sandbox of that agent gets its login from there instead of from the host, and tokens a sandbox
refreshes go back there. The host's login stays untouched, so host and sandboxes never use up
each other's rotating refresh tokens.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
AGENTS = ("claude", "codex", "opencode")
# The agent's own login command. Codex's device code flow needs no callback to localhost, which
# the browser on the host could not reach inside the sandbox.
LOGIN_ARGS = {"claude": ["auth", "login"], "codex": ["login", "--device-auth"], "opencode": ["auth", "login"]}
# OpenCode logs in to other providers (Anthropic, OpenAI, ...); allow their hosts for the login.
LOGIN_OPTIONS = {"opencode": ["--profile", "frontier"]}
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_login", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
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
               "--no-auth", "--no-config", "--no-tmux", "--no-default-kit", "--no-memory", "--no-plugins",
               "--binary", agent, "--state-dir", str(home),
               "--workspace", str(work), "--", *(login_args or LOGIN_ARGS[agent])]
        out(f"sandbox-ai login: running `{agent} {' '.join(login_args or LOGIN_ARGS[agent])}` in a sandbox; "
            "open the link it shows in your browser")
        status = subprocess.run(cmd).returncode
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
        out(f"{agent:9} {place:12} {detail}  ({l.printable(source)})")
    return 0


def cli(argv):
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
                                            "passed on to the login sandbox.")
    parser.add_argument("--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    parser.add_argument("agent", nargs="?", choices=AGENTS)
    parser.add_argument("--status", action="store_true", help="Show which login each agent's sandboxes get")
    parser.add_argument("--logout", action="store_true", help="Forget the stored login of AGENT")
    a, options = parser.parse_known_args(argv)
    if a.status:
        return status()
    if not a.agent:
        parser.error("name an agent, or use --status")
    if a.logout:
        return logout(a.agent)
    return login(a.agent, options, login_args)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai login: {exc}", file=sys.stderr)
        sys.exit(1)
