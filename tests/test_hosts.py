"""sandbox-ai hosts (sandbox/hosts.py): other machines (LXC containers) whose sandbox-ai is managed over SSH. A
stand-in ssh runs the remote command with sh, in a remote HOME whose PATH has no sandbox-ai (a non-interactive
login reads no ~/.bashrc): quoting and the fallback to ~/.local/bin are tested for real."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hosts_for_tests", ROOT / "sandbox" / "hosts.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A stand-in ssh on PATH and a remote HOME with sandbox-ai in ~/.local/bin only."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    bin_dir, home = tmp_path / "bin", tmp_path / "remote-home"
    (home / ".local/bin").mkdir(parents=True)
    bin_dir.mkdir()
    log = tmp_path / "ssh.json"
    (bin_dir / "ssh").write_text(f"""#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(args) + "\\n")
if "unreachable" in args:
    print("ssh: connect to host unreachable port 22: Connection refused", file=sys.stderr)
    sys.exit(255)
command = args[args.index("--") + 2]
env = {{"HOME": {str(home)!r}, "PATH": "/usr/bin:/bin"}}
sys.exit(subprocess.run(["sh", "-c", command], env=env).returncode)
""")
    (bin_dir / "ssh").chmod(0o755)
    (home / ".local/bin/sandbox-ai").write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("sandbox-ai 0.1.30")
elif args == ["doctor", "--json"]:
    print(json.dumps([{{"status": "ok", "check": "Podman", "detail": "rootless", "hint": ""}},
                      {{"status": "warn", "check": "Login claude", "detail": "no login", "hint": "sandbox-ai login"}}]))
else:
    print(json.dumps(args))
""")
    (home / ".local/bin/sandbox-ai").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return {"log": log, "home": home, "tmp": tmp_path}


def calls(remote):
    return [json.loads(line) for line in remote["log"].read_text().splitlines()]


def test_add_list_remove(remote):
    lines = []
    assert h.cli(["add", "ct1", "user@ct1.lan", "--port", "2222", "--description", "Proxmox 101"], lines.append) == 0
    assert h.cli(["list", "--json"], lines.append) == 0
    found = json.loads(lines[-1])
    assert [e["name"] for e in found] == ["local", "ct1"]
    assert found[1] == {"name": "ct1", "kind": "ssh", "ssh": "user@ct1.lan", "port": 2222, "description": "Proxmox 101"}
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
