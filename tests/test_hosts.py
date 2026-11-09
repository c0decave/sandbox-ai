"""sandbox-ai hosts (sandbox/hosts.py): other machines (LXC containers) whose sandbox-ai is managed over SSH. A
stand-in ssh runs the remote command with sh, in a remote HOME whose PATH has no sandbox-ai (a non-interactive
login reads no ~/.bashrc): quoting and the fallback to ~/.local/bin are tested for real."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hosts_for_tests", ROOT / "sandbox" / "hosts.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A stand-in ssh on PATH and a remote HOME with sandbox-ai in ~/.local/bin only. The stand-in ssh refuses a key
    file others can read, as ssh does; the remote sandbox-ai answers version --verbose, update --check and update
    as an installation of the kind in ~/kind (user, system, checkout, pip, old), its version in ~/version; a sudo
    on the remote PATH does what ~/sudo says (nopasswd, password, refused)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "local-home"))  # ~/.ssh: the keys add and test may make private
    (tmp_path / "local-home").mkdir()
    bin_dir, home = tmp_path / "bin", tmp_path / "remote-home"
    (home / ".local/bin").mkdir(parents=True)
    (home / "rbin").mkdir()
    bin_dir.mkdir()
    log = tmp_path / "ssh.json"
    (bin_dir / "ssh").write_text(f"""#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(args) + "\\n")
if "unreachable" in args:
    print("ssh: connect to host unreachable port 22: Connection refused", file=sys.stderr)
    sys.exit(255)
if "-i" in args:
    key = args[args.index("-i") + 1]
    if os.path.exists(key) and os.stat(key).st_mode & 0o077:
        print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@", file=sys.stderr)
        print("@         WARNING: UNPROTECTED PRIVATE KEY FILE!          @", file=sys.stderr)
        print(f"Permissions 0{{os.stat(key).st_mode & 0o777:o}} for '{{key}}' are too open.", file=sys.stderr)
        print("This private key will be ignored.", file=sys.stderr)
        print(f'Load key "{{key}}": bad permissions', file=sys.stderr)
        print("user@ct1: Permission denied (publickey).", file=sys.stderr)
        sys.exit(255)
command = args[args.index("--") + 2]
env = {{"HOME": {str(home)!r}, "PATH": {str(home / "rbin")!r} + ":/usr/bin:/bin"}}
sys.exit(subprocess.run(["sh", "-c", command], env=env).returncode)
""")
    (bin_dir / "ssh").chmod(0o755)
    (home / "rbin/sudo").write_text(f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
home = {str(home)!r}
mode = open(os.path.join(home, "sudo")).read().strip() if os.path.exists(os.path.join(home, "sudo")) else "nopasswd"
batch = args[:1] == ["-n"]
args = args[1:] if batch else args
args = args[1:] if args[:1] == ["--"] else args
open(os.path.join(home, "sudo.log"), "a").write(("-n " if batch else "") + " ".join(args) + "\\n")
if mode == "refused":
    print("user is not in the sudoers file.", file=sys.stderr)
    sys.exit(1)
if mode == "password" and batch:
    print("sudo: a password is required", file=sys.stderr)
    sys.exit(1)
os.environ["FAKE_ROOT"] = "1"
os.execv(args[0], args)
""")
    (home / "rbin/sudo").chmod(0o755)
    # The folders a system installation depends on (hosts.prefix_problem) as on a usual host: root's, 0755. The
    # machine running the tests has its own owners and modes (a CI runner owns /tmp/pytest-of-runner, its
    # /usr/local/bin is 0777); a test that is about them fakes its own.
    (home / "rbin/ls").write_text('#!/bin/sh\nif [ "$1" = -ndL ]; then echo "drwxr-xr-x 2 0 0 0 Jan 1 00:00 $2"; '
                                  'else exec /bin/ls "$@"; fi\n')
    (home / "rbin/ls").chmod(0o755)
    (home / ".local/bin/sandbox-ai").write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
home = os.environ["HOME"]
def read(name, default):
    path = os.path.join(home, name)
    return open(path).read().strip() if os.path.exists(path) else default
version, kind = read("version", "0.1.30"), read("kind", "user")
where = {{"user": home + "/.local/share/sandbox-ai/versions/" + version,
         "system": "/opt/sai/lib/sandbox-ai/versions/" + version, "checkout": "/srv/sandbox-ai",
         "pip": "/usr/lib/python3/site-packages"}}.get(kind)
if args == ["--version"]:
    print("sandbox-ai " + version)
elif args == ["version", "--verbose"]:
    print("sandbox-ai " + version)
    if kind != "old":
        print("installed in: " + where)
        print("updates: not through install.sh (pip install -U sandbox-ai)" if kind == "pip" else
              "updates: must be signed by one of these keys (sandbox-ai update)")
elif args == ["update", "--check"]:
    if kind in ("checkout", "pip"):
        print("sandbox-ai update: this copy was not installed by install.sh", file=sys.stderr)
        sys.exit(1)
    print("installed: " + (version if kind == "user" or os.environ.get("FAKE_ROOT") else "none") + " (somewhere)")
    print("available: " + read("available", "0.1.66") + " (https://github.com/c0decave/sandbox-ai)")
elif args[:1] == ["update"]:
    open(os.path.join(home, "update.log"), "a").write(json.dumps({{"args": args, "root": bool(os.environ.get("FAKE_ROOT"))}}) + "\\n")
    if "--system" in args and not os.environ.get("FAKE_ROOT"):
        print("install.sh: --system needs root: sudo bash install.sh --system", file=sys.stderr)
        sys.exit(1)
    new = args[args.index("--version") + 1] if "--version" in args else read("available", "0.1.66")
    open(os.path.join(home, "version"), "w").write(new)
    print("install.sh: installed sandbox-ai " + new + " (was " + version + ")", file=sys.stderr)
elif args == ["doctor", "--json"]:
    print(json.dumps([{{"status": "ok", "check": "Podman", "detail": "rootless", "hint": ""}},
                      {{"status": "warn", "check": "Login claude", "detail": "no login", "hint": "sandbox-ai login"}}]))
else:
    print(json.dumps(args))
