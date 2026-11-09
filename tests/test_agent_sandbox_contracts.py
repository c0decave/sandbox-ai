"""Happy/sad/edge contracts for every launcher function.

Only OS/process boundaries are mocked. Filesystem copies, pipes, validation,
configuration serialization and security decisions use the implementation.
The separate live suite proves the actual namespace and model boundaries.
"""
import argparse
import ast
import base64
import contextlib
import fcntl
import hashlib
import io
import gzip
import importlib.util
import ipaddress
import json
from types import SimpleNamespace
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import stat
import struct
import subprocess
import sys
import ssl
import threading
import time
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10: the same parser as a package
    import tomli as tomllib
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_contracts", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)


class Both:
    """Patch target for names the launcher re-exports from runtime.py: sets them in both modules.

    __dict__ is the launcher's, so pytest's monkeypatch records and restores the real old value."""
    __slots__ = ("module",)

    def __init__(self, module):
        object.__setattr__(self, "module", module)

    @property
    def __dict__(self):
        return vars(self.module)

    def modules(self):
        return (self.module, self.module.RUNTIME)

    def __getattr__(self, name):
        return getattr(self.module, name)

    def __setattr__(self, name, value):
        for module in self.modules():
            setattr(module, name, value)

    def __delattr__(self, name):
        for module in self.modules():
            if name in vars(module):
                delattr(module, name)


sr = Both(s)
SPEC.loader.exec_module(s)


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                 "XDG_CONFIG_HOME", "TERM"):
        monkeypatch.delenv(name, raising=False)


def states():
    before = {"mnt": "mnt:[10]", "pid": "pid:[11]"}
    current = dict(mnt="mnt:[12]", pid="pid:[13]", CapEff="0", CapPrm="0",
                   CapBnd="0", CapInh="0", CapAmb="0", NoNewPrivs="1", Seccomp="2")
    return before, current


def test_parse_args(scenario):
    if scenario == "sad":
        with pytest.raises(SystemExit) as exc:
            s.parse_args(["codex", "--unknown-wrapper-option"])
        assert exc.value.code == 2
    else:
        tail = ["exec", "--", "--sandbox=literal-prompt"] if scenario == "edge" else ["exec", "task"]
        args = s.parse_args(["codex", "--", *tail])
        assert args.agent_args == tail
        assert args.backend == "podman"


def test_allowed_hosts(scenario):
    options = {"happy": [], "sad": ["--no-provider-defaults"],
               "edge": ["--allow-host", "opencode.ai", "--allow-host", "opencode.ai"]}[scenario]
    answer = s.allowed_hosts(s.parse_args(["opencode", *options]))
    if scenario == "sad":
        assert answer == []
    else:
        assert answer.count("opencode.ai") == 1
        assert answer == ["opencode.ai"]


def test_safe_dest(scenario):
    if scenario == "sad":
        for dest in ("/work/../etc", "/work//file", "/work/./file", "/etc", "relative"):
            with pytest.raises(ValueError):
                s.safe_dest(dest)
    else:
        value = "/mnt" if scenario == "edge" else "/work/nested/file"
        assert s.safe_dest(value) == value


def test_parse_mount(scenario, tmp_path):
    if scenario == "sad":
        for mount in ("missing-separators", f"{tmp_path}/missing:/mnt/data:rw", f"{tmp_path}:/mnt/data:bad"):
            with pytest.raises(ValueError):
                s.parse_mount(mount)
        # Security review round 3: a socket (/var/run/docker.sock), also through a symlink, gives what runs behind it.
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(tmp_path / "engine.sock"))
            (tmp_path / "link").symlink_to(tmp_path / "engine.sock")
            for source in ("engine.sock", "link"):
                for mode in ("ro", "rw"):
                    with pytest.raises(ValueError, match=re.escape(f"--mount {tmp_path}/engine.sock is a socket")):
                        s.parse_mount(f"{tmp_path / source}:/var/run/docker.sock:{mode}")
    else:
        path = tmp_path / ("colon: and spaces" if scenario == "edge" else "reference")
        path.write_text("unchanged")
        assert s.parse_mount(f"{path}:/mnt/reference:ro") == (str(path), "/mnt/reference", "ro")
        assert path.read_text() == "unchanged"


def test_validate(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        cases = [["--network", "research", "--ip", "2001:db8::1"],
                 ["--network", "research", "--ip6", "192.0.2.1"],
                 ["--runtime", "/usr/bin"], ["--env", "INVALID=VALUE"],
                 ["--env", "UNSET_CONTRACT_VARIABLE"], ["--allow-host", "a..example.org"],
                 ["--offline", "--dns"], ["--offline", "--egress-all-allow"], ["--offline", "--nameserver", "1.1.1.1"],
                 ["--nameserver", "dns.example"], ["--nameserver", "1.1.1.1; accept"], ["--nameserver", "10.0.0.0/8"],
                 ["--allow-host-port", "3000", "--offline"], ["--allow-host-port", "3000", "--backend", "bwrap"],
                 ["--allow-host-port", "3000", "--network", "research"], ["--allow-host-port", "3000,22"],
                 ["--git-token-env", "https://github.com=UNSET_CONTRACT_VARIABLE"],
                 ["--git-token-env", "https://github.com=PATH", "--offline"],
                 ["--git-credentials", "/nonexistent/git-credentials"],
                 ["--git-token-env", "http://localhost:3080=UNSET_CONTRACT_VARIABLE"],
                 ["--git-token-env", "http://localhost:3080=PATH", "--backend", "bwrap"],
                 ["--git-token-env", "https://git.example.org=PATH", "--offline"],
                 ["--device", "/dev/console"], ["--device", "/dev/null"],
                 ["--gpu", "dri", "--podman-url", "unix:///example/podman.sock"]]
        monkeypatch.delenv("UNSET_CONTRACT_VARIABLE", raising=False)
        for args in cases:
            with pytest.raises(ValueError):
                s.validate(s.parse_args(["codex", *args]))
    else:
        workspace = tmp_path / ("new project" if scenario == "edge" else "work")
        args = s.parse_args(["codex", "--workspace", str(workspace), "--allow", "2001:db8::/32"])
        s.validate(args)
        assert args.workspace == str(workspace)
        assert not workspace.exists()


def test_prepare_workspace(scenario, tmp_path):
    path = tmp_path / "parent/work"
    if scenario == "sad":
        path.parent.mkdir()
        path.write_text("preserve")
        with pytest.raises(ValueError):
            s.prepare_workspace(path)
        assert path.read_text() == "preserve"
    else:
        s.prepare_workspace(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        (path / "marker").write_text("preserve")
        if scenario == "edge":
            s.prepare_workspace(path)
        assert (path / "marker").read_text() == "preserve"
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_resolve_hosts(scenario, monkeypatch):
    if scenario == "sad":
        monkeypatch.setattr(s.socket, "getaddrinfo", lambda *a, **kw: [])
        with pytest.raises(ValueError, match="No addresses"):
            s.resolve_hosts(["example.org"])
    else:
        values = ["203.0.113.1"] if scenario == "happy" else ["2001:db8::1", "203.0.113.1", "203.0.113.1"]
        monkeypatch.setattr(s.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", (v, 443)) for v in values])
        assert s.resolve_hosts(["example.org"]) == {"example.org": sorted(set(values))}


def test_hosts_file(scenario):
    # Negative case: an empty permission list must not imply general DNS.
    values = {} if scenario == "sad" else {"example.org": ["203.0.113.1"]}
    if scenario == "edge":
        values["example.org"].append("2001:db8::1")
    text = s.hosts_file(values)
    assert text.startswith("127.0.0.1 localhost\n::1 localhost\n")
    assert "nameserver" not in text
    assert text.count(" example.org\n") == len(values.get("example.org", []))


def test_podman_command(scenario):
    options = ["--podman-url", "unix:///example/podman.sock"] if scenario == "edge" else []
    arg = "$(touch MUST_NOT_RUN)" if scenario == "sad" else "ps"
    cmd = s.podman_command(s.parse_args(["codex", *options]), "pod", arg)
    assert cmd[-2:] == ["pod", arg]
    assert ("--remote" in cmd) == (scenario == "edge")
    assert cmd[0] == "podman"


def test_resolve_pod_hosts(scenario, monkeypatch):
    run = Mock(return_value=Mock(stdout='{"example.org":["203.0.113.1","203.0.113.1"]}'))
    monkeypatch.setattr(s.subprocess, "run", run)
    args = s.parse_args(["codex"])
    if scenario == "sad":
        run.side_effect = subprocess.TimeoutExpired("podman", 45)
        with pytest.raises(ValueError, match="within 45 s"):
            s.resolve_pod_hosts(args, ["example.org"])
        # Podman itself failed (125): its own message and what helps, never only the command line.
        run.side_effect = subprocess.CalledProcessError(
            125, ["podman", "run"], output="", stderr="Error: localhost/codex-sandbox:latest: image not known\n")
        with pytest.raises(ValueError) as failed:
            s.resolve_pod_hosts(args, ["example.org"])
        text = str(failed.value)
        assert "exit status 125" in text and "image not known" in text and "sandbox-ai build codex" in text
    elif scenario == "edge":
        assert s.resolve_pod_hosts(args, []) == {}
        run.assert_not_called()
    else:
        assert s.resolve_pod_hosts(args, ["example.org"]) == {"example.org": ["203.0.113.1"]}
        assert run.call_args.kwargs["timeout"] == 45


def test_podman_failure(scenario):
    a = s.parse_args(["claude"])

    def failed(stderr):
        return s.podman_failure(a, subprocess.CalledProcessError(125, ["podman"], output="", stderr=stderr))
    if scenario == "happy":  # a known cause: what helps
        assert failed("Error: could not find pasta, the network namespace can't be configured: exec: \"pasta\"") \
            .endswith("-> the network needs pasta: install the package passt (Podman 4.4 or newer)")
        assert "subordinate IDs" in failed("cannot find UID/GID for user u: no subuid ranges found for user")
        # Without a user session four WARN lines come first, whatever the error: they decide nothing.
        warned = failed("WARN[0000] The cgroupv2 manager is set to systemd but there is no systemd user session\n"
                        "WARN[0000] Falling back to --cgroup-manager=cgroupfs\nError: something nobody has seen")
        assert "WARN" not in warned and "cgroups" not in warned and warned.endswith("sandbox-ai doctor")
        assert "pasta" not in failed("Error: invalid log driver: passthrough is only supported with a tty")
        assert "podman system migrate" in failed("Error: cannot re-exec process to join the existing user namespace")
        lookup = s.podman_failure(a, subprocess.CalledProcessError(1, ["podman"], output="",
                                                                   stderr="socket.gaierror: [Errno -2] Name unknown"))
        assert lookup.startswith("the host lookup in the image failed (exit status 1)")
        # 127 on the user's Proxmox container: pasta was missing ("executable file not found"), not python3.
        missing = s.podman_failure(a, subprocess.CalledProcessError(
            127, ["podman"], output="", stderr='Error: could not find pasta, the network namespace cannot be '
                                               'configured: exec: "pasta": executable file not found in $PATH'))
        assert missing.startswith("Podman could not start a program it needs (pasta, or the image's python3) "
                                  "(exit status 127)")
        assert missing.endswith("install the package passt (Podman 4.4 or newer)")
        # The same 127 when pasta is there but the container has no /dev/net/tun (the user's Proxmox container).
        tun = s.podman_failure(a, subprocess.CalledProcessError(
            127, ["podman"], output="", stderr="Error: pasta failed with exit code 1:\nFailed to open() /dev/net/tun: "
                                               "No such file or directory\nFailed to set up tap device in namespace"))
        assert "install the package passt" not in tun, "pasta is installed: no advice to install it"
        assert "lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file" in tun and "modprobe tun" in tun
        assert "pasta could not set up the network" in failed("Error: pasta failed with exit code 1: something else")
        assert s.podman_failure(a, subprocess.CalledProcessError(127, ["podman"], output="", stderr="")) \
            .endswith("-> check the host with: sandbox-ai doctor")
        lxc = failed('newuidmap: write to uid_map failed: Operation not permitted\nError: cannot set up namespace '
                     'using "/usr/bin/newuidmap": exit status 1')
        assert "unprivileged container" in lxc and "sandbox-ai doctor" in lxc
        few = failed("Error: potentially insufficient UIDs or GIDs available in user namespace (requested 0:65534 "
                     "for /nonexistent): Check /etc/subuid and /etc/subgid")
        assert "higher IDs" in few and "65536" in few
        assert "sandbox-ai build claude" in failed("Error: localhost/claude-sandbox:latest: image not known")
    elif scenario == "sad":  # an unknown one: Podman's message, and the doctor
        text = failed("Error: something new")
        assert "something new" in text and text.endswith("-> check the host with: sandbox-ai doctor")
    else:  # no message at all, or one with control characters: still readable, never a terminal sequence
        assert s.podman_failure(a, subprocess.CalledProcessError(125, ["podman"], output="", stderr="")) \
            == "Podman failed (exit status 125) before the sandbox started\n  -> check the host with: sandbox-ai doctor"
        assert "\x1b" not in failed("\x1b]0;evil\x07 image not known")


def test_firewall_rules(scenario):
    if scenario == "sad":
        with pytest.raises(ValueError):
            s.firewall_rules(["203.0.113.1; accept"], [])
    else:
        rules = s.firewall_rules(["192.0.2.5/24"] if scenario == "happy" else [],
                                 ["2001:db8::1", "2001:db8::1"] if scenario == "happy" else [])
        assert rules.count("policy drop") == 3
        assert rules.count("ip6 daddr 2001:db8::1 tcp dport 443 accept") == (1 if scenario == "happy" else 0)
        assert "udp dport 53 accept" not in rules
    if scenario == "edge":
        # A VPN's own rules (vpn.py: Tor's ports) come first in the output chain, before loopback is accepted.
        rules = s.firewall_rules([], [], vpn_rules=["ip daddr 127.0.53.1 drop"])
        output = rules.split(" chain output")[1]
        assert output.index("ip daddr 127.0.53.1 drop") < output.index('oifname "lo" accept')
    if scenario == "sad":
        with pytest.raises(ValueError, match="firewall rule"):
            s.firewall_rules([], [], vpn_rules=["accept }\n chain x {"])


def test_copy_private(scenario, tmp_path):
    source, home = tmp_path / "source", tmp_path / "home"
    home.mkdir()
    if scenario == "sad":
        source.mkdir()
        with pytest.raises(ValueError, match="regular file"):
            s.copy_private(source, home, "target")
        assert not (home / "target").exists()
    else:
        source.write_bytes(b"" if scenario == "edge" else b"fixture-data")
        s.copy_private(source, home, "a/b/c")
        assert (home / "a/b/c").read_bytes() == source.read_bytes()
        assert stat.S_IMODE((home / "a/b/c").stat().st_mode) == 0o600
        assert stat.S_IMODE((home / "a").stat().st_mode) == 0o700


def test_auth_sources(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    if scenario == "sad":
        assert not s.auth_sources("codex")[0][0].exists()
    elif scenario == "edge":
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-data"))
        assert s.auth_sources("opencode")[0][0] == tmp_path / "custom-data/opencode/auth.json"
    else:
        assert s.auth_sources("codex")[0] == (tmp_path / "custom-codex/auth.json", ".codex/auth.json")


def test_auth_mode(scenario):
    options = {"happy": ["--copy-auth"], "sad": ["--no-auth"],
               "edge": ["--copy", "/example/metadata:.claude.json"]}[scenario]
    assert s.auth_mode(s.parse_args(["claude", *options])) == {"happy": "copy", "sad": "none", "edge": "manual"}[scenario]


def test_prepare_home(scenario, tmp_path):
    home = tmp_path / "sandbox-home"
    home.mkdir()
    if scenario == "sad":
        with pytest.raises(ValueError, match="Copy syntax"):
            s.prepare_home(s.parse_args(["codex", "--no-auth", "--copy", "invalid"]), home)
    elif scenario == "edge":
        source = tmp_path / "config.jsonc"
        source.write_text('{"model":"fixture"}')
        args = s.parse_args(["opencode", "--no-auth", "--local-config", str(source)])
        s.prepare_home(args, home)
        assert (home / ".config/opencode/opencode.jsonc").read_text() == '{"model":"fixture"}'
        # A reused HOME follows the host's config, even if the agent swapped its copy for a FIFO.
        source.write_text('{"model":"changed"}')
        (home / ".config/opencode/opencode.jsonc").unlink()
        os.mkfifo(home / ".config/opencode/opencode.jsonc")
        s.prepare_home(args, home)
        assert (home / ".config/opencode/opencode.jsonc").read_text() == '{"model":"changed"}'
    else:
        s.prepare_home(s.parse_args(["codex", "--no-auth"]), home)
        assert (home / ".codex").is_dir()
        assert not (home / ".codex/auth.json").exists()


def test_opencode_auth_from_codex(scenario, tmp_path):
    exp = {"happy": 2000000000, "sad": True, "edge": 1}[scenario]
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    source = tmp_path / "fixture.json"
    source.write_text(json.dumps({"tokens": {"access_token": "fixture." + payload + ".signature",
                                            "refresh_token": "fixture-refresh", "account_id": "fixture-account"}}))
    if scenario == "sad":
        with pytest.raises(ValueError, match="JWT expiry"):
            s.opencode_auth_from_codex(source)
    else:
        assert s.opencode_auth_from_codex(source)["openai"]["expires"] == exp * 1000


def test_namespace_state(scenario, monkeypatch):
    if scenario == "sad":
        monkeypatch.setattr(s.os, "readlink", Mock(side_effect=PermissionError("proc denied")))
        with pytest.raises(PermissionError):
            s.namespace_state()
    else:
        monkeypatch.setattr(s.os, "readlink", lambda p: "ns:" + p.rsplit("/", 1)[1])
        lines = "Name:\tignored\nCapEff:\t00000000\nNoNewPrivs:\t1\nSeccomp:\t2\n"
        if scenario == "edge":
            lines += "Unknown: ignored\n"
        monkeypatch.setattr(s.Path, "read_text", lambda *a, **kw: lines)
        result = s.namespace_state()
        assert result == dict(mnt="ns:mnt", pid="ns:pid", CapEff="00000000", NoNewPrivs="1", Seccomp="2")


def test_verify_inner(scenario):
    before, current = states()
    if scenario == "sad":
        for key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb"):
            with pytest.raises(RuntimeError, match="capabilities"):
                s.verify_inner(before, dict(current, **{key: "8000000000000000"}))
    elif scenario == "edge":
        for key in ("mnt", "pid"):
            with pytest.raises(RuntimeError, match="namespaces"):
                s.verify_inner(before, dict(current, **{key: before[key]}))
    else:
        s.verify_inner(before, current)


def test_wait_child(scenario, monkeypatch):
    handlers, restored = {}, []
    def register(sig, handler):
        if callable(handler):
            handlers[sig] = handler
        else:
            restored.append((sig, handler))
        return "previous-handler"
    monkeypatch.setattr(s.signal, "signal", register)
    child = Mock()
    child.poll.return_value = 0 if scenario == "edge" else None
    def wait():
        for sig, handler in handlers.items():
            handler(sig, None)
        if scenario == "sad":
            raise OSError("wait failed")
        return 7
    child.wait.side_effect = wait
    if scenario == "sad":
        with pytest.raises(OSError, match="wait failed"):
            s.wait_child(child)
    else:
        assert s.wait_child(child) == 7
    assert len(restored) == 3
    assert child.send_signal.call_count == (0 if scenario == "edge" else 3)
    if scenario != "edge":
        assert [call.args[0] for call in child.send_signal.call_args_list] == [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]


def test_inner_bootstrap(scenario, monkeypatch, tmp_path):
    monkeypatch.setattr(s.RUNTIME, "SESSION_CONFIG", tmp_path / "absent.json")  # not the session of a sandbox running the tests
    monkeypatch.setattr(s.RUNTIME, "LOG_DIR", tmp_path / "absent-log")
    monkeypatch.setattr(s.os, "getpid", lambda: 1)
    libc = Mock()
    libc.prctl.return_value = 0
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    run, popen = Mock(), Mock()
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(sr, "namespace_state", lambda: states()[0])
    monkeypatch.setattr(sr, "firejail_command", lambda state: ["firejail-fixture"])
    monkeypatch.setattr(sr, "wait_child", lambda child: 0)
    oom = []
    monkeypatch.setattr(s.RUNTIME, "oom_first", oom.append)  # never the real /proc of the machine running the tests
    if scenario == "sad":
        run.side_effect = subprocess.CalledProcessError(1, "nft")
        with pytest.raises(subprocess.CalledProcessError):
            s.inner_bootstrap()
        popen.assert_not_called()
    elif scenario == "edge":
        monkeypatch.setattr(s.os, "getpid", lambda: 2)
        with pytest.raises(RuntimeError, match="PID 1"):
            s.inner_bootstrap()
        run.assert_not_called()
        popen.assert_not_called()
    else:
        assert s.inner_bootstrap() == 0
        assert run.call_args_list[0].args[0] == ["nft", "-f", "/sandbox/session/firewall.nft"]
        assert run.call_args_list[1].args[0][:3] == ["nft", "list", "table"]
        assert popen.call_args.kwargs["close_fds"] is True
        assert oom == [popen.return_value.pid]  # the agent goes first when memory runs out


def test_oom_first(scenario, tmp_path):
    """The agent's oom_score_adj goes OOM_MARGIN above the supervisor's, within the kernel's range."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "42").mkdir()
    (proc / "self" / "oom_score_adj").write_text({"happy": "0\n", "edge": "900\n"}.get(scenario, "nonsense\n"))
    (proc / "42" / "oom_score_adj").write_text("0\n")
    if scenario == "happy":
        assert rt.oom_first(42, proc=proc) == rt.OOM_MARGIN
        assert (proc / "42" / "oom_score_adj").read_text() == f"{rt.OOM_MARGIN}\n"
    elif scenario == "sad":
        assert rt.oom_first(42, proc=proc) is None  # unreadable value: nothing written
        assert (proc / "42" / "oom_score_adj").read_text() == "0\n"
        (proc / "self" / "oom_score_adj").write_text("0\n")
        assert rt.oom_first(7, proc=proc) is None  # the process ended already
    else:
        assert rt.oom_first(42, proc=proc) == 1000  # capped at the kernel's maximum
        assert rt.oom_first(42, proc=proc, margin=-5000) == -1000


def test_memory_part(scenario, monkeypatch):
    """A table of the supervisor, registered for MemoryGuard's records by its size."""
    monkeypatch.setattr(rt, "MEMORY_PARTS", {})
    if scenario == "happy":
        table = rt.memory_part("seen", {1, 2})
        assert table == {1, 2} and rt.MEMORY_PARTS["seen"]() == 2
        table.add(3)
        assert rt.MEMORY_PARTS["seen"]() == 3  # read at each record, not at registration
    elif scenario == "sad":
        rt.memory_part("gone", lambda: 7)  # a callable gives the number itself
        assert rt.MEMORY_PARTS["gone"]() == 7
    else:
        rt.memory_part("same", [1])
        rt.memory_part("same", [1, 2, 3])  # the newest registration wins (one supervisor, one table)
        assert rt.MEMORY_PARTS["same"]() == 3


def test_resident_bytes(scenario, tmp_path):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    if scenario == "happy":
        (proc / "self" / "status").write_text("Name:\tkthreadd\nVmRSS:\t  2048 kB\nThreads:\t9\n")
        assert rt.resident_bytes(proc) == 2 << 20
        assert rt.resident_bytes() > 0  # this process, for real
    elif scenario == "sad":
        assert rt.resident_bytes(proc) is None  # no status file
    else:
        (proc / "self" / "status").write_text("Name:\tkthreadd\nVmRSS:\tlots\n")
        assert rt.resident_bytes(proc) is None


def test_memory_guard_records_each_step_with_its_tables_and_threads(tmp_path, monkeypatch):
    """The supervisor's own look at its memory: a record per step (then doubled), with its tables and threads, at
    most RECORDS; freed memory given back at every look."""
    rss, trims = [100 << 20], []
    guard = rt.MemoryGuard(tmp_path, rss=lambda: rss[0], trim=lambda: trims.append(1), step=256 << 20,
                           trace=False, records=2, parts={"seen": lambda: 41, "broken": lambda: 1 / 0},
                           shedders={})
    assert guard.check() is None and trims == [1]          # below the first step: nothing written
    rss[0] = 300 << 20
    record = guard.check()
    assert record["rss"] == 300 << 20 and record["parts"] == {"seen": 41}  # a broken table is left out
    assert guard.next == 512 << 20 and record["threads"].get("MainThread") == 1 and record["objects"]
    assert guard.check() is None                           # the same step again: no new record
    rss[0] = 3 << 30
    assert guard.check()["rss"] == 3 << 30 and guard.next == 4 << 30  # past several steps: one record
    rss[0] = 5 << 30
    assert guard.check() is None                           # RECORDS reached
    lines = (tmp_path / rt.SUPERVISOR_MEMORY_FILE).read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["peak"] == 3 << 30
    assert len(trims) == 5


def test_memory_guard_traces_the_growth_from_its_threshold(tmp_path):
    import tracemalloc
    if tracemalloc.is_tracing():
        pytest.skip("allocations are traced already")
    rss = [600 << 20]
    guard = rt.MemoryGuard(None, rss=lambda: rss[0], trim=lambda: None, step=256 << 20, trace_from=512 << 20,
                           shedders={})
    try:
        assert "traced" in guard.check() or tracemalloc.is_tracing()
        held = [bytearray(1000) for _ in range(2000)]  # noqa: F841 - the growth to be found
        rss[0] = 1100 << 20
        record = guard.check()
        assert record["traced"] and all({"where", "bytes", "count"} <= set(entry) for entry in record["traced"])
        assert not (Path(".") / rt.SUPERVISOR_MEMORY_FILE).exists()  # no directory: nothing written anywhere
    finally:
        tracemalloc.stop()


def test_memory_guard_survives_a_broken_look(tmp_path):
    guard = rt.MemoryGuard(tmp_path / "gone", rss=lambda: None, trim=lambda: 1 / 0, interval=0.01)
    assert guard.check() is None  # no RSS, a failing trim: nothing, no exception
    guard = rt.MemoryGuard(tmp_path / "gone", rss=lambda: 1 << 30, trim=lambda: None, trace=False, shedders={})
    assert guard.check()["rss"] == 1 << 30  # a directory that is not there: the record still comes back
    stop = rt.threading.Event()
    thread = rt.MemoryGuard(None, rss=lambda: 1 / 0, trim=lambda: None, interval=0.01).start(stop)
    time.sleep(0.05)
    assert thread.is_alive() and thread.name == "memory-guard"
    stop.set()
    thread.join(1)
    assert not thread.is_alive()
    assert callable(rt.MemoryGuard.malloc_trim())


def test_firejail_command(scenario, monkeypatch):
    run = Mock(return_value=Mock(stdout="--shell=program" if scenario == "edge" else "--seccomp"))
    monkeypatch.setattr(s.subprocess, "run", run)
    if scenario == "sad":
        run.side_effect = FileNotFoundError("firejail")
        with pytest.raises(FileNotFoundError):
            s.firejail_command(states()[0])
    else:
        cmd = s.firejail_command(states()[0])
        assert ("--shell=none" in cmd) == (scenario == "edge")
        assert "--caps.drop=all" in cmd and "--noprofile" in cmd
        assert json.loads(cmd[-1]) == states()[0]


def test_base_env(scenario, monkeypatch):
    monkeypatch.setenv("HOST_SECRET_FIXTURE", "must-not-leak")
    if scenario == "edge":
        monkeypatch.setenv("TERM", "xterm-fixture")
    env = s.base_env()
    assert env["HOME"] == "/root" and env["CODEX_HOME"] == "/root/.codex"
    assert "HOST_SECRET_FIXTURE" not in env
    assert env["TERM"] == ("xterm-fixture" if scenario == "edge" else "xterm-256color")
    if scenario == "sad":
        monkeypatch.setenv("HOME", "/untrusted-home")
        assert s.base_env()["HOME"] == "/root"


def test_agent_environment(scenario, monkeypatch):
    monkeypatch.setenv("CONTRACT_VALUE", "fixture-value")
    options = ["--env", "CONTRACT_VALUE"]
    if scenario == "edge":
        options += ["--local-config", "/example/config.jsonc"]
    args = s.parse_args(["opencode", *options])
    if scenario == "sad":
        monkeypatch.delenv("CONTRACT_VALUE")
        with pytest.raises(KeyError):
            s.agent_environment(args)
    else:
        env = s.agent_environment(args)
        assert env["CONTRACT_VALUE"] == "fixture-value"
        assert (env.get("OPENCODE_DISABLE_PROJECT_CONFIG") == "1") == (scenario == "edge")


def test_agent_command(scenario):
    if scenario == "sad":
        args = s.parse_args(["codex", "--", "exec", "--sandbox=read-only", "task"])
        assert s.agent_command(args, "codex") == ["codex", *args.agent_args]
    else:
        tail = ["exec", "--", "--sandbox=literal-task"] if scenario == "edge" else ["exec", "task"]
        args = s.parse_args(["codex", "--", *tail])
        assert s.agent_command(args, "codex") == ["codex", "--sandbox", "danger-full-access", *tail]


def test_inner_guard(scenario, monkeypatch, capsys):
    before, current = states()
    monkeypatch.setattr(sr, "namespace_state", lambda: current)
    config = {"probe": scenario == "edge", "env": {"CONTRACT_VALUE": "fixture"}, "command": ["agent", "task"]}
    read = Mock(return_value=json.dumps(config))
    monkeypatch.setattr(s.Path, "read_text", read)
    chdir, execute = Mock(), Mock()
    monkeypatch.setattr(s.os, "chdir", chdir)
    monkeypatch.setattr(s.os, "execvpe", execute)
    if scenario == "sad":
        current["CapEff"] = "1"
        with pytest.raises(RuntimeError):
            s.inner_guard(before)
        read.assert_not_called()
        execute.assert_not_called()
    elif scenario == "edge":
        assert s.inner_guard(before) == 0
        assert json.loads(capsys.readouterr().out)["verified"] is True
        execute.assert_not_called()
        chdir.assert_not_called()
    else:
        s.inner_guard(before)
        chdir.assert_called_once_with("/work")
        cmd, argv, env = execute.call_args.args
        assert cmd == "agent" and argv == ["agent", "task"]
        assert env["CONTRACT_VALUE"] == "fixture" and env["HOME"] == "/root"
        config["tmux"] = True
        read.return_value = json.dumps(config)
        s.inner_guard(before)
        cmd, argv, env = execute.call_args.args
        assert cmd == "tmux" and argv == s.tmux_command(["agent", "task"])


def test_mount_option(scenario):
    if scenario == "sad":
        for source in ("/tmp/a,b", "/tmp/a\nb", "/tmp/a\rb"):
            with pytest.raises(ValueError):
                s.mount_option(source, "/work", "rw")
    else:
        mode = "ro" if scenario == "edge" else "rw"
        assert s.mount_option("/tmp/space dir", "/work", mode) == ["--mount", f"type=bind,src=/tmp/space dir,dst=/work,{mode},nodev"]


def test_pod_commands(scenario, tmp_path, monkeypatch):
    options = ["--offline"] if scenario == "sad" else []
    if scenario == "edge":
        options = ["--network", "research", "--ip", "192.0.2.2", "--ip6", "2001:db8::2", "--workspace-ro"]
    args = s.parse_args(["codex", "--workspace", str(tmp_path), *options])
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: scenario == "edge")
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: scenario == "edge")
    create, run = s.pod_commands(args, "owned-fixture", tmp_path / "session", tmp_path / "home")
    assert create[create.index("--network") + 1] == ("none" if scenario == "sad" else "research" if scenario == "edge" else s.POD_NETWORK)
    if scenario == "happy":
        forwarded = s.parse_args(["codex", "--workspace", str(tmp_path), "--allow-host-port", "3000"])
        create, _ = s.pod_commands(forwarded, "owned-fixture", tmp_path / "session", tmp_path / "home")
        assert create[create.index("--network") + 1] == s.pod_network(forwarded)
        assert "-T,3000" in create[create.index("--network") + 1]
    assert ("--tty" in run) == (scenario == "edge")
    assert not any(v.startswith("--publish") for v in run)
    assert "--label" not in create
    if scenario == "happy":
        # Session pods carry their session's tmux directory, so a stale one can be recognised.
        session = s.parse_args(["codex", "--workspace", str(tmp_path), "--pod-name", "agent-session-x",
                                "--tmux-socket-dir", str(tmp_path / "sess" / "tmux")])
        create, _ = s.pod_commands(session, "agent-session-x", tmp_path / "session", tmp_path / "home")
        assert create[create.index("--label") + 1] == f"{s.SESSION_LABEL}={(tmp_path / 'sess' / 'tmux').resolve()}"
    assert f"type=bind,src={tmp_path},dst=/work,{'ro' if scenario == 'edge' else 'rw'},nodev" in run
    assert "--device" not in run
    if scenario == "happy":
        # Single devices and CDI names through --device; directories as bind mounts, so replugged devices show.
        monkeypatch.setattr(s, "devices", lambda a: [("/dev/ttyACM0", "node"), ("/dev/bus/usb", "dir"),
                                                     ("nvidia.com/gpu=all", "cdi")])
        _, run = s.pod_commands(args, "owned-fixture", tmp_path / "session", tmp_path / "home")
        assert [run[i + 1] for i, v in enumerate(run) if v == "--device"] == ["/dev/ttyACM0", "nvidia.com/gpu=all"]
        assert "type=bind,src=/dev/bus/usb,dst=/dev/bus/usb,rw,dev" in run


def test_run_pod(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--offline"])
    masks = []
    run = Mock(side_effect=lambda *a, **k: masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
    popen, wait = Mock(), Mock(return_value=9 if scenario == "edge" else 0)
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(sr, "wait_child", wait)
    if scenario == "sad":
        popen.side_effect = OSError("spawn failed")
        with pytest.raises(OSError, match="spawn failed"):
            s.run_pod(args, tmp_path, tmp_path)
    else:
        assert s.run_pod(args, tmp_path, tmp_path) == (9 if scenario == "edge" else 0)
    cleanup = run.call_args_list[-1].args[0]
    assert cleanup[:4] == ["podman", "pod", "rm", "--force"]
    assert cleanup[-1].startswith("agent-codex-")
    # A SIGTERM or Ctrl-C cannot cut the pod removal short; the commands before it stay interruptible.
    assert set(s.CLEANUP_SIGNALS) <= masks[-1] and not set(s.CLEANUP_SIGNALS) & masks[0]


def test_bwrap_command(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--backend", "bwrap", "--workspace", str(tmp_path)])
    if scenario == "sad":
        args.runtime = [str(tmp_path)]
        with pytest.raises(ValueError, match="runtime"):
            s.bwrap_command(args, tmp_path, tmp_path)
    else:
        args.workspace_ro = scenario == "edge"
        cmd = s.bwrap_command(args, tmp_path, tmp_path)
        assert "--share-net" not in cmd and "--clearenv" in cmd
        position = cmd.index("/work")
        assert cmd[position - 2] == ("--ro-bind" if scenario == "edge" else "--bind")
        assert "--dev-bind" not in cmd
        monkeypatch.setattr(s, "devices", lambda a: [("/dev/kvm", "node"), ("/dev/dri", "dir")])
        cmd = s.bwrap_command(args, tmp_path, tmp_path)
        assert [cmd[i + 1:i + 3] for i, v in enumerate(cmd) if v == "--dev-bind"] == [["/dev/kvm", "/dev/kvm"],
                                                                                     ["/dev/dri", "/dev/dri"]]
        assert cmd.index("--dev-bind") > cmd.index("--dev")  # on top of the sandbox's own /dev
        monkeypatch.setattr(s, "devices", lambda a: [("nvidia.com/gpu=all", "cdi")])
        with pytest.raises(ValueError, match="CDI"):
            s.bwrap_command(args, tmp_path, tmp_path)


def test_read_ready(scenario):
    read_fd, write_fd = os.pipe()
    child = Mock(returncode=3)
    child.poll.return_value = None
    try:
        if scenario == "sad":
            os.close(write_fd)
            write_fd = -1
            with pytest.raises(RuntimeError, match="exited early"):
                s.read_ready(read_fd, child, timeout=0.2)
        else:
            value = b'1' if scenario == "edge" else b'{"child-pid":12345}'
            os.write(write_fd, value)
            assert s.read_ready(read_fd, child, timeout=0.2) == (1 if scenario == "edge" else {"child-pid": 12345})
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_stop_process(scenario):
    process = Mock()
    process.poll.return_value = 0 if scenario == "edge" else None
    if scenario == "happy":
        # The proxy: its stdin closes, it exits; no signal (it runs as another user, without CAP_KILL).
        s.stop_process(process)
        assert process.stdin.close.call_count == 1 and process.terminate.call_count == 0
        # Still running after that and not ours to signal: left to the end of the PID namespace.
        other = Mock()
        other.poll.return_value = None
        other.wait.side_effect = subprocess.TimeoutExpired("fixture", 2)
        other.terminate.side_effect = PermissionError(1, "Operation not permitted")
        s.stop_process(other)
        assert other.kill.call_count == 0
    elif scenario == "sad":
        process.stdin = None
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
        s.stop_process(process)
        assert process.terminate.call_count == 1 and process.kill.call_count == 1
    else:
        s.stop_process(process)
        assert process.terminate.call_count == 0 and process.kill.call_count == 0
    s.stop_process(None)


def test_exit_on_eof(scenario):
    import io
    exits = []
    stream = {"happy": io.BytesIO(b"x" * 10000), "sad": Mock(read=Mock(side_effect=OSError(9, "bad"))),
              "edge": io.BytesIO(b"")}[scenario]
    rt.exit_on_eof(stream, exit=exits.append)
    assert exits == [0]


def test_subordinate_range(scenario, monkeypatch):
    monkeypatch.setattr(s.pwd, "getpwuid", lambda uid: Mock(pw_name="fixture-user"))
    monkeypatch.setattr(s.os, "getuid", lambda: 1234)
    entries = {"happy": "fixture-user:100000:65536\n", "sad": "fixture-user:100000:65535\n",
               "edge": "invalid\nother:200000:65536\n1234:300000:65536\n"}[scenario]
    monkeypatch.setattr(s.Path, "read_text", lambda *a, **kw: entries)
    if scenario == "sad":
        with pytest.raises(ValueError, match="65536"):
            s.subordinate_range("uid")
    else:
        assert s.subordinate_range("uid") == (300000 if scenario == "edge" else 100000, 65536)


def test_configure_user_maps(scenario, monkeypatch):
    monkeypatch.setattr(s, "subordinate_range", lambda kind: (100000, 65536))
    run = Mock()
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.Path, "read_text", lambda *a, **kw: "allow\n" if scenario != "edge" else "deny\n")
    if scenario == "sad":
        run.side_effect = subprocess.CalledProcessError(1, "newuidmap")
        with pytest.raises(subprocess.CalledProcessError):
            s.configure_user_maps(12345)
        assert run.call_count == 1
    elif scenario == "edge":
        with pytest.raises(RuntimeError, match="setgroups"):
            s.configure_user_maps(12345)
    else:
        s.configure_user_maps(12345)
        assert [c.args[0][0] for c in run.call_args_list] == ["newuidmap", "newgidmap"]


def test_run_bwrap(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--backend", "bwrap", *(["--offline"] if scenario == "edge" else [])])
    monkeypatch.setattr(s, "bwrap_command", lambda *a: ["bwrap-fixture"])
    children = [Mock(), Mock()]
    popen = Mock(side_effect=children)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    ready = Mock(side_effect=[{"child-pid": 12345}, True])
    monkeypatch.setattr(s, "read_ready", ready)
    maps = Mock(side_effect=RuntimeError("mapping failed") if scenario == "sad" else None)
    monkeypatch.setattr(s, "configure_user_maps", maps)
    monkeypatch.setattr(sr, "wait_child", lambda child: 0)
    stop = Mock(side_effect=lambda process: masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
    masks = []
    monkeypatch.setattr(sr, "stop_process", stop)
    write = Mock()
    monkeypatch.setattr(s.os, "write", write)
    real_pipe, descriptors = os.pipe, []
    def pipe():
        pair = real_pipe()
        descriptors.extend(pair)
        return pair
    monkeypatch.setattr(s.os, "pipe", pipe)
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="mapping failed"):
            s.run_bwrap(args, tmp_path, tmp_path)
        write.assert_not_called()
        assert popen.call_count == 1
    else:
        assert s.run_bwrap(args, tmp_path, tmp_path) == 0
        assert popen.call_count == (1 if scenario == "edge" else 2)
        assert write.call_count == 2
    assert stop.call_count == 2
    assert all(set(s.CLEANUP_SIGNALS) <= mask for mask in masks)
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_main(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = Mock(return_value=0)
    monkeypatch.setattr(s, "run_pod", runner)
    options = ["codex", "--workspace", str(tmp_path / "work"), "--offline", "--no-auth"]
    if scenario == "sad":
        monkeypatch.setattr(s.os, "geteuid", lambda: 0)
        with pytest.raises(ValueError, match="ordinary host user"):
            s.main(options)
        runner.assert_not_called()
        assert not (tmp_path / "work").exists()
    else:
        seen = {}
        def inspect(args, session, home):
            seen.update(config=json.loads((session / "config.json").read_text()), home=home, session=session)
            assert (session / "config.json").stat().st_mode & 0o777 == 0o600
            return 0
        runner.side_effect = inspect
        if scenario == "edge":
            options += ["--state-dir", str(tmp_path / "persistent")]
        else:
            options += ["--no-keep"]  # the HOME goes with the run (kept by default: see the feature matrix)
        assert s.main(options) == 0
        assert seen["config"]["command"][:3] == ["codex", "--sandbox", "danger-full-access"]
        assert not seen["session"].exists()
        assert seen["home"].exists() == (scenario == "edge")


def test_regression_later_parent_mount_hides_previous_child(tmp_path):
    """The most recent parent bind determines the visible destination tree."""
    workspace, parent, child = [tmp_path / p for p in ("work", "parent", "child")]
    for path in (workspace, parent, child):
        path.mkdir()
    (parent / "child").symlink_to("/sandbox", target_is_directory=True)
    args = s.parse_args(["codex", "--workspace", str(workspace),
                         "--mount", f"{child}:/mnt/data/child:rw",
                         "--mount", f"{parent}:/mnt/data:rw",
                         "--mount", f"{workspace}:/mnt/data/child/session:rw"])
    with pytest.raises(ValueError, match="symlink"):
        s.validate(args)


def test_regression_partial_pipe_allocation_closes_owned_descriptors(tmp_path, monkeypatch):
    real_pipe = os.pipe
    allocated = []
    def pipe():
        if allocated:
            raise OSError("fixture pipe allocation failure")
        pair = real_pipe()
        allocated.extend(pair)
        return pair
    monkeypatch.setattr(s.os, "pipe", pipe)
    try:
        with pytest.raises(OSError, match="pipe allocation"):
            s.run_bwrap(s.parse_args(["codex", "--backend", "bwrap"]), tmp_path, tmp_path)
        for fd in allocated:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for fd in allocated:
            try:
                os.close(fd)
            except OSError:
                pass


class StopLoop(BaseException):
    """Ends an endless logger loop from a mocked boundary."""


def claude_credentials(path, expires, refresh="refresh-fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "access-fixture", "refreshToken": refresh,
                                                  "expiresAt": expires, "scopes": ["user:inference"]}}))
    return path


def test_host_networks(scenario):
    hosts = {"happy": ["api.anthropic.com"], "sad": ["api.openai.com"], "edge": []}[scenario]
    assert s.host_networks(hosts) == (["160.79.104.0/23", "2607:6bc0::/48"] if scenario == "happy" else [])


def test_log_event(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / ("missing" if scenario == "sad" else "log"))
    if scenario == "sad":
        with pytest.raises(OSError):
            s.log_event("files.jsonl", {"action": "read"})
        return
    (tmp_path / "log").mkdir()
    for _ in range(2 if scenario == "edge" else 1):
        s.log_event("files.jsonl", {"path": "/work/\u00e4 \"quoted\""})
    lines = [json.loads(line) for line in (tmp_path / "log/files.jsonl").read_text().splitlines()]
    assert len(lines) == (2 if scenario == "edge" else 1)
    assert lines[0]["path"] == "/work/\u00e4 \"quoted\"" and lines[0]["time"].endswith("Z")


def test_start_loggers(scenario, tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    monkeypatch.setattr(s.RUNTIME, "SESSION_CONFIG", config)
    started = []
    monkeypatch.setattr(s.RUNTIME.threading, "Thread", lambda target, args, daemon, name=None: Mock(start=lambda: started.append((target, args))))
    if scenario == "sad":
        with pytest.raises(OSError):
            s.start_loggers()
        assert started == []
        return
    config.write_text(json.dumps({"pinned": ["api.openai.com"] if scenario == "happy" else [], "watch": ["/work"],
                                  "env": {"API_KEY": "value-of-the-key", "PATH": "/usr/bin"},
                                  "secret_names": ["API_KEY", "MISSING"] if scenario == "happy" else []}))
    who = object()  # the supervisor's Attribution, handed to the loggers that name a process
    stop = s.start_loggers(None, (), who)
    assert not stop.is_set() and stop.seen == set() and len(stop.threads) == 4
    targets = {target.__name__: args for target, args in started}
    assert targets["dns_logger"][:8] == ({"api.openai.com"} if scenario == "happy" else set(), 53, stop, None, (), who,
                                         None, None)
    # The host wildcards (none here): the start's from config.json, with those added during the run.
    upstream_port, wildcards = targets["dns_logger"][8:]
    assert upstream_port == 53 and wildcards() == set()
    assert targets["file_logger"] == (["/work"], stop)
    # The process log redacts the values of --env and --secret-env variables by value.
    assert "denied_loop" in targets and targets["process_logger"] == (
        stop, 0.5, Path("/proc"), ["value-of-the-key"] if scenario == "happy" else [])


def test_poll_denied(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    elements = {"happy": [{"concat": ["198.51.100.7", "tcp", 80]}],
                "edge": [{"elem": {"val": {"concat": ["2001:db8::9", "udp", 123]}, "expires": 1}},
                         {"concat": ["198.51.100.7", "tcp", 80]}]}.get(scenario, [])
    output = json.dumps({"nftables": [{"metainfo": {}}, {"set": {"name": "denied4", "elem": elements}}]})
    run = Mock(return_value=Mock(returncode=1 if scenario == "sad" else 0, stdout=output))
    monkeypatch.setattr(s.subprocess, "run", run)
    seen = {("198.51.100.7", "tcp", 80)} if scenario == "edge" else set()
    s.poll_denied(seen)
    s.poll_denied(seen)
    log = tmp_path / "egress-denied.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert [r.get("address") for r in records] == {"happy": ["198.51.100.7"], "sad": [], "edge": ["2001:db8::9"]}[scenario]
    assert run.call_args_list[0].args[0] == ["nft", "-j", "list", "set", "inet", "agent_sandbox", "denied4"]


def test_dns_reply(scenario):
    question = (b"\x07example\x03org\x00" if scenario != "edge" else b"\x03API\x07Example\x03COM\x00") + b"\x00\x01\x00\x01"
    query = b"\xbe\xef" + (b"\x01\x00" if scenario == "happy" else b"\x00\x00") + b"\x00\x01\x00\x00\x00\x00\x00\x00" + question
    if scenario == "sad":
        with pytest.raises(struct_error()):
            s.dns_reply(b"\x00")
        return
    name, reply = s.dns_reply(query)
    assert name == ("example.org" if scenario == "happy" else "api.example.com")
    assert reply[:2] == b"\xbe\xef" and reply[12:] == question
    flags = int.from_bytes(reply[2:4], "big")
    assert flags & 0xF == 3 and flags & 0x8000 and bool(flags & 0x0100) == (scenario == "happy")


def test_forward_dns(scenario):
    """The DNS stub's forwarder: the first nameserver whose answer carries the query's id."""
    import socket as _socket
    import threading
    query = b"\xbe\xef\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03org\x00\x00\x01\x00\x01"
    server = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def answer():
        data, peer = server.recvfrom(4096)
        ident = data[:2] if scenario != "sad" else b"\x00\x00"  # sad: an answer to another query
        server.sendto(ident + b"\x81\x80" + data[4:], peer)
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        if scenario == "happy":
            found = rt.forward_dns(query, ["127.0.0.1"], timeout=2.0, port=port)
            assert found is not None and found[1] == "127.0.0.1" and found[0][:2] == b"\xbe\xef"
        elif scenario == "sad":
            assert rt.forward_dns(query, ["127.0.0.1"], timeout=1.0, port=port) is None
            assert rt.forward_dns(query, [], port=port) is None
        else:
            # A nameserver that cannot be reached is skipped; the next one answers.
            found = rt.forward_dns(query, ["::1" if _socket.has_ipv6 else "127.0.0.2", "127.0.0.1"],
                                   timeout=0.5, port=port)
            assert found is not None and found[1] == "127.0.0.1"
    finally:
        thread.join(timeout=3)
        server.close()


def struct_error():
    import struct
    return struct.error


def test_dns_logger(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    name = {"happy": b"\x04evil\x07example\x00", "edge": b"\x03api\x06openai\x03com\x00"}.get(scenario, b"")
    packet = b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + b"\x00\x01\x00\x01"
    sock = Mock()
    sock.recvfrom.side_effect = [(b"\x01" if scenario == "sad" else packet, ("127.0.0.1", 5353)), StopLoop()]
    monkeypatch.setattr(s.socket, "socket", lambda *a: sock)
    with pytest.raises(StopLoop):
        s.dns_logger({"api.openai.com"})
    sock.bind.assert_called_once_with(("127.0.0.1", 53))
    log = tmp_path / "egress-denied.jsonl"
    hosts = [json.loads(line)["host"] for line in log.read_text().splitlines()] if log.exists() else []
    assert hosts == (["evil.example"] if scenario == "happy" else [])
    assert sock.sendto.called == (scenario != "sad")


def wildcard_query(name=b"\x03api\x07example\x03com\x00", qtype=1):
    return b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + qtype.to_bytes(2, "big") + b"\x00\x01"


def test_dns_address_reply(scenario):
    rt = s.RUNTIME
    if scenario == "happy":
        reply = rt.dns_address_reply(wildcard_query(), ["203.0.113.7", "2001:db8::1", "198.51.100.2"])
        assert reply[:2] == b"\x12\x34" and int.from_bytes(reply[2:4], "big") == 0x8180 | 0x0100
        assert int.from_bytes(reply[6:8], "big") == 2  # the A records only
        record = b"\xc0\x0c\x00\x01\x00\x01" + (30).to_bytes(4, "big") + b"\x00\x04"
        assert reply.endswith(record + bytes([203, 0, 113, 7]) + record + bytes([198, 51, 100, 2]))
        aaaa = rt.dns_address_reply(wildcard_query(qtype=28), ["203.0.113.7", "2001:db8::1"])
        assert int.from_bytes(aaaa[6:8], "big") == 1 and aaaa.endswith(socket.inet_pton(socket.AF_INET6, "2001:db8::1"))
    elif scenario == "sad":
        # Another type (MX), or a question cut short: no answer of ours, the caller sends its refusal.
        assert rt.dns_address_reply(wildcard_query(qtype=15), ["203.0.113.7"]) is None
        assert rt.dns_address_reply(b"\x12\x34" + b"\x00" * 10 + b"\x03api", ["203.0.113.7"]) is None
    else:
        # No address of the asked type: NOERROR without answers (NODATA), so the client takes the other type.
        reply = rt.dns_address_reply(wildcard_query(qtype=28), ["203.0.113.7", "not an address"])
        assert int.from_bytes(reply[2:4], "big") & 0xF == 0 and int.from_bytes(reply[6:8], "big") == 0


def test_public_address(scenario):
    rt = s.RUNTIME
    ip = ipaddress.ip_address
    if scenario == "happy":
        assert rt.public_address(ip("203.0.114.9")) is not None and rt.public_address(ip("2606:4700::1111"))
    elif scenario == "sad":
        for value in ["10.0.0.1", "127.0.0.1", "192.168.1.1", "::1", "fd00::1", "169.254.1.1"]:
            assert not rt.public_address(ip(value))
    else:
        # NAT64 counts as global to Python, but a local translator turns it into any IPv4 address.
        assert ip("64:ff9b::7f00:1").is_global and not rt.public_address(ip("64:ff9b::7f00:1"))
        assert not rt.public_address(ip("64:ff9b:1::a00:1"))


def test_a_remembered_name_resolving_to_nat64_is_not_opened(tmp_path):
    """NAT64 counts as global to Python, yet a local translator turns 64:ff9b::a00:1 into 10.0.0.1: a name approved
    "always" that resolves there now is not opened at the next start."""
    a = argparse.Namespace(policy={})
    pins, notes = s.remembered_pins(a, ["api.example.org"], lambda names: {"api.example.org": ["64:ff9b::a00:1"]})
    assert "api.example.org" not in pins and any("not public" in note for note in notes)


def test_wildcard_suffix(scenario):
    rt = s.RUNTIME
    if scenario == "happy":
        assert rt.wildcard_suffix("*.example.com") == "example.com" and rt.wildcard_suffix("*.A.Example.org") == "a.example.org"
    elif scenario == "sad":
        for value in ["*.com", "example.com", "*example.com", "*.*.example.com", "*.exa mple.com", "*.", None, "*.-a.com"]:
            with pytest.raises(ValueError, match="host wildcard"):
                rt.wildcard_suffix(value)
    else:
        # Below a public suffix or a hosting service anyone has names of their own: the agent could rent one.
        for value in ["*.co.uk", "*.com.au", "*.github.io", "*.ec2.amazonaws.com", "*.herokuapp.com", "*.nip.io"]:
            with pytest.raises(ValueError, match="anyone can have"):
                rt.wildcard_suffix(value)
        assert rt.wildcard_suffix("*.shop.co.uk") == "shop.co.uk"  # a company's own domain below one is fine


def test_under_wildcard(scenario):
    rt = s.RUNTIME
    if scenario == "happy":
        assert rt.under_wildcard("api.example.com", {"example.com"}) and rt.under_wildcard("a.b.example.com", {"example.com"})
    elif scenario == "sad":
        # Not the domain itself, not a name that merely ends the same way.
        assert not rt.under_wildcard("example.com", {"example.com"}) and not rt.under_wildcard("evilexample.com", {"example.com"})
    else:
        assert rt.under_wildcard("API.Example.COM.", {"example.com"}) and not rt.under_wildcard("x.example.com", set())


def test_change_wildcards(scenario, monkeypatch):
    rt = s.RUNTIME
    monkeypatch.setattr(rt, "WILDCARDS", set())
    if scenario == "happy":
        assert rt.change_wildcards({"add": ["*.example.com", "*.Example.org"]}) == ["example.com", "example.org"]
        assert rt.change_wildcards({"remove": ["*.example.org"]}) == ["example.com"]
    elif scenario == "sad":
        for forbid, mode in ((["--allow-host"], None), ((), {"name": "high", "refuses": ["--allow-host"]})):
            with pytest.raises(ValueError, match="refused for this run"):
                rt.change_wildcards({"add": ["*.example.com"]}, forbid, mode)
        assert rt.WILDCARDS == set()
    else:
        with pytest.raises(ValueError, match="host wildcard"):
            rt.change_wildcards({"add": ["*.com"]})
        assert rt.change_wildcards({"remove": ["*.example.com"]}, ["--allow-host"]) == []  # taking back is always allowed


def test_host_wildcards(scenario, tmp_path):
    (tmp_path / "w").mkdir()
    extra = {"happy": ["--allow-host", "*.Example.com", "--allow-host", "api.example.org"], "sad": ["--offline"],
             "edge": ["--allow-host", "*.example.com", "--allow-host", "*.EXAMPLE.com"]}[scenario]
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), *extra])
    if scenario == "sad":
        assert s.host_wildcards(a) == []
        return
    assert s.host_wildcards(a) == ["*.example.com"]
    assert not any(h.startswith("*.") for h in s.allowed_hosts(a))  # never resolved nor pinned as a name
    if scenario == "happy":
        assert "api.example.org" in s.allowed_hosts(a)


def test_wildcard_lookups_are_held_and_answered_with_their_addresses(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    sock = Mock()
    sent = threading.Event()
    sock.sendto.side_effect = lambda data, peer: sent.set()
    sock.recvfrom.side_effect = [(wildcard_query(), ("127.0.0.1", 5353)), StopLoop()]
    monkeypatch.setattr(s.socket, "socket", lambda *a: sock)
    broker = Mock()
    broker.ask.return_value = {"decision": "allow", "scope": "run", "addresses": ["203.0.113.7"]}
    with pytest.raises(StopLoop):
        s.RUNTIME.dns_logger(set(), broker=broker, wildcards=lambda: {"example.com"})
    assert sent.wait(5)
    reply = sock.sendto.call_args[0][0]
    assert int.from_bytes(reply[6:8], "big") == 1 and reply.endswith(bytes([203, 0, 113, 7]))
    name, details = broker.ask.call_args[0]
    assert name == "dns" and details["name"] == "api.example.com" and broker.ask.call_args[1] == {"wait": True}
    assert not (tmp_path / "egress-denied.jsonl").exists()  # held, not refused
    # Refused on the host (outside the policy, no public address): the client gets the refusal, not a hang.
    sock.sendto.reset_mock()
    sent.clear()
    sock.recvfrom.side_effect = [(wildcard_query(), ("127.0.0.1", 5353)), StopLoop()]
    broker.ask.return_value = {"decision": "deny", "scope": "run"}
    with pytest.raises(StopLoop):
        s.RUNTIME.dns_logger(set(), broker=broker, wildcards=lambda: {"example.com"})
    assert sent.wait(5) and int.from_bytes(sock.sendto.call_args[0][0][2:4], "big") & 0xF == 3


def test_file_logger(scenario, tmp_path, monkeypatch):
    import struct
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    (tmp_path / "log").mkdir()
    root = tmp_path / "work"
    (root / "sub").mkdir(parents=True)
    libc = Mock()
    libc.inotify_init1.return_value = -1 if scenario == "sad" else 7
    watched = []
    # Watches go through the walked directory's descriptor (/proc/self/fd/N), never a path a swap could redirect.
    libc.inotify_add_watch.side_effect = lambda fd, path, mask: watched.append(
        os.readlink(os.fsdecode(path)) if os.fsdecode(path).startswith("/proc/self/fd/") else "not by descriptor") \
        or len(watched)
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    def event(wd, mask, name):
        raw = name.encode() + b"\0" * (16 - len(name))
        return struct.pack("iIII", wd, mask, 0, len(raw)) + raw
    reads = {"happy": [event(1, 0x8, "a.txt") + event(1, 0x10, "a.txt") + event(1, 0x8, "a.txt")],
             "edge": [event(1, 0x40000100, "newdir") + event(1, 0x40000010, "sub") + event(1, 0x4000, "")
                      # The watch of sub ends (IN_IGNORED): its path is forgotten, later events carry "?".
                      + event(2, 0x8000, "") + event(2, 0x8, "late.txt")]}
    monkeypatch.setattr(s.os, "read", Mock(side_effect=[*reads.get(scenario, []), StopLoop()]))
    if scenario == "edge":
        (root / "newdir").mkdir()
    if scenario == "sad":
        s.file_logger([str(root)])
    else:
        with pytest.raises(StopLoop):
            s.file_logger([str(root)])
    records = [json.loads(line) for line in (tmp_path / "log/files.jsonl").read_text().splitlines()]
    if scenario == "happy":
        assert sorted(watched) == [str(root), str(root / "sub")]
        assert [(r["action"], r["path"]) for r in records] == [("write", str(root / "a.txt")), ("read", str(root / "a.txt"))]
    elif scenario == "edge":
        assert str(root / "newdir") in watched
        assert [r.get("action") for r in records] == ["create", None, "write"]
        assert "overflow" in records[1]["error"]
        assert records[2]["path"] == os.path.join("?", "late.txt")
    else:
        assert records == [{"time": records[0]["time"], "error": "inotify unavailable"}]


def test_file_logger_watches_what_it_walked_never_through_a_link(tmp_path, monkeypatch):
    """Each watch goes through the descriptor of the directory the walk opened (no link followed): a directory the
    agent swaps for a link to elsewhere, or a link inside the tree, never adds a watch outside it."""
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    (tmp_path / "log").mkdir()
    root, outside = tmp_path / "work", tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    (outside / "secret").mkdir(parents=True)
    (root / "link").symlink_to(outside, target_is_directory=True)
    libc, watched = Mock(), []
    libc.inotify_init1.return_value = 7
    libc.inotify_add_watch.side_effect = lambda fd, path, mask: watched.append((os.readlink(os.fsdecode(path)), mask)) \
        or len(watched)
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    monkeypatch.setattr(s.os, "read", Mock(side_effect=StopLoop()))
    with pytest.raises(StopLoop):
        s.file_logger([str(root)])
    assert sorted(path for path, _ in watched) == [str(root), str(root / "sub")]
    # The descriptor's link must be followed (no IN_DONT_FOLLOW), and only a directory is taken (IN_ONLYDIR).
    assert all(not mask & 0x02000000 and mask & 0x01000000 for _, mask in watched)


def test_file_logger_follows_a_deep_tree_only_so_far(tmp_path, monkeypatch):
    """A tree a thousand levels deep (built where nothing watches, then moved in) must not end the file log by a
    RecursionError: it is followed FILE_DEPTH levels deep."""
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(s.RUNTIME, "FILE_DEPTH", 20)
    (tmp_path / "log").mkdir()
    root = tmp_path / "work"
    deep = root
    for _ in range(40):
        deep = deep / "d"
    deep.mkdir(parents=True)
    libc, watched = Mock(), []
    libc.inotify_init1.return_value = 7
    libc.inotify_add_watch.side_effect = lambda fd, path, mask: watched.append(1) or len(watched)
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    monkeypatch.setattr(s.os, "read", Mock(side_effect=StopLoop()))
    with pytest.raises(StopLoop):
        s.file_logger([str(root)])
    assert 20 <= len(watched) <= 21


def test_watch_budget(scenario, tmp_path):
    rt = s.RUNTIME
    limits = tmp_path / "sys/fs/inotify"
    limits.mkdir(parents=True)
    if scenario == "happy":
        (limits / "max_user_watches").write_text("8192\n")
        assert rt.watch_budget(tmp_path) == 4096  # half of the user's: the rest for their own programs
    elif scenario == "sad":
        assert rt.watch_budget(tmp_path) == rt.FILE_WATCHES // 8  # unknown: a modest share
    else:
        (limits / "max_user_watches").write_text("1048576\n")
        assert rt.watch_budget(tmp_path) == rt.FILE_WATCHES


def test_file_logger_forgets_old_paths(tmp_path, monkeypatch):
    """The 2-second de-duplication keeps at most FILE_RECENT_LIMIT paths: a long run touches millions."""
    import struct
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    (tmp_path / "log").mkdir()
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(s.RUNTIME, "FILE_RECENT_LIMIT", 8)
    libc = Mock()
    libc.inotify_init1.return_value = 7
    libc.inotify_add_watch.return_value = 1
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    def event(name):
        raw = name.encode() + b"\0" * (16 - len(name))
        return struct.pack("iIII", 1, 0x8, 0, len(raw)) + raw
    batch = b"".join(event(f"f{i}.txt") for i in range(50)) + event("f0.txt")
    monkeypatch.setattr(s.os, "read", Mock(side_effect=[batch, StopLoop()]))
    with pytest.raises(StopLoop):
        s.file_logger([str(tmp_path / "work")])
    records = [json.loads(line) for line in (tmp_path / "log/files.jsonl").read_text().splitlines()]
    # Every distinct path is logged; f0.txt again within 2 s was forgotten under the limit and logged again.
    assert len(records) == 51 and records[-1]["path"].endswith("f0.txt")


def test_live_watches(scenario, tmp_path):
    proc = tmp_path / "proc"
    (proc / "self" / "fd").mkdir(parents=True)
    (proc / "self" / "fdinfo").mkdir()
    (proc / "self" / "fd" / "7").symlink_to("anon_inode:inotify" if scenario != "sad" else "/dev/null")
    (proc / "self" / "fdinfo" / "7").write_text(
        "pos:\t0\nflags:\t02000000\n"
        + ("inotify wd:1 ino:2 sdev:3 mask:4 ignored_mask:0\ninotify wd:1a ino:5 sdev:3 mask:4 ignored_mask:0\n"
           if scenario == "happy" else ""))
    if scenario == "happy":
        assert s.RUNTIME.live_watches(7, proc) == {1, 0x1a}  # hexadecimal, as the kernel writes them
    elif scenario == "sad":
        assert s.RUNTIME.live_watches(7, proc) is None  # not an inotify instance: nothing may be pruned
        assert s.RUNTIME.live_watches(8, proc) is None
    else:
        assert s.RUNTIME.live_watches(7, proc) == set()  # every watch gone


def test_file_logger_matches_the_kernels_watches_after_an_overflow(tmp_path):
    """Directories removed while the queue overflowed: their IN_IGNORED are lost, so their entries go when the table
    is matched with the kernel's watches; real inotify, a real overflow."""
    root = tmp_path / "work"
    for i in range(300):
        (root / f"d{i}" / "x").mkdir(parents=True)
    log = tmp_path / "log"
    log.mkdir()
    rt_module = s.RUNTIME
    stop = threading.Event()
    old = rt_module.LOG_DIR
    rt_module.LOG_DIR = log
    rt_module.MEMORY_PARTS.pop("file watches", None)
    watches = lambda: rt_module.MEMORY_PARTS.get("file watches", lambda: 0)()  # noqa: E731
    try:
        thread = threading.Thread(target=rt_module.file_logger, args=([str(root)], stop, 3600), daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while watches() < 601 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert watches() == 601
        # A real overflow, then the directories go: the logger is told by the queue's overflow event.
        # The logger held at its next line while the queue fills past the kernel's limit and the directories go.
        original, held = rt_module.log_event, threading.Event()
        rt_module.log_event = lambda *a: (held.wait(30), original(*a))
        for i in range(300):
            for j in range(30):
                (root / f"d{i}" / "x" / f"f{j}").write_text("x")
        for i in range(300):
            shutil.rmtree(root / f"d{i}")
        held.set()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and watches() > 1:
            time.sleep(0.2)
        rt_module.log_event = original
        records = (log / "files.jsonl").read_text()
        assert watches() == 1  # only the root is left, as in the kernel
        if "overflow" not in records:
            pytest.skip("this machine drained the queue in time: no overflow to recover from")
    finally:
        stop.set()
        rt_module.LOG_DIR = old


def test_file_logger_stops_when_the_supervisor_sheds_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(rt, "MEMORY_SHEDDERS", {})
    (tmp_path / "log").mkdir()
    (tmp_path / "work").mkdir()
    stop = threading.Event()
    thread = threading.Thread(target=rt.file_logger, args=([str(tmp_path / "work")], stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while "file log" not in rt.MEMORY_SHEDDERS and time.monotonic() < deadline:
        time.sleep(0.02)
    rt.MEMORY_SHEDDERS["file log"]()
    thread.join(5)
    assert not thread.is_alive() and not stop.is_set()  # ended by itself: the run goes on
    assert "ran short of memory" in (tmp_path / "log/files.jsonl").read_text()


def test_memory_shedder(scenario, monkeypatch):
    monkeypatch.setattr(rt, "MEMORY_SHEDDERS", {})
    calls = []
    if scenario == "happy":
        assert rt.memory_shedder("cache", lambda: calls.append(1)) is rt.MEMORY_SHEDDERS["cache"]
        rt.MEMORY_SHEDDERS["cache"]()
        assert calls == [1]
    elif scenario == "sad":
        assert rt.MEMORY_SHEDDERS == {}  # nothing registered: nothing to shed
    else:
        rt.memory_shedder("same", lambda: calls.append("old"))
        rt.memory_shedder("same", lambda: calls.append("new"))
        rt.MEMORY_SHEDDERS["same"]()
        assert calls == ["new"]


def test_memory_guard_sheds_once_from_its_threshold(tmp_path):
    rss, calls = [100 << 20], []
    guard = rt.MemoryGuard(tmp_path, rss=lambda: rss[0], trim=lambda: None, trace=False, shed_from=768 << 20,
                           shedders={"file log": lambda: calls.append("files"), "broken": lambda: 1 / 0})
    guard.check()
    assert calls == []
    rss[0] = 800 << 20
    guard.check()
    guard.check()
    assert calls == ["files"] and guard.shed == ["file log", "broken"]  # once each; a broken one does not stop it
    lines = [json.loads(line) for line in (tmp_path / rt.SUPERVISOR_MEMORY_FILE).read_text().splitlines()]
    assert {"shed": ["file log", "broken"]}.items() <= lines[0].items()


def test_oauth_fields(scenario):
    oauth = {"happy": {"accessToken": "a", "expiresAt": 5, "scopes": ["x"]},
             "sad": {"accessToken": 1, "expiresAt": True, "scopes": ["x", 2], "evil": "y"},
             "edge": {"expiresAt": 1.5, "refreshTokenExpiresAt": 0, "subscriptionType": "max"}}[scenario]
    fields = {**s.OAUTH_TOKEN_FIELDS, **s.OAUTH_INFO_FIELDS}
    expected = {"happy": oauth, "sad": {}, "edge": oauth}[scenario]
    assert s.oauth_fields(oauth, fields) == expected


def test_claude_oauth(scenario, tmp_path):
    path = tmp_path / "credentials.json"
    if scenario == "sad":
        path.write_text('{"claudeAiOauth": {"accessToken": "", "refreshToken": "x", "expiresAt": 1}}')
        assert s.claude_oauth(path) is None
        assert s.claude_oauth(tmp_path / "missing.json") is None
    else:
        claude_credentials(path, 2.5e12 if scenario == "edge" else 2000)
        assert s.claude_oauth(path)["refreshToken"] == "refresh-fixture"


def test_sync_claude_credentials(scenario, tmp_path):
    source = claude_credentials(tmp_path / "sandbox/.credentials.json", 3000, "rotated")
    target = tmp_path / "host/.credentials.json"
    if scenario == "sad":
        claude_credentials(target, 4000, "host-newer")
        assert s.sync_claude_credentials(source, target) is False
        assert s.claude_oauth(target)["refreshToken"] == "host-newer"
        assert s.sync_claude_credentials(tmp_path / "missing.json", target) is False
    else:
        claude_credentials(target, 1000, "stale")
        if scenario == "edge":
            document = json.loads(target.read_text())
            target.write_text(json.dumps({**document, "mcpOAuth": {"keep": True}}))
        assert s.sync_claude_credentials(source, target) is True
        document = json.loads(target.read_text())
        assert document["claudeAiOauth"]["refreshToken"] == "rotated"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert (document.get("mcpOAuth") == {"keep": True}) == (scenario == "edge")
        assert sorted(p.name for p in target.parent.iterdir()) == [".credentials.json"]


def test_claude_credential_pair(scenario, tmp_path, monkeypatch):
    home = tmp_path / "home"
    if scenario == "sad":
        assert s.claude_credential_pair(s.parse_args(["claude", "--no-auth"]), home) is None
        assert s.claude_credential_pair(s.parse_args(["codex"]), home) is None
    else:
        options = ["claude"] + (["--copy-auth"] if scenario == "edge" else [])
        if scenario == "edge":
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
        host, sandbox = s.claude_credential_pair(s.parse_args(options), home)
        expected = tmp_path / "custom" if scenario == "edge" else tmp_path / "host/.claude"
        assert host == expected / ".credentials.json"
        assert sandbox == home / ".claude/.credentials.json"


def test_nameservers(scenario):
    if scenario == "sad":
        assert s.nameservers(s.parse_args(["codex"])) == []
        assert s.nameservers(s.parse_args(["codex", "--offline"])) == []
    elif scenario == "happy":
        assert s.nameservers(s.parse_args(["codex", "--dns"])) == ["1.1.1.1", "8.8.8.8"]
        assert s.nameservers(s.parse_args(["codex", "--egress-all-allow"])) == ["1.1.1.1", "8.8.8.8"]
    else:
        args = s.parse_args(["codex", "--nameserver", "9.9.9.9", "--nameserver", "2620:fe::fe", "--nameserver", "9.9.9.9"])
        assert s.nameservers(args) == ["9.9.9.9", "2620:fe::fe"]


def test_resolver_files(scenario):
    options = {"happy": ["--dns"], "sad": [], "edge": ["--log-dir", "/tmp/contract-logs"]}[scenario]
    resolv, nsswitch = s.resolver_files(s.parse_args(["codex", *options]))
    if scenario == "happy":
        assert resolv == "nameserver 1.1.1.1\nnameserver 8.8.8.8\n"
    elif scenario == "edge":
        assert resolv == "nameserver 127.0.0.1\n"
    else:
        assert "nameserver" not in resolv
    assert nsswitch.endswith("hosts: files dns\n" if scenario != "sad" else "hosts: files\n")


def test_workspace_targets(scenario, tmp_path):
    main, lib = tmp_path / "main", tmp_path / "lib"
    if scenario == "sad":
        args = s.parse_args(["codex", "--workspace", str(main)])
        assert s.workspace_targets(args) == [(str(main), "/work")]
    elif scenario == "happy":
        args = s.parse_args(["codex", "--workspace", f"{main},{lib}"])
        assert s.workspace_targets(args) == [(str(main), "/work"), (str(lib), "/workspaces/lib")]
    else:
        other = tmp_path / "other" / "lib"
        args = s.parse_args(["codex", "--workspace", str(main), "--workspace", f"{lib},{other}", "--workspace", str(tmp_path / "x y")])
        assert s.workspace_targets(args) == [(str(main), "/work"), (str(lib), "/workspaces/lib"),
                                             (str(other), "/workspaces/lib-2"), (str(tmp_path / "x y"), "/workspaces/x y")]


def test_workspace_names(scenario, tmp_path):
    """--workspace-name NAME=DIR (what `sandbox-ai workspace add --name` keeps for a session): by resolved path."""
    main, lib = tmp_path / "main", tmp_path / "lib"
    if scenario == "happy":
        a = s.parse_args(["codex", "--workspace", f"{main},{lib}", "--workspace-name", f"docs={tmp_path}/./lib"])
        assert s.workspace_names(a) == {str(lib): "docs"}
        assert s.workspace_targets(a) == [(str(main), "/work"), (str(lib), "/workspaces/docs")]
    elif scenario == "sad":
        for value in ("docs", f"={lib}", f"../x={lib}", "docs="):
            with pytest.raises(ValueError, match="--workspace-name NAME=DIR"):
                s.workspace_names(s.parse_args(["codex", "--workspace-name", value]))
    else:  # the others go around a name (a kept lib-named one keeps "lib"): never two at one place
        other = tmp_path / "other" / "lib"
        a = s.parse_args(["codex", "--workspace", f"{main},{other},{lib}", "--workspace-name", f"lib={lib}"])
        assert s.workspace_targets(a) == [(str(main), "/work"), (str(other), "/workspaces/lib-2"),
                                          (str(lib), "/workspaces/lib")]
        assert s.workspace_names(s.parse_args(["codex"])) == {}


def test_check_private_dir(scenario, tmp_path):
    work = tmp_path / "work"
    args = s.parse_args(["codex", "--workspace", f"{work},{tmp_path / 'lib'}"])
    if scenario == "sad":
        for inside in (work / "logs", tmp_path / "lib", tmp_path):
            with pytest.raises(ValueError, match="--gui-vnc must lie outside"):
                s.check_private_dir(args, inside, "--gui-vnc")
    elif scenario == "edge":
        with pytest.raises(ValueError, match="control characters"):
            s.check_private_dir(args, tmp_path / "a,b", "--log-dir")
    else:
        s.check_private_dir(args, tmp_path / "private", "--log-dir")


def test_wayland_socket(scenario, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    if scenario == "sad":
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with pytest.raises(ValueError, match="WAYLAND_DISPLAY"):
            s.wayland_socket()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
        (runtime / "wayland-9").write_text("not a socket")
        with pytest.raises(ValueError, match="not found"):
            s.wayland_socket()
        return
    server = socket.socket(socket.AF_UNIX)
    path = runtime / "wayland-1" if scenario == "happy" else tmp_path / "abs-wayland"
    server.bind(str(path))
    try:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1" if scenario == "happy" else str(path))
        assert s.wayland_socket() == path
    finally:
        server.close()


def test_gui_config(scenario, tmp_path):
    options = {"happy": ["--gui"], "sad": [], "edge": ["--gui-vnc", str(tmp_path / "vnc"), "--gui-screen", "1280x720x16"]}[scenario]
    config = s.gui_config(s.parse_args(["codex", *options]))
    if scenario == "sad":
        assert config is None
    else:
        assert config == {"display": ":99", "screen": "1920x1080x24" if scenario == "happy" else "1280x720x16",
                          "vnc": scenario == "edge"}


def test_start_gui(scenario, tmp_path, monkeypatch):
    socket_path = tmp_path / "X99"
    monkeypatch.setattr(sr, "x_socket", lambda display: socket_path)
    started = []
    def popen(cmd, **kwargs):
        started.append(cmd)
        if cmd[-1] != "-quiet" and scenario != "sad":
            socket_path.write_text("")
        return Mock(poll=Mock(return_value=1 if scenario == "sad" else None))
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    stopped = []
    monkeypatch.setattr(sr, "stop_process", stopped.append)
    monkeypatch.setattr(s.time, "sleep", lambda _: None)
    waited = []
    monkeypatch.setattr(rt, "wait_for_unix_listener", lambda path, process, errors, name: waited.append((str(path), name)))
    config = {"display": ":99", "screen": "1920x1080x24", "vnc": scenario == "edge"}
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="Xvfb exited"):
            s.start_gui(config)
        assert len(stopped) == 1
        return
    processes = s.start_gui(config)
    assert len(processes) == (2 if scenario == "edge" else 1)
    drop = ["setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "--"]
    assert started[0] == drop + ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"]
    if scenario == "edge":
        assert started[1] == drop + ["x11vnc", "-display", ":99", "-unixsock", "/sandbox/vnc/vnc.sock",
                                     "-rfbport", "0", "-forever", "-shared", "-nopw", "-quiet", "-nosel"]
        assert waited == [("/sandbox/vnc/vnc.sock", "x11vnc")]  # started only once it listens


def test_x_socket(scenario):
    display = {"happy": ":99", "sad": ":0", "edge": ":1234"}[scenario]
    assert s.x_socket(display) == Path("/tmp/.X11-unix/X" + display[1:])


def test_auth_plan(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s, "auth_sources", lambda name: [(tmp_path / "primary.json", s.AUTH_TARGETS[name]),
                                                         (tmp_path / "meta.json", ".claude.json")])
    if scenario == "sad":
        monkeypatch.setattr(s, "auth_sources", Mock(side_effect=AssertionError("no discovery")))
        assert s.auth_plan(s.parse_args(["claude"])).startswith("automatic")
        assert s.auth_plan(s.parse_args(["claude", "--no-auth"])) == []
    elif scenario == "happy":
        assert s.auth_plan(s.parse_args(["claude", "--copy-auth"])) == [
            {"source": str(tmp_path / "primary.json"), "target": "/root/.claude/.credentials.json"}]
        (tmp_path / "meta.json").write_text("{}")
        assert len(s.auth_plan(s.parse_args(["claude", "--copy-auth"]))) == 2
    else:
        plan = s.auth_plan(s.parse_args(["opencode", "--copy", f"{tmp_path}/x.json:./.local/share/opencode/auth.json",
                                         "--copy", f"{tmp_path}/y:.config/other"]))
        assert plan == [{"source": f"{tmp_path}/x.json", "target": "/root/.local/share/opencode/auth.json"}]
        assert s.auth_plan(s.parse_args(["opencode", "--import-codex-auth", f"{tmp_path}/c.json"])) == [
            {"source": f"{tmp_path}/c.json", "target": "/root/.local/share/opencode/auth.json"}]


def test_tmux_command(scenario):
    import shlex
    command = {"happy": ["claude"], "sad": ["codex", "exec", "a b; rm -rf / $(x) && echo x"], "edge": ["opencode", "run", "it's"]}[scenario]
    cmd = s.tmux_command(command)
    assert cmd[:8] == ["tmux", "-u", "new-session", "-s", "sandbox", "-n", "agent", "-c"]
    assert shlex.split(cmd[9]) == command
    assert cmd[10:] == [";", "new-window", "-d", "-n", "shell", "-c", "/work", "bash"]
    # Detached sessions: the guard keeps running while the tmux session exists.
    detached = s.tmux_command(command, detached=True)
    assert detached[:2] == ["sh", "-c"]
    tmux_part, _, loop = detached[2].rpartition(" && ")
    assert shlex.split(tmux_part)[:4] == ["tmux", "-u", "new-session", "-d"]
    assert shlex.split(shlex.split(tmux_part)[10]) == command
    assert loop == "while tmux has-session -t sandbox 2>/dev/null; do sleep 1; done"
    # The relay that terminals attach through starts inside the same confinement.
    assert "--internal-attach-relay /sandbox/tmux/attach.sock" in tmux_part


def test_claude_token_status(scenario, tmp_path):
    path = tmp_path / "credentials.json"
    expires = {"happy": time.time() + 3600, "sad": 0, "edge": time.time() - 60}[scenario] * 1000
    claude_credentials(path, expires)
    status = s.claude_token_status(path)
    assert "access-fixture" not in status and "refresh-fixture" not in status
    assert status.startswith({"happy": "Claude access token valid", "sad": "Claude access token expired",
                              "edge": "Claude access token expired"}[scenario])


def test_load_sessions_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_sessions_module()
    else:
        module = s.load_sessions_module()
        assert callable(module.start) and callable(module.cli)
        if scenario == "edge":
            assert s.load_sessions_module() is not module


def test_tcp_targets(scenario):
    values = {"happy": ["10.0.0.5:11434"], "sad": ["10.0.0.5:99999"], "edge": ["[fe80::1]:1", "10.0.0.5:65535"]}[scenario]
    args = s.parse_args(["opencode", *[x for v in values for x in ("--allow-tcp", v)]])
    if scenario == "sad":
        with pytest.raises(ValueError, match="range"):
            s.tcp_targets(args)
    else:
        expected = {"happy": [("10.0.0.5", 11434)], "edge": [("fe80::1", 1), ("10.0.0.5", 65535)]}[scenario]
        assert s.tcp_targets(args) == expected


def test_host_ports(scenario):
    values = {"happy": ["3000"], "sad": ["0"], "edge": ["8080", "3000", "8080", "65535"]}[scenario]
    args = s.parse_args(["claude", *[x for v in values for x in ("--allow-host-port", v)]])
    if scenario == "sad":
        for bad in ["0", "65536", "-1", "3000,22", "3000:22", " 22", "２２", "1e3"]:
            args.allow_host_port = [bad]
            with pytest.raises(ValueError, match="allow-host-port"):
                s.host_ports(args)
    else:
        assert s.host_ports(args) == {"happy": [3000], "edge": [3000, 8080, 65535]}[scenario]


def test_pod_network(scenario):
    options = {"happy": ["--allow-host-port", "3000"], "sad": ["--offline"],
               "edge": ["--allow-host-port", "8080", "--allow-host-port", "3000"]}[scenario]
    network = s.pod_network(s.parse_args(["claude", *options]))
    if scenario == "sad":
        assert network == "none"
        assert s.pod_network(s.parse_args(["claude", "--network", "research"])) == "research"
        # No host ports: the default stays fully closed towards the host.
        assert s.pod_network(s.parse_args(["claude"])) == s.POD_NETWORK
        return
    expected = {"happy": "-T,3000", "edge": "-T,3000,-T,8080"}[scenario]
    assert network == f"pasta:--no-map-gw,-t,none,-u,none,{expected},-U,none"
    # Only namespace-to-host TCP forwards change; inbound and UDP stay off.
    assert "-t,none" in network and "-u,none" in network and "-U,none" in network and "-T,none" not in network


TOKEN = "github_pat_" + "A1b2" * 10


def test_github_token(scenario, monkeypatch):
    """A token for https://github.com opens the GitHub preset and becomes GH_TOKEN; the former
    --github-token-env/-file aliases are gone."""
    monkeypatch.setenv("CONTRACT_GH", TOKEN)
    if scenario == "happy":
        args = s.parse_args(["claude", "--git-token-env", "https://github.com=CONTRACT_GH"])
        assert "github" in args.presets
        assert args.allow_host == []  # the preset covers github.com, api.github.com and the download hosts
        env = s.git_environment(args)
        assert env["GH_TOKEN"] == TOKEN == env["SANDBOX_GIT_TOKEN_0"]
    elif scenario == "sad":
        for removed in (["--github-token-env", "CONTRACT_GH"], ["--github-token-file", "/x"]):
            with pytest.raises(SystemExit):
                s.parse_args(["claude", *removed])
        assert "--github-token" not in s.build_parser().format_help()
    else:
        # The same URL with and without a trailing slash counts once.
        args = s.parse_args(["claude", "--git-token-env", "https://github.com=CONTRACT_GH",
                             "--git-token-env", "https://github.com/=CONTRACT_GH"])
        assert len(s.git_tokens(args)) == 1


def test_network_profiles(scenario):
    if scenario == "happy":
        args = s.parse_args(["claude", "--profile", "development"])
        assert args.presets == ["pypi", "github"] and args.frontier
        assert "pypi.org" in s.allowed_hosts(args) and "api.openai.com" in s.allowed_hosts(args)
    elif scenario == "sad":
        # offline cannot be combined with destinations
        with pytest.raises(ValueError, match="offline"):
            s.validate(s.parse_args(["claude", "--no-auth", "--profile", "offline", "--profile", "development"]))
        # Git tokens need their server: a clear message instead of a generic conflict.
        with pytest.raises(ValueError, match="Git tokens need network"):
            s.validate(s.parse_args(["claude", "--no-auth", "--profile", "offline",
                                     "--git-token-env", "https://github.com=PATH"]))
    else:
        args = s.parse_args(["claude", "--profile", "egress-all", "--allow-pypi", "--profile", "development"])
        assert args.egress_all_allow and args.presets == ["pypi", "github"] and s.nameservers(args)


GIT_TOKEN = "0123456789abcdef" * 2 + "01234567"


def test_git_token_credential(scenario):
    if scenario == "happy":
        assert s.git_token_credential("http://alice@localhost:3080=GITEA") == {
            "url": "http://localhost:3080", "user": "alice", "host": "localhost", "port": 3080,
            "loopback": True, "env": "GITEA"}
        assert s.git_token_credential("https://Git.Example.org:443/=T")["url"] == "https://git.example.org"
    elif scenario == "sad":
        # Plain http leaves the host only on loopback; paths, quotes in the user and bad names are refused.
        for bad in ["http://git.example.org=T", "http://localhost:3080/owner=T", "http://localhost:3080",
                    "http://a'b@localhost:3080=T", "ssh://localhost:22=T", "http://localhost:70000=T",
                    "https://a..example.org=T", "http://localhost:3080=T=x"]:
            with pytest.raises(ValueError):
                s.git_token_credential(bad)
    else:
        credential = s.git_token_credential("https://[::1]:8443=T")
        assert credential["loopback"] and credential["port"] == 8443 and credential["user"] == "x-access-token"


def test_git_token_value(scenario, monkeypatch, tmp_path):
    """The token of one --git-token-* login: from the host variable, the file or the secret store, checked."""
    if scenario == "happy":
        monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
        credential = s.git_token_credential("https://git.example.org=CONTRACT_GIT", "env")
        assert s.git_token_value(credential, "env") == GIT_TOKEN
        store = Mock()
        store.get.return_value = GIT_TOKEN
        monkeypatch.setattr(s, "load_secret_store_module", lambda: store)
        assert s.git_token_value(s.git_token_credential("https://git.example.org=GIT_PUSH", "secret"), "secret") == GIT_TOKEN
    elif scenario == "sad":
        monkeypatch.delenv("CONTRACT_UNSET", raising=False)
        with pytest.raises(ValueError, match="Host variable is not set: CONTRACT_UNSET"):
            s.git_token_value(s.git_token_credential("https://git.example.org=CONTRACT_UNSET", "env"), "env")
        monkeypatch.setenv("CONTRACT_BAD", "two words")
        with pytest.raises(ValueError, match="does not look like an access token"):
            s.git_token_value(s.git_token_credential("https://git.example.org=CONTRACT_BAD", "env"), "env")
        store = Mock()
        store.get.return_value = None
        monkeypatch.setattr(s, "load_secret_store_module", lambda: store)
        with pytest.raises(ValueError, match="sandbox-ai secrets set GIT_PUSH"):
            s.git_token_value(s.git_token_credential("https://git.example.org=GIT_PUSH", "secret"), "secret")
    else:
        token = tmp_path / "token"
        token.write_text(GIT_TOKEN + "\n")
        token.chmod(0o600)
        assert s.git_token_value(s.git_token_credential(f"https://git.example.org={token}", "file"), "file") == GIT_TOKEN


def test_git_tokens(scenario, monkeypatch):
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    if scenario == "happy":
        args = s.parse_args(["claude", "--git-token-env", "http://localhost:3080=CONTRACT_GIT",
                             "--git-token-env", "https://git.example.org=CONTRACT_GIT"])
        # Loopback is reached through pasta's host forward, a remote host through its pinned name.
        assert args.allow_host_port == ["3080"] and args.allow_host == ["git.example.org"]
        assert [(c["url"], token) for c, token in s.git_tokens(args)] == [
            ("http://localhost:3080", GIT_TOKEN), ("https://git.example.org", GIT_TOKEN)]
        # The same entry from the command line and the project file counts once.
        twice = s.parse_args(["claude", "--git-token-env", "http://localhost:3080=CONTRACT_GIT",
                              "--git-token-env", "http://localhost:3080/=CONTRACT_GIT"])
        assert len(s.git_tokens(twice)) == 1
    elif scenario == "sad":
        monkeypatch.delenv("UNSET_CONTRACT_VARIABLE", raising=False)
        for options in [["http://localhost:3080=UNSET_CONTRACT_VARIABLE"],
                        ["http://localhost:3080=CONTRACT_GIT", "http://localhost:3080/=OTHER_GIT"]]:
            with pytest.raises(ValueError):
                s.git_tokens(s.parse_args(["claude", *(f"--git-token-env={o}" for o in options)]))
        with pytest.raises(ValueError, match="loopback"):
            s.git_tokens(s.parse_args(["claude", "--backend", "bwrap", "--git-token-env",
                                       "http://localhost:3080=CONTRACT_GIT"]))
    else:
        # Tokens that could add credential protocol fields are refused, and never echoed.
        for bad in [GIT_TOKEN + "\npassword=evil", GIT_TOKEN + " x", "short", "x" * 600]:
            monkeypatch.setenv("CONTRACT_GIT", bad)
            with pytest.raises(ValueError) as error:
                s.git_tokens(s.parse_args(["claude", "--git-token-env", "http://localhost:3080=CONTRACT_GIT"]))
            assert bad not in str(error.value)


def test_git_token_values(scenario):
    options = {"happy": ["--git-token-env", "http://localhost:3080=A", "--git-token-file", "https://h.example=/t"],
               "sad": [], "edge": ["--git-token-file", "http://localhost:3080=/x=y"]}[scenario]
    expected = {"happy": [("env", "http://localhost:3080=A"), ("file", "https://h.example=/t")], "sad": [],
                "edge": [("file", "http://localhost:3080=/x=y")]}[scenario]
    assert s.git_token_values(s.parse_args(["claude", *options])) == expected


def test_read_token_file(scenario, tmp_path):
    path = tmp_path / "token"
    if scenario == "happy":
        path.write_text(GIT_TOKEN + "\n")
        path.chmod(0o600)
        assert s.read_token_file(path, s.GIT_TOKEN, "--git-token-file") == GIT_TOKEN
    elif scenario == "sad":
        # Readable by others, a symlink, a directory, or not a token: refused without showing the content.
        path.write_text(GIT_TOKEN)
        path.chmod(0o644)
        with pytest.raises(ValueError, match="chmod 600"):
            s.read_token_file(path, s.GIT_TOKEN, "--git-token-file")
        (tmp_path / "link").symlink_to(path)
        with pytest.raises(ValueError, match="symlink"):
            s.read_token_file(tmp_path / "link", s.GIT_TOKEN, "--git-token-file")
        with pytest.raises(ValueError, match="not a regular file"):
            s.read_token_file(tmp_path, s.GIT_TOKEN, "--git-token-file")
        secret = tmp_path / "secret"
        # Assembled at run time, so that secret scanners do not take the fixture for a real key.
        secret.write_text("-----BEGIN " + "PRIVATE KEY-----\nabc\n")
        secret.chmod(0o600)
        with pytest.raises(ValueError) as error:
            s.read_token_file(secret, s.GIT_TOKEN, "--git-token-file")
        assert "PRIVATE" not in str(error.value)
    else:
        with pytest.raises(ValueError, match="cannot read"):
            s.read_token_file(tmp_path / "missing", s.GIT_TOKEN, "--git-token-file")
        path.write_bytes(b"x" * 5000)
        path.chmod(0o600)
        with pytest.raises(ValueError, match="does not hold a token"):
            s.read_token_file(path, s.GIT_TOKEN, "--git-token-file")


def test_token_files_in_launcher(scenario, tmp_path, monkeypatch):
    token = tmp_path / "private" / "t"
    token.parent.mkdir()
    token.write_text(GIT_TOKEN + "\n")
    token.chmod(0o600)
    work = tmp_path / "work"
    if scenario == "happy":
        gh = tmp_path / "private" / "gh"
        gh.write_text(TOKEN)
        gh.chmod(0o600)
        args = s.parse_args(["claude", "--workspace", str(work), "--git-token-file", f"http://localhost:3080={token}",
                             "--git-token-file", f"https://github.com={gh}"])
        s.validate(args)
        assert args.allow_host_port == ["3080"] and "github" in args.presets
        env = s.git_environment(args)
        assert env["SANDBOX_GIT_TOKEN_0"] == GIT_TOKEN and env["GH_TOKEN"] == TOKEN
    elif scenario == "sad":
        # A token file inside the workspace could be swapped by the agent for any host file.
        inside = work / "t"
        work.mkdir()
        inside.write_text(GIT_TOKEN)
        inside.chmod(0o600)
        with pytest.raises(ValueError, match="agent can change"):
            s.validate(s.parse_args(["claude", "--workspace", str(work),
                                     "--git-token-file", f"http://localhost:3080={inside}"]))
        monkeypatch.setenv("CONTRACT_GH", TOKEN)
        with pytest.raises(ValueError, match="given twice"):
            s.validate(s.parse_args(["claude", "--workspace", str(work),
                                     "--git-token-env", "https://github.com=CONTRACT_GH",
                                     "--git-token-file", f"https://github.com={token}"]))
    else:
        # The same URL from the command line and the project file, once as a file and once as a variable: refused.
        monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
        with pytest.raises(ValueError, match="given twice"):
            s.git_tokens(s.parse_args(["claude", "--git-token-file", f"http://localhost:3080={token}",
                                       "--git-token-env", "http://localhost:3080=CONTRACT_GIT"]))


def test_git_credential_helper(scenario):
    # Users from --git-credentials may hold any printable character: they reach the helper as a variable.
    user = {"happy": "alice", "sad": "x-access-token", "edge": "first.last@example.org'; echo pwned"}[scenario]
    helper = s.git_credential_helper("SANDBOX_GIT_USER_0", "SANDBOX_GIT_TOKEN_0")
    assert helper.startswith("!f() {") and '"$SANDBOX_GIT_USER_0"' in helper and '"$SANDBOX_GIT_TOKEN_0"' in helper
    if not shutil.which("sh"):
        pytest.skip("sh not installed")
    run = subprocess.run(["sh", "-c", helper[1:] + ' "$@"', "helper", "get" if scenario != "sad" else "store"],
                         env={"SANDBOX_GIT_TOKEN_0": GIT_TOKEN, "SANDBOX_GIT_USER_0": user},
                         capture_output=True, text=True)
    expected = "" if scenario == "sad" else f"username={user}\npassword={GIT_TOKEN}\n"
    assert run.stdout == expected


def test_read_private_file(scenario, tmp_path):
    path = tmp_path / "private"
    path.write_bytes(b"line one\nline two\n")
    path.chmod(0o600)
    if scenario == "happy":
        assert s.read_private_file(path, "--git-credentials", 100) == b"line one\nline two\n"
    elif scenario == "sad":
        path.chmod(0o640)
        with pytest.raises(ValueError, match="chmod 600") as error:
            s.read_private_file(path, "--git-credentials", 100)
        assert "line" not in str(error.value)
        (tmp_path / "link").symlink_to(path)
        with pytest.raises(ValueError, match="symlink"):
            s.read_private_file(tmp_path / "link", "--git-credentials", 100)
    else:
        # A longer file shows as LIMIT + 1 bytes, never more.
        assert s.read_private_file(path, "--git-credentials", 4) == b"line "


def test_git_server(scenario):
    if scenario == "happy":
        assert s.git_server("http://localhost:3080", "--x") == {
            "url": "http://localhost:3080", "user": None, "host": "localhost", "port": 3080, "loopback": True}
        assert s.git_server("https://alice@Git.Example.org:443/", "--x")["url"] == "https://git.example.org"
    elif scenario == "sad":
        for bad in ["http://git.example.org", "https://git.example.org/owner", "ftp://localhost", "https://a_b.org"]:
            with pytest.raises(ValueError, match="--x"):
                s.git_server(bad, "--x")
        # A password in the URL is refused, and the message does not repeat it.
        secret_url = "https://alice" + ":" + "hunter2" + "@" + "github.com"  # put together: no such URL in the source
        with pytest.raises(ValueError, match="no password in the URL") as refused:
            s.git_server(secret_url, "--x")
        assert "hunter2" not in str(refused.value) and "alice:" + "***" in str(refused.value)
    else:
        server = s.git_server("https://[::1]:8443", "--x")
        assert server["loopback"] and server["port"] == 8443 and server["url"] == "https://[::1]:8443"


def test_loopback_reachable(scenario):
    options = {"happy": [["--podman-url", "unix:///run/podman.sock"], []],
               "sad": [["--backend", "bwrap"], ["--network", "research"], ["--offline"]],
               "edge": [["--profile", "offline"]]}[scenario]
    expected = scenario == "happy"
    assert [s.loopback_reachable(s.parse_args(["claude", *o])) for o in options] == [expected] * len(options)


GIT_LOGIN = "p@ss w'rd\"$(id);\\%"  # every character the credential store percent-encodes and a shell would expand


def credential_line(user, password, server):
    quote = __import__("urllib.parse").parse.quote
    return f"{server.split('://')[0]}://{quote(user, safe='')}:{quote(password, safe='')}@{server.split('://')[1]}"


def test_git_credentials_line(scenario):
    if scenario == "happy":
        server, user, password = s.git_credentials_line(credential_line("alice", GIT_LOGIN, "http://localhost:3080"))
        assert (server["url"], user, password) == ("http://localhost:3080", "alice", GIT_LOGIN)
        # A path, as git stores it with credential.useHttpPath, does not narrow the server.
        server, user, _ = s.git_credentials_line("https://me%40example.org:tok@GitHub.com/owner/repo.git")
        assert server["url"] == "https://github.com" and user == "me@example.org"
    elif scenario == "sad":
        cases = {"no-url": "not a login", "ssh://u:secret@host": "not a login",
                 "https://u@host": "not a login", "https://:secret@host": "empty",
                 "https://u:se%0Acret@host": "control characters", "https://u:%FFsecret@host": "UTF-8",
                 "http://u:secret@git.example.org": "unencrypted"}
        for line, reason in cases.items():
            with pytest.raises(ValueError, match=reason) as error:
                s.git_credentials_line(line)
            assert "secret" not in str(error.value)
    else:
        server, user, password = s.git_credentials_line("http://u:%3A%40%2F@[::1]:3000")
        assert server["loopback"] and server["url"] == "http://[::1]:3000" and password == ":@/"
        with pytest.raises(ValueError, match="too long"):
            s.git_credentials_line("https://u:" + "x" * 5000 + "@host")


def credentials_file(directory, *lines, newline="\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "git-credentials"
    path.write_text(newline.join(lines) + newline)
    path.chmod(0o600)
    return path


def test_git_credentials_file(scenario, tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    if scenario == "happy":
        path = credentials_file(tmp_path / "private",
                                credential_line("x-access-token", TOKEN, "https://github.com"),
                                credential_line("alice", GIT_TOKEN, "http://localhost:3080"),
                                credential_line("me@example.org", GIT_LOGIN, "https://git.example.org"))
        args = s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(path)])
        s.validate(args)
        # Each server is reached as with --git-token-*: loopback forward, GitHub preset, pinned name.
        assert args.allow_host_port == ["3080"] and "github" in args.presets and args.allow_host == ["git.example.org"]
        entries, notes = s.git_credentials_file(args)
        assert notes == [] and [(c["url"], c["user"], c["line"], password) for c, password in entries] == [
            ("https://github.com", "x-access-token", 1, TOKEN), ("http://localhost:3080", "alice", 2, GIT_TOKEN),
            ("https://git.example.org", "me@example.org", 3, GIT_LOGIN)]
        assert s.git_tokens(args) == entries
        env = s.git_environment(args)
        assert env["GH_TOKEN"] == TOKEN and env["SANDBOX_GIT_USER_2"] == "me@example.org"
        # Logins reach only their own variables, never the git configuration.
        config = {k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")}
        assert not any(secret in json.dumps(config) for secret in (TOKEN, GIT_TOKEN, GIT_LOGIN, "me@example.org"))
    elif scenario == "sad":
        # Nothing usable: refused, with the line numbers and reasons but no secret.
        path = credentials_file(tmp_path / "private", "https://u:secret1@host.example:99999",
                                "http://u:secret2@git.example.org", "garbage-secret3")
        with pytest.raises(ValueError, match="no login the sandbox can use") as error:
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(path)]))
        assert "line 1" in str(error.value) and "line 3" in str(error.value) and "unencrypted" in str(error.value)
        assert "secret" not in str(error.value)
        # A loopback server needs the Podman default network.
        local = credentials_file(tmp_path / "local", credential_line("u", GIT_TOKEN, "http://localhost:3080"))
        with pytest.raises(ValueError, match="loopback"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--backend", "bwrap",
                                     "--git-credentials", str(local)]))
        with pytest.raises(ValueError, match="Git tokens need network"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--offline", "--git-credentials", str(local)]))
        # Inside a workspace the agent could swap the file; readable by others it is not private.
        inside = credentials_file(work, credential_line("u", GIT_TOKEN, "https://github.com"))
        with pytest.raises(ValueError, match="agent can change"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(inside)]))
        local.chmod(0o644)
        with pytest.raises(ValueError, match="chmod 600"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(local)]))
        # The MCP server sandboxes never get Git logins.
        assert "--git-credentials" in load_module("sandbox_mcp_contracts", "sandbox/mcp.py").forbidden()
    else:
        # CRLF, blank and malformed lines; the first line per server wins; a token option wins over the file;
        # notes name lines only, and the launcher prints them.
        monkeypatch.setenv("CONTRACT_GH", TOKEN)
        path = credentials_file(tmp_path / "private", "", "broken-secret",
                                credential_line("first", GIT_TOKEN, "http://localhost:3080"),
                                credential_line("second", "secret-second", "http://localhost:3080/other/repo"),
                                credential_line("x", "secret-gh", "https://github.com"), newline="\r\n")
        args = s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(path),
                             "--git-token-env", "https://github.com=CONTRACT_GH"])
        entries, notes = s.git_credentials_file(args)
        assert [(c["user"], password) for c, password in entries] == [("first", GIT_TOKEN)]
        assert [note.split(" skipped: ")[0].rsplit(" ", 1)[1] for note in notes] == ["2", "4", "5"]
        assert "earlier line" in notes[1] and "--git-token-*" in notes[2]
        assert not any("secret" in note or "second" in note for note in notes)
        assert [(c["url"], token) for c, token in s.git_tokens(args)] == [
            ("https://github.com", TOKEN), ("http://localhost:3080", GIT_TOKEN)]
        monkeypatch.setattr(s, "load_project_config", lambda a: (None, None))
        monkeypatch.setattr(s, "check_login", lambda a: None)
        assert s.launch(["claude", "--workspace", str(work), "--no-auth", "--dry-run", "--git-credentials", str(path),
                         "--git-token-env", "https://github.com=CONTRACT_GH"]) in (0, None)
        out, err = capsys.readouterr()
        plan = json.loads(out)
        assert plan["git_credentials"] == [
            {"url": "https://github.com", "user": "x-access-token", "env": "CONTRACT_GH"},
            {"url": "http://localhost:3080", "user": "first", "credentials": str(path), "line": 3}]
        assert plan["host_ports"] == [3080] and f"{path} line 4 skipped" in err
        assert not any(secret in out + err for secret in (TOKEN, GIT_TOKEN, "secret-second", "secret-gh", "broken-secret"))


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_git_credentials_real_git(tmp_path):
    """Real git against a Git server on loopback that demands the login (HTTP Basic, git-http-backend):
    with only the launcher's GIT_CONFIG_* variables and a planted helper in HOME, clone and push work
    with the right login and fail with a wrong one."""
    import base64 as b64
    import http.server
    import threading
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    backend = Path(subprocess.run([git, "--exec-path"], capture_output=True, text=True).stdout.strip()) / "git-http-backend"
    if not backend.exists():
        pytest.skip("git-http-backend not installed")
    root = tmp_path / "srv"
    subprocess.run([git, "init", "--bare", "-q", "-b", "main", str(root / "repo.git")], check=True)
    expected = "Basic " + b64.b64encode(f"me@example.org:{GIT_LOGIN}".encode()).decode()
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_backend(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            env = {"GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
                   "QUERY_STRING": query, "REQUEST_METHOD": self.command, "REMOTE_USER": "me",
                   "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                   "PATH": os.environ["PATH"]}
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            out = subprocess.run([str(backend)], input=body, env=env, capture_output=True).stdout
            head, _, payload = out.partition(b"\r\n\r\n")
            status = 200
            headers = []
            for line in head.decode().split("\r\n"):
                name, _, value = line.partition(": ")
                if name.lower() == "status":
                    status = int(value.split()[0])
                elif name:
                    headers.append((name, value))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = handle_backend

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        def attempt(password, name):
            path = credentials_file(tmp_path / name, credential_line("me@example.org", password,
                                                                     f"http://localhost:{port}"))
            args = s.parse_args(["claude", "--workspace", str(tmp_path / "work"), "--git-credentials", str(path)])
            s.validate(args)
            assert args.allow_host_port == [str(port)]
            home = tmp_path / f"home-{name}"
            home.mkdir()
            # A helper the agent planted in the sandbox HOME is dropped for this server.
            (home / ".gitconfig").write_text("[credential]\n\thelper = !echo username=evil; echo password=planted #\n")
            env = {"HOME": str(home), "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@example.org", "GIT_COMMITTER_NAME": "A",
                   "GIT_COMMITTER_EMAIL": "a@example.org", **s.git_environment(args)}
            clone = home / "clone"
            url = f"http://localhost:{port}/repo.git"
            result = subprocess.run([git, "clone", "-q", url, str(clone)], env=env, capture_output=True, text=True)
            if result.returncode:
                return result
            (clone / "file.txt").write_text(name)
            subprocess.run([git, "-C", str(clone), "add", "file.txt"], env=env, check=True)
            subprocess.run([git, "-C", str(clone), "commit", "-qm", name], env=env, check=True)
            return subprocess.run([git, "-C", str(clone), "push", "-q", "origin", "HEAD:main"],
                                  env=env, capture_output=True, text=True)

        good = attempt(GIT_LOGIN, "good")
        assert good.returncode == 0, good.stderr
        log = subprocess.run([git, "--git-dir", str(root / "repo.git"), "log", "--format=%s", "main"],
                             capture_output=True, text=True).stdout
        assert log.split() == ["good"]
        assert expected in seen and not any("planted" in b64.b64decode(h.split()[1]).decode()
                                            for h in seen if h)
        bad = attempt("wrong", "bad")
        assert bad.returncode != 0 and "Authentication failed" in bad.stderr
    finally:
        server.shutdown()


def test_host_git_identity(scenario, monkeypatch):
    answers = {"happy": {"user.name": "Ada Lovelace\n", "user.email": "ada@example.org\n"},
               "sad": {"user.name": "Ada\n"},
               "edge": {"user.name": "Ada\x1b]0;pwn\x07\n", "user.email": "ada@example.org\n"}}[scenario]

    def run(cmd, **kwargs):
        # Global scope only, from HOME, without GIT_* overrides: never the workspace's .git/config.
        assert cmd[:4] == ["git", "config", "--global", "--get"]
        assert kwargs["cwd"] == Path.home() and not any(k.startswith("GIT_") for k in kwargs["env"])
        value = answers.get(cmd[4])
        return Mock(returncode=0 if value else 1, stdout=value or "")
    monkeypatch.setattr(s.subprocess, "run", run)
    if scenario == "happy":
        assert s.host_git_identity() == ("Ada Lovelace", "ada@example.org")
    else:
        # Missing values and control characters (terminal escapes) are refused.
        with pytest.raises(ValueError, match="user.email" if scenario == "sad" else "user.name"):
            s.host_git_identity()


def test_git_environment(scenario, monkeypatch, tmp_path):
    monkeypatch.setenv("CONTRACT_GH", TOKEN)
    monkeypatch.setattr(s, "host_git_identity", lambda: ("Ada", "ada@example.org"))
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    options = {"happy": ["--git-identity", "--git-token-env", "https://github.com=CONTRACT_GH"], "sad": [],
               "edge": ["--git-token-env", "https://github.com=CONTRACT_GH",
                        "--git-token-env", "http://alice@localhost:3080=CONTRACT_GIT"]}[scenario]
    env = s.git_environment(s.parse_args(["claude", *options]))
    if scenario == "sad":
        # The helper for logins added during the run, and the privacy hooks unless they are switched off.
        helper = {"GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": s.RUNTIME_GIT_HELPER}
        assert env == {"GIT_CONFIG_COUNT": "2", **helper, "GIT_CONFIG_KEY_1": "core.hooksPath",
                       "GIT_CONFIG_VALUE_1": s.PRIVACY_HOOKS}
        assert s.git_environment(s.parse_args(["claude", "--privacy-check", "off"])) == {"GIT_CONFIG_COUNT": "1", **helper}
        # With --token-proxy the tokens stay outside the sandbox: no helper (net allow refuses logins then).
        assert s.git_environment(s.parse_args(["claude", "--privacy-check", "off", "--token-proxy"])) == {}
        return
    count = int(env["GIT_CONFIG_COUNT"])
    assert (env[f"GIT_CONFIG_KEY_{count - 1}"], env[f"GIT_CONFIG_VALUE_{count - 1}"]) == \
        ("core.hooksPath", s.PRIVACY_HOOKS)
    if scenario == "happy":
        assert env["GIT_CONFIG_KEY_0"] == "user.name" and env["GIT_CONFIG_VALUE_0"] == "Ada"
        assert env["GIT_CONFIG_KEY_1"] == "user.email" and env["GIT_CONFIG_VALUE_1"] == "ada@example.org"
    assert env["GH_TOKEN"] == TOKEN == env["SANDBOX_GIT_TOKEN_0"]
    assert TOKEN not in json.dumps({k: v for k, v in env.items() if k not in ("GH_TOKEN", "SANDBOX_GIT_TOKEN_0")})
    if not shutil.which("git"):
        pytest.skip("git not installed")
    # Real git: a helper the agent planted in ~/.gitconfig is dropped for github.com,
    # the token answers only for https://github.com and nothing else.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = !echo password=planted #\n")
    run_env = {"HOME": str(home), "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0", **env}

    def fill(url):
        return subprocess.run(["git", "credential", "fill"], input=f"url={url}\n\n", env=run_env,
                              capture_output=True, text=True).stdout
    answer = fill("https://github.com/owner/repo.git")
    assert f"password={TOKEN}" in answer and "username=x-access-token" in answer and "planted" not in answer
    for other in ["https://github.com.evil.example/x.git", "http://github.com/x.git", "https://gitlab.com/x.git"]:
        assert TOKEN not in fill(other)
    local = fill("http://localhost:3080/alice/sandbox-ai.git")
    if scenario == "edge":
        assert env["SANDBOX_GIT_TOKEN_1"] == GIT_TOKEN and GIT_TOKEN not in json.dumps(
            {k: v for k, v in env.items() if k != "SANDBOX_GIT_TOKEN_1"})
        assert f"password={GIT_TOKEN}" in local and "username=alice" in local and "planted" not in local
        for other in ["http://localhost:3001/x.git", "http://127.0.0.1:3080/x.git", "https://localhost:3080/x.git"]:
            assert GIT_TOKEN not in fill(other)
    else:
        assert GIT_TOKEN not in local


def test_config_store(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert s.config_store() == Path.home() / ".config/sandbox-ai/trusted.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ("x" if scenario == "happy" else "with space")))
        assert s.config_store().parent.parent == tmp_path / ("x" if scenario == "happy" else "with space")


def test_trusted_configs(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = tmp_path / "sandbox-ai/trusted.json"
    digest = "a" * 64
    if scenario != "sad":
        store.parent.mkdir(parents=True)
        store.write_text(json.dumps({"/p/.sandbox-ai.json": digest}) if scenario == "happy" else "not json")
    assert s.trusted_configs() == ({"/p/.sandbox-ai.json": digest} if scenario == "happy" else {})
    if scenario == "edge":  # a damaged store: no object, or damaged entries, is never a crash in a caller
        said = []
        store.write_text(json.dumps(["/p/.sandbox-ai.json"]))
        assert s.trusted_configs(said.append) == {} and "damaged" in said[-1]
        store.write_text(json.dumps({"/p/.sandbox-ai.json": digest, "/q/x.json": "short", "relative": digest,
                                     "/r/y.json": None}))
        assert s.trusted_configs(said.append) == {"/p/.sandbox-ai.json": digest}
        assert sum("damaged entry" in line for line in said) == 3


def test_trust_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / ".sandbox-ai.json"
    if scenario == "sad":
        with pytest.raises(OSError):
            s.trust_config(path)
        return
    path.write_text("{}")
    s.trust_config(path)
    if scenario == "edge":
        path.write_text('{"changed": 1}')
        s.trust_config(path)
    import hashlib
    assert s.trusted_configs() == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}
    assert (tmp_path / "cfg/sandbox-ai").stat().st_mode & 0o777 == 0o700


def test_load_project_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.with_name(tmp_path.name + "-cfg")))  # outside the workspace
    path = tmp_path / ".sandbox-ai.json"
    args = s.parse_args(["codex", "--workspace", str(tmp_path)])
    if scenario == "sad":
        assert s.load_project_config(args) == (None, None)
        with pytest.raises(ValueError, match="not found"):
            s.load_project_config(s.parse_args(["codex", "--config", str(tmp_path / "missing.json")]))
        return
    document = {"version": 1, "agent": "codex", "options": ["--dns"]}
    path.write_text(json.dumps(document))
    s.trust_config(path)
    if scenario == "edge":
        assert s.load_project_config(s.parse_args(["codex", "--workspace", str(tmp_path), "--no-config"])) == (None, None)
    else:
        assert s.load_project_config(args) == (path, document)


def test_merge_config(scenario):
    document = {"options": ["--dns", "--log-dir", "/l"], "agent_args": ["run"], "workspaces": ["/extra"]}
    argv = {"happy": ["codex"], "sad": ["codex", "--workspace", "/main", "--", "own"],
            "edge": ["codex", "--workspace=/main,/extra", "--log-dir", "/mine"]}[scenario]
    args = s.parse_args(argv)
    merged = s.merge_config(argv, args, document)
    expected_workspaces = {"happy": f"{os.getcwd()},/extra", "sad": "/main,/extra", "edge": "/main,/extra"}[scenario]
    assert merged[merged.index("--workspace") + 1] == expected_workspaces
    assert merged[merged.index("--") + 1:] == (["own"] if scenario == "sad" else ["run"])
    final = s.parse_args(merged)
    assert final.log_dir == Path("/mine" if scenario == "edge" else "/l")
    assert final.workspaces[0] == ("/main" if scenario != "happy" else os.getcwd())


def test_merge_config_credential_copy(scenario, tmp_path):
    """A --copy of a login file is an auth choice: the command line's replaces the project file's."""
    notes = tmp_path / "notes.txt"
    credential = f"{Path.home()}/.claude/.credentials.json:.claude/.credentials.json"
    document = {"options": ["--copy", f"{notes}:notes.txt", "--copy", credential, "--dns"]}
    cli = {"happy": ["--no-auth"], "sad": ["--copy", f"{notes}:other.txt"],
           "edge": ["--copy=/secrets/auth.json:.codex/auth.json"]}[scenario]
    (tmp_path / "project").mkdir()
    argv = ["claude", "--workspace", str(tmp_path / "project"), *cli]
    final = s.parse_args(s.merge_config(argv, s.parse_args(argv), document))
    assert final.dns is True
    if scenario == "sad":  # a plain file on the command line leaves the file's login alone
        assert final.copy == [f"{notes}:notes.txt", credential, f"{notes}:other.txt"]
    else:
        assert credential not in final.copy and f"{notes}:notes.txt" in final.copy
        if scenario == "happy":
            s.validate(final)  # no "--no-auth conflicts with --copy of a credential file"


def test_copies_credential(scenario, tmp_path):
    if scenario == "happy":
        assert s.copies_credential("/anywhere/creds:.claude/.credentials.json")
        assert s.copies_credential("/backup/auth.json:x.json")
        assert s.copies_credential(f"{Path.home()}/.claude.json:state.json")
    elif scenario == "sad":
        assert not s.copies_credential("/etc/gitconfig:.gitconfig")
        assert not s.copies_credential("no-separator")
    else:  # another name or a symlink for a login file counts as well
        (Path.home() / ".codex").mkdir()
        (Path.home() / ".codex/auth.json").write_text("{}")
        (tmp_path / "link").symlink_to(Path.home() / ".codex/auth.json")
        assert s.copies_credential(f"{tmp_path}/link:token")
        assert s.copies_credential("/x:./.codex/../.codex/auth.json")


def test_auth_choices(scenario):
    if scenario == "happy":
        assert s.auth_choices(["--dns", "--auth-file", "/a", "--copy-auth"]) == {1, 2, 3}
    elif scenario == "sad":
        assert s.auth_choices(["--copy", "/etc/hosts:hosts", "--log-dir", "--copy-auth"]) == set()
    else:
        assert s.auth_choices(["--copy=/b/auth.json:a", "--copy", "/c:.codex/auth.json", "--copy"]) == {0, 1, 2}


def test_claude_default_settings(scenario, tmp_path):
    home = tmp_path / "home"
    settings = home / ".claude/settings.json"
    if scenario == "sad":
        settings.parent.mkdir(parents=True)
        settings.write_text("{broken")
        s.claude_default_settings(home)
        assert settings.read_text() == "{broken"
        return
    if scenario == "edge":
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "sonnet", "theme": "dark",
                                        "modelSettings": {"claude-opus-5-5": {"effortLevel": "max"}}}))
    s.claude_default_settings(home)
    document = json.loads(settings.read_text())
    if scenario == "happy":
        assert document == {"model": "claude-opus-5-5", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
        assert settings.stat().st_mode & 0o777 == 0o600
    else:
        # Existing choices win; only missing keys are added.
        assert document["model"] == "sonnet" and document["theme"] == "dark"
        assert document["modelSettings"]["claude-opus-5-5"]["effortLevel"] == "max"


def test_state_dir_lock(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    lock = home / s.STATE_LOCK
    if scenario == "happy":
        with s.state_dir_lock(home) as fd:
            assert lock.read_text() == f"{os.getpid()}\n" and stat.S_IMODE(lock.stat().st_mode) == 0o600
            # A second run on the same directory stops at once, naming the directory and the holder.
            with pytest.raises(ValueError, match=f"--state-dir {home} is in use by another run \\(PID {os.getpid()}\\)"):
                with s.state_dir_lock(home):
                    pass
            # Not inherited: no child (the sandbox, the agent) holds the lock.
            assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            children = subprocess.run(["ls", "/proc/self/fd"], close_fds=False, capture_output=True, text=True)
            assert str(fd) not in children.stdout.split()
        with s.state_dir_lock(home):  # released with the run; the stale PID is overwritten
            assert lock.read_text() == f"{os.getpid()}\n"
    elif scenario == "sad":
        # The agent can write the HOME: a planted symlink or hard link never makes us write elsewhere.
        target = tmp_path / "target"
        target.write_text("keep")
        lock.symlink_to(target)
        with pytest.raises(ValueError, match="cannot open its lock file"):
            with s.state_dir_lock(home):
                pass
        lock.unlink()
        os.link(target, lock)
        with pytest.raises(ValueError, match="not a private regular file"):
            with s.state_dir_lock(home):
                pass
        assert target.read_text() == "keep"
    else:
        os.mkfifo(lock)  # never blocks on a FIFO
        with pytest.raises(ValueError, match="not a private regular file"):
            with s.state_dir_lock(home):
                pass


def test_update_json_defaults(scenario, tmp_path):
    home = tmp_path / "home"
    target = home / "sub/state.json"
    target.parent.mkdir(parents=True)

    def add_k(document):
        changed = "k" not in document
        document.setdefault("k", 1)
        return changed

    if scenario == "sad":
        # A planted symlink is refused, never followed or replaced.
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        target.symlink_to(outside)
        with pytest.raises(ValueError):
            s.update_json_defaults(home, "sub/state.json", add_k)
        assert outside.read_text() == "{}" and target.is_symlink()
        return
    if scenario == "edge":
        target.write_text("[1, 2]")  # Valid JSON but not an object: untouched, with a reason.
        assert "JSON object" in s.update_json_defaults(home, "sub/state.json", add_k)
        assert target.read_text() == "[1, 2]"
        big = json.dumps({"pad": "x" * 64})
        target.write_text(big)  # Over the given limit: refused, file kept, reason names the limit.
        assert "16 bytes" in s.update_json_defaults(home, "sub/state.json", add_k, limit=16)
        assert target.read_text() == big
        # Nothing to add: the bytes and the inode stay, so concurrent writers keep their data.
        original = '{"k": 2,   "x": "\\u00e4"}'
        target.write_text(original)
        inode = target.stat().st_ino
        assert s.update_json_defaults(home, "sub/state.json", add_k) is None
        assert target.read_text() == original and target.stat().st_ino == inode
        return
    assert s.update_json_defaults(home, "sub/state.json", add_k) is None
    assert json.loads(target.read_text()) == {"k": 1}
    assert target.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]
    # Non-ASCII text is kept as written, not turned into escapes.
    target.write_text(json.dumps({"projects": {"/home/jörg": {}}}, ensure_ascii=False), encoding="utf-8")
    assert s.update_json_defaults(home, "sub/state.json", add_k) is None
    assert "/home/jörg" in target.read_text(encoding="utf-8")


def test_claude_default_state(scenario, tmp_path):
    home = tmp_path / "home"
    state = home / ".claude.json"
    credentials = home / ".claude/.credentials.json"
    credentials.parent.mkdir(parents=True)
    oauth = {"accessToken": "a", "refreshToken": "r", "expiresAt": int(time.time() * 1000) + 3600_000}
    if scenario == "sad":
        # No usable login: leave onboarding (and its login step) to Claude.
        credentials.write_text("{}")
        s.claude_default_state(home)
        assert not state.exists()
        # A planted ~/.claude symlink is refused before anything is read through it.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        shutil.rmtree(home / ".claude")
        (home / ".claude").symlink_to(outside)
        with pytest.raises(ValueError):
            s.claude_default_state(home)
        assert not state.exists()
        return
    if scenario == "edge":
        # A refresh token past its expiry cannot log in: keep onboarding's login step.
        expired = {**oauth, "refreshTokenExpiresAt": int(time.time() * 1000) - 1000}
        credentials.write_text(json.dumps({"claudeAiOauth": expired}))
        s.claude_default_state(home)
        assert not state.exists()
    credentials.write_text(json.dumps({"claudeAiOauth": oauth}))
    if scenario == "edge":
        # Real host files grow past CONFIG_LIMIT through project history.
        projects = {f"/p{i}": {"history": ["x" * 100] * 10} for i in range(s.CONFIG_LIMIT // 1000 + 1)}
        state.write_text(json.dumps({"theme": "light", "hasCompletedOnboarding": False,
                                     "oauthAccount": {"emailAddress": "x"}, "projects": projects}))
        assert state.stat().st_size > s.CONFIG_LIMIT
    s.claude_default_state(home)
    document = json.loads(state.read_text())
    if scenario == "happy":
        assert document == {"hasCompletedOnboarding": True, "theme": "dark"}
        assert state.stat().st_mode & 0o777 == 0o600
    else:
        assert document["hasCompletedOnboarding"] is True and document["theme"] == "light"
        assert document["oauthAccount"] == {"emailAddress": "x"} and document["projects"] == projects


def test_printable(scenario):
    text = {"happy": "plain ä text\n\tok", "sad": "\x1b]52;c;cGF5\x07\r", "edge": "a‮b\x9bc"}[scenario]
    expected = {"happy": text, "sad": "\\x1b]52;c;cGF5\\x07\\x0d", "edge": "a\\u202eb\\x9bc"}[scenario]
    assert s.printable(text) == expected


def exposure_args(tmp_path, *options):
    for name in ("work", "rw", "ro", "state", "outside"):
        (tmp_path / name).mkdir(exist_ok=True)
    return s.parse_args(["opencode", "--workspace", str(tmp_path / "work"), "--mount", f"{tmp_path / 'rw'}:/mnt/rw:rw",
                         "--mount", f"{tmp_path / 'ro'}:/mnt/ro:ro", "--state-dir", str(tmp_path / "state"), *options])


def test_exposed_trees(scenario, tmp_path):
    options = {"happy": [], "sad": [], "edge": []}[scenario]
    args = exposure_args(tmp_path, *options)
    if scenario == "sad":
        args.state_dir = None
    trees = s.exposed_trees(args)
    expected = [tmp_path / "work", tmp_path / "rw"] + ([] if scenario == "sad" else [tmp_path / "state"])
    assert trees == expected  # read-only mounts are not writable by the agent
    assert tmp_path / "ro" not in trees


def test_check_host_source(scenario, tmp_path):
    args = exposure_args(tmp_path)
    (tmp_path / "outside/config.json").write_text("{}")
    if scenario == "happy":
        s.check_host_source(args, tmp_path / "outside/config.json", "--local-config")
        return
    if scenario == "sad":
        # Inside the workspace, the rw mount or the persistent HOME: refused even before it exists.
        for path in [tmp_path / "work/cfg.json", tmp_path / "rw/x", tmp_path / "state/.ssh/id",
                     tmp_path / "outside/../work/cfg.json"]:
            with pytest.raises(ValueError, match="agent can change"):
                s.check_host_source(args, path, "--copy")
        return
    # A host symlink outside that leads into the workspace is refused by its target.
    (tmp_path / "outside/link.json").symlink_to(tmp_path / "work/cfg.json")
    with pytest.raises(ValueError, match="agent can change"):
        s.check_host_source(args, tmp_path / "outside/link.json", "--auth-file")


def test_check_host_sources(scenario, tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/auth.json").write_text("{}")
    if scenario == "happy":
        s.validate(exposure_args(tmp_path, "--auth-file", str(tmp_path / "outside/auth.json")))
        return
    if scenario == "sad":
        (tmp_path / "work").mkdir()
        (tmp_path / "work/data").mkdir()
        cases = [["--local-config", str(tmp_path / "work/x.json")],
                 ["--copy", f"{tmp_path / 'work/x.json'}:.config/x"],
                 ["--mount", f"{tmp_path / 'work/data'}:/mnt/data:ro"],
                 ["--mount", f"{tmp_path / 'rw'}:/mnt/again:ro"]]
        (tmp_path / "work/x.json").write_text("{}")
        for options in cases:
            with pytest.raises(ValueError, match="agent can change"):
                s.validate(exposure_args(tmp_path, *options))
        # --log-dir and --gui-vnc (bound rw) never in sandbox-ai's own folders; a private location of yours (~/.ssh)
        # is your choice: given, with a warning.
        for option in ("--log-dir", "--gui-vnc"):
            # (the runs' folder holds their control sockets: refused as a folder of sockets first)
            with pytest.raises(ValueError, match=f"{option} .*(sandbox-ai's own folder|sandbox-ai's runs)"):
                s.validate(exposure_args(tmp_path, option, str(Path.home() / ".local/state/sandbox-ai/runs/x")))
            with pytest.raises(ValueError, match=f"{option} .*sandbox-ai's own folder"):
                s.validate(exposure_args(tmp_path, option, str(Path.home() / ".local/state/sandbox-ai/sessions")))
        return
    # The persistent HOME must not lie in a workspace, where it could be committed or swapped.
    (tmp_path / "work").mkdir()
    (tmp_path / "work/.state").mkdir(mode=0o700)
    with pytest.raises(ValueError, match="agent can change"):
        s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "work"),
                                 "--state-dir", str(tmp_path / "work/.state")]))


def test_watch_added_workspace(scenario, tmp_path, monkeypatch):
    rt = s.RUNTIME
    monkeypatch.setattr(rt, "FILE_ROOTS_ADDED", [])
    monkeypatch.setattr(rt, "FILE_LOGGING", threading.Event())
    real = tmp_path / "lib"
    real.mkdir()
    monkeypatch.setattr(rt.os.path, "isdir", lambda path: path in ("/workspaces/lib", str(real)))
    if scenario == "happy":
        assert rt.watch_added_workspace("/workspaces/lib") is False  # no file log in this run
        rt.FILE_LOGGING.set()
        assert rt.watch_added_workspace("/workspaces/lib") is True
        assert rt.FILE_ROOTS_ADDED == ["/workspaces/lib"]
    elif scenario == "sad":
        # Only the place an added workspace has: never another directory, nothing with a slash or "..".
        for value in ["/root", "/workspaces/a/b", "/workspaces/..", "/workspaces/", None, str(real)]:
            with pytest.raises(ValueError, match="watch"):
                rt.watch_added_workspace(value)
        assert rt.FILE_ROOTS_ADDED == []
    else:
        with pytest.raises(ValueError, match="no directory"):
            rt.watch_added_workspace("/workspaces/missing")


def test_host_sources(scenario, tmp_path):
    (tmp_path / "outside/auth.json").parent.mkdir(exist_ok=True)
    options = {"happy": [], "sad": ["--auth-file", str(tmp_path / "outside/auth.json")],
               "edge": ["--local-config", str(tmp_path / "outside/local.json")]}[scenario]
    found = s.host_sources(exposure_args(tmp_path, *options))
    labels = [label for _, label in found]
    # Always the run directory and your rules: a workspace added during the run is held to them as well.
    assert any(label.startswith("The run directory") for label in labels) and "Your rules (XDG_CONFIG_HOME)" in labels
    if scenario == "sad":
        assert (str(tmp_path / "outside/auth.json"), "--auth-file") in [(str(p), l) for p, l in found]
    if scenario == "edge":
        assert "--local-config" in labels


def test_host_stores(scenario, tmp_path, monkeypatch):
    """Security finding M2: the stores are protected whatever the start's agent and kit options (every sandbox, or
    the next start of another agent, reads them); only the plugin installer's own start may hold the plugin store."""
    options = {"happy": [], "sad": ["--no-default-kit", "--no-memory", "--no-plugins"], "edge": []}[scenario]
    args = exposure_args(tmp_path, *options)
    stores = [label for _, label in s.host_stores(args)]
    for label in ("the default kit of claude", "the default kit of opencode", "the kits directory",
                  "the project memory", "the plugin store"):
        assert label in stores, label
    if scenario == "edge":
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        store = tmp_path / "data" / "sandbox-ai" / "plugins" / "claude"
        store.mkdir(parents=True)
        installer = s.parse_args(["claude", "--no-plugins", "--state-dir", str(store), "--workspace",
                                  str(tmp_path / "work")])
        held = [str(path) for path, _ in s.host_stores(installer)]
        assert str(store) not in held, "the installer's own start writes its store"
        other = s.parse_args(["claude", "--state-dir", str(store), "--workspace", str(tmp_path / "work")])
        assert str(store) in [str(path) for path, _ in s.host_stores(other)], "with plugins on it is no installer"


def test_check_stores(scenario, tmp_path):
    store = tmp_path / "kits"
    store.mkdir()
    if scenario == "happy":
        s.check_stores([tmp_path / "work"], [(store, "the kits directory")])
    elif scenario == "sad":
        with pytest.raises(ValueError, match="lies inside the kits directory"):
            s.check_stores([store / "mine"], [(store, "the kits directory")])
    else:
        # A store reached through a link counts by its target.
        (tmp_path / "link").symlink_to(store)
        with pytest.raises(ValueError, match="the kits directory"):
            s.check_stores([store], [(tmp_path / "link", "the kits directory")])


def test_workspace_checks(scenario, tmp_path):
    options = {"happy": [], "sad": ["--workspace-ro"], "edge": []}[scenario]
    args = exposure_args(tmp_path, *options)
    args.sandbox_home = tmp_path / "home" if scenario == "edge" else None
    args.mode_other_workspaces_ro = False
    checks = s.workspace_checks(args, tmp_path / "session")
    assert checks["workspaces"] == [{"host": str(tmp_path / "work"), "sandbox": "/work",
                                     "mode": "ro" if scenario == "sad" else "rw"}]
    assert checks["read_only"] is (scenario == "sad")
    # Every mount (read-only ones too) and the persistent HOME: a later workspace may overlap none.
    assert {str(tmp_path / n) for n in ("rw", "ro", "state")} <= set(checks["trees"])
    assert (str(tmp_path / "home") in checks["trees"]) is (scenario == "edge")
    assert [str(tmp_path / "session"), "The run's session directory"] in checks["private"]
    json.dumps(checks)


def test_check_tiocsti(scenario, tmp_path, monkeypatch):
    sysctl = tmp_path / "legacy_tiocsti"
    monkeypatch.setattr(s, "TIOCSTI_SYSCTL", sysctl)
    if scenario == "happy":
        sysctl.write_text("0\n")
        s.check_tiocsti()
        return
    if scenario == "sad":
        sysctl.write_text("1\n")
    # edge: no sysctl at all means a kernel before 6.2, which always allows TIOCSTI.
    with pytest.raises(ValueError, match="TIOCSTI"):
        s.check_tiocsti()


def test_claude_state_subset(scenario, tmp_path):
    source = tmp_path / ".claude.json"
    if scenario == "sad":
        source.write_text("[1]")
        assert s.claude_state_subset(source) is None
        source.write_text("{broken")
        assert s.claude_state_subset(source) is None
        return
    document = {"oauthAccount": {"emailAddress": "a@example.org"}, "userID": "u", "theme": "dark",
                "mcpServers": {"db": {"env": {"TOKEN": "secret"}}}, "projects": {"/home/me/x": {}}}
    source.write_text(json.dumps(document))
    if scenario == "edge":
        # A host file managed as a symlink (dotfiles) is followed on the host side.
        real = tmp_path / "dotfiles.json"
        source.rename(real)
        source.symlink_to(real)
    subset = json.loads(s.claude_state_subset(source))
    assert subset == {"oauthAccount": {"emailAddress": "a@example.org"}, "userID": "u", "theme": "dark"}


def test_copy_auth_file(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"oauthAccount": {"x": 1}, "mcpServers": {"a": {}}}))
    if scenario == "happy":
        s.copy_auth_file(source, home, ".codex/auth.json")
        assert (home / ".codex/auth.json").read_bytes() == source.read_bytes()
    elif scenario == "sad":
        source.write_text("{broken")
        assert s.copy_auth_file(source, home, ".claude.json") is None
        assert not (home / ".claude.json").exists()
    else:
        s.copy_auth_file(source, home, ".claude.json")
        assert json.loads((home / ".claude.json").read_text()) == {"oauthAccount": {"x": 1}}
        assert (home / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_replace_private(scenario, tmp_path):
    home = tmp_path / "home"
    target = home / ".config/app/config.json"
    if scenario == "sad":
        target.parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.write_text("keep")
        target.symlink_to(outside)
        with pytest.raises(ValueError):
            s.replace_private(b"new", home, ".config/app/config.json")
        assert outside.read_text() == "keep"
        return
    home.mkdir()
    assert s.replace_private(b"one", home, ".config/app/config.json") is True
    assert target.read_bytes() == b"one" and target.stat().st_mode & 0o777 == 0o600
    if scenario == "edge":
        inode = target.stat().st_ino
        assert s.replace_private(b"one", home, ".config/app/config.json") is False
        assert target.stat().st_ino == inode
        target.unlink()
        os.mkfifo(target)  # A planted FIFO is replaced without being opened.
        assert s.replace_private(b"two", home, ".config/app/config.json") is True
        assert target.read_bytes() == b"two"


def test_timestamp_ms(scenario):
    values = {"happy": [0, 1, time.time() * 1000, 2 ** 53 - 1],
              "sad": [-1, 2 ** 53, 10 ** 400, float("inf"), float("nan"), True, "1", None],
              "edge": [0.5, 1e15]}[scenario]
    assert all(bool(s.timestamp_ms(v)) == (scenario != "sad") for v in values)


def test_credential_digest(scenario, tmp_path):
    path = tmp_path / "credentials.json"
    if scenario == "sad":
        assert s.credential_digest(path) is None
        return
    path.write_text("a" if scenario == "happy" else "")
    digest = s.credential_digest(path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("b")
    assert s.credential_digest(path) != digest


def test_claude_writeback_state(scenario, tmp_path):
    host, sandbox = tmp_path / "host.json", tmp_path / "home/.claude/.credentials.json"
    soon = (time.time() + 3600) * 1000
    if scenario != "sad":
        claude_credentials(host, soon)
        claude_credentials(sandbox, soon)
    state = s.claude_writeback_state((host, sandbox))
    if scenario == "sad":
        assert state == {"host": None, "host_login": False, "tokens": None}
        return
    assert state["host"] == s.oauth_token_digest(s.claude_oauth(host)) and state["host_login"] is True
    assert state["tokens"] == {"accessToken": "access-fixture", "refreshToken": "refresh-fixture", "expiresAt": soon}
    if scenario == "edge":
        assert "access-fixture" not in state["host"]


def test_claude_writeback(scenario, tmp_path):
    home = tmp_path / "home"
    host, sandbox = tmp_path / "host/.claude/.credentials.json", home / ".claude/.credentials.json"
    soon, later = (time.time() + 3600) * 1000, (time.time() + 7200) * 1000
    claude_credentials(sandbox, soon)
    if scenario == "sad":
        # No host login before: the sandbox never creates one; the agent's tokens stay inside.
        before = s.claude_writeback_state((host, sandbox))
        claude_credentials(sandbox, later, refresh="forged")
        assert s.claude_writeback(home, (host, sandbox), before) is None
        assert not host.exists()
        # Review finding: a host file without a login (e.g. only MCP OAuth tokens) must not receive one either.
        host.parent.mkdir(parents=True)
        host.write_text(json.dumps({"mcpOAuth": {"srv": {"accessToken": "m"}}}))
        before = s.claude_writeback_state((host, sandbox))
        claude_credentials(sandbox, later, refresh="forged-again")
        assert s.claude_writeback(home, (host, sandbox), before) is None
        assert "claudeAiOauth" not in json.loads(host.read_text())
        return
    claude_credentials(host, soon)
    before = s.claude_writeback_state((host, sandbox))
    if scenario == "happy":
        assert s.claude_writeback(home, (host, sandbox), before) is None  # nothing refreshed, nothing written
    # Regression: Claude on the host rewrote its file for another reason (an MCP OAuth token) while the
    # sandbox refreshed the login. The login itself is unchanged, so the rotated tokens must still go back;
    # dropping them left the host with a used-up refresh token and the next start with "Login expired".
    claude_credentials(sandbox, later, refresh="rotated")
    document = json.loads(host.read_text())
    document["mcpOAuth"] = {"srv": {"accessToken": "m"}}
    host.write_text(json.dumps(document))
    if scenario == "happy":
        assert "written back" in s.claude_writeback(home, (host, sandbox), before)
        written = json.loads(host.read_text())
        assert written["claudeAiOauth"]["refreshToken"] == "rotated" and written["mcpOAuth"]["srv"]["accessToken"] == "m"
        return
    # edge: the user logged in again on the host during the run; the host's login wins.
    claude_credentials(sandbox, later, refresh="rotated")
    claude_credentials(host, soon, refresh="host-relogin")
    assert "changed during the run; kept that one" in s.claude_writeback(home, (host, sandbox), before)
    assert json.loads(host.read_text())["claudeAiOauth"]["refreshToken"] == "host-relogin"
    # A symlinked ~/.claude in the sandbox is refused before anything is read through it.
    target = tmp_path / "elsewhere"
    target.mkdir()
    shutil.rmtree(home / ".claude")
    (home / ".claude").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        s.claude_writeback(home, (host, sandbox), before)


def test_load_mcp_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_mcp_module()
    else:
        module = s.load_mcp_module()
        assert callable(module.ensure_running) and "demo" in module.BUILTIN
        if scenario == "edge":
            assert s.load_mcp_module() is not module


def test_load_vpn_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_vpn_module()
    else:
        module = s.load_vpn_module()
        assert callable(module.agent_up) and module.PROFILE_NAME == s.VPN_PROFILE_NAME
        # The module sees this launcher as loaded, what a caller replaced in it included.
        monkeypatch.setattr(s, "podman_command", lambda a, *args: ["fake-podman", *args])
        assert module.launcher().podman_command(None, "ps") == ["fake-podman", "ps"]
        if scenario == "edge":
            assert s.load_vpn_module() is not module


def test_load_activity_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_activity_module()
    else:
        module = s.load_activity_module()
        assert callable(module.start) and "prompt" in module.KINDS
        if scenario == "edge":
            assert s.load_activity_module() is not module


def test_start_activity(scenario, monkeypatch, tmp_path, capsys):
    a = SimpleNamespace(log_dir=tmp_path / "logs", probe=False, mcp_socket_dir=None, run_id="claude-1", agent="claude",
                        backend="bwrap", pod_name=None, tmux_socket_dir=None, session=None, state_dir=None,
                        control_dir=None, workspaces=[str(tmp_path / "w")])
    cfg = {"env": {"API_TOKEN": "tok-123456"}, "secret_names": []}
    if scenario == "happy":  # a timeline in the log directory, also before the agent wrote anything
        (tmp_path / "logs").mkdir()
        collector = s.start_activity(a, tmp_path / "home", None, cfg)
        collector.finish(0)
        records = [json.loads(line) for line in (tmp_path / "logs" / "activity.jsonl").read_text().splitlines()]
        assert [r["details"]["event"] for r in records] == ["start", "stop"]
        assert records[0]["details"]["workspaces"] == [{"host": str(tmp_path / "w"), "sandbox": "/work"}]
    elif scenario == "sad":  # probes, MCP servers and runs without a log directory keep no timeline
        for change in ({"probe": True}, {"mcp_socket_dir": tmp_path}, {"log_dir": None}):
            assert s.start_activity(SimpleNamespace(**{**vars(a), **change}), tmp_path, None, cfg) is None
    else:  # a broken activity module never fails the launch
        def broken():
            raise RuntimeError("broken")
        monkeypatch.setattr(s, "load_activity_module", broken)
        assert s.start_activity(a, tmp_path, None, cfg) is None
        assert "activity: not recorded" in capsys.readouterr().err


def test_mcp_socket_dir(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert s.mcp_socket_dir("x") == Path.home() / ".local/state/sandbox-ai/mcp/x/sock"
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ("s" if scenario == "happy" else "with space")))
        name = "demo" if scenario == "happy" else "a_b-1"
        assert s.mcp_socket_dir(name) == tmp_path / ("s" if scenario == "happy" else "with space") / f"sandbox-ai/mcp/{name}/sock"


def test_mcp_bridge_args(scenario):
    name = {"happy": "demo", "sad": "x", "edge": "a_b-1"}[scenario]
    assert s.mcp_bridge_args(name) == ["-I", "/sandbox/runtime.py", "--internal-mcp-bridge",
                                       f"/run/sandbox-mcp/{name}/server.sock"]


def test_mcp_agent_args(scenario):
    if scenario == "sad":
        assert s.mcp_agent_args(s.parse_args(["claude"])) == []
        assert s.mcp_agent_args(s.parse_args(["opencode", "--mcp", "demo"])) == []
    elif scenario == "happy":
        args = s.mcp_agent_args(s.parse_args(["claude", "--mcp", "demo"]))
        assert args[0] == "--mcp-config" and "demo" in json.loads(args[1])["mcpServers"]
    else:
        args = s.mcp_agent_args(s.parse_args(["codex", "--mcp", "a", "--mcp", "a", "--mcp", "b"]))
        assert args.count("-c") == 4 and 'mcp_servers.b.command="python3"' in args


def test_mcp_serve(scenario, tmp_path):
    import threading, time as clock
    sock = tmp_path / "s.sock"
    if scenario == "sad":
        with pytest.raises(OSError):
            s.mcp_serve(str(tmp_path / "missing-dir" / "s.sock"), ["cat"])
        return
    if scenario == "edge":
        sock.write_text("stale")  # A stale socket file is replaced.
    command = ["cat"] if scenario == "happy" else ["/nonexistent/server"]
    threading.Thread(target=s.mcp_serve, args=(str(sock), command), kwargs={"cwd": str(tmp_path)}, daemon=True).start()
    for _ in range(100):
        if sock.exists() and sock.is_socket():
            break
        clock.sleep(0.05)
    ping = b'{"jsonrpc": "2.0", "method": "ping", "id": 1}\n'

    def exchange(data=ping):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(10)
        client.connect(str(sock))
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        received = b""
        try:
            while chunk := client.recv(100):
                received += chunk
        except ConnectionResetError:
            pass  # a failed server closes with unread input
        return received
    assert exchange() == (ping if scenario == "happy" else b"")
    # One failing or finished instance never takes the listener down.
    assert exchange() == (ping if scenario == "happy" else b"")
    if scenario == "happy":
        # What is not one JSON message never reaches the server: the relay answers it.
        answer = json.loads(exchange(b"echo\n"))
        assert answer["error"]["code"] == -32700 and answer["id"] is None


def test_mcp_bridge(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="unavailable"):
            s.mcp_bridge(str(tmp_path / "absent.sock"))
        return
    import threading
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(tmp_path / "b.sock"))
    server.listen(1)
    payload = b"x" * (300000 if scenario == "edge" else 5)
    def answer():
        connection, _ = server.accept()
        with connection:
            while connection.recv(65536):
                pass
            connection.sendall(payload)
    threading.Thread(target=answer, daemon=True).start()
    read_r, read_w = os.pipe()
    write_r, write_w = os.pipe()
    os.write(read_w, b"request")
    os.close(read_w)
    saved = os.dup(0), os.dup(1)
    os.dup2(read_r, 0)
    os.dup2(write_w, 1)
    received = []
    reader = threading.Thread(target=lambda: received.append(b"".join(iter(lambda: os.read(write_r, 65536), b""))))
    reader.start()
    try:
        assert s.mcp_bridge(str(tmp_path / "b.sock")) == 0
    finally:
        os.dup2(saved[0], 0)
        os.dup2(saved[1], 1)
        os.close(write_w)
        reader.join(10)
    assert received == [payload]


def test_config_key(scenario, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    if scenario == "happy":
        assert s.config_key(real / ".sandbox-ai.json") == real / ".sandbox-ai.json"
    elif scenario == "sad":
        # The file name itself is never followed, even when it is a link.
        (real / "target.json").write_text("{}")
        (real / ".sandbox-ai.json").symlink_to(real / "target.json")
        assert s.config_key(real / ".sandbox-ai.json").name == ".sandbox-ai.json"
    else:
        (tmp_path / "alias").symlink_to(real)
        assert s.config_key(tmp_path / "alias" / ".sandbox-ai.json") == real / ".sandbox-ai.json"


def test_read_config_bytes(scenario, tmp_path):
    path = tmp_path / "c.json"
    if scenario == "happy":
        path.write_bytes(b'{"a": 1}')
        assert s.read_config_bytes(path) == b'{"a": 1}'
    elif scenario == "sad":
        (tmp_path / "t.json").write_text("{}")
        path.symlink_to(tmp_path / "t.json")
        with pytest.raises(ValueError, match="symlink"):
            s.read_config_bytes(path)
        os.mkfifo(tmp_path / "fifo")
        with pytest.raises(ValueError, match="regular file"):
            s.read_config_bytes(tmp_path / "fifo")
    else:
        path.write_bytes(b"x" * (s.CONFIG_LIMIT + 1))
        with pytest.raises(ValueError, match="larger"):
            s.read_config_bytes(path)


def test_attach_frame(scenario):
    import struct as st
    kind, payload = {"happy": (b"d", b"ls\r"), "sad": (b"r", st.pack("!HH", 40, 120)), "edge": (b"d", b"")}[scenario]
    frame = s.attach_frame(kind, payload)
    assert frame[:1] == kind and st.unpack("!I", frame[1:5])[0] == len(payload) and frame[5:] == payload


def test_read_frames(scenario):
    import struct as st
    data = s.attach_frame(b"d", b"abc") + s.attach_frame(b"r", st.pack("!HH", 30, 100))
    if scenario == "happy":
        frames, rest = s.read_frames(data)
        assert frames == [(b"d", b"abc"), (b"r", st.pack("!HH", 30, 100))] and rest == b""
    elif scenario == "sad":
        frames, rest = s.read_frames(data[:10])  # an incomplete second header stays buffered
        assert frames == [(b"d", b"abc")] and rest == data[8:10]
    else:
        frames, rest = s.read_frames(data[:3])
        assert frames == [] and rest == data[:3]


def relay_round_trip(tmp_path, child_script, frames):
    import threading, time as clock
    sock = tmp_path / "attach.sock"
    command = [sys.executable, "-c", child_script]
    threading.Thread(target=s.attach_relay, args=(str(sock), command), daemon=True).start()
    for _ in range(200):
        if sock.exists():
            break
        clock.sleep(0.02)
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(15)
    client.connect(str(sock))
    for frame in frames:
        client.sendall(frame)
    received = b""
    try:
        while chunk := client.recv(4096):
            received += chunk
    except ConnectionResetError:
        pass
    return received


def test_attach_relay(scenario, tmp_path):
    import struct as st
    script = ("import os,sys; r,c=os.get_terminal_size(0).lines,os.get_terminal_size(0).columns; "
              "print('SIZE',r,c,flush=True); line=sys.stdin.readline(); print('GOT',line.strip(),flush=True)")
    if scenario == "sad":
        out = relay_round_trip(tmp_path, "import sys; sys.exit(3)", [])
        assert out == b""  # the attach client ended: connection closed, relay keeps serving
        return
    size = (30, 100) if scenario == "happy" else (12, 345)
    out = relay_round_trip(tmp_path, script, [s.attach_frame(b"r", st.pack("!HH", *size)),
                                              s.attach_frame(b"d", b"hello\r")])
    assert f"SIZE {size[0]} {size[1]}".encode() in out and b"GOT hello" in out


def test_attach_client(scenario, tmp_path):
    import threading
    sock = tmp_path / "c.sock"
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="not reachable"):
            s.attach_client(str(sock), os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY))
        # The sandbox swaps attach.sock for a symlink to another host socket of the user:
        # the client refuses instead of sending keystrokes there.
        victim = socket.socket(socket.AF_UNIX)
        victim.bind(str(tmp_path / "victim.sock"))
        victim.listen(1)
        victim.settimeout(0.5)
        sock.symlink_to(tmp_path / "victim.sock")
        with pytest.raises(RuntimeError, match="not reachable"):
            s.attach_client(str(sock), os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY))
        with pytest.raises(OSError):
            victim.accept()
        sock.unlink()
        (tmp_path / "plain").write_text("")  # not a socket at all
        with pytest.raises(RuntimeError, match="not a socket"):
            s.attach_client(str(tmp_path / "plain"), os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY))
        return
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock))
    server.listen(1)
    got = []
    # A hostile relay sends a tmux-style exec request: the client only ever writes bytes out.
    hostile = b"\x00\x00\x00\x0cMSG_EXEC rm -rf ~" if scenario == "edge" else b"screen output"
    def serve():
        connection, _ = server.accept()
        with connection:
            data = b""
            while b"typed" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
            got.append(data)
            connection.sendall(hostile)
    threading.Thread(target=serve, daemon=True).start()
    stdin_r, stdin_w = os.pipe()
    os.write(stdin_w, b"typed")
    os.close(stdin_w)
    out_r, out_w = os.pipe()
    assert s.attach_client(str(sock), stdin_r, out_w) == 0
    os.close(out_w)
    assert os.read(out_r, 4096) == hostile
    frames, _ = s.read_frames(got[0])
    assert (b"d", b"typed") in frames


def test_check_home_path(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if scenario == "happy":
        assert s.check_home_path(home, home / ".claude/x.json") == home / ".claude/x.json"
    elif scenario == "sad":
        (tmp_path / "outside").mkdir()
        (home / "link").symlink_to(tmp_path / "outside")
        with pytest.raises(ValueError, match="symlink"):
            s.check_home_path(home, home / "link/x.json")
    else:
        with pytest.raises(ValueError, match="outside"):
            s.check_home_path(home, tmp_path / "elsewhere.json")


def test_save_trusted(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    data = {"happy": {"/p/.sandbox-ai.json": "ab" * 32}, "sad": {}, "edge": {"/ü ñ/x": "cd" * 32}}[scenario]
    s.save_trusted(data)
    assert s.trusted_configs() == data
    assert (tmp_path / "cfg/sandbox-ai").stat().st_mode & 0o777 == 0o700


def test_forget_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / ".sandbox-ai.json"
    path.write_text("{}")
    if scenario == "sad":
        assert s.forget_config(path) is False
        return
    s.trust_config(path)
    if scenario == "edge":
        path.unlink()
    assert s.forget_config(path) is True and s.trusted_configs() == {}


def test_listen_unix(scenario, tmp_path):
    path = tmp_path / "l.sock"
    if scenario == "sad":
        with pytest.raises(OSError):
            s.listen_unix(tmp_path / "missing" / "l.sock")
        return
    if scenario == "edge":
        path.write_text("stale")
    server = s.listen_unix(path)
    try:
        assert path.is_socket() and not [p for p in tmp_path.iterdir() if p.name.startswith(".l.sock")]
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(path))  # accepted at once: listening before the name appeared
        client.close()
    finally:
        server.close()


def test_relay_terminal(scenario, tmp_path):
    import struct as st, threading
    left, right = socket.socketpair()
    if scenario == "sad":
        right.close()  # the client vanishes before sending a size: nothing is started
        s.relay_terminal(left, ["/nonexistent"])
        return
    command = ([sys.executable, "-c", "import os; print(os.get_terminal_size(0).columns, flush=True)"]
               if scenario == "happy" else ["/nonexistent/attach"])
    thread = threading.Thread(target=s.relay_terminal, args=(left, command), daemon=True)
    thread.start()
    right.sendall(s.attach_frame(b"r", st.pack("!HH", 20, 77)))
    right.settimeout(10)
    received = b""
    try:
        while chunk := right.recv(4096):
            received += chunk
    except (ConnectionResetError, socket.timeout):
        pass
    thread.join(10)
    assert not thread.is_alive()
    assert (b"77" in received) if scenario == "happy" else received == b""


def test_terminate(scenario):
    signum = {"happy": signal.SIGTERM, "sad": signal.SIGHUP, "edge": signal.SIGINT}[scenario]
    with pytest.raises(SystemExit) as raised:
        s.terminate(signum, None)
    assert raised.value.code == 128 + signum


def blocked():
    return signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_signals_deferred(scenario):
    received = []
    def handler(signum, _frame):
        received.append(signum)
    previous = signal.signal(signal.SIGTERM, handler)
    try:
        before = blocked()
        if scenario == "happy":
            with s.signals_deferred():
                assert set(s.CLEANUP_SIGNALS) <= blocked()
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.01)
                assert received == []
            # Held, not lost: delivered once cleanup is over.
            assert received == [signal.SIGTERM] and blocked() == before
        elif scenario == "sad":
            # Off the main thread: no mask, no handler change (Python runs handlers in the main thread).
            inside = []
            def worker():
                with s.signals_deferred():
                    inside.append(blocked())
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            assert not set(s.CLEANUP_SIGNALS) & inside[0]
        else:
            # A helper thread with an open mask takes the signal; the handler still waits for cleanup,
            # and an exception in the cleanup restores mask and handler.
            with pytest.raises(RuntimeError, match="cleanup failed"):
                with s.signals_deferred():
                    def helper_takes_signal():
                        # A new thread inherits the blocked mask; helpers started before cleanup have it open.
                        signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
                        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
                    helper = threading.Thread(target=helper_takes_signal)
                    helper.start()
                    helper.join()
                    time.sleep(0.01)
                    assert received == []
                    raise RuntimeError("cleanup failed")
            assert received == [signal.SIGTERM] and blocked() == before
            assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_launch(scenario, tmp_path, monkeypatch, capsys):
    if scenario == "sad":
        with pytest.raises(ValueError):
            s.launch(["codex", "--workspace", str(tmp_path), "--offline", "--dns", "--dry-run"])
    else:
        options = ["--dry-run"] + (["--gui"] if scenario == "edge" else [])
        assert s.launch(["codex", "--workspace", str(tmp_path / "w"), *options]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["agent"] == "codex" and (plan["gui"] is not None) == (scenario == "edge")


def test_socket_locations(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        found = dict(s.socket_locations())
        assert (tmp_path / "run").resolve() in found and Path(f"/run/user/{os.getuid()}").resolve() in found
    elif scenario == "sad":
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert all("XDG_RUNTIME_DIR" not in label for _, label in s.socket_locations())
    else:
        monkeypatch.setenv("TMPDIR", str(tmp_path / "t"))
        assert (tmp_path / "t" / f"sandbox-ai-{os.getuid()}").resolve() in dict(s.socket_locations())


def test_check_socket_location(scenario, tmp_path, monkeypatch):
    """Security review round 2: a folder of the user's sockets (the shared SSH connections among them) never reaches
    a sandbox, not even read-only."""
    run = tmp_path / "run"
    (run / "sandbox-ai" / "ssh").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    if scenario == "happy":
        s.check_socket_location((tmp_path / "project").resolve(), "Workspace")
    elif scenario == "sad":
        with pytest.raises(ValueError, match="lies in .*a socket there"):
            s.check_socket_location((run / "sandbox-ai" / "ssh").resolve(), "--mount")
    else:
        with pytest.raises(ValueError, match="is or holds"):
            s.check_socket_location(tmp_path.resolve(), "Workspace")  # holds it


MOUNTINFO_SAMPLE = (
    "22 1 0:21 / / rw - ext4 /dev/sda1 rw\n"
    "40 22 0:5 / /srv/chroot/proc rw - proc proc rw\n"
    "41 22 0:6 / /srv/chroot/sys rw - sysfs sysfs rw\n"
    "42 22 0:7 / /srv/other\\040dir/proc rw - proc proc rw\n"
    "43 22 0:8 / /srv/chroot/data rw - ext4 /dev/sdb1 rw\n")


def test_kernel_mounts_below(scenario, tmp_path):
    table = tmp_path / "mountinfo"
    table.write_text(MOUNTINFO_SAMPLE)
    if scenario == "happy":
        assert s.kernel_mounts_below(Path("/srv/chroot"), table) == [("/srv/chroot/proc", "proc"),
                                                                       ("/srv/chroot/sys", "sysfs")]
    elif scenario == "sad":
        assert s.kernel_mounts_below(Path("/srv/chroot/data"), table) == []
        assert s.kernel_mounts_below(Path("/srv/chroot"), tmp_path / "missing") == []
    else:
        assert s.kernel_mounts_below(Path("/srv/other dir"), table) == [("/srv/other dir/proc", "proc")]
        assert s.kernel_mounts_below(Path("/srv/chro"), table) == [], "a prefix of a name is not a parent"


def test_kernel_mount_holding(scenario, tmp_path):
    """Security review round 3: a path INSIDE a kernel file system mounted outside /proc, /sys and /dev (a chroot's
    procfs) is refused as well: the mount holding it is the one with the longest mount point, the last on top."""
    table = tmp_path / "mountinfo"
    table.write_text(MOUNTINFO_SAMPLE)
    if scenario == "happy":
        assert s.kernel_mount_holding(Path("/srv/chroot/data/x"), table) is None
        assert s.kernel_mount_holding(Path("/srv/chroot"), table) is None, "holding one is kernel_mounts_below's"
        assert s.kernel_mount_holding(Path("/srv/chroot/proc/1"), table) == ("/srv/chroot/proc", "proc")
        assert s.kernel_mount_holding(Path("/srv/chroot/proc"), table) == ("/srv/chroot/proc", "proc")
        assert s.kernel_mount_holding(Path("/srv/other dir/proc/self"), table) == ("/srv/other dir/proc", "proc")
    elif scenario == "sad":
        assert s.kernel_mount_holding(Path("/srv/chroot/proc/1"), tmp_path / "missing") is None
        assert s.kernel_mount_holding(Path("/srv/chroot/processes"), table) is None, "a prefix of a name"
    else:
        # Overmounts: the last line on the same point lies on top; a kernel file system mounted later on a parent
        # may hide the deeper mount, so it counts.
        table.write_text(MOUNTINFO_SAMPLE + "50 40 0:30 / /srv/chroot/proc rw - tmpfs tmpfs rw\n")
        assert s.kernel_mount_holding(Path("/srv/chroot/proc/1"), table) is None
        table.write_text(MOUNTINFO_SAMPLE + "51 22 0:31 / /srv/chroot/data rw - proc proc rw\n")
        assert s.kernel_mount_holding(Path("/srv/chroot/data/1"), table) == ("/srv/chroot/data", "proc")
        table.write_text(MOUNTINFO_SAMPLE + "52 22 0:32 / /srv rw - sysfs sysfs rw\n")
        assert s.kernel_mount_holding(Path("/srv/chroot/data/1"), table) == ("/srv", "sysfs")


def test_check_kernel_mounts(scenario, tmp_path):
    table = tmp_path / "mountinfo"
    table.write_text(MOUNTINFO_SAMPLE)
    if scenario == "happy":
        s.check_kernel_mounts(Path("/srv/chroot/data"), "Workspace", table)
        s.check_kernel_mounts(Path("/srv/chroot/data/deeper"), "--state-dir", table)
    elif scenario == "sad":
        with pytest.raises(ValueError, match="kernel's own: /srv/chroot/proc \\(proc\\)"):
            s.check_kernel_mounts(Path("/srv/chroot"), "Workspace", table)
    else:
        with pytest.raises(ValueError, match="--mount"):
            s.check_kernel_mounts(Path("/srv"), "--mount", table)
        # Inside one mounted outside /proc, /sys and /dev (a chroot's procfs: /srv/chroot/proc/1/root is /).
        for option in ("Workspace", "--mount", "--state-dir"):
            with pytest.raises(ValueError, match=f"{option} /srv/chroot/proc/1 lies in /srv/chroot/proc, a file "
                                                 "system of the kernel's own \\(proc\\)"):
                s.check_kernel_mounts(Path("/srv/chroot/proc/1"), option, table)


def test_check_output_dir(scenario, tmp_path, monkeypatch):
    """Security review round 3: --log-dir and --gui-vnc are bound read-write; like a workspace they must not be,
    hold or lie in a folder of your sockets, a private location or a kernel file system."""
    run, state = tmp_path / "run", tmp_path / "state"
    (run / "bus").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    if scenario == "happy":
        s.check_output_dir(tmp_path / "logs", "--log-dir")
        # sandbox-ai's own folders for them in XDG_STATE_HOME (the wizard's defaults) are not private locations.
        s.check_output_dir(state / "sandbox-ai" / "logs" / "project-1234", "--log-dir")
        s.check_output_dir(state / "sandbox-ai" / "vnc" / "project-1234", "--gui-vnc")
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--gui-vnc .*a socket there"):
            s.check_output_dir(run / "bus", "--gui-vnc")
        for private in (state, state / "sandbox-ai" / "logs", state / "sandbox-ai" / "runs"):
            with pytest.raises(ValueError, match="--log-dir .*(lies in|contains|is or holds)"):
                s.check_output_dir(private, "--log-dir")
        s.check_output_dir(Path.home() / ".ssh", "--log-dir")  # yours to give: a warning, not a refusal
    else:
        table = tmp_path / "mountinfo"
        table.write_text(f"22 1 0:21 / / rw - ext4 /dev/sda1 rw\n40 22 0:5 / {tmp_path}/chroot/proc rw - proc "
                         "proc rw\n")
        real = s.check_kernel_mounts
        monkeypatch.setattr(s, "check_kernel_mounts", lambda path, option: real(path, option, table))
        with pytest.raises(ValueError, match="--log-dir .*lies in .*kernel's own"):
            s.check_output_dir(tmp_path / "chroot/proc/1", "--log-dir")
        with pytest.raises(ValueError, match="--gui-vnc .*holds file systems of the kernel's own"):
            s.check_output_dir(tmp_path / "chroot", "--gui-vnc")


def test_check_workspace_scope(scenario, tmp_path, monkeypatch):
    """Review finding: HOME, /, the temp directory or the launcher's own checkout must not become the workspace."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    if scenario == "happy":
        s.check_workspace_scope(tmp_path / "home" / "src" / "app")
    elif scenario == "sad":
        for risky in [Path("/"), tmp_path / "home", s.HERE, s.HERE.parent, Path(tempfile.gettempdir())]:
            with pytest.raises(ValueError, match="Workspace"):
                s.check_workspace_scope(risky.resolve())
        # The kernel's and the devices' trees, also where a read-only workspace was meant (review finding).
        for tree in ("/proc", "/proc/1/root", "/sys", "/sys/fs/cgroup", "/dev", "/dev/shm"):
            with pytest.raises(ValueError, match="kernel's and the devices' trees"):
                s.check_workspace_scope(Path(tree))
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        (tmp_path / "run").mkdir()
        with pytest.raises(ValueError, match="XDG_RUNTIME_DIR"):
            s.check_workspace_scope(tmp_path / "run")
        s.check_workspace_scope(tmp_path / "run-not")


def test_auth_source_candidates(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    store = tmp_path / ".local/share/sandbox-ai/auth"
    if scenario == "happy":
        assert s.auth_source_candidates("claude") == [store / "claude/.claude/.credentials.json",
                                                      store / "claude/.claude.json",
                                                      tmp_path / ".claude/.credentials.json",
                                                      tmp_path / ".claude/.claude.json", tmp_path / ".claude.json"]
        # The login store feeds every sandbox: a workspace that contains it is refused.
        (tmp_path / ".local/share").mkdir(parents=True)
        with pytest.raises(ValueError, match="agent can change|lies in |sandbox-ai's own folder|is or holds"):
            s.validate(s.parse_args(["claude", "--workspace", str(tmp_path / ".local")]))
    elif scenario == "sad":
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work/.codex"))
        assert s.auth_source_candidates("codex") == [store / "codex/.codex/auth.json", tmp_path / "work/.codex/auth.json"]
        # A credential directory inside the workspace is refused before anything is read.
        with pytest.raises(ValueError, match="agent can change|lies in |sandbox-ai's own folder"):
            s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "work")]))
    else:
        # Set but empty counts as unset (XDG): never a path relative to the current directory.
        monkeypatch.setenv("XDG_DATA_HOME", "")
        monkeypatch.setenv("CODEX_HOME", "")
        assert all(path.is_absolute() for agent in ("codex", "opencode")
                   for path in s.auth_source_candidates(agent) + [src for src, _ in s.auth_sources(agent)])
        monkeypatch.delenv("CODEX_HOME")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        assert s.auth_source_candidates("opencode") == [tmp_path / "data/sandbox-ai/auth/opencode/.local/share/opencode/auth.json",
                                                        tmp_path / "data/opencode/auth.json"]


def test_opencode_catalog(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    if scenario == "sad":
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert s.opencode_catalog() == tmp_path / ".cache/opencode/models.json"
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
        assert s.opencode_catalog() == tmp_path / "c/opencode/models.json"


def test_has_controlling_terminal(scenario, monkeypatch):
    if scenario == "sad":
        monkeypatch.setattr(s.os, "open", Mock(side_effect=OSError(6, "No such device or address")))
        assert s.has_controlling_terminal() is False
    elif scenario == "happy":
        monkeypatch.setattr(s.os, "open", Mock(return_value=99))
        monkeypatch.setattr(s.os, "close", Mock())
        assert s.has_controlling_terminal() is True
    else:
        # bwrap with every standard stream redirected still checks TIOCSTI when a controlling terminal exists.
        monkeypatch.setattr(s, "has_controlling_terminal", lambda: True)
        monkeypatch.setattr(s, "check_tiocsti", Mock(side_effect=ValueError("TIOCSTI")))
        monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(s.os, "isatty", lambda fd: False)
        with pytest.raises(ValueError, match="TIOCSTI"):
            s.main(["codex", "--backend", "bwrap", "--offline", "--binary", "/usr/bin/true"])


def fake_proc(proc, pid, cmdline, start, comm="python3"):
    (proc / str(pid)).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "cmdline").write_bytes(cmdline)
    # Fields 3..21 before the start time (field 22); comm may contain spaces and parentheses.
    (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S 1 " + "0 " * 17 + f"{start} 4096 12\n")


def test_process_start(scenario, tmp_path):
    proc = tmp_path / "proc"
    if scenario == "happy":
        fake_proc(proc, 7, b"x\0", 123456)
        assert s.process_start(7, proc) == 123456
        assert s.process_start(os.getpid()) == s.process_start(os.getpid())  # the real /proc
    elif scenario == "sad":
        assert s.process_start(8, proc) is None  # gone
    else:
        fake_proc(proc, 9, b"x\0", 42, comm="a) S 1 2 (b")
        assert s.process_start(9, proc) == 42
        (proc / "9" / "stat").write_text("9 (short) S 1\n")
        assert s.process_start(9, proc) is None


def test_process_cmdline(scenario, tmp_path):
    proc = tmp_path / "proc"
    fake_proc(proc, 7, b"python3\0--tmux-socket-dir\0/s\0", 500)
    if scenario == "happy":
        assert s.process_cmdline(7, 500, proc) == [b"python3", b"--tmux-socket-dir", b"/s", b""]
    elif scenario == "sad":
        # The PID was reused by a process with the same command line, started later.
        assert s.process_cmdline(7, 499, proc) == []
        assert s.process_cmdline(8, 500, proc) == [] and s.process_cmdline(None, None, proc) == []
    else:
        # A record of an older version has no start time: the command line alone decides, as before.
        assert s.process_cmdline(7, None, proc)[1] == b"--tmux-socket-dir"


def test_pod_owner(scenario, tmp_path):
    options = {"happy": ["--pod-name", "agent-session-x", "--tmux-socket-dir", str(tmp_path / "t")],
               "sad": ["--tmux-socket-dir", str(tmp_path / "t")],
               "edge": ["--pod-name", "agent-mcp-x", "--mcp-socket-dir", str(tmp_path / "m")]}[scenario]
    owner = s.pod_owner(s.parse_args(["codex", *options]))
    assert owner == {"happy": str((tmp_path / "t").resolve()), "sad": None, "edge": str((tmp_path / "m").resolve())}[scenario]


def test_remove_stale_pod(scenario, monkeypatch):
    calls = []
    labels = {"happy": {s.SESSION_LABEL: "/own"}, "sad": {s.SESSION_LABEL: "/other"}, "edge": None}[scenario]
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pod", "inspect"]:
            return Mock(returncode=0 if labels is not None else 125, stdout=json.dumps(labels))
        return Mock(returncode=0)
    monkeypatch.setattr(s.subprocess, "run", run)
    engine = Mock(podman_url=None)
    if scenario == "happy":
        assert s.remove_stale_pod(engine, "agent-mcp-x", "/own") is True
        assert calls[-1] == ["podman", "pod", "rm", "--force", "agent-mcp-x"]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="not created here"):
            s.remove_stale_pod(engine, "agent-mcp-x", "/own")
        assert len(calls) == 1
    else:
        assert s.remove_stale_pod(engine, "agent-mcp-x", "/own") is False


def test_relative_paths(scenario):
    options = {"happy": ["--mount", "/a:/mnt/a:ro", "--state-dir=~/s", "--git-token-file", "http://x=/t"],
               "sad": ["--mount", "data:/mnt/d:rw", "--local-config=c.json", "--git-token-file", "https://h=t",
                       "--copy", "f:.x"],
               "edge": ["--dns", "--allow-tcp", "1.2.3.4:5", "--workspace", "."]}[scenario]
    expected = {"happy": [], "sad": ["--mount data:/mnt/d:rw", "--local-config c.json", "--git-token-file https://h=t",
                                     "--copy f:.x"], "edge": ["--workspace ."]}[scenario]
    assert s.relative_paths(options) == expected


def test_review_workspace_inside_rw_mount_or_state_dir_is_refused(scenario, tmp_path):
    """Review finding: the agent could swap a directory on the way to the workspace for a symlink."""
    data = tmp_path / "data"
    workspace = data / "a" / "b" / "proj"
    workspace.mkdir(parents=True)
    options = {"happy": ["--mount", f"{tmp_path / 'other'}:/mnt/o:rw"],
               "sad": ["--mount", f"{data}:/mnt/data:rw"], "edge": ["--state-dir", str(data)]}[scenario]
    (tmp_path / "other").mkdir()
    args = s.parse_args(["codex", "--workspace", str(workspace), "--no-auth", *options])
    if scenario == "happy":
        s.validate(args)
    else:
        with pytest.raises(ValueError, match="agent can change"):
            s.validate(args)


def test_review_allow_refuses_scope_ids(scenario):
    value = {"happy": "2001:db8::/32", "sad": "fe80::1%x y", "edge": "fe80::1%eth0"}[scenario]
    args = s.parse_args(["codex", "--no-auth", "--allow", value])
    if scenario == "happy":
        s.validate(args)
    else:
        with pytest.raises(ValueError, match="scope ID"):
            s.validate(args)


def test_default_log_dir(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a, b = tmp_path / "x" / "app", tmp_path / "y" / "app"
    if scenario == "happy":
        assert s.default_log_dir(a).parent == tmp_path / "state/sandbox-ai/logs"
        assert s.default_log_dir(a).name.startswith("app-")
    elif scenario == "sad":
        # Same directory name, different projects: different log directories.
        assert s.default_log_dir(a) != s.default_log_dir(b)
    else:
        assert s.default_log_dir(Path("/")).name.startswith("root-")


def test_load_runtime(scenario):
    """The launcher and the in-sandbox runtime share helpers; the runtime never loads the launcher."""
    runtime = s.load_runtime()
    if scenario == "happy":
        assert runtime.base_env().keys() == s.base_env().keys() and s.LOG_DIR == runtime.LOG_DIR
    elif scenario == "sad":
        source = (ROOT / "sandbox/runtime.py").read_text()
        code = source.split('"""', 2)[2]
        # Standalone inside the sandbox: it never loads the launcher or any other sibling.
        assert "agent_sandbox.py" not in code and "spec_from_file_location" not in code
    else:
        assert runtime is not s.RUNTIME and runtime.ATTACH_SOCKET == s.ATTACH_SOCKET


def test_contract_manifest_covers_every_launcher_function():
    """New production functions must get the same three contract scenarios."""
    functions = set()
    for source in ("sandbox/agent_sandbox.py", "sandbox/runtime.py"):
        tree = ast.parse((ROOT / source).read_text())
        functions |= {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for function in sorted(functions):
        contract = globals().get("test_" + function)
        assert callable(contract), "Missing happy/sad/edge contract for " + function
        assert "scenario" in contract.__code__.co_varnames[:contract.__code__.co_argcount]


def test_check_image_variant(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    variant = tmp_path / "sandbox-ai" / "images" / "web"
    variant.mkdir(parents=True)
    (variant / "image.json").write_text(json.dumps({"version": 1, "agent": "claude", "apt": [], "npm": [], "pip": [],
                                                    "files": []}))
    if scenario == "happy":
        a = s.parse_args(["claude", "--image-variant", "web"])
        s.check_image_variant(a)
        assert a.image == "localhost/claude-sandbox-web:latest"
        s.check_image_variant(s.parse_args(["claude"]))  # nothing to check
    elif scenario == "sad":
        with pytest.raises(ValueError, match="variant of claude"):
            s.check_image_variant(s.parse_args(["codex", "--image-variant", "web"]))
        with pytest.raises(ValueError, match="no such variant"):
            s.check_image_variant(s.parse_args(["claude", "--image-variant", "gone"]))
    else:
        with pytest.raises(ValueError, match="exclude each other"):
            s.check_image_variant(s.parse_args(["claude", "--image-variant", "web", "--image", "localhost/x"]))
        with pytest.raises(ValueError, match="not a variant name"):
            s.check_image_variant(s.parse_args(["claude", "--image-variant", "../x"]))


# --- tmux by default, login store, write-back by token digest -------------------------------------


def test_build_parser(scenario):
    parser = s.build_parser()
    actions = parser._option_string_actions
    if scenario == "happy":
        assert actions["--tmux"].nargs == 0 and actions["--mount"].nargs is None
    elif scenario == "sad":
        with pytest.raises(SystemExit):
            parser.parse_args(["codex", "--tmu"])  # no abbreviations
    else:
        assert parser.parse_args(["codex", "--tmux", "--no-tmux"]).tmux is False  # the last one wins
        assert parser.parse_args(["codex"]).tmux is None


def test_resolve_tmux(scenario, monkeypatch):
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    if scenario == "happy":
        assert s.resolve_tmux(s.parse_args(["claude"])) is True
        assert s.resolve_tmux(s.parse_args(["claude", "--", "--model", "opus"])) is True
        assert s.resolve_tmux(s.parse_args(["claude", "--no-tmux"])) is False
    elif scenario == "sad":
        monkeypatch.setattr(s.sys.stdin, "isatty", lambda: False, raising=False)
        assert s.resolve_tmux(s.parse_args(["claude"])) is False
        assert s.resolve_tmux(s.parse_args(["claude", "--tmux"])) is True  # insisted on: checked later
    else:
        assert s.resolve_tmux(s.parse_args(["claude", "--", "-p", "task"])) is False
        assert s.resolve_tmux(s.parse_args(["claude", "--", "mcp", "list"])) is False
        assert s.resolve_tmux(s.parse_args(["claude", "--binary", "python3"])) is False
        assert s.resolve_tmux(s.parse_args(["codex", "--probe"])) is False
        assert s.resolve_tmux(s.parse_args(["codex", "--session", "x"])) is False
        monkeypatch.setattr(s.shutil, "which", lambda name: None)
        assert s.resolve_tmux(s.parse_args(["codex", "--backend", "bwrap"])) is False


def test_auth_store(scenario, monkeypatch, tmp_path):
    if scenario == "happy":
        assert s.auth_store("claude") == Path.home() / ".local/share/sandbox-ai/auth/claude"
    elif scenario == "sad":
        monkeypatch.setenv("XDG_DATA_HOME", "")
        assert s.auth_store() == Path.home() / ".local/share/sandbox-ai/auth"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
        assert s.auth_store("codex") == tmp_path / "d/sandbox-ai/auth/codex"


def test_store_login(scenario, tmp_path):
    store = s.auth_store("claude")
    if scenario == "happy":
        claude_credentials(store / ".claude/.credentials.json", 4102444800000)
        assert s.store_login("claude")
        (s.auth_store("codex") / ".codex").mkdir(parents=True)
        (s.auth_store("codex") / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk"}')
        assert s.store_login("codex")
        # Without the agent's structure it is no login, and the host's is used instead.
        (s.auth_store("codex") / ".codex/auth.json").write_text('{"x": 1}')
        assert not s.store_login("codex")
    elif scenario == "sad":
        assert not s.store_login("claude") and not s.store_login("opencode")
        (store / ".claude").mkdir(parents=True)
        (store / ".claude/.credentials.json").write_text('{"other": 1}')
        assert not s.store_login("claude")
    else:
        # A link in the store is never followed.
        claude_credentials(tmp_path / "elsewhere.json", 4102444800000)
        (store / ".claude").mkdir(parents=True)
        (store / ".claude/.credentials.json").symlink_to(tmp_path / "elsewhere.json")
        assert not s.store_login("claude")


def test_store_sources(scenario):
    store = s.auth_store({"happy": "claude", "sad": "codex", "edge": "opencode"}[scenario])
    expected = {"happy": [(store / ".claude/.credentials.json", ".claude/.credentials.json"),
                          (store / ".claude.json", ".claude.json")],
                "sad": [(store / ".codex/auth.json", ".codex/auth.json")],
                "edge": [(store / ".local/share/opencode/auth.json", ".local/share/opencode/auth.json")]}[scenario]
    assert s.store_sources(store.name) == expected


def test_oauth_token_digest(scenario):
    login = {"accessToken": "a", "refreshToken": "r", "expiresAt": 1}
    if scenario == "happy":
        digest = s.oauth_token_digest(login)
        assert len(digest) == 64 and "a" * 2 not in digest[:0]
        # Fields other than the tokens do not count.
        assert s.oauth_token_digest({**login, "scopes": ["x"]}) == digest
    elif scenario == "sad":
        assert s.oauth_token_digest(None) is None
    else:
        assert s.oauth_token_digest(login) != s.oauth_token_digest({**login, "refreshToken": "rotated"})


def test_claude_recover_writeback(scenario, tmp_path):
    home = tmp_path / "home"
    source, sandbox = tmp_path / "host/.credentials.json", home / ".claude/.credentials.json"
    soon, later = (time.time() + 3600) * 1000, (time.time() + 7200) * 1000
    claude_credentials(source, soon)
    claude_credentials(sandbox, soon)
    s.remember_sync("claude", home, s.oauth_token_digest(s.claude_oauth(source)))
    if scenario == "happy":
        # The earlier run refreshed (rotating the refresh token) and was killed before writing back.
        claude_credentials(sandbox, later, refresh="rotated")
        assert s.claude_recover_writeback(home, (source, sandbox)) is True
        assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "rotated"
    elif scenario == "sad":
        # The host logged in again since: its login wins, nothing is written back.
        claude_credentials(sandbox, later, refresh="rotated")
        claude_credentials(source, soon, refresh="host-relogin")
        assert s.claude_recover_writeback(home, (source, sandbox)) is False
        assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "host-relogin"
    else:
        # Nothing refreshed, or tokens claiming an implausible lifetime: nothing written.
        assert s.claude_recover_writeback(home, (source, sandbox)) is False
        claude_credentials(sandbox, (time.time() + 365 * 86400) * 1000, refresh="forged")
        assert s.claude_recover_writeback(home, (source, sandbox)) is False
        assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "refresh-fixture"


# --- the Claude login kept alive during a run: live sync with its source -------------------------------------


def test_claude_oauth_from(scenario):
    good = {"accessToken": "a", "refreshToken": "r", "expiresAt": 2000}
    if scenario == "happy":
        assert s.claude_oauth_from(json.dumps({"claudeAiOauth": good}).encode()) == good
    elif scenario == "sad":
        for data in (b"", b"[", b"\xff", b"[]", b'{"claudeAiOauth": []}', b'{"other": {}}',
                     json.dumps({"claudeAiOauth": {**good, "refreshToken": ""}}).encode(),
                     json.dumps({"claudeAiOauth": {**good, "expiresAt": True}}).encode(), b"[" * 100000):
            assert s.claude_oauth_from(data) is None
    else:
        # refreshTokenExpiresAt is optional, but must be a timestamp when present.
        assert s.claude_oauth_from(json.dumps({"claudeAiOauth": {**good, "refreshTokenExpiresAt": 9e12}}).encode())
        assert s.claude_oauth_from(json.dumps({"claudeAiOauth": {**good, "refreshTokenExpiresAt": "x"}}).encode()) is None


def test_merge_claude_login(scenario):
    new = {"accessToken": "a2", "refreshToken": "r2", "expiresAt": 3000, "scopes": ["user:inference"],
           "subscriptionType": "max", "unknown": "dropped"}
    if scenario == "happy":
        data = json.dumps({"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1", "expiresAt": 1000,
                                             "subscriptionType": "pro"}, "mcpOAuth": {"srv": {"x": 1}}}).encode()
        merged = json.loads(s.merge_claude_login(data, new))
        assert merged["mcpOAuth"] == {"srv": {"x": 1}}  # the rest of the file stays
        assert merged["claudeAiOauth"] == {"accessToken": "a2", "refreshToken": "r2", "expiresAt": 3000,
                                           "scopes": ["user:inference"], "subscriptionType": "pro"}
    elif scenario == "sad":
        for data in (None, b"", b"[1]", b"\xff", b"[" * 100000):
            assert json.loads(s.merge_claude_login(data, new)) == {"claudeAiOauth": {
                "accessToken": "a2", "refreshToken": "r2", "expiresAt": 3000, "scopes": ["user:inference"],
                "subscriptionType": "max"}}
    else:
        # A claudeAiOauth that is no object counts as none; mistyped token fields of NEW never cross.
        merged = json.loads(s.merge_claude_login(b'{"claudeAiOauth": "x"}', {**new, "expiresAt": "soon"}))
        assert "expiresAt" not in merged["claudeAiOauth"] and merged["claudeAiOauth"]["subscriptionType"] == "max"


def test_claude_login_lock(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "state/sandbox-ai/claude-login.lock"
    if scenario == "happy":
        with s.claude_login_lock():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        with s.claude_login_lock():  # released again
            pass
    elif scenario == "sad":
        fd = os.open(path.parent.mkdir(parents=True) or path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # another process holds it
            started = time.monotonic()
            with pytest.raises(TimeoutError, match="holds"):
                with s.claude_login_lock(timeout=0.2):
                    pytest.fail("got a lock that is held")
            assert time.monotonic() - started < 5
        finally:
            os.close(fd)
    else:
        # A symlink in its place is never followed (O_NOFOLLOW).
        path.parent.mkdir(parents=True)
        (tmp_path / "target").write_text("keep")
        path.symlink_to(tmp_path / "target")
        with pytest.raises(OSError):
            with s.claude_login_lock():
                pass
        assert (tmp_path / "target").read_text() == "keep"


def test_open_home_dir(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if scenario == "happy":
        fd = s.open_home_dir(home, ".claude/.credentials.json")
        try:
            assert os.path.samestat(os.fstat(fd), (home / ".claude").stat())
        finally:
            os.close(fd)
    elif scenario == "sad":
        shutil.rmtree(home / ".claude")
        (tmp_path / "elsewhere").mkdir()
        (home / ".claude").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ValueError, match="symlink"):
            s.open_home_dir(home, ".claude/.credentials.json")
        with pytest.raises(FileNotFoundError):
            s.open_home_dir(home, "missing/file")
    else:
        for relative in ("../outside", "", "a/../../b"):
            with pytest.raises(ValueError, match="outside"):
                s.open_home_dir(home, relative)
        fd = s.open_home_dir(home, "top-level-file")  # a file in HOME itself: HOME's descriptor
        os.close(fd)


def test_read_home_file(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    path = home / ".claude/.credentials.json"
    if scenario == "happy":
        path.write_text("{}")
        assert s.read_home_file(home, ".claude/.credentials.json") == b"{}"
    elif scenario == "sad":
        with pytest.raises(FileNotFoundError):
            s.read_home_file(home, ".claude/.credentials.json")
        (tmp_path / "secret").write_text("host file")
        path.symlink_to(tmp_path / "secret")
        with pytest.raises(ValueError, match="symlink"):
            s.read_home_file(home, ".claude/.credentials.json")
        path.unlink()
        os.mkfifo(path)  # never waited on
        with pytest.raises(ValueError, match="regular"):
            s.read_home_file(home, ".claude/.credentials.json")
    else:
        path.write_bytes(b"x" * 11)
        with pytest.raises(ValueError, match="larger"):
            s.read_home_file(home, ".claude/.credentials.json", limit=10)
        assert s.read_home_file(home, ".claude/.credentials.json", limit=11) == b"x" * 11


def test_replace_home_file(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    path = home / ".claude/.credentials.json"
    if scenario == "happy":
        path.write_text("old")
        path.chmod(0o644)
        s.replace_home_file(home, ".claude/.credentials.json", b"new")
        assert path.read_bytes() == b"new" and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sorted(p.name for p in path.parent.iterdir()) == [".credentials.json"]
    elif scenario == "sad":
        outside = tmp_path / "outside"
        outside.mkdir()
        shutil.rmtree(home / ".claude")
        (home / ".claude").symlink_to(outside)  # swapped while the agent runs
        with pytest.raises(ValueError, match="symlink"):
            s.replace_home_file(home, ".claude/.credentials.json", b"tokens")
        assert list(outside.iterdir()) == []
    else:
        # A link in the file's place is replaced, its target never written.
        (tmp_path / "target").write_text("keep")
        path.symlink_to(tmp_path / "target")
        s.replace_home_file(home, ".claude/.credentials.json", b"new")
        assert not path.is_symlink() and path.read_bytes() == b"new"
        assert (tmp_path / "target").read_text() == "keep"


def sync_fixture(tmp_path, monkeypatch, store=False):
    """A source (host file or login store) and a sandbox HOME that share one login, with the state of a start."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    home = tmp_path / "home"
    host = (s.auth_store("claude") / s.AUTH_TARGETS["claude"]) if store else tmp_path / "host/.claude/.credentials.json"
    sandbox = home / ".claude/.credentials.json"
    soon = (time.time() + 3600) * 1000
    claude_credentials(host, soon)
    claude_credentials(sandbox, soon)
    return home, (host, sandbox), s.claude_writeback_state((host, sandbox)), soon


def test_claude_login_sync(scenario, tmp_path, monkeypatch):
    home, pair, state, soon = sync_fixture(tmp_path, monkeypatch, store=scenario == "happy")
    host, sandbox = pair
    later = soon + 3600 * 1000
    assert s.claude_login_sync(home, pair, state) is None  # nothing changed
    if scenario == "happy":
        # The sandbox refreshed: the store gets the new pair at once, and the record of the HOME follows.
        claude_credentials(sandbox, later, refresh="rotated-1")
        assert s.claude_login_sync(home, pair, state) == "refreshed Claude credentials written back to " \
                                                         "sandbox-ai's login store"
        assert s.claude_oauth(host)["refreshToken"] == "rotated-1"
        assert s.load_login_sync()[s.sync_key("claude", home)] == s.oauth_token_digest(s.claude_oauth(host))
        # Then another sandbox refreshed and wrote back: this one takes it over before it refreshes itself.
        claude_credentials(host, later + 1000, refresh="rotated-2")
        document = json.loads(sandbox.read_text())
        sandbox.write_text(json.dumps({**document, "mcpOAuth": {"srv": {"x": 1}}}))
        assert "took over the newer Claude login" in s.claude_login_sync(home, pair, state)
        written = json.loads(sandbox.read_text())
        assert written["claudeAiOauth"]["refreshToken"] == "rotated-2" and written["mcpOAuth"] == {"srv": {"x": 1}}
        assert stat.S_IMODE(sandbox.stat().st_mode) == 0o600
        assert s.claude_login_sync(home, pair, state) is None  # in step again
    elif scenario == "sad":
        # Implausible (a year ahead) or not newer than the source: never written back, and not pushed over either.
        claude_credentials(sandbox, (time.time() + 365 * 86400) * 1000, refresh="forged")
        assert s.claude_login_sync(home, pair, state) is None
        assert s.claude_login_sync(home, pair, state) is None
        assert s.claude_oauth(host)["refreshToken"] == "refresh-fixture"
        claude_credentials(sandbox, soon - 1000, refresh="older")
        assert s.claude_login_sync(home, pair, state) is None
        assert s.claude_oauth(host)["refreshToken"] == "refresh-fixture"
        # A source without a login never gets one; a source that lost its login is left alone.
        empty = {"host": None, "host_login": False, "tokens": None}
        claude_credentials(sandbox, later, refresh="rotated")
        assert s.claude_login_sync(home, pair, empty) is None
        host.write_text("{}")
        assert s.claude_login_sync(home, pair, state) is None and host.read_text() == "{}"
    else:
        # Both refreshed (a new login on the host during the run): the source wins and goes into the sandbox.
        claude_credentials(sandbox, later, refresh="rotated")
        claude_credentials(host, soon, refresh="host-relogin")
        assert "changed during the run; kept that one" in s.claude_login_sync(home, pair, state)
        assert s.claude_oauth(host)["refreshToken"] == "host-relogin"
        assert s.claude_oauth(sandbox)["refreshToken"] == "host-relogin"
        # The agent swapped ~/.claude for a link to a host directory: nothing is read or written through it.
        outside = tmp_path / "outside"
        outside.mkdir()
        claude_credentials(outside / ".credentials.json", later + 5000, refresh="host-secret")
        shutil.rmtree(home / ".claude")
        (home / ".claude").symlink_to(outside)
        claude_credentials(host, soon, refresh="host-again")
        assert s.claude_login_sync(home, pair, state) is None
        assert s.claude_oauth(host)["refreshToken"] == "host-again"
        assert s.claude_oauth(outside / ".credentials.json")["refreshToken"] == "host-secret"


def test_watch_claude_login(scenario, tmp_path, monkeypatch):
    home, pair, state, soon = sync_fixture(tmp_path, monkeypatch)
    host, sandbox = pair
    stop = threading.Event()
    calls = []
    real = s.claude_login_sync

    def counting(*args):
        calls.append(1)
        if scenario == "sad":
            raise OSError("disk full")
        return real(*args)
    monkeypatch.setattr(s, "claude_login_sync", counting)
    watcher = threading.Thread(target=s.watch_claude_login, args=(home, pair, state, stop, 0.01))
    watcher.start()
    try:
        deadline = time.monotonic() + 10
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        if scenario == "happy":
            claude_credentials(sandbox, soon + 1000, refresh="rotated")
            deadline = time.monotonic() + 10  # its own: a loaded machine may have used up the first one
            while s.claude_oauth(host)["refreshToken"] != "rotated" and time.monotonic() < deadline:
                time.sleep(0.01)
            assert s.claude_oauth(host)["refreshToken"] == "rotated"
        elif scenario == "edge":
            # Nothing changes: one look at the files, then lstat only.
            time.sleep(0.2)
            assert len(calls) == 1
    finally:
        stop.set()
        watcher.join(10)
    assert not watcher.is_alive()
    if scenario == "happy":
        assert state["notes"] == ["refreshed Claude credentials written back to the host"]
    elif scenario == "sad":
        # Errors never end the watcher or the run; each is said once the run is over.
        assert set(state["notes"]) == {"could not sync the Claude login during the run: disk full"}
        assert len(calls) == 1
    else:
        assert state["notes"] == []


def test_claude_sync_notes(scenario):
    if scenario == "happy":
        assert s.claude_sync_notes({"notes": ["a", "b", "a"]}) == ["a (2 times during the run)", "b (during the run)"]
    elif scenario == "sad":
        assert s.claude_sync_notes(None) == [] and s.claude_sync_notes({}) == []
    else:
        assert s.claude_sync_notes({"notes": []}) == []

def test_sandbox_digest(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    if scenario == "happy":
        (home / ".codex/auth.json").write_text("{}")
        assert s.sandbox_digest(home, ".codex/auth.json") == hashlib.sha256(b"{}").hexdigest()
    elif scenario == "sad":
        assert s.sandbox_digest(home, ".codex/auth.json") is None
    else:
        (tmp_path / "x").write_text("{}")
        (home / ".codex/auth.json").symlink_to(tmp_path / "x")
        assert s.sandbox_digest(home, ".codex/auth.json") is None


def test_write_private_file(scenario, tmp_path):
    path = tmp_path / "f.json"
    if scenario == "sad":
        path.symlink_to(tmp_path / "target")
        with pytest.raises(ValueError, match="symlink"):
            s.write_private_file(path, b"x")
        assert not (tmp_path / "target").exists()
        return
    if scenario == "edge":
        path.write_text("old")
        path.chmod(0o644)
    s.write_private_file(path, b"new")
    assert path.read_bytes() == b"new" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".auth-*"))  # no temporary file left


def test_login_state(scenario, monkeypatch):
    host = Path.home() / ".claude/.credentials.json"
    if scenario == "happy":
        claude_credentials(host, 4102444800000)
        assert s.login_state("claude") == ("host", None)
        claude_credentials(s.auth_store("claude") / ".claude/.credentials.json", 4102444800000)
        assert s.login_state("claude") == ("store", None)
    elif scenario == "sad":
        assert s.login_state("claude") == ("host", "missing") and s.login_state("codex") == ("host", "missing")
        store_codex_login()
        assert s.login_state("codex") == ("store", None)
    else:
        host.parent.mkdir(parents=True)
        login = {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000, "refreshTokenExpiresAt": 2000}
        host.write_text(json.dumps({"claudeAiOauth": login}))
        assert s.login_state("claude") == ("host", "expired")


def test_check_login(scenario, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    host = Path.home() / ".claude/.credentials.json"
    login = Mock(return_value=0)
    monkeypatch.setattr(s, "load_login_module", lambda: Mock(login=login))
    if scenario == "happy":
        claude_credentials(host, 4102444800000)
        s.check_login(s.parse_args(["claude"]))
        assert capsys.readouterr().err == "" and not login.called
        # Missing: a hint only, no question.
        s.check_login(s.parse_args(["codex"]))
        assert "sandbox-ai login codex" in capsys.readouterr().err and not login.called
        return
    host.parent.mkdir(parents=True)
    expired = {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000, "refreshTokenExpiresAt": 2000}
    host.write_text(json.dumps({"claudeAiOauth": expired}))
    if scenario == "sad":
        # Expired in a terminal: the user is asked and the login runs inside a sandbox.
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        s.check_login(s.parse_args(["claude", "--backend", "bwrap"]))
        assert login.call_args[0] == ("claude", ["--backend", "bwrap"])
        login.return_value = 1
        with pytest.raises(ValueError, match="Login failed"):
            s.check_login(s.parse_args(["claude"]))
    else:
        # No question without a terminal, for --no-auth, dry runs or a HOME that has a login.
        monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("asked")))
        for args in (["claude", "--no-auth"], ["claude", "--dry-run"]):
            s.check_login(s.parse_args(args))
        state = tmp_path / "state"
        claude_credentials(state / ".claude/.credentials.json", 4102444800000)
        s.check_login(s.parse_args(["claude", "--state-dir", str(state)]))
        monkeypatch.setattr(s.sys.stdin, "isatty", lambda: False, raising=False)
        s.check_login(s.parse_args(["claude"]))
        assert "expired claude login" in capsys.readouterr().err and not login.called


def test_load_login_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_login_module()
    else:
        module = s.load_login_module()
        assert callable(module.login) and module.LOGIN_ARGS["claude"] == ["auth", "login"]
        if scenario == "edge":
            assert s.load_login_module() is not module


def test_interactive_args(scenario):
    if scenario == "happy":
        assert s.interactive_args("claude", []) and s.interactive_args("claude", ["--model", "opus", "--continue"])
        assert s.interactive_args("codex", ["-m", "gpt-5", "--search"])
    elif scenario == "sad":
        for agent, args in (("claude", ["-p", "x"]), ("claude", ["--version"]), ("claude", ["mcp", "list"]),
                            ("codex", ["exec", "x"]), ("codex", ["login", "status"]), ("opencode", ["run", "x"]),
                            ("opencode", ["models"])):
            assert not s.interactive_args(agent, args), (agent, args)
    else:
        # An option we do not know, followed by a word: that word may be a subcommand; no tmux.
        assert not s.interactive_args("claude", ["--unknown", "value"])
        assert not s.interactive_args("claude", ["first message"])


def store_codex_login(refresh="r1", account="acct-1", **extra):
    path = s.auth_store("codex") / ".codex/auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"OPENAI_API_KEY": None, "tokens": {"id_token": "i", "access_token": "a",
                                                                  "refresh_token": refresh, "account_id": account},
                                "last_refresh": "t0", **extra}))
    return path


def test_login_document_ok(scenario):
    if scenario == "happy":
        assert s.login_document_ok("codex", {"tokens": {"access_token": "a", "refresh_token": "r", "account_id": "x"}})
        assert s.login_document_ok("codex", {"OPENAI_API_KEY": "sk-1"})
        assert s.login_document_ok("opencode", {"anthropic": {"type": "oauth", "refresh": "r"}})
    elif scenario == "sad":
        for agent, document in (("codex", {"x": 1}), ("codex", []), ("opencode", {}), ("opencode", {"a": "b"}),
                                ("codex", {"tokens": {"refresh_token": "r"}})):
            assert not s.login_document_ok(agent, document)
    else:
        assert not s.login_document_ok("codex", {"OPENAI_API_KEY": ""})


def test_stored_document(scenario, tmp_path):
    path = tmp_path / "auth.json"
    if scenario == "happy":
        path.write_text('{"OPENAI_API_KEY": "sk"}')
        assert s.stored_document("codex", path) == {"OPENAI_API_KEY": "sk"}
    elif scenario == "sad":
        path.write_text("{broken")
        assert s.stored_document("codex", path) is None
        assert s.stored_document("codex", tmp_path / "missing") is None
    else:
        (tmp_path / "real").write_text('{"OPENAI_API_KEY": "sk"}')
        path.symlink_to(tmp_path / "real")
        assert s.stored_document("codex", path) is None


def test_merge_refreshed(scenario):
    stored = {"OPENAI_API_KEY": None, "last_refresh": "t0",
              "tokens": {"id_token": "i", "access_token": "a", "refresh_token": "r1", "account_id": "acct"}}
    if scenario == "happy":
        sandbox = {"OPENAI_API_KEY": None, "last_refresh": "t1",
                   "tokens": {"id_token": "i2", "access_token": "a2", "refresh_token": "r2", "account_id": "acct"}}
        merged = s.merge_refreshed("codex", stored, sandbox)
        assert merged["tokens"]["refresh_token"] == "r2" and merged["last_refresh"] == "t1"
        oc = {"anthropic": {"type": "oauth", "refresh": "r1", "access": "a1", "expires": 1, "accountId": "x"}}
        new = {"anthropic": {"type": "oauth", "refresh": "r2", "access": "a2", "expires": 2, "accountId": "x"}}
        assert s.merge_refreshed("opencode", oc, new)["anthropic"]["refresh"] == "r2"
    elif scenario == "sad":
        # Review finding: the agent must not plant an API key or another account for every later sandbox.
        planted = {"OPENAI_API_KEY": "sk-attacker", "x": 1,
                   "tokens": {"access_token": "evil", "refresh_token": "evil", "account_id": "attacker"}}
        assert s.merge_refreshed("codex", stored, planted) is None
        assert s.merge_refreshed("codex", stored, {"OPENAI_API_KEY": "sk-attacker", "tokens": stored["tokens"]}) is None
        oc = {"openai": {"type": "api", "key": "sk-real"}}
        assert s.merge_refreshed("opencode", oc, {"openai": {"type": "api", "key": "sk-evil"},
                                                  "evil": {"type": "api", "key": "x"}}) is None
        assert s.merge_refreshed("codex", stored, "not a dict") is None
    else:
        assert s.merge_refreshed("codex", stored, json.loads(json.dumps(stored))) is None  # nothing new
        assert s.merge_refreshed("codex", {"OPENAI_API_KEY": "sk"}, {"OPENAI_API_KEY": "sk2"}) is None


def test_login_sync_store(scenario, monkeypatch, tmp_path):
    if scenario == "edge":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        assert s.login_sync_store() == tmp_path / "st/sandbox-ai/login-sync.json"
    else:
        assert s.login_sync_store() == Path.home() / ".local/state/sandbox-ai/login-sync.json"


def test_load_login_sync(scenario):
    store = s.login_sync_store()
    if scenario == "sad":
        assert s.load_login_sync() == {}
        return
    store.parent.mkdir(parents=True)
    store.write_text('{"claude:/x": "d"}' if scenario == "happy" else "[1]")
    assert s.load_login_sync() == ({"claude:/x": "d"} if scenario == "happy" else {})


def test_sync_key(scenario, tmp_path):
    home = tmp_path / {"happy": "h", "sad": "x/../h", "edge": "a:b"}[scenario]
    assert s.sync_key("codex", home) == f"codex:{home.resolve()}"


def test_remember_sync(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    if scenario == "sad":
        s.remember_sync("claude", home, None)  # nothing to forget: no file is created
        assert not s.login_sync_store().exists()
        return
    s.remember_sync("claude", home, "d1")
    assert s.load_login_sync() == {s.sync_key("claude", home): "d1"}
    if scenario == "happy":
        s.remember_sync("claude", home, None)
        assert s.load_login_sync() == {}
    else:
        # Entries of HOMEs that no longer exist are dropped.
        shutil.rmtree(home)
        other = tmp_path / "other"
        other.mkdir()
        s.remember_sync("codex", other, "d2")
        assert s.load_login_sync() == {s.sync_key("codex", other): "d2"}


def test_remember_sync_locked(scenario, tmp_path):
    homes = [tmp_path / f"home{i}" for i in range(12)]
    for home in homes:
        home.mkdir()
    store = s.login_sync_store()
    store.parent.mkdir(parents=True, exist_ok=True)
    if scenario == "happy":
        s.remember_sync_locked("claude", homes[0], "d0", store)
        assert s.load_login_sync() == {s.sync_key("claude", homes[0]): "d0"}
    elif scenario == "sad":
        # A store that cannot be written: nothing is left behind (no .login-sync-* leftovers).
        store.parent.chmod(0o500)
        try:
            s.remember_sync_locked("claude", homes[0], "d0", store)
        finally:
            store.parent.chmod(0o700)
        assert not list(store.parent.glob(".login-sync-*"))
    else:
        # Runs starting and ending at once: every HOME's entry is kept (remember_sync takes the lock).
        threads = [threading.Thread(target=s.remember_sync, args=("claude", home, f"d{i}"))
                   for i, home in enumerate(homes)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert s.load_login_sync() == {s.sync_key("claude", home): f"d{i}" for i, home in enumerate(homes)}


def test_claude_recover_writeback_never_after_an_unshared_run(tmp_path):
    """Review finding: a --no-auth run on the HOME (another account) must not be taken for a missed
    write-back on the next start."""
    home, host = tmp_path / "home", Path.home() / ".claude/.credentials.json"
    soon = (time.time() + 3600) * 1000
    claude_credentials(host, soon)
    a = s.parse_args(["claude", "--state-dir", str(home)])
    home.mkdir(mode=0o700)
    s.prepare_home(a, home)
    assert s.sync_key("claude", home) in s.load_login_sync()
    s.prepare_home(s.parse_args(["claude", "--no-auth", "--state-dir", str(home)]), home)
    assert s.sync_key("claude", home) not in s.load_login_sync()
    claude_credentials(home / ".claude/.credentials.json", soon + 3600_000, refresh="other-account")
    s.prepare_home(a, home)
    assert json.loads(host.read_text())["claudeAiOauth"]["refreshToken"] == "refresh-fixture"


def test_store_writeback_state(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    a = s.parse_args(["codex"])
    if scenario == "sad":
        assert s.store_writeback_state(a, home) is None  # no stored login
        return
    source = store_codex_login()
    if scenario == "happy":
        shutil.copy(source, home / ".codex/auth.json")
        before = s.store_writeback_state(a, home)
        assert before["source"] == source and before["store"] == before["sandbox"]
        assert s.store_writeback_state(s.parse_args(["codex", "--no-auth"]), home) is None
    else:
        # A persistent HOME with a login of its own never writes it into the store.
        (home / ".codex/auth.json").write_text('{"tokens": {"refresh_token": "own"}}')
        assert s.store_writeback_state(a, home) is None


def test_store_writeback(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    source = store_codex_login()
    shutil.copy(source, home / ".codex/auth.json")
    a = s.parse_args(["codex"])
    before = s.store_writeback_state(a, home)
    refreshed = json.loads(source.read_text())
    refreshed["tokens"]["refresh_token"] = "r2"
    if scenario == "happy":
        assert s.store_writeback(home, a, before) is None  # unchanged
        (home / ".codex/auth.json").write_text(json.dumps(refreshed))
        assert "written back" in s.store_writeback(home, a, before)
        assert json.loads(source.read_text())["tokens"]["refresh_token"] == "r2"
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
    elif scenario == "sad":
        # Review finding: a planted API key or another account never reaches the store.
        (home / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk-attacker", "x": 1}')
        assert s.store_writeback(home, a, before) is None
        assert "sk-attacker" not in source.read_text() and s.store_login("codex")
        store_codex_login(refresh="other-run")
        (home / ".codex/auth.json").write_text(json.dumps(refreshed))
        assert "kept the stored one" in s.store_writeback(home, a, before)
        assert "other-run" in source.read_text()
    else:
        shutil.rmtree(home / ".codex")
        (home / ".codex").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="symlink"):
            s.store_writeback(home, a, before)


def test_sync_store_login(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    a = s.parse_args(["codex", "--state-dir", str(home)])
    if scenario == "sad":
        s.sync_store_login(a, home)  # no stored login: nothing recorded
        assert s.load_login_sync() == {}
        return
    store_codex_login()
    s.prepare_home(a, home)  # copied from the store and recorded
    assert s.load_login_sync()[s.sync_key("codex", home)] == s.credential_digest(s.auth_store("codex") / ".codex/auth.json")
    if scenario == "happy":
        # Another sandbox refreshed into the store: this HOME's tokens are used up, take over the new ones.
        store_codex_login(refresh="r2")
        s.sync_store_login(a, home)
        assert json.loads((home / ".codex/auth.json").read_text())["tokens"]["refresh_token"] == "r2"
        assert "took over the newer stored login" in capsys.readouterr().err
    else:
        # The HOME changed its login itself: kept, and no longer followed.
        (home / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk-own"}')
        store_codex_login(refresh="r2")
        s.sync_store_login(a, home)
        assert "sk-own" in (home / ".codex/auth.json").read_text()
        assert s.sync_key("codex", home) not in s.load_login_sync()


def test_check_login_prompt_keys(scenario, monkeypatch, capsys):
    """Review finding: Ctrl-D or Ctrl-C at the question gave a traceback; commands that print and exit
    are never asked."""
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    host = Path.home() / ".claude/.credentials.json"
    host.parent.mkdir(parents=True)
    host.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000,
                                                  "refreshTokenExpiresAt": 2000}}))
    login = Mock(return_value=0)
    monkeypatch.setattr(s, "load_login_module", lambda: Mock(login=login))
    if scenario == "happy":
        monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))
        s.check_login(s.parse_args(["claude"]))  # Ctrl-D: no, start anyway with the hint
        assert "sandbox-ai login claude" in capsys.readouterr().err
    elif scenario == "sad":
        monkeypatch.setattr("builtins.input", Mock(side_effect=KeyboardInterrupt))
        with pytest.raises(SystemExit) as exc:
            s.check_login(s.parse_args(["claude"]))
        assert exc.value.code == 130
    else:
        monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("asked")))
        s.check_login(s.parse_args(["claude", "--", "-p", "task"]))
    assert not login.called


# --- kits, project memory and plugins ---------------------------------------------------------------

def make_kit(root, files):
    """Files below ROOT from {relative path: text}; returns ROOT."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def kit_args(tmp_path, agent="claude", *options):
    a = s.parse_args([agent, "--workspace", str(tmp_path / "work"), "--no-auth", *options])
    s.validate(a)
    return a


def data_home():
    return Path(os.environ["HOME"]) / ".local/share/sandbox-ai"


def test_load_kits_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_kits_module()
    else:
        module = s.load_kits_module()
        assert module.LAYOUT["claude"]["skills"] == ".claude/skills" and callable(module.scan)
        if scenario == "edge":
            assert s.load_kits_module() is not module


def test_load_privacy_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_privacy_module()
    else:
        module = s.load_privacy_module()
        assert callable(module.scan_repository) and module.builtin_rules().patterns
        if scenario == "edge":
            assert s.load_privacy_module() is not module


def test_privacy_hooks_apply(scenario, tmp_path):
    if scenario == "happy":
        assert s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path)]))
    elif scenario == "sad":
        assert not s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path),
                                                       "--privacy-check", "off"]))
    else:
        # Probes and MCP servers commit nothing.
        assert not s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path), "--probe"]))
        assert s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path),
                                                   "--privacy-check", "always"]))


def test_privacy_rules_dir(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    if scenario == "happy":
        rules = tmp_path / "rules"
        args = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--privacy-rules", str(rules)])
        assert s.privacy_rules_dir(args) == rules
    elif scenario == "sad":
        assert s.privacy_rules_dir(s.parse_args(["claude", "--workspace", str(tmp_path / "w")])) is None
    else:
        default = tmp_path / "config" / "sandbox-ai" / "privacy"
        default.mkdir(parents=True)
        assert s.privacy_rules_dir(s.parse_args(["claude", "--workspace", str(tmp_path / "w")])) == default
        # Rules the agent could change or read would be no rules: refused inside a workspace.
        args = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--privacy-rules",
                             str(tmp_path / "w" / "r")])
        with pytest.raises(ValueError, match="--privacy-rules"):
            s.check_host_sources(args)


def test_privacy_context(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    options = {"happy": ["--allow-tcp", "192.168.50.10:1234", "--git-token-env",
                         "http://alice@localhost:3080=CONTRACT_GIT"],
               "sad": [], "edge": ["--allow-tcp", "[fd00::5]:11434"]}[scenario]
    context = s.privacy_context(s.parse_args(["claude", "--workspace", str(tmp_path), *options]))
    assert str(Path.home()) in context["homes"]
    if scenario == "happy":
        assert context["addresses"] == ["192.168.50.10"]
        assert context["servers"] == [{"host": "localhost", "port": 3080, "user": "alice"}]
        assert context["tokens"] == [GIT_TOKEN]
    elif scenario == "sad":
        assert context["addresses"] == context["servers"] == context["tokens"] == []
    else:
        assert context["addresses"] == ["fd00::5"]


def test_privacy_workspaces(scenario, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    if scenario == "happy":
        args = s.parse_args(["claude", "--workspace", f"{first},{second}"])
        assert s.privacy_workspaces(args) == [(first, "first"), (second, "second")]
    elif scenario == "sad":
        # A read-only workspace cannot have changed.
        assert s.privacy_workspaces(s.parse_args(["claude", "--workspace", str(first), "--workspace-ro"])) == []
    else:
        (tmp_path / "link").symlink_to(first)
        assert s.privacy_workspaces(s.parse_args(["claude", "--workspace", str(tmp_path / "link")])) == \
            [(first.resolve(), "first")]


def test_added_workspaces_of_run(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s, "runs_dir", lambda: tmp_path)
    run = tmp_path / "r1"
    run.mkdir()
    args = type("A", (), {"run_id": "r1"})()
    if scenario == "happy":
        (run / "workspaces.json").write_text(json.dumps({"workspaces": [
            {"host": "/home/me/docs", "mode": "rw", "state": "removed"},
            {"host": "/home/me/docs", "mode": "rw", "state": "attached"}]}))
        assert s.added_workspaces_of_run(args) == [Path("/home/me/docs")]
    elif scenario == "sad":
        (run / "workspaces.json").write_text("{broken")
        assert s.added_workspaces_of_run(args) == []
    else:
        # Read-only ones cannot have changed; a path that is not absolute is never trusted.
        (run / "workspaces.json").write_text(json.dumps({"workspaces": [{"host": "/x", "mode": "ro"},
                                                                         {"host": "x", "mode": "rw"}, "junk"]}))
        assert s.added_workspaces_of_run(args) == []


def test_prepare_privacy(scenario, tmp_path, monkeypatch):
    workspace, session = tmp_path / "proj", tmp_path / "session"
    workspace.mkdir()
    session.mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    options = ["claude", "--workspace", str(workspace), "--privacy-rules", str(rules)]
    if scenario == "sad":
        (rules / "proj.rules.json").write_text('{"pattern": [{"id": "x", "regex": "(", "severity": "block", '
                                               '"reason": "r"}]}')
        with pytest.raises(ValueError, match="uncompilable"):
            s.prepare_privacy(s.parse_args(options), session)
        assert s.prepare_privacy(s.parse_args([*options, "--privacy-check", "off"]), session) is None
        return
    (rules / "global.rules.json").write_text(json.dumps({"pattern": [
        {"id": "codename", "regex": r"\bbluefin\b", "severity": "block", "reason": "internal codename"}]}))
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    check = s.prepare_privacy(s.parse_args([*options, "--git-token-env", "http://alice@localhost:3080=CONTRACT_GIT"]),
                              session)
    hook = json.loads((session / "privacy" / "hook.json").read_text())
    # Your own rules and the tokens stay on the host; the agent's hooks know the rest.
    assert "bluefin" not in json.dumps(hook) and GIT_TOKEN not in json.dumps(hook)
    assert {p["id"] for p in hook["rules"]["pattern"]} >= {"host-home", "own-git-account"}
    assert hook["own_hosts"] == ["localhost"]
    assert (session / "privacy" / "privacy.py").read_bytes() == (ROOT / "sandbox/privacy.py").read_bytes()
    if scenario == "edge" and shutil.which("git"):
        # After the run: a workspace with a public remote and an unpushed secret is reported.
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/o/r.git"],
                       check=True)
        (workspace / "notes.txt").write_text("project bluefin\n")
        printed = []
        monkeypatch.setattr(sys, "stderr", Mock(write=printed.append))
        check()
        text = "".join(printed)
        assert "1 blocking" in text and "notes.txt:1 (untracked) [codename]" in text


def test_kits_apply(scenario, tmp_path):
    if scenario == "happy":
        assert s.kits_apply(s.parse_args(["claude", "--workspace", str(tmp_path)]))
    elif scenario == "sad":
        assert not s.kits_apply(s.parse_args(["claude", "--workspace", str(tmp_path), "--probe"]))
    else:
        # MCP server sandboxes run a server, not an agent: no skills, memory or plugins.
        assert not s.kits_apply(s.parse_args(["codex", "--workspace", str(tmp_path),
                                              "--mcp-socket-dir", str(tmp_path / "sock")]))


def test_kit_managed_paths(scenario, tmp_path):
    from pathlib import PurePosixPath as P
    if scenario == "happy":
        paths = s.kit_managed_paths(s.parse_args(["claude", "--workspace", str(tmp_path)]))
        assert {P(".claude/skills"), P(".claude/CLAUDE.md"), P(".claude/projects/-work/memory"),
                P(".claude/plugins/cache")} <= set(paths)
    elif scenario == "sad":
        assert s.kit_managed_paths(s.parse_args(["codex", "--workspace", str(tmp_path), "--no-default-kit",
                                                 "--no-memory", "--no-plugins"])) == []
        assert s.kit_managed_paths(s.parse_args(["codex", "--workspace", str(tmp_path), "--probe"])) == []
    else:
        # Without the default kit, a --kit still fills the agent's directories; memory alone otherwise.
        assert P(".agents/skills") in s.kit_managed_paths(
            s.parse_args(["codex", "--workspace", str(tmp_path), "--no-default-kit", "--kit", "x"]))
        assert s.kit_managed_paths(s.parse_args(["opencode", "--workspace", str(tmp_path), "--no-default-kit"])) \
            == [P(".config/opencode/AGENTS.md")]


def test_kit_plan(scenario, tmp_path):
    make_kit(data_home() / "kits/review/claude", {"skills/review/SKILL.md": "---\nname: review\n---\n",
                                                   "commands/check.md": "check"})
    if scenario == "happy":
        plan = s.kit_plan(kit_args(tmp_path, "claude", "--kit", "review"))
        assert plan["entries"] == {"skills": ["review", "sandbox-ai"], "commands": ["check.md"]}
        assert plan["memory"].startswith(str(Path(os.environ["HOME"]) / ".local/state/sandbox-ai/memory/work-"))
        assert any("created with examples on the first start" in note for note in plan["notes"])
        assert not (data_home() / "home").exists()  # planning changes nothing
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--kit missing: .* does not exist .*kits for claude: egm, review"):
            s.kit_plan(kit_args(tmp_path, "claude", "--kit", "missing"))
        assert s.kit_plan(kit_args(tmp_path, "claude", "--probe")) is None
    else:
        make_kit(data_home() / "kits/other/claude", {"skills/review/SKILL.md": "x"})
        with pytest.raises(ValueError, match="skill 'review' comes from both kit review and kit other"):
            s.kit_plan(kit_args(tmp_path, "claude", "--kit", "review", "--kit", "other"))
        plan = s.kit_plan(kit_args(tmp_path, "claude", "--kit", "review", "--no-memory", "--no-plugins"))
        assert plan["memory"] is None and "plugins" not in plan


def test_ensure_mount_point(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    if scenario == "happy":
        s.ensure_mount_point(home, ".claude/skills")
        assert (home / ".claude/skills").is_dir()
    elif scenario == "sad":
        (tmp_path / "elsewhere").mkdir()
        (home / ".claude").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ValueError, match="symlink"):
            s.ensure_mount_point(home, ".claude/skills")
        assert not (tmp_path / "elsewhere/skills").exists()
    else:
        (home / ".codex").mkdir()
        (home / ".codex/prompts").write_text("a file the agent left")
        with pytest.raises(ValueError, match="not a directory"):
            s.ensure_mount_point(home, ".codex/prompts")
        s.ensure_mount_point(home, ".codex")  # existing directory: fine


def plugin_store_fixture(installed=("sandcastle@sandbox-ai",), known=("sandbox-ai",)):
    store = data_home() / "plugins/claude/.claude/plugins"
    (store / "cache/sandbox-ai/sandcastle/1.0.0").mkdir(parents=True)
    (store / "marketplaces").mkdir()
    (store / "known_marketplaces.json").write_text(json.dumps(
        {name: {"source": {"source": "directory", "path": "/sandbox/marketplace"},
                "installLocation": "/sandbox/marketplace"} for name in known}))
    (store / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        plugin: [{"scope": "user", "installPath": "/root/.claude/plugins/cache/x", "version": "1.0.0"}]
        for plugin in installed}}))
    return store


def test_claude_plugins(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marketplace = {"sandbox-ai": {"source": "directory", "path": "/sandbox/marketplace"}}
    notes = []
    if scenario == "happy":
        store = plugin_store_fixture(installed=("sandcastle@sandbox-ai", "other@sandbox-ai"))
        mounts = s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes)
        assert mounts == [(store / "cache", "/root/.claude/plugins/cache", "ro"),
                          (store / "marketplaces", "/root/.claude/plugins/marketplaces", "ro")]
        settings = json.loads((home / ".claude/settings.json").read_text())
        # Installed for another kit, so off in this run.
        assert settings["enabledPlugins"] == {"sandcastle@sandbox-ai": True, "other@sandbox-ai": False}
        assert settings["extraKnownMarketplaces"] == {"sandbox-ai": {"source": marketplace["sandbox-ai"]}}
        known = json.loads((home / ".claude/plugins/known_marketplaces.json").read_text())
        assert known["sandbox-ai"]["autoUpdate"] is False
        assert notes == []
    elif scenario == "sad":
        # Nothing installed yet: nothing mounted or enabled, and a hint how to install.
        assert s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes) == []
        assert "sandbox-ai plugins claude" in notes[0] and "sandcastle@sandbox-ai" in notes[0]
        assert json.loads((home / ".claude/settings.json").read_text())["enabledPlugins"] == {}
    else:
        plugin_store_fixture()
        # A reused HOME whose settings are not JSON is left alone, with a note.
        (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text("{broken")
        s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes)
        assert (home / ".claude/settings.json").read_text() == "{broken"
        assert any(note.startswith("plugins not enabled") for note in notes)


def test_claude_drop_plugins(scenario, tmp_path):
    """--no-plugins after a run with plugins on the same HOME: nothing of that run enables a plugin."""
    home = tmp_path / "home"
    home.mkdir()
    marketplace = {"sandbox-ai": {"source": "directory", "path": "/sandbox/marketplace"}}
    notes = []
    if scenario == "happy":
        plugin_store_fixture()
        settings = home / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash"]}}))
        s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes)
        a = kit_args(tmp_path, "claude", "--no-plugins", "--no-memory", "--state-dir", str(tmp_path / "state"))
        mounts, notes, _ = s.prepare_kits(a, home, tmp_path / "staging")
        assert not any("/root/.claude/plugins" in dest for _, dest, _ in mounts)
        assert json.loads(settings.read_text()) == {"model": "opus", "permissions": {"allow": ["Bash"]}}
        assert sorted(os.listdir(home / ".claude/plugins")) == ["cache", "marketplaces"]  # empty mount points
    elif scenario == "sad":
        # A fresh HOME gets no settings file; an unreadable one is left as it is, with a note.
        s.claude_drop_plugins(home, notes)
        assert not (home / ".claude").exists() and notes == []
        (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text("{broken")
        s.claude_drop_plugins(home, notes)
        assert (home / ".claude/settings.json").read_text() == "{broken" and "may stay enabled" in notes[0]
    else:
        # Planted symlinks are not followed: neither the settings nor the index outside the HOME change.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "settings.json").write_text(json.dumps({"enabledPlugins": {"x@y": True}}))
        (outside / "installed_plugins.json").write_text("{}")
        (home / ".claude").mkdir()
        (home / ".claude/plugins").symlink_to(outside)
        (home / ".claude/settings.json").symlink_to(outside / "settings.json")
        with pytest.raises(ValueError, match="symlink"):
            s.claude_drop_plugins(home, notes)
        assert (outside / "installed_plugins.json").exists() and "left in place" in notes[0]
        assert json.loads((outside / "settings.json").read_text()) == {"enabledPlugins": {"x@y": True}}


def test_prepare_kits(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    if scenario == "happy":
        a = kit_args(tmp_path, "claude")
        mounts, notes, memory = s.prepare_kits(a, home, tmp_path / "staging")
        # First start: the default kit is created from the templates, its skill mounted read-only.
        assert (data_home() / "home/claude/skills/sandbox-example/SKILL.md").is_file()
        assert ((tmp_path / "staging/skills"), "/root/.claude/skills", "ro") in mounts
        assert (s.load_kits_module().MARKETPLACE, "/sandbox/marketplace", "ro") in mounts
        assert (tmp_path / "staging/skills/sandbox-example/SKILL.md").is_file()
        assert (home / ".claude/skills").is_dir()  # the mount point
        # The memory template was taken once into the new project memory and the HOME.
        assert (home / ".claude/CLAUDE.md").read_text().startswith("# Project notes")
        assert set(memory) == {".claude/CLAUDE.md"}
        assert any(note.startswith("created the default kit") for note in notes)
        assert any(note.startswith("new project memory") for note in notes)
    elif scenario == "sad":
        make_kit(data_home() / "kits/bad/claude", {"marketplaces.json": json.dumps(
            {"marketplaces": {"x": {"source": {"source": "directory", "path": "/etc"}}}})})
        a = kit_args(tmp_path, "claude", "--kit", "bad")
        with pytest.raises(ValueError, match="directory source can only be sandbox-ai's own"):
            s.prepare_kits(a, home, tmp_path / "staging")
        assert s.prepare_kits(kit_args(tmp_path, "claude", "--probe"), home, tmp_path / "s2") == ([], [], None)
    else:
        # OpenCode: npm plugins go into OPENCODE_CONFIG_CONTENT, with a note about the npm registry.
        make_kit(data_home() / "home/opencode", {"marketplaces.json": json.dumps({"plugins": ["opencode-foo@1.2.3"]})})
        a = kit_args(tmp_path, "opencode", "--no-memory")
        mounts, notes, memory = s.prepare_kits(a, home, tmp_path / "staging")
        assert [dest for _, dest, _ in mounts] == ["/root/.config/opencode/skills"] and memory is None
        assert json.loads(s.agent_environment(a)["OPENCODE_CONFIG_CONTENT"]) == {"plugin": ["opencode-foo@1.2.3"],
                                                                                 **s.OPENCODE_TELEMETRY_CONFIG}
        assert any("registry.npmjs.org" in note for note in notes)


def test_save_memory(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    a = kit_args(tmp_path, "claude", "--no-default-kit")
    manifest, _, store, _ = s.load_kits_module().memory_prepare("claude", a.workspaces[0], home)
    memory = home / ".claude/projects/-work/memory"
    if scenario == "happy":
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text("- builds with make")
        s.save_memory(a, home, manifest)
        assert (store / ".claude/projects/-work/memory/MEMORY.md").read_text() == "- builds with make"
        assert "1 file(s) kept" in capsys.readouterr().err
    elif scenario == "sad":
        memory.mkdir(parents=True)
        (memory / "leak").symlink_to("/etc/passwd")
        s.save_memory(a, home, manifest)
        assert not (store / ".claude/projects/-work/memory/leak").exists()
        assert "leak skipped: symlink" in capsys.readouterr().err
    else:
        # Nothing changed: nothing written, nothing printed.
        s.save_memory(a, home, manifest)
        assert capsys.readouterr().err == ""
        assert sorted(p.name for p in store.rglob("*")) == []
        assert not list(store.parent.glob(".pending-*"))  # the run is recorded as saved


# --- host devices (--device, --gpu) ---------------------------------------------------------------


@pytest.fixture
def fake_dev(tmp_path, monkeypatch):
    """A /dev of regular files that device_mode presents as devices: "sda*" as block devices,
    everything else as character devices."""
    dev = tmp_path / "dev"
    for name in ("ttyACM0", "kvm", "kfd", "video0", "console", "null", "hidraw0", "sda", "nvidia0", "nvidiactl",
                 "nvidia-uvm", "bus/usb/001/004", "dri/renderD128", "snd/pcmC0D0c", "input/event0"):
        (dev / name).parent.mkdir(parents=True, exist_ok=True)
        (dev / name).write_text("")
        (dev / name).chmod(0o666)
    (dev / "serial/by-id").mkdir(parents=True)
    (dev / "serial/by-id/usb-Board").symlink_to("../../ttyACM0")
    (dev / "escape").symlink_to(tmp_path)
    real_mode = s.device_mode

    def mode(path):
        path = Path(path)
        if path.is_dir():
            return stat.S_IFDIR | 0o755
        if path.name.startswith("sda"):
            return stat.S_IFBLK | 0o660
        if dev in path.parents:
            return stat.S_IFCHR | 0o666
        return real_mode(path)

    monkeypatch.setattr(s, "DEV", dev)
    monkeypatch.setattr(s, "device_mode", mode)
    monkeypatch.setattr(s, "CDI_DIRS", (tmp_path / "cdi",))
    return dev


def test_device_mode(scenario, tmp_path):
    if scenario == "happy":
        assert stat.S_ISCHR(s.device_mode("/dev/null"))
    elif scenario == "sad":
        with pytest.raises(OSError):
            s.device_mode(tmp_path / "missing")
    else:
        assert stat.S_ISDIR(s.device_mode(tmp_path))


def test_parse_device(scenario, fake_dev, tmp_path):
    if scenario == "happy":
        assert s.parse_device(str(fake_dev / "ttyACM0")) == (str(fake_dev / "ttyACM0"), "node")
        # Symlinks are resolved: the agent sees the device under its real name.
        assert s.parse_device(str(fake_dev / "serial/by-id/usb-Board")) == (str(fake_dev / "ttyACM0"), "node")
        assert s.parse_device(str(fake_dev / "bus/usb")) == (str(fake_dev / "bus/usb"), "dir")
        assert s.parse_device(str(fake_dev / "bus/usb/001")) == (str(fake_dev / "bus/usb/001"), "dir")
    elif scenario == "sad":
        cases = {"console": "consoles", "null": "already", "hidraw0": "keyboards", "input/event0": "keyboards",
                 "sda": "block devices", "serial": "directories", "escape": "not to a device",
                 "missing": "No such file"}
        for name, message in cases.items():
            with pytest.raises(ValueError, match=message):
                s.parse_device(str(fake_dev / name))
        for value in ("", "relative/ttyACM0", "/etc/passwd", f"{fake_dev}/ttyACM0:rw", f"{fake_dev}/a,b"):
            with pytest.raises(ValueError, match="--device expects"):
                s.parse_device(value)
    else:
        # The option name in messages follows the caller; a file outside /dev is no device.
        (fake_dev / "plain").write_text("")
        with pytest.raises(ValueError, match="--gpu dri"):
            s.parse_device(str(fake_dev / "console"), "--gpu dri")
        assert s.parse_device(str(fake_dev / "dri")) == (str(fake_dev / "dri"), "dir")


def test_nvidia_cdi_available(scenario, fake_dev, tmp_path):
    cdi = tmp_path / "cdi"
    if scenario == "happy":
        cdi.mkdir()
        (cdi / "nvidia.yaml").write_text("cdiVersion: 0.5.0\nkind: nvidia.com/gpu\n")
        assert s.nvidia_cdi_available()
    elif scenario == "sad":
        assert not s.nvidia_cdi_available()  # no directory at all
    else:
        cdi.mkdir()
        (cdi / "notes.txt").write_text("nvidia.com/gpu")  # not a specification
        (cdi / "other.json").write_text('{"kind": "vendor.example/fpga"}')
        assert not s.nvidia_cdi_available()


def test_gpu_devices(scenario, fake_dev, tmp_path):
    if scenario == "happy":
        assert s.gpu_devices(s.parse_args(["codex"])) == []
        assert s.gpu_devices(s.parse_args(["codex", "--gpu", "dri"])) == [(str(fake_dev / "dri"), "dir")]
        assert s.gpu_devices(s.parse_args(["codex", "--gpu", "rocm"])) == [(str(fake_dev / "dri"), "dir"),
                                                                          (str(fake_dev / "kfd"), "node")]
        (tmp_path / "cdi").mkdir()
        (tmp_path / "cdi" / "nvidia.yaml").write_text("kind: nvidia.com/gpu\n")
        assert s.gpu_devices(s.parse_args(["codex", "--gpu", "nvidia"])) == [("nvidia.com/gpu=all", "cdi")]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="nvidia-ctk cdi generate"):
            s.gpu_devices(s.parse_args(["codex", "--gpu", "nvidia"]))
        (fake_dev / "kfd").unlink()
        with pytest.raises(ValueError, match="kfd"):
            s.gpu_devices(s.parse_args(["codex", "--gpu", "rocm"]))
        for name in ("nvidia0", "nvidiactl", "nvidia-uvm"):
            (fake_dev / name).unlink()
        with pytest.raises(ValueError, match="NVIDIA driver"):
            s.gpu_devices(s.parse_args(["codex", "--backend", "bwrap", "--gpu", "nvidia"]))
    else:
        # bwrap gets the device nodes (the host's /usr brings the libraries); a remote Podman its own CDI.
        assert s.gpu_devices(s.parse_args(["codex", "--backend", "bwrap", "--gpu", "nvidia"])) == [
            (str(fake_dev / name), "node") for name in ("nvidia-uvm", "nvidia0", "nvidiactl")]
        remote = s.parse_args(["codex", "--gpu", "nvidia", "--podman-url", "unix:///example/podman.sock"])
        assert s.gpu_devices(remote) == [("nvidia.com/gpu=all", "cdi")]


def test_devices(scenario, fake_dev):
    if scenario == "happy":
        a = s.parse_args(["codex", "--device", str(fake_dev / "ttyACM0"), "--device", str(fake_dev / "kvm")])
        assert s.devices(a) == [(str(fake_dev / "ttyACM0"), "node"), (str(fake_dev / "kvm"), "node")]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="consoles"):
            s.devices(s.parse_args(["codex", "--device", str(fake_dev / "kvm"), "--device", str(fake_dev / "console")]))
    else:
        # Repeats and devices inside a directory passed whole are dropped.
        a = s.parse_args(["codex", "--device", str(fake_dev / "serial/by-id/usb-Board"), "--device",
                          str(fake_dev / "ttyACM0"), "--device", str(fake_dev / "dri/renderD128"), "--gpu", "dri"])
        assert s.devices(a) == [(str(fake_dev / "ttyACM0"), "node"), (str(fake_dev / "dri"), "dir")]


def device_ruleset(*rules, defaults=None):
    """a.rulesets with one ruleset of device RULES."""
    document = {"version": 1, "devices": list(rules), **({"defaults": {"devices": defaults}} if defaults else {})}
    return [{"name": "mine", **s.RUNTIME.Ruleset.validated(document, "mine")}]


def plugged_pico(tmp_path, monkeypatch, dev):
    """A Raspberry Pi Pico in a fake sysfs, its nodes in DEV (tests/test_devices.py)."""
    from test_devices import FakeUsb
    usb = FakeUsb(tmp_path, monkeypatch, devices=s.load_devices_module(), launcher=None, dev=dev)
    usb.plug()
    return usb


def test_device_entries(scenario, fake_dev, tmp_path, monkeypatch):
    if scenario == "happy":
        entries = s.device_entries(s.parse_args(["codex", "--device", str(fake_dev / "serial/by-id/usb-Board")]))
        assert [(e["value"], e["path"], e["kind"]) for e in entries] == [
            (str(fake_dev / "serial/by-id/usb-Board"), str(fake_dev / "ttyACM0"), "node")]
        assert entries[0]["facts"]["names"] == ["/dev/ttyACM0", "/dev/serial/by-id/usb-Board"]
    elif scenario == "sad":
        plugged_pico(tmp_path, monkeypatch, fake_dev)
        with pytest.raises(ValueError, match="no such USB device"):
            s.device_entries(s.parse_args(["codex", "--device", "usb:0403:6001"]))
    else:
        plugged_pico(tmp_path, monkeypatch, fake_dev)
        entries = s.device_entries(s.parse_args(["codex", "--device", "usb:2e8a:000a", "--gpu", "dri"]))
        assert [e["path"] for e in entries] == [str(fake_dev / "bus/usb/001/005"), str(fake_dev / "ttyACM0"),
                                                str(fake_dev / "dri")]
        assert entries[0]["device"]["serial"] == "E6609CB2" and entries[-1]["value"] == "--gpu dri"


def test_launcher_view(scenario, fake_dev):
    view = s.launcher_view()
    if scenario == "happy":
        assert view.parse_device(str(fake_dev / "kvm")) == (str(fake_dev / "kvm"), "node")
    elif scenario == "sad":
        with pytest.raises(ValueError, match="consoles"):
            view.parse_device(str(fake_dev / "console"))
    else:
        assert view.DEV == fake_dev  # what a caller replaced is seen


def test_check_device_rules(scenario, fake_dev):
    a = s.parse_args(["codex", "--device", str(fake_dev / "kvm"), "--device", str(fake_dev / "ttyACM0")])
    if scenario == "happy":
        a.rulesets = device_ruleset({"id": "kvm", "decision": "allow", "node": "/dev/kvm"})
        s.check_device_rules(a)
        assert a.device_verdicts[str(fake_dev / "kvm")]["rule"] == "kvm"
        assert a.device_verdicts[str(fake_dev / "ttyACM0")]["rule"] is None
        a.rulesets = []
        s.check_device_rules(a)
        assert a.device_verdicts == {}  # no device rules: nothing judged
    elif scenario == "sad":
        a.rulesets = device_ruleset({"id": "no-tty", "decision": "deny", "node": "/dev/ttyACM*", "reason": "no"})
        with pytest.raises(ValueError, match=r"ttyACM0: no$"):
            s.check_device_rules(a)
    else:
        a.rulesets = device_ruleset({"id": "ask-kvm", "decision": "ask", "node": "/dev/kvm"})
        s.check_device_rules(a)
        assert s.devices(a) == [(str(fake_dev / "ttyACM0"), "node")]
        assert s.devices(a, held=True) == [(str(fake_dev / "kvm"), "node")]
        a.ask = "off"
        with pytest.raises(ValueError, match="--ask is off"):
            s.check_device_rules(a)


def test_device_grants(scenario, fake_dev, tmp_path, monkeypatch):
    if scenario == "happy":
        a = s.parse_args(["codex", "--device", str(fake_dev / "kvm")])
        grant, = s.device_grants(a)
        assert grant["value"] == str(fake_dev / "kvm") and grant["state"] == "attached" and grant["by"] == "path"
        assert grant["source"] == "start" and not grant["track"] and grant["decision"] == "allow"
    elif scenario == "sad":
        assert s.device_grants(s.parse_args(["codex"])) == []
    else:
        plugged_pico(tmp_path, monkeypatch, fake_dev)
        a = s.parse_args(["codex", "--device", "usb:2e8a:000a"])
        a.rulesets = device_ruleset({"id": "ask-raw", "decision": "ask", "subsystem": "usb"})
        s.check_device_rules(a)
        grant, = s.device_grants(a)
        assert grant["state"] == "held" and grant["nodes"] == {} and grant["track"] and grant["rule"] == "ask-raw"
        assert grant["pending"] == [str(fake_dev / "bus/usb/001/005"), str(fake_dev / "ttyACM0")]


def test_load_devices_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "_devices_module", None)
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_devices_module()
    else:
        module = s.load_devices_module()
        assert callable(module.plan) and callable(module.watch_devices)
        if scenario == "edge":
            assert s.load_devices_module() is module  # loaded once: what tests replace in it holds


def test_start_devices(scenario, fake_dev, tmp_path, monkeypatch):
    a = s.parse_args(["codex"] + (["--device", str(fake_dev / "kvm")] if scenario == "happy" else
                                  ["--device", "usb:2e8a:000a"] if scenario == "edge" else []))
    a.control_dir = tmp_path / "run-dir"
    a.control_dir.mkdir()
    (a.control_dir / "run.json").write_text(json.dumps({"run": "codex-1", "pid": os.getpid(), "log_dir": None}))
    a.rulesets = device_ruleset({"id": "kvm", "decision": "allow", "node": "/dev/kvm"})
    if scenario == "happy":
        assert s.start_devices(a) is None  # nothing on USB to follow
        grants = json.loads((a.control_dir / "devices.json").read_text())["grants"]
        assert grants[0]["id"] == "1" and grants[0]["state"] == "attached"
        rules = json.loads((a.control_dir / "device-rules.json").read_text())
        assert rules[0]["devices"][0]["id"] == "kvm" and stat.S_IMODE(
            (a.control_dir / "device-rules.json").stat().st_mode) == 0o600
    elif scenario == "sad":
        a.rulesets = []
        assert s.start_devices(a) is None and not (a.control_dir / "devices.json").exists()
        assert json.loads((a.control_dir / "device-rules.json").read_text()) == []
    else:
        plugged_pico(tmp_path, monkeypatch, fake_dev)
        record = s.start_devices(a)
        assert record["directory"] == str(a.control_dir) and record["run"] == "codex-1"
        assert json.loads((a.control_dir / "devices.json").read_text())["grants"][0]["track"] is True


def acl(*entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def test_device_access(scenario, tmp_path, monkeypatch):
    device = tmp_path / "ttyACM0"
    device.write_text("")
    uid = os.getuid()

    def getxattr(path, name, value=None):
        if value is None:
            raise OSError(61, "No data available")
        return value

    if scenario == "happy":
        device.chmod(0o600)
        assert s.device_access(device)
        # udev's uaccess: a named ACL entry for this user, limited by the mask.
        device.chmod(0o000)
        monkeypatch.setattr(os, "getxattr", lambda path, name: acl((1, 0, 0xFFFFFFFF), (2, 6, uid), (4, 0, 0xFFFFFFFF),
                                                                  (0x10, 6, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)))
        monkeypatch.setattr(os, "stat", lambda path: Mock(st_uid=uid + 1, st_gid=os.getgid() + 1, st_mode=0o20000))
        assert s.device_access(device)
    elif scenario == "sad":
        # Only the group could open it, and the sandbox keeps no supplementary groups.
        monkeypatch.setattr(os, "getxattr", lambda path, name: getxattr(path, name))
        monkeypatch.setattr(os, "stat", lambda path: Mock(st_uid=uid + 1, st_gid=os.getgid() + 1, st_mode=0o20660))
        assert not s.device_access(device)
        # A named entry masked down to read only.
        monkeypatch.setattr(os, "getxattr", lambda path, name: acl((1, 6, 0xFFFFFFFF), (2, 6, uid), (4, 0, 0xFFFFFFFF),
                                                                  (0x10, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)))
        assert not s.device_access(device)
    else:
        # Others may: the device is open to everyone; an ACL in an unknown version counts as none.
        monkeypatch.setattr(os, "getxattr", lambda path, name: struct.pack("<I", 9))
        monkeypatch.setattr(os, "stat", lambda path: Mock(st_uid=uid + 1, st_gid=os.getgid() + 1, st_mode=0o20666))
        assert s.device_access(device)


def test_group_name(scenario):
    if scenario == "happy":
        assert s.group_name(0) == "root"
    elif scenario == "sad":
        assert s.group_name(2 ** 31 - 7) == str(2 ** 31 - 7)
    else:
        assert s.group_name(os.getgid()) == s.grp.getgrgid(os.getgid()).gr_name


def test_device_notes(scenario, fake_dev, monkeypatch):
    if scenario == "happy":
        notes = s.device_notes(s.parse_args(["codex", "--device", str(fake_dev / "ttyACM0"), "--device",
                                             str(fake_dev / "bus/usb")]))
        assert notes[0] == (f"devices for the agent (their drivers run in your host's kernel): {fake_dev}/ttyACM0, "
                            f"{fake_dev}/bus/usb (directory: devices plugged in later appear too)")
        assert len(notes) == 2 and "security keys" in notes[1]
        assert s.device_notes(s.parse_args(["codex"])) == []
    elif scenario == "sad":
        monkeypatch.setattr(s, "device_access", lambda path: False)
        notes = s.device_notes(s.parse_args(["codex", "--device", str(fake_dev / "ttyACM0"), "--device",
                                             str(fake_dev / "dri")]))
        assert "cannot open" in notes[1] and "ttyACM0" in notes[1] and "only through group" in notes[1]
        assert "any device in" in notes[2] and "udev rule" in notes[2]
    else:
        notes = s.device_notes(s.parse_args(["codex", "--device", str(fake_dev / "video0"), "--device",
                                             str(fake_dev / "snd")]))
        assert any("camera" in note for note in notes) and any("microphones" in note for note in notes)


# --- workspaces trusted without asking -------------------------------------------------------------


def test_claude_trusted_workspaces(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    a = s.parse_args(["claude", "--workspace", f"{tmp_path / 'app'},{tmp_path / 'lib'}"])
    if scenario == "happy":
        s.claude_trusted_workspaces(a, home)
        projects = json.loads((home / ".claude.json").read_text())["projects"]
        assert projects == {"/work": {"hasTrustDialogAccepted": True},
                            "/workspaces/lib": {"hasTrustDialogAccepted": True}}
    elif scenario == "sad":
        (home / ".claude.json").write_text("{not json")
        s.claude_trusted_workspaces(a, home)
        assert (home / ".claude.json").read_text() == "{not json"
        assert "may ask whether you trust" in capsys.readouterr().err
    else:
        # Other keys of a project and other projects stay; a second run changes nothing.
        (home / ".claude.json").write_text(json.dumps({"projects": {"/work": {"history": [1]}, "/old": {}}}))
        s.claude_trusted_workspaces(a, home)
        before = (home / ".claude.json").stat().st_mtime_ns
        s.claude_trusted_workspaces(a, home)
        projects = json.loads((home / ".claude.json").read_text())["projects"]
        assert projects["/work"] == {"history": [1], "hasTrustDialogAccepted": True} and projects["/old"] == {}
        assert (home / ".claude.json").stat().st_mtime_ns == before


def test_codex_trusted_workspaces(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    a = s.parse_args(["codex", "--workspace", f"{tmp_path / 'app'},{tmp_path / 'lib'}"])
    config = home / ".codex/config.toml"
    if scenario == "happy":
        s.codex_trusted_workspaces(a, home)
        assert config.read_text() == ('[projects."/work"]\ntrust_level = "trusted"\n\n'
                                      '[projects."/workspaces/lib"]\ntrust_level = "trusted"\n')
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
    elif scenario == "sad":
        config.symlink_to(tmp_path / "elsewhere.toml")
        with pytest.raises(ValueError, match="symlink"):
            s.codex_trusted_workspaces(a, home)
        assert not (tmp_path / "elsewhere.toml").exists()
    else:
        # Existing settings and an explicit decision for a workspace are kept.
        config.write_text('model = "gpt-5"\n\n[projects.\'/work\']\ntrust_level = "untrusted"\n')
        s.codex_trusted_workspaces(a, home)
        assert config.read_text() == ('model = "gpt-5"\n\n[projects.\'/work\']\ntrust_level = "untrusted"\n\n'
                                      '[projects."/workspaces/lib"]\ntrust_level = "trusted"\n')
        s.codex_trusted_workspaces(a, home)
        assert config.read_text().count("/workspaces/lib") == 1


# --- connection proxy (runtime.py) and its launcher side -------------------------------------------

rt = s.RUNTIME
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
OID_A, OID_B = "a" * 40, "b" * 40


def client_hello(name):
    """A real ClientHello with SNI NAME, taken from Python's TLS client."""
    context = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_run_openssl(scenario, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    if scenario == "happy":
        rt.run_openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out",
                        str(tmp_path / "k.pem")])
        assert (tmp_path / "k.pem").read_text().startswith("-----BEGIN PRIVATE KEY")
    elif scenario == "sad":
        with pytest.raises(RuntimeError, match="openssl genpkey failed"):
            rt.run_openssl(["genpkey", "-algorithm", "NOPE"])
    else:
        # The configuration's $ENV::SAN comes from the san argument.
        (tmp_path / "c.cnf").write_text("x = $ENV::SAN\n[req]\ndistinguished_name=dn\n[dn]\n")
        rt.run_openssl(["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout",
                        str(tmp_path / "k"), "-subj", "/CN=x", "-config", str(tmp_path / "c.cnf"), "-out",
                        str(tmp_path / "r")], san="DNS:x")


@needs_openssl
def test_create_ca(scenario, tmp_path):
    bundle = tmp_path / "system.crt"
    bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----\n")
    if scenario == "happy":
        rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=(str(tmp_path / "missing"), str(bundle)))
        ca = (tmp_path / "ca" / "ca.pem").read_bytes()
        assert (tmp_path / "ca" / "bundle.pem").read_bytes() == bundle.read_bytes() + ca
        assert (tmp_path / "run" / "system.pem").read_bytes() == bundle.read_bytes()
        assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "ca" / "bundle.pem").stat().st_mode) == 0o644
        text = subprocess.run(["openssl", "x509", "-in", str(tmp_path / "ca" / "ca.pem"), "-noout", "-text"],
                              capture_output=True, text=True).stdout
        assert "CA:TRUE, pathlen:0" in text and "this run only" in text
    elif scenario == "sad":
        (tmp_path / "run").write_text("a file")
        with pytest.raises(OSError):
            rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
    else:
        # Without a system bundle the agent's bundle is the session CA alone.
        rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
        assert (tmp_path / "ca" / "bundle.pem").read_bytes() == (tmp_path / "ca" / "ca.pem").read_bytes()


@needs_openssl
def test_server_certificate(scenario, tmp_path):
    rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
    run = tmp_path / "run"
    if scenario == "happy":
        path = rt.server_certificate(run, "API.Example.org.")
        assert path.name == "api.example.org.pem"
        assert rt.server_certificate(run, "api.example.org") == path  # made once
        verify = subprocess.run(["openssl", "verify", "-CAfile", str(tmp_path / "ca" / "ca.pem"), str(path)],
                                capture_output=True, text=True)
        assert verify.returncode == 0
    elif scenario == "sad":
        for name in ("../../etc/passwd", "a b", "x" * 300, "-subj"):
            with pytest.raises(ValueError, match="not a host name"):
                rt.server_certificate(run, name)
    else:
        path = rt.server_certificate(run, "2001:db8::1")
        text = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-ext", "subjectAltName"],
                              capture_output=True, text=True).stdout
        assert "IP Address:2001:DB8:0:0:0:0:0:1" in text.upper().replace("IP ADDRESS", "IP Address")


def test_client_hello_sni(scenario):
    hello = client_hello("api.example.org")
    if scenario == "happy":
        assert rt.client_hello_sni(hello) == ("tls", "api.example.org")
    elif scenario == "sad":
        assert rt.client_hello_sni(b"GET / HTTP/1.1\r\n") == ("other", None)
        assert rt.client_hello_sni(b"SSH-2.0-x") == ("other", None)
        assert rt.client_hello_sni(b"\x16\x03\x01\x00\x05\x02\x00\x00\x01\x00") == ("tls", None)  # not a ClientHello
    else:
        assert rt.client_hello_sni(hello[:3]) == ("partial", None)
        assert rt.client_hello_sni(hello[:-1]) == ("partial", None)
        assert rt.client_hello_sni(b"") == ("other", None)


def test_original_destination(scenario):
    if scenario == "happy":
        sock = Mock(family=socket.AF_INET)
        sock.getsockopt.return_value = struct.pack("!HH4s8x", socket.AF_INET, 443, socket.inet_aton("192.0.2.7"))
        assert rt.original_destination(sock) == ("192.0.2.7", 443)
        assert sock.getsockopt.call_args.args == (socket.SOL_IP, 80, 16)
    elif scenario == "sad":
        sock = Mock(family=socket.AF_INET)
        sock.getsockopt.side_effect = OSError(2, "No such file or directory")  # not redirected
        with pytest.raises(OSError):
            rt.original_destination(sock)
    else:
        sock = Mock(family=socket.AF_INET6)
        sock.getsockopt.return_value = (struct.pack("!HHI", socket.AF_INET6, 8443, 0)
                                        + socket.inet_pton(socket.AF_INET6, "2001:db8::5") + b"\0" * 4)
        assert rt.original_destination(sock) == ("2001:db8::5", 8443)


def test_allowed_destination(scenario):
    allow = {"nets": ["192.0.2.0/24"], "https": ["198.51.100.4/32"], "tcp": [["203.0.113.9", 5432]],
             "loopback": [3000]}
    if scenario == "happy":
        assert rt.allowed_destination(allow, "192.0.2.200", 22)
        assert rt.allowed_destination(allow, "198.51.100.4", 443)
        assert rt.allowed_destination(allow, "203.0.113.9", 5432)
        assert rt.allowed_destination(allow, "127.0.0.1", 3000)
    elif scenario == "sad":
        assert not rt.allowed_destination(allow, "198.51.100.4", 80)
        assert not rt.allowed_destination(allow, "203.0.113.9", 5433)
        assert not rt.allowed_destination(allow, "127.0.0.1", 15001)  # the proxy itself
        assert not rt.allowed_destination({}, "192.0.2.1", 443)
    else:
        # Everything outside, but loopback only where forwarded.
        assert rt.allowed_destination({"all": True}, "2001:db8::1", 1)
        assert not rt.allowed_destination({"all": True}, "::1", 22)


def test_redact_headers(scenario):
    if scenario == "happy":
        assert rt.redact_headers([["Authorization", "Bearer x"], ["X-Api-Key", "k"], ["Accept", "*/*"]]) == [
            ["Authorization", "[redacted]"], ["X-Api-Key", "[redacted]"], ["Accept", "*/*"]]
    elif scenario == "sad":
        assert rt.redact_headers([["Cookie", "a=b"], ["Set-Cookie", "s=1"], ["Proxy-Authorization", "x"]]) == [
            ["Cookie", "[redacted]"], ["Set-Cookie", "[redacted]"], ["Proxy-Authorization", "[redacted]"]]
    else:
        # Other headers keep their value, minus anything that looks like a token.
        assert rt.redact_headers([["Referer", "https://x/?access_token=abc"]]) == [
            ["Referer", "https://x/?access_token=[redacted]"]]
        assert rt.redact_headers([]) == []


def test_redact_text(scenario):
    if scenario == "happy":
        text = '{"refreshToken": "r-1", "api_key": "k", "model": "m"}'
        assert rt.redact_text(text) == '{"refreshToken": "[redacted]", "api_key": "[redacted]", "model": "m"}'
        assert rt.redact_text("grant_type=x&code=abc&client_secret=s") == "grant_type=x&code=[redacted]&client_secret=[redacted]"
    elif scenario == "sad":
        token = "ghp_" + "Z" * 36
        assert token not in rt.redact_text(f"push with {token} now")
        key = "sk-" + "ant-api03-" + "abcdefghijklmnop"  # assembled, so that no scanner takes the file for a leak
        assert key not in rt.redact_text(f"key {key}")
        assert rt.redact_text("Authorization: Bearer abcdefghijkl") == "Authorization: Bearer [redacted]"
    else:
        assert rt.redact_text("") == "" and rt.redact_text("plain words") == "plain words"
        assert rt.redact_text('{"password": "a\\"b"}') == '{"password": "[redacted]"}'


def test_body_record(scenario):
    if scenario == "happy":
        assert rt.body_record(b'{"a": 1}', 8, [], 100) == {"size": 8, "truncated": False, "text": '{"a": 1}'}
        packed = gzip.compress(b"hello world")
        assert rt.body_record(packed, len(packed), [["Content-Encoding", "gzip"]], 100)["text"] == "hello world"
    elif scenario == "sad":
        record = rt.body_record(b"\x00\x01\xff", 3, [], 100)
        assert record["base64"] == base64.b64encode(b"\x00\x01\xff").decode()
        assert rt.body_record(b"xx", 2, [["Content-Encoding", "br"]], 100)["encoding"] == "br"
        assert rt.body_record(b"", 0, [], 100) is None
    else:
        # Cut in the middle of a character: the text ends before it; the size is the whole body's.
        record = rt.body_record("aé".encode()[:2], 3, [], 2)
        assert record == {"size": 3, "truncated": True, "text": "a"}


def test_pkt_lines(scenario):
    if scenario == "happy":
        assert rt.pkt_lines(b"0009hello0000PACK") == ([b"hello"], True)
    elif scenario == "sad":
        with pytest.raises(ValueError):
            rt.pkt_lines(b"zzzzhello")
        with pytest.raises(ValueError):
            rt.pkt_lines(b"0002")
    else:
        assert rt.pkt_lines(b"0009hel") == ([], False)
        assert rt.pkt_lines(b"0009hello") == ([b"hello"], False)
        assert rt.pkt_lines(b"0000") == ([], True)


def test_push_commands(scenario):
    first = f"{OID_A} {OID_B} refs/heads/main\0report-status side-band-64k\n".encode()
    if scenario == "happy":
        assert rt.push_commands([first, f"{'0' * 40} {OID_A} refs/tags/v1\n".encode()]) == (
            [(OID_A, OID_B, "refs/heads/main"), ("0" * 40, OID_A, "refs/tags/v1")], {"report-status", "side-band-64k"})
    elif scenario == "sad":
        for lines in ([], [b"push-cert\0report-status\n"], [f"{OID_A} xyz refs/heads/main\n".encode()]):
            with pytest.raises(ValueError):
                rt.push_commands(lines)
    else:
        assert rt.push_commands([b"shallow " + OID_A.encode(), first])[0] == [(OID_A, OID_B, "refs/heads/main")]


def test_advertised_refs(scenario):
    if scenario == "happy":
        body = (rt.pkt(b"# service=git-receive-pack\n") + b"0000" + rt.pkt(f"{OID_A} refs/heads/main\0caps\n".encode())
                + rt.pkt(f"{OID_B} refs/tags/v1\n".encode()) + b"0000")
        assert rt.advertised_refs(body) == {OID_A, OID_B}
    elif scenario == "sad":
        assert rt.advertised_refs(b"<html>not git</html>") == set()
    else:
        # An empty repository advertises the zero id.
        empty = rt.pkt(f"{'0' * 40} capabilities^{{}}\0report-status\n".encode()) + b"0000"
        assert rt.advertised_refs(empty) == set()


def test_pkt(scenario):
    if scenario == "happy":
        assert rt.pkt(b"hello") == b"0009hello"
    elif scenario == "sad":
        assert rt.pkt(b"") == b"0004"
    else:
        assert rt.pkt(b"x" * 65516)[:4] == b"fff0"


def test_push_rejection(scenario):
    commands = [(OID_A, OID_B, "refs/heads/main")]
    if scenario == "happy":
        response = rt.push_rejection(commands, "line one\nline two", {"report-status", "side-band-64k"})
        head, _, body = response.partition(b"\r\n\r\n")
        assert b"application/x-git-receive-pack-result" in head and b"Content-Length: %d" % len(body) in head
        assert b"\x02sandbox-ai: line one\n" in body and b"\x02sandbox-ai: line two\n" in body
        assert b"ng refs/heads/main refused by the sandbox-ai privacy check" in body and body.endswith(b"0000")
    elif scenario == "sad":
        body = rt.push_rejection(commands, "why", {"report-status"}).partition(b"\r\n\r\n")[2]
        assert body.startswith(rt.pkt(b"unpack ok\n")) and b"why" not in body  # no side band for messages
    else:
        body = rt.push_rejection(commands * 3, "m", {"side-band"}).partition(b"\r\n\r\n")[2]
        assert body.count(b"ng refs/heads/main") == 3


def test_parse_head(scenario):
    if scenario == "happy":
        assert rt.parse_head(b"GET /x HTTP/1.1\r\nHost: a\r\nX-Y:  z \r\n\r\n") == ("GET /x HTTP/1.1",
                                                                                 [["Host", "a"], ["X-Y", "z"]])
    elif scenario == "sad":
        for head in (b"GET / HTTP/1.1\r\nno colon\r\n\r\n", b"GET / HTTP/1.1\r\n Folded: x\r\n\r\n"):
            with pytest.raises(ValueError):
                rt.parse_head(head)
    else:
        assert rt.parse_head(b"HTTP/1.1 204 No Content\r\n\r\n") == ("HTTP/1.1 204 No Content", [])


def test_header(scenario):
    headers = [["Content-Length", "1"], ["X", "a"], ["x", "b"]]
    if scenario == "happy":
        assert rt.header(headers, "content-length") == "1"
    elif scenario == "sad":
        assert rt.header(headers, "missing") is None
    else:
        assert rt.header(headers, "x") == "b"  # the last one


def test_send_all(scenario):
    left, right = socket.socketpair()
    try:
        if scenario == "happy":
            rt.send_all(left, b"x" * 100000)
            data = b""
            while len(data) < 100000:
                data += right.recv(65536)
            assert data == b"x" * 100000
        elif scenario == "sad":
            right.close()
            with pytest.raises(OSError):
                for _ in range(100):
                    rt.send_all(left, b"x" * 65536)
        else:
            # A non-blocking socket with a full buffer waits instead of failing.
            left.setblocking(False)
            reader = threading.Thread(target=lambda: [right.recv(1 << 20) for _ in range(40)], daemon=True)
            reader.start()
            rt.send_all(left, b"y" * (2 << 20))
    finally:
        left.close()
        right.close()


def test_relay(scenario):
    a1, a2 = socket.socketpair()
    b1, b2 = socket.socketpair()
    counts = [0, 0]
    thread = threading.Thread(target=rt.relay, args=(a2, b1, counts), kwargs={"grace": 1}, daemon=True)
    thread.start()
    try:
        if scenario == "happy":
            a1.sendall(b"ping")
            assert b2.recv(10) == b"ping"
            b2.sendall(b"pong!")
            assert a1.recv(10) == b"pong!"
            a1.close()
            b2.close()
            thread.join(5)
            assert counts == [4, 5] and not thread.is_alive()
        elif scenario == "sad":
            # One side ends, the other stays quiet: the relay stops after the grace time.
            a1.close()
            thread.join(5)
            assert not thread.is_alive()
        else:
            # Half-close travels: the far side sees the end and still answers.
            a1.shutdown(socket.SHUT_WR)
            assert b2.recv(10) == b""
            b2.sendall(b"late")
            assert a1.recv(10) == b"late"
            b2.close()
            thread.join(5)
            assert counts == [0, 4]
    finally:
        for sock in (a1, b2, a2, b1):
            sock.close()


def test_request_framing(scenario):
    if scenario == "happy":
        assert rt.request_framing([["Content-Length", "12"]]) == ("length", 12)
        assert rt.request_framing([["Transfer-Encoding", "Chunked "]]) == ("chunked",)
    elif scenario == "sad":
        # Anything a server could frame differently is refused (request smuggling).
        for headers in ([["Content-Length", "-1"]], [["Transfer-Encoding", "gzip, chunked"]],
                        [["Transfer-Encoding", "chunked"], ["Content-Length", "5"]],
                        [["Content-Length", "5"], ["Content-Length", "6"]], [["Transfer-Encoding", "xchunked"]]):
            with pytest.raises(ValueError):
                rt.request_framing(headers)
    else:
        assert rt.request_framing([]) is None and rt.request_framing([["Content-Length", "0"]]) is None


def test_response_framing(scenario):
    if scenario == "happy":
        assert rt.response_framing("GET", 200, [["Content-Length", "3"]]) == ("length", 3)
        assert rt.response_framing("GET", 200, [["Transfer-Encoding", "chunked"]]) == ("chunked",)
    elif scenario == "sad":
        assert rt.response_framing("GET", 200, []) == ("close",)
    else:
        for method, status in (("HEAD", 200), ("GET", 204), ("GET", 304), ("GET", 101)):
            assert rt.response_framing(method, status, [["Content-Length", "9"]]) is None


def test_host_matches(scenario):
    if scenario == "happy":
        assert rt.host_matches("API.example.org.", ["api.example.org"])
        assert rt.host_matches("a.b.example.org", ["*.example.org"])
    elif scenario == "sad":
        assert not rt.host_matches("example.org", ["*.example.org"])
        assert not rt.host_matches("evilexample.org", ["*.example.org"])
    else:
        assert not rt.host_matches(None, ["*.example.org"]) and not rt.host_matches("x", [])


def test_drop_to_proxy_user(scenario, monkeypatch):
    calls = []
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(rt.os, name, lambda *args, n=name: calls.append((n, args)))
    libc = Mock()
    libc.prctl.return_value = -1 if scenario == "sad" else 0
    monkeypatch.setattr(rt.ctypes, "CDLL", lambda *a, **kw: libc)
    if scenario == "sad":
        with pytest.raises(OSError, match="no_new_privs"):
            rt.drop_to_proxy_user()
    else:
        rt.drop_to_proxy_user()
        # Groups first, then the group, the user last (afterwards nothing could be changed).
        assert calls == [("setgroups", ([],)), ("setresgid", (1, 1, 1)), ("setresuid", (1, 1, 1))]
        assert libc.prctl.call_args.args[:2] == (38, 1)


def test_proxy_listeners(scenario):
    if scenario == "happy":
        listeners = rt.proxy_listeners(0)
        try:
            assert listeners[0].getsockname()[0] == "127.0.0.1"
        finally:
            for sock in listeners:
                sock.close()
    elif scenario == "sad":
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        try:
            with pytest.raises(OSError):
                rt.proxy_listeners(taken.getsockname()[1])
        finally:
            taken.close()
    else:
        listeners = rt.proxy_listeners(0)
        try:
            assert all(sock.type == socket.SOCK_STREAM for sock in listeners) and 1 <= len(listeners) <= 2
        finally:
            for sock in listeners:
                sock.close()


@needs_openssl
def test_proxy_main(scenario, tmp_path, monkeypatch):
    proxy_dir, run = tmp_path / "proxy", tmp_path / "run"
    proxy_dir.mkdir()
    (proxy_dir / "proxy.json").write_text(json.dumps({"port": 0, "allow": {"all": True},
                                                      "push_check": scenario == "edge"}))
    monkeypatch.setattr(rt, "PROXY_DIR", proxy_dir)
    monkeypatch.setattr(rt, "PROXY_RUN", run)
    monkeypatch.setattr(rt, "CA_DIR", tmp_path / "ca")
    chowned, popen, real_popen = [], Mock(), subprocess.Popen
    monkeypatch.setattr(rt.os, "chown", lambda path, uid, gid: chowned.append((str(path), uid, gid)))
    # openssl really runs (through subprocess.run); only the checker process is recorded.
    monkeypatch.setattr(rt.subprocess, "Popen",
                        lambda command, *a, **kw: real_popen(command, *a, **kw) if command[0] == "openssl"
                        else popen(command, *a, **kw))
    monkeypatch.setattr(rt, "drop_to_proxy_user", Mock(side_effect=OSError("drop") if scenario == "sad" else None))
    monkeypatch.setattr(rt.ProxyServer, "wait", lambda self: 0)
    served = Mock()
    monkeypatch.setattr(rt.ProxyServer, "serve", served)
    ready_r, ready_w = os.pipe()
    log = os.open(tmp_path / "log", os.O_WRONLY | os.O_CREAT)
    try:
        if scenario == "sad":
            with pytest.raises(OSError, match="drop"):
                rt.proxy_main(log, ready_w)
            assert not served.called  # nothing is served with root's rights
            return
        assert rt.proxy_main(log, ready_w) == 0
        assert os.read(ready_r, 1) == b"1"
        assert (str(run), 1, 1) in chowned and (str(run / "ca.key"), 1, 1) in chowned
        assert json.loads((run / "allow.json").read_text()) == {"all": True}
        if scenario == "edge":
            command = popen.call_args.args[0]
            assert command[:len(rt.DROP_ALL)] == rt.DROP_ALL and command[-2:] == ["serve", str(proxy_dir / "push.json")]
            assert popen.call_args.kwargs["env"]["HOME"] == "/nonexistent"
        else:
            popen.assert_not_called()
    finally:
        os.close(ready_r)
        os.close(log)
        with contextlib.suppress(OSError):
            os.close(ready_w)
        for sock in served.call_args.args[0] if served.called else []:
            sock.close()


def test_start_proxy(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    trusted = Mock()
    monkeypatch.setattr(rt, "trust_session_ca", trusted)
    started = []

    def popen(command, pass_fds, **kwargs):
        log_fd, ready_fd = pass_fds
        started.append(command)
        process = Mock()
        process.poll.return_value = None if scenario != "sad" else 1
        if scenario == "happy":
            os.write(ready_fd, b"1")
        elif scenario == "edge":
            os.write(ready_fd, b"x")
        return process
    monkeypatch.setattr(rt.subprocess, "Popen", popen)
    monkeypatch.setattr(rt, "stop_process", Mock())
    if scenario == "happy":
        rt.start_proxy({"log": "connections-x.jsonl"})
        assert started[0][2:4] == ["/sandbox/runtime.py", "--internal-proxy"]
        assert (tmp_path / "connections-x.jsonl").exists() and trusted.called
        assert stat.S_IMODE((tmp_path / "connections-x.jsonl").stat().st_mode) == 0o600
    else:
        with pytest.raises(RuntimeError, match="did not start"):
            rt.start_proxy({"log": "connections-x.jsonl"})
        assert not trusted.called and rt.stop_process.called


def test_trust_session_ca(scenario, tmp_path):
    bundle = tmp_path / "certs" / "ca-certificates.crt"
    bundle.parent.mkdir()
    bundle.write_text("system")
    (tmp_path / "cert.pem").symlink_to(bundle)
    mounts = []
    if scenario == "happy":
        done = rt.trust_session_ca((str(bundle), str(tmp_path / "cert.pem")), mount=lambda s, t: mounts.append((s, t)))
        # A link to the same file is bound once.
        assert done == [str(bundle)] and mounts == [(str(rt.CA_DIR / "bundle.pem"), str(bundle))]
    elif scenario == "sad":
        def fail(source, target):
            raise OSError(1, "no")
        with pytest.raises(OSError):
            rt.trust_session_ca((str(bundle),), mount=fail)
    else:
        assert rt.trust_session_ca((str(tmp_path / "missing"),), mount=lambda s, t: mounts.append(t)) == []
        assert mounts == []


def test_relay_ports(scenario):
    """The host ports of a relay request: a short list of real ports."""
    if scenario == "happy":
        assert rt.relay_ports({"host_ports": [3000, 11434, 3000]}) == [3000, 11434]
    elif scenario == "sad":
        for ports in ([0], [65536], ["3000"], [True], "3000", list(range(1, rt.RELAY_PORTS + 2))):
            with pytest.raises(ValueError):
                rt.relay_ports({"host_ports": ports})
    else:
        assert rt.relay_ports({}) == [] and rt.relay_ports({"host_ports": []}) == []


def test_relay_rules(scenario):
    """Relayed ports go through the proxy (IPv4 and IPv6 loopback); the chain is replaced each time."""
    rules = rt.relay_rules({3000: None, 22: None} if scenario != "edge" else {}).splitlines()
    assert rules[0] == "flush chain inet agent_sandbox relay"
    if scenario == "edge":
        assert rules == ["flush chain inet agent_sandbox relay"]
        return
    assert rules[1:] == [f"add rule inet agent_sandbox relay ip daddr 127.0.0.1 tcp dport 22 redirect to :{rt.PROXY_PORT}",
                         f"add rule inet agent_sandbox relay ip6 daddr ::1 tcp dport 22 redirect to :{rt.PROXY_PORT}",
                         f"add rule inet agent_sandbox relay ip daddr 127.0.0.1 tcp dport 3000 redirect to :{rt.PROXY_PORT}",
                         f"add rule inet agent_sandbox relay ip6 daddr ::1 tcp dport 3000 redirect to :{rt.PROXY_PORT}"]
    if scenario == "sad":
        # The firewall jumps to the chain after the proxy's own connections returned: no loop through the proxy.
        rules = s.firewall_rules([], [], proxy={"port": s.PROXY_PORT, "loopback": [3000]})
        assert rules.index(" chain relay {") < rules.index(" chain proxy")  # declared before the jump to it
        nat = rules.split(" chain proxy")[1]
        lines = nat.splitlines()
        assert lines.index(f"  meta skuid {s.PROXY_UID} return") < lines.index("  jump relay") < \
            next(i for i, line in enumerate(lines) if "127.0.0.0/8" in line)
        assert "jump relay" not in s.firewall_rules([], [])


def test_join_streams(scenario):
    """Both directions are copied until each side is done; a side that fails ends the join, not the process."""
    import socket as _socket
    left_a, left_b = _socket.socketpair()
    right_a, right_b = _socket.socketpair()
    worker = threading.Thread(target=rt.join_streams, args=(left_b, right_a), daemon=True)
    worker.start()
    if scenario == "happy":
        left_a.sendall(b"to the host")
        assert right_b.recv(100) == b"to the host"
        right_b.sendall(b"to the sandbox")
        assert left_a.recv(100) == b"to the sandbox"
        left_a.shutdown(_socket.SHUT_WR)
        assert right_b.recv(100) == b""  # the end of one direction reaches the other side
        right_b.sendall(b"late answer")  # the other direction still works (half-close)
        assert left_a.recv(100) == b"late answer"
        right_b.close()
    elif scenario == "sad":
        right_b.close()  # the host side is gone
        left_a.sendall(b"x" * 10)
        left_a.close()
    else:
        left_a.close()
        right_b.close()
    worker.join(5)
    assert not worker.is_alive()
    for sock in (left_a, right_b):
        sock.close()


def test_runtime_rules(scenario):
    if scenario == "happy":
        script = rt.runtime_rules({"nets": {"192.0.2.0/24"}, "https": {"198.51.100.4/32"},
                                   "tcp": {("203.0.113.9", 5432)}})
        assert script.splitlines() == [
            "flush chain inet agent_sandbox runtime",
            "add rule inet agent_sandbox runtime ip daddr 192.0.2.0/24 accept",
            "add rule inet agent_sandbox runtime ip daddr 198.51.100.4/32 tcp dport 443 accept",
            "add rule inet agent_sandbox runtime ip daddr 203.0.113.9 tcp dport 5432 accept"]
    elif scenario == "sad":
        with pytest.raises(ValueError):
            rt.runtime_rules({"nets": {"not-an-address"}, "https": set(), "tcp": set()})
    else:
        assert rt.runtime_rules({"nets": set(), "https": {"2001:db8::/32"}, "tcp": set()}).splitlines()[1] == \
            "add rule inet agent_sandbox runtime ip6 daddr 2001:db8::/32 tcp dport 443 accept"


def test_runtime_change(scenario):
    empty = {"nets": set(), "https": set(), "tcp": set()}
    if scenario == "happy":
        state = rt.runtime_change(empty, {"op": "allow", "https": ["192.0.2.7"], "tcp": [["192.0.2.8", 22]]})
        assert state == {"nets": set(), "https": {"192.0.2.7/32"}, "tcp": {("192.0.2.8", 22)}, "dns": set()}
        assert rt.runtime_change(state, {"op": "revoke", "https": ["192.0.2.7/32"]})["https"] == set()
        # DNS during the run (`net allow --dns`): nameserver addresses, normalised.
        state = rt.runtime_change(state, {"op": "allow", "dns": ["192.0.2.53", "2001:DB8::53"]})
        assert state["dns"] == {"192.0.2.53", "2001:db8::53"}
        assert rt.runtime_change(state, {"op": "revoke", "dns": ["192.0.2.53"]})["dns"] == {"2001:db8::53"}
    elif scenario == "sad":
        for request in ({"op": "drop"}, {"op": "allow", "nets": ["192.0.2.1; flush ruleset"]},
                        {"op": "allow", "tcp": [["192.0.2.1", 0]]}, {"op": "allow", "tcp": [["192.0.2.1", True]]},
                        {"op": "allow", "tcp": [["192.0.2.1", "22"]]}, {"op": "allow", "nets": ["fe80::1%eth0"]},
                        {"op": "allow", "dns": "192.0.2.53"}, {"op": "allow", "dns": ["192.0.2.0/24"]},
                        {"op": "allow", "dns": ["192.0.2.%d" % i for i in range(9)]},
                        {"op": "allow", "dns": ["fe80::1%eth0"]}):
            with pytest.raises(ValueError):
                rt.runtime_change(empty, request)
    else:
        # Revoking what was never allowed changes nothing; networks are normalised.
        assert rt.runtime_change(empty, {"op": "revoke", "nets": ["10.0.0.0/8"]}) == {**empty, "dns": set()}
        assert rt.runtime_change(empty, {"op": "allow", "nets": ["10.1.2.3/8"]})["nets"] == {"10.0.0.0/8"}


def test_merged_allow(scenario):
    base = {"all": False, "nets": ["192.0.2.0/24"], "https": ["198.51.100.1/32"], "tcp": [["203.0.113.1", 22]],
            "loopback": [3000]}
    empty = {"nets": set(), "https": set(), "tcp": set()}
    if scenario == "happy":
        merged = rt.merged_allow(base, {"nets": set(), "https": {"198.51.100.2/32"}, "tcp": {("203.0.113.2", 5432)}})
        assert merged["https"] == ["198.51.100.1/32", "198.51.100.2/32"]
        assert merged["tcp"] == [["203.0.113.1", 22], ["203.0.113.2", 5432]] and merged["loopback"] == [3000]
    elif scenario == "sad":
        assert rt.merged_allow({}, empty) == {"nets": [], "https": [], "tcp": [], "ssh": [], "ssh_tcp": []}
    else:
        assert rt.merged_allow(base, empty) == {**base, "ssh": [], "ssh_tcp": []} and base["nets"] == ["192.0.2.0/24"]
        # What `net allow` opened explicitly may also take SSH, as --allow and --allow-tcp at the start: a
        # range on any port, an IP:PORT on that port only.
        merged = rt.merged_allow(base, {"nets": {"10.0.0.0/8"}, "https": set(), "tcp": {("192.0.2.9", 22)}})
        assert merged["ssh"] == ["10.0.0.0/8"] and merged["ssh_tcp"] == [["192.0.2.9", 22]]


def test_control_server(scenario, tmp_path):
    socket_path, allow = tmp_path / "control.sock", tmp_path / "allow.json"
    applied, stop = [], threading.Event()
    apply = (lambda script: applied.append(script)) if scenario != "sad" else Mock(
        side_effect=subprocess.CalledProcessError(1, "nft"))
    network = rt.RuntimeNetwork({"loopback": [3000]}, allow if scenario != "edge" else None, apply)
    thread = threading.Thread(target=rt.control_server, args=(socket_path, network, stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    def ask(request):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(socket_path))
            client.sendall(json.dumps(request).encode() + b"\n")
            return json.loads(client.makefile().readline())
    try:
        if scenario == "happy":
            answer = ask({"op": "allow", "https": ["192.0.2.7"]})
            assert answer == {"ok": True, "nets": [], "https": ["192.0.2.7/32"], "tcp": [], "dns": [], "host_ports": []}
            assert "tcp dport 443 accept" in applied[-1]
            assert json.loads(allow.read_text())["https"] == ["192.0.2.7/32"]
            assert ask({"op": "show"})["https"] == ["192.0.2.7/32"]
        elif scenario == "sad":
            # The firewall refused: nothing is recorded as allowed.
            answer = ask({"op": "allow", "https": ["192.0.2.7"]})
            assert not answer["ok"] and ask({"op": "show"})["https"] == [] and not allow.exists()
            assert not ask({"op": "allow", "nets": ["x"]})["ok"]
        else:
            assert ask({"op": "allow", "tcp": [["192.0.2.9", 22]]})["tcp"] == [["192.0.2.9", 22]]
            assert ask({"op": "revoke", "tcp": [["192.0.2.9", 22]]})["tcp"] == []
            assert applied[-1] == "flush chain inet agent_sandbox runtime\n"
    finally:
        stop.set()
        thread.join(5)


def test_proxy_enabled(scenario):
    if scenario == "happy":
        assert s.proxy_enabled(s.parse_args(["codex"]))
    elif scenario == "sad":
        assert not s.proxy_enabled(s.parse_args(["codex", "--no-proxy"]))
    else:
        assert not s.proxy_enabled(s.parse_args(["codex", "--offline"]))


def test_proxy_allow(scenario):
    hosts = {"api.openai.com": ["192.0.2.10"]}
    if scenario == "happy":
        a = s.parse_args(["codex", "--allow", "198.51.100.0/24", "--allow-tcp", "203.0.113.5:5432"])
        allow = s.proxy_allow(a, hosts)
        assert allow == {"all": False, "nets": ["198.51.100.0/24"], "https": ["192.0.2.10/32"],
                         "tcp": [["203.0.113.5", 5432]], "loopback": []}
    elif scenario == "sad":
        assert s.proxy_allow(s.parse_args(["codex"]), {}) == {"all": False, "nets": [], "https": [], "tcp": [],
                                                              "loopback": []}
    else:
        allow = s.proxy_allow(s.parse_args(["codex", "--egress-all-allow", "--allow-host-port", "3000"]), hosts)
        assert allow["all"] and allow["loopback"] == [3000]
        assert ["1.1.1.1", 53] in allow["tcp"]  # resolvers over TCP


def test_proxy_config(scenario):
    hosts = {"github.com": ["192.0.2.20"], "api.github.com": ["192.0.2.21"], "pypi.org": ["192.0.2.30"]}
    if scenario == "happy":
        config = s.proxy_config(s.parse_args(["codex", "--allow-tcp", "203.0.113.5:22"]), hosts, "connections-x.jsonl")
        assert config["ssh"] == {"block": True, "allowed": [], "allowed_tcp": [["203.0.113.5", 22]],
                                 "public_git": ["192.0.2.20", "192.0.2.21"]}
        assert config["push_check"] and config["log"] == "connections-x.jsonl" and config["body_limit"] == 65536
    elif scenario == "sad":
        config = s.proxy_config(s.parse_args(["codex", "--privacy-check", "off"]), hosts, "l")
        assert not config["ssh"]["block"] and not config["push_check"]
    else:
        config = s.proxy_config(s.parse_args(["codex", "--no-inspect", "Login.Example.org"]), {}, "l")
        assert config["no_inspect"] == ["login.example.org"] and config["port"] == s.PROXY_PORT
        # Approved "always" (remembered): reachable, but never over SSH, unlike --allow-tcp.
        a = s.parse_args(["codex", "--allow-tcp", "203.0.113.5:22"])
        a.allow_tcp.append("198.51.100.9:22")
        a.approved_tcp = ["198.51.100.9:22"]
        config = s.proxy_config(a, {}, "l")
        assert config["ssh"]["allowed_tcp"] == [["203.0.113.5", 22]]
        assert ["198.51.100.9", 22] in config["allow"]["tcp"]


def test_write_proxy_files(scenario, tmp_path):
    work = tmp_path / "app"
    work.mkdir()
    out = tmp_path / "proxy"
    out.mkdir()
    if scenario == "happy":
        a = s.parse_args(["codex", "--workspace", str(work)])
        s.write_proxy_files(a, out, {}, "connections-x.jsonl")
        assert sorted(p.name for p in out.iterdir()) == ["privacy.py", "proxy.json", "push.json"]
        push = json.loads((out / "push.json").read_text())
        assert push["mode"] == "public" and push["workspaces"][0]["path"] == "/work"
        assert any(p["id"] for p in push["workspaces"][0]["rules"]["pattern"])
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out.iterdir())
    elif scenario == "sad":
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "global.rules.json").write_text("{broken")
        with pytest.raises(ValueError, match="--privacy-rules"):
            s.write_proxy_files(s.parse_args(["codex", "--workspace", str(work), "--privacy-rules", str(rules)]),
                                out, {}, "l")
    else:
        s.write_proxy_files(s.parse_args(["codex", "--workspace", str(work), "--privacy-check", "off"]), out, {}, "l")
        assert [p.name for p in out.iterdir()] == ["proxy.json"]


def test_rotate_connection_logs(scenario, tmp_path):
    for index in range(3):
        (tmp_path / f"connections-2026110{index}-x.jsonl").write_bytes(b"x" * 100)
        os.utime(tmp_path / f"connections-2026110{index}-x.jsonl", (time.time() - 7200, time.time() - 7200))
    (tmp_path / "files.jsonl").write_bytes(b"y" * 1000)
    if scenario == "happy":
        assert s.rotate_connection_logs(tmp_path, limit=150) == ["connections-20261100-x.jsonl",
                                                                  "connections-20261101-x.jsonl"]
        assert (tmp_path / "files.jsonl").exists() and (tmp_path / "connections-20261102-x.jsonl").exists()
    elif scenario == "sad":
        assert s.rotate_connection_logs(tmp_path / "missing") == []
    else:
        assert s.rotate_connection_logs(tmp_path, limit=300) == []
        # A log written within the hour may belong to a sandbox that still runs: kept.
        os.utime(tmp_path / "connections-20261100-x.jsonl", None)
        assert s.rotate_connection_logs(tmp_path, limit=150) == ["connections-20261101-x.jsonl",
                                                                  "connections-20261102-x.jsonl"]


def test_connection_summary(scenario, tmp_path):
    log = tmp_path / "connections.jsonl"
    if scenario == "happy":
        log.write_text("\n".join(json.dumps(r) for r in [
            {"event": "http", "url": "https://a/"}, {"event": "denied"}, {"event": "tcp"},
            {"event": "http", "push": {"ok": False, "refs": ["refs/heads/main"]}},
            {"event": "tls-refused", "host": "pinned.example.org"}]) + "\nnot json\n")
        lines = s.connection_summary(log)
        assert lines[0].startswith("proxy: 2 HTTP request(s), 1 other connection(s), 1 refused")
        assert "refs/heads/main" in lines[1] and "--no-inspect pinned.example.org" in lines[2]
    elif scenario == "sad":
        assert s.connection_summary(tmp_path / "missing") == []
    else:
        log.write_text("")
        assert s.connection_summary(log) == [f"proxy: 0 HTTP request(s), 0 other connection(s), 0 refused; log: {log}"]


def test_runs_dir(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert s.runs_dir() == tmp_path / "sandbox-ai" / "runs"
    elif scenario == "sad":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        assert s.runs_dir() == tmp_path / "state" / "sandbox-ai" / "runs"
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert s.runs_dir() == Path.home() / ".local/state/sandbox-ai/runs"


def test_register_run(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # beside the workspace: never around it
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    s.validate(a)
    if scenario == "happy":
        directory = s.register_run(a, "claude-1234abcd", tmp_path / "session")
        record = json.loads((directory / "run.json").read_text())
        assert record["run"] == "claude-1234abcd" and record["workspace"] == str(tmp_path / "app")
        assert record["session_dir"] == str(tmp_path / "session") and record["proxy"]
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert record["own_cgroup"] is True  # Podman: the pod's cgroup is the run's own
        # sandbox-ai mcp attach --live: its supervisor opens relays during the run, the agent's HOME is named.
        assert record["mcp_live"] is True and "agent_home" in record
        # sandbox-ai workspace add checks against what the start recorded.
        assert record["workspace_checks"]["workspaces"] == [{"host": str(tmp_path / "app"), "sandbox": "/work",
                                                              "mode": "rw"}]
    elif scenario == "sad":
        # A run of that name that still lives (another process) stops the start; a dead one is replaced.
        directory = s.register_run(a, "session-x", tmp_path / "session")
        record = json.loads((directory / "run.json").read_text())
        (directory / "run.json").write_text(json.dumps({**record, "pid": os.getppid(),
                                                         "pid_start": s.process_start(os.getppid())}))
        with pytest.raises(ValueError, match="already running"):
            s.register_run(a, "session-x", tmp_path / "session")
        # The same PID with another start time: given out again since, no run of that name lives.
        (directory / "run.json").write_text(json.dumps({**record, "pid": os.getppid(),
                                                         "pid_start": s.process_start(os.getppid()) + 1}))
        assert s.register_run(a, "session-x", tmp_path / "session") == directory
        (directory / "run.json").write_text(json.dumps({**record, "pid": 2 ** 22 + 9}))
        (directory / "control.sock").write_text("stale")
        assert s.register_run(a, "session-x", tmp_path / "other") == directory and not (directory / "control.sock").exists()
    else:
        assert json.loads((s.register_run(a, "x", tmp_path) / "run.json").read_text())["session"] is None


def test_first_bytes_state(scenario):
    hello = client_hello("example.org")
    if scenario == "happy":
        for data in (hello, b"GET / HTTP/1.1\r\n", b"SSH-2.0-x", b"\x00\x01binary"):
            assert rt.first_bytes_state(data) == "complete", data
    elif scenario == "sad":
        # Pieces that would change meaning with the next byte: waited for, never classified early.
        for data in (b"", hello[:10], b"S", b"SS", b"SSH", b"POS", b"PRI"):
            assert rt.first_bytes_state(data) == "more", data
    else:
        assert rt.first_bytes_state(b"POSTXXXXXXX") == "complete"  # longer than any method: not HTTP
        assert rt.first_bytes_state(b"SS1") == "complete"  # (SSX could still begin a method)


def test_review_regressions_launcher(tmp_path, monkeypatch):
    """Findings of the review before 0.1.20, each kept closed."""
    # --mount never of device or kernel trees (Podman would keep device nodes usable).
    for source in ("/dev", "/proc/self", "/sys"):
        if Path(source).exists():
            with pytest.raises(ValueError, match="device and kernel trees"):
                s.parse_mount(f"{source}:/mnt/x:ro")
    # --no-inspect neither for public Git hosts nor for names below them.
    for value in ("github.com", "*.github.com", "www.github.com", "*.com"):
        with pytest.raises(ValueError, match="pass unchecked"):
            s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--no-inspect", value]))
    s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--no-inspect", "*.github.com",
                             "--privacy-check", "off"]))


@pytest.mark.parametrize("existing", [
    '[projects]\n"/work" = { trust_level = "untrusted" }\n',
    'projects."/work".trust_level = "untrusted"\n',
    '[ projects."/work" ]\ntrust_level = "untrusted"\n',
    'projects = { "/other" = { trust_level = "trusted" } }\n',
    'not = valid = toml\n',
])
def test_codex_trust_never_breaks_config_toml(tmp_path, existing):
    toml = s.load_privacy_module()._toml_module()
    if toml is None:
        pytest.skip("no TOML parser (Python < 3.11 without tomli)")
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(existing)
    s.codex_trusted_workspaces(s.parse_args(["codex", "--workspace", str(tmp_path / "app")]), home)
    after = (home / ".codex/config.toml").read_text()
    if existing.startswith("not"):
        assert after == existing
    else:
        document = toml.loads(after)  # still valid
        assert "/work" in document["projects"] or after == existing
        if "untrusted" in existing:
            assert after == existing  # the decision stored there is kept


def test_new_device_refusals(fake_dev):
    for name in ("fb0", "usbmon0", "snapshot", "iommu", "usb/hiddev0"):
        (fake_dev / name).parent.mkdir(parents=True, exist_ok=True)
        (fake_dev / name).write_text("")
        with pytest.raises(ValueError, match="refused"):
            s.parse_device(str(fake_dev / name))


def test_long_option(scenario):
    actions = s.build_parser()._option_string_actions
    if scenario == "happy":
        assert s.long_option(actions["-D"]) == "--dangerously-skip-permissions"
    elif scenario == "sad":
        assert s.long_option(actions["-h"]) == "--help"
    else:
        assert s.long_option(actions["-gl"]) == "--allow-gitlab"


def test_canonical_options(scenario):
    if scenario == "happy":
        assert s.canonical_options(["claude", "-Dn", "-w/tmp/a", "-gl", "-S", "/x", "-V"]) == [
            "claude", "--dangerously-skip-permissions", "--dry-run", "--workspace", "/tmp/a", "--allow-gitlab",
            "--state-dir", "/x", "--version"]
    elif scenario == "sad":
        # Values are never read as options; unknown tokens stay as they are for argparse to refuse.
        assert s.canonical_options(["--env", "-D", "-e", "-S", "-jz", "--", "-D"]) == [
            "--env", "-D", "--env", "-S", "-jz", "--", "-D"]
    else:
        assert s.canonical_options(["--mount=a:/mnt/a:ro", "-d/dev/kvm", "-x", "*.example.org"]) == [
            "--mount=a:/mnt/a:ro", "--device", "/dev/kvm", "--no-inspect", "*.example.org"]


def test_short_forms_cover_every_option_and_reach_every_check(tmp_path):
    parser = s.build_parser()
    visible = [a for a in parser._actions if a.option_strings and a.help != argparse.SUPPRESS]
    assert all(any(not o.startswith("--") for o in a.option_strings) for a in visible)
    shorts = [o for a in parser._actions for o in a.option_strings if not o.startswith("--")]
    assert len(shorts) == len(set(shorts))
    # A short form in a project file is checked like the long one.
    work = tmp_path / "w"
    work.mkdir()
    config = work / s.CONFIG_NAME
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["-S/tmp/x"]}))
    s.trust_config(config)
    _, document = s.load_project_config(s.parse_args(["codex", "--workspace", str(work)]))
    assert document["options"] == ["--state-dir", "/tmp/x"]
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["-s", "x"]}))  # --session: forbidden
    s.trust_config(config)
    with pytest.raises(ValueError, match="not allowed in a project configuration"):
        s.load_project_config(s.parse_args(["codex", "--workspace", str(work)]))
    mcp = s.load_mcp_module()
    with pytest.raises(ValueError, match="not allowed for an MCP server"):
        mcp.validated({"x": {"command": ["true"], "options": ["-S/tmp/state"]}}, "test")


def test_short_forms_like_argparse_reads_them():
    assert s.canonical_options(["-Dw", "/tmp/a", "-w=/tmp/b", "-pr=/rules", "-ne", "A"]) == [
        "--dangerously-skip-permissions", "--workspace", "/tmp/a", "--workspace", "/tmp/b", "--privacy-rules",
        "/rules", "--dry-run", "--env", "A"]
    wizard = s.load_wizard_module() if hasattr(s, "load_wizard_module") else None
    spec = importlib.util.spec_from_file_location("wizard_short", ROOT / "sandbox/wizard.py")
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    with pytest.raises(ValueError, match="Not allowed in a project configuration"):
        wizard.check_free_options("codex", "-s name")
    assert wizard.check_free_options("codex", "-g -A example.org") == ["--allow-github", "--allow-host", "example.org"]


# --- resource limits ------------------------------------------------------------------------------


def test_resource_limits(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    if scenario == "happy":
        assert s.resource_limits(s.parse_args(["codex"])) == {"memory": "8g", "cpus": "4", "pids": 4096}
        assert s.resource_limits(s.parse_args(["codex", "--memory", "2G", "--cpus", "1.5", "--pids", "512"])) == {
            "memory": "2g", "cpus": "1.5", "pids": 512}
    elif scenario == "sad":
        for args in (["--memory", "lots"], ["--memory", "0g"], ["--cpus", "0"], ["--cpus", "-1"], ["--cpus", "99"],
                     ["--pids", "3"], ["--pids", "x"]):
            with pytest.raises(ValueError):
                s.resource_limits(s.parse_args(["codex", *args]))
    else:
        assert s.resource_limits(s.parse_args(["codex", "--memory", "none", "--cpus", "none", "--pids", "none"])) == {}
        monkeypatch.setattr(s.os, "cpu_count", lambda: 1)
        assert s.resource_limits(s.parse_args(["codex"]))["cpus"] == "1"  # at least one CPU


def test_delegated_controllers(scenario, tmp_path):
    if scenario == "happy":
        (tmp_path / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
        assert s.delegated_controllers(tmp_path / "cgroup.controllers") == {"cpuset", "cpu", "io", "memory", "pids"}
    elif scenario == "sad":
        assert s.delegated_controllers(tmp_path / "missing") is None  # unknown: limits are passed on
    else:
        (tmp_path / "cgroup.controllers").write_text("")
        assert s.delegated_controllers(tmp_path / "cgroup.controllers") == set()


def test_enforced_limits(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    a = s.parse_args(["codex"])
    if scenario == "happy":
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"cpu", "memory", "pids"})
        assert s.enforced_limits(a) == ({"memory": "8g", "cpus": "4", "pids": 4096}, [])
    elif scenario == "sad":
        # cpu not delegated to the user (older systemd): that limit is left out, with a note.
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"memory", "pids"})
        limits, notes = s.enforced_limits(a)
        assert limits == {"memory": "8g", "pids": 4096} and "cpu" in notes[0] and "--cpus" in notes[0]
    else:
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: None)
        assert s.enforced_limits(a)[0] == {"memory": "8g", "cpus": "4", "pids": 4096}
        remote = s.parse_args(["codex", "--podman-url", "unix:///x.sock"])
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: set())
        assert s.enforced_limits(remote)[0] == {"memory": "8g", "cpus": "4", "pids": 4096}  # remote: not ours to check
        # No systemd user session: rootless Podman uses cgroupfs and cannot set limits at all.
        monkeypatch.setattr(s, "user_manager_running", lambda: False)
        limits, notes = s.enforced_limits(a)
        assert limits == {} and "linger" in notes[0]


def test_limits_prefix(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"cpu", "memory", "pids"})
    a = s.parse_args(["codex", "--backend", "bwrap"])
    if scenario == "happy":
        monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(s, "user_manager_running", lambda: True)
        assert s.limits_prefix(a) == ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=8G",
                                      "-p", "MemorySwapMax=8G", "-p", "CPUQuota=400%", "-p", "TasksMax=4096", "--"]
    elif scenario == "sad":
        monkeypatch.setattr(s.shutil, "which", lambda name: None)
        assert s.limits_prefix(a) == []
    else:
        assert s.limits_prefix(s.parse_args(["codex", "--backend", "bwrap", "--memory", "none", "--cpus", "none",
                                             "--pids", "none"])) == []


def test_user_manager_running(scenario, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(s, "Path", lambda value: seen.append(value) or (tmp_path / "private" if scenario == "happy"
                                                                       else tmp_path / "missing"))
    if scenario == "happy":
        (tmp_path / "private").write_text("")
        assert s.user_manager_running()
    elif scenario == "sad":
        assert not s.user_manager_running()
    else:
        s.user_manager_running()
        assert seen == [f"/run/user/{os.getuid()}/bus"]


def test_podman_run_carries_the_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"memory", "pids"})
    _, run = s.pod_commands(s.parse_args(["codex", "--workspace", str(tmp_path), "--memory", "2g"]), "p",
                            tmp_path / "session", tmp_path / "home")
    assert run[run.index("--memory") + 1] == "2g" and run[run.index("--pids-limit") + 1] == "4096"
    assert "--cpus" not in run  # cpu not delegated: left out, not failing the start


def test_user_bus_environment(scenario, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/elsewhere")
    env = s.user_bus_environment()
    if scenario == "happy":
        assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    elif scenario == "sad":
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"
    else:
        assert env["PATH"] == os.environ["PATH"] and os.environ["XDG_RUNTIME_DIR"] == "/tmp/elsewhere"


# --- approvals ------------------------------------------------------------------------------------


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def answer_file(directory, decision, scope="once", **extra):
    """Answer the first open question in DIRECTORY like sandbox-ai approvals does."""
    wait_for(lambda: list(directory.glob("*.json")))
    question = json.loads(sorted(directory.glob("*.json"))[0].read_text())
    (directory / f"{question['id']}.answer").write_text(json.dumps({"decision": decision, "scope": scope, **extra}))
    return question


def test_approval_broker_and_runtime_network(tmp_path):
    rules = []
    network = rt.RuntimeNetwork({"https": []}, tmp_path / "allow.json", rules.append)
    broker = rt.ApprovalBroker(tmp_path / "approvals", network, timeout=3, poll=0.02)
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", {
        "destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443, "host": "api.example.org"})))
    thread.start()
    question = answer_file(tmp_path / "approvals", "allow", "run")
    thread.join(5)
    assert question["kind"] == "connect" and question["host"] == "api.example.org" and question["timeout"] == 3
    assert results[0]["decision"] == "allow" and results[0]["scope"] == "run"
    # Applied to the firewall and the proxy's allow-list before the answer; remembered for the run.
    assert "ip daddr 192.0.2.7 tcp dport 8443 accept" in rules[-1]
    assert json.loads((tmp_path / "allow.json").read_text())["tcp"] == [["192.0.2.7", 8443]]
    assert broker.ask("connect", {"destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443})["scope"] == "run"
    assert json.loads((tmp_path / "approvals" / f"{question['id']}.done").read_text())["decision"] == "allow"
    assert stat.S_IMODE((tmp_path / "approvals" / f"{question['id']}.json").stat().st_mode) == 0o600


def test_approval_broker_once_timeout_and_dns(tmp_path, monkeypatch):
    rules = []
    network = rt.RuntimeNetwork({}, None, rules.append)
    timers = []
    monkeypatch.setattr(network, "allow_once", lambda address, port, timer=None: (
        rt.RuntimeNetwork.allow_once(network, address, port, timer=lambda s, f: timers.append(f) or Mock())))
    broker = rt.ApprovalBroker(tmp_path / "a", network, timeout=1, poll=0.02)
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", {
        "destination": "198.51.100.1:22", "address": "198.51.100.1", "port": 22})))
    thread.start()
    answer_file(tmp_path / "a", "allow", "once")
    thread.join(5)
    assert results[0]["decision"] == "allow" and "198.51.100.1 tcp dport 22" in rules[-1]
    timers[0]()  # the once-rule expires
    assert "198.51.100.1" not in rules[-1]
    # Unanswered: refused after the timeout and not asked again during the run.
    started = time.monotonic()
    denied = broker.ask("connect", {"destination": "203.0.113.9:443", "address": "203.0.113.9", "port": 443})
    assert denied["decision"] == "deny" and "no answer" in denied["reason"] and time.monotonic() - started < 4
    assert broker.ask("connect", {"destination": "203.0.113.9:443", "address": "203.0.113.9", "port": 443}) is denied or \
        broker.ask("connect", {"destination": "203.0.113.9:443", "address": "203.0.113.9", "port": 443})["decision"] == "deny"
    # A lookup is filed without waiting; its answer brings the addresses the host resolved.
    assert broker.ask("dns", {"name": "pypi.org"}, wait=False) is None
    files = [p for p in (tmp_path / "a").glob("*.json") if json.loads(p.read_text())["kind"] == "dns"]
    (tmp_path / "a" / f"{files[0].stem}.answer").write_text(json.dumps(
        {"decision": "allow", "scope": "run", "addresses": ["151.101.0.223", "not-an-address"]}))
    wait_for(lambda: (tmp_path / "a" / f"{files[0].stem}.done").exists())
    done = json.loads((tmp_path / "a" / f"{files[0].stem}.done").read_text())
    assert done["decision"] == "deny" and "could not be applied" in done["reason"]  # a bad address refuses all


def test_serve_asks(scenario, tmp_path):
    proxy_end, supervisor_end = socket.socketpair()
    if scenario == "edge":
        # Lines that are not questions are skipped; the channel keeps working.
        proxy_end.sendall(b"not json\n[1, 2]\n")
    asked = []

    class Broker:
        def ask(self, kind, details):
            asked.append((kind, details))
            return {"decision": "allow" if details.get("port") == 443 else "deny", "scope": "run", "reason": "answered"}
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, Broker()), daemon=True).start()
    channel = rt.AskChannel(proxy_end, timeout=5)
    results = {}
    threads = [threading.Thread(target=lambda p=p: results.__setitem__(p, channel("connect", {"port": p})))
               for p in (443, 22)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results[443]["decision"] == "allow" and results[22]["decision"] == "deny"
    assert sorted(d["port"] for _, d in asked) == [22, 443]
    if scenario == "sad":
        assert channel("other", {"port": 443})["decision"] == "deny"  # an unknown kind is never allowed
        assert len(asked) == 2  # and never reaches the broker
    proxy_end.close()


def test_load_approvals_module(scenario):
    module = s.load_approvals_module()
    if scenario == "happy":
        assert callable(module.answer) and callable(module.watch)
    elif scenario == "sad":
        assert module.ID.fullmatch("../x") is None
    else:
        assert module is not s.load_approvals_module()  # loaded fresh, like the other helpers


def test_asking(scenario):
    if scenario == "happy":
        assert s.asking(s.parse_args(["codex"]))
    elif scenario == "sad":
        assert not s.asking(s.parse_args(["codex", "--ask", "off"])) and not s.asking(s.parse_args(["codex", "-X"]))
    else:
        assert not s.asking(s.parse_args(["codex", "--probe"])) and not s.asking(s.parse_args(["codex", "--offline"]))


def test_remembered_pins(scenario):
    a = s.parse_args(["codex"])
    a.policy = {}
    if scenario == "happy":
        assert s.remembered_pins(a, ["api.github.com"], lambda names: {"api.github.com": ["140.82.121.4"]}) == (
            {"api.github.com": ["140.82.121.4"]}, [])
    elif scenario == "sad":  # rebound into the LAN or this host: an approval never opens those
        pins, notes = s.remembered_pins(a, ["x.example"], lambda names: {"x.example": ["10.0.0.1", "127.0.0.1"]})
        assert pins == {} and "not public" in notes[0]
    else:  # one name that no longer resolves takes no other with it, and never stops the start
        def resolve(names):
            if "gone.example" in names:
                raise ValueError("Cannot resolve allowed host gone.example")
            return {name: ["140.82.121.4"] for name in names}
        pins, notes = s.remembered_pins(a, ["gone.example", "api.github.com"], resolve)
        assert pins == {"api.github.com": ["140.82.121.4"]} and "does not resolve now" in notes[0]


def test_apply_remembered_pins(scenario, capsys):
    a = s.parse_args(["codex", "--allow-host", "kept.example", "--allow-host", "dropped.example"])
    a.policy = {}
    if scenario == "happy":
        assert s.apply_remembered_pins(a, ["kept.example"], lambda names: {"kept.example": ["140.82.121.4"]}) == \
            {"kept.example": ["140.82.121.4"]}
        assert "kept.example" in a.allow_host and capsys.readouterr().err == ""
    elif scenario == "sad":
        assert s.apply_remembered_pins(a, ["dropped.example"], lambda names: {"dropped.example": ["192.168.0.2"]}) == {}
        assert a.allow_host == ["kept.example"] and "dropped.example (approved always)" in capsys.readouterr().err
    else:
        assert s.apply_remembered_pins(a, [], lambda names: 1 / 0) == {}  # nothing remembered: nothing resolved


def test_apply_remembered_approvals(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    work = tmp_path / "work"
    work.mkdir()
    approvals = s.load_approvals_module()
    a = s.parse_args(["codex", "--workspace", str(work)])
    if scenario == "happy":
        approvals.remember(work, host="pypi.org")
        approvals.remember(work, tcp="192.0.2.5:5432")
        notes = s.apply_remembered_approvals(a)
        assert a.allow_host == ["pypi.org"] and a.allow_tcp == ["192.0.2.5:5432"] and "approved" in notes[0]
        s.validate(a)
    elif scenario == "sad":
        assert s.apply_remembered_approvals(a) == [] and a.allow_host == []
    else:
        # Another project's approvals do not apply; damaged entries are ignored.
        approvals.remember(tmp_path / "other", host="pypi.org")
        (tmp_path / "config/sandbox-ai/approved.json").write_text(json.dumps(
            {str(work.resolve()): {"hosts": ["bad host;"], "tcp": ["x"]}, **json.loads(
                (tmp_path / "config/sandbox-ai/approved.json").read_text())}))
        assert s.apply_remembered_approvals(a) == [] and a.allow_host == []


# --- telemetry: model usage, credentials, processes, MCP tool calls -------------------------------


def test_usage_scanner_reads_streams_and_bodies():
    anthropic = rt.UsageScanner()
    for chunk in (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
                  b'"usage":{"input_tokens":1200,"cache_read_input_tokens":800,"output_tokens":1}}}\n\n',
                  b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_t',
                  b'okens":345}}\n\n'):
        anthropic.feed(chunk)
    assert anthropic.result() == {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 1200, "cache_read_input_tokens": 800, "output_tokens": 345}}
    openai = rt.UsageScanner()
    openai.feed(json.dumps({"model": "gpt-5.1", "usage": {"input_tokens": 50, "output_tokens": 7,
                                                           "total_tokens": 57}}).encode())
    assert openai.result() == {"model": "gpt-5.1", "usage": {"input_tokens": 50, "output_tokens": 7, "total_tokens": 57}}
    assert rt.UsageScanner().result() is None
    noisy = rt.UsageScanner()
    noisy.feed(b"data: [DONE]\n\ndata: {not json}\n\n" + b"x" * 100)
    assert noisy.result() is None


def test_is_model_request(scenario):
    if scenario == "happy":
        assert rt.is_model_request("/v1/messages?beta=true") and rt.is_model_request("/v1/responses")
    elif scenario == "sad":
        assert not rt.is_model_request("/v1/models") and not rt.is_model_request("/simple/httpx/")
    else:
        assert rt.is_model_request("/v1/chat/completions") and rt.is_model_request("/backend-api/codex/responses")


def test_credential_headers(scenario):
    if scenario == "happy":
        assert rt.credential_headers([["Authorization", "Bearer x"], ["x-api-key", "k"], ["Accept", "*/*"]]) == [
            "authorization", "x-api-key"]
    elif scenario == "sad":
        assert rt.credential_headers([]) == []
    else:
        assert rt.credential_headers([["Cookie", "a"], ["cookie", "b"]]) == ["cookie"]


def test_tool_call_record(scenario):
    if scenario == "happy":
        record = rt.tool_call_record(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "web_search", "arguments": {"query": "x", "api_key": "secret-value"}}}).encode())
        assert record["tool"] == "web_search" and '"api_key": "[redacted]"' in record["arguments"]
    elif scenario == "sad":
        assert rt.tool_call_record(b"not json") is None
        assert rt.tool_call_record(b'{"method": "tools/list"}') is None
    else:
        record = rt.tool_call_record(json.dumps({"method": "tools/call", "params": {
            "name": "x", "arguments": {f"text{i}": "y" * 5000 for i in range(10)}}}).encode())
        assert len(record["arguments"]) == 2003 and record["arguments"].endswith("...")


def test_process_logger(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    (tmp_path / "log").mkdir()
    proc = tmp_path / "proc"

    def process(pid, argv, start="100", ppid=1):
        (proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (proc / str(pid) / "stat").write_text(f"{pid} (x y) S {ppid} " + " ".join(["0"] * 16) + f" {start} 0 0")
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    process(7, [b"git", b"push", b"https://x-access-token:ghp_" + b"A" * 36 + b"@github.com/a/b"])
    if scenario == "edge":
        (proc / "self").mkdir(parents=True)  # not a process
        process(8, [])  # a kernel thread: no command line

    class Stop:
        calls = 0

        def wait(self, interval):
            Stop.calls += 1
            if Stop.calls == 2 and scenario == "happy":
                process(9, [b"python3", b"-c", b"pass"], ppid=7)
            return Stop.calls > 3
    rt.process_logger(Stop(), proc=proc)
    records = [json.loads(line) for line in (tmp_path / "log/processes.jsonl").read_text().splitlines()]
    if scenario == "sad":
        assert len(records) == 1 and "ghp_" not in records[0]["command"]  # once, redacted
    elif scenario == "happy":
        assert [(r["pid"], r["ppid"]) for r in records] == [(7, 1), (9, 7)]
    else:
        assert [r["pid"] for r in records] == [7]


def test_load_secret_store_module(scenario):
    module = s.load_secret_store_module()
    if scenario == "happy":
        assert module.check_name("GH_PAT") == "GH_PAT"
    elif scenario == "sad":
        with pytest.raises(ValueError):
            module.check_name("../etc")
    else:
        assert module.NAME.fullmatch("_x") and not module.NAME.fullmatch("x" * 65)


def test_secret_environment(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    store = s.load_secret_store_module().FileStore()
    store.set("KEY", "value")
    if scenario == "happy":
        assert s.secret_environment(s.parse_args(["codex", "--secret-env", "KEY", "--secret-env", "OTHER=KEY"])) == {
            "KEY": "value", "OTHER": "value"}
    elif scenario == "sad":
        with pytest.raises(ValueError, match="no secret"):
            s.secret_environment(s.parse_args(["codex", "--secret-env", "NOPE"]))
    else:
        assert s.secret_environment(s.parse_args(["codex"])) == {}


def test_api_key_values(scenario):
    if scenario == "happy":
        a = s.parse_args(["codex", "--api-key", "Dev.To=DEVTO_API_KEY", "--api-key", "gql.hashnode.com=HN=HASHNODE"])
        assert s.api_key_values(a) == [("dev.to", "DEVTO_API_KEY", "DEVTO_API_KEY"), ("gql.hashnode.com", "HN", "HASHNODE")]
    elif scenario == "sad":
        for value in ("dev.to", "192.0.2.1=KEY", "localhost=KEY", "dev.to=1KEY", "dev.to=KEY=", "*.dev.to=KEY",
                      "dev.to:8443=KEY", "dev.to=KEY=SEC=RET", "[::1]=KEY"):
            with pytest.raises(ValueError, match="--api-key expects"):
                s.api_key_values(argparse.Namespace(api_key=[value]))
            assert s.api_key_values(argparse.Namespace(api_key=[value]), check=False) == []
    else:
        assert s.api_key_values(argparse.Namespace()) == [] and s.api_key_values(s.parse_args(["codex"])) == []


def test_check_api_keys(scenario):
    if scenario == "happy":
        s.check_api_keys(s.parse_args(["codex", "--api-key", "dev.to=A", "--api-key", "api.medium.com=B"]),
                         {"A": "a-key", "B": "b-key"}.get)
    elif scenario == "sad":
        for argv, text in ((["--api-key", "dev.to=A", "--api-key", "api.medium.com=A"], "given twice"),
                           (["--api-key", "dev.to=A", "--secret-env", "A"], "given twice"),
                           (["--api-key", "dev.to=HOME"], "Sandbox-owned"),
                           (["--api-key", "dev.to=A", "--no-inspect", "DEV.TO"], "drop --no-inspect"),
                           (["--api-key", "dev.to=A", "--no-proxy"], "needs the connection proxy")):
            with pytest.raises(ValueError, match=text):
                s.check_api_keys(s.parse_args(["codex", *argv]), False)
        with pytest.raises(ValueError, match="no secret B"):  # not kept: refused before anything runs
            s.check_api_keys(s.parse_args(["codex", "--api-key", "dev.to=A", "--api-key", "api.medium.com=B"]),
                             {"A": "a-key"}.get)
    else:
        s.check_api_keys(s.parse_args(["codex", "--no-proxy"]))  # nothing to add: no proxy needed


def test_api_key_environment(scenario):
    if scenario == "happy":
        env = s.api_key_environment(s.parse_args(["codex", "--api-key", "dev.to=DEVTO_API_KEY=STORED"]))
        assert env == {"DEVTO_API_KEY": "sandbox-ai-api-key-placeholder-DEVTO_API_KEY"}
    elif scenario == "sad":
        with pytest.raises(ValueError):
            s.api_key_environment(argparse.Namespace(api_key=["not a host=KEY"]))
    else:
        assert s.api_key_environment(s.parse_args(["codex"])) == {}


def test_api_key_entries(scenario):
    a = s.parse_args(["codex", "--api-key", "dev.to=DEVTO_API_KEY", "--api-key", "api.medium.com=MEDIUM=STORED"])
    store = {"DEVTO_API_KEY": "devto-SECRET-1", "STORED": "medium-SECRET-2"}
    if scenario == "happy":
        assert s.api_key_entries(a, store.get) == [
            {"host": "dev.to", "port": 443, "name": "DEVTO_API_KEY",
             "placeholder": "sandbox-ai-api-key-placeholder-DEVTO_API_KEY", "key": "devto-SECRET-1"},
            {"host": "api.medium.com", "port": 443, "name": "MEDIUM",
             "placeholder": "sandbox-ai-api-key-placeholder-MEDIUM", "key": "medium-SECRET-2"}]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="no secret STORED; keep one with: sandbox-ai secrets set STORED"):
            s.api_key_entries(a, {"DEVTO_API_KEY": "x"}.get)
        for bad in ("two words", "line\r\nX-Injected: 1", ""):
            with pytest.raises(ValueError, match="not usable in a header"):
                s.api_key_entries(a, {"DEVTO_API_KEY": bad, "STORED": "ok"}.get)
    else:
        assert s.api_key_entries(s.parse_args(["codex"]), store.get) == []


@pytest.fixture
def publish_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(s, "_publish_logins_module", None)
    module = s.load_publish_logins_module()
    module.save([{"kind": "pypi", "host": "upload.pypi.org", "secret": "PYPI_TOKEN"},
                 {"kind": "npm", "host": "registry.npmjs.org", "secret": "NPM_SECRET"},
                 {"kind": "github", "host": "api.github.com", "secret": "GH_PUB"}])
    return module


def test_load_publish_logins_module(scenario, publish_home):
    if scenario == "happy":
        assert s.load_publish_logins_module() is publish_home and callable(publish_home.proxy_entries)
    elif scenario == "sad":
        assert "nosuch" not in publish_home.KINDS
    else:
        assert publish_home.path().name == "publish-logins.json"


def test_publish_kinds(scenario, publish_home):
    if scenario == "happy":
        assert s.publish_kinds(s.parse_args(["codex", "--publish", "pypi,npm", "--publish", "PyPI"])) == ["pypi", "npm"]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="no kind 'docker'"):
            s.publish_kinds(s.parse_args(["codex", "--publish", "docker"]))
    else:
        assert s.publish_kinds(s.parse_args(["codex"])) == [] and s.publish_kinds(argparse.Namespace()) == []


def test_publish_hosts(scenario, publish_home):
    if scenario == "happy":
        assert s.publish_hosts(s.parse_args(["codex", "--publish", "pypi,github"])) == [
            "upload.pypi.org", "api.github.com", "uploads.github.com"]
    elif scenario == "sad":
        assert s.publish_hosts(argparse.Namespace(publish=["crates"])) == []  # no login: validate() says so
    else:
        assert s.publish_hosts(s.parse_args(["codex"])) == []


def test_publish_environment(scenario, publish_home):
    if scenario == "happy":
        env = s.publish_environment(s.parse_args(["codex", "--publish", "pypi,npm"]))
        assert env["TWINE_USERNAME"] == "__token__" and env["TWINE_PASSWORD"] == env["UV_PUBLISH_TOKEN"] == \
            "sandbox-ai-publish-placeholder-pypi"
        assert env["NPM_TOKEN"] == "sandbox-ai-publish-placeholder-npm" and env["NPM_CONFIG_GLOBALCONFIG"] == s.NPMRC_TARGET
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--publish crates: no login"):
            s.publish_environment(argparse.Namespace(publish=["crates"]))
    else:
        assert s.publish_environment(s.parse_args(["codex"])) == {}
        assert "NPM_CONFIG_GLOBALCONFIG" not in s.publish_environment(s.parse_args(["codex", "--publish", "pypi"]))


def test_check_publish(scenario, publish_home):
    store = {"PYPI_TOKEN": "pypi-REAL", "NPM_SECRET": "npm_REAL", "GH_PUB": "ghp_REAL"}
    if scenario == "happy":
        s.check_publish(s.parse_args(["codex", "--publish", "pypi,npm,github"]), store.get)
    elif scenario == "sad":
        for argv, text in ((["--publish", "pypi", "--no-proxy"], "needs the connection proxy"),
                           (["--publish", "pypi", "--no-inspect", "upload.pypi.org"], "drop --no-inspect upload.pypi.org"),
                           (["--publish", "pypi", "--secret-env", "TWINE_PASSWORD"], "TWINE_PASSWORD is given twice"),
                           (["--publish", "crates"], "--publish crates: no login"),
                           (["--publish", "github", "--git-token-env", "https://github.com=GH"], "add --token-proxy")):
            with pytest.raises(ValueError, match=text):
                s.check_publish(s.parse_args(["codex", *argv]), False)
        with pytest.raises(ValueError, match="no secret NPM_SECRET"):
            s.check_publish(s.parse_args(["codex", "--publish", "npm"]), {}.get)
    else:
        s.check_publish(s.parse_args(["codex", "--no-proxy"]), {}.get)  # nothing to publish: nothing to check
        s.check_publish(s.parse_args(["codex", "--publish", "github", "--git-token-env", "https://github.com=GH",
                                      "--token-proxy"]), store.get)


@pytest.fixture
def identities_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(s, "_identities_module", None)
    module = s.load_identities_module()
    module.save({"c0decave": {"name": "dash", "email": "c0decave@users.noreply.github.com",
                              "git": [{"url": "https://github.com", "user": "c0decave", "secret": "GH_C0DECAVE"}],
                              "publish": [{"kind": "pypi", "host": "upload.pypi.org", "secret": "PYPI_C0DECAVE"}]},
                 "investigation": {"description": "OSINT", "git": [], "publish": []}})
    return module


def test_load_identities_module(scenario, identities_home):
    if scenario == "happy":
        assert s.load_identities_module() is identities_home and identities_home.path().name == "identities.json"
    elif scenario == "sad":
        with pytest.raises(ValueError, match="no such identity"):
            identities_home.get("nobody")
    else:
        assert identities_home.DEFAULT == "default"


def test_identity_of(scenario, identities_home):
    if scenario == "happy":
        name, identity = s.identity_of(s.parse_args(["codex", "--identity", "c0decave"]))
        assert name == "c0decave" and identity["email"] == "c0decave@users.noreply.github.com"
    elif scenario == "sad":
        for value, text in (("nobody", "no such identity .yours: c0decave, investigation"), ("Bad Name", "lower-case")):
            with pytest.raises(ValueError, match=text):
                s.identity_of(s.parse_args(["codex", "--identity", value]))
    else:
        assert s.identity_of(s.parse_args(["codex"])) == (None, None)
        assert s.identity_of(s.parse_args(["codex", "--identity", "default"])) == (None, None)


def test_check_identity(scenario, identities_home):
    if scenario == "happy":
        s.check_identity(s.parse_args(["codex", "--identity", "c0decave"]))
    elif scenario == "sad":
        with pytest.raises(ValueError, match="drop --git-identity"):
            s.check_identity(s.parse_args(["codex", "--identity", "c0decave", "--git-identity"]))
        a = s.parse_args(["codex", "--identity", "c0decave"])
        a.mcp_socket_dir = "/x"
        with pytest.raises(ValueError, match="not an MCP server's"):
            s.check_identity(a)
    else:
        s.check_identity(s.parse_args(["codex", "--git-identity"]))  # default: the host's identity as before


def test_identity_plan(scenario, identities_home):
    if scenario == "happy":
        assert s.identity_plan(s.parse_args(["codex", "--identity", "c0decave"])) == {
            "name": "c0decave", "commit_name": "dash", "commit_email": "c0decave@users.noreply.github.com",
            "git": ["https://github.com"], "publish": ["pypi"]}
    elif scenario == "sad":
        plan = s.identity_plan(s.parse_args(["codex", "--identity", "investigation"]))
        assert plan["git"] == [] and plan["commit_name"] is None  # an identity with no logins has none of default's
    else:
        assert s.identity_plan(s.parse_args(["codex"])) is None


def test_publish_logins_of(scenario, identities_home):
    if scenario == "happy":
        assert s.publish_logins_of(s.parse_args(["codex", "--identity", "c0decave"])) == [
            {"kind": "pypi", "host": "upload.pypi.org", "secret": "PYPI_C0DECAVE"}]
    elif scenario == "sad":
        assert s.publish_logins_of(s.parse_args(["codex", "--identity", "investigation"])) == []
    else:
        assert s.publish_logins_of(s.parse_args(["codex"])) is None  # default: sandbox-ai publish-logins


def test_git_credential(scenario, tmp_path):
    """The sandbox's Git helper for logins added during the run: "get" for a login's origin, nothing else."""
    import io
    tokens = tmp_path / "git-tokens.json"
    tokens.write_text(json.dumps({"version": 1, "logins": [
        {"url": "http://localhost:3080", "user": "alice", "token": "gitea_token_1"},
        {"url": "https://git.example.org", "user": "x-access-token", "token": "glpat_token_2"}]}))

    def ask(text, operation="get", path=tokens):
        out = io.StringIO()
        assert rt.git_credential(operation, io.StringIO(text), out, path=path) == 0  # never fails Git
        return out.getvalue()
    if scenario == "happy":
        assert ask("protocol=http\nhost=localhost:3080\npath=alice/repo.git\n\n") == \
            "username=alice\npassword=gitea_token_1\n"
        # The default port is the origin without it; the host name is not case-sensitive.
        assert ask("protocol=https\nhost=Git.Example.org:443\n") == "username=x-access-token\npassword=glpat_token_2\n"
        assert ask("protocol=http\nhost=localhost:3080\nusername=alice\n").startswith("username=alice")
    elif scenario == "sad":
        for text in ("protocol=http\nhost=localhost:3001\n", "protocol=https\nhost=localhost:3080\n",
                     "protocol=http\nhost=localhost:3080\nusername=mallory\n", "protocol=ftp\nhost=localhost:3080\n",
                     "host=localhost:3080\n", "", "protocol=https\nhost=git.example.org.evil.example\n"):
            assert ask(text) == "", text
        # store and erase change nothing: the agent cannot replace what the human gave.
        before = tokens.read_text()
        assert ask("protocol=http\nhost=localhost:3080\nusername=x\npassword=y\n", "store") == ""
        assert ask("protocol=http\nhost=localhost:3080\n", "erase") == "" and tokens.read_text() == before
    else:
        # No file (no login added), a damaged one, or a login with a line break: no answer, no error.
        assert ask("protocol=http\nhost=localhost:3080\n", path=tmp_path / "absent.json") == ""
        (tmp_path / "bad.json").write_text("{not json")
        assert ask("protocol=http\nhost=localhost:3080\n", path=tmp_path / "bad.json") == ""
        (tmp_path / "list.json").write_text("[]")
        assert ask("protocol=http\nhost=localhost:3080\n", path=tmp_path / "list.json") == ""
        (tmp_path / "evil.json").write_text(json.dumps({"logins": [
            {"url": "http://localhost:3080", "user": "a", "token": "t\nusername=evil"}]}))
        assert ask("protocol=http\nhost=localhost:3080\n", path=tmp_path / "evil.json") == ""


def test_open_request(scenario, tmp_path):
    import io
    out = io.StringIO()
    if scenario == "happy":
        short = Path(tempfile.mkdtemp(prefix="o-", dir="/tmp"))
        server = rt.listen_unix(short / "s.sock")

        def answer():
            connection, _ = server.accept()
            with connection:
                assert json.loads(connection.makefile("r").readline()) == {"url": "https://example.org/x"}
                connection.sendall(b'{"ok": true, "message": "asked"}\n')
        thread = threading.Thread(target=answer)
        thread.start()
        assert rt.open_request(["--new-window", "https://example.org/x"], str(short / "s.sock"), out) == 0
        thread.join(5)
        assert out.getvalue().strip() == "asked"
    elif scenario == "sad":
        assert rt.open_request(["https://example.org"], str(tmp_path / "missing.sock"), out) == 1
        assert "open it yourself" in out.getvalue()
    else:
        assert rt.open_request(["https://a", "https://b"], "/x", out) == 1
        assert rt.open_request(["ftp://example.org"], "/x", out) == 1


def test_write_browser_shims(scenario, tmp_path):
    directory = tmp_path / "bin"
    if scenario == "sad":
        directory.write_text("in the way")
        with pytest.raises(OSError):
            s.write_browser_shims(directory)
        return
    s.write_browser_shims(directory)
    if scenario == "happy":
        assert sorted(p.name for p in directory.iterdir()) == sorted(s.BROWSER_SHIMS)
        assert (directory / "xdg-open").read_text().endswith('--internal-open "$@"\n')
    else:
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o755 for p in directory.iterdir())
        s.write_browser_shims(directory)  # again: unchanged


def test_browser_bindings_and_environment(tmp_path):
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "w")])
    a.browser_dir = tmp_path / "run.browser"
    _, run = s.pod_commands(a, "p", tmp_path / "session", tmp_path / "home")
    assert f"type=bind,src={a.browser_dir},dst=/sandbox/browser,ro,nodev" in run
    cmd = s.bwrap_command(a, tmp_path, tmp_path)
    assert ["--ro-bind", str(a.browser_dir), "/sandbox/browser"] == cmd[cmd.index(str(a.browser_dir)) - 1:cmd.index(str(a.browser_dir)) + 2]
    env = s.agent_environment(a)
    assert env["BROWSER"] == "/sandbox/session/bin/xdg-open" and env["PATH"].startswith("/sandbox/session/bin:")
    assert "BROWSER" not in s.agent_environment(s.parse_args(["claude", "--ask", "off"]))


def test_question_name(scenario):
    if scenario == "happy":
        assert rt.question_name("API.Example.org.") == "api.example.org"
        assert rt.question_name("xn--bcher-kva.example") == "xn--bcher-kva.example"
    elif scenario == "sad":
        for bad in ("pypi.org\n1-1-1  x", "a b", "evil\x1b[8m.org", "b\u00fccher.example", "", "-x.org", "a..b"):
            assert rt.question_name(bad) is None, bad
    else:
        assert rt.question_name(None) is None and rt.question_name("a" * 254) is None
        assert rt.question_name("localhost") == "localhost"


def test_printable_text(scenario):
    if scenario == "happy":
        assert rt.printable_text("https://github.com/a/b") == "https://github.com/a/b"
    elif scenario == "sad":
        assert rt.printable_text("x\n\x1b[31m\u202e") == "x%0A%1B[31m%E2%80%AE"
        assert rt.printable_text("100%") == "100%25"  # unambiguous
    else:
        assert rt.printable_text("a" * 10, limit=4) == "aaaa" and rt.printable_text("") == ""


def test_write_new(scenario, tmp_path):
    target = tmp_path / "allow.json"
    if scenario == "happy":
        rt.write_new(target, "{}", 0o644)
        assert target.read_text() == "{}" and stat.S_IMODE(target.stat().st_mode) == 0o644
        rt.write_new(target, "[]", 0o600)
        assert target.read_text() == "[]" and stat.S_IMODE(target.stat().st_mode) == 0o600
    elif scenario == "sad":
        # A planted symlink at any temporary name is removed, never followed.
        victim = tmp_path / "victim"
        victim.write_text("keep")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}")
        temporary.symlink_to(victim)
        rt.write_new(target, "new", 0o644)
        assert victim.read_text() == "keep" and target.read_text() == "new" and not target.is_symlink()
    else:
        with pytest.raises(OSError):
            rt.write_new(tmp_path / "missing" / "x.json", "{}", 0o644)
        assert not (tmp_path / "missing").exists()


def test_review_findings_after_0_1_20(tmp_path, monkeypatch):
    """Findings of the review of approvals, secrets and browser requests, each kept closed."""
    rules = []
    network = rt.RuntimeNetwork({}, tmp_path / "allow.json", rules.append)
    # Approved destinations never take SSH; what the human allows with sandbox-ai net does.
    network.change({"op": "allow", "tcp": [["192.0.2.9", 443]]}, ssh=False)
    assert json.loads((tmp_path / "allow.json").read_text())["ssh_tcp"] == []
    network.change({"op": "allow", "tcp": [["192.0.2.9", 443]]})
    assert json.loads((tmp_path / "allow.json").read_text())["ssh_tcp"] == [["192.0.2.9", 443]]
    network.change({"op": "allow", "tcp": [["192.0.2.10", 22]]}, ssh=False)
    assert json.loads((tmp_path / "allow.json").read_text())["ssh_tcp"] == [["192.0.2.9", 443]]
    # IDs carry a random part: two sandboxes asking in the same second do not collide.
    one = rt.ApprovalBroker(tmp_path / "a1", network, timeout=0.2, poll=0.02)
    two = rt.ApprovalBroker(tmp_path / "a2", network, timeout=0.2, poll=0.02)
    one.ask("dns", {"name": "a.example"}, wait=False)
    two.ask("dns", {"name": "a.example"}, wait=False)
    ids = [json.loads(next((tmp_path / d).glob("*.json")).read_text())["id"] for d in ("a1", "a2")]
    assert ids[0] != ids[1]
    # A deny counts for the run (no re-asking to wear the human down); "once" may be asked again.
    broker = rt.ApprovalBroker(tmp_path / "a3", network, timeout=3, poll=0.02)
    details = {"destination": "198.51.100.1:443", "address": "198.51.100.1", "port": 443}
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", details)))
    thread.start()
    answer_file(tmp_path / "a3", "deny", "run")
    thread.join(5)
    assert results[0]["decision"] == "deny"
    assert broker.ask("connect", details)["decision"] == "deny" and len(list((tmp_path / "a3").glob("*.json"))) == 1
    # Open questions are capped; beyond that the agent is refused without asking.
    capped = rt.ApprovalBroker(tmp_path / "a4", network, timeout=5, poll=0.05)
    for n in range(rt.OPEN_QUESTIONS):
        capped.ask("dns", {"name": f"n{n}.example"}, wait=False)
    assert capped.ask("dns", {"name": "more.example"}, wait=False)["reason"] == "too many questions"
    assert len(list((tmp_path / "a4").glob("*.json"))) == rt.OPEN_QUESTIONS
    # A push answer covers the same warnings only; "always" is the run at most.
    assert rt.ApprovalBroker.key("push", {"url": "u", "message": "a"}) != rt.ApprovalBroker.key("push", {"url": "u", "message": "b"})
    # A name the agent controls opens only public addresses.
    broker._apply("dns", {"name": "x.example"}, {"addresses": ["10.0.0.5", "127.0.0.1", "151.101.0.223"], "scope": "run"})
    assert json.loads((tmp_path / "allow.json").read_text())["https"] == ["151.101.0.223/32"]
    # Command lines: URL passwords, secret-named flags, curl -u.
    for text in ("git clone https://oauth2:s3cr3t-value@git.example/x", "tool --api-key s3cr3t-value",
                 "tool --password=s3cr3t-value", "curl -u admin:s3cr3t-value https://x"):
        assert "s3cr3t-value" not in rt.redact_text(text), text
    assert rt.redact_text("ls -la --color=auto /work") == "ls -la --color=auto /work"


def test_remembered_approvals_inside_a_workspace_are_not_read(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / ".config/sandbox-ai").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(work / ".config"))
    (work / ".config/sandbox-ai/approved.json").write_text(json.dumps({str(work.resolve()): {"hosts": ["evil.example"]}}))
    a = s.parse_args(["claude", "--workspace", str(work)])
    notes = s.apply_remembered_approvals(a)
    assert "evil.example" not in a.allow_host and "not read" in notes[0]


def test_firewall_accepts_what_it_redirects_to_the_proxy():
    """Found by the e2e run on a real host: after the NAT redirect the filter hook still sees the original
    output interface, so the redirected connection must be accepted by destination, before any drop."""
    rules = s.firewall_rules([], ["192.0.2.1"], proxy={"port": 15001, "loopback": []})
    output = rules.split(" chain output ")[1].split("\n }")[0].splitlines()
    accepts = [i for i, line in enumerate(output) if "ct status dnat" in line]
    assert [output[i].strip() for i in accepts] == ["ct status dnat ip daddr 127.0.0.1 tcp dport 15001 accept",
                                                    "ct status dnat ip6 daddr ::1 tcp dport 15001 accept"]
    assert accepts[-1] < next(i for i, line in enumerate(output) if "jump runtime" in line)
    assert "ct status dnat" not in s.firewall_rules([], ["192.0.2.1"])  # without the proxy: no such door


def test_stdin_is_pipe(scenario, tmp_path):
    if scenario == "happy":
        r, w = os.pipe()
        try:
            assert rt.stdin_is_pipe(r)
        finally:
            os.close(r), os.close(w)
    elif scenario == "sad":
        with open(tmp_path / "f", "w") as stream:
            assert not rt.stdin_is_pipe(stream.fileno())  # /dev/null, a file, a terminal: never exits on its own
    else:
        assert not rt.stdin_is_pipe(987654)


def test_wait_for_unix_listener(scenario, tmp_path):
    import tempfile as tempfile_module
    path = tmp_path / "s.sock"
    errors = tempfile_module.TemporaryFile()
    if scenario == "happy":
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)
        rt.wait_for_unix_listener(path, Mock(poll=Mock(return_value=None)), errors, "x11vnc", timeout=5)
        server.close()
    elif scenario == "sad":
        errors.write(b"x11vnc: cannot open display\n")
        with pytest.raises(RuntimeError, match="exited with status 1: x11vnc: cannot open display"):
            rt.wait_for_unix_listener(path, Mock(poll=Mock(return_value=1)), errors, "x11vnc", timeout=5)
    else:
        bound = socket.socket(socket.AF_UNIX)
        bound.bind(str(path))  # the file is there, nobody listens yet
        with pytest.raises(RuntimeError, match="did not listen"):
            rt.wait_for_unix_listener(path, Mock(poll=Mock(return_value=None)), errors, "x11vnc", timeout=0.5)
        bound.close()


# --- state an earlier run may have left: symlinked workspaces, private directories ------------------


def test_logical_cwd(scenario, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(real)
    if scenario == "happy":  # the shell's name for the directory, symlink kept
        monkeypatch.setenv("PWD", str(link))
        assert s.logical_cwd() == str(link)
    elif scenario == "sad":  # a stale or foreign PWD is ignored
        monkeypatch.setenv("PWD", str(tmp_path))
        assert s.logical_cwd() == str(real)
    else:
        monkeypatch.setenv("PWD", "relative/path")
        assert s.logical_cwd() == str(real)


def test_planted_symlink(scenario, tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    planted = project / "frontend"
    planted.symlink_to(target)
    if scenario == "happy":  # a link inside a tree a sandbox could write
        assert s.planted_symlink(planted / "x", [project]) == planted
    elif scenario == "sad":  # your own link elsewhere (~/src -> /data/src) is fine
        assert s.planted_symlink(planted / "x", [tmp_path / "other"]) is None
    else:
        assert s.planted_symlink(project / "src", [project]) is None  # no link on the way


def test_check_planted(scenario, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    (project / "shared").symlink_to(config)
    if scenario == "happy":
        with pytest.raises(ValueError, match="goes through the symlink .*shared.*give the real path: .*config"):
            s.check_planted(project / "shared", "--mount", [project])
    elif scenario == "sad":
        s.check_planted(config, "--mount", [project])
    else:
        s.check_planted(str(project / "shared"), "--mount", [])  # no registry yet: nothing to compare


def test_exposed_registry(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert s.exposed_registry() == tmp_path / "sandbox-ai" / "exposed.json"
    elif scenario == "sad":
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert s.exposed_registry().parts[-3:] == ("state", "sandbox-ai", "exposed.json")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", "")
        assert s.exposed_registry().name == "exposed.json"


def test_exposed_before(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "sandbox-ai" / "exposed.json"
    path.parent.mkdir()
    if scenario == "happy":
        path.write_text(json.dumps(["/home/u/proj", "relative", 3]))
        assert s.exposed_before() == [Path("/home/u/proj")]
    elif scenario == "sad":
        path.write_text("not json")
        assert s.exposed_before() == []
    else:
        path.write_text(json.dumps({"not": "a list"}))
        assert s.exposed_before() == []


def test_remember_exposed(scenario, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work = tmp_path / "work"
    work.mkdir()
    a = s.parse_args(["codex", "--workspace", str(work)])
    if scenario == "happy":
        s.remember_exposed(a)
        s.remember_exposed(a)  # no duplicates
        assert s.exposed_before() == [work]
    elif scenario == "sad":  # an unwritable registry is a warning, not a failed start
        (tmp_path / "state").write_text("a file, not a directory")
        s.remember_exposed(a)
        assert "cannot record exposed directories" in capsys.readouterr().err
    else:  # the newest first, older ones kept
        (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
        (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text(json.dumps(["/old/tree"]))
        s.remember_exposed(a)
        assert s.exposed_before() == [work, Path("/old/tree")]


def test_session_workspaces(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sessions = tmp_path / "state" / "sandbox-ai" / "sessions"
    if scenario == "happy":  # the main workspace and those of -w
        (sessions / "web").mkdir(parents=True)
        (sessions / "web" / "session.json").write_text(json.dumps({"workspace": "/p/one",
                                                                    "options": ["-w", "/p/two,/p/three"]}))
        assert s.session_workspaces() == ["/p/one", "/p/two", "/p/three"]
    elif scenario == "sad":  # a damaged record is skipped
        (sessions / "bad").mkdir(parents=True)
        (sessions / "bad" / "session.json").write_text("not json")
        assert s.session_workspaces() == []
    else:  # no sessions at all
        assert s.session_workspaces() == []


def test_sandbox_writable(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    earlier, running, added, kept, work = (tmp_path / name for name in ("earlier", "running", "added", "kept", "work"))
    for folder in (earlier, running, added, kept, work):
        folder.mkdir()
    if scenario == "happy":  # this run's, earlier runs', the running sandboxes' (also added) and the kept sessions'
        s.remember_exposed(None, [earlier])
        run = s.runs_dir() / "r1"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"run": "r1", "name": "web", "workspace": str(running)}))
        (run / "workspaces.json").write_text(json.dumps({"workspaces": [{"host": str(added), "mode": "rw"},
                                                                       {"host": "/ro/one", "mode": "ro"}]}))
        session = tmp_path / "state" / "sandbox-ai" / "sessions" / "s"
        session.mkdir(parents=True)
        (session / "session.json").write_text(json.dumps({"workspace": str(kept)}))
        found = dict(s.sandbox_writable(s.parse_args(["codex", "--workspace", str(work)])))
        assert list(found) == [work, earlier, running, added, kept]
        assert found[running] == "the workspace of the sandbox web" and "earlier run" in found[earlier]
    elif scenario == "sad":  # damaged run records are skipped
        run = s.runs_dir() / "r1"
        run.mkdir(parents=True)
        (run / "run.json").write_text("not json")
        assert s.sandbox_writable() == []
    else:  # relative paths in a record are no folders
        s.remember_exposed(None, [earlier])
        run = s.runs_dir() / "r1"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"workspace": "relative"}))
        assert s.sandbox_writable() == [(earlier, "a folder a sandbox could write in an earlier run")]


def test_kit_check(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    project, ssh, work = tmp_path / "proj", tmp_path / ".ssh", tmp_path / "work"
    for folder in (project, ssh, work):
        folder.mkdir()
    (project / "skill").symlink_to(ssh)  # what an agent working on PROJECT planted
    a = s.parse_args(["claude", "--workspace", str(work)])
    if scenario == "happy":  # a link inside a folder an earlier sandbox could write: refused, naming it
        s.remember_exposed(None, [project])
        with pytest.raises(ValueError, match=r"Kit file \(kit x\): .*proj/skill -> .*\.ssh is a symlink inside "
                                             r".*proj \(a folder a sandbox could write in an earlier run\).*planted"):
            s.kit_check(a)(project / "skill", "kit x")
    elif scenario == "sad":  # inside this run's workspace: check_host_source refuses any path
        with pytest.raises(ValueError, match="lies inside .*work, which the agent can change"):
            s.kit_check(a)(work / "skill", "kit x")
    else:  # a link no sandbox could have written, and real folders: fine; without a run only links are checked
        s.kit_check(a)(project / "skill", "kit x")
        s.kit_check(a)(ssh, "kit x")
        s.kit_check()(work / "skill", "kit x")


def test_kit_source_notes(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    checkout = tmp_path / "src" / "proj"
    (checkout / "skills" / "foo").mkdir(parents=True)
    plan = {"entries": {"skills": {"foo": ("kit x", tmp_path / "kit" / "foo")}, "commands": {}}}
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "foo").symlink_to(checkout / "skills" / "foo")
    if scenario == "happy":  # the user's link to a skill in a checkout an agent worked on: said
        s.remember_exposed(None, [checkout])
        notes = s.kit_source_notes(plan)
        assert len(notes) == 1 and "comes from" in notes[0] and str(checkout) in notes[0]
    elif scenario == "sad":  # a dangling entry says nothing here (staging notes it)
        (tmp_path / "kit" / "foo").unlink()
        (tmp_path / "kit" / "foo").symlink_to(tmp_path / "gone")
        assert s.kit_source_notes(plan) == []
    else:  # nobody else writes there
        assert s.kit_source_notes(plan) == []


def test_private_locations(scenario, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    if scenario == "happy":
        monkeypatch.setenv("PATH", f"{home}/.cargo/bin:relative/bin")
        fixed, program_dirs = s.private_locations()
        assert home / ".ssh" in fixed and program_dirs == [home / ".cargo/bin"]
    elif scenario == "sad":
        monkeypatch.setenv("PATH", "")
        assert s.private_locations()[1] == []
    else:  # XDG directories wherever they are set
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        assert tmp_path / "cfg" in s.private_locations()[0]
        assert tmp_path / "cache" / "sandbox-ai" in s.private_locations()[0]


def test_sandbox_ai_folders(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    if scenario == "happy":
        assert set(s.sandbox_ai_folders()) == {tmp_path / ".config/sandbox-ai", tmp_path / ".local/share/sandbox-ai",
                                                tmp_path / ".local/state/sandbox-ai", tmp_path / ".cache/sandbox-ai"}
    elif scenario == "sad":
        # Security finding L2: both where XDG_* point and the default places (the app, the web UI, another start
        # may run with other values).
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert tmp_path / "cfg/sandbox-ai" in s.sandbox_ai_folders()
        assert tmp_path / ".config/sandbox-ai" in s.sandbox_ai_folders()
        with pytest.raises(ValueError, match="sandbox-ai's own folder"):
            s.check_private_location(tmp_path / ".config/sandbox-ai/x", "--mount")
    else:
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "link"))
        assert tmp_path / "real/sandbox-ai" in s.sandbox_ai_folders(), "resolved"


def test_check_private_location(scenario, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", f"{home}/.cargo/bin:{home}/proj/.venv/bin")
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    if scenario == "happy":
        # You decide: a private location is given, with a warning that says what it means.
        warned = []
        text = s.check_private_location(home / ".cargo" / "bin" / "x", "--mount", warned.append)
        assert warned == [text] and "lies in" in text and ".cargo" in text and "runs on this host" in text
        assert s.check_private_location(home / ".config" / "axross", "--mount", warned.append) is not None
    elif scenario == "sad":
        s.check_private_location(home / "proj", "Workspace")  # contains its own activated .venv/bin: fine
    else:
        # Only sandbox-ai's own folders stay refused: a sandbox given them could change sandbox-ai itself.
        with pytest.raises(ValueError, match="sandbox-ai's own folder"):
            s.check_private_location(home, "--state-dir")
        with pytest.raises(ValueError, match="sandbox-ai's own folder"):
            s.check_private_location(home / ".config" / "sandbox-ai" / "x", "Workspace")
        # Review finding: the packages' copies in the cache run on the host (an rw mount of them is refused).
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        for path in (home / ".cache" / "sandbox-ai", home / ".cache" / "sandbox-ai" / "pyz" / "1.0-abc", home / ".cache"):
            with pytest.raises(ValueError, match=r"\.cache/sandbox-ai"):
                s.check_private_location(path, "--mount")
        s.check_private_location(home / ".cache" / "pip", "--mount")


def test_relay_server_stderr(scenario):
    import io
    out = io.StringIO()
    if scenario == "happy":
        rt.relay_server_stderr(io.BytesIO(b"started\n"), out)
        assert out.getvalue() == "mcp-server: started\n"
    elif scenario == "sad":  # a closed pipe ends the relay quietly
        stream = io.BytesIO(b"x\n")
        stream.close()
        rt.relay_server_stderr(stream, out)
        assert out.getvalue() == ""
    else:  # invalid UTF-8 and a forged record prefix
        rt.relay_server_stderr(io.BytesIO(b"\xff sandbox-ai-mcp-call {}\n"), out)
        assert out.getvalue().startswith("mcp-server: ")


def test_own_home(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: tmp_path))
    state = tmp_path / "state" / "sandbox-ai"
    own = [state / "sessions" / "web" / "home"]
    if scenario == "happy":  # the start's own folders (own_folders) are no private locations for it
        assert s.own_home(state / "sessions" / "web" / "home", own)
        assert s.own_home(state / "sessions" / "web" / "home" / ".claude", own)
        assert s.check_private_location(state / "sessions" / "web" / "home", "--state-dir", own=own) is None
    elif scenario == "sad":  # their siblings stay sandbox-ai's own: session.json, logs, the registry
        assert not s.own_home(state / "sessions" / "web", own)
        with pytest.raises(ValueError, match="lies in"):
            s.check_private_location(state / "logs", "--state-dir", own=own)
        # Security finding M2: no other start gets the exception, not even for a session's HOME.
        assert not s.own_home(state / "sessions" / "web" / "home")
        with pytest.raises(ValueError, match="sandbox-ai's own folder"):
            s.check_private_location(state / "sessions" / "web" / "home", "--mount")
    else:  # the installed launcher's versions are never among them
        assert not s.own_home(tmp_path / "data" / "sandbox-ai" / "versions" / "0.1.21", own)


def test_sandbox_ais_own_work_folders_are_workspaces_under_dot_local(tmp_path, monkeypatch):
    """The default layout (no XDG_* set): everything below ~/.local. The work folders of sandbox-ai's own
    sandboxes are their workspaces: a login's (sandbox-ai login refused every login on a new host), an MCP
    server's without one of its own (the demo server), a team's board. Their siblings stay refused, and (security
    finding M2) so do they for any start but that one."""
    for name in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    local = tmp_path / ".local"
    login = local / "share/sandbox-ai/auth/.login-claude-abc"
    mcp = local / "state/sandbox-ai/mcp"
    starts = [  # (the start's options, the folders it may be given)
        (["claude", "--no-auth", "--state-dir", str(login / "home"), "--workspace", str(login / "work")],
         [login / "work", login / "home"]),
        (["codex", "--mcp-socket-dir", str(mcp / "demo/sock"), "--state-dir", str(mcp / "demo/home"),
          "--workspace", str(mcp / "demo/work")], [mcp / "demo/work", mcp / "demo/home"]),
        (["codex", "--mcp-socket-dir", str(mcp / "team-app/sock"), "--workspace",
          str(local / "state/sandbox-ai/teams/app/board")], [local / "state/sandbox-ai/teams/app/board"])]
    refused = [local / "share/sandbox-ai/auth", local / "share/sandbox-ai/auth/claude",
               local / "state/sandbox-ai/mcp/demo", local / "state/sandbox-ai/mcp/demo/sock",
               local / "state/sandbox-ai/teams/app", local / "state/sandbox-ai/teams", local / "share/sandbox-ai"]
    for _, allowed in starts:
        for path in allowed:
            path.mkdir(parents=True, exist_ok=True)
    for path in refused:
        path.mkdir(parents=True, exist_ok=True)
    for options, allowed in starts:
        own = s.own_folders(s.parse_args(options))
        for path in allowed:
            s.check_workspace_scope(path.resolve(), own)
            with pytest.raises(ValueError, match="sandbox-ai's own folder"):
                s.check_workspace_scope(path.resolve())  # any other start
        for path in refused:
            with pytest.raises(ValueError, match="lies in|contains"):
                s.check_workspace_scope(path.resolve(), own)
    # Another login's or server's folders are not this start's own.
    other = s.own_folders(s.parse_args(["claude", "--state-dir", str(login / "home"), "--workspace",
                                        str(tmp_path / "project")]))
    assert other == []


def test_project_tool_dir(scenario, tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    if scenario == "happy":
        assert s.project_tool_dir(tmp_path / "proj" / ".venv" / "bin", tmp_path / "proj")
    elif scenario == "sad":  # ~/.cargo as a workspace would let the agent replace ~/.cargo/bin/cargo
        home = tmp_path / "home"
        monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("PATH", f"{home}/.cargo/bin")
        assert not s.project_tool_dir(home / ".cargo" / "bin", home / ".cargo")
        assert "contains" in s.check_private_location(home / ".cargo", "Workspace", lambda line: None)
    else:
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "proj" / "env"))
        assert s.project_tool_dir(tmp_path / "proj" / "env" / "bin", tmp_path / "proj")


def test_seed_exposed(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "cfg" / "sandbox-ai").mkdir(parents=True)
    if scenario == "happy":  # projects of approvals, trusted configurations, sessions and MCP definitions
        (tmp_path / "cfg" / "sandbox-ai" / "approved.json").write_text(json.dumps({"/p/one": {}}))
        (tmp_path / "cfg" / "sandbox-ai" / "trusted.json").write_text(json.dumps({"/p/two/.sandbox-ai.json": "x"}))
        (tmp_path / "cfg" / "sandbox-ai" / "mcp.json").write_text(json.dumps({"bw": {
            "image": "x", "mounts": [{"host": "/p/out", "target": "/out", "mode": "rw"}]}}))
        session = tmp_path / "state" / "sandbox-ai" / "sessions" / "web"
        session.mkdir(parents=True)
        (session / "session.json").write_text(json.dumps({"workspace": "/p/three"}))
        assert s.seed_exposed() == [Path("/p/one"), Path("/p/two"), Path("/p/three"), Path("/p/out")]
        assert s.exposed_before() == s.seed_exposed()  # used until the registry is written
    elif scenario == "sad":
        (tmp_path / "cfg" / "sandbox-ai" / "approved.json").write_text("broken")
        assert s.seed_exposed() == []
    else:
        (tmp_path / "cfg" / "sandbox-ai" / "approved.json").write_text(json.dumps({"relative": {}}))
        assert s.seed_exposed() == []


def test_shortened(scenario):
    if scenario == "happy":  # long secrets keep their closing quote, so the redaction still matches
        record = rt.tool_call_record(json.dumps({"method": "tools/call", "params": {
            "name": "x", "arguments": {"client_secret": "ya29." + "A" * 9000}}}).encode())
        assert "AAAAAAAAAA" not in record["arguments"]
    elif scenario == "sad":
        assert rt.shortened({"k": ["v"] * 500})["k"][-1] == "..."
    else:
        assert rt.shortened(7) == 7 and rt.shortened("x" * 501).endswith("...")


def test_private_directories_are_no_workspaces(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "systemd").mkdir(parents=True)
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # You decide what a sandbox gets: ~/.config/systemd is given (with a warning naming what it means); only a
    # folder that holds or lies in sandbox-ai's own (~/.config itself holds ~/.config/sandbox-ai) is refused.
    s.check_workspace_scope(home / ".config" / "systemd")
    with pytest.raises(ValueError, match="sandbox-ai's own folder"):
        s.check_workspace_scope(home / ".config")
    s.check_workspace_scope(home / "src" / "app")  # a project directory in HOME is fine


def test_overlapping_once_approvals_keep_the_target_open_until_the_last_ends():
    rules, timers = [], []
    network = rt.RuntimeNetwork({}, None, rules.append)
    for _ in range(2):
        network.allow_once("198.51.100.1", 443, timer=lambda seconds, function: timers.append(function) or Mock())
    timers[0]()
    assert "198.51.100.1" in rules[-1]  # the second approval is still running
    timers[1]()
    assert "198.51.100.1" not in rules[-1]


def test_mounts_changed(scenario, tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    info = os.stat(folder)
    record = [{"path": str(folder), "dev": info.st_dev, "ino": info.st_ino}]
    if scenario == "happy":
        assert rt.mounts_changed(record) is None and rt.mounts_changed([]) is None
    elif scenario == "sad":  # swapped for a link to another folder while the server ran
        other = tmp_path / "config"
        other.mkdir()
        folder.rmdir()
        folder.symlink_to(other)
        assert "has become a symlink" in rt.mounts_changed(record)
    else:  # replaced by another directory of the same name
        folder.rmdir()
        folder.mkdir()
        (tmp_path / "keep").mkdir()  # the old inode must not come back
        # ext4 may hand the same inode out again at once: then nothing can tell the two apart, and nothing is said.
        found = rt.mounts_changed(record)
        assert os.stat(folder).st_ino == info.st_ino or "replaced" in found


def test_image_mount_hosts(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "sandbox-ai").mkdir()
    path = tmp_path / "sandbox-ai" / "mcp.json"
    if scenario == "happy":
        path.write_text(json.dumps({"bw": {"image": "x", "mounts": [{"host": str(tmp_path / "out"), "target": "/o"}]}}))
        assert s.image_mount_hosts() == [(tmp_path / "out").resolve()]
    elif scenario == "sad":
        assert s.image_mount_hosts() == []  # no mcp.json
    else:
        path.write_text(json.dumps({"bw": {"mounts": ["not a dict"]}, "x": []}))
        assert s.image_mount_hosts() == []
        # Image servers of the trusted project file count too.
        project = {"mcp_servers": {"p": {"image": "x", "mounts": [{"host": str(tmp_path / "p"), "target": "/o"}]}}}
        assert s.image_mount_hosts(project) == [(tmp_path / "p").resolve()]


def test_workspace_holding_an_image_mount_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work = tmp_path / "data"
    (work / "extracted").mkdir(parents=True)
    (tmp_path / "cfg" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "cfg" / "sandbox-ai" / "mcp.json").write_text(json.dumps(
        {"bw": {"image": "x", "mounts": [{"host": str(work / "extracted"), "target": "/out", "mode": "rw"}]}}))
    with pytest.raises(ValueError, match="an MCP image server mounts"):
        s.validate(s.parse_args(["codex", "--workspace", str(work), "--no-config"]))
    s.validate(s.parse_args(["codex", "--workspace", str(work / "extracted"), "--no-config"]))  # the folder itself


def test_tool_dirs_state(scenario, tmp_path, monkeypatch):
    work = tmp_path / "proj"
    tools = work / ".venv" / "bin"
    tools.mkdir(parents=True)
    (tools / "python").write_text("x")
    a = argparse.Namespace(workspaces=[str(work)])
    if scenario == "happy":
        monkeypatch.setenv("PATH", f"{tools}:/usr/bin")
        assert list(s.tool_dirs_state(a)) == [str(tools)] and "python" in s.tool_dirs_state(a)[str(tools)]
    elif scenario == "sad":
        monkeypatch.setenv("PATH", "/usr/bin:relative")
        assert s.tool_dirs_state(a) == {}
    else:
        monkeypatch.setenv("PATH", str(tmp_path / "missing"))
        assert s.tool_dirs_state(a) == {}


def test_a_symlink_loop_in_a_tool_directory_is_a_change_not_a_crash(tmp_path, monkeypatch):
    """The agent turns a PATH directory of the workspace into a symlink loop: the check after the run names it
    (Path.resolve raises RuntimeError there before Python 3.13) instead of ending the launcher before the login
    write-back and the privacy check."""
    work = tmp_path / "proj"
    venv, modules = work / ".venv" / "bin", work / "node_modules" / ".bin"
    for directory in (venv, modules):
        directory.mkdir(parents=True)
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv), str(modules)]))
    before = s.tool_dirs_state(argparse.Namespace(workspaces=[str(work)]))
    shutil.rmtree(work / "node_modules")
    (work / "node_modules").symlink_to(work / "node_modules")
    notes = s.tool_dirs_changed(before)
    assert any("replaced" in note and "node_modules" in note for note in notes)
    assert s.tool_dirs_state(argparse.Namespace(workspaces=[str(work)]))  # a start with such a PATH still works


def test_tool_dirs_changed(scenario, tmp_path, monkeypatch):
    work = tmp_path / "proj"
    tools = work / "node_modules" / ".bin"
    tools.mkdir(parents=True)
    (tools / "eslint").write_text("x")
    monkeypatch.setenv("PATH", str(tools))
    before = s.tool_dirs_state(argparse.Namespace(workspaces=[str(work)]))
    if scenario == "happy":  # the agent put a git where your shell finds it first
        (tools / "git").write_text("#!/bin/sh\n")
        assert "git" in s.tool_dirs_changed(before)[0] and "on your PATH" in s.tool_dirs_changed(before)[0]
    elif scenario == "sad":
        assert s.tool_dirs_changed(before) == []
    else:  # same size, mtime reset: the ctime still tells
        stat_before = os.stat(tools / "eslint")
        time.sleep(0.01)
        (tools / "eslint").write_text("y")
        os.utime(tools / "eslint", ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        assert "eslint" in s.tool_dirs_changed(before)[0]
        # The directory, or a parent of it, swapped for a link to one the agent filled: named, not compared away.
        evil = work / "evil"
        evil.mkdir()
        (evil / "git").write_text("#!/bin/sh\n")
        os.rename(tools, work / "node_modules" / "old")
        os.symlink(evil, tools)
        assert "replaced" in s.tool_dirs_changed(before)[0]
        os.unlink(tools)
        os.rename(work / "node_modules", work / "nm-old")
        os.symlink(work / "nm-old", work / "node_modules")  # a parent is a link now
        assert "replaced" in s.tool_dirs_changed(before)[0]
        # A PATH entry that leads out of the workspace through a link in it is recorded as written, and a swap of
        # that link for a folder of the agent's is named (the venv kept outside a synced folder).
        outside = tmp_path / "virtualenvs" / "proj"
        (outside / "bin").mkdir(parents=True)
        (outside / "bin" / "python").write_text("real")
        os.symlink(outside, work / ".venv")
        monkeypatch.setenv("PATH", str(work / ".venv" / "bin"))
        kept = s.tool_dirs_state(argparse.Namespace(workspaces=[str(work)]))
        assert list(kept) == [str(work / ".venv" / "bin")]
        os.unlink(work / ".venv")
        (work / ".venv" / "bin").mkdir(parents=True)
        (work / ".venv" / "bin" / "python").write_text("the agent's")
        assert "replaced" in s.tool_dirs_changed(kept)[0]


# --- the administrator's policy -------------------------------------------------------------------


def policy_file(tmp_path, document, monkeypatch, owner=0, mode=0o644):
    """A policy file as root would own it (os.lstat/os.stat report OWNER and MODE)."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    real_lstat, real_stat = os.lstat, os.stat

    def fake(function):
        def call(target, *args, **kwargs):
            info = function(target, *args, **kwargs)
            if Path(target) in (path, path.parent):
                values = list(info)
                values[stat.ST_UID], values[stat.ST_MODE] = owner, (info.st_mode & ~0o7777) | mode
                return os.stat_result(values)
            return info
        return call
    monkeypatch.setattr(s.os, "lstat", fake(real_lstat))
    monkeypatch.setattr(s.os, "stat", fake(real_stat))
    return path


def test_load_policy(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        path = policy_file(tmp_path, {"version": 1, "forbid": ["--egress-all-allow", "--ask=off"],
                                      "max_approval_scope": "run"}, monkeypatch)
        assert s.load_policy(path)["max_approval_scope"] == "run"
        assert s.load_policy(tmp_path / "missing.json") == {}  # no policy: nothing changes
    elif scenario == "sad":  # a file the user could edit is no policy: every start stops
        path = policy_file(tmp_path, {"version": 1}, monkeypatch, owner=os.getuid() or 1000)
        with pytest.raises(ValueError, match="owned by root"):
            s.load_policy(path)
    else:
        path = policy_file(tmp_path, {"version": 1, "forbid": ["--no-such-option"]}, monkeypatch)
        with pytest.raises(ValueError, match="unknown option"):
            s.load_policy(path)
        path = policy_file(tmp_path, {"version": 1}, monkeypatch, mode=0o666)
        with pytest.raises(ValueError, match="writable by root only"):
            s.load_policy(path)


def test_policy_require_signed_updates(tmp_path, monkeypatch):
    """install.sh reads require_signed_updates itself; the launcher must accept the key, but only as a boolean."""
    path = policy_file(tmp_path, {"version": 1, "require_signed_updates": True}, monkeypatch)
    assert s.load_policy(path)["require_signed_updates"] is True
    path.write_text(json.dumps({"version": 1, "require_signed_updates": "yes"}))
    with pytest.raises(ValueError, match="require_signed_updates is true or false"):
        s.load_policy(path)


def test_check_policy(scenario):
    policy = {"version": 1, "forbid": ["--egress-all-allow", "--privacy-check=off", "--no-proxy"],
              "backends": ["podman"], "max_approval_scope": "once", "message": "ask IT"}
    if scenario == "happy":
        a = s.parse_args(["codex", "--allow-pypi"])
        s.check_policy(a, policy)
        assert a.max_approval_scope == "once"
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--privacy-check=off is not allowed.*ask IT"):
            s.check_policy(s.parse_args(["codex", "-pc", "off"]), policy)  # a short form counts too
        with pytest.raises(ValueError, match="backend bwrap"):
            s.check_policy(s.parse_args(["codex", "--backend", "bwrap"]), policy)
    else:  # whatever sets the option: a profile that implies --egress-all-allow is refused as well
        a = s.parse_args(["codex"])
        a.egress_all_allow = True
        with pytest.raises(ValueError, match="--egress-all-allow"):
            s.check_policy(a, policy)
        empty = s.parse_args(["codex"])
        s.check_policy(empty, {})
        assert empty.max_approval_scope == "always"


def test_policy_caps_the_scope_of_answers(tmp_path):
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda rules: None), max_scope="run")
    (tmp_path / "a" / "1-1-1.answer").write_text(json.dumps({"decision": "allow", "scope": "always"}))
    assert broker._read_answer("1-1-1")["scope"] == "run"


def test_http_rules(scenario):
    if scenario == "happy":
        a = s.parse_args(["codex", "--allow-http", "api.github.com GET,HEAD /repos/**", "-ah", "*.example.org * /"])
        assert s.http_rules(a) == [{"host": "api.github.com", "methods": ["GET", "HEAD"], "path": "/repos/**"},
                                   {"host": "*.example.org", "methods": ["*"], "path": "/"}]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="HOST METHODS PATH"):
            s.http_rules(s.parse_args(["codex", "--allow-http", "api.github.com get /x"]))
    else:  # rules for a host whose TLS passes unopened could never hold
        with pytest.raises(ValueError, match="--no-inspect"):
            s.http_rules(s.parse_args(["codex", "--allow-http", "api.github.com GET /", "--no-inspect", "*.github.com"]))


GH_RULES = {"version": 1, "http": [{"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny"}],
            "mcp": [{"id": "fs-write", "server": "files", "tool": "write_file", "decision": "ask"}]}


def test_user_rules_file(scenario, monkeypatch, tmp_path):
    if scenario == "happy":
        assert s.user_rules_file() == Path.home() / ".config/sandbox-ai/rules.json"
    elif scenario == "sad":
        assert not s.user_rules_file().exists()  # optional: none by default
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert s.user_rules_file() == tmp_path / "cfg/sandbox-ai/rules.json"


def test_read_ruleset(scenario, tmp_path):
    path = tmp_path / "rules.json"
    if scenario == "happy":
        path.write_text(json.dumps(GH_RULES))
        checked = s.read_ruleset(path, "--rules rules.json")
        assert checked["http"][0]["host"] == ["api.github.com"] and checked["http"][0]["methods"] == ["*"]
        assert checked["mcp"][0]["server"] == ["files"] and checked["defaults"] == {"http": "allow", "mcp": "allow",
                                                                                  "devices": "allow",
                                                                                  "tool": "allow"}
    elif scenario == "sad":
        for text, message in (('{"version": 1, "http": [{"host": "x"}]}', '"id" is required'),
                              ('{"version": 1, "version": 1}', "given twice"), ("{", "not valid JSON"),
                              ('{"version": 2}', '"version": 1'), ('{"version": 1, "http": NaN}', "not valid JSON")):
            path.write_text(text)
            with pytest.raises(ValueError, match=message):
                s.read_ruleset(path, "x")
        with pytest.raises(ValueError, match="No such file"):
            s.read_ruleset(tmp_path / "absent.json", "x")
    else:
        path.write_bytes(b" " * (s.CONFIG_LIMIT + 1))
        with pytest.raises(ValueError, match="larger than"):
            s.read_ruleset(path, "x")
        os.mkfifo(tmp_path / "fifo")
        with pytest.raises(ValueError, match="regular file"):
            s.read_ruleset(tmp_path / "fifo", "x")  # refused, never waited on


def test_load_rulesets(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(GH_RULES))
    if scenario == "happy":
        (tmp_path / "cfg/sandbox-ai").mkdir(parents=True)
        s.user_rules_file().write_text(json.dumps({"version": 1, "defaults": {"mcp": "ask"}}))
        a = s.parse_args(["codex", "--rules", str(mine), "-ru", str(mine), "--allow-http", "api.github.com GET /**"])
        s.check_policy(a, {"version": 1, "rules": {"version": 1, "http": [
            {"id": "admin", "host": "*", "methods": "DELETE", "decision": "ask"}]}})
        a.project_document = {"rules": {"version": 1, "mcp": [{"id": "p", "tool": "*", "decision": "deny"}]}}
        names = [source["name"] for source in s.load_rulesets(a)]
        # In order; two files of one name are told apart.
        assert names == ["policy", "rules.json", "--rules mine.json", "--rules mine.json (2)", "--allow-http", "project",
                         "telemetry"]  # --telemetry off: the agent's telemetry refused by the proxy too
        assert s.load_rulesets(a) is a.rulesets  # read once
        assert s.load_rulesets(a)[4]["http"][0]["id"] == "allow-http-1"
    elif scenario == "sad":
        mine.write_text('{"version": 1, "http": [{"id": "x", "host": "api.github.com", "decision": "maybe"}]}')
        with pytest.raises(ValueError, match="decision"):
            s.load_rulesets(s.parse_args(["codex", "--rules", str(mine)]))
    else:
        # Nothing configured: only the proxy's part of --telemetry off; with it on, nothing decided by rules.
        assert [r["name"] for r in s.load_rulesets(s.parse_args(["codex"]))] == ["telemetry"]
        assert s.load_rulesets(s.parse_args(["codex", "--telemetry", "on"])) == []


def test_check_rulesets(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(GH_RULES))
    if scenario == "happy":
        s.check_rulesets(s.parse_args(["codex", "--rules", str(mine), "--no-inspect", "pypi.org"]))
        s.check_rulesets(s.parse_args(["codex", "--rules", str(mine), "--offline"]))  # nothing to reach
    elif scenario == "sad":
        with pytest.raises(ValueError, match="need the connection proxy"):
            s.check_rulesets(s.parse_args(["codex", "--rules", str(mine), "--no-proxy"]))
    else:  # TLS passed through unopened: rules for that host could never hold
        for host in ("*.github.com", "api.github.com"):
            with pytest.raises(ValueError, match="cannot hold"):
                s.check_rulesets(s.parse_args(["codex", "--rules", str(mine), "--no-inspect", host]))
        mine.write_text('{"version": 1, "defaults": {"http": "ask"}}')
        with pytest.raises(ValueError, match="cannot hold"):
            s.check_rulesets(s.parse_args(["codex", "--rules", str(mine), "--no-inspect", "pypi.org"]))


def test_mcp_gateway(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(GH_RULES))
    a = s.parse_args(["codex", "--rules", str(mine)])
    if scenario == "happy":
        config = s.mcp_gateway(a, "files", "/sandbox/mcp-approvals")
        assert config["server"] == "files" and config["approvals"] == "/sandbox/mcp-approvals"
        assert [rule["id"] for rule in config["rules"][0]["mcp"]] == ["fs-write"] and "http" not in config["rules"][0]
    elif scenario == "sad":
        assert s.mcp_gateway(a, "other", "/x") is None  # no rule for that server: no gateway rules
    else:
        mine.write_text(json.dumps({"version": 1, "mcp": [{"id": "d", "tool": "rm*", "decision": "deny"}]}))
        config = s.mcp_gateway(s.parse_args(["codex", "--rules", str(mine)]), "any", "/x")
        assert "approvals" not in config  # nothing can ask: no answers to read


def test_sandbox_identity(scenario, tmp_path):
    if scenario == "happy":
        a = s.parse_args(["claude", "--backend", "bwrap"])
        a.run_id = "claude-1a2b3c4d"
        assert s.sandbox_identity(a) == {"run": "claude-1a2b3c4d", "agent": "claude", "backend": "bwrap", "role": "agent"}
    elif scenario == "sad":  # before the run has an ID: named by what is known
        assert s.sandbox_identity(s.parse_args(["codex"])) == {"agent": "codex", "backend": "podman", "role": "agent"}
    else:  # an MCP server's sandbox: named by its server
        a = s.parse_args(["codex", "--mcp-socket-dir", str(tmp_path / "files/sock"), "--pod-name", "agent-mcp-files"])
        assert s.sandbox_identity(a) == {"agent": "codex", "backend": "podman", "role": "mcp-server",
                                         "pod": "agent-mcp-files", "mcp": "files"}


def test_proxy_rules(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(GH_RULES))
    hosts = {"api.github.com": ["192.0.2.21"]}
    if scenario == "happy":
        config = s.proxy_rules(s.parse_args(["codex", "--rules", str(mine)]), hosts)
        assert [source["name"] for source in config["rules"]] == ["--rules mine.json", "telemetry"]
        assert config["rules"][0]["http"][0]["id"] == "no-gists" and "mcp" not in config["rules"][0]
        assert config["pinned"] == hosts
    elif scenario == "sad":
        assert s.proxy_rules(s.parse_args(["codex", "--telemetry", "on"]), hosts) == {}
    else:  # --allow-http travels as http_rules, not twice
        assert s.proxy_rules(s.parse_args(["codex", "--telemetry", "on", "--allow-http", "api.github.com GET /**"]),
                             hosts) == {}
        assert s.proxy_config(s.parse_args(["codex", "--rules", str(mine)]), hosts, "l")["rules"][0]["http"]


def test_host_matches_any(scenario):
    if scenario == "happy":
        assert s.host_matches_any("api.github.com", "*.github.com") and s.host_matches_any("a.b", "a.b")
    elif scenario == "sad":
        assert not s.host_matches_any("github.com.evil.example", "github.com")
    else:
        assert s.host_matches_any("*.github.com", "github.com")


def test_proxy_config_carries_http_rules_with_pinned_addresses():
    a = s.parse_args(["codex", "--allow-http", "api.github.com POST /repos/*/issues"])
    config = s.proxy_config(a, {"api.github.com": ["192.0.2.21"], "pypi.org": ["192.0.2.30"]}, "l")
    assert config["http_rules"] == [{"host": "api.github.com", "methods": ["POST"], "path": "/repos/*/issues",
                                     "addresses": ["192.0.2.21"]}]


def test_policy_network_ceiling_and_mcp(tmp_path, monkeypatch):
    policy = {"version": 1, "allow_within": ["10.0.0.0/8"], "mcp_network": "none"}
    s.check_policy(s.parse_args(["codex", "--allow", "10.1.0.0/16", "--allow-tcp", "10.2.3.4:5432"]), policy)
    for options in (["--allow", "0.0.0.0/1"], ["--allow-tcp", "192.0.2.9:22"], ["--egress-all-allow"]):
        with pytest.raises(ValueError, match="administrator"):
            s.check_policy(s.parse_args(["codex", *options]), policy)
    a = s.parse_args(["codex"])
    s.check_policy(a, {"version": 1, "forbid": ["--no-proxy"], "allow_within": ["10.0.0.0/8"]})
    assert a.policy == {"forbid": ["--no-proxy"], "allow_within": ["10.0.0.0/8"]}  # handed to the supervisor


def test_runtime_changes_obey_the_policy(tmp_path):
    """sandbox-ai net allow and approvals during a run: the supervisor holds the ceiling and the forbidden options."""
    network = rt.RuntimeNetwork({}, None, lambda rules: None, within=["10.0.0.0/8"])
    network.change({"op": "allow", "nets": ["10.9.0.0/16"]})
    with pytest.raises(ValueError, match="outside the networks"):
        network.change({"op": "allow", "nets": ["0.0.0.0/1"]})
    with pytest.raises(ValueError, match="outside the networks"):
        network.allow_once("192.0.2.9", 443)
    import socket as socket_module
    path = tmp_path / "control.sock"
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(path, rt.RuntimeNetwork({}, None, lambda r: None), stop,
                                                               ["--allow", "--egress-all-allow"]), daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)

    def ask(request):
        with socket_module.socket(socket_module.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall((json.dumps(request) + "\n").encode())
            return json.loads(client.makefile().readline())
    assert "not allowed by the administrator" in ask({"op": "allow", "nets": ["192.0.2.0/24"]})["error"]
    assert ask({"op": "allow", "tcp": [["192.0.2.9", 443]]})["ok"]
    stop.set()
    thread.join(5)


def test_host_name(scenario):
    if scenario == "happy":
        assert rt.host_name("API.github.com:443") == "api.github.com"
    elif scenario == "sad":
        assert rt.host_name(None) is None and rt.host_name("") is None
    else:
        assert rt.host_name("[2001:db8::1]:8443") == "2001:db8::1" and rt.host_name("[broken") is None


def test_host_authority(scenario):
    """Review 5 H1: what a Host header names: the name (case, trailing dot ignored) and the port it gives."""
    if scenario == "happy":
        assert rt.host_authority("API.GitHub.com.") == ("api.github.com", None)
        assert rt.host_authority("api.github.com:8443") == ("api.github.com", 8443)
        assert rt.host_authority("10.0.0.5") == ("10.0.0.5", None)
    elif scenario == "sad":
        for value in (None, "", "a b", "user@host", "host:", "host:99999", "host:0", "host/x", "[not-v6]",
                      "[10.0.0.1]"):
            assert rt.host_authority(value) is None, value
    else:  # IPv6 in brackets, in its usual form
        assert rt.host_authority("[2001:DB8:0::1]:443") == ("2001:db8::1", 443)


def test_ssh_banner(scenario):
    """Review 5 L2: an SSH banner on any early line, not only the first."""
    if scenario == "happy":
        assert rt.ssh_banner(b"SSH-2.0-OpenSSH_9.6\r\n") and rt.ssh_banner(b"junk\r\nSSH-2.0-Go\r\n")
    elif scenario == "sad":
        assert not rt.ssh_banner(b"GET / HTTP/1.1\r\n") and not rt.ssh_banner(b"") and not rt.ssh_banner(None)
    else:  # only the first 255 bytes count (as the protocol allows lines before the server's banner)
        assert not rt.ssh_banner(b"x" * 300 + b"\nSSH-2.0-late")


def test_ambiguous_path(scenario):
    """Review 5 L1: a dot segment after decoding makes a path read differently by server."""
    if scenario == "happy":
        assert rt.ambiguous_path("/b/x%2F..%2F..%2Fa/v1") and rt.ambiguous_path("/b/x\\..\\a")
    elif scenario == "sad":
        assert not rt.ambiguous_path("/v1/chat/completions?x=..") and not rt.ambiguous_path("/api/v4/projects/g%2Fp")
    else:  # double encoding, parameters and plain dot segments
        assert rt.ambiguous_path("/a/%252e%252e/b") and rt.ambiguous_path("/a/..;/b") and rt.ambiguous_path("/a/./b")


def test_pkt_sections(scenario):
    """Review 5 L3: the commands and then the push options of a receive-pack request."""
    data = rt.pkt(b"cmd") + b"0000" + rt.pkt(b"opt1") + rt.pkt(b"opt2") + b"0000" + b"PACK"
    if scenario == "happy":
        assert rt.pkt_sections(data, 2) == ([[b"cmd"], [b"opt1", b"opt2"]], True)
    elif scenario == "sad":
        assert rt.pkt_sections(data[:-10], 2) == ([[b"cmd"]], False)
        with pytest.raises(ValueError):
            rt.pkt_sections(b"zzzz", 1)
    else:  # an empty options list is a flush alone
        assert rt.pkt_sections(rt.pkt(b"cmd") + b"0000" + b"0000", 2) == ([[b"cmd"], []], True)


def test_host_agrees(scenario):
    """Review 5 H1: the name decides; a port, when given, must be the connection's."""
    names = {"llm.example.org"}
    if scenario == "happy":
        assert rt.host_agrees("LLM.example.org.", names, 443) and rt.host_agrees("llm.example.org:8443", names, 8443)
    elif scenario == "sad":
        assert not rt.host_agrees("other.example.org", names, 443)
        assert not rt.host_agrees("llm.example.org:443", names, 8443)
        assert not rt.host_agrees("llm.example.org.evil.example", names, 443)
    else:  # malformed is never agreement; an address in another spelling is the same address
        assert not rt.host_agrees("llm.example.org:", names, 443)
        assert rt.host_agrees("[0:0::1]:80", {"::1"}, 80)


def test_host_header_problem(scenario):
    """Review 5 H1: a credential goes only with a Host naming its server (domain fronting is refused)."""
    names = {"myendpoint.hf.space"}
    if scenario == "happy":
        for value in ("myendpoint.hf.space", "MyEndpoint.HF.space.", "myendpoint.hf.space:443"):
            assert rt.host_header_problem([("Host", value)], "https", 443, names) is None, value
    elif scenario == "sad":
        assert "differs from myendpoint.hf.space" in rt.host_header_problem(
            [("Host", "attacker.hf.space")], "https", 443, names)
        assert "differs" in rt.host_header_problem([("Host", "myendpoint.hf.space:8443")], "https", 443, names)
        assert "no single Host" in rt.host_header_problem([], "https", 443, names)
    else:  # malformed, and a port other than the default is named in the message
        assert "malformed" in rt.host_header_problem([("Host", "a b")], "https", 443, names)
        assert ":8443" in rt.host_header_problem([("Host", "x")], "https", 8443, names)
        assert rt.host_header_problem([("Host", "myendpoint.hf.space:8443")], "https", 8443, names) is None


def test_verified_authority(scenario):
    """Review 5 M1: a request is shown and logged as where it goes; a different Host is only a claim."""
    record = {"destination": "198.51.100.7:443"}
    if scenario == "happy":
        assert rt.verified_authority("https", "api.github.com", record, "api.github.com") == ("api.github.com", None)
        assert rt.verified_authority("https", "api.github.com", record, None) == ("api.github.com", None)
    elif scenario == "sad":
        assert rt.verified_authority("https", "collector.example", record, "api.github.com") == \
            ("collector.example", "api.github.com")
    else:  # plain http goes by the address; another port is part of the authority; agreeing, the client's form
        assert rt.verified_authority("http", "10.0.0.5:8080", {"destination": "10.0.0.5:8080"}, "intranet:8080") == \
            ("10.0.0.5:8080", "intranet:8080")
        assert rt.verified_authority("http", "[::1]:80", {"destination": "[::1]:80"}, "[::1]") == ("[::1]", None)
        assert rt.verified_authority("https", "a.example", {"destination": "192.0.2.1:8443"}, "A.example:8443") == \
            ("A.example:8443", None)


LLM_ENTRY = {"name": "gpu", "scheme": "https", "host": "llm.example.org", "port": 8443, "addresses": ["192.0.2.9"],
             "prefix": "/ollama", "placeholder": "sandbox-ai-llm-key-placeholder", "key": "sk-live-0123456789",
             "headers": [["CF-Access-Client-Secret", "cf-value"]], "ca": None}


def test_api_key_head(scenario):
    entries = [{"host": "dev.to", "port": 443, "name": "DEVTO", "placeholder": "sandbox-ai-api-key-placeholder-DEVTO",
                "key": "devto-SECRET-1"}]
    head = (b"POST /api/articles HTTP/1.1\r\nHost: dev.to\r\napi-key: sandbox-ai-api-key-placeholder-DEVTO\r\n"
            b"X-Echo: sandbox-ai-api-key-placeholder-DEVTO\r\n\r\n")
    if scenario == "happy":
        changed, added = rt.api_key_head(entries, "https", "DEV.TO", 443, head)
        assert added == ["DEVTO"] and b"api-key: devto-SECRET-1\r\n" in changed
        # Only credential headers: one the server might echo back keeps the placeholder.
        assert b"X-Echo: sandbox-ai-api-key-placeholder-DEVTO" in changed
    elif scenario == "sad":
        for scheme, origin, port in (("http", "dev.to", 443), ("https", "evil.example", 443), ("https", "dev.to", 8443),
                                     ("https", "dev.to.evil.example", 443)):
            assert rt.api_key_head(entries, scheme, origin, port, head) == (head, []), (scheme, origin, port)
        assert rt.api_key_head(entries, "https", "dev.to", 443, head[:-2]) == (head[:-2], [])  # not a whole head
    else:
        assert rt.api_key_head([], "https", "dev.to", 443, head) == (head, [])
        assert rt.api_key_head(None, "https", "dev.to", 443, head) == (head, [])
        plain = b"GET / HTTP/1.1\r\nHost: dev.to\r\n\r\n"
        assert rt.api_key_head(entries, "https", "dev.to", 443, plain) == (plain, [])


def test_basic_with_key(scenario):
    import base64
    entry = {"placeholder": "sandbox-ai-publish-placeholder-pypi", "key": "pypi-REAL-token", "name": "publish:pypi"}
    sent = b" Basic " + base64.b64encode(b"__token__:sandbox-ai-publish-placeholder-pypi")
    if scenario == "happy":
        value, added = rt.basic_with_key(sent, [entry])
        assert added == ["publish:pypi"] and base64.b64decode(value.split()[1]) == b"__token__:pypi-REAL-token"
    elif scenario == "sad":
        for value in (b" Bearer sandbox-ai-publish-placeholder-pypi", b" Basic !!!not-base64!!!",
                      b" Basic " + base64.b64encode(b"__token__:agents-own")):
            assert rt.basic_with_key(value, [entry]) == (value, [])
    else:
        assert rt.basic_with_key(sent, []) == (sent, []) and rt.basic_with_key(b"", [entry]) == (b"", [])


def test_scrubbed_pieces(scenario):
    key = b"sk-SECRET-0123456789"
    if scenario == "happy":
        # Chunked, the key split over two chunks: masked, and the new framing is valid.
        pieces = [(b"6\r\n", b""), (b"ab" + key[:4], b"ab" + key[:4]), (b"\r\n", b""),
                  (b"%x\r\n" % (len(key) - 4 + 1), b""), (key[4:] + b"z", key[4:] + b"z"), (b"\r\n", b""),
                  (b"0\r\n\r\n", b"")]
        out = b"".join(raw for raw, _ in rt.scrubbed_pieces(iter(pieces), rt.Scrubber([key]), True))
        body, rest = b"", out
        while True:
            size, _, rest = rest.partition(b"\r\n")
            if int(size, 16) == 0:
                break
            body, rest = body + rest[:int(size, 16)], rest[int(size, 16) + 2:]
        assert body == b"ab" + b"*" * len(key) + b"z" and key not in out
    elif scenario == "sad":
        # A length-framed body: the same bytes in the same places, only the key's are stars.
        pieces = [(b"x" + key[:7], b"x" + key[:7]), (key[7:] + b"y", key[7:] + b"y")]
        out = b"".join(raw for raw, _ in rt.scrubbed_pieces(iter(pieces), rt.Scrubber([key]), False))
        assert out == b"x" + b"*" * len(key) + b"y"
    else:
        # A chunk size with a leading zero is not the last chunk; nothing to mask leaves the body as it was.
        pieces = [(b"0a\r\n", b""), (b"0123456789", b"0123456789"), (b"\r\n", b""), (b"0\r\n\r\n", b"")]
        out = b"".join(raw for raw, _ in rt.scrubbed_pieces(iter(pieces), rt.Scrubber([key]), True))
        assert out.endswith(b"0\r\n\r\n") and b"0123456789" in out
        assert b"".join(r for r, _ in rt.scrubbed_pieces(iter([]), rt.Scrubber([]), False)) == b""


def test_identity_head(scenario):
    if scenario == "happy":
        head = b"GET / HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip, br\r\n\r\n"
        assert rt.identity_head(head) == b"GET / HTTP/1.1\r\nHost: x\r\nAccept-Encoding: identity\r\n\r\n"
    elif scenario == "sad":
        head = b"GET / HTTP/1.1\r\naccept-encoding: gzip\r\nACCEPT-ENCODING: br\r\n\r\n"
        assert rt.identity_head(head).count(b"ncoding") == 1 and b"gzip" not in rt.identity_head(head)
    else:
        assert rt.identity_head(b"GET / HTTP/1.1\r\n\r\n") == b"GET / HTTP/1.1\r\nAccept-Encoding: identity\r\n\r\n"


def test_llm_head(scenario):
    head = (b"POST /ollama/v1/chat/completions HTTP/1.1\r\nHost: llm.example.org:8443\r\n"
            b"Authorization: Bearer sandbox-ai-llm-key-placeholder\r\nCF-Access-Client-Secret: agent\r\n\r\n")
    if scenario == "happy":
        changed, name = rt.llm_head([LLM_ENTRY], "https", "llm.example.org", 8443, "/ollama/v1/chat/completions", head)
        assert name == "gpu" and b"Authorization: Bearer sk-live-0123456789\r\n" in changed
        assert b"placeholder" not in changed and b"agent" not in changed
        assert changed.endswith(b"CF-Access-Client-Secret: cf-value\r\n\r\n")
    elif scenario == "sad":
        # Another name, plain http, another port, outside the prefix: unchanged.
        for args in (("https", "evil.example", 8443, "/ollama/v1/x"), ("http", "192.0.2.9:8443", 8443, "/ollama/x"),
                     ("https", "llm.example.org", 443, "/ollama/x"), ("https", "llm.example.org", 8443, "/ollamax/v1")):
            assert rt.llm_head([LLM_ENTRY], *args, head) == (head, None)
        assert rt.llm_head(None, "https", "llm.example.org", 8443, "/ollama", head) == (head, None)
    else:
        # No key: the placeholder is dropped, not sent; no credentials at all: the key goes in as a Bearer token.
        keyless = dict(LLM_ENTRY, key=None, headers=[])
        changed, _ = rt.llm_head([keyless], "https", "llm.example.org", 8443, "/ollama", head)
        assert b"Authorization" not in changed and b"CF-Access-Client-Secret: agent" in changed
        bare = b"GET /ollama/v1/models HTTP/1.1\r\nHost: x\r\n\r\n"
        changed, _ = rt.llm_head([dict(LLM_ENTRY, headers=[])], "https", "llm.example.org", 8443, "/ollama/v1/models",
                                 bare)
        assert changed == b"GET /ollama/v1/models HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer sk-live-0123456789\r\n\r\n"


def test_load_llm_module(scenario):
    module = s.load_llm_module()
    if scenario == "happy":
        assert module.parse_choice("gpu:qwen2.5-coder:7b") == ("gpu", "qwen2.5-coder:7b")
    elif scenario == "sad":
        with pytest.raises(ValueError):
            module.parse_choice("../x")
    else:
        assert module.parse_choice("gpu") == ("gpu", None) and module.PLACEHOLDER not in os.environ.values()


def test_is_address_origin(scenario):
    if scenario == "happy":
        assert rt.is_address_origin("192.0.2.7:80") and rt.is_address_origin("[2001:db8::1]:443")
    elif scenario == "sad":
        assert not rt.is_address_origin("api.github.com")
    else:
        assert rt.is_address_origin("192.0.2.7")


def test_normal_path(scenario):
    if scenario == "happy":
        assert rt.normal_path("/repos/a/b/../c?q=1#f") == "/repos/a/c"
    elif scenario == "sad":  # encodings and dot segments resolve as the server would
        assert rt.normal_path("/x/%2e%2e/%2e%2e/../gists") == "/gists"
    else:
        assert rt.normal_path("") == "" and rt.normal_path("relative/path") == "/relative/path"


def test_path_matches(scenario):
    if scenario == "happy":
        assert rt.path_matches("/repos/*/pulls", "/repos/app/pulls") and rt.path_matches("/**", "/a/b/c")
    elif scenario == "sad":
        assert not rt.path_matches("/repos/*", "/repos/a/b") and not rt.path_matches("/a.b", "/axb")
    else:
        assert rt.path_matches("/", "") and rt.path_matches("/*", "/")


def test_within_ceiling(scenario):
    a = s.parse_args(["codex"])
    a.policy = {"allow_within": ["10.0.0.0/8"], "allow_hosts": ["*.corp.example", "pypi.org"]}
    if scenario == "happy":
        assert s.within_ceiling(a, network="10.1.2.3") and s.within_ceiling(a, host="git.corp.example")
    elif scenario == "sad":
        assert not s.within_ceiling(a, network="0.0.0.0/1") and not s.within_ceiling(a, host="pastebin.com")
    else:
        a.policy = {}
        assert s.within_ceiling(a, network="0.0.0.0/0") and s.within_ceiling(a, host="anything.example")


def test_check_ceiling(scenario):
    policy = {"version": 1, "allow_within": ["10.0.0.0/8"], "allow_hosts": ["pypi.org", "files.pythonhosted.org"]}
    if scenario == "happy":  # the provider and listed hosts are fine
        s.check_policy(s.parse_args(["codex", "--allow-pypi", "--allow-host", "pypi.org"]), policy)
    elif scenario == "sad":  # named hosts, presets, DNS, host ports: all widen past the ceiling
        for options in (["--allow-host", "pastebin.com"], ["--allow-github"], ["--dns"], ["--nameserver", "8.8.8.8"],
                        ["--allow-host-port", "11434"]):
            with pytest.raises(ValueError, match="administrator"):
                s.check_policy(s.parse_args(["codex", *options]), policy)
    else:  # a resolver inside the ceiling is fine
        s.check_policy(s.parse_args(["codex", "--nameserver", "10.0.0.53"]), policy)


def test_token_proxy_keeps_tokens_out_of_the_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no git-logins of the machine running the tests
    monkeypatch.setattr(s, "_git_logins_module", None)
    monkeypatch.setenv("GIT_TOKEN_FOR_TEST", "ghp_" + "a" * 36)
    a = s.parse_args(["codex", "--git-token-env", "https://github.com=GIT_TOKEN_FOR_TEST", "--token-proxy"])
    env = s.git_environment(a)
    assert "ghp_" + "a" * 36 not in json.dumps(env) and "GH_TOKEN" not in env
    config = s.proxy_config(a, {}, "l")
    assert config["git_tokens"] == [{"host": "github.com", "port": 443, "user": "x-access-token", "loopback": False,
                                     "token": "ghp_" + "a" * 36}]
    with pytest.raises(ValueError, match="plain http"):
        s.validate(s.parse_args(["codex", "--git-token-env", "http://git.example.org=GIT_TOKEN_FOR_TEST",
                                 "--token-proxy", "--no-config"]))
    with pytest.raises(ValueError, match="needs the connection proxy"):
        s.validate(s.parse_args(["codex", "--git-token-env", "https://github.com=GIT_TOKEN_FOR_TEST",
                                 "--token-proxy", "--no-proxy", "--no-config"]))


def test_group_help(scenario):
    """--help: options by topic, short and long form on one line, the value named once."""
    text = s.build_parser().format_help()
    if scenario == "happy":
        assert text.index("Workspace and files:") < text.index("Network:") < text.index("Git:")
        assert "-w, --workspace DIR[,DIR...]" in text and "-A, --allow-host HOST" in text
    elif scenario == "sad":  # internal options and the agent positional stay hidden, nothing is lost
        assert "--pod-name" not in text and "positional arguments" not in text
        listed = {name for _, names in s.HELP_GROUPS for name in names}
        visible = {a.option_strings[-1] for a in s.build_parser()._actions
                   if a.option_strings and a.help != argparse.SUPPRESS}
        assert visible <= listed, visible - listed  # every option has a topic
    else:  # parsing is unchanged by the grouping
        assert s.parse_args(["codex", "-w", "/tmp/x", "-tp"]).token_proxy is True


def fake_usb(tmp_path, monkeypatch, cls="ff", interface=("ff", "00")):
    """A sysfs tree with one USB device (1-2) behind /dev/bus/usb/001/002 (char 189:1)."""
    sys_root = tmp_path / "sys"
    device = sys_root / "devices" / "pci0000:00" / "usb1" / "1-2"
    (device / "1-2:1.0").mkdir(parents=True)
    for name, value in (("idVendor", "0a12"), ("idProduct", "0001"), ("bDeviceClass", cls)):
        (device / name).write_text(value + "\n")
    (device / "1-2:1.0" / "bInterfaceClass").write_text(interface[0])
    (device / "1-2:1.0" / "bInterfaceProtocol").write_text(interface[1])
    (sys_root / "dev" / "char").mkdir(parents=True)
    (sys_root / "dev" / "char" / "189:1").symlink_to(device)
    node = tmp_path / "002"
    node.write_text("")
    real = os.stat
    monkeypatch.setattr(s.os, "stat", lambda p, *a, **k: os.stat_result((0o20660, 0, 0, 1, 0, 0, 0, 0, 0, 0))
                        if str(p) == str(node) else real(p, *a, **k))
    monkeypatch.setattr(s.os, "major", lambda rdev: 189)
    monkeypatch.setattr(s.os, "minor", lambda rdev: 1)
    return sys_root, node


def test_usb_device_dir(scenario, tmp_path, monkeypatch):
    sys_root, node = fake_usb(tmp_path, monkeypatch)
    if scenario == "happy":
        assert s.usb_device_dir(node, sys_root).name == "1-2"
    elif scenario == "sad":
        assert s.usb_device_dir(tmp_path / "missing", sys_root) is None
    else:
        (sys_root / "dev" / "char" / "189:1").unlink()
        assert s.usb_device_dir(node, sys_root) is None


def test_usb_class_bluetooth(scenario, tmp_path, monkeypatch):
    if scenario == "happy":  # a typical dongle: class on the interface (e0/01)
        sys_root, node = fake_usb(tmp_path, monkeypatch, interface=("e0", "01"))
        assert s.usb_class_bluetooth(s.usb_device_dir(node, sys_root))
    elif scenario == "sad":
        sys_root, node = fake_usb(tmp_path, monkeypatch)
        assert not s.usb_class_bluetooth(s.usb_device_dir(node, sys_root))
    else:  # class on the device
        sys_root, node = fake_usb(tmp_path, monkeypatch, cls="e0")
        assert s.usb_class_bluetooth(s.usb_device_dir(node, sys_root))


def test_udev_hint(scenario, tmp_path, monkeypatch):
    sys_root, node = fake_usb(tmp_path, monkeypatch)
    if scenario == "happy":
        assert s.udev_hint(node, sys_root) == \
            'SUBSYSTEM=="usb", ATTRS{idVendor}=="0a12", ATTRS{idProduct}=="0001", TAG+="uaccess"'
    elif scenario == "sad":
        assert s.udev_hint(tmp_path / "missing", sys_root) is None
    else:  # IDs that are not four hex digits never reach the rule text
        (s.usb_device_dir(node, sys_root) / "idVendor").write_text('0a12", RUN+="x')
        assert s.udev_hint(node, sys_root) is None


def test_blocked_always_is_refused_without_a_question(tmp_path):
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda r: None),
                               blocked={"hosts": ["tracker.example"], "tcp": ["192.0.2.9:5432"]})
    assert broker.ask("dns", {"name": "tracker.example"})["reason"] == "blocked always for this project"
    assert broker.ask("connect", {"destination": "192.0.2.9:5432", "address": "192.0.2.9", "port": 5432})[
        "decision"] == "deny"
    assert list((tmp_path / "a").glob("*.json")) == []  # nobody was asked


def test_dangerously_skip_permissions_answers_claudes_bypass_prompt(tmp_path):
    """-D: Claude's "Bypass Permissions mode" confirmation is answered in the sandbox HOME (it reappeared at every
    start of a throwaway HOME); without -D nothing is set."""
    home = tmp_path / "home"
    s.claude_default_settings(home)
    assert "skipDangerousModePermissionPrompt" not in json.loads((home / ".claude/settings.json").read_text())
    s.claude_default_settings(home, skip_danger_prompt=True)
    assert json.loads((home / ".claude/settings.json").read_text())["skipDangerousModePermissionPrompt"] is True


# --- the sandbox's own network: --subnet, --subnet6, defaults and the policy's subnet_within -------------


def route_hex(address):
    """/proc/net/route's hexadecimal for an IPv4 address (the kernel's byte order)."""
    import ipaddress
    return f"{int.from_bytes(ipaddress.IPv4Address(address).packed, sys.byteorder):08X}"


def route_files(tmp_path, ipv4=(), ipv6=()):
    """Fake /proc/net/route and /proc/net/ipv6_route with (destination, mask) and (hex destination, prefix)."""
    header = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    (tmp_path / "route").write_text(header + "".join(
        f"eth0\t{route_hex(dest)}\t00000000\t0001\t0\t0\t100\t{route_hex(mask)}\t0\t0\t0\n" for dest, mask in ipv4))
    (tmp_path / "ipv6_route").write_text("".join(
        f"{dest} {prefix:02x} 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 "
        "00000001 00000000 00000001 eth0\n" for dest, prefix in ipv6))
    return (tmp_path / "route", tmp_path / "ipv6_route")


@pytest.fixture
def routes(monkeypatch):
    """The host's routes, as host_routes() reports them (the test machine's own stay out)."""
    import ipaddress
    found = [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("fd17:625c:f037:2::/64")]
    monkeypatch.setattr(s, "host_routes", lambda files=None: list(found))
    return found


def test_slirp_command(scenario):
    plain = ["slirp4netns", "--configure", "--disable-host-loopback", "--enable-sandbox", "--enable-seccomp",
             "--enable-ipv6", "--ready-fd", "7", "42", "tap0"]
    if scenario == "happy":
        command = s.slirp_command(s.parse_args(["codex", "--backend", "bwrap", "--subnet", "10.20.0.0/24"]), 42, 7)
        assert command == plain[:6] + ["--cidr=10.20.0.0/24"] + plain[6:]
    elif scenario == "sad":  # nothing chosen: slirp4netns's own 10.0.2.0/24, as before
        assert s.slirp_command(s.parse_args(["codex", "--backend", "bwrap"]), 42, 7) == plain
    else:  # a defaults value applied later counts like the option; IPv6 has no option at all
        a = s.parse_args(["codex", "--backend", "bwrap"])
        a.subnet = "172.20.0.0/16"
        command = s.slirp_command(a, 42, 7)
        assert command.count("--cidr=172.20.0.0/16") == 1 and "fd00" not in " ".join(command)


def test_subnet_hosts(scenario):
    import ipaddress
    if scenario == "happy":
        assert s.subnet_hosts(ipaddress.ip_network("10.20.0.0/24")) == {
            "address": "10.20.0.100", "gateway": "10.20.0.2", "dns": "10.20.0.3"}
    elif scenario == "sad":  # a /26 has no 100th address: parse_subnet refuses it before
        with pytest.raises(IndexError):
            s.subnet_hosts(ipaddress.ip_network("10.20.0.0/26"))
    else:  # IPv6 is written like the others: ::100, not the 100th (::64)
        assert s.subnet_hosts(ipaddress.ip_network("fd20::/64")) == {
            "address": "fd20::100", "gateway": "fd20::2", "dns": "fd20::3"}
        assert s.subnet_hosts(ipaddress.ip_network("192.168.7.128/25"))["address"] == "192.168.7.228"


def test_parse_subnet(scenario):
    if scenario == "happy":
        assert str(s.parse_subnet("10.20.0.0/24", 4)) == "10.20.0.0/24"
        assert str(s.parse_subnet("fd20:1:2:3::/64", 6, "--subnet6")) == "fd20:1:2:3::/64"
        assert str(s.parse_subnet("100.64.8.0/22", 4)) == "100.64.8.0/22"  # shared address space
    elif scenario == "sad":
        for value, message in (("10.20.0.5", "with its prefix"), ("x/24", "with its prefix"),
                               ("127.0.0.0/8", "loopback"), ("169.254.0.0/16", "link-local"),
                               ("224.0.0.0/8", "multicast"), ("8.8.8.0/24", "not a private range"),
                               ("10.0.0.0/26", "/8 to /25"), ("10.0.0.0/7", "/8 to /25"),
                               ("fd20::/64", "an IPv4 network"), (["10.0.0.0/8"], "with its prefix")):
            with pytest.raises(ValueError, match=message):
                s.parse_subnet(value, 4)
        for value, message in (("fd20::/48", "must be /64"), ("2001:db8::/64", "not a private range"),
                               ("fe80::/64", "link-local"), ("fd20::%eth0/64", "with its prefix")):
            with pytest.raises(ValueError, match=message):
                s.parse_subnet(value, 6, "--subnet6")
    else:  # an address in the network names the network to use instead, as slirp4netns would refuse it
        with pytest.raises(ValueError, match=r"--subnet 10.20.0.100/24: give the network address, 10.20.0.0/24"):
            s.parse_subnet("10.20.0.100/24", 4)
        assert str(s.parse_subnet("192.168.7.128/25", 4)) == "192.168.7.128/25"  # the smallest one
        with pytest.raises(ValueError, match="^policy subnet "):
            s.parse_subnet("8.8.8.0/24", 4, "policy subnet")


def test_host_routes(scenario, tmp_path):
    if scenario == "happy":
        files = route_files(tmp_path, [("0.0.0.0", "0.0.0.0"), ("192.168.1.0", "255.255.255.0"),
                                       ("10.8.0.0", "255.255.0.0")],
                            [("00000000000000000000000000000000", 0), ("fd17625cf0370002" + "0" * 16, 64),
                             ("fe80" + "0" * 28, 64), ("ff00" + "0" * 28, 8), ("0" * 31 + "1", 128)])
        assert [str(n) for n in s.host_routes(files)] == ["192.168.1.0/24", "10.8.0.0/16", "fd17:625c:f037:2::/64"]
    elif scenario == "sad":  # unreadable: no routes (the check spares clashes; it is no boundary)
        assert s.host_routes((tmp_path / "missing", tmp_path / "missing6")) == []
    else:  # malformed lines are skipped, the rest is kept
        files = route_files(tmp_path, [("172.17.0.0", "255.255.0.0")])
        with files[0].open("a") as handle:
            handle.write("eth0\tZZZZ\n\n")
        files[1].write_text("garbage\nfd00000000000000000000000000000 40\n")
        assert [str(n) for n in s.host_routes(files)] == ["172.17.0.0/16"]


def test_defaults_file(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert s.defaults_file() == tmp_path / "cfg/sandbox-ai/defaults.json"
    else:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / ("home" if scenario == "sad" else "other")))
        assert s.defaults_file() == tmp_path / ("home" if scenario == "sad" else "other") / ".config/sandbox-ai/defaults.json"


def test_load_user_defaults(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg/sandbox-ai/defaults.json"
    path.parent.mkdir(parents=True)
    a = s.parse_args(["codex", "--workspace", str(tmp_path / "work")])
    if scenario == "happy":
        assert s.load_user_defaults(a) == {}  # no file: nothing changes
        path.write_text(json.dumps({"version": 1, "subnet": "10.20.0.0/24", "subnet6": "fd20::/64"}))
        assert s.load_user_defaults(a)["subnet"] == "10.20.0.0/24"
    elif scenario == "sad":
        for document, message in (({"version": 2}, "expected version 1"), ({"version": 1, "allow": ["x"]}, "keys"),
                                  ({"version": 1, "subnet": "8.8.8.0/24"}, "subnet 8.8.8.0/24 is not a private"),
                                  ({"version": 1, "subnet6": "10.0.0.0/8"}, "IPv6 network")):
            path.write_text(json.dumps(document))
            with pytest.raises(ValueError, match=message):
                s.load_user_defaults(a)
        path.write_text("{")
        with pytest.raises(ValueError, match="invalid JSON"):
            s.load_user_defaults(a)
    else:  # a link is not followed, and a file the agent could write is not read at all
        (tmp_path / "real.json").write_text(json.dumps({"version": 1, "subnet": "10.20.0.0/24"}))
        path.symlink_to(tmp_path / "real.json")
        with pytest.raises(ValueError, match="symlink"):
            s.load_user_defaults(a)
        exposed = s.parse_args(["codex", "--workspace", str(tmp_path / "cfg")])
        with pytest.raises(ValueError, match="lies inside"):
            s.load_user_defaults(exposed)


def test_apply_subnet_defaults(scenario):
    values = {"version": 1, "subnet": "10.20.0.0/24", "subnet6": "fd20::/64"}
    if scenario == "happy":
        a = s.parse_args(["codex"])
        s.apply_subnet_defaults(a, values, "defaults.json")
        assert (a.subnet, a.subnet6) == ("10.20.0.0/24", "fd20::/64")
        assert a.subnet_origin == {"subnet": "defaults.json", "subnet6": "defaults.json"}
    elif scenario == "sad":  # no default network to give them to: offline, or a Podman network decides
        for options in (["--offline"], ["--network", "lab"]):
            a = s.parse_args(["codex", *options])
            s.apply_subnet_defaults(a, values, "defaults.json")
            assert (a.subnet, a.subnet6, a.subnet_origin) == (None, None, {})
    else:  # an option wins; bwrap takes no IPv6 default (slirp4netns cannot), which is no error
        a = s.parse_args(["codex", "--backend", "bwrap", "--subnet", "172.20.0.0/16"])
        s.apply_subnet_defaults(a, values, "defaults.json")
        assert (a.subnet, a.subnet6, a.subnet_origin) == ("172.20.0.0/16", None, {})


def test_subnet_label(scenario):
    a = s.parse_args(["codex", "--subnet", "10.20.0.0/24"])
    if scenario == "happy":
        assert s.subnet_label(a, "subnet") == "--subnet 10.20.0.0/24"
    elif scenario == "sad":
        assert s.subnet_label(a, "subnet6") == "--subnet6 None"
    else:
        a.subnet_origin["subnet"] = "/home/u/.config/sandbox-ai/defaults.json"
        assert s.subnet_label(a, "subnet") == "--subnet 10.20.0.0/24 (from /home/u/.config/sandbox-ai/defaults.json)"


def test_subnet_destinations(scenario):
    if scenario == "happy":
        a = s.parse_args(["claude", "--allow", "10.1.0.0/16", "--allow-tcp", "[fd30::7]:22", "--nameserver", "9.9.9.9"])
        labels = [label for label, _ in s.subnet_destinations(a, ["203.0.113.5"])]
        assert labels[:3] == ["--allow 10.1.0.0/16", "--allow-tcp fd30::7:22", "the nameserver 9.9.9.9"]
        assert "160.79.104.0/23 (an allowed host's network)" in labels  # api.anthropic.com's ranges
        assert labels[-1] == "203.0.113.5 (an allowed host's address)"
    elif scenario == "sad":
        assert s.subnet_destinations(s.parse_args(["codex", "--offline"])) == []
    else:  # --dns brings the default resolvers in
        a = s.parse_args(["codex", "--dns", "--no-provider-defaults"])
        assert [str(net) for _, net in s.subnet_destinations(a)] == ["1.1.1.1/32", "8.8.8.8/32"]


def test_check_subnets(scenario, routes):
    if scenario == "happy":
        s.check_subnets(s.parse_args(["claude", "--subnet", "10.20.0.0/24", "--subnet6", "fd20::/64",
                                      "--allow", "10.21.0.0/16", "--allow-tcp", "192.168.2.5:22"]))
        s.check_subnets(s.parse_args(["claude"]))  # nothing chosen: nothing to check
    elif scenario == "sad":
        for options, message in (
                (["--subnet", "192.168.0.0/16"], "overlaps 192.168.1.0/24, a network this host routes to"),
                (["--subnet6", "fd17:625c:f037:2::/64"], "overlaps fd17:625c:f037:2::/64"),
                (["--subnet", "10.20.0.0/24", "--allow", "10.0.0.0/8"], "--allow 10.0.0.0/8 lies inside the sandbox"),
                (["--subnet", "10.20.0.0/24", "--allow-tcp", "10.20.0.2:22"], "--allow-tcp 10.20.0.2:22 lies inside"),
                (["--subnet", "10.20.0.0/24", "--nameserver", "10.20.0.2"], "the nameserver 10.20.0.2 lies inside"),
                (["--subnet", "10.20.0.0/24", "--network", "lab"], "with --network the Podman network decides"),
                (["--subnet6", "fd20::/64", "--backend", "bwrap"], "slirp4netns .*fd00::/64"),
                (["--subnet", "nope"], "expects a network")):
            with pytest.raises(ValueError, match=message):
                s.check_subnets(s.parse_args(["claude", *options]))
        # Resolved hosts: the address an allowed name had at the start.
        with pytest.raises(ValueError, match=r"10.20.0.3 \(an allowed host's address\) lies inside"):
            s.check_subnets(s.parse_args(["claude", "--subnet", "10.20.0.0/24"]), ["10.20.0.3"])
    else:
        # .3 is the backend's DNS forwarder to the host's resolvers: the one address inside that may be used.
        s.check_subnets(s.parse_args(["claude", "--subnet", "10.20.0.0/24", "--nameserver", "10.20.0.3"]))
        # A remote Podman's routes are not this host's.
        s.check_subnets(s.parse_args(["claude", "--subnet", "192.168.1.0/24", "--podman-url", "unix:///x.sock"]))
        a = s.parse_args(["claude", "--subnet", "10.20.0.0/24", "--allow-tcp", "10.20.0.9:22"])
        a.subnet_origin["subnet"] = "defaults.json"
        with pytest.raises(ValueError, match=r"inside the sandbox's own network, --subnet 10.20.0.0/24 \(from defaults.json\)"):
            s.check_subnets(a)


def test_settle_network(scenario, routes, capsys):
    """After the hosts are resolved (by the host, the Podman machine or through the VPN): --llm's name pinned
    with RESOLVE_NAMES, every address checked against the sandbox's own network."""
    asked = []

    def resolve(names):
        asked.append(list(names))
        return {name: ["10.20.0.7"] for name in names}
    if scenario == "happy":
        a = s.parse_args(["claude", "--subnet", "10.20.0.0/24"])
        s.settle_network(a, {"api.anthropic.com": ["203.0.113.5"]}, resolve)
        assert a.llm_pins == {} and asked == []
        assert "sandbox network --subnet 10.20.0.0/24: the sandbox 10.20.0.100, gateway 10.20.0.2" in \
            capsys.readouterr().err
    elif scenario == "sad":
        with pytest.raises(ValueError, match=r"10.20.0.3 \(an allowed host's address\) lies inside"):
            s.settle_network(s.parse_args(["claude", "--subnet", "10.20.0.0/24"]), {"x.example": ["10.20.0.3"]},
                             resolve)
    else:  # an --llm name on its own port: resolved the caller's way, pinned, opened on that port only
        llm = s.load_llm_module()
        store = llm.load_store()
        store["endpoints"]["gpu"] = {"kind": "vllm", "url": "https://llm.example:8443", "model": "m"}
        llm.save_store(store)
        a = s.parse_args(["codex", "--llm", "gpu"])
        s.settle_network(a, {}, resolve)
        assert asked == [["llm.example"]] and a.llm_pins == {"llm.example": ["10.20.0.7"]}
        assert "10.20.0.7:8443" in a.allow_tcp
        with pytest.raises(ValueError, match="--allow-tcp 10.20.0.7:8443 lies inside"):
            s.settle_network(s.parse_args(["codex", "--llm", "gpu", "--subnet", "10.20.0.0/24"]), {}, resolve)


def test_in_subnet(scenario):
    a = s.parse_args(["claude", "--subnet", "10.20.0.0/24", "--subnet6", "fd20::/64"])
    if scenario == "happy":
        assert s.in_subnet(a, "10.20.0.9") and s.in_subnet(a, "fd20::9")
    elif scenario == "sad":
        assert not s.in_subnet(a, "10.21.0.9") and not s.in_subnet(s.parse_args(["claude"]), "10.0.2.3")
    else:  # an invalid value is check_subnets' to report
        a.subnet = "nope"
        assert not s.in_subnet(a, "10.20.0.9") and not s.in_subnet(a, "not an address")


def test_sandbox_network(scenario):
    if scenario == "happy":
        a = s.parse_args(["claude", "--subnet", "10.20.0.0/24", "--subnet6", "fd20::/64"])
        assert s.sandbox_network(a) == {
            "ipv4": {"subnet": "10.20.0.0/24", "address": "10.20.0.100", "gateway": "10.20.0.2", "dns": "10.20.0.3",
                     "from": "--subnet"},
            "ipv6": {"subnet": "fd20::/64", "address": "fd20::100", "gateway": "fd20::2", "dns": None,
                     "from": "--subnet6"}}
    elif scenario == "sad":
        assert s.sandbox_network(s.parse_args(["claude", "--offline"])) is None
        assert s.sandbox_network(s.parse_args(["claude", "--network", "lab"])) is None
    else:  # the backends' own: slirp4netns's networks, pasta the host's addresses
        bwrap = s.sandbox_network(s.parse_args(["claude", "--backend", "bwrap"]))
        assert bwrap["ipv4"] == {"subnet": "10.0.2.0/24", "address": "10.0.2.100", "gateway": "10.0.2.2",
                                 "dns": "10.0.2.3", "from": "slirp4netns"}
        assert bwrap["ipv6"]["subnet"] == "fd00::/64" and bwrap["ipv6"]["address"] is None
        assert s.sandbox_network(s.parse_args(["claude"]))["ipv4"] == {"subnet": None,
                                                                         "from": "the host's addresses (pasta)"}


def test_check_subnet_ceiling(scenario):
    policy = {"version": 1, "subnet": "10.200.0.0/24", "subnet_within": ["10.200.0.0/16"]}
    if scenario == "happy":
        s.check_subnet_ceiling(s.parse_args(["claude", "--subnet", "10.200.5.0/24"]), policy)
        s.check_subnet_ceiling(s.parse_args(["claude", "--subnet", "10.0.0.0/8"]), {"version": 1})  # no ceiling
        s.check_subnet_ceiling(s.parse_args(["claude", "--offline"]), policy)
    elif scenario == "sad":
        with pytest.raises(ValueError, match=r"--subnet 10.20.0.0/24 lies outside .*subnet_within: 10.200.0.0/16\) x"):
            s.check_subnet_ceiling(s.parse_args(["claude", "--subnet", "10.20.0.0/24"]), policy, " x")
        # Podman without a subnet would take the host's address.
        with pytest.raises(ValueError, match="choose one with --subnet"):
            s.check_subnet_ceiling(s.parse_args(["claude"]), policy)
    else:  # bwrap: slirp4netns's own networks are what is checked; its IPv6 network cannot be moved
        s.check_subnet_ceiling(s.parse_args(["claude", "--backend", "bwrap"]), {"subnet_within": ["10.0.0.0/8"]})
        with pytest.raises(ValueError, match="slirp4netns's 10.0.2.0/24 lies outside"):
            s.check_subnet_ceiling(s.parse_args(["claude", "--backend", "bwrap"]), policy)
        with pytest.raises(ValueError, match="slirp4netns's fd00::/64 lies outside"):
            s.check_subnet_ceiling(s.parse_args(["claude", "--backend", "bwrap", "--subnet6", "fd20::/64"]),
                                   {"subnet_within": ["fd20::/48"]})


def test_policy_subnet_keys(tmp_path, monkeypatch):
    """The policy's subnet, subnet6 and subnet_within are checked when it is read: a broken one stops every start."""
    good = {"version": 1, "subnet": "10.200.0.0/24", "subnet6": "fd20::/64", "subnet_within": ["10.200.0.0/16",
                                                                                              "fd20::/48"]}
    assert s.load_policy(policy_file(tmp_path, good, monkeypatch))["subnet"] == "10.200.0.0/24"
    for document, message in (({"subnet": "8.8.8.0/24"}, "not a private range"),
                              ({"subnet_within": "10.0.0.0/8", "subnet": "10.1.0.0/24"}, "subnet_within is a list"),
                              ({"subnet_within": ["10.0.0.0/8"]}, "needs subnet, the default inside them"),
                              ({"subnet_within": ["fd00::/8"], "subnet": "10.1.0.0/24"}, "needs subnet6"),
                              ({"subnet_within": ["10.0.0.0/16"], "subnet": "10.1.0.0/24"}, "lies outside subnet_within")):
        path = policy_file(tmp_path, {"version": 1, **document}, monkeypatch)
        with pytest.raises(ValueError, match=message):
            s.load_policy(path)


def test_policy_subnet_default_and_forbid(routes):
    """The policy's default applies only where nothing chose one, after the forbid check (it is no choice of
    yours); a defaults-file value counts as yours."""
    policy = {"version": 1, "subnet": "10.200.0.0/24", "subnet_within": ["10.200.0.0/16"], "forbid": ["--subnet"]}
    a = s.parse_args(["claude"])
    s.check_policy(a, policy)
    assert a.subnet == "10.200.0.0/24" and a.subnet_origin["subnet"].startswith("the administrator's policy")
    with pytest.raises(ValueError, match="--subnet is not allowed"):
        s.check_policy(s.parse_args(["claude", "--subnet", "10.200.1.0/24"]), policy)
    mine = s.parse_args(["claude"])
    s.apply_subnet_defaults(mine, {"subnet": "10.200.1.0/24"}, "defaults.json")
    with pytest.raises(ValueError, match="--subnet is not allowed"):
        s.check_policy(mine, policy)


def test_load_modes(scenario):
    modes = s.load_modes()
    if scenario == "happy":
        assert modes.NAMES == s.MODES.NAMES and modes.DEFAULT == "medium"
    elif scenario == "sad":
        with pytest.raises(ValueError, match="security mode is one of"):
            modes.check_name("none", "x")
    else:  # loaded afresh: a module of its own, not the launcher's
        assert modes is not s.MODES


def test_workspace_mode(scenario):
    a = s.parse_args(["codex", "--workspace", "/tmp/a,/tmp/b", "--no-config"])
    if scenario == "happy":
        assert [s.workspace_mode(a, i) for i in (0, 1)] == ["rw", "rw"]
    elif scenario == "sad":
        a.workspace_ro = True
        assert [s.workspace_mode(a, i) for i in (0, 1)] == ["ro", "ro"]
    else:  # the security mode high: every workspace but the first
        a.mode_other_workspaces_ro = True
        assert [s.workspace_mode(a, i) for i in (0, 1)] == ["rw", "ro"]


def test_launcher_namespace(scenario, monkeypatch):
    if scenario == "happy":
        assert s.launcher_namespace().PROVIDERS is s.PROVIDERS
    elif scenario == "sad":  # what a caller replaced is what other modules see
        monkeypatch.setattr(s, "load_policy", lambda path=None: {"version": 1, "min_mode": "high"})
        assert s.launcher_namespace().load_policy()["min_mode"] == "high"
    else:
        assert s.launcher_namespace() is not s.launcher_namespace()


# --- canary files (canary.py; the supervisor's and the proxy's side in runtime.py) -------------------

CANARY_TOKEN = "AKIA" + "ABCDEFGHJ" + "EXAMPLE"


def canary_server(block=True, models=(), who=True):
    """A stand-in for ProxyServer with the canary entry of proxy.json; WHO records what it reports."""
    reported = []
    forms = [[CANARY_TOKEN, "aws", "plain"], [base64.b64encode(CANARY_TOKEN.encode()).decode(), "aws", "base64"]]
    server = SimpleNamespace(config={"canary": {"forms": forms, "names": [[CANARY_TOKEN.lower(), "aws", "plain"]],
                                                "block": block, "model_hosts": list(models)}},
                             who=SimpleNamespace(request=lambda kind, details: reported.append((kind, details)))
                             if who else None, asked_by=lambda record: {"process": {"comm": "curl"}}, logged=[])
    server.log = server.logged.append
    server.refuse = lambda client, entry, reset=False: server.logged.append(entry)
    return server, reported


def test_canary_event(scenario, tmp_path):
    path = tmp_path / ("missing/events.jsonl" if scenario == "sad" else "events.jsonl")
    for _ in range(2 if scenario == "edge" else 1):
        written = s.RUNTIME.canary_event({"event": "read", "canary": "aws"}, path)
    if scenario == "sad":
        assert written is False and not path.exists()
        return
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == (2 if scenario == "edge" else 1) and lines[0]["event"] == "read" and lines[0]["time"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_canary_matchers(scenario):
    server, _ = canary_server(block=scenario != "edge")
    if scenario == "sad":
        server.config = {}
    forms, names, block, models = s.RUNTIME.canary_matchers(server)
    assert s.RUNTIME.canary_matchers(server)[0] is forms  # built once
    if scenario == "sad":
        assert not forms and not names and block is True and models == set()
    else:
        assert forms.find(b"x" + CANARY_TOKEN.encode()) == {"canary": "aws", "encoding": "plain"}
        assert block is (scenario == "happy")


def test_canary_found(scenario):
    server, reported = canary_server(block=scenario != "edge", models=["api.openai.com"])
    entry = {"url": "https://evil.example/x"}
    hit = {"canary": "aws", "encoding": "plain"}
    if scenario == "happy":  # blocked
        with pytest.raises(s.RUNTIME.CanaryRefused):
            s.RUNTIME.canary_found(server, hit, "body", entry, {}, "evil.example")
        assert reported[0][1]["event"] == "send" and reported[0][1]["process"] == {"comm": "curl"}
    elif scenario == "sad":  # to the model provider: the conversation, not a leak; reported once
        for _ in range(2):
            s.RUNTIME.canary_found(server, hit, "body", entry, {}, "api.openai.com")
        assert [d["event"] for _, d in reported] == ["context"] and entry["canary"]["context"] is True
    else:  # report only
        s.RUNTIME.canary_found(server, hit, "url", entry, {}, "evil.example")
        assert entry["canary"] == {**hit, "where": "url"} and reported[0][1]["where"] == "url"


def test_canary_request(scenario):
    server, reported = canary_server()
    head = b"GET /?k=" + (CANARY_TOKEN.encode() if scenario == "sad" else b"x") + b" HTTP/1.1\r\nHost: h\r\n\r\n"
    body = [(b"raw", b"aaa" + CANARY_TOKEN.encode()[:5]), (b"raw2", CANARY_TOKEN.encode()[5:] + b"zz")] \
        if scenario == "edge" else [(b"raw", b"clean")]
    if scenario == "sad":
        with pytest.raises(s.RUNTIME.CanaryRefused) as refused:
            s.RUNTIME.canary_request(server, head, iter(body), [], {}, {}, "h")
        assert refused.value.hit["where"] == "url"
        return
    parts = s.RUNTIME.canary_request(server, head, iter(body), [], {}, {}, "h")
    if scenario == "happy":
        assert list(parts) == body and reported == []
    else:  # a token across two pieces of a body: the second never passes
        passed = []
        with pytest.raises(s.RUNTIME.CanaryRefused):
            for part in parts:
                passed.append(part)
        assert passed == body[:1]


def test_canary_refusal(scenario):
    server, _ = canary_server()
    client, agent = socket.socketpair()
    if scenario == "sad":
        client.close()  # the agent went away: the refusal is still logged
    refused = s.RUNTIME.CanaryRefused({"canary": "aws", "encoding": "base64", "where": "body"})
    s.RUNTIME.canary_refusal(server, client, refused, {"url": "https://x/" if scenario != "edge" else None})
    record = server.logged[-1]
    assert record["event"] == "denied" and "canary token (aws, base64) in the request's body" in record["reason"]
    if scenario != "sad":
        assert agent.recv(4096).startswith(b"HTTP/1.1 403 Forbidden")
    client.close()
    agent.close()


def test_canary_sni(scenario):
    server, reported = canary_server(block=scenario != "edge")
    name = {"happy": f"{CANARY_TOKEN.lower()}.evil.example", "sad": "api.github.com",
            "edge": f"{CANARY_TOKEN}.evil.example"}[scenario]
    refused = s.RUNTIME.canary_sni(server, Mock(), name, {"connection": 1})
    assert refused is (scenario == "happy")
    assert len(reported) == (0 if scenario == "sad" else 1)
    if scenario == "happy":
        assert server.logged[-1]["canary"]["where"] == "sni"


def test_canary_push(scenario):
    server, reported = canary_server(who=scenario != "edge")
    entry = {"url": "https://git.example/x"}
    s.RUNTIME.canary_push(server, {"ok": False, "canary": [] if scenario == "sad" else ["aws"]}, entry, {})
    if scenario == "sad":
        assert reported == [] and "canary" not in entry
    else:
        assert entry["canary"] == {"canary": "aws", "where": "push"}
        assert [d["source"] for _, d in reported] == ([] if scenario == "edge" else ["push"])


def test_load_canary_module(scenario):
    module = s.load_canary_module()
    if scenario == "happy":
        assert module.CATEGORIES == ("credentials", "home", "workspace")
    elif scenario == "sad":
        with pytest.raises(ValueError):
            module.parse_categories("nope")
    else:
        assert module is not s.load_canary_module()  # loaded fresh each time, like the other modules


def test_plant_canaries(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    a = s.parse_args(["codex", "--no-config", "-w", str(tmp_path / "w")] +
                     {"happy": [], "sad": ["--no-canaries"], "edge": ["--canaries", "home"]}[scenario])
    a.canary_categories, a.run_id, a.kit_mounts = s.load_canary_module().categories(a), "codex-contract", []
    with contextlib.ExitStack() as cleanup:
        plan = s.plant_canaries(a, home, cleanup)
        if scenario == "sad":
            assert plan is None and not any(home.iterdir())
            return
        assert (home / (".aws/credentials" if scenario == "happy" else ".kube/config")).exists()
    assert not any(home.iterdir())  # removed with the cleanup (a throwaway HOME)


def test_mode_refusal(scenario):
    high = {"name": "high", "refuses": ["--allow", "--allow-host", "--allow-tcp"], "approval_scope": "run"}
    if scenario == "happy":
        assert s.RUNTIME.mode_refusal(high, {"op": "revoke", "https": ["192.0.2.1/32"]}) is None
        assert s.RUNTIME.mode_refusal(None, {"op": "allow", "https": ["192.0.2.1/32"]}) is None
    elif scenario == "sad":
        assert "--allow-tcp widens the security mode high" in s.RUNTIME.mode_refusal(
            high, {"op": "allow", "tcp": [["192.0.2.1", 22]]})
    else:  # empty fields open nothing
        assert s.RUNTIME.mode_refusal(high, {"op": "allow", "nets": [], "https": [], "tcp": []}) is None


def test_narrowest_scope(scenario):
    if scenario == "happy":
        assert s.RUNTIME.narrowest_scope("always", "run") == "run"
    elif scenario == "sad":
        assert s.RUNTIME.narrowest_scope("always", None) == "once"
    else:
        assert s.RUNTIME.narrowest_scope("always") == "always"
TAINT_RULES = {"version": 1, "untrusted": [{"id": "web", "server": "web", "tool": "fetch"},
                                           {"id": "news", "host": "news.example"}],
               "mcp": [{"id": "write-tainted", "server": "files", "tool": "write_file", "decision": "ask",
                        "when": {"tainted": True}},
                       {"id": "fs-write", "server": "files", "tool": "write_file", "decision": "ask"}]}


def test_provider_hosts(scenario):
    if scenario == "happy":
        assert s.provider_hosts(s.parse_args(["codex"])) == ["api.openai.com", "chatgpt.com", "auth.openai.com"]
    elif scenario == "sad":  # another agent's provider is no provider of this run
        assert "api.openai.com" not in s.provider_hosts(s.parse_args(["opencode"]))
    else:  # --frontier adds both, once each
        hosts = s.provider_hosts(s.parse_args(["claude", "--frontier"]))
        assert "api.openai.com" in hosts and len(hosts) == len(set(hosts))


def test_mcp_relay_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(TAINT_RULES))
    a = s.parse_args(["codex", "--rules", str(mine)])
    if scenario == "happy":  # only the rules for tainted runs: the server's own gateway has the others
        config = s.mcp_relay_config(a, "files")
        assert config["server"] == "files" and [r["id"] for r in config["rules"][0]["mcp"]] == ["write-tainted"]
        assert config["rules"][0]["untrusted"] == []
    elif scenario == "sad":  # nothing for a server no rule names
        assert s.mcp_relay_config(a, "other") == {"server": "other", "rules": []}
    else:  # a tool source of the server travels, a host source does not
        assert [e["id"] for e in s.mcp_relay_config(a, "web")["rules"][0]["untrusted"]] == ["web"]


def test_start_mcp_relays(scenario, tmp_path, capsys):
    directory = Path(tempfile.mkdtemp(prefix="relays-", dir="/tmp"))
    try:
        if scenario == "sad":  # no server: no relay, no directory
            assert rt.start_mcp_relays({}, run_dir=directory / "run", upstream_dir=directory / "up") == []
            assert not (directory / "run").exists()
            return
        relays = {"web": {"server": "web", "rules": []}}
        threads = rt.start_mcp_relays(relays, run_dir=directory / "run", upstream_dir=directory / "up",
                                      log=directory / "calls.jsonl")
        assert len(threads) == 1 and (directory / "run/web/server.sock").exists()
        assert stat.S_IMODE((directory / "run/web").stat().st_mode) == 0o755
        if scenario == "edge":  # the server's own socket is gone: the agent's connection ends, the relay stays
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(10)
                client.connect(str(directory / "run/web/server.sock"))
                # The relay may close the connection before all of it is sent: that is what this case expects.
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n')
                    client.shutdown(socket.SHUT_WR)
                    while client.recv(65536):
                        pass
            assert threads[0].is_alive() and "MCP server failed" in capsys.readouterr().err
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_connect_socket_file(scenario, tmp_path):
    """The relay reaches a server's socket without following a link the server put in its directory."""
    directory = Path(tempfile.mkdtemp(prefix="csf-", dir="/tmp"))
    try:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(directory / "server.sock"))
        listener.listen(1)
        with listener, socket.socket(socket.AF_UNIX) as client:
            if scenario == "happy":
                rt.connect_socket_file(client, str(directory / "server.sock"))
                listener.accept()[0].close()
            elif scenario == "sad":  # a link to the socket (or to the supervisor's control socket): refused
                (directory / "link.sock").symlink_to(directory / "server.sock")
                with pytest.raises(OSError, match="no socket"):
                    rt.connect_socket_file(client, str(directory / "link.sock"))
                (directory / "file").write_text("")
                with pytest.raises(OSError, match="no socket"):
                    rt.connect_socket_file(client, str(directory / "file"))
            else:  # a name in a directory given as a descriptor (a server attached during the run)
                fd = os.open(directory, os.O_PATH | os.O_DIRECTORY)
                try:
                    rt.connect_socket_file(client, "server.sock", fd)
                finally:
                    os.close(fd)
                listener.accept()[0].close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def fake_mcp_upstream(directory):
    """An MCP server's socket in DIRECTORY (as the server's sandbox listens): answers each request line with a result
    that names the method; (thread, stop)."""
    listener = rt.listen_unix(str(directory / "server.sock"))
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            def answer(connection=connection):
                with connection:
                    for line in connection.makefile("rb"):
                        request = json.loads(line)
                        if "id" in request:
                            reply = {"jsonrpc": "2.0", "id": request["id"],
                                     "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
                                     if request["method"] == "tools/list" else {"method": request["method"]}}
                            connection.sendall((json.dumps(reply) + "\n").encode())
            threading.Thread(target=answer, daemon=True).start()
        listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, stop


def mcp_request(path, message, timeout=10):
    """One request to the relay at PATH as the agent's bridge sends it; the answer (dict), or None if cut."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((json.dumps(message) + "\n").encode())
        line = client.makefile("rb").readline()
        return json.loads(line) if line else None


def test_mcp_control(scenario, tmp_path, monkeypatch):
    """mcp-open hands the relay a descriptor of the server's socket directory: the agent reaches the server through
    the run's relay at once (and mcp-calls.jsonl says so); mcp-close cuts it; policy, mode and odd requests refuse.
    The descriptor must be a detached clone (detached_root): a host directory's own is refused (finding M4)."""
    if scenario == "sad":
        with pytest.raises(ValueError, match="no detached copy"):
            plain = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
            try:
                rt.mcp_control({"op": "mcp-open", "name": "web", "relay": {"server": "web"}},
                               rt.McpRelays(run_dir=tmp_path / "r", live=True), [plain])
            finally:
                os.close(plain)
    # Here the test's own folders stand in for the clone (open_tree needs the sandbox's namespaces).
    monkeypatch.setattr(rt, "detached_root", lambda fd: True)
    base = Path(tempfile.mkdtemp(prefix="mcpc-", dir="/tmp"))
    upstream = base / "up"
    upstream.mkdir()
    thread, stop = fake_mcp_upstream(upstream)
    relays = rt.McpRelays(run_dir=base / "run", log=base / "calls.jsonl", live=True)
    fd = os.open(upstream, os.O_PATH | os.O_DIRECTORY)
    try:
        if scenario == "happy":
            fds = [fd]
            answer = rt.mcp_control({"op": "mcp-open", "name": "web", "relay": {"server": "web", "rules": []}},
                                    relays, fds)
            fd = None  # taken over (closed with the relay)
            assert answer["opened"] and fds == [] and answer["servers"] == ["web"]
            reply = mcp_request(base / "run/web/server.sock", {"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
            assert reply["id"] == 7 and reply["result"]["tools"][0]["name"] == "echo"
            assert rt.mcp_control({"op": "mcp-list"}, relays, [])["servers"] == ["web"]
            again = os.open(upstream, os.O_PATH | os.O_DIRECTORY)
            assert rt.mcp_control({"op": "mcp-open", "name": "web", "relay": {"server": "web"}}, relays,
                                  [again])["already"]  # closed by the relays: not leaked
            with pytest.raises(OSError):
                os.fstat(again)
            events = [json.loads(line).get("event") for line in (base / "calls.jsonl").read_text().splitlines()]
            assert "attached" in events
        elif scenario == "sad":
            for request, fds, why, kwargs in (
                    ({"op": "mcp-open", "name": "web", "relay": {"server": "web"}}, [fd], "policy",
                     {"forbid": ["--mcp"]}),
                    ({"op": "mcp-open", "name": "web", "relay": {"server": "web"}}, [fd], "security mode high",
                     {"mode": {"name": "high", "refuses": ["--mcp"]}}),
                    ({"op": "mcp-open", "name": "web", "relay": {"server": "web"}}, [], "one descriptor", {}),
                    ({"op": "mcp-open", "name": "../x", "relay": {}}, [fd], "1-32 letters", {}),
                    ({"op": "mcp-open", "name": "web", "relay": {"server": "other"}}, [fd], "configuration", {})):
                with pytest.raises(ValueError, match=why):
                    rt.mcp_control(request, relays, list(fds), **kwargs)
            assert relays.names() == [] and not (base / "run/web").exists()
            with pytest.raises(ValueError, match="relays no MCP servers"):
                rt.mcp_control({"op": "mcp-list"}, None, [])
            old = rt.McpRelays(run_dir=base / "old", live=False)  # an older start: no read-only place for the agent
            with pytest.raises(ValueError, match="older version"):
                rt.mcp_control({"op": "mcp-open", "name": "web", "relay": {"server": "web"}}, old,
                               [os.open(upstream, os.O_PATH | os.O_DIRECTORY)])
        else:  # closed: the agent's open connection is cut, the socket goes, a new connection is refused
            rt.mcp_control({"op": "mcp-open", "name": "web", "relay": {"server": "web", "rules": []}}, relays, [fd])
            fd = None
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(10)
            client.connect(str(base / "run/web/server.sock"))
            client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
            stream = client.makefile("rb")
            assert json.loads(stream.readline())["result"] == {"method": "ping"}
            assert rt.mcp_control({"op": "mcp-close", "name": "web"}, relays, []) == {"ok": True, "closed": True,
                                                                                       "servers": []}
            assert stream.readline() == b""  # cut
            client.close()
            assert not (base / "run/web").exists()
            assert rt.mcp_control({"op": "mcp-close", "name": "web"}, relays, [])["closed"] is False
            events = [json.loads(line).get("event") for line in (base / "calls.jsonl").read_text().splitlines()]
            assert events[-1] == "detached"
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        for name in relays.names():
            relays.detach(name)
        stop.set()
        thread.join(5)
        shutil.rmtree(base, ignore_errors=True)


HOOK_RULES = {"version": 1, "untrusted": [{"id": "search", "agent": "claude", "tool": "WebSearch"}],
              "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": "(?s).*curl.*"}},
                        "decision": "deny"}]}


def test_hook_value(scenario):
    if scenario == "happy":
        assert rt.hook_value({"command": "ls", "n": [1, None]}) == {"command": "ls", "n": [1, None]}
    elif scenario == "sad":  # too large to check: unknown to the rules, never cut short
        assert rt.hook_value("x" * (rt.HOOK_VALUE_LIMIT + 1)) == {"sandbox-ai": "too large",
                                                                  "length": rt.HOOK_VALUE_LIMIT + 1}
    else:
        deep = []
        for _ in range(40):
            deep = [deep]
        assert "too large" in json.dumps(rt.hook_value(deep))


def test_hook_client(scenario, tmp_path):
    message = json.dumps({"tool_name": "Bash", "tool_input": {"command": "curl x" if scenario == "sad" else "ls"},
                          "cwd": "/work"}).encode()
    path, stop = Path(tempfile.mkdtemp(prefix="hookc-", dir="/tmp")) / "s.sock", threading.Event()
    try:
        if scenario != "edge":
            rt.start_hook_server({"rules": [{"name": "r", **HOOK_RULES}]}, path=path, stop=stop)
        stderr = io.StringIO()
        status = rt.hook_client("claude", "PreToolUse", stdin=io.BytesIO(message), stderr=stderr, socket_path=path,
                                timeout=20)
        if scenario == "happy":
            assert status == 0 and stderr.getvalue() == ""
        elif scenario == "sad":
            assert status == 2 and "no-curl" in stderr.getvalue()
        else:  # no supervisor: refused, never let through
            assert status == 2 and "could not be checked" in stderr.getvalue()
    finally:
        stop.set()
        shutil.rmtree(path.parent, ignore_errors=True)


def test_start_hook_server(scenario, tmp_path):
    path = Path(tempfile.mkdtemp(prefix="hooks-", dir="/tmp")) / "hooks" / "decide.sock"
    taint, stop = rt.Taint(), threading.Event()
    try:
        rules = HOOK_RULES if scenario != "sad" else {"version": 1}
        thread = rt.start_hook_server({"rules": [{"name": "r", **rules}]}, taint=taint, path=path,
                                      log=tmp_path / "tools.jsonl", stop=stop)
        assert thread.is_alive() and path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o666
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
        tool = "WebSearch" if scenario == "edge" else "Bash"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(20)
            client.connect(str(path))
            client.sendall(json.dumps({"agent": "claude", "event": "PreToolUse", "tool": tool,
                                       "input": {"command": "curl x"}}).encode() + b"\n")
            answer = json.loads(client.makefile("rb").readline())
        record = json.loads((tmp_path / "tools.jsonl").read_text().splitlines()[-1])
        if scenario == "happy":
            assert answer["decision"] == "deny" and record["rule"] == "no-curl"
        elif scenario == "sad":  # no tool rules: allowed, still logged
            assert answer["decision"] == "allow" and record["decision"] == "allow"
        else:  # an untrusted tool: the run is tainted before the tool runs
            assert answer["decision"] == "allow" and taint.shown()["source"]["rule"] == "search"
    finally:
        stop.set()
        thread.join(5)
        assert not thread.is_alive()  # STOP ends it
        shutil.rmtree(path.parent.parent, ignore_errors=True)


def test_hooks_active(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(HOOK_RULES if scenario != "edge" else {"version": 1, "defaults": {"tool": "ask"}}))
    if scenario == "happy":
        assert s.hooks_active(s.parse_args(["claude", "--rules", str(mine)]))
    elif scenario == "sad":  # rules for HTTP and MCP only, or no agent at all (a probe)
        github = tmp_path / "github.json"
        github.write_text(json.dumps(GH_RULES))
        assert not s.hooks_active(s.parse_args(["claude", "--rules", str(github)]))
        assert not s.hooks_active(s.parse_args(["claude", "--rules", str(mine), "--probe"]))
    else:  # a default other than allow is enough
        assert s.hooks_active(s.parse_args(["codex", "--rules", str(mine)]))


def test_hooks_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(HOOK_RULES))
    if scenario == "happy":
        config = s.hooks_config(s.parse_args(["claude", "--rules", str(mine)]))
        assert [r["id"] for r in config["rules"][0]["tool"]] == ["no-curl"]
        assert config["rules"][0]["untrusted"][0]["id"] == "search" and config["rules"][0]["defaults"] == {"tool": "allow"}
    elif scenario == "sad":
        assert s.hooks_config(s.parse_args(["claude"])) is None
    else:  # only the rulesets with something for the agent's tools, and only their tool part
        github = tmp_path / "github.json"
        github.write_text(json.dumps(GH_RULES))
        config = s.hooks_config(s.parse_args(["claude", "--rules", str(github), "--rules", str(mine)]))
        assert [r["name"] for r in config["rules"]] == ["--rules mine.json"] and "http" not in config["rules"][0]


def test_hook_files(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(HOOK_RULES))
    a = s.parse_args([{"happy": "claude", "sad": "opencode", "edge": "codex"}[scenario], "--rules", str(mine)])
    files = s.hook_files(a)
    if scenario == "happy":
        settings = json.loads(files["claude-code/managed-settings.json"])
        assert settings["allowManagedHooksOnly"] is True and set(settings["hooks"]) == {"PreToolUse", "PostToolUse"}
    elif scenario == "sad":
        assert list(files) == ["opencode-hooks.js"] and "--internal-hook" in files["opencode-hooks.js"]
    else:
        requirements = tomllib.loads(files["codex/requirements.toml"])
        assert requirements["allow_managed_hooks_only"] is True and requirements["features"]["hooks"] is True


def test_hook_mount(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(HOOK_RULES))
    if scenario == "happy":
        assert s.hook_mount(s.parse_args(["codex", "--rules", str(mine)]), tmp_path) == (tmp_path / "codex",
                                                                                          "/etc/codex")
    elif scenario == "sad":
        assert s.hook_mount(s.parse_args(["claude"]), tmp_path) is None
    else:  # OpenCode's plugin lies in the session directory, mounted anyway
        assert s.hook_mount(s.parse_args(["opencode", "--rules", str(mine)]), tmp_path) is None


def test_hook_problem(scenario):
    def version(text):
        return lambda command, **kwargs: SimpleNamespace(stdout=text)
    old = version("codex-cli 0.130.9")
    if scenario == "happy":  # no managed hooks, or an agent that holds them: nothing to say
        assert rt.hook_problem({"command": ["codex"], "hooks": None, "hook_agent": None}, run=old) is None
        assert rt.hook_problem({"command": ["codex"], "hook_agent": "codex"}, run=version("codex-cli 0.131.0")) is None
    elif scenario == "sad":  # approved kit hooks alone, an old Codex: refused like the rule hooks
        assert "the kits' approved hooks cannot hold" in rt.hook_problem(
            {"command": ["codex"], "hooks": None, "hook_agent": "codex"}, run=old)
        assert "the rules for the agent's own tools" in rt.hook_problem(
            {"command": ["codex"], "hooks": {"agent": "codex"}}, run=old)
    else:  # a config.json of before (no hook_agent) still has its rule hooks checked
        assert rt.hook_problem({"command": ["codex"], "hooks": {"agent": "codex", "rules": []}}, run=old)


def test_kit_hook(scenario):
    message = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}).encode()
    asked = []

    def check(agent, event, stdin=None, stderr=None):
        asked.append(json.loads(stdin.read()))
        return 2 if "curl" in json.dumps(asked[-1]) else 0
    out, err = io.BytesIO(), io.StringIO()
    if scenario == "happy":  # no rewrite: passed through, nothing asked
        status = rt.kit_hook("claude", 10, "cat >/dev/null; echo '{}'", stdin=io.BytesIO(message), stdout=out,
                             stderr=err, check=check)
        assert status == 0 and out.getvalue() == b"{}\n" and asked == []
    elif scenario == "sad":  # a rewrite the rules refuse never reaches the agent
        rewrite = json.dumps({"hookSpecificOutput": {"updatedInput": {"command": "curl x"}}})
        status = rt.kit_hook("claude", 10, f"cat >/dev/null; printf '%s' '{rewrite}'", stdin=io.BytesIO(message),
                             stdout=out, stderr=err, check=check)
        assert status == 2 and out.getvalue() == b"" and asked[0]["tool_input"] == {"command": "curl x"}
    else:  # an answer naming updatedInput that cannot be read is refused
        status = rt.kit_hook("claude", 10, "cat >/dev/null; echo 'updatedInput {'", stdin=io.BytesIO(message),
                             stdout=out, stderr=err, check=check)
        assert status == 2 and "no JSON" in err.getvalue() and asked == []


def test_kit_handler(scenario):
    a = SimpleNamespace(agent="claude")
    if scenario == "happy":
        handler = s.kit_handler(a, {"type": "command", "command": "/k/check.sh", "timeout": 5})
        assert handler["command"] == "/usr/bin/python3 -I /sandbox/runtime.py --internal-kit-hook claude 5 /k/check.sh"
        assert handler["timeout"] == 5 + s.HOOK_AGENT_TIMEOUT
    elif scenario == "sad":  # a command with spaces and quotes stays one argument
        handler = s.kit_handler(a, {"type": "command", "command": "/k/a b.sh 'x'"})
        assert shlex_split(handler["command"])[6:] == ["/k/a b.sh 'x'"]
    else:  # no timeout of its own: the agents' default
        assert s.kit_handler(a, {"type": "command", "command": "x"})["timeout"] == s.KIT_HOOK_TIMEOUT + \
            s.HOOK_AGENT_TIMEOUT


def shlex_split(text):
    import shlex
    return shlex.split(text)


def test_opencode_recheck(scenario, monkeypatch):
    staged = {"trusted": True, "errors": [], "plugins": ["guard.js"] if scenario != "edge" else []}
    a = SimpleNamespace(kit_hooks=[staged])
    monkeypatch.setattr(s, "hooks_active", lambda a: scenario != "sad")
    # happy: rules and a kit plugin; sad: no rules; edge: kit hooks without plugins
    assert s.opencode_recheck(a) is (scenario == "happy")


def test_helper_name(scenario, tmp_path):
    name = s.helper_name("opencode", tmp_path / "w")
    if scenario == "happy":  # an MCP server name of its agent
        assert re.fullmatch(s.MCP_NAME, name) and name.startswith("helper-opencode-")
    elif scenario == "sad":  # another workspace: another helper
        assert name != s.helper_name("opencode", tmp_path / "other")
    else:  # the same: the same helper, reused
        assert name == s.helper_name("opencode", str(tmp_path / "w"))


def test_add_helpers(scenario, tmp_path):
    if scenario == "happy":
        a = s.parse_args(["claude", "--helper", "codex"])
        a.workspaces = [str(tmp_path)]
        s.add_helpers(a)
        assert a.mcp == [s.helper_name("codex", str(tmp_path))]
        assert a.helper_definitions[a.mcp[0]]["helper"] == "codex"
    elif scenario == "sad":  # an MCP server sandbox has no helpers
        a = s.parse_args(["claude", "--helper", "codex", "--mcp-socket-dir", str(tmp_path / "s")])
        a.workspaces = [str(tmp_path)]
        with pytest.raises(ValueError, match="agent's start"):
            s.add_helpers(a)
    else:  # none asked for: nothing added
        a = s.parse_args(["claude", "--mcp", "demo"])
        a.workspaces = [str(tmp_path)]
        s.add_helpers(a)
        assert a.mcp == ["demo"] and a.helper_definitions == {}


def test_runs_agent(scenario, tmp_path):
    if scenario == "happy":
        assert s.runs_agent(s.parse_args(["opencode"]))
    elif scenario == "sad":  # another program in the sandbox
        assert not s.runs_agent(s.parse_args(["opencode", "--binary", "python3"]))
    else:  # a helper's server runs the agent for each task
        assert s.runs_agent(s.parse_args(["opencode", "--binary", "python3", "--helper-of", "helper-x",
                                          "--mcp-socket-dir", str(tmp_path)]))


def test_companions(scenario):
    if scenario == "happy":
        assert s.companions(s.parse_args(["claude", "--with", "codex", "--with", "opencode"])) == ["codex", "opencode"]
    elif scenario == "sad":  # the lead itself is never its own companion
        assert s.companions(s.parse_args(["claude", "--with", "claude"])) == []
    else:  # named twice: once
        assert s.companions(s.parse_args(["claude", "--with", "codex", "--with", "codex"])) == ["codex"]


def test_team(scenario):
    if scenario == "happy":
        assert s.team(s.parse_args(["codex", "--with", "claude"])) == ["codex", "claude"]
    elif scenario == "sad":
        assert s.team(s.parse_args(["codex"])) == ["codex"]
    else:  # the lead first, whatever the order of --with
        assert s.team(s.parse_args(["opencode", "--with", "codex", "--with", "opencode"]))[0] == "opencode"


def test_check_companions(scenario, tmp_path):
    if scenario == "happy":
        s.check_companions(s.parse_args(["claude", "--with", "codex"]))
    elif scenario == "sad":
        with pytest.raises(ValueError, match="that is the agent"):
            s.check_companions(s.parse_args(["claude", "--with", "claude"]))
    else:
        with pytest.raises(ValueError, match="agent's start"):
            s.check_companions(s.parse_args(["claude", "--with", "codex", "--mcp-socket-dir", str(tmp_path)]))


def test_companion_auth(scenario):
    if scenario == "happy":
        assert s.companion_auth(s.parse_args(["claude", "--with", "codex"])) == "auto"
    elif scenario == "sad":
        assert s.companion_auth(s.parse_args(["claude", "--with", "codex", "--no-auth"])) == "none"
    else:  # --llm: the endpoint is the provider, for them too
        a = s.parse_args(["claude", "--with", "codex"])
        a.llm = "local"
        assert s.companion_auth(a) == "none"


def test_companion_logins(scenario, tmp_path, monkeypatch):
    home, source = tmp_path / "home", tmp_path / "auth.json"
    home.mkdir()
    source.write_text("{}")
    source.chmod(0o600)
    monkeypatch.setattr(s, "auth_sources", lambda agent: [(source, s.AUTH_TARGETS[agent])])
    a = s.parse_args(["claude", "--with", "codex"] + (["--no-auth"] if scenario == "sad" else []))
    if scenario == "edge":  # a login already in a reused HOME is kept
        (home / ".codex").mkdir()
        (home / ".codex/auth.json").write_text('{"kept": 1}')
    copied = s.companion_logins(a, home)
    if scenario == "happy":
        assert copied == [(source, ".codex/auth.json")] and (home / ".codex/auth.json").read_text() == "{}"
    elif scenario == "sad":
        assert copied == [] and not (home / ".codex/auth.json").exists()
    else:
        assert copied == [] and json.loads((home / ".codex/auth.json").read_text()) == {"kept": 1}


def test_codex_companion_defaults(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config = home / ".codex/config.toml"
    if scenario == "happy":
        config.write_text('[projects."/work"]\ntrust_level = "trusted"\n')
        s.codex_companion_defaults(home)
        document = tomllib.loads(config.read_text())
        assert document["sandbox_mode"] == "danger-full-access" and document["projects"]["/work"]
    elif scenario == "sad":  # the human's own choice stays, however the key is written; a broken file stays as it is
        for text in ('sandbox_mode = "workspace-write"\n', '"sandbox_mode" = "workspace-write"\n'):
            config.write_text(text)
            s.codex_companion_defaults(home)
            assert config.read_text() == text
        # A sandbox_mode inside a table (behind an indented header and a string holding "[t]") is no choice for
        # the top level: added there, the rest untouched and still valid.
        config.write_text('  [projects."/a"]\n  x = """\n[t]\n"""\nsandbox_mode = "read-only"\n')
        s.codex_companion_defaults(home)
        document = tomllib.loads(config.read_text())
        assert document["sandbox_mode"] == "danger-full-access"
        assert document["projects"]["/a"] == {"x": "[t]\n", "sandbox_mode": "read-only"}
        config.write_text("not = = toml")
        s.codex_companion_defaults(home)
        assert config.read_text() == "not = = toml"
    else:  # no file yet
        s.codex_companion_defaults(home)
        assert tomllib.loads(config.read_text()) == {"sandbox_mode": "danger-full-access"}


def test_rule_groups(scenario):
    groups = s.rule_groups({"happy": "claude", "sad": "codex", "edge": "opencode"}[scenario])
    assert set(groups) == {"PreToolUse", "PostToolUse"}
    command = groups["PreToolUse"][0]["hooks"][0]["command"]
    assert command.endswith(f"--internal-hook {({'happy': 'claude', 'sad': 'codex', 'edge': 'opencode'})[scenario]} "
                            "PreToolUse") and groups["PreToolUse"][0]["matcher"] == "*"


def test_agent_hook_files(scenario):
    a = s.parse_args(["claude"])
    if scenario == "happy":
        files = s.agent_hook_files(a, "claude", s.rule_groups("claude"))
        assert json.loads(files["claude-code/managed-settings.json"])["allowManagedHooksOnly"] is True
    elif scenario == "sad":
        files = s.agent_hook_files(a, "codex", s.rule_groups("codex"))
        assert tomllib.loads(files["codex/requirements.toml"])["allow_managed_hooks_only"] is True
    else:  # OpenCode without rules: no plugin
        assert s.agent_hook_files(a, "opencode", {}, companion=True) == {}


def test_companion_hook_mounts(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s, "hooks_active", lambda a: scenario != "sad")
    agents = ["claude", "--with", "codex", "--with", "opencode"]
    mounts = s.companion_hook_mounts(s.parse_args(agents), tmp_path)
    if scenario == "happy":  # Codex's directory; OpenCode's plugin lies in the session directory anyway
        assert mounts == [(tmp_path / "codex", "/etc/codex")]
    elif scenario == "sad":  # no rules: nothing to mount
        assert mounts == []
    else:
        assert s.companion_hook_mounts(s.parse_args(["codex", "--with", "claude"]), tmp_path) == [
            (tmp_path / "claude-code", "/etc/claude-code")]
def test_trusted_plugin_hooks(scenario):
    staged = {"happy": {"trusted": True, "errors": []}, "sad": {"trusted": False, "errors": []},
              "edge": {"trusted": True, "errors": ["broken"]}}[scenario]
    a = SimpleNamespace(plugin_hooks=[staged])
    assert s.trusted_plugin_hooks(a) == ([staged] if scenario == "happy" else [])


def test_plugin_hook_notes(scenario, monkeypatch):
    item = {"label": "plugin p@m", "plugin": "p@m", "hooks": [{}], "trusted": scenario == "happy",
            "errors": ["no file"] if scenario == "edge" else []}
    a = SimpleNamespace(plugin_hooks=[item])
    monkeypatch.setattr(s, "managed_hooks", lambda a: True)
    (note,) = s.plugin_hook_notes(a)
    if scenario == "happy":
        assert note == "plugin p@m: 1 approved hook(s)"
    elif scenario == "sad":  # named with the command that approves it
        assert note.endswith("sandbox-ai plugins claude trust-hooks p@m")
    else:
        assert "do not run: no file" in note
    monkeypatch.setattr(s, "managed_hooks", lambda a: False)
    assert s.plugin_hook_notes(a) == [], "without managed hooks Claude Code runs them itself"


def test_toml_string(scenario):
    if scenario == "happy":  # what TOML reads back: quotes and backslashes included
        value = 'echo "a" \\ b'
        assert tomllib.loads("x = " + s.toml_string(value))["x"] == value
    elif scenario == "sad":  # an unpaired surrogate is no character
        with pytest.raises(ValueError, match="unpaired surrogate"):
            s.toml_string("\udcff")
    else:  # beyond the BMP: the character itself, not a surrogate pair TOML refuses
        assert s.toml_string("🚫") == '"🚫"' and tomllib.loads("x = " + s.toml_string("🚫"))["x"] == "🚫"


def test_hooks_supported(scenario):
    def version(text):
        return lambda command, **kwargs: SimpleNamespace(stdout=text)
    if scenario == "happy":
        assert rt.hooks_supported("claude", ["claude"], run=lambda *a, **kw: 1 / 0) is None  # only Codex is asked
        assert rt.hooks_supported("codex", ["codex"], run=version("codex-cli 0.131.0\n")) is None
        assert rt.hooks_supported("codex", ["codex"], run=version("codex-cli 1.2.3")) is None
    elif scenario == "sad":  # an older Codex would ignore the hooks without a word: refused
        assert "older than 0.131.0" in rt.hooks_supported("codex", ["codex"], run=version("codex-cli 0.130.9"))
    else:  # a version that cannot be read is no version that holds them
        assert "could not be read" in rt.hooks_supported("codex", ["codex"], run=version("codex-cli dev"))

        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])
        assert "could not be read" in rt.hooks_supported("codex", ["/nowhere/codex"], run=missing)
        assert "could not be read" in rt.hooks_supported("codex", [], run=version("0.200.0"))


# --- every start: names and history (starts.py) ----------------------------------------------------------


def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path / "state" / "sandbox-ai"


def test_load_starts_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_starts_module()
        return
    module = s.load_starts_module()
    if scenario == "happy":
        assert callable(module.record) and callable(module.auto_name)
    else:
        assert module is not s.load_starts_module()  # loaded afresh, like the other helpers


def test_pod_name(scenario):
    a = s.parse_args(["codex"])
    a.run_id = "codex-1a2b3c4d"
    if scenario == "happy":
        assert s.pod_name(a) == "agent-codex-1a2b3c4d" and s.pod_name(a, "x-1") == "agent-x-1"
    elif scenario == "sad":
        a.pod_name = "agent-mcp-demo"  # a session or MCP server chose it
        assert s.pod_name(a) == "agent-mcp-demo"
    else:
        a.vpn_pod = "agent-vpn-pod"  # vpn.py made the pod with the tunnel
        assert s.pod_name(a) == "agent-vpn-pod"


def test_run_name(scenario, tmp_path, monkeypatch):
    state_home(tmp_path, monkeypatch)
    if scenario == "happy":
        a = s.parse_args(["codex"])
        a.start_name = "codex-app-1a2b"
        assert s.run_name(a) == "codex-app-1a2b"
        assert s.run_name(s.parse_args(["codex"])) is None  # an MCP server, a probe: no name of its own
        return
    a = s.parse_args(["codex", "--tmux-socket-dir", str(tmp_path / "t"), "--start-id", "web"])
    if scenario == "sad":
        assert s.run_name(a) == "web"  # its session.json is gone: the id
    else:
        sessions = s.load_sessions_module()
        sessions.root().joinpath("web").mkdir(parents=True)
        sessions.save("web", {"agent": "codex", "options": [], "name": "renamed"})
        assert s.run_name(a) == "renamed"  # renamed since the start


def test_project_label(scenario, tmp_path):
    if scenario == "happy":
        assert s.project_label(s.parse_args(["codex", "--workspace", str(tmp_path / "my-app")])) == "my-app"
    elif scenario == "sad":
        a = s.parse_args(["codex", "--workspace", str(tmp_path / "my-app"), "--project-name", "shop"])
        assert s.project_label(a) == "shop"
    else:
        assert s.project_label(s.parse_args(["codex", "--workspace", str(tmp_path / "Mein Projekt (alt)")])) == \
            "Mein-Projekt-alt"
        assert s.project_label(s.parse_args(["codex", "--workspace", "/"])) == "work"


def test_name_start(scenario, tmp_path, monkeypatch):
    state_home(tmp_path, monkeypatch)
    workspace = str(tmp_path / "shop")
    if scenario == "happy":
        name = s.name_start(s.parse_args(["claude", "--workspace", workspace]))
        assert re.fullmatch(r"claude-shop-[0-9a-f]{4}", name)
        assert s.name_start(s.parse_args(["claude", "--workspace", workspace, "--name", "review"])) == "review"
        assert s.name_start(s.parse_args(["claude", "--session", "web"])) == "web"
    elif scenario == "sad":
        run = s.runs_dir() / "claude-1"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"run": "claude-1", "name": "review", "pid": os.getpid()}))
        with pytest.raises(ValueError, match="The name review is in use"):
            s.name_start(s.parse_args(["claude", "--name", "review"]))
        # A plan starts nothing: the name is shown, not checked.
        assert s.name_start(s.parse_args(["claude", "--name", "review", "--dry-run"])) == "review"
    else:
        # MCP servers and the supervisor of a session are named elsewhere.
        assert s.name_start(s.parse_args(["codex", "--mcp-socket-dir", str(tmp_path / "m")])) is None
        assert s.name_start(s.parse_args(["codex", "--tmux-socket-dir", str(tmp_path / "t")])) is None
        long = s.name_start(s.parse_args(["opencode", "--workspace", str(tmp_path / ("x" * 60))]))
        assert len(long) == 40 and long.startswith("opencode-xxx")


def test_history_event(scenario, tmp_path, monkeypatch):
    root = state_home(tmp_path, monkeypatch)
    control = tmp_path / "control"
    control.mkdir()
    (control / "run.json").write_text(json.dumps({"run": "claude-1a2b3c4d", "name": "review", "agent": "claude",
                                                  "workspace": "/w", "start": None}))
    a = s.parse_args(["claude"])
    a.control_dir = control
    history = lambda: [json.loads(l) for l in (root / "history" / "review.jsonl").read_text().splitlines()]
    if scenario == "happy":
        assert s.history_event(a, "start") and s.history_event(a, "exit", 0)
        assert [(e["event"], e["run"], e.get("status")) for e in history()] == \
            [("start", "claude-1a2b3c4d", None), ("exit", "claude-1a2b3c4d", 0)]
        assert not s.history_event(a, "exit", 1)  # one exit per run
    elif scenario == "sad":
        for options in (["--probe"], ["--mcp-socket-dir", str(tmp_path / "m")]):
            b = s.parse_args(["claude", *options])
            b.control_dir = control
            assert s.history_event(b, "start") is False
        assert not (root / "history").exists()
        (control / "run.json").write_text("{broken")
        assert s.history_event(a, "start") is False  # never raises
    else:
        # A session's supervisor: sessions.py notes the start (or rebirth); the exit is the launcher's.
        b = s.parse_args(["claude", "--tmux-socket-dir", str(tmp_path / "t")])
        b.control_dir = control
        assert s.history_event(b, "start") is False and s.history_event(b, "exit", 143)
        assert history()[-1]["status"] == 143


def test_names_in_project_files_and_validation(tmp_path, monkeypatch):
    """The project file's "name" is the PROJECT part of the automatic names; --name is not for project files."""
    project = tmp_path / "proj"
    project.mkdir()
    path = project / ".sandbox-ai.json"
    for document, message in (({"name": "../x"}, "name \\(the PROJECT part"), ({"name": 5}, "name \\(the PROJECT part"),
                              ({"options": ["--name", "x"]}, "option --name is not allowed"),
                              ({"options": ["-pn", "x"]}, "option --project-name is not allowed")):
        path.write_text(json.dumps({"version": 1, "agent": "codex", **document}))
        s.trust_config(path)
        with pytest.raises(ValueError, match=message):
            s.load_project_config(s.parse_args(["codex", "--workspace", str(project)]))
    path.write_text(json.dumps({"version": 1, "agent": "codex", "name": "shop"}))
    s.trust_config(path)
    argv = ["codex", "--workspace", str(project)]
    _, document = s.load_project_config(s.parse_args(argv))
    assert s.parse_args(s.merge_config(argv, s.parse_args(argv), document)).project_name == "shop"
    # The command line wins.
    argv = ["codex", "--workspace", str(project), "-pn", "mine"]
    assert s.parse_args(s.merge_config(argv, s.parse_args(argv), document)).project_name == "mine"
    for options, message in ((["--name", "a b"], "--name NAME: 1-40"), (["--project-name=-x"], "--project-name"),
                             (["--name", "x", "--session", "y"], "names the start already"),
                             (["--name", "mcp-x"], "belong to the MCP server"),
                             (["--start-id", "x"], "Invalid --start-id")):
        with pytest.raises(ValueError, match=message):
            s.validate(s.parse_args(["codex", *options]))


def test_detachable(scenario, tmp_path):
    if scenario == "happy":
        a = s.parse_args(["claude", "--tmux"])
        assert s.detachable(a) is True and s.detachable(s.parse_args(["claude", "--session", "web"])) is True
    elif scenario == "sad":
        assert s.detachable(s.parse_args(["claude", "--no-tmux"])) is False
        assert s.detachable(s.parse_args(["claude", "--tmux", "--probe"])) is False
    else:
        # A session's own supervisor and an MCP server run in the background already.
        for options in (["--tmux-socket-dir", str(tmp_path)], ["--mcp-socket-dir", str(tmp_path)]):
            assert s.detachable(s.parse_args(["claude", "--tmux", *options])) is False


# --- kept HOMEs ---------------------------------------------------------------------------------------------


def test_resolve_keep(scenario, tmp_path):
    if scenario == "happy":
        for argv, keep in ((["claude"], True), (["claude", "--keep"], True), (["claude", "--no-keep"], False),
                           (["claude", "--session", "web"], True)):
            a = s.parse_args(argv)
            assert s.resolve_keep(a) is None and a.keep_home is keep, argv
    elif scenario == "sad":
        a = s.parse_args(["claude", "--keep"])
        a.forbid_keep = True  # the administrator's policy
        assert "forbid_keep); --keep is ignored" in s.resolve_keep(a) and a.keep_home is False
        a = s.parse_args(["claude", "--keep", "--state-dir", str(tmp_path)])
        assert "no effect with --state-dir" in s.resolve_keep(a) and a.keep_home is False
        a = s.parse_args(["claude", "--session", "web", "--no-keep"])
        assert "--no-keep is ignored" in s.resolve_keep(a) and a.keep_home is True
    else:
        # A session's supervisor, an MCP server and a probe keep nothing of their own.
        for options in (["--tmux-socket-dir", str(tmp_path)], ["--mcp-socket-dir", str(tmp_path)], ["--probe"]):
            a = s.parse_args(["claude", "--keep", *options])
            assert s.resolve_keep(a) is None and a.keep_home is False


def test_plan_kept_home(scenario, tmp_path, monkeypatch):
    state_home(tmp_path, monkeypatch)
    a = s.parse_args(["codex", "--no-tmux"])
    a.start_name = "codex-app-1a2b"
    s.resolve_keep(a)
    if scenario == "happy":
        assert s.plan_kept_home(a) is True and a.start_key == "codex-app-1a2b" and a.foreground_start
        assert a.state_dir == s.load_sessions_module().root() / "codex-app-1a2b" / "home"
        assert not a.state_dir.exists()  # made by sessions.begin once every check passed
    elif scenario == "sad":
        for argv in (["codex", "--no-keep"], ["codex", "--dry-run"], ["codex", "--tmux"]):
            b = s.parse_args(argv)
            b.start_name = "x"
            s.resolve_keep(b)
            assert s.plan_kept_home(b) is False and b.state_dir is None, argv
    else:
        a.start_name = "bad name"  # validate() refuses it
        assert s.plan_kept_home(a) is False and a.state_dir is None


def test_expire_kept(scenario, tmp_path, monkeypatch):
    state_home(tmp_path, monkeypatch)
    gc = Mock(return_value={"removed": [{"name": "old"}]})
    monkeypatch.setattr(s, "load_starts_module", lambda: SimpleNamespace(gc=gc))
    a = s.parse_args(["claude"])
    if scenario == "happy":
        assert s.expire_kept(a, {"keep_days": 30}) == ["old"]
        assert gc.call_args.args == (30,) and gc.call_args.kwargs == {"expired_only": True}
    elif scenario == "sad":
        assert s.expire_kept(a, {}) == [] and not gc.called  # never on its own
        assert s.expire_kept(s.parse_args(["claude", "--dry-run"]), {"keep_days": 1}) == []
    else:
        gc.side_effect = OSError("busy")
        assert s.expire_kept(a, {"keep_days": 30}) == []  # a failed expiry never stops a start


def test_keep_in_project_files_policy_and_defaults(tmp_path, monkeypatch):
    """"keep" in a project file, forbid_keep in the policy and keep_days in your defaults are checked when read."""
    project = tmp_path / "proj"
    project.mkdir()
    path = project / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "codex", "keep": False}))
    s.trust_config(path)
    argv = ["codex", "--workspace", str(project)]
    _, document = s.load_project_config(s.parse_args(argv))
    assert s.parse_args(s.merge_config(argv, s.parse_args(argv), document)).keep is False
    assert s.parse_args(s.merge_config(argv + ["-kp"], s.parse_args(argv + ["-kp"]), document)).keep is True
    path.write_text(json.dumps({"version": 1, "agent": "codex", "keep": 1}))
    s.trust_config(path)
    with pytest.raises(ValueError, match="keep is true or false"):
        s.load_project_config(s.parse_args(argv))
    with pytest.raises(ValueError, match="forbid_keep is true or false"):
        s.load_policy(policy_file(tmp_path, {"version": 1, "forbid_keep": "yes"}, monkeypatch))
    assert s.load_policy(policy_file(tmp_path, {"version": 1, "forbid_keep": True}, monkeypatch))["forbid_keep"]
    a = s.parse_args(["codex", "--session", "web"])
    with pytest.raises(ValueError, match="administrator's policy forbids"):
        s.check_policy(a, {"version": 1, "forbid_keep": True})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    s.defaults_file().parent.mkdir(parents=True)
    for days, ok in ((30, True), (0, False), ("30", False), (True, False)):
        s.defaults_file().write_text(json.dumps({"version": 1, "keep_days": days}))
        if ok:
            assert s.load_user_defaults(s.parse_args(["codex"]))["keep_days"] == days
        else:
            with pytest.raises(ValueError, match="keep_days is a number of days"):
                s.load_user_defaults(s.parse_args(["codex"]))


def test_kept_start_dir(scenario, tmp_path, monkeypatch):
    state_home(tmp_path, monkeypatch)
    root = s.load_sessions_module().root()
    if scenario == "happy":
        a = s.parse_args(["codex", "--tmux-socket-dir", str(root / "web/tmux"), "--start-id", "web",
                          "--state-dir", str(root / "web/home")])
        assert s.kept_start_dir(a) == root / "web"
        b = s.parse_args(["codex"])
        b.start_key, b.state_dir = "task", root / "task/home"  # a kept task (plan_kept_home)
        assert s.kept_start_dir(b) == root / "task"
    elif scenario == "sad":
        assert s.kept_start_dir(s.parse_args(["codex"])) is None
        # A session that does not keep its HOME: the launcher's HOME is temporary.
        a = s.parse_args(["codex", "--tmux-socket-dir", str(root / "web/tmux"), "--start-id", "web"])
        assert s.kept_start_dir(a) is None
    else:
        # A HOME of your own given to a session's supervisor is not the start's.
        a = s.parse_args(["codex", "--tmux-socket-dir", str(root / "web/tmux"), "--start-id", "web",
                          "--state-dir", str(tmp_path / "mine")])
        assert s.kept_start_dir(a) is None


def test_record_home_manifest(scenario, tmp_path, monkeypatch, capsys):
    state_home(tmp_path, monkeypatch)
    directory = s.load_sessions_module().root() / "task"
    home = directory / "home"
    home.mkdir(parents=True)
    (home / ".bashrc").write_text("x\n")
    a = s.parse_args(["codex"])
    a.start_key, a.state_dir, a.run_id = "task", home, "codex-1a2b3c4d"
    if scenario == "happy":
        assert s.record_home_manifest(a, home) is True
        manifest = json.loads((directory / "home-manifest.json").read_text())
        assert manifest["run"] == "codex-1a2b3c4d" and ".bashrc" in manifest["files"]
    elif scenario == "sad":
        assert s.record_home_manifest(s.parse_args(["codex"]), home) is False
        assert not (directory / "home-manifest.json").exists()
    else:
        (directory / "home-manifest.json").mkdir()  # in the way: reported, the start goes on
        assert s.record_home_manifest(a, home) is False
        assert "were not recorded" in capsys.readouterr().err


def staged_hooks(trusted=True, errors=(), plugins=()):
    return {"kit": "mine", "label": "kit mine", "trusted": trusted, "errors": list(errors), "plugins": list(plugins),
            "hooks": [{"event": "PreToolUse"}], "target": "/sandbox/session/kits/hooks/1",
            "groups": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/sandbox/session/kits/hooks/1/check.sh"}]}]}}


def test_trusted_kit_hooks(scenario):
    a = s.parse_args(["claude"])
    if scenario == "happy":
        a.kit_hooks = [staged_hooks()]
        assert s.trusted_kit_hooks(a) == a.kit_hooks
    elif scenario == "sad":  # not approved, or broken: never
        a.kit_hooks = [staged_hooks(trusted=False), staged_hooks(errors=["x is a symlink"])]
        assert s.trusted_kit_hooks(a) == []
    else:  # a run that staged no hooks
        assert s.trusted_kit_hooks(a) == []


def test_managed_hooks(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    a = s.parse_args(["codex"])
    if scenario == "happy":
        a.kit_hooks = [staged_hooks()]
        assert s.managed_hooks(a) and not s.hooks_active(a)
    elif scenario == "sad":
        a.kit_hooks = [staged_hooks(trusted=False)]
        assert not s.managed_hooks(a)
    else:  # rules for the agent's tools alone
        mine = tmp_path / "mine.json"
        mine.write_text(json.dumps(HOOK_RULES))
        assert s.managed_hooks(s.parse_args(["codex", "--rules", str(mine)]))


def test_hook_groups(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(HOOK_RULES))
    if scenario == "happy":  # the rule hook first, then the kit's
        a = s.parse_args(["claude", "--rules", str(mine)])
        a.kit_hooks = [staged_hooks()]
        commands = [h["command"] for g in s.hook_groups(a)["PreToolUse"] for h in g["hooks"]]
        assert commands[0].endswith("--internal-hook claude PreToolUse") and commands[1].endswith("/1/check.sh")
    elif scenario == "sad":
        assert s.hook_groups(s.parse_args(["claude"])) == {}
    else:  # kit hooks alone: no rule hook
        a = s.parse_args(["codex"])
        a.kit_hooks = [staged_hooks()]
        assert list(s.hook_groups(a)) == ["PreToolUse"] and len(s.hook_groups(a)["PreToolUse"]) == 1


def test_kit_hook_notes(scenario):
    if scenario == "happy":
        assert s.kit_hook_notes("claude", [staged_hooks()]) == ["kit mine: 1 approved hook(s) for claude"]
    elif scenario == "sad":
        note, = s.kit_hook_notes("claude", [staged_hooks(trusted=False)])
        assert "not approved" in note and "sandbox-ai kits trust-hooks mine --agent claude" in note
    else:
        note, = s.kit_hook_notes("codex", [staged_hooks(errors=["a.sh is a symlink", "b"])])
        assert note == "kit mine: its hooks for codex do not run: a.sh is a symlink (and 1 more)"


# --- --telemetry (test_telemetry.py has the runs) ---------------------------------------------------------

def test_telemetry_level(scenario):
    a = s.parse_args(["claude"])
    if scenario == "happy":
        assert s.telemetry_level(a) == "off"
        a.telemetry_default = "strict"
        assert s.telemetry_level(a) == "strict"
        a.telemetry = "on"
        assert s.telemetry_level(a) == "on", "the option wins over the defaults file"
    elif scenario == "sad":  # below the security mode's floor: raised to it
        a.mode, a.telemetry_default = "paranoid", "on"
        assert s.telemetry_level(a) == "strict"
    else:
        a.mode = "high"
        assert s.telemetry_level(a) == "off" and s.MODES.MODES["high"].get("telemetry") is None


def test_telemetry_environment(scenario):
    if scenario == "happy":
        env = s.telemetry_environment(s.parse_args(["claude", "--with", "opencode"]))
        assert env == {**s.TELEMETRY_ENV["claude"]["off"], **s.TELEMETRY_ENV["opencode"]["off"]}
    elif scenario == "sad":
        assert s.telemetry_environment(s.parse_args(["claude", "--telemetry", "on"])) == {}
        assert s.telemetry_environment(s.parse_args(["claude", "--binary", "/bin/sh"])) == {}, "no agent runs"
    else:
        env = s.telemetry_environment(s.parse_args(["opencode", "--telemetry", "strict"]))
        assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1" and env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


def test_telemetry_plan(scenario):
    if scenario == "happy":
        plan = s.telemetry_plan(s.parse_args(["codex"]))
        assert plan["level"] == "off" and plan["origin"] == "default" and "analytics.enabled" in plan["codex_config"]
    elif scenario == "sad":
        a = s.parse_args(["claude"])
        a.mode = "paranoid"
        assert s.telemetry_plan(a)["origin"] == "--mode paranoid"
    else:
        plan = s.telemetry_plan(s.parse_args(["claude", "--telemetry", "on"]))
        assert plan == {"level": "on", "origin": "--telemetry", "environment": [], "opencode_config": None,
                        "codex_config": []}


def test_telemetry_rules(scenario):
    if scenario == "happy":
        rules = s.telemetry_rules(s.parse_args(["claude", "--with", "codex"]))
        assert [r["host"] for r in rules["http"]] == ["api.anthropic.com", "chatgpt.com"]
    elif scenario == "sad":
        assert s.telemetry_rules(s.parse_args(["claude", "--no-proxy"])) is None
        assert s.telemetry_rules(s.parse_args(["claude", "--telemetry", "on"])) is None
    else:  # a host passed unopened gets no rule; OpenCode has none at all
        assert s.telemetry_rules(s.parse_args(["claude", "--no-inspect", "api.anthropic.com"])) is None
        assert s.telemetry_rules(s.parse_args(["opencode"])) is None


def test_codex_telemetry_keys(scenario):
    if scenario == "happy":
        assert [k for _, k, _, _ in s.codex_telemetry_keys("off")] == [
            "check_for_update_on_startup", "enabled", "enabled", "metrics_exporter"]
    elif scenario == "sad":
        assert s.codex_telemetry_keys("on") == []
    else:
        assert s.codex_telemetry_keys("strict")[-1] == ("features", "plugins", False, "false")


def test_codex_config_with(scenario):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9 and 3.10
        import tomli as tomllib
    keys = s.codex_telemetry_keys("off")
    if scenario == "happy":
        text, kept = s.codex_config_with('model = "x"\n', keys, tomllib)
        assert tomllib.loads(text)["analytics"] == {"enabled": False} and kept == []
    elif scenario == "sad":
        with pytest.raises(ValueError):
            s.codex_config_with("analytics = { a = 1 }\n", keys, tomllib)
        with pytest.raises(ValueError):
            s.codex_config_with("not = [toml\n", keys, tomllib)
    else:
        text, kept = s.codex_config_with("[feedback]\nenabled = true\n", keys, tomllib)
        assert ("feedback.enabled", True) in kept and tomllib.loads(text)["feedback"] == {"enabled": True}


def test_codex_telemetry_defaults(scenario, tmp_path, capsys):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9 and 3.10
        import tomli as tomllib
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config = home / ".codex/config.toml"
    if scenario == "happy":
        s.codex_telemetry_defaults(home, "off")
        assert tomllib.loads(config.read_text())["otel"] == {"metrics_exporter": "none"}
        assert oct(config.stat().st_mode & 0o777) == "0o600"
    elif scenario == "sad":
        config.write_text("analytics = { a = 1 }\n")
        s.codex_telemetry_defaults(home, "off")
        assert config.read_text() == "analytics = { a = 1 }\n" and "left as its" in capsys.readouterr().err
    else:
        s.codex_telemetry_defaults(home, "strict")
        first = config.read_text()
        s.codex_telemetry_defaults(home, "strict")
        assert config.read_text() == first and tomllib.loads(first)["features"] == {"plugins": False}



def test_web_service_port(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    listen = tmp_path / "sandbox-ai/web/listen.json"
    if scenario == "happy":  # a running service: its port is no host port a sandbox gets
        listen.parent.mkdir(parents=True)
        listen.write_text(json.dumps({"address": "127.0.0.1", "port": 8443, "pid": os.getpid()}))
        assert s.web_service_port() == 8443
        with pytest.raises(ValueError, match="the web interface .* listens there"):
            s.host_ports(s.parse_args(["claude", "--allow-host-port", "8443"]))
        assert s.host_ports(s.parse_args(["claude", "--allow-host-port", "8080"])) == [8080]
    elif scenario == "sad":  # a service that ended: its port is free again
        listen.parent.mkdir(parents=True)
        listen.write_text(json.dumps({"port": 8443, "pid": 2 ** 22 + 7}))
        assert s.web_service_port() is None
    else:  # none, or a damaged record
        assert s.web_service_port() is None
        listen.parent.mkdir(parents=True)
        listen.write_text("{")
        assert s.web_service_port() is None


# --- memory watch (MemoryWatch: tests/test_memory_watch.py runs it against real processes) -------------------------

def fake_cgroup(root, relative, limit="8589934592", current="4294967296"):
    group = root / relative
    group.mkdir(parents=True)
    if limit is not None:
        (group / "memory.max").write_text(limit + "\n")
    (group / "memory.current").write_text(current + "\n")
    (group / "memory.stat").write_text("anon 3000\nfile 10\nshmem 500\nweird line\n")
    (group / "memory.events").write_text("low 0\noom 1\noom_kill 2\n")
    return group


def test_cgroup_dir(scenario, tmp_path):
    proc, root = tmp_path / "proc", tmp_path / "cg"
    (proc / "42").mkdir(parents=True)
    group = fake_cgroup(root, "user.slice/libpod-abc.scope")
    if scenario == "happy":
        (proc / "42" / "cgroup").write_text("0::/user.slice/libpod-abc.scope\n")
        assert s.cgroup_dir(42, proc, root) == group
    elif scenario == "sad":
        (proc / "42" / "cgroup").write_text("0::/user.slice/other\n")  # no limit there nor above
        fake_cgroup(root, "user.slice/other", limit="max")
        assert s.cgroup_dir(42, proc, root) is None
        assert s.cgroup_dir(43, proc, root) is None  # gone
        (proc / "42" / "cgroup").write_text("1:name=systemd:/x\n")  # cgroup v1 only
        assert s.cgroup_dir(42, proc, root) is None
    else:
        # The process sits in a leaf without a limit: its parent carries it (podman's split cgroups).
        fake_cgroup(root, "user.slice/libpod-abc.scope/container", limit=None)
        (proc / "42" / "cgroup").write_text("0::/user.slice/libpod-abc.scope/container\n")
        assert s.cgroup_dir(42, proc, root) == group


def test_memory_stall(scenario, tmp_path):
    if scenario == "happy":
        (tmp_path / "memory.pressure").write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=9\n"
                                                  "full avg10=4.25 avg60=1.00 avg300=0.20 total=4\n")
        assert s.memory_stall(tmp_path) == 4.25  # all processes stalled, not just some
    elif scenario == "sad":
        assert s.memory_stall(tmp_path) is None  # no PSI (an older kernel, psi=0)
    else:
        (tmp_path / "memory.pressure").write_text("some avg10=1.00 avg60=0 avg300=0 total=1\nfull avg10=x\n")
        assert s.memory_stall(tmp_path) is None


def test_memory_state(scenario, tmp_path):
    group = fake_cgroup(tmp_path, "g")
    if scenario == "happy":
        assert s.memory_state(group) == {"current": 4294967296, "limit": 8589934592, "oom_kill": 2, "anon": 3000,
                                         "shmem": 500}
    elif scenario == "sad":
        (group / "memory.max").write_text("max\n")  # no limit: not a cgroup the watch takes
        with pytest.raises(ValueError):
            s.memory_state(group)
    else:
        (group / "memory.events").write_text("")
        (group / "memory.stat").write_text("")
        assert s.memory_state(group)["oom_kill"] == 0 and s.memory_state(group)["shmem"] == 0


def test_cgroup_processes(scenario, tmp_path):
    group = tmp_path / "g"
    (group / "leaf").mkdir(parents=True)
    if scenario == "happy":
        (group / "cgroup.procs").write_text("1\n2\n")
        (group / "leaf" / "cgroup.procs").write_text("3\n")
        assert sorted(s.cgroup_processes(group)) == [1, 2, 3]
    elif scenario == "sad":
        assert s.cgroup_processes(tmp_path / "absent") == []
    else:
        (group / "cgroup.procs").write_text("1\nnot-a-pid\n\n")
        assert s.cgroup_processes(group) == [1]


def test_memory_victim(scenario, tmp_path):
    proc = tmp_path / "proc"

    def process(pid, name, rss_kib, *argv):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "status").write_text(f"Name:\t{name}\nVmRSS:\t{rss_kib} kB\n")
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    process(1, "kthreadd", 900000, "python3", "-I", "/sandbox/runtime.py", "--internal-bootstrap")
    process(2, "claude", 800000, "claude", "--continue")
    process(3, "node", 700000, "/usr/bin/node", "/usr/local/bin/codex")
    process(4, "python3", 600000, "python3", "-I", "/sandbox/runtime.py", "--internal-proxy", "3", "4")
    process(5, "pytest", 500000, "python3", "-m", "pytest")
    process(6, "cc1", 100000, "cc1", "x.c")
    process(7, "firejail", 990000, "firejail", "--noprofile")
    if scenario == "happy":
        assert s.memory_victim([1, 2, 3, 4, 5, 6, 7], proc) == (5, "pytest", 500000 * 1024)
    elif scenario == "sad":
        assert s.memory_victim([1, 2, 3, 4, 7], proc) is None  # only sandbox-ai's own and the agents
        assert s.memory_victim([99], proc) is None  # gone
    else:
        assert s.memory_victim([5, 6], proc, protected={5}) == (6, "cc1", 100000 * 1024)


def test_gib(scenario):
    assert s.gib({"happy": 1 << 30, "sad": 0, "edge": 3 << 29}[scenario]) == {"happy": "1.0 GiB", "sad": "0.0 GiB",
                                                                             "edge": "1.5 GiB"}[scenario]


def test_podman_agent_pid(scenario, monkeypatch):
    a = s.parse_args(["claude"])
    answers = {"happy": ["abc false\ninfra1 true\n", "4242\n"], "sad": ["infra1 true\n"], "edge": ["abc false\n", "0\n"]}
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return Mock(stdout=answers[scenario][len(calls) - 1])
    monkeypatch.setattr(s.subprocess, "run", run)
    assert s.podman_agent_pid(a, "agent-pod") == {"happy": 4242, "sad": None, "edge": None}[scenario]
    assert calls[0][-4:] == ["--filter", "pod=agent-pod", "--format", "{{.ID}} {{.IsInfra}}"]
    if scenario == "happy":
        assert calls[1][-3:] == ["{{.State.Pid}}", "abc"] or calls[1][-2:] == ["{{.State.Pid}}", "abc"]


def test_start_memory_watch(scenario, monkeypatch, tmp_path):
    a = s.parse_args(["claude", *(["--memory", "none"] if scenario == "sad" else [])])
    monkeypatch.setattr(s, "enforced_limits", lambda a: (s.resource_limits(a), []))
    started = []
    monkeypatch.setattr(s.MemoryWatch, "start", lambda self: started.append(self) or self)
    if scenario == "edge":
        a.podman_url = "unix:///run/podman.sock"  # a remote engine: its cgroups are not this host's
    watch = s.start_memory_watch(a, tmp_path)
    if scenario == "happy":
        assert watch is started[0] and watch.session == tmp_path and watch.log is None
    else:
        assert watch is None and started == []


def test_tmpfs_size(scenario, monkeypatch):
    """/tmp holds at most half the memory limit: a full /tmp is an error the agent sees, not an OOM of the run."""
    if scenario == "happy":
        assert s.tmpfs_size({"memory": "8g"}) == f",size={4 << 30}"
        a = s.parse_args(["claude", "--memory", "16g"])
        monkeypatch.setattr(s, "enforced_limits", lambda a: (s.resource_limits(a), []))
        run = s.pod_commands(a, "agent-x", Path("/tmp/s"), Path("/tmp/h"))[1]
        assert f"--tmpfs=/tmp:rw,nosuid,nodev,size={8 << 30}" in run and "--memory" in run
        assert f"--tmpfs=/var/tmp:rw,nosuid,nodev,size={8 << 30}" in run and "--tmpfs=/run:rw,nosuid,nodev" in run
    elif scenario == "sad":
        assert s.tmpfs_size({}) == ""  # --memory none: no limit to keep to
    else:
        assert s.tmpfs_size({"memory": "100m"}) == f",size={64 << 20}"  # never below 64 MiB
        assert s.tmpfs_size({"memory": "1073741824"}) == f",size={1 << 29}"  # plain bytes


def test_load_presets_module(scenario, monkeypatch, tmp_path):
    """presets.py, loaded once; a launch with --preset is expanded by it (test_presets.py, test_feature_matrix.py)."""
    monkeypatch.setattr(s, "_presets_module", None)
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_presets_module()
        return
    module = s.load_presets_module()
    assert module.FORMAT == "sandbox-ai preset" and module.apply(["claude", "--gui"]) == ["claude", "--gui"]
    if scenario == "edge":
        assert s.load_presets_module() is module  # once per process


def test_load_git_logins_module(scenario, monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_git_logins_module", None)
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_git_logins_module()
        return
    module = s.load_git_logins_module()
    assert callable(module.credentials) and module.path().name == "git-logins.json"
    if scenario == "edge":
        assert s.load_git_logins_module() is module


def git_logins_home(monkeypatch, tmp_path, logins):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(s, "_git_logins_module", None)
    store = {"GITEA": GIT_TOKEN}
    fake = Mock()
    fake.get.side_effect = store.get
    monkeypatch.setattr(s, "load_secret_store_module", lambda: fake)
    module = s.load_git_logins_module()
    monkeypatch.setattr(module, "launcher", lambda: s)
    if logins:
        module.save(logins)


def test_registry_git_tokens(scenario, monkeypatch, tmp_path):
    """The run's logins from sandbox-ai git-logins: only with the proxy, --git-logins on, and not for a server a
    start option names; read once per start."""
    git_logins_home(monkeypatch, tmp_path, [{"url": "http://localhost:3080", "user": "ch", "secret": "GITEA"},
                                            {"url": "https://git.example.org", "user": "u", "secret": "MISSING"}])
    if scenario == "happy":
        a = s.parse_args(["claude"])
        pairs, notes = s.registry_git_tokens(a)
        assert [(c["url"], c["user"], c["loopback"], t) for c, t in pairs] == [("http://localhost:3080", "ch", True, GIT_TOKEN)]
        assert len(notes) == 1 and "no usable secret MISSING" in notes[0]
        assert s.registry_git_tokens(a) is s.registry_git_tokens(a)  # cached for the start
    elif scenario == "sad":
        for argv in (["claude", "--git-logins", "off"], ["claude", "--no-proxy"]):
            assert s.registry_git_tokens(s.parse_args(argv)) == ([], [])
        a = s.parse_args(["claude"])
        a.mcp_socket_dir = tmp_path  # an MCP server's sandbox: never your logins
        assert s.registry_git_tokens(a) == ([], [])
    else:
        monkeypatch.setenv("OWN", GIT_TOKEN)
        a = s.parse_args(["claude", "--git-token-env", "http://localhost:3080=OWN"])
        assert s.registry_git_tokens(a)[0] == []  # the start's option wins for its server


def test_proxied_git_tokens(scenario, monkeypatch, tmp_path):
    git_logins_home(monkeypatch, tmp_path, [{"url": "http://localhost:3080", "user": "ch", "secret": "GITEA"}]
                    if scenario != "sad" else [])
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    argv = {"happy": ["claude", "--token-proxy", "--git-token-env", "https://git.example.org=CONTRACT_GIT"],
            "sad": ["claude"], "edge": ["claude", "--git-token-env", "https://git.example.org=CONTRACT_GIT"]}[scenario]
    found = [c["url"] for c, _ in s.proxied_git_tokens(s.parse_args(argv))]
    assert found == {"happy": ["https://git.example.org", "http://localhost:3080"], "sad": [],
                     # without --token-proxy the start's token is the agent's (git_environment), the login the proxy's
                     "edge": ["http://localhost:3080"]}[scenario]


def test_local_engine(scenario, monkeypatch):
    """The engine's cgroups, routes and devices are this host's only on Linux without --podman-url."""
    monkeypatch.setattr(s.sys, "platform", {"happy": "linux", "sad": "darwin", "edge": "linux"}[scenario])
    a = s.parse_args(["claude", *(["--podman-url", "ssh://core@vm/run/podman.sock"] if scenario == "edge" else [])])
    assert s.local_engine(a) is (scenario == "happy")


def test_check_platform(scenario, monkeypatch):
    """macOS: Podman (podman machine) only; this host's devices and Wayland are refused with what works instead."""
    monkeypatch.setattr(s.sys, "platform", "linux" if scenario == "edge" else "darwin")
    if scenario == "happy":
        s.check_platform(s.parse_args(["claude", "--backend", "podman", "--gui", "--gpu", "nvidia"]))
    elif scenario == "sad":
        for argv, text in ((["--backend", "bwrap"], "needs Linux"), (["--device", "/dev/ttyUSB0"], "Linux VM"),
                           (["--gpu", "dri"], "Linux VM"), (["--gui-wayland"], "Wayland session")):
            with pytest.raises(ValueError, match=text):
                s.check_platform(s.parse_args(["claude", *argv]))
    else:  # Linux: nothing of it is refused here (the other checks decide)
        s.check_platform(s.parse_args(["claude", "--backend", "bwrap", "--device", "/dev/ttyUSB0", "--gui-wayland"]))


def test_limits_on_macos_are_left_to_podman_machine(monkeypatch):
    """On macOS the systemd and cgroup checks of this host do not apply: Podman's VM enforces the limits."""
    monkeypatch.setattr(s.sys, "platform", "darwin")
    monkeypatch.setattr(s, "user_manager_running", lambda: (_ for _ in ()).throw(AssertionError("not on macOS")))
    monkeypatch.setattr(s, "delegated_controllers", lambda path=None: (_ for _ in ()).throw(AssertionError("no")))
    limits, notes = s.enforced_limits(s.parse_args(["claude", "--memory", "4g", "--pids", "512"]))
    assert limits["memory"] == "4g" and limits["pids"] == 512 and notes == []
    assert s.start_memory_watch(s.parse_args(["claude"]), None) is None  # the VM's cgroups are not visible here


MACHINE = {"Name": "podman-machine-default", "State": "running",
           "Mounts": [{"Source": "/Users"}, {"Source": "/private"}, {"Source": "/var/folders"}],
           "Resources": {"CPUs": 6, "Memory": 12288}}


def test_podman_machine(scenario):
    answers = {"happy": json.dumps([MACHINE]), "sad": "not json", "edge": "[]"}
    found = s.podman_machine(lambda *a, **k: Mock(stdout=answers[scenario]))
    assert found == (MACHINE if scenario == "happy" else {})


def test_machine_paths(scenario, tmp_path):
    (tmp_path / "m").mkdir()
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), *(["--mount", f"{tmp_path}/m:/mnt/m:ro"]
                                                                       if scenario != "sad" else [])])
    if scenario == "edge":
        a.state_dir = tmp_path / "state"
    whats = [what for what, _ in s.machine_paths(a)]
    assert whats[0] == "the workspace" and "sandbox-ai's files" in whats and "$TMPDIR (the run's session)" in whats
    assert ("--mount" in whats) is (scenario != "sad") and ("--state-dir" in whats) is (scenario == "edge")


def test_check_machine_shares(scenario, monkeypatch, tmp_path):
    monkeypatch.setattr(s.sys, "platform", "darwin" if scenario != "edge" else "linux")
    monkeypatch.setattr(s, "machine_paths", lambda a: [("the workspace", Path("/Users/ada/src/app")),
                                                       ("sandbox-ai's files", Path("/Users/ada/.local/share/x")),
                                                       ("$TMPDIR (the run's session)", Path("/var/folders/ab/T"))]
                        + ([("--mount", Path("/Volumes/Data/samples"))] if scenario == "sad" else []))
    a = s.parse_args(["claude", "--backend", "podman"])
    if scenario == "sad":
        with pytest.raises(ValueError, match=r"--mount /Volumes/Data/samples is not shared with podman machine .*"
                                             r"podman machine set -v /Volumes/Data/samples:/Volumes/Data/samples"):
            s.check_machine_shares(a, MACHINE)
    else:
        s.check_machine_shares(a, MACHINE)  # inside /Users and /var/folders; on Linux nothing to check
        s.check_machine_shares(a, {})  # no machine: doctor and podman say so


def test_engine_cpus(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(s, "podman_machine", lambda runner=None: {"Resources": {"CPUs": 6}} if scenario != "sad" else {})
    monkeypatch.setattr(s.sys, "platform", "linux" if scenario == "edge" else "darwin")
    assert s.engine_cpus(s.parse_args(["claude"])) == {"happy": 6, "sad": 16, "edge": 16}[scenario]
    if scenario == "happy":
        assert s.resource_limits(s.parse_args(["claude"]))["cpus"] == "3"  # half the VM's, not the Mac's
        with pytest.raises(ValueError, match="from 0.01 to 6 \\(the engine's CPUs\\)"):
            s.resource_limits(s.parse_args(["claude", "--cpus", "8"]))


def test_tmux_is_off_by_default_on_macos_until_attaching_reaches_the_vm(monkeypatch):
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys, "platform", "darwin")
    assert s.resolve_tmux(s.parse_args(["claude"])) is False
    assert s.resolve_tmux(s.parse_args(["claude", "--tmux"])) is True  # asked for: still possible
    monkeypatch.setattr(s.sys, "platform", "linux")
    assert s.resolve_tmux(s.parse_args(["claude"])) is True


def test_audio_command(scenario):
    """PulseAudio as a user of its own, without capabilities, loading nothing a client asks for."""
    command = rt.audio_command(Path({"happy": "/tmp/sandbox-audio/native", "sad": "/x", "edge": "/a b/native"}[scenario]))
    program = command[command.index("--") + 1:]
    assert command[0] == "setpriv" and f"--reuid={rt.AUDIO_UID}" in command and "--no-new-privs" in command
    assert program[0] == "pulseaudio" and "--disallow-module-loading" in program and "--use-pid-file=no" in program
    assert any(arg.startswith("--load=module-native-protocol-unix socket=") for arg in program)


def test_start_audio(scenario, tmp_path, capsys):
    """No PulseAudio in the image: said, nothing started (test_audio.py starts a stand-in for real)."""
    if scenario == "happy":
        assert rt.start_audio({}, which=lambda name: None, directory=tmp_path) == []
        assert "images recipe media" in capsys.readouterr().err
    elif scenario == "sad":
        def popen(command, **kwargs):
            return subprocess.Popen([sys.executable, "-c", "pass"], stderr=kwargs["stderr"])
        assert rt.start_audio({}, which=lambda name: "x", popen=popen, directory=tmp_path, uid=os.getuid()) == []
        assert "pulseaudio exited" in capsys.readouterr().err
    else:
        assert rt.AUDIO_SOCKET.parent == rt.AUDIO_DIR and rt.AUDIO_UID not in (0, rt.PROXY_UID)


def test_host_ports_go_through_the_proxy_over_ipv6_too():
    """pasta forwards [::1]:PORT as well as 127.0.0.1:PORT: a push to http://[::1]:3000 meets the proxy (and its
    privacy check) as one to 127.0.0.1:3080 does; other ::1 ports stay the sandbox's own."""
    rules = s.firewall_rules([], [], proxy={"port": s.PROXY_PORT, "loopback": [3000]})
    assert "  ip daddr 127.0.0.0/8 tcp dport != { 3000 } return" in rules
    assert "  ip6 daddr ::1 tcp dport != { 3000 } return" in rules
    assert "  ip6 daddr ::1 return" not in rules
    assert "  ip6 daddr ::1 return" in s.firewall_rules([], [], proxy={"port": s.PROXY_PORT, "loopback": []})


def test_hold_unserved_loopback(scenario):
    """[::1]:PORT is taken where pasta does not serve it, so the agent cannot listen there and get a login."""
    import socket as _socket
    try:
        probe = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
        probe.bind(("::1", 0))
        port = probe.getsockname()[1]
        probe.close()
    except OSError:
        pytest.skip("no IPv6 loopback here")
    if scenario == "happy":  # nobody serves it: held, and the agent's bind fails
        held = rt.hold_unserved_loopback([port])
        try:
            assert len(held) == 1
            agent = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
            agent.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 1)
            with pytest.raises(OSError):
                agent.bind(("::1", port))
            agent.close()
            with pytest.raises(OSError):
                _socket.create_connection(("::1", port), timeout=2)  # refused: nothing listens
        finally:
            for sock in held:
                sock.close()
    elif scenario == "sad":  # served (pasta's listener): left alone
        server = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
        server.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 1)
        server.bind(("::1", port))
        server.listen()
        try:
            assert rt.hold_unserved_loopback([port]) == []
        finally:
            server.close()
    else:
        assert rt.hold_unserved_loopback([]) == []


def test_check_model_effort(scenario):
    if scenario == "happy":
        for argv in (["claude", "--model", "opus", "--effort", "max"], ["codex", "--model", "gpt-5-codex", "--effort",
                     "minimal"], ["opencode", "--model", "anthropic/claude-sonnet-5-5"], ["claude"]):
            s.check_model_effort(s.parse_args(argv))
    elif scenario == "sad":
        for argv, text in ((["claude", "--effort", "minimal"], "one of low, medium, high, xhigh, max"),
                           (["codex", "--effort", "max"], "one of minimal, low, medium, high, xhigh"),
                           (["opencode", "--effort", "high"], "sets its effort per model"),
                           (["claude", "--model", "opus --dangerously-skip-permissions"], "expects a model name"),
                           (["claude", "--model", "opus", "--", "--model", "sonnet"], "given twice"),
                           (["claude", "--effort", "high", "--", "--effort=low"], "given twice")):
            with pytest.raises(ValueError, match=text):
                s.check_model_effort(s.parse_args(argv))
    else:
        a = s.parse_args(["claude", "--model", "opus"])
        a.llm = "gpu"
        with pytest.raises(ValueError, match="--llm NAME:MODEL"):
            s.check_model_effort(a)
        s.check_model_effort(s.parse_args(["claude", "--", "--model", "sonnet"]))  # the agent's own, alone: fine


def test_model_effort_args(scenario):
    if scenario == "happy":
        assert s.model_effort_args(s.parse_args(["claude", "--model", "fable", "--effort", "xhigh"])) == [
            "--model", "fable", "--effort", "xhigh"]
        assert s.model_effort_args(s.parse_args(["codex", "--model", "gpt-5-codex", "--effort", "high"])) == [
            "--model", "gpt-5-codex", "-c", "model_reasoning_effort=high"]
    elif scenario == "sad":
        assert s.model_effort_args(s.parse_args(["opencode", "--model", "ollama/qwen3"])) == ["--model", "ollama/qwen3"]
    else:
        assert s.model_effort_args(s.parse_args(["claude"])) == []


def test_a_refresh_written_while_the_login_is_synced_is_not_missed(tmp_path, monkeypatch):
    """Regression (seen as a flaky test under load): the watcher took the files' signature after a sync, so a
    refresh the agent wrote while that sync ran was counted as seen and never written back; with refresh-token
    reuse detection the next start then used a dead token."""
    home, pair, state, soon = sync_fixture(tmp_path, monkeypatch)
    host, sandbox = pair
    real, raced = s.claude_login_sync, []

    def racing(*args):
        result = real(*args)
        if not raced:  # the agent refreshes just as this first sync finishes
            raced.append(1)
            claude_credentials(sandbox, soon + 1000, refresh="rotated-during-sync")
        return result
    monkeypatch.setattr(s, "claude_login_sync", racing)
    stop = threading.Event()
    watcher = threading.Thread(target=s.watch_claude_login, args=(home, pair, state, stop, 0.01))
    watcher.start()
    try:
        deadline = time.monotonic() + 10
        while s.claude_oauth(host)["refreshToken"] != "rotated-during-sync" and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        watcher.join(10)
    assert raced and s.claude_oauth(host)["refreshToken"] == "rotated-during-sync"


def package_fixture(tmp_path, monkeypatch):
    """A ready-made package (a .pyz) and the copy it was unpacked to, with this launcher inside the copy."""
    package = tmp_path / "dl" / "sandbox-ai.pyz"
    package.parent.mkdir()
    package.write_bytes(b"#!/bin/sh\n")
    copy = tmp_path / "cache" / "sandbox-ai" / "pyz" / "1.0-abc"
    (copy / "sandbox").mkdir(parents=True)
    monkeypatch.setattr(s, "HERE", copy / "sandbox")
    monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(package))
    monkeypatch.setenv("SANDBOX_AI_PACKAGE_COPY", str(copy))
    return package, copy


def test_package_paths(scenario, tmp_path, monkeypatch):
    """Review finding: the .pyz or AppImage a launcher runs from (and its copy) come from the boot's variables,
    each only for what it claims."""
    package, copy = package_fixture(tmp_path, monkeypatch)
    if scenario == "happy":
        assert [path for path, _ in s.package_paths()] == [copy, package]
    elif scenario == "sad":  # a copy that does not hold this launcher (an inherited variable): nothing counts
        monkeypatch.setattr(s, "HERE", tmp_path / "installed" / "sandbox")
        assert s.package_paths() == []
        monkeypatch.delenv("SANDBOX_AI_PACKAGE_COPY")
        assert s.package_paths() == []
    else:  # relative, missing or not a file/folder: ignored, the copy still counts
        for value in ("sandbox-ai.pyz", str(tmp_path / "missing.pyz"), "/dev/null"):
            monkeypatch.setenv("SANDBOX_AI_PACKAGE", value)
            assert [path for path, _ in s.package_paths()] == [copy]


def test_check_package_location(scenario, tmp_path, monkeypatch):
    """Review finding (HIGH): a workspace, rw mount or --state-dir holding the package lets the agent rewrite it."""
    package, copy = package_fixture(tmp_path, monkeypatch)
    if scenario == "happy":
        s.check_package_location(tmp_path / "project", "Workspace")
        s.check_package_location(copy.parent.parent / "other", "--mount")
    elif scenario == "sad":
        with pytest.raises(ValueError, match=r"Workspace .* contains .*sandbox-ai.pyz.*~/.local/bin"):
            s.check_package_location(package.parent, "Workspace")
        with pytest.raises(ValueError, match="--mount"):
            s.check_package_location(package, "--mount")
        with pytest.raises(ValueError, match="--state-dir .* contains"):
            s.check_package_location(copy.parent, "--state-dir")
    else:  # inside the copy, too; nothing set: nothing refused
        with pytest.raises(ValueError, match="lies in"):
            s.check_package_location(copy / "sandbox", "Workspace")
        monkeypatch.delenv("SANDBOX_AI_PACKAGE")
        monkeypatch.delenv("SANDBOX_AI_PACKAGE_COPY")
        s.check_package_location(package.parent, "Workspace")


# --- security review round 4: every way a host path enters a sandbox ------------------------------------------


def short_dir(prefix):
    """A folder short enough for Unix socket paths (107 bytes)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def listening_socket(path):
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    return listener


def test_login_locations(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    found = dict(s.login_locations())
    if scenario == "happy":
        assert "Claude Code" in found[tmp_path / ".claude"] and "Codex" in found[tmp_path / ".codex"]
        assert "OpenCode" in found[tmp_path / ".local/share/opencode"] and tmp_path / ".aws" in found
    elif scenario == "sad":  # a relative variable is no folder
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/dir")
        assert all("CLAUDE_CONFIG_DIR" not in label for label in dict(s.login_locations()).values())
    else:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
        assert "CODEX_HOME" in dict(s.login_locations())[tmp_path / "elsewhere"]


def test_own_folders(scenario, tmp_path, monkeypatch):
    """Security finding M2: only sandbox-ai's own internal start of a folder gets it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    state, data = tmp_path / "state/sandbox-ai", tmp_path / "data/sandbox-ai"
    if scenario == "happy":
        # The plugin installer's own start: the store and its work folder beside it.
        a = s.parse_args(["claude", "--no-plugins", "--state-dir", str(data / "plugins/claude"),
                          "--workspace", str(data / "plugins/.work-abc")])
        assert s.own_folders(a) == [data / "plugins/claude", data / "plugins/.work-abc"]
        # The wizard's persistent HOME of this project (its digest), and a session's own HOME.
        project = tmp_path / "app"
        digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
        a = s.parse_args(["codex", "--state-dir", str(state / f"home/app-{digest}"), "--workspace", str(project)])
        assert s.own_folders(a) == [state / f"home/app-{digest}"]
        monkeypatch.setattr(s, "kept_start_dir", lambda a: state / "sessions/web")
        assert state / "sessions/web/home" in s.own_folders(s.parse_args(["codex"]))
    elif scenario == "sad":
        # The same folders for a start that is not theirs: none.
        assert s.own_folders(s.parse_args(["claude", "--state-dir", str(data / "plugins/claude")])) == []
        assert s.own_folders(s.parse_args(["codex", "--state-dir", str(state / "home/app-000000000000"),
                                           "--workspace", str(tmp_path / "app")])) == []
        assert s.own_folders(s.parse_args(["codex", "--mcp-socket-dir", str(tmp_path / "elsewhere/sock")])) == []
    else:  # an MCP server: home/ and work/ beside its socket folder; a team's board for team-NAME
        a = s.parse_args(["codex", "--mcp-socket-dir", str(state / "mcp/team-app/sock")])
        assert s.own_folders(a) == [state / "mcp/team-app/home", state / "mcp/team-app/work",
                                    state / "teams/app/board"]


def test_launcher_trees(scenario, monkeypatch, tmp_path):
    if scenario == "happy":  # run from this checkout: the whole tree (install.sh beside sandbox/)
        assert s.launcher_trees() == [s.HERE, s.HERE.parent]
    elif scenario == "sad":  # a sandbox/ folder elsewhere (a Python package): itself only
        (tmp_path / "sandbox").mkdir()
        monkeypatch.setattr(s, "HERE", tmp_path / "sandbox")
        assert s.launcher_trees() == [tmp_path / "sandbox"]
    else:
        (tmp_path / "sandbox").mkdir()
        (tmp_path / "install.sh").write_text("")
        (tmp_path / "sandbox/agent_sandbox.py").write_text("")
        monkeypatch.setattr(s, "HERE", tmp_path / "sandbox")
        assert s.launcher_trees() == [tmp_path / "sandbox", tmp_path]


def test_in_launcher_tree(scenario):
    if scenario == "happy":
        assert s.in_launcher_tree(s.HERE / "agent_mcp.py") and s.in_launcher_tree(s.HERE.parent / "install.sh")
    elif scenario == "sad":
        assert not s.in_launcher_tree(Path("/usr/bin"))
    else:  # a folder holding it is not in it
        assert not s.in_launcher_tree(s.HERE.parent.parent)


def test_live_socket(scenario):
    directory = short_dir("ls-")
    try:
        if scenario == "happy":
            with listening_socket(directory / "s"):
                assert s.live_socket(directory / "s")
        elif scenario == "sad":  # nothing listens any longer: a stale file
            listening_socket(directory / "s").close()
            assert not s.live_socket(directory / "s")
            assert not s.live_socket(directory / "gone")
        else:  # a datagram socket answers no connect: someone is there
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as datagram:
                datagram.bind(str(directory / "d"))
                assert s.live_socket(directory / "d")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_sockets_below(scenario):
    directory = short_dir("sb-")
    try:
        (directory / "a/b").mkdir(parents=True)
        if scenario == "happy":
            assert s.sockets_below(directory) == (None, True)
            with listening_socket(directory / "a/b/S.gpg-agent"):
                assert s.sockets_below(directory) == (directory / "a/b/S.gpg-agent", True)
                assert s.sockets_below(directory / "a/b/S.gpg-agent")[0] is not None  # the socket itself
        elif scenario == "sad":  # links are not followed, stale sockets are no way anywhere
            listening_socket(directory / "stale").close()
            (directory / "link").symlink_to("/run")
            assert s.sockets_below(directory) == (None, True)
        else:  # past its budget: not looked at in full
            for index in range(5):
                (directory / f"f{index}").write_text("")
            assert s.sockets_below(directory, entries=3) == (None, False)
            assert s.sockets_below(directory, depth=1) == (None, False)
            assert s.sockets_below(directory / "missing") == (None, True)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_check_sockets_below(scenario, monkeypatch):
    """Finding H6: a tree with a live socket in it (gpg-agent's, an SSH ControlMaster's ~/.ssh/cm-*) is refused."""
    directory = short_dir("csb-")
    try:
        if scenario == "happy":
            assert s.check_sockets_below(directory, "--mount") is None
        elif scenario == "sad":
            with listening_socket(directory / "cm-host"):
                with pytest.raises(ValueError, match="holds the socket .*cm-host"):
                    s.check_sockets_below(directory, "--mount")
        else:
            monkeypatch.setattr(s, "SOCKET_SCAN_ENTRIES", 1)
            (directory / "x").write_text("")
            (directory / "y").write_text("")
            said = []
            assert "could not be ruled out" in s.check_sockets_below(directory, "Workspace", said.append)
            assert said
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_check_launcher_location(scenario, tmp_path):
    """Review 5 M2: what a sandbox gets writable may neither lie in nor hold the launcher (a checkout, a venv)."""
    tree = tmp_path / "c0de/sandbox-ai"
    if scenario == "happy":
        s.check_launcher_location(tmp_path / "c0de/other", "--mount", [tree])
        s.check_launcher_location(tmp_path / "elsewhere", "--state-dir", [tree])
    elif scenario == "sad":
        for path, word in ((tmp_path / "c0de", "contains"), (tree / "sandbox", "lies in"), (tree, "is")):
            with pytest.raises(ValueError, match=f"--mount {re.escape(str(path))} {word} sandbox-ai's own launcher"
                                                 ".*read-only"):
                s.check_launcher_location(path, "--mount", [tree])
    else:  # the real launcher trees by default; a sibling whose name only begins like it is fine
        with pytest.raises(ValueError, match="own launcher"):
            s.check_launcher_location(s.HERE.parent, "--state-dir")
        s.check_launcher_location(tmp_path / "c0de/sandbox-ai-other", "--mount", [tree])


def test_writable_mounts_and_state_dirs_may_not_hold_the_launcher(tmp_path, monkeypatch):
    """Review 5 M2 (PoC mount_launcher_parent.py): --mount PARENT_OF_CHECKOUT:rw and --state-dir were accepted;
    read-only stays allowed. The workspace check and check_given_path use the same helper."""
    parent = tmp_path / "c0de"
    checkout = parent / "sandbox-ai"
    (checkout / "sandbox").mkdir(parents=True)
    monkeypatch.setattr(s, "launcher_trees", lambda: [checkout / "sandbox", checkout])
    with pytest.raises(ValueError, match="contains sandbox-ai's own launcher"):
        s.check_given_path(parent, "--mount", True)
    with pytest.raises(ValueError, match="--state-dir .* contains sandbox-ai's own launcher"):
        s.check_given_path(parent, "--state-dir", True)
    with pytest.raises(ValueError, match="Workspace .* contains sandbox-ai's own launcher"):
        s.check_workspace_scope(parent)
    try:
        s.check_given_path(parent, "--mount", False, warn=lambda line: None)
    except ValueError as exc:  # read-only: never for the launcher (another check may still object to the place)
        assert "launcher" not in str(exc)


def test_check_given_path(scenario, tmp_path, monkeypatch):
    """Findings H1/M1: read-only too, a host path never reaches sandbox-ai's own folders; private ones warn."""
    home = tmp_path / "home"
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    if scenario == "happy":
        said = []
        s.check_given_path(tmp_path / "project", "--mount", False, warn=said.append)
        assert said == []
        s.check_given_path(home / ".ssh", "--mount", False, warn=said.append)
        assert said and ".ssh" in said[0]
        # The launcher's own files read-only (an MCP helper's script): given as they are, without a warning.
        s.check_given_path(s.HERE / "agent_mcp.py", "--mount", False, warn=said.append)
        assert len(said) == 1
    elif scenario == "sad":
        for path in (home / ".local/share/sandbox-ai", home / ".local", home / ".local/state/sandbox-ai/sessions"):
            with pytest.raises(ValueError, match="sandbox-ai's own folder|sandbox-ai's runs"):
                s.check_given_path(path, "--mount", False)
        with pytest.raises(ValueError, match="sandbox-ai's own launcher"):
            s.check_given_path(s.HERE / "agent_mcp.py", "--mount", True)
    else:  # further own folders a start recorded (another XDG_* environment)
        with pytest.raises(ValueError, match="sandbox-ai's own folder"):
            s.check_given_path(tmp_path / "elsewhere/sandbox-ai/x", "Workspace", True,
                               refused=[tmp_path / "elsewhere/sandbox-ai"])


def test_check_held_both_ways(scenario, tmp_path):
    """Finding L3: a tree inside what the launcher reads (--privacy-rules, a --kit-dir) is refused."""
    rules = tmp_path / "rules"
    if scenario == "happy":
        s.check_held_both_ways([tmp_path / "work"], rules, "--privacy-rules")
    elif scenario == "sad":
        with pytest.raises(ValueError, match="lies inside .*--privacy-rules"):
            s.check_held_both_ways([rules / "sub"], rules, "--privacy-rules")
    else:  # through a link: by where it leads
        rules.mkdir()
        (tmp_path / "link").symlink_to(rules)
        with pytest.raises(ValueError, match="--kit-dir"):
            s.check_held_both_ways([rules / "claude"], tmp_path / "link", "--kit-dir")


def test_gnupg_socket_dir(scenario, monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_GNUPG_SOCKETS", {})
    if scenario == "happy":
        monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/gpgconf")
        monkeypatch.setattr(s.subprocess, "run", lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="/run/user/1000/gnupg\n"))
        assert s.gnupg_socket_dir() == Path("/run/user/1000/gnupg")
    elif scenario == "sad":  # no gpgconf: nothing, no failure
        monkeypatch.setattr(s.shutil, "which", lambda name: None)
        assert s.gnupg_socket_dir() is None
    else:  # it fails or answers nonsense
        monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/gpgconf")
        monkeypatch.setattr(s.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="relative\n"))
        assert s.gnupg_socket_dir() is None


def test_read_only_mounts_get_every_check(tmp_path, monkeypatch):
    """Finding H1: --mount ~/.local/share/sandbox-ai:/mnt/x:ro and ~/.local:/mnt/l:ro were accepted silently."""
    home = Path.home()
    for path in (home / ".local/share/sandbox-ai", home / ".ssh", tmp_path / "work"):
        path.mkdir(parents=True, exist_ok=True)
    for source in (home / ".local/share/sandbox-ai", home / ".local"):
        with pytest.raises(ValueError, match="sandbox-ai's own folder|sandbox-ai's runs"):
            s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "work"), "--mount",
                                     f"{source}:/mnt/x:ro"]))


def test_runs_without_xdg_runtime_dir_are_a_socket_location(tmp_path, monkeypatch):
    """Finding H1: without XDG_RUNTIME_DIR the runs register in XDG_STATE_HOME/sandbox-ai/runs."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folders = [path for path, _ in s.socket_locations()]
    assert s.runs_dir().resolve() in folders and (tmp_path / "state/sandbox-ai/runs").resolve() in folders
    assert (Path.home() / ".local/state/sandbox-ai/runs").resolve() in folders  # the default place too
    assert (Path.home() / ".gnupg").resolve() in folders  # finding H6
    with pytest.raises(ValueError, match="sandbox-ai's runs"):
        s.check_socket_location((tmp_path / "state/sandbox-ai/runs/codex-1").resolve(), "--mount")


def test_workspace_below_the_launcher_tree_is_refused():
    """Finding M3: at or below sandbox/ and, from a checkout, below the tree (the app, install.sh, scripts)."""
    for path in (s.HERE / "builtin", s.HERE.parent / "projects/sandbox-ai-app", s.HERE.parent / "scripts",
                 s.HERE.parent / "install.sh"):
        with pytest.raises(ValueError, match="sandbox-ai's own launcher"):
            s.check_workspace_scope(path)


def test_private_files_and_login_folders_warn(tmp_path, monkeypatch):
    """Findings L1/H5: files directly in HOME and the agents' login folders are given with a warning that says what
    they are (the owner's decision: given, not refused)."""
    home = Path.home()
    home.mkdir(exist_ok=True)
    for name in (".bashrc", ".gitconfig", ".netrc", ".claude.json", ".other"):
        (home / name).write_text("")
    (home / ".claude").mkdir()
    said = []
    for name, words in ((".bashrc", "shell"), (".gitconfig", "Git"), (".netrc", "credentials"),
                        (".claude.json", "Claude Code"), (".other", "a file of your HOME")):
        text = s.check_private_location(home / name, "Workspace", said.append)
        assert text and words in text, (name, text)
    assert "Claude Code's folder" in s.check_private_location(home / ".claude", "--mount", said.append)
    assert s.check_private_location(home / "project", "Workspace", said.append) is None


def test_workspace_checks_record_own_folders(tmp_path):
    """Finding L2: a later workspace add refuses sandbox-ai's own folders as this start saw them."""
    checks = s.workspace_checks(exposure_args(tmp_path), tmp_path / "session")
    assert str((Path.home() / ".config/sandbox-ai").resolve()) in checks["own_folders"]


def test_control_peer_refused(scenario):
    """Finding H1 (defence in depth): a peer whose pid shows in the sandbox's PID namespace is refused."""
    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        if scenario == "happy":  # outside a sandbox's namespace (tests): nothing refused; its own pid neither
            assert rt.control_peer_refused(left) is None
            assert rt.control_peer_refused(left, sandboxed=True) is None
        elif scenario == "sad":  # another process of the sandbox
            child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                     pass_fds=(right.fileno(),))
            try:
                # The peer of the pair is this process; a peer of another pid stands in through a fake.
                fake = SimpleNamespace(getsockopt=lambda *a: struct.pack("3i", child.pid, 0, 0))
                assert "may not use the control socket" in rt.control_peer_refused(fake, sandboxed=True)
            finally:
                child.stdin.close()
                child.wait(5)
        else:  # outside the namespace: pid 0, the human's CLI
            fake = SimpleNamespace(getsockopt=lambda *a: struct.pack("3i", 0, 1000, 1000))
            assert rt.control_peer_refused(fake, sandboxed=True) is None
            broken = SimpleNamespace(getsockopt=Mock(side_effect=OSError("gone")))
            assert "cannot be told" in rt.control_peer_refused(broken, sandboxed=True)


def test_detached_root(scenario, tmp_path):
    if scenario == "happy":  # '..' of a tree's root is itself (as a detached clone's is)
        fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
        try:
            assert rt.detached_root(fd)
        finally:
            os.close(fd)
    elif scenario == "sad":  # a host directory leads to its parent
        fd = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
        try:
            assert not rt.detached_root(fd)
        finally:
            os.close(fd)
    else:
        assert not rt.detached_root(987654)


def test_control_server_survives_a_failing_request_and_refuses_sandbox_peers(tmp_path, monkeypatch):
    """Round 4: a thread that cannot be started (the pids limit) refuses that request; the socket keeps serving. And
    a peer inside the sandbox's PID namespace is refused (sandboxed)."""
    directory = short_dir("cs-")
    stop = threading.Event()
    relays = rt.McpRelays(run_dir=directory / "relays", live=True)

    def no_thread(*args, **kwargs):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(relays, "attach", no_thread)
    monkeypatch.setattr(rt, "detached_root", lambda fd: True)
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    thread = threading.Thread(target=rt.control_server, args=(directory / "c.sock", network, stop, [], None, None,
                                                              relays), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (directory / "c.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    def ask(request, fds=()):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(directory / "c.sock"))
            data = json.dumps(request).encode() + b"\n"
            if fds:
                socket.send_fds(client, [data], list(fds))
            else:
                client.sendall(data)
            return json.loads(client.makefile().readline())
    try:
        fd = os.open(directory, os.O_PATH | os.O_DIRECTORY)
        try:
            answer = ask({"op": "mcp-open", "name": "web", "relay": {"server": "web"}}, [fd])
        finally:
            os.close(fd)
        assert not answer["ok"] and "thread" in answer["error"]
        assert ask({"op": "show"})["ok"], "still serving"
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)
    # The same socket in a sandbox: this process's own pid passes, another's would not (test_control_peer_refused).
    directory = short_dir("cs-")
    stop = threading.Event()
    monkeypatch.setattr(rt, "control_peer_refused", lambda connection, sandboxed=None: "process 7 of the sandbox "
                        "may not use the control socket" if sandboxed else None)
    thread = threading.Thread(target=rt.control_server, args=(directory / "c.sock", network, stop),
                              kwargs={"sandboxed": True}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (directory / "c.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        with socket.socket(socket.AF_UNIX) as client:  # refused before it reads anything
            client.settimeout(10)
            client.connect(str(directory / "c.sock"))
            answer = json.loads(client.makefile().readline())
        assert not answer["ok"] and "may not use the control socket" in answer["error"]
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


def test_relay_connect_never_holds_the_lock(tmp_path):
    """Round 4 (HIGH): a server that takes no connections (its backlog full) must not hold up close() or add():
    the relay connects with a timeout, without its lock."""
    directory = short_dir("rc-")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(directory / "server.sock"))
    listener.listen(0)
    fillers = []
    try:
        for _ in range(8):  # fill the backlog: the next connect waits
            filler = socket.socket(socket.AF_UNIX)
            filler.setblocking(False)
            try:
                filler.connect(str(directory / "server.sock"))
            except BlockingIOError:
                pass
            fillers.append(filler)
        relay = rt.McpRelay(None, os.open(directory, os.O_PATH | os.O_DIRECTORY))
        errors = []

        def connect():
            try:
                relay.connect()
            except OSError as exc:
                errors.append(exc)
        worker = threading.Thread(target=connect, daemon=True)
        started = time.monotonic()
        worker.start()
        time.sleep(0.3)
        pair = socket.socketpair()
        fillers += pair
        assert relay.add(pair[0]) is True and time.monotonic() - started < 2, "add() was not held up"
        assert relay.close() is True
        worker.join(rt.MCP_CONNECT_SECONDS + 5)
        assert not worker.is_alive()
    finally:
        for filler in fillers:
            filler.close()
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_update_json_defaults_keeps_the_mode(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "c.json").write_text("{}")
    (home / "c.json").chmod(0o640)
    s.update_json_defaults(home, "c.json", lambda d: d.setdefault("k", 1) == 1, keep_mode=True)
    assert stat.S_IMODE((home / "c.json").stat().st_mode) == 0o640
    s.update_json_defaults(home, "c.json", lambda d: d.setdefault("j", 1) == 1)
    assert stat.S_IMODE((home / "c.json").stat().st_mode) == 0o600


def test_stores_and_other_starts_folders_are_refused_for_any_start(tmp_path, monkeypatch):
    """Finding M2: the plugin store is protected also for a codex start or one with --no-plugins, and another
    session's HOME, an MCP server's HOME or a login's staging folder is no start's but its own."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    state, data = tmp_path / "state/sandbox-ai", tmp_path / "data/sandbox-ai"
    for path in (data / "plugins/claude/x", state / "sessions/other/home", state / "mcp/demo/home",
                 data / "auth/.login-claude-a/home", tmp_path / "work"):
        path.mkdir(parents=True, exist_ok=True)
    for options in (["codex", "--workspace", str(data / "plugins/claude/x")],
                    ["claude", "--no-plugins", "--workspace", str(data / "plugins/claude/x")],
                    ["codex", "--workspace", str(tmp_path / "work"), "--state-dir", str(state / "sessions/other/home")],
                    ["codex", "--workspace", str(tmp_path / "work"), "--mount", f"{state / 'mcp/demo/home'}:/mnt/h:ro"],
                    ["codex", "--workspace", str(tmp_path / "work"), "--state-dir",
                     str(data / "auth/.login-claude-a/home")]):
        with pytest.raises(ValueError, match="sandbox-ai's own folder|plugin store"):
            s.validate(s.parse_args(options))


def test_project_directory(scenario, tmp_path, monkeypatch):
    """The directory you are in decides, when it holds a project configuration: it is the project (/work and the
    file that applies) and what --workspace names follows it. The mix-up of 2026-11-08: a start in the development
    checkout with --workspace of the publishing copy gave that copy as /work, and its project file was never read."""
    here, other = tmp_path / "project", tmp_path / "other"
    (here / "sub").mkdir(parents=True)
    other.mkdir()
    (here / s.CONFIG_NAME).write_text('{"version": 1, "agent": "claude"}')
    monkeypatch.chdir(here)
    monkeypatch.setenv("PWD", str(here))
    if scenario == "happy":
        a = s.parse_args(["claude", "--workspace", str(other)])
        assert a.cwd_project == str(here) and a.workspaces == [str(here), str(other)] and a.workspace == str(here)
    elif scenario == "sad":
        # Nothing takes a project over: no project file here, or the options say the workspaces are as given.
        monkeypatch.chdir(here / "sub")
        monkeypatch.setenv("PWD", str(here / "sub"))
        assert s.parse_args(["claude", "--workspace", str(other)]).workspaces == [str(other)]
        monkeypatch.chdir(here)
        monkeypatch.setenv("PWD", str(here))
        for extra in (["--workspaces-as-given"], ["--no-config"], ["--config", str(here / s.CONFIG_NAME)]):
            assert s.parse_args(["claude", "--workspace", str(other), *extra]).workspaces == [str(other)], extra
    else:
        # Already the main workspace, or no --workspace at all: nothing changes, and it is never listed twice.
        assert s.parse_args(["claude", "--workspace", f"{here},{other}"]).workspaces == [str(here), str(other)]
        assert s.parse_args(["claude", "--workspace", str(here)]).cwd_project is None
        assert s.parse_args(["claude"]).workspaces == [str(here)]
        # Review 5 (correctness M1): the directory named again in another spelling is listed once ("Workspaces must
        # not overlap: X and X" before); a folder in it or one holding it is what the user chose: their order stands.
        a = s.parse_args(["claude", "--workspace", f"{other},."])
        assert a.cwd_project == str(here) and a.workspaces == [str(here), str(other)]
        for named in (f"{here}/", f"../{here.name}", "sub", str(tmp_path), f"{other},sub"):
            a = s.parse_args(["claude", "--workspace", named])
            assert a.cwd_project is None and a.workspaces == named.split(","), named
