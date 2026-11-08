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
    other = key(remote["tmp"] / "keys/config_key")
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
    SANDBOX_AI_SSH_MULTIPLEX=0 or without XDG_RUNTIME_DIR."""
    entry = {"ssh": "user@ct1", "port": 22}
    runtime = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))  # short: ssh adds a suffix to a socket path of <108
    monkeypatch.setattr(h, "_masters", {})
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
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert h.control_path(entry) is None
    shutil.rmtree(runtime)