""")
    (home / ".local/bin/sandbox-ai").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")  # one stand-in ssh call per command (see the test below)
    return {"log": log, "home": home, "tmp": tmp_path}


def calls(remote):
    return [json.loads(line) for line in remote["log"].read_text().splitlines()]


def test_add_list_remove(remote):
    lines = []
    assert h.cli(["add", "ct1", "user@ct1.lan", "--port", "2222", "--description", "Proxmox 101"], lines.append) == 0
    assert h.cli(["list", "--json"], lines.append) == 0
    found = json.loads(lines[-1])
    assert [e["name"] for e in found] == ["local", "ct1"]
    status = found[1].pop("sandbox_ai")
    assert found[1] == {"name": "ct1", "kind": "ssh", "ssh": "user@ct1.lan", "port": 2222, "description": "Proxmox 101"}
    assert (status["version"], status["kind"], status["available"], status["update"]) == ("0.1.30", "user", "0.1.66",
                                                                                         True)
    assert found[0]["sandbox_ai"]["version"] == h.module_version()
    assert oct(h.config_file().stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError, match="exists"):
        h.cli(["add", "ct1", "user@other"], lines.append)
    assert h.cli(["remove", "ct1"], lines.append) == 0 and h.load() == {}
    # --command (sandbox-ai's path there) is the host's, not the subcommand's
    assert h.cli(["add", "ct2", "root@ct2", "--command", "/opt/sai/sandbox-ai"], lines.append) == 0
    assert h.load()["ct2"]["command"] == "/opt/sai/sandbox-ai"


@pytest.mark.parametrize("name, target, extra, why", [
    ("local", "u@h", {}, "name"), ("Bad", "u@h", {}, "name"), ("x" * 33, "u@h", {}, "name"),
    ("ok", "-oProxyCommand=touch /tmp/pwned", {}, "ssh is"), ("ok", "u@h;id", {}, "ssh is"),
    ("ok", "u@h h", {}, "ssh is"), ("ok", "u@h", {"port": 0}, "port"), ("ok", "u@h", {"port": True}, "port"),
    ("ok", "u@h", {"identity": "relative/key"}, "identity"), ("ok", "u@h", {"command": "x; rm -rf ~"}, "command"),
    # Security review round 3: a command ssh's remote shell would take for an option; an identity outside HOME.
    ("ok", "u@h", {"command": "-oProxyCommand=x"}, "command"), ("ok", "u@h", {"command": "--help"}, "command"),
    ("ok", "u@h", {"identity": "/etc/shadow"}, "outside your HOME"),
    ("ok", "u@h", {"identity": "~/../../etc/passwd"}, "outside your HOME"),
    ("ok", "u@h", {"identity": "~"}, "outside your HOME"), ("ok", "u@h", {"identity": ""}, "identity"),
    ("ok", "u@h", {"extra": 1}, "allowed keys"),
])
def test_what_a_host_may_not_be(name, target, extra, why):
    with pytest.raises(ValueError, match=why):
        h.check(name, {"ssh": target, **extra})


def test_arguments_arrive_as_they_were_given(remote):
    """Each argument quoted on its own: spaces, quotes and $(...) reach the remote sandbox-ai unchanged, and no
    shell on the way runs anything."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    marker = remote["tmp"] / "pwned"
    args = ["list", "--json", "a b", "it's", f"$(touch {marker})", "`id`", "--", "-x"]
    status, out, err = h.run("ct1", args)
    assert status == 0, err
    assert json.loads(out) == args and not marker.exists()


