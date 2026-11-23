"""The sandbox-ai installations of a host, end to end (user reports 2026-11-23): after `hosts update` the Hosts page's
version column was empty (the update forgot the host's kept status instead of reading it again) and on coding-101 the
user's terminal ran their own install.sh installation (0.1.188, ~/.local/bin first on PATH through ~/.bashrc) while
`hosts list`, the app's commands and its Update went to a second, system one (0.1.234 in /usr/local/bin: a
non-interactive SSH command reads no ~/.bashrc). Now every installation of a host is listed (which one a terminal
there runs, which one the app's commands run), the app's commands go to the user's own first - the one a terminal
finds - and `hosts uninstall` removes the one chosen (the user's, so that the system one is left).

A stand-in ssh runs each remote command with sh in a remote HOME (PATH without ~/.local/bin, as sshd gives it); the
installations there are real ones, made by this checkout's install.sh (a user's own, and a system one in a prefix
outside the homes); every command is the one a user types: ./sandbox-ai hosts ...
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import release_kit as kit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = kit.VERSION
OLDER = "0.1.150"
APP_OFF = ["--no-app-deps", "--no-app-terminal"]

SSH = r'''#!PYTHON
import json, os, subprocess, sys
config = json.load(open(CONFIG))
args = sys.argv[1:]
open(config["log"], "a").write(json.dumps(args) + "\n")
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
command = args[args.index("--") + 2]
env = config["env"]
os.chdir(env["HOME"])
sys.exit(subprocess.run(["sh", "-c", command], env=env).returncode)
'''
SUDO = r'''#!/bin/sh
# sudo there: what ~/sudo says (nopasswd, password); -n never asks
mode=$(cat "$HOME/sudo" 2>/dev/null || echo nopasswd)
batch=0
[ "$1" = -n ] && { batch=1; shift; }
[ "$1" = -- ] && shift
echo "$batch $*" >> "$HOME/sudo.log"
if [ "$mode" = password ] && [ $batch = 1 ]; then echo "sudo: a password is required" >&2; exit 1; fi
exec "$@"
'''

pytestmark = pytest.mark.skipif(not shutil.which("git") or not shutil.which("bash"), reason="needs git and bash")


def system_prefix():
    """A folder in nobody's home where programs may run (a system installation's prefix there is not a user's own)."""
    for base in ("/var/tmp", "/tmp", tempfile.gettempdir(), "/dev/shm"):
        if not os.access(base, os.W_OK) or base.startswith(("/home/", "/root/", "/Users/", "/var/home/")):
            continue
        folder = Path(tempfile.mkdtemp(prefix="sandbox-ai-system-", dir=base))
        probe = folder / "probe"
        probe.write_text("#!/bin/sh\nexit 0\n")
        probe.chmod(0o755)
        try:
            runs = subprocess.run([str(probe)], capture_output=True).returncode == 0
        except OSError:
            runs = False
        probe.unlink()
        if runs:
            return folder
        shutil.rmtree(folder, ignore_errors=True)
    return None


@pytest.fixture(scope="module")
def older(tmp_path_factory):
    """This checkout as version OLDER (its release keys kept): an installation the update replaces."""
    base = tmp_path_factory.mktemp("older")
    tree = kit.tree_copy(base / "tree", OLDER)
    shutil.copy2(ROOT / "sandbox/release-signers", tree / "sandbox/release-signers")
    kit.commit(tree, "the release keys")
    yield tree
    kit.remove(base)