def test_the_remote_sandbox_ai_is_found_without_bashrc(remote):
    """The remote PATH has no ~/.local/bin (Debian's ~/.bashrc returns early without a terminal): the command
    falls back to ~/.local/bin/sandbox-ai; a host's own command wins."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    assert h.run("ct1", ["--version"])[1].strip() == "sandbox-ai 0.1.30"
    other = remote["home"] / "opt-sandbox-ai"
    other.write_text("#!/bin/sh\necho custom \"$@\"\n")
    other.chmod(0o755)
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1", "command": str(other)})})
    assert h.run("ct1", ["--version"])[1].strip() == "custom --version"


def test_ssh_never_prompts_and_learns_the_host_key_once(remote):
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1", "port": 2222, "identity": "~/.ssh/id_ct"})})
    h.run("ct1", ["list"])
    (argv,) = calls(remote)
    options = argv[:argv.index("--")]
    assert argv[0] == "-T" and "BatchMode=yes" in options and "StrictHostKeyChecking=accept-new" in options
    assert any(o.startswith("UserKnownHostsFile=") and str(h.known_hosts_file()) in o for o in options)
    assert options[options.index("-p") + 1] == "2222" and "IdentitiesOnly=yes" in options
    assert options[options.index("-i") + 1] == os.path.expanduser("~/.ssh/id_ct")
    assert argv[argv.index("--") + 1] == "user@ct1", "the target after --: never taken for an option"
    assert h.known_hosts_file().parent.is_dir()
    tty = h.argv("ct1", ["attach", "web"], tty=True)
    assert tty[1] == "-tt", "an attach gets a terminal"


def test_test_says_what_is_wrong(remote):
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"}), "gone": h.check("gone", {"ssh": "unreachable"})})
    result = h.test("ct1")
    assert result["ssh"] and result["version"] == "sandbox-ai 0.1.30" and result["error"] is None
    assert [c["check"] for c in result["doctor"]] == ["Podman", "Login claude"]
    failed = h.test("gone")
    assert not failed["ssh"] and "Connection refused" in failed["error"] and "authorized_keys" in failed["error"]
    lines = []
    assert h.cli(["test", "ct1"], lines.append) == 0
    assert any("WARN" in line and "Login claude" in line for line in lines), "only what is not ok"


def test_local_is_this_machine(remote):
    command = h.argv("local", ["list", "--json"])
    assert command[:2] == [sys.executable, "-I"] and command[2].endswith("sandbox/main.py")
    with pytest.raises(ValueError, match="no host"):
        h.argv("nope", ["list"])
    with pytest.raises(ValueError, match="NUL"):
        h.argv("local", ["a\0b"])


def test_a_damaged_file_is_an_error_not_half_read(remote):
    h.config_file().parent.mkdir(parents=True, exist_ok=True)
    h.config_file().write_text('{"version": 1, "hosts": {"ct1": {"ssh": "-oProxyCommand=x"}}}')
    with pytest.raises(ValueError, match="ssh is"):
        h.load()
    h.config_file().write_text("not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        h.entries()


def test_the_command_line(remote):
    main = ROOT / "sandbox" / "main.py"
    env = {**os.environ, "SANDBOX_AI_NO_DEPRECATION": "1"}
    done = subprocess.run([sys.executable, "-I", str(main), "hosts", "list"], capture_output=True, text=True, env=env)
    assert done.returncode == 0 and done.stdout.startswith("local"), done.stderr
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    done = subprocess.run([sys.executable, "-I", str(main), "hosts", "run", "ct1", "--", "list", "--json"],
                          capture_output=True, text=True, env=env)
    assert done.returncode == 0 and json.loads(done.stdout) == ["list", "--json"], done.stderr


def test_a_known_hosts_path_with_a_space_stays_one_file(tmp_path, monkeypatch):
    """ssh splits UserKnownHostsFile on whitespace: sandbox-ai's own file under a HOME with a space must stay one
    path (else accept-new records the key nowhere and accepts any key at every connection)."""
    import shutil
    import subprocess
    if not shutil.which("ssh"):
        pytest.skip("no ssh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "my config"))
    argv = h.ssh_base({"ssh": "admin@192.0.2.10", "port": 22})
    shown = subprocess.run([*argv[:-2], "-G", "admin@192.0.2.10"], capture_output=True, text=True, timeout=30).stdout
    line = next(l for l in shown.splitlines() if l.startswith("userknownhostsfile "))
    assert str(tmp_path / "my config") in line and line.count(str(tmp_path / "my config")) == 1
    assert "known_hosts" in line.split(str(tmp_path / "my config"))[1].split()[0]


# --- key files others can read: made private, then the connection is tried again --------------------------

def key(path, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = "PRIVATE" + " KEY-----"  # in two pieces: no key-looking line in the source (privacy check)
    path.write_text(f"-----BEGIN OPENSSH {marker}\nnot a real key\n-----END OPENSSH {marker}\n")
    path.chmod(mode)
    return path


def test_test_makes_an_unprotected_key_private_and_tries_again(remote):
    identity = key(remote["tmp"] / "local-home/.ssh/id_ct1")
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1", "identity": "~/.ssh/id_ct1"})})
    result = h.test("ct1")
    assert result["ssh"] and result["version"] == "sandbox-ai 0.1.30", result
    assert result["fixed"] == [{"path": str(identity), "was": "0644", "now": "0600"}]
    assert identity.stat().st_mode & 0o777 == 0o600
    # Made private before the first try: ssh never had to refuse it.
    assert len([c for c in calls(remote) if c[-1].endswith("--version; fi")]) == 1
    lines = []
    identity.chmod(0o640)
    assert h.cli(["test", "ct1"], lines.append) == 0
    assert any("key:" in line and "id_ct1" in line and "0640" in line and "0600" in line for line in lines), lines


def test_a_key_ssh_names_as_too_open_is_made_private_and_tried_again(remote, monkeypatch):
    """A key ssh_config names (not the host's identity, not a default one): ssh says it is too open; it is made
    private, and the connection is tried once more."""
    other = key(remote["tmp"] / "local-home/keys/config_key")
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1", "identity": str(other)})})
    real = h.key_candidates
    monkeypatch.setattr(h, "key_candidates", lambda entry: [])  # as if only ssh_config knew it
    status, out, err, fixed, warnings = h.run_fixing("ct1", ["--version"])
    assert status == 0 and out.strip() == "sandbox-ai 0.1.30", err
    assert fixed == [{"path": str(other), "was": "0644", "now": "0600"}]
    assert len(calls(remote)) == 2, "refused once, then tried again"
    monkeypatch.setattr(h, "key_candidates", real)
    # What ssh's output names is changed only if it is a private key: never any file a remote side printed.
    victim = remote["tmp"] / "notes.txt"
    victim.write_text("my notes\n")
    victim.chmod(0o644)
    fixed, _ = h.protect_keys({"ssh": "x"}, f"Permissions 0644 for '{victim}' are too open.")
    assert fixed == [] and victim.stat().st_mode & 0o777 == 0o644


def test_what_is_never_changed(remote, tmp_path):
    """A symbolic link (only named), a file of another user, a folder, a private key: left as they are."""
    target = key(tmp_path / "real_key")
    link = tmp_path / "local-home/.ssh/id_link"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    done, warning = h.protect_key(link)
    assert done is None and "symbolic link" in warning and target.stat().st_mode & 0o777 == 0o644
    private = key(tmp_path / "private", 0o600)
    assert h.protect_key(private) == (None, None)
    assert h.protect_key(tmp_path / "missing") == (None, None)
    folder = tmp_path / "folder"
    folder.mkdir(mode=0o755)
    assert h.protect_key(folder) == (None, None) and folder.stat().st_mode & 0o777 == 0o755
    foreign = key(tmp_path / "foreign")
    try:  # a file of another user (only root can make one, and not in every user namespace)
        os.chown(foreign, 12345, 12345)
    except PermissionError:
        pass
    else:
        done, warning = h.protect_key(foreign)
        assert done is None and "another user" in warning and foreign.stat().st_mode & 0o777 == 0o644


def test_a_key_folder_others_can_write_is_named_not_changed(remote):
    ssh_dir = remote["tmp"] / "local-home/.ssh"
    key(ssh_dir / "id_ed25519", 0o600)
    ssh_dir.chmod(0o777)
    fixed, warnings = h.protect_keys({"ssh": "user@ct1"})
    assert fixed == [] and any("written by others" in w and "chmod 700" in w for w in warnings)
    assert ssh_dir.stat().st_mode & 0o777 == 0o777
    ssh_dir.chmod(0o700)


def test_add_makes_the_key_private_and_tries_the_connection(remote):
    identity = key(remote["tmp"] / "local-home/.ssh/id_new", 0o664)
    lines = []
    assert h.cli(["add", "ct1", "user@ct1", "--identity", str(identity)], lines.append) == 0
    assert identity.stat().st_mode & 0o777 == 0o600
    assert any("key:" in line and "0664" in line for line in lines), lines
    assert any("connection: ok, sandbox-ai 0.1.30" in line for line in lines), lines
    lines = []
    assert h.cli(["add", "ct2", "user@ct2", "--identity", str(identity)], lines.append) == 0
    assert any("hosts test ct2" in line for line in lines) and not any("connection" in line for line in lines), \
        "nothing to fix: no connection is tried at add"


# --- the hosts' sandbox-ai: version, update check, update ------------------------------------------------

def setup(remote, kind="user", version="0.1.30", sudo="nopasswd", **extra):
    for name, value in (("kind", kind), ("version", version), ("sudo", sudo)):
        (remote["home"] / name).write_text(value)
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1", **extra})})


def updates(remote):
    path = remote["home"] / "update.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_installation_kinds():
    assert h.installation("sandbox-ai 0.1.66\ninstalled in: /home/u/.local/share/sandbox-ai/versions/0.1.66\n") == \
        {"version": "0.1.66", "path": "/home/u/.local/share/sandbox-ai/versions/0.1.66", "kind": "user", "prefix": None}
    system = h.installation("sandbox-ai 0.1.66\ninstalled in: /usr/local/lib/sandbox-ai/versions/0.1.66\n")
    assert system["kind"] == "system" and system["prefix"] == "/usr/local"
    assert h.installation("sandbox-ai 0.1.66\ninstalled in: /srv/sandbox-ai\n")["kind"] == "checkout"
    assert h.installation("sandbox-ai 0.1.66\ninstalled in: /x\npackage: pyz\n")["kind"] == "package"
    assert h.installation("sandbox-ai 0.1.66\n") == {"version": "0.1.66", "path": None, "kind": "unknown",
                                                     "prefix": None}
    assert h.update_args(system, "0.1.70") == ["update", "--version", "0.1.70", "--system", "--prefix", "/usr/local"]
    with pytest.raises(ValueError, match="X.Y.Z"):
        h.update_args(system, "1.2; rm -rf ~")


def test_update_a_user_installation(remote):
    setup(remote)
    result = h.update("ct1")
    assert (result["before"], result["after"], result["changed"], result["error"]) == ("0.1.30", "0.1.66", True, None)
    assert updates(remote) == [{"args": ["update", "--user"], "root": False}]
    lines = []
    assert h.cli(["update", "ct1", "--version", "0.1.67"], lines.append) == 0
    assert "ct1: 0.1.66 -> 0.1.67" in lines and lines[-1] == "1 host: 1 updated, 0 up to date, 0 not updated"
    assert updates(remote)[-1]["args"] == ["update", "--version", "0.1.67", "--user"]
    lines = []
    assert h.cli(["update", "ct1", "--version", "0.1.67"], lines.append) == 0
    assert "ct1: 0.1.67, up to date" in lines
    # sudo is never used for a user's own installation (as root it would become root's)
    result = h.update("ct1", sudo=True)
    assert not result["sudo"] and any("user's own" in note for note in result["notes"])
    assert not (remote["home"] / "sudo.log").exists()


def test_a_system_installation_needs_sudo(remote):
    setup(remote, kind="system")
    result = h.update("ct1")
    assert result["needs_sudo"] and "--sudo" in result["error"] and "/opt/sai/lib/sandbox-ai" in result["error"]
    assert result["after"] is None
    result = h.update("ct1", sudo=True)
    assert result["changed"] and result["sudo"] and result["after"] == "0.1.66", result
    assert updates(remote)[-1] == {"args": ["update", "--system", "--prefix", "/opt/sai"], "root": True}
    assert (remote["home"] / "sudo.log").read_text().startswith("-n "), "sudo -n: never a password prompt"
    # the host's own setting
    (remote["home"] / "version").write_text("0.1.30")
    assert h.cli(["set", "ct1", "--update-sudo", "on"], print) == 0 and h.load()["ct1"]["update_sudo"] is True
    assert h.update("ct1")["sudo"] is True
    assert h.update("ct1", sudo=False)["needs_sudo"]


def test_sudo_that_wants_a_password_is_named_and_asked_in_a_terminal(remote, monkeypatch):
    setup(remote, kind="system", sudo="password", update_sudo=True)
    result = h.update("ct1")
    assert result["needs_password"] and "wants a password" in result["error"] and not result["changed"]
    terminal = h.update_terminal("ct1")
    assert terminal[1] == "-tt" and 'exec sudo -- "$(command -v sandbox-ai)" update --system --prefix /opt/sai' \
        in terminal[-1]
    # From a terminal, hosts update asks there (the stand-in runs it; a real sudo would prompt).
    monkeypatch.setattr(h.sys.stdin, "isatty", lambda: True)
    lines = []
    assert h.cli(["update", "ct1"], lines.append) == 0
    assert any("asking in this terminal" in line for line in lines) and "ct1: 0.1.30 -> 0.1.66 (sudo)" in lines, lines
    setup(remote, kind="system", sudo="refused", update_sudo=True)
    assert "may not use sudo" in h.update("ct1")["error"]


def test_what_is_not_updated_over_ssh(remote):
    for kind, words in (("checkout", "git pull"), ("pip", "pip install -U sandbox-ai")):
        setup(remote, kind=kind)
        result = h.update("ct1")
        assert words in result["error"] and "install.sh" in result["error"] and not result["changed"]
    assert updates(remote) == []
    setup(remote, kind="old")  # a version before version --verbose: its own update, as it would run it
    assert h.update("ct1")["after"] == "0.1.66" and updates(remote)[-1]["args"] == ["update"]


def test_update_all_and_the_summary(remote):
    setup(remote)
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"}), "gone": h.check("gone", {"ssh": "unreachable"})})
    lines = []
    assert h.cli(["update", "--all", "--jobs", "2"], lines.append) == 1
    assert "ct1: 0.1.30 -> 0.1.66" in lines
    assert any(line.startswith("gone: not updated:") and "Connection refused" in line for line in lines)
    assert lines[-1] == "2 hosts: 1 updated, 0 up to date, 1 not updated"
    lines = []
    assert h.cli(["update", "--all", "--json"], lines.append) == 1
    results = {r["name"]: r for r in json.loads(lines[-1])}
    assert results["ct1"]["after"] == "0.1.66" and not results["ct1"]["changed"] and results["gone"]["error"]
    with pytest.raises(ValueError, match="--all"):
        h.cli(["update"], lines.append)
    with pytest.raises(ValueError, match="no host"):
        h.cli(["update", "nope"], lines.append)


def test_list_shows_versions_and_keeps_them_a_while(remote):
    setup(remote)
    lines = []
    assert h.cli(["list"], lines.append) == 0
    assert any(line.startswith("ct1") and "0.1.30, update: 0.1.66" in line for line in lines), lines
    asked = len(calls(remote))
    assert h.cli(["list"], lines.append) == 0 and len(calls(remote)) == asked, "kept: no SSH within 10 minutes"
    assert h.cli(["list", "--refresh"], lines.append) == 0 and len(calls(remote)) > asked
    h.update("ct1")
    lines = []
    assert h.cli(["list", "--cached"], lines.append) == 0
    assert any(line.startswith("ct1") and line.split()[3] == "?" for line in lines), "an update forgets what was kept"
    assert h.cli(["list"], lines.append) == 0
    assert any(line.startswith("ct1") and "0.1.66, up to date" in line for line in lines), lines


def test_sandbox_ai_update_keeps_the_kind_of_installation():
    """install.sh decides by who runs it (root: a system installation, anyone else: their own): the launcher's
    update says which installation it is, so a system one is never doubled by a user's, nor a user's by root's."""
    spec = importlib.util.spec_from_file_location("main_for_hosts_tests", ROOT / "sandbox" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    system = "/usr/local/lib/sandbox-ai/versions/0.1.66/sandbox"
    user = "/home/u/.local/share/sandbox-ai/versions/0.1.66/sandbox"
    assert main.installation_mode([], system, euid=0) == ["--system", "--prefix", "/usr/local"]
    with pytest.raises(ValueError, match="sudo sandbox-ai update"):
        main.installation_mode([], system, euid=1000, writable=lambda path: False)
    assert main.installation_mode(["--check"], system, euid=1000) == ["--check"]
    assert main.installation_mode(["--user"], system, euid=1000) == ["--user"], "what was given wins"
    assert main.installation_mode([], user, euid=0) == ["--user"]
    assert main.installation_mode(["--version", "0.1.67"], user, euid=1000) == ["--version", "0.1.67"]
    assert main.installation_mode([], "/home/u/lib/sandbox-ai/versions/1/sandbox", euid=1000) == []
    assert main.installation_mode([], "/srv/checkout/sandbox", euid=0) == []


def test_commands_share_one_connection_per_host(tmp_path, monkeypatch):
    """A master started detached from every pipe (-f -N, stdio /dev/null), in a private XDG_RUNTIME_DIR folder;
    commands use it without ever becoming one; a terminal (attach) never goes through it; off with
    SANDBOX_AI_SSH_MULTIPLEX=0 or in a folder others can read."""
    entry = {"ssh": "user@ct1", "port": 22}
    runtime = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))  # short: ssh adds a suffix to a socket path of <108
    monkeypatch.setattr(h, "_failed", {})
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("SANDBOX_AI_SSH_MULTIPLEX", raising=False)
    path = h.control_path(entry)
    assert path.parent == runtime / "sandbox-ai" / "ssh" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert h.control_path({**entry, "port": 2222}) != path, "one connection per destination"
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0)
    assert h.ensure_master(entry, path, runner=runner)
    (start, options), = seen  # no socket yet: no check, a master
    assert "ControlMaster=yes" in start and f"ControlPersist={h.CONTROL_PERSIST}" in start
    assert "-f" in start and "-N" in start and start[-2:] == ["--", "user@ct1"]
    assert "ControlMaster=no" not in start and start.count("-o") * 2 == len([a for a in start if a == "-o"]) * 2
    assert all(options[k] is subprocess.DEVNULL for k in ("stdin", "stdout", "stderr")), "detached from every pipe"
    base = h.ssh_base(entry, control=path)
    assert base[base.index(f"ControlPath={path}") + 2] == "ControlMaster=no"
    monkeypatch.setattr(h, "load", lambda path=None: {"ct1": entry})
    assert f"ControlPath={path}" not in h.argv("ct1", ["attach", "x"], tty=True, control=path)
    assert f"ControlPath={path}" in h.argv("ct1", ["list"], control=path)
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")
    assert h.control_path(entry) is None
    monkeypatch.delenv("SANDBOX_AI_SSH_MULTIPLEX")
    (runtime / "sandbox-ai" / "ssh").chmod(0o755)
    assert h.control_path(entry) is None, "a folder others can read is not used"
    shutil.rmtree(runtime)


def test_control_folder_without_xdg_runtime_dir(monkeypatch):
    """Without XDG_RUNTIME_DIR (macOS): ~/Library/Caches/sandbox-ai/ssh on macOS, else $TMPDIR/sandbox-ai-UID/ssh,
    each folder 0700 and the user's; a folder others can read or a symbolic link is not used; a socket path that
    would not fit (with ssh's temporary suffix) is no sharing."""
    entry = {"ssh": "user@ct1", "port": 22}
    scratch = Path(tempfile.mkdtemp(prefix="c", dir="/tmp"))
    try:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("SANDBOX_AI_SSH_MULTIPLEX", raising=False)
        monkeypatch.setenv("HOME", str(scratch / "home"))
        monkeypatch.setenv("TMPDIR", str(scratch / "tmp"))
        (scratch / "home").mkdir()
        (scratch / "tmp").mkdir()
        mac = h.control_path(entry, platform="darwin")
        assert mac.parent == scratch / "home/Library/Caches/sandbox-ai/ssh" and len(mac.name) == 16
        other = h.control_path(entry, platform="linux")
        top = scratch / "tmp" / f"sandbox-ai-{os.getuid()}"
        assert other.parent == top / "ssh"
        for folder in (top, top / "ssh", mac.parent.parent, mac.parent):
            info = folder.lstat()
            assert oct(info.st_mode & 0o777) == "0o700" and info.st_uid == os.getuid()
        assert len(os.fsencode(other)) + 17 < 104
        top.chmod(0o755)
        assert h.control_path(entry, platform="linux") is None, "a folder in a shared TMPDIR others can read"
        shutil.rmtree(top)
        (scratch / "elsewhere").mkdir(mode=0o700)
        top.symlink_to(scratch / "elsewhere")
        assert h.control_path(entry, platform="linux") is None, "never through a symbolic link"
        top.unlink()
        deep = scratch / ("t" * 70)
        deep.mkdir()
        monkeypatch.setenv("TMPDIR", str(deep))
        assert h.control_path(entry, platform="linux") is None, "too long for a socket: no sharing"
        assert not (deep / f"sandbox-ai-{os.getuid()}").exists()
    finally:
        shutil.rmtree(scratch)