class Host:
    """The host ct1: its HOME (REMOTE), the environment its commands get (sshd's PATH, no ~/.local/bin), and this
    machine's (LOCAL) where ./sandbox-ai hosts runs."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.remote = tmp_path / "remote-home"
        self.rbin = self.remote / "rbin"
        self.rbin.mkdir(parents=True)
        (self.rbin / "sudo").write_text(SUDO)
        (self.rbin / "sudo").chmod(0o755)
        # (a login shell there reads ~/.profile, which reads ~/.bashrc - Debian's own does: install.sh's PATH block)
        (self.remote / ".profile").write_text('[ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"\n')
        self.prefixes = []
        self.config = tmp_path / "ssh.json"
        self.log = tmp_path / "ssh.log"
        self.write_config()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1).replace("CONFIG", repr(str(self.config))))
        (bin_dir / "ssh").chmod(0o755)
        local = tmp_path / "local-home"
        local.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("XDG_", "SANDBOX_AI_", "PYTHON"))}
        self.env.update(HOME=str(local), PATH=f"{bin_dir}:/usr/bin:/bin", TMPDIR=str(tmp_path), NO_COLOR="1",
                        XDG_CONFIG_HOME=str(local / ".config"), XDG_CACHE_HOME=str(local / ".cache"),
                        XDG_STATE_HOME=str(local / ".local/state"), XDG_DATA_HOME=str(local / ".local/share"),
                        SANDBOX_AI_SSH_MULTIPLEX="0")
        assert self.sandbox_ai("hosts", "add", "ct1", "user@ct1").returncode == 0

    def write_config(self):
        path = ":".join([str(self.rbin), *(f"{prefix}/bin" for prefix in self.prefixes), "/usr/bin", "/bin"])
        env = {"HOME": str(self.remote), "PATH": path, "TMPDIR": str(self.tmp), "SHELL": "/bin/bash", "NO_COLOR": "1",
               "LANG": "C.UTF-8"}
        self.config.write_text(json.dumps({"env": env, "log": str(self.log)}))

    def sandbox_ai(self, *args):
        return subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True, env=self.env,
                              timeout=600, stdin=subprocess.DEVNULL)

    def json(self, *args):
        done = self.sandbox_ai(*args, "--json")
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise AssertionError(done.stdout + done.stderr) from None

    def status(self, *how):
        """ct1's sandbox_ai of hosts list --json (HOW: --cached, --refresh)."""
        return next(entry for entry in self.json("hosts", "list", *how) if entry["name"] == "ct1")["sandbox_ai"]

    def there(self, *argv):
        """A command on the host as its user (sshd's environment)."""
        env = json.loads(self.config.read_text())["env"]
        return subprocess.run([str(word) for word in argv], capture_output=True, text=True, env=env, timeout=120,
                              cwd=str(self.remote), stdin=subprocess.DEVNULL)

    def install_user(self, source=ROOT):
        """install.sh --user there (with its PATH block in ~/.bashrc, as for a user)."""
        done = self.there("bash", ROOT / "install.sh", "--from", source, "--user", *APP_OFF)
        assert done.returncode == 0, done.stderr[-3000:]

    def install_system(self, source=ROOT):
        """install.sh --system --prefix P there (P/bin on the SSH command's PATH, as /usr/local/bin is)."""
        prefix = system_prefix()
        if prefix is None:
            pytest.skip("no folder outside the homes to install into")
        self.prefixes.append(prefix)
        self.write_config()
        done = self.there("bash", ROOT / "install.sh", "--from", source, "--system", "--prefix", prefix, *APP_OFF)
        assert done.returncode == 0, done.stderr[-3000:]
        return prefix

    def not_root(self):
        """The SSH user there is no root (this test may run as root): id -u says 1000."""
        (self.rbin / "id").write_text('#!/bin/sh\nif [ "$1" = -u ]; then echo 1000; else /usr/bin/id "$@"; fi\n')
        (self.rbin / "id").chmod(0o755)

    def close(self):
        for prefix in self.prefixes:
            kit.remove(prefix)
        kit.remove(self.tmp)


@pytest.fixture
def host(tmp_path):
    found = Host(tmp_path)
    yield found
    found.close()


def in_process(monkeypatch, host):
    """sandbox/hosts.py in this process, with the environment of HOST's ./sandbox-ai (what the app calls)."""
    for key in [key for key in os.environ if key.startswith(("XDG_", "SANDBOX_AI_"))]:
        monkeypatch.delenv(key)
    for key, value in host.env.items():
        monkeypatch.setenv(key, value)
    return kit.load(ROOT / "sandbox/hosts.py", "hosts_for_installations")


def own(host):
    return host.remote / ".local/bin/sandbox-ai"


# -- after an update: the version at once ------------------------------------------------------------------------------