def test_one_master_start_at_a_time_across_processes(monkeypatch):
    """A lock file (0600) beside the socket: while another process holds it, a command never waits and never
    starts a second master; a failed start is remembered (in this process and, in the lock file, for the
    others) and not tried again for MASTER_RETRY seconds; a socket that does not answer is removed under the
    lock only."""
    import fcntl
    import socket
    runtime = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("SANDBOX_AI_SSH_MULTIPLEX", raising=False)
        monkeypatch.setattr(h, "_failed", {})
        entry = {"ssh": "user@ct1", "port": 22}
        path = h.control_path(entry)
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 255 if "-O" in argv else answer[0])
        answer = [255]
        holder = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)  # another process starting the master
        assert h.ensure_master(entry, path, runner=runner) is False and seen == [], "no wait, no second master"
        os.close(holder)
        assert oct(os.stat(f"{path}.lock").st_mode & 0o777) == "0o600"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))  # a socket left by a master that ended
        listener.close()
        assert h.ensure_master(entry, path, runner=runner) is False
        assert [a[a.index("-O") + 1] if "-O" in a else "start" for a in seen] == ["check", "start"]
        assert not path.exists(), "the socket nobody answers on was removed (under the lock)"
        assert h.ensure_master(entry, path, runner=runner) is False and len(seen) == 2, "the failure is remembered"
        monkeypatch.setattr(h, "_failed", {})  # another process: the lock file has the time
        assert h.ensure_master(entry, path, runner=runner) is False and len(seen) == 2
        real = h.time.time
        monkeypatch.setattr(h.time, "time", lambda: real() + h.MASTER_RETRY + 1)
        answer[0] = 0
        assert h.ensure_master(entry, path, runner=runner) is True and len(seen) == 3, "tried again after a minute"
        assert Path(f"{path}.lock").read_text() == "", "a start that worked clears the failure"
        assert h.ensure_master(entry, path, runner=runner, deadline=h.time.monotonic() + 0.5) is False
        assert len(seen) == 3, "no time left in the command's budget: no master start"
    finally:
        shutil.rmtree(runtime)


@pytest.fixture
def mux(tmp_path, monkeypatch):
    """A stand-in ssh that plays a master as well: -O check answers while its socket (a file here) exists, -O exit
    removes it, ControlMaster=yes makes it (or, ~/mode "slow", hangs); a command through it fails as ssh (mode
    "dead") or never answers (mode "hang"); anything else runs and says so."""
    runtime = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SANDBOX_AI_SSH_MULTIPLEX", raising=False)
    monkeypatch.setattr(h, "_failed", {})
    bin_dir, log, mode = tmp_path / "bin", tmp_path / "ssh.log", tmp_path / "mode"
    bin_dir.mkdir()
    mode.write_text("ok")
    (bin_dir / "ssh").write_text(f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
mode = open({str(mode)!r}).read()
path = next((a.split("=", 1)[1] for a in args if a.startswith("ControlPath=")), None)
kind = ("-O " + args[args.index("-O") + 1] if "-O" in args else "master" if "ControlMaster=yes" in args
        else "shared" if path and os.path.exists(path) else "own")
open({str(log)!r}, "a").write(kind + "\\n")
if kind == "-O check":
    sys.exit(0 if os.path.exists(path) else 255)
if kind in ("-O exit", "-O stop"):
    os.unlink(path)
    sys.exit(0)
if kind == "master":
    if mode == "slow":
        time.sleep(30)
    open(path, "w").close()
    sys.exit(0)
if kind == "shared" and mode == "dead":
    print("mux_client_request_session: read from master failed: Broken pipe", file=sys.stderr)
    sys.exit(255)
if kind == "shared" and mode == "lost":
    print("Connection to ct1 closed by remote host.", file=sys.stderr)
    sys.exit(255)
if kind == "shared" and mode == "hang":
    time.sleep(30)
print("ran " + ("list" if " list;" in args[-1] else args[-1]))
""")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    h.save({"ct1": {"ssh": "user@ct1", "port": 22, "description": ""}})
    yield {"log": log, "mode": mode}
    shutil.rmtree(runtime)


def test_master_start_is_spent_out_of_the_timeout(mux):
    """A host that drops packets: the master start and the command together take the command's timeout (not
    timeout + ConnectTimeout); the next command does not try a master again, it connects on its own."""
    mux["mode"].write_text("slow")
    began = time.monotonic()
    status, _, err = h.run("ct1", ["list"], timeout=2)
    assert status == 124 and "within 2 s" in err and time.monotonic() - began < 3.5
    mux["mode"].write_text("ok")
    began = time.monotonic()
    assert h.run("ct1", ["list"], timeout=10)[:2] == (0, "ran list\n") and time.monotonic() - began < 5
    assert mux["log"].read_text().split() == ["master", "own"]


def test_dead_master_is_left_for_a_connection_of_its_own(mux):
    """A master whose TCP connection died answers -O check: it is asked to stop taking sessions (-O stop: what runs
    through it goes on), and the command runs once more on a connection of its own only when it never reached the
    host (the master refused the session); a command that may have run (lost on the way, no answer) never twice."""
    assert h.run("ct1", ["list"], timeout=10)[:2] == (0, "ran list\n")
    assert mux["log"].read_text().splitlines() == ["master", "shared"]
    mux["mode"].write_text("dead")
    assert h.run("ct1", ["list"], timeout=10)[:2] == (0, "ran list\n")
    assert mux["log"].read_text().splitlines()[2:] == ["-O check", "shared", "-O stop", "own"]
    assert "-O exit" not in mux["log"].read_text(), "never ends the other sessions on that host"
    for mode, status in (("lost", 255), ("hang", 124)):
        mux["mode"].write_text("ok")
        assert h.run("ct1", ["list"], timeout=10)[0] == 0  # a new master
        mux["mode"].write_text(mode)
        mux["log"].write_text("")
        began = time.monotonic()
        assert h.run("ct1", ["list"], timeout=2)[0] == status and time.monotonic() - began < 8
        assert mux["log"].read_text().splitlines()[-2:] == ["shared", "-O stop"], "not run a second time"


def test_remove_and_changed_settings_end_the_shared_connection(mux, monkeypatch):
    """hosts remove asks the host's master to exit at once (else it serves commands for up to CONTROL_PERSIST s);
    so does hosts set when it changes what the connection was made for."""
    assert h.run("ct1", ["list"], timeout=10)[0] == 0
    assert h.cli(["set", "ct1", "--update-sudo", "on"], lambda line: None) == 0
    assert "-O exit" not in mux["log"].read_text(), "update_sudo is no connection setting"
    assert h.cli(["remove", "ct1"], lambda line: None) == 0
    assert mux["log"].read_text().splitlines()[-1] == "-O exit"
    assert not h.control_path({"ssh": "user@ct1", "port": 22}).exists()


# --- security review round 3: the identity and hosts.json's writers ---------------------------------------

def test_identity_is_a_key_file_in_home(remote, tmp_path):
    """The identity is made private (0600) by add and test: it must be a regular file in HOME (also with its links
    resolved), and only one that holds a private key is changed."""
    home = tmp_path / "local-home"
    good = key(home / ".ssh/id_ok")
    assert h.check("ct1", {"ssh": "u@h", "identity": "~/.ssh/id_ok"})["identity"] == "~/.ssh/id_ok"
    assert h.check("ct1", {"ssh": "u@h", "identity": str(home / ".ssh/not-yet")})  # missing: ssh says so
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (home / "linked").symlink_to(outside)
    (home / ".ssh/id_link").symlink_to(good)
    (home / ".ssh/folder").mkdir()
    for identity, why in [(str(home / "linked/key"), "outside your HOME"), ("~/.ssh/id_link", "symbolic link"),
                          ("~/.ssh/folder", "not a regular file")]:
        with pytest.raises(ValueError, match=why):
            h.check("ct1", {"ssh": "u@h", "identity": identity})
    # A file of yours in HOME that is no key (your notes): never made private through the identity.
    notes = home / "notes.txt"
    notes.write_text("my notes\n")
    notes.chmod(0o644)
    fixed, _ = h.protect_keys(h.check("ct1", {"ssh": "u@h", "identity": str(notes)}))
    assert fixed == [] and notes.stat().st_mode & 0o777 == 0o644
    fixed, _ = h.protect_keys(h.check("ct1", {"ssh": "u@h", "identity": str(good)}))
    assert [change["path"] for change in fixed] == [str(good)] and good.stat().st_mode & 0o777 == 0o600
    lines = []
    with pytest.raises(ValueError, match="outside your HOME"):
        h.cli(["add", "ct9", "u@h", "--identity", str(outside / "key")], lines.append)
    assert "ct9" not in h.load()


def test_hosts_file_lock(remote):
    """Every writer of hosts.json (add, set, remove; the web interface) holds hosts_file_lock: one at a time, a
    lock file beside it (0600), reentrant within a thread (save takes it too)."""
    import threading
    path = h.config_file()
    with h.hosts_file_lock() as held:
        assert held == path
        lock = path.with_name("hosts.json.lock")
        assert lock.is_file() and lock.stat().st_mode & 0o777 == 0o600
        with h.hosts_file_lock(path):  # reentrant: save() within a read-modify-write
            h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
        got = []

        def other():
            try:
                with h.hosts_file_lock(timeout=0.3):
                    got.append("locked")
            except ValueError as exc:
                got.append(str(exc))
        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        assert "being changed by another" in got[0], got
        # A writer that waits gets it once the first one is done.
        waiting = threading.Thread(target=lambda: h.cli(["remove", "ct1"], got.append))
        waiting.start()
        time.sleep(0.2)
        assert "ct1" in h.load(), "the remove waits for the lock"
    waiting.join(5)
    assert h.load() == {} and "host ct1 removed" in got
    # Another process: the same lock (flock), as the app's and the web interface's.
    script = (f"import importlib.util, sys; spec = importlib.util.spec_from_file_location('h', {str(ROOT / 'sandbox/hosts.py')!r}); "
              "h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)\n"
              "try:\n    with h.hosts_file_lock(timeout=0.3): print('locked')\nexcept ValueError: print('busy')")
    with h.hosts_file_lock():
        assert subprocess.run([sys.executable, "-c", script], capture_output=True, text=True).stdout.strip() == "busy"
    assert subprocess.run([sys.executable, "-c", script], capture_output=True, text=True).stdout.strip() == "locked"
    lock.chmod(0o644)
    with h.hosts_file_lock():
        assert lock.stat().st_mode & 0o777 == 0o600


def not_root(remote):
    """The SSH user there is no root (this test may run as root): id -u says 1000."""
    fake = remote["home"] / "rbin/id"
    fake.write_text('#!/bin/sh\nif [ "$1" = -u ]; then echo 1000; else /usr/bin/id "$@"; fi\n')
    fake.chmod(0o755)


def test_install_copies_this_sandbox_ai_and_installs_it_for_the_user(remote):
    """hosts install: this machine's tree goes to the host (no release server needed there), install.sh installs it
    for the SSH user in ~/.local, the temporary folder is gone afterwards, and the host's sandbox-ai answers."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    (remote["home"] / ".local/bin/sandbox-ai").unlink(missing_ok=True)  # nothing there yet
    not_root(remote)
    lines = []
    assert h.cli(["install", "ct1", "--no-sudo"], lines.append) == 0, lines
    assert "installed for the user" in lines[0], lines
    installed = list((remote["home"] / ".local/share/sandbox-ai").glob("versions/*/sandbox/agent_sandbox.py"))
    assert installed, "install.sh put it into ~/.local/share/sandbox-ai"
    sent = [json.loads(line) for line in remote["log"].read_text().splitlines()]
    assert any("tar -xzf - -C" in args[-1] for args in sent) and any("--no-app-deps" in args[-1] for args in sent)


def test_install_needs_root_for_a_system_installation(remote):
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    not_root(remote)
    result = h.install("ct1", mode="system", sudo=False)
    assert "needs root" in result["error"] and not result["sudo"]
    (remote["home"] / "sudo").write_text("password")
    result = h.install("ct1", mode="system")
    assert result["needs_password"] and "wants a password" in result["error"]
    assert not list(Path("/tmp").glob("sandbox-ai-install.*")), "the copy is removed when nothing is installed"


def test_install_bundle_and_arguments(tmp_path):
    import io
    import tarfile
    names = tarfile.open(fileobj=io.BytesIO(h.install_bundle("here")), mode="r:gz").getnames()
    assert "install.sh" in names and "sandbox/agent_sandbox.py" in names
    assert not any("__pycache__" in n or n == ".git" or n.startswith(".git/") or n.endswith(".pyc") for n in names)
    small = tarfile.open(fileobj=io.BytesIO(h.install_bundle("download")), mode="r:gz").getnames()
    assert set(small) <= {"install.sh", "sandbox/release-signers"}
    assert h.install_args("user") == '--from "$d" --user --no-app-deps'
    assert h.install_args("system", "/opt", "download", "0.1.78") == "--system --prefix /opt --no-app-deps --version 0.1.78"
    for bad in (("root",), ("system", "/opt/../etc"), ("system", "relative"), ("user", None, "here", "1.2")):
        with pytest.raises(ValueError):
            h.install_args(*bad)
    with pytest.raises(ValueError, match="no sandbox-ai tree"):
        h.install_bundle("here", tmp_path)