def test_after_an_update_the_list_names_the_new_version_at_once(host, older):
    """The user: "wenn man unter Hosts updated hat, verschwindet die Version in der Spalte". hosts update forgot the
    host's kept status; the Hosts page then read the kept statuses (no SSH) and found none: an empty column. Now the
    update reads the host's sandbox-ai again and keeps it: hosts list --cached names the new version."""
    host.install_user(older)
    assert host.status("--refresh")["version"] == OLDER
    result = host.json("hosts", "update", "ct1", "--from-here")[0]
    assert (result["before"], result["after"], result["changed"], result["error"]) == (OLDER, INSTALLED, True, None)
    kept = host.status("--cached")
    assert kept is not None and kept["version"] == INSTALLED and kept["kind"] == "user", kept
    assert [(item["kind"], item["version"]) for item in kept["installations"]] == [("user", INSTALLED)]
    # and on the host itself, as its user types it
    assert host.there(own(host), "--version").stdout.strip() == f"sandbox-ai {INSTALLED}"
    lines = host.sandbox_ai("hosts", "list", "--cached").stdout
    assert f"{INSTALLED}" in next(line for line in lines.splitlines() if line.startswith("ct1")), lines


# -- two installations: the user's own and a system one ----------------------------------------------------------------

def test_every_installation_is_listed_and_the_users_own_is_the_one_that_runs(host, older):
    """coding-101: the user's own installation (0.1.188, ~/.local/bin first in a terminal) and a system one
    (0.1.234, /usr/local/bin first for a non-interactive SSH command). Both are listed with which one a terminal there
    runs; the app's commands go to the same one (the user's), and hosts update updates that one."""
    prefix = host.install_system()
    host.install_user(older)
    status = host.status("--refresh")
    assert (status["version"], status["kind"]) == (OLDER, "user"), status
    found = {item["kind"]: item for item in status["installations"]}
    assert (found["user"]["version"], found["system"]["version"]) == (OLDER, INSTALLED), status["installations"]
    assert found["user"]["commands"] and found["user"]["terminal"], "the app's commands and a terminal: the same"
    assert not found["system"]["commands"] and found["system"]["prefix"] == str(prefix)
    assert status["differ"] is False
    line = next(line for line in host.sandbox_ai("hosts", "list", "--cached").stdout.splitlines()
                if line.startswith("ct1"))
    assert OLDER in line and f"system {INSTALLED}" in line, line
    # every command of the app runs the user's own: the version, a --help, hosts run
    assert host.sandbox_ai("hosts", "run", "ct1", "--", "--version").stdout.strip() == f"sandbox-ai {OLDER}"
    # update: the user's own (from here: no release server in this test), the system one stays as it was
    result = host.json("hosts", "update", "ct1", "--from-here")[0]
    assert (result["before"], result["after"], result["error"]) == (OLDER, INSTALLED, None), result
    assert result["mode"] == "user"
    assert host.there(own(host), "--version").stdout.strip() == f"sandbox-ai {INSTALLED}"
    assert host.there(Path(prefix) / "bin/sandbox-ai", "--version").stdout.strip() == f"sandbox-ai {INSTALLED}"
    assert host.status("--cached")["version"] == INSTALLED


def test_a_terminal_that_runs_another_installation_than_the_apps_commands_is_said(host):
    """A login shell there whose PATH puts the system installation first (its ~/.bashrc without install.sh's block,
    PATH set by hand) runs another sandbox-ai than the app's commands: said in the list, both named."""
    prefix = host.install_system()
    host.install_user()
    (host.remote / ".bashrc").write_text(f'export PATH="{prefix}/bin:$PATH"\n')
    status = host.status("--refresh")
    assert status["differ"] is True, status
    terminal = next(item for item in status["installations"] if item["terminal"])
    assert terminal["kind"] == "system"
    line = next(line for line in host.sandbox_ai("hosts", "list", "--cached").stdout.splitlines()
                if line.startswith("ct1"))
    assert "a terminal there runs system" in line, line


# -- uninstall on a host --------------------------------------------------------------------------------------------