# --- review round 4 ------------------------------------------------------------------------------------------


def test_bundle_from_a_checkout_sends_what_install_sh_would_copy(tmp_path):
    """Finding M7: from a git checkout only tracked and new not-ignored files (git ls-files --cached --others
    --exclude-standard), deleted ones gone; never .claude/, logins, keys or tokens; without git the tree as is."""
    import io
    import tarfile
    root = tmp_path / "tree"
    (root / "sandbox").mkdir(parents=True)
    (root / "install.sh").write_text("#!/bin/sh\n")
    (root / "sandbox/agent_sandbox.py").write_text("")
    (root / "gone.txt").write_text("")
    (root / ".gitignore").write_text("*.log\n.sandbox-ai.json\n")
    git = ["git", "-C", str(root), "-c", "user.email=t@example.org", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    (root / "gone.txt").unlink()  # deleted, not committed
    (root / ".claude").mkdir()
    (root / ".claude/settings.local.json").write_text("{}")
    (root / "run.log").write_text("ignored")
    (root / ".sandbox-ai.json").write_text("{}")
    (root / "new.py").write_text("")  # new, not ignored: install.sh copies it too
    for name in (".credentials.json", "id_ed25519", "github-token", ".env", "server.pem", "auth.json"):
        (root / name).write_text("secret")
    names = h.bundle_files(root)
    assert names == [".gitignore", "install.sh", "new.py", "sandbox/agent_sandbox.py"], names
    archive = tarfile.open(fileobj=io.BytesIO(h.install_bundle("here", root)), mode="r:gz").getnames()
    assert sorted(archive) == names
    # Not a checkout (an installed version): the tree as it is, but still never a login or .claude/.
    import shutil
    shutil.rmtree(root / ".git")
    names = h.bundle_files(root)
    assert "run.log" in names and ".sandbox-ai.json" in names and "new.py" in names
    assert not any(n.startswith(".claude/") or n in (".credentials.json", "id_ed25519", "github-token", ".env",
                                                      "server.pem", "auth.json") for n in names)


def test_mux_refused_is_only_sshs_own_line_at_the_start():
    """Finding L4: a remote command's own "Control socket" error is no refusal of the shared connection (the
    command may have run: never run twice); only ssh's own mux lines first in stderr, with nothing on stdout."""
    assert h.mux_refused("", "mux_client_request_session: session request failed: Session open refused by peer\n")
    assert h.mux_refused("", "Warning: Permanently added 'ct1' (ED25519) to the list of known hosts.\n"
                             "Control socket connect(/run/user/1/sandbox-ai/ssh/x): Connection refused\n")
    assert h.mux_refused("", "muxclient: master hello exchange failed\n")
    for out, err in (("", "error: Control socket connect failed in my tool: Connection refused\n"),
                     ("", "make: *** mux_client_ broken\nControl socket connect(x): Connection refused\n"),
                     ("partial output", "mux_client_request_session: session request failed\n"),
                     ("", "the master is not running, says my program\n"), ("", "")):
        assert not h.mux_refused(out, err), (out, err)


def test_install_dry_run_says_what_would_be_done(remote):
    """Finding L5: the mode, root and sudo are decided by the host's id -u, and --dry-run reports them before
    anything is copied."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    not_root(remote)
    lines = []
    assert h.cli(["install", "ct1", "--system", "--dry-run", "--json"], lines.append) == 0
    result = json.loads(lines[0])
    assert result["planned"] and result["mode"] == "system" and result["sudo"] and not result["root"]
    assert result["prefix"] == "/usr/local" and "would install for every user in /usr/local with sudo" in \
        result["notes"][0]
    sent = [json.loads(line) for line in remote["log"].read_text().splitlines()]
    assert not any("tar -xzf" in args[-1] for args in sent), "nothing copied"
    assert all(args[-1].startswith("sh -c ") for args in sent), "run by sh, whatever the login shell"
    lines = []
    assert h.cli(["install", "ct1", "--dry-run"], lines.append) == 0 and "nothing done" in lines[0]


def test_install_refuses_a_prefix_of_another_user(remote, tmp_path):
    """Finding L5: a --system prefix that belongs to a user other than root or the SSH user is refused."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    not_root(remote)
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    fake = remote["home"] / "rbin/ls"
    fake.write_text('#!/bin/sh\ncase "$2" in */bin) echo "drwxr-xr-x 2 4321 4321 0 Jan 1 00:00 $2" ;; '
                    f'{prefix}) echo "drwxr-xr-x 2 1000 1000 0 Jan 1 00:00 $2" ;; '  # not the runner's own uid
                    '*) echo "drwxr-xr-x 2 0 0 0 Jan 1 00:00 $2" ;; esac\n')  # the folders above: root's
    fake.chmod(0o755)
    result = h.install("ct1", mode="system", prefix=str(prefix), dry_run=True)
    assert "belongs to the user 4321" in (result["error"] or "") and not result["planned"]
    fake.write_text('#!/bin/sh\necho "drwxr-xr-x 2 1000 1000 0 Jan 1 00:00 $2"\n')  # the SSH user's own: fine
    assert h.install("ct1", mode="system", prefix=str(prefix), dry_run=True)["planned"]
    # Review 5 L5: a folder above that another user owns, or one others may change, is refused as well.
    fake.write_text(f'#!/bin/sh\ncase "$2" in {prefix.parent}) echo "drwxr-xr-x 2 4321 4321 0 Jan 1 00:00 $2" ;; '
                    '*) echo "drwxr-xr-x 2 0 0 0 Jan 1 00:00 $2" ;; esac\n')
    assert f"{prefix.parent} belongs to the user 4321" in h.install("ct1", mode="system", prefix=str(prefix),
                                                                      dry_run=True)["error"]
    fake.write_text(f'#!/bin/sh\ncase "$2" in {prefix}/bin) echo "drwxrwxrwx 2 0 0 0 Jan 1 00:00 $2" ;; '
                    '*) echo "drwxr-xr-x 2 0 0 0 Jan 1 00:00 $2" ;; esac\n')
    assert "may be changed by others" in h.install("ct1", mode="system", prefix=str(prefix), dry_run=True)["error"]


def test_install_probes_the_prefixs_copy(remote, tmp_path):
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    not_root(remote)
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/sandbox-ai").write_text("#!/bin/sh\necho sandbox-ai 0.1.11\n")
    (prefix / "bin/sandbox-ai").chmod(0o755)
    result = h.install("ct1", mode="system", prefix=str(prefix), dry_run=True)
    assert result["before"] == "0.1.11", "the copy in the prefix, not the one on PATH (0.1.30)"


def test_install_removes_the_copy_also_when_sudo_fails(remote):
    """Finding L5: the host's temporary copy goes also when sudo fails (here: sudo may not be used)."""
    assert "sudo -n rm -rf" in h.cleanup_script("/tmp/sandbox-ai-install.x", True)
    assert "sudo" not in h.cleanup_script("/tmp/sandbox-ai-install.x", False)
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    not_root(remote)
    (remote["home"] / "sudo").write_text("refused")
    before = set(Path("/tmp").glob("sandbox-ai-install.*"))
    result = h.install("ct1", mode="system")
    assert result["error"] and "sudo" in result["error"]
    sent = [json.loads(line)[-1] for line in remote["log"].read_text().splitlines()]
    assert any(command.startswith("sh -c ") and "rm -rf --" in command and "case $d in" in command
               for command in sent), "cleaned up after"
    assert set(Path("/tmp").glob("sandbox-ai-install.*")) <= before


def test_update_from_here_installs_this_machines_copy_as_the_same_kind(remote):
    """hosts update --from-here: a host that cannot reach the release server (or before a release is published) gets
    this machine's copy, as the kind of installation it has (here: the user's own)."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    not_root(remote)
    (remote["home"] / "kind").write_text("user")
    result = h.update("ct1", from_here=True)
    assert not result["error"], result
    assert any("from this machine's copy" in note for note in result["notes"])
    assert result["from_here"] and result["mode"] == "user" and result["prefix"] is None
    assert list((remote["home"] / ".local/share/sandbox-ai").glob("versions/*/sandbox/agent_sandbox.py"))
    with pytest.raises(ValueError, match="--version is for the signed release"):
        h.update("ct1", version="0.1.77", from_here=True)


def test_a_host_whose_version_is_not_known_yet_is_asked_before_a_start(remote, tmp_path, monkeypatch):
    """Review 6 (L10, A-N3): a remote host just added (no status kept yet) got its starts without
    --workspaces-as-given; asked, one short `version --verbose` says its version, kept for the next start while
    hosts list still asks for the whole status."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    (remote["home"] / "version").write_text("0.1.78")
    assert h.known_version("ct1") == (None, None)
    assert not h.knows_workspaces_as_given("ct1")  # not asked: counts as older
    assert h.knows_workspaces_as_given("ct1", ask=True)
    sent = [json.loads(line)[-1] for line in remote["log"].read_text().splitlines()]
    assert sum("version --verbose" in command for command in sent) == 1 and not any("--check" in c for c in sent)
    assert h.known_version("ct1")[0] == "0.1.78" and h.knows_workspaces_as_given("ct1")
    assert h.statuses(["ct1"], cached=True)["ct1"]["checked"] == 0  # hosts list asks again for the whole status
    (remote["home"] / "version").write_text("0.1.77")
    h.forget_status("ct1")
    assert not h.knows_workspaces_as_given("ct1", ask=True)  # older: not given the option it does not know
    assert not h.knows_workspaces_as_given("nohost", ask=True)


def test_from_here_with_a_version_is_refused_before_any_output_or_ssh(remote, monkeypatch):
    """Review 6 N2: `hosts update NAME --from-here --version X` said "updating NAME to X ..." and refused only after
    the SSH round trip; now it is refused at once, as hosts install does."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    monkeypatch.setattr(h, "update_many", lambda *a, **k: pytest.fail("no update may start"))
    monkeypatch.setattr(h, "run_fixing", lambda *a, **k: pytest.fail("no SSH"))
    lines = []
    with pytest.raises(ValueError, match="--version is for the signed release"):
        h.cli(["update", "ct1", "--from-here", "--version", "0.1.70"], lines.append)
    with pytest.raises(ValueError, match="--from-here is for another host"):
        h.cli(["update", "local", "--from-here"], lines.append)
    assert lines == []


def test_an_update_with_no_release_points_to_from_here(remote, monkeypatch):
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    real = h.run

    def run(name, args, **kw):
        if args[:1] == ["update"]:
            return 1, "", "install.sh: https://github.com/c0decave/sandbox-ai has no published release"
        return real(name, args, **kw)
    monkeypatch.setattr(h, "run", run)
    (remote["home"] / "kind").write_text("user")
    result = h.update("ct1")
    assert "sandbox-ai hosts update ct1 --from-here" in result["error"]


def test_a_from_here_update_whose_sudo_wants_a_password_asks_in_the_terminal_from_here(remote, monkeypatch):
    """In a terminal, the second try of `hosts update --from-here` is this machine's copy again (sudo asking there),
    never the release update."""
    h.save({"ct1": h.check("ct1", {"ssh": "user@ct1"})})
    calls = []

    def update(name, version=None, sudo=None, path=None, runner=None, from_here=False, terminal=False):
        calls.append((name, sudo, from_here, terminal))
        if not terminal:
            return {"name": name, "before": "0.1.70", "after": None, "changed": False, "error": "sudo wants one",
                    "needs_password": True, "notes": []}
        return {"name": name, "before": "0.1.70", "after": "0.1.78", "changed": True, "error": None,
                "needs_password": False, "notes": []}
    monkeypatch.setattr(h, "update", update)
    monkeypatch.setattr(h, "update_terminal", lambda *a, **k: pytest.fail("the release update in a terminal"))
    monkeypatch.setattr(h.sys.stdin, "isatty", lambda: True)
    lines = []
    assert h.cli(["update", "ct1", "--from-here"], lines.append) == 0
    assert calls == [("ct1", None, True, False), ("ct1", True, True, True)]
    assert any("0.1.78" in line for line in lines), lines


def test_a_system_prefix_is_checked_from_the_root_down():
    """Review 5 L5: only the owners of PREFIX, PREFIX/bin and PREFIX/lib were checked; a group- or world-writable
    folder, one above PREFIX of another user, and lib/sandbox-ai itself went unseen."""
    paths = [path for path, _ in h.prefix_paths("/opt/sai")]
    assert paths == ["/", "/opt", "/opt/sai", "/opt/sai/bin", "/opt/sai/lib", "/opt/sai/lib/sandbox-ai"]
    good = {"uid": "1000", **{f"folder{i}": "drwxr-xr-x 0" for i in range(len(paths))}}
    assert h.prefix_problem("/opt/sai", good) is None
    assert h.prefix_problem("/opt/sai", {**good, "folder2": "drwxr-xr-x 1000"}) is None  # the SSH user's own
    assert "/opt belongs to the user 4321" in h.prefix_problem("/opt/sai", {**good, "folder1": "drwxr-xr-x 4321"})
    assert "its group" in h.prefix_problem("/opt/sai", {**good, "folder3": "drwxrwxr-x 0"})
    assert "others" in h.prefix_problem("/opt/sai", {**good, "folder5": "drwxr-xrwx 0"})
    assert "others" in h.prefix_problem("/opt/sai", {**good, "folder1": "drwxrwxrwx 0"})
    assert h.prefix_problem("/opt/sai", {**good, "folder1": "drwxrwxrwt 0"}) is None  # sticky above, as /tmp
    assert "others" in h.prefix_problem("/opt/sai", {**good, "folder2": "drwxrwxrwt 0"})  # never PREFIX itself
    script = h.probe_script("system", "/opt/sai")
    assert "ls -ndL /opt/sai/lib/sandbox-ai" in script and "folder0=" in script


def test_a_from_here_update_of_the_same_version_says_it_reinstalled():
    """Review 5 (correctness L11): `hosts update NAME --from-here` that replaced the files with this machine's copy
    of the same version said "up to date"."""
    base = {"name": "ct1", "before": "0.1.78", "after": "0.1.78", "changed": False, "error": None, "notes": [],
            "fixed": []}
    assert h.update_lines({**base, "from_here": True})[0] == "ct1: reinstalled from this machine's copy (0.1.78)"
    assert h.update_lines(base)[0] == "ct1: 0.1.78, up to date"