def test_uninstall_the_users_own_leaves_the_system_one(host):
    """The user: "uninstall user/root sodass man nachher nur noch eine systemweite Install hat". hosts uninstall
    ct1 --installation user runs that installation's own `sandbox-ai uninstall` there: its links, its folder and its
    PATH block go, the system one stays and is the one that runs from then on; the host stays in the list."""
    prefix = host.install_system()
    host.install_user()
    assert "# >>> sandbox-ai >>>" in (host.remote / ".bashrc").read_text()
    (host.remote / ".local/share/sandbox-ai/kits").mkdir(parents=True)  # the user's data: kept without --purge
    plan = host.sandbox_ai("hosts", "uninstall", "ct1", "--installation", "user", "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "WHAT" in plan.stdout and "Command links" in plan.stdout and str(host.remote) in plan.stdout
    assert own(host).is_symlink(), "a dry run removes nothing"
    refused = host.sandbox_ai("hosts", "uninstall", "ct1", "--installation", "user")
    assert refused.returncode != 0 and "--yes" in refused.stderr + refused.stdout and own(host).is_symlink()
    result = host.json("hosts", "uninstall", "ct1", "--installation", "user", "--yes")
    assert result["error"] is None and result["removed"]["kind"] == "user", result
    assert not own(host).exists() and not own(host).is_symlink()
    assert not (host.remote / ".local/share/sandbox-ai/versions").exists()
    assert (host.remote / ".local/share/sandbox-ai/kits").is_dir()
    assert "# >>> sandbox-ai >>>" not in (host.remote / ".bashrc").read_text()
    assert host.there(Path(prefix) / "bin/sandbox-ai", "--version").returncode == 0
    status = host.status("--cached")
    assert (status["version"], status["kind"]) == (INSTALLED, "system"), status
    assert [item["kind"] for item in status["installations"]] == ["system"]
    assert "ct1" in [entry["name"] for entry in host.json("hosts", "list", "--cached")]


def test_uninstall_asks_which_when_there_are_two_and_refuses_what_it_cannot_do(host):
    host.install_system()
    host.install_user()
    done = host.sandbox_ai("hosts", "uninstall", "ct1", "--yes")
    assert done.returncode != 0 and "--installation user" in done.stderr and "--installation system" in done.stderr
    done = host.sandbox_ai("hosts", "uninstall", "local", "--yes")
    assert done.returncode != 0 and "sandbox-ai uninstall" in done.stderr, done.stderr
    done = host.sandbox_ai("hosts", "uninstall", "ct1", "--installation", "user", "--secrets", "--yes")
    assert done.returncode != 0 and "--secrets goes with --purge" in done.stderr
    host.not_root()
    done = host.json("hosts", "uninstall", "ct1", "--installation", "system", "--purge", "--yes")
    assert done["error"] and "--purge" in done["error"] and own(host).is_symlink(), done
    assert not (host.remote / "sudo.log").exists(), "refused before anything ran there"


def test_uninstall_a_system_installation_with_sudo_and_remove_the_host(host, monkeypatch):
    """A system installation is root's: removed with sudo -n (never a password prompt); sudo that wants a password
    is said, with the terminal's command that asks. --remove-from-list also forgets the host."""
    prefix = host.install_system()
    host.not_root()
    (host.remote / "sudo").write_text("password")
    result = host.json("hosts", "uninstall", "ct1", "--yes")
    assert result["needs_password"] and "password" in result["error"], result
    assert (Path(prefix) / "bin/sandbox-ai").is_symlink()
    terminal = in_process(monkeypatch, host).uninstall_terminal("ct1", "system")
    script = in_process(monkeypatch, host).neutral_script(terminal[-1])
    assert terminal[1] == "-tt" and script.startswith("exec sudo -- ") and "uninstall --yes" in script, script
    (host.remote / "sudo").write_text("nopasswd")
    result = host.json("hosts", "uninstall", "ct1", "--yes", "--remove-from-list")
    assert result["error"] is None and result["sudo"] and result["removed"]["kind"] == "system", result
    assert not (Path(prefix) / "bin/sandbox-ai").exists() and not (Path(prefix) / "lib/sandbox-ai").exists()
    assert (host.remote / "sudo.log").read_text().startswith("1 ")
    assert [entry["name"] for entry in host.json("hosts", "list", "--cached")] == ["local"]
