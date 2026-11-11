import importlib.util
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import signal
import socket
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_sandbox", ROOT / "sandbox/agent_sandbox.py")
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


class SandboxTests(unittest.TestCase):
    def test_missing_workspace_is_created_including_parents(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d) / "new parent" / "project"
            a = s.parse_args(["codex", "--workspace", str(workspace)])
            s.validate(a)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                s.prepare_workspace(a.workspace)
            self.assertTrue(workspace.is_dir())
            self.assertNotIn("bereits", stderr.getvalue())

    def test_existing_workspace_warns_and_preserves_content_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            marker = workspace / "keep.txt"
            marker.write_text("operator content")
            previous_mode = workspace.stat().st_mode
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                s.prepare_workspace(workspace)
            self.assertIn("warning", stderr.getvalue())
            self.assertIn("exists already", stderr.getvalue())
            self.assertEqual(marker.read_text(), "operator content")
            self.assertEqual(workspace.stat().st_mode, previous_mode)

    def test_workspace_file_and_invalid_path_are_rejected_without_creation(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "file"
            file.write_text("keep")
            for workspace in [file, Path(d) / "invalid\npath"]:
                with self.subTest(workspace=str(workspace)), self.assertRaises(ValueError):
                    s.validate(s.parse_args(["codex", "--workspace", str(workspace)]))
            self.assertEqual(file.read_text(), "keep")
            self.assertFalse((Path(d) / "invalid\npath").exists())
            # A comma separates workspaces; validation alone creates nothing.
            a = s.parse_args(["codex", "--workspace", f"{d}/one,{d}/two"])
            s.validate(a)
            self.assertEqual(a.workspaces, [f"{d}/one", f"{d}/two"])
            self.assertFalse((Path(d) / "one").exists() or (Path(d) / "two").exists())

    def test_workspace_permission_error_aborts(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s.Path, "mkdir",
                                                              side_effect=PermissionError(13, "denied", "/mnt/ext")):
            with self.assertRaisesRegex(ValueError, "new cannot be created: /mnt/ext is not writable for you"):
                s.prepare_workspace(Path(d) / "new")

    def test_a_workspace_that_cannot_be_created_is_refused_by_the_checks_a_dry_run_too(self):
        """Regression ("[Errno 13] Permission denied: '/mnt/ext'" after a check on another host said it can start):
        a workspace that is not there and whose nearest existing folder is not writable is refused before the start
        - by the dry run too - naming that folder."""
        with tempfile.TemporaryDirectory() as d:
            locked = Path(d) / "mnt"
            locked.mkdir()
            workspace = locked / "ext" / "home" / "project"
            writable = lambda path, mode: Path(path) != locked  # noqa: E731 - (root may write anything: patched)
            with patch.object(s.os, "access", side_effect=writable):
                with self.assertRaisesRegex(ValueError, f"{workspace} does not exist and cannot be created: "
                                                        f"{locked} is not writable for you"):
                    s.validate(s.parse_args(["codex", "--workspace", str(workspace), "--dry-run"]))
                s.validate(s.parse_args(["codex", "--workspace", str(Path(d) / "elsewhere" / "p")]))
                locked_existing = locked / "there"
                locked_existing.mkdir()
                s.validate(s.parse_args(["codex", "--workspace", str(locked_existing)]))  # there already: as before
            self.assertFalse((locked / "ext").exists())

    def test_missing_workspace_dry_run_never_creates_directory(self):
        with tempfile.TemporaryDirectory() as d:
            for agent in ["codex", "claude", "opencode"]:
                workspace = Path(d) / agent / "workspace"
                result = subprocess.run([str(ROOT / "sandbox-ai"), agent, "--workspace", str(workspace), "--dry-run"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["workspace"], str(workspace))
                self.assertFalse(workspace.exists())

    def test_workspace_is_prepared_before_both_backends_and_warning_does_not_abort(self):
        with tempfile.TemporaryDirectory() as d:
            for backend in ["podman", "bwrap"]:
                workspace = Path(d) / backend / "project"
                def launch(a, session, home):
                    self.assertTrue(workspace.is_dir())
                    (workspace / "keep").write_text("preserved")
                    return 0
                with self.subTest(backend=backend), patch.object(s.os, "geteuid", return_value=1000), \
                     patch.object(s.shutil, "which", return_value="/usr/bin/tool"), \
                     patch.object(s, "run_" + ("pod" if backend == "podman" else "bwrap"), side_effect=launch) as runner, \
                     contextlib.redirect_stderr(io.StringIO()) as stderr:
                    argv = ["codex", "--backend", backend, "--workspace", str(workspace), "--offline", "--probe", "--no-auth"]
                    self.assertEqual(s.main(argv), 0)
                    self.assertEqual(s.main(argv), 0)
                    self.assertEqual(runner.call_count, 2)
                    self.assertIn("exists already", stderr.getvalue())
                    self.assertEqual((workspace / "keep").read_text(), "preserved")

    def test_provider_defaults_and_no_host_network(self):
        a = s.parse_args(["codex", "--dry-run"])
        self.assertIn("api.openai.com", s.allowed_hosts(a))
        self.assertNotIn("api.anthropic.com", s.allowed_hosts(a))
        self.assertIn("chatgpt.com", s.allowed_hosts(a))
        self.assertNotIn("platform.claude.com", s.allowed_hosts(a))
        self.assertEqual(a.backend, "podman")

    def test_opencode_cloud_endpoint_is_enabled_by_default_only_for_opencode(self):
        self.assertIn("opencode.ai", s.allowed_hosts(s.parse_args(["opencode"])))
        self.assertNotIn("opencode.ai", s.allowed_hosts(s.parse_args(["codex"])))
        self.assertNotIn("opencode.ai", s.allowed_hosts(s.parse_args(["claude"])))
        self.assertNotIn("opencode.ai", s.allowed_hosts(s.parse_args(["opencode", "--no-provider-defaults"])))
        self.assertEqual(s.allowed_hosts(s.parse_args(["opencode", "--offline"])), [])

    def test_explicit_local_config_disables_cloud_and_is_copied_privately(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "model.jsonc"
            source.write_text('{"enabled_providers":["private-model"]}')
            home = Path(d) / "home"
            home.mkdir()
            a = s.parse_args(["opencode", "--local-config", str(source), "--no-auth"])
            s.validate(a)
            self.assertEqual(s.allowed_hosts(a), [])
            s.prepare_home(a, home)
            target = home / ".config/opencode/opencode.jsonc"
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            s.prepare_home(a, home)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(s.agent_environment(a)["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")

    def test_local_config_rejects_wrong_agent_and_missing_source(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "missing.json"
            for agent in ["codex", "opencode"]:
                with self.subTest(agent=agent), self.assertRaises(ValueError):
                    s.validate(s.parse_args([agent, "--local-config", str(source)]))

    def test_offline_removes_provider_hosts(self):
        a = s.parse_args(["claude", "--offline"])
        self.assertEqual(s.allowed_hosts(a), [])

    def test_offline_rejects_network_permissions(self):
        for args in (["--allow", "192.0.2.0/24"], ["--allow-host", "example.org"], ["--network", "lab"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                s.validate(s.parse_args(["codex", "--offline", *args]))

    def test_agent_args_preserve_shell_characters(self):
        a = s.parse_args(["opencode", "--", "run", "$(touch /tmp/bad); 'quoted'"])
        self.assertEqual(a.agent_args, ["run", "$(touch /tmp/bad); 'quoted'"])

    def test_codex_uses_verified_external_sandbox_by_default(self):
        a = s.parse_args(["codex", "--", "exec", "task"])
        self.assertEqual(s.agent_command(a, "codex"), ["codex", "--sandbox", "danger-full-access", "exec", "task"])

    def test_explicit_codex_sandbox_selection_is_preserved(self):
        for args in [["exec", "--sandbox", "read-only", "task"], ["-s", "workspace-write", "exec", "task"], ["--sandbox=read-only", "exec", "task"], ["exec", "--dangerously-bypass-approvals-and-sandbox", "task"]]:
            with self.subTest(args=args):
                a = s.parse_args(["codex", "--", *args])
                self.assertEqual(s.agent_command(a, "codex"), ["codex", *args])

    def test_codex_flags_are_not_added_to_custom_binary_or_other_agents(self):
        for agent, options, binary in [("codex", ["--binary", "/usr/bin/python3"], "/usr/bin/python3"), ("opencode", [], "opencode"), ("claude", [], "claude")]:
            with self.subTest(agent=agent):
                a = s.parse_args([agent, *options, "--", "--version"])
                self.assertEqual(s.agent_command(a, binary), [binary, "--version"])

    def test_dangerously_skip_permissions_per_agent(self):
        expected = {"codex": ["codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "task"],
                    "claude": ["claude", "--dangerously-skip-permissions", "exec", "task"],
                    "opencode": ["opencode", "exec", "task"]}
        for agent, command in expected.items():
            with self.subTest(agent=agent):
                a = s.parse_args([agent, "--dangerously-skip-permissions", "--", "exec", "task"])
                self.assertEqual(s.agent_command(a, agent), command)
        # The Git helper for logins added during the run is there in every start (sandbox-ai net allow --git-token-*).
        helper = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": s.RUNTIME_GIT_HELPER}
        self.assertEqual(s.agent_environment(s.parse_args(["claude", "--dangerously-skip-permissions", "--privacy-check", "off",
                                                           "--no-proxy"])),
                         {**s.TELEMETRY_ENV["claude"]["off"], **helper, "IS_SANDBOX": "1"})
        env = s.agent_environment(s.parse_args(["opencode", "--dangerously-skip-permissions"]))
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"]), {"*": "allow"})
        self.assertEqual(s.agent_environment(s.parse_args(["claude", "--privacy-check", "off", "--no-proxy"])),
                         {**s.TELEMETRY_ENV["claude"]["off"], **helper})
        # With the proxy (the default) TLS libraries are pointed at the bundle with the session CA.
        env = s.agent_environment(s.parse_args(["claude", "--privacy-check", "off"]))
        self.assertEqual(env["SSL_CERT_FILE"], "/run/sandbox-ca/bundle.pem")
        self.assertEqual(env["GIT_SSL_CAINFO"], "/run/sandbox-ca/bundle.pem")
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], "/run/sandbox-ca/ca.pem")
        self.assertEqual(s.agent_environment(s.parse_args(["claude", "--privacy-check", "off", "--offline"])),
                         {**s.TELEMETRY_ENV["claude"]["off"], **helper})

    def test_dangerously_skip_permissions_not_duplicated_or_used_in_probe(self):
        a = s.parse_args(["claude", "--dangerously-skip-permissions", "--", "--dangerously-skip-permissions", "-p", "x"])
        self.assertEqual(s.agent_command(a, "claude"), ["claude", "--dangerously-skip-permissions", "-p", "x"])
        a = s.parse_args(["claude", "--dangerously-skip-permissions", "--probe"])
        self.assertEqual(s.agent_command(a, "claude"), ["claude"])

    def test_frontier_allows_openai_and_anthropic_only(self):
        hosts = s.allowed_hosts(s.parse_args(["opencode", "--frontier"]))
        self.assertEqual(hosts, ["opencode.ai", *s.PROVIDERS["codex"], *s.PROVIDERS["claude"]])
        self.assertEqual(s.allowed_hosts(s.parse_args(["opencode", "--frontier", "--no-provider-defaults"])),
                         [*s.PROVIDERS["codex"], *s.PROVIDERS["claude"]])
        with self.assertRaises(ValueError):
            s.validate(s.parse_args(["codex", "--offline", "--frontier"]))

    def test_anthropic_ingress_ranges_are_https_only(self):
        self.assertEqual(s.host_networks(s.allowed_hosts(s.parse_args(["claude"]))), ["160.79.104.0/23", "2607:6bc0::/48"])
        self.assertEqual(s.host_networks(s.allowed_hosts(s.parse_args(["codex"]))), [])
        rules = s.firewall_rules([], ["203.0.113.4", "160.79.104.0/23", "2607:6bc0::/48"])
        self.assertIn("ip daddr 203.0.113.4 tcp dport 443 accept", rules)
        self.assertIn("ip daddr 160.79.104.0/23 tcp dport 443 accept", rules)
        self.assertIn("ip6 daddr 2607:6bc0::/48 tcp dport 443 accept", rules)
        self.assertIn("policy drop", rules)

    def test_log_dir_must_stay_outside_agent_visible_trees(self):
        with tempfile.TemporaryDirectory() as d:
            for log in [d, str(Path(d) / "logs"), str(Path(d).parent)]:
                with self.subTest(log=log), self.assertRaisesRegex(ValueError, "outside"):
                    s.validate(s.parse_args(["codex", "--workspace", d, "--log-dir", log]))
            with tempfile.TemporaryDirectory() as other:
                s.validate(s.parse_args(["codex", "--workspace", d, "--log-dir", other]))

    def test_logging_adds_denied_sets_and_local_dns_stub(self):
        rules = s.firewall_rules([], ["203.0.113.4"], log=True)
        self.assertIn("set denied4 { type ipv4_addr . inet_proto . inet_service; flags dynamic;", rules)
        self.assertIn("add @denied6 { ip6 daddr . meta l4proto . th dport }", rules)
        self.assertLess(rules.index("tcp dport 443 accept"), rules.index("add @denied4"))
        self.assertNotIn("denied", s.firewall_rules([], ["203.0.113.4"]))
        with tempfile.TemporaryDirectory() as d:
            seen = {}
            def inspect(a, session, home):
                seen.update({name: (session / name).read_text() for name in ("resolv.conf", "nsswitch.conf", "config.json")})
                return 0
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=inspect):
                s.main(["codex", "--workspace", str(Path(d) / "w"), "--offline", "--no-auth", "--log-dir", str(Path(d) / "logs")])
            self.assertEqual(seen["resolv.conf"], "nameserver 127.0.0.1\n")
            self.assertIn("hosts: files dns", seen["nsswitch.conf"])
            self.assertEqual(json.loads(seen["config.json"])["watch"], ["/work", "/root", "/tmp"])
            self.assertEqual((Path(d) / "logs").stat().st_mode & 0o777, 0o700)

    def test_log_dir_mounted_for_both_backends(self):
        with tempfile.TemporaryDirectory() as d:
            a = s.parse_args(["codex", "--workspace", str(Path(d) / "w"), "--log-dir", str(Path(d) / "logs")])
            _, run = s.pod_commands(a, "pod", Path(d), Path(d) / "home")
            self.assertIn(f"type=bind,src={Path(d).resolve() / 'logs'},dst=/sandbox/log,rw,nodev", run)
            self.assertIn("NET_BIND_SERVICE", run)
            self.assertNotIn("NET_BIND_SERVICE", s.pod_commands(s.parse_args(["codex"]), "pod", Path(d), Path(d))[1])
            a.backend = "bwrap"
            cmd = s.bwrap_command(a, Path(d), Path(d) / "home")
            index = cmd.index("/sandbox/log")
            self.assertEqual(cmd[index - 2:index], ["--bind", str(Path(d).resolve() / "logs")])

    def test_claude_credentials_written_back_after_sandbox_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            host = Path(d) / "host"
            (host / ".claude").mkdir(parents=True)
            creds = host / ".claude/.credentials.json"
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "old", "expiresAt": 1000}}))
            # The account Claude keeps for the host's login, and the one Anthropic names for the sandbox's new token.
            (host / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acc", "organizationUuid": "org"}}))
            def refresh(a, session, home):
                (home / ".claude/.credentials.json").write_text(json.dumps(
                    {"claudeAiOauth": {"accessToken": "b", "refreshToken": "rotated", "expiresAt": 2000}}))
                return 0
            with patch.dict(os.environ, {"HOME": str(host)}), patch.object(s.os, "geteuid", return_value=1000), \
                    patch.object(s, "claude_profile_account", side_effect=lambda token, timeout=None: ("acc", "org")), \
                    patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), patch.object(s, "run_pod", side_effect=refresh), \
                    contextlib.redirect_stderr(io.StringIO()):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                s.main(["claude", "--workspace", str(Path(d) / "w"), "--offline"])
            self.assertEqual(json.loads(creds.read_text())["claudeAiOauth"]["refreshToken"], "rotated")

    def test_newer_host_claude_credentials_refresh_reused_state(self):
        with tempfile.TemporaryDirectory() as d:
            host, state = Path(d) / "host", Path(d) / "state"
            for root, token, expires in [(host, "host-new", 5000), (state, "state-old", 1000)]:
                (root / ".claude").mkdir(parents=True)
                (root / ".claude/.credentials.json").write_text(json.dumps(
                    {"claudeAiOauth": {"accessToken": "a", "refreshToken": token, "expiresAt": expires}}))
            for options in [[], ["--copy-auth"]]:
                with self.subTest(options=options), patch.dict(os.environ, {"HOME": str(host)}):
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                    a = s.parse_args(["claude", *options])
                    s.prepare_home(a, state)
                    self.assertEqual(json.loads((state / ".claude/.credentials.json").read_text())["claudeAiOauth"]["refreshToken"], "host-new")

    def test_dns_rules_open_only_port_53_to_selected_nameservers(self):
        rules = s.firewall_rules([], [], dns=["1.1.1.1", "2001:4860:4860::8888"])
        for line in ["ip daddr 1.1.1.1 udp dport 53 accept", "ip daddr 1.1.1.1 tcp dport 53 accept",
                     "ip6 daddr 2001:4860:4860::8888 udp dport 53 accept", "ip6 daddr 2001:4860:4860::8888 tcp dport 53 accept"]:
            self.assertIn(line, rules)
        self.assertNotIn("dport 53 accept", s.firewall_rules([], []))
        with self.assertRaises(ValueError):
            s.firewall_rules([], [], dns=["1.1.1.1; accept"])

    def test_egress_all_allow_accepts_output_but_keeps_input_closed(self):
        rules = s.firewall_rules([], [], log=True, allow_all=True)
        output = rules[rules.index("chain output"):]
        self.assertIn("\n  accept\n", output)
        self.assertNotIn("add @denied", rules)
        self.assertIn("chain input { type filter hook input priority 0; policy drop;", rules)
        self.assertIn("chain forward { type filter hook forward priority 0; policy drop; }", rules)
        self.assertNotIn("\n  accept\n", s.firewall_rules([], []))

    def test_main_writes_dns_and_allow_all_into_session(self):
        for options, resolv, accept_all in [(["--egress-all-allow"], "nameserver 1.1.1.1\nnameserver 8.8.8.8\n", True),
                                            (["--dns", "--nameserver", "9.9.9.9"], "nameserver 9.9.9.9\n", False),
                                            (["--egress-all-allow", "--nameserver", "9.9.9.9"], "nameserver 9.9.9.9\n", True)]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as d:
                seen = {}
                def inspect(a, session, home):
                    seen.update({name: (session / name).read_text() for name in ("resolv.conf", "nsswitch.conf", "firewall.nft")})
                    return 0
                with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                        patch.object(s, "run_pod", side_effect=inspect), patch.object(s, "resolve_pod_hosts", return_value={}):
                    s.main(["codex", "--workspace", str(Path(d) / "w"), "--no-auth", "--no-provider-defaults", *options])
                self.assertEqual(seen["resolv.conf"], resolv)
                self.assertIn("hosts: files dns", seen["nsswitch.conf"])
                self.assertEqual("\n  accept\n" in seen["firewall.nft"], accept_all)

    def test_dry_run_reports_dns_and_egress_mode(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            s.main(["codex", "--egress-all-allow", "--dry-run"])
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["nameservers"], ["1.1.1.1", "8.8.8.8"])
        self.assertTrue(plan["egress_all_allow"])

    def test_dry_run_reports_host_ports(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            s.main(["claude", "--allow-host-port", "3000", "--allow-host-port", "3000", "--dry-run", "--no-config"])
        self.assertEqual(json.loads(stdout.getvalue())["host_ports"], [3000])

    def test_real_inotify_records_write_and_read(self):
        # Order matters: the watched tree is removed while LOG_DIR still points
        # at a live directory, so the daemon thread never writes elsewhere.
        with tempfile.TemporaryDirectory() as logs, patch.object(sr, "LOG_DIR", Path(logs)), tempfile.TemporaryDirectory() as d:
            root, log = Path(d) / "work", Path(logs)
            root.mkdir()
            stop = threading.Event()
            logger = threading.Thread(target=s.file_logger, args=([str(root)], stop), daemon=True)
            logger.start()
            time.sleep(0.3)
            (root / "a.txt").write_text("x")
            (root / "a.txt").read_text()
            (root / "sub").mkdir()
            time.sleep(0.3)
            (root / "sub/b.txt").write_text("y")
            deadline = time.time() + 5
            records = []
            while time.time() < deadline:
                path = log / "files.jsonl"
                records = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
                if ("write", str(root / "sub/b.txt")) in {(r.get("action"), r.get("path")) for r in records}:
                    break
                time.sleep(0.1)
            stop.set()
            logger.join(5)
            self.assertFalse(logger.is_alive())
            seen = {(r.get("action"), r.get("path")) for r in records}
            for expected in [("create", str(root / "a.txt")), ("write", str(root / "a.txt")),
                             ("read", str(root / "a.txt")), ("write", str(root / "sub/b.txt"))]:
                self.assertIn(expected, seen)

    def test_real_inotify_drains_events_queued_before_stop(self):
        with tempfile.TemporaryDirectory() as logs, patch.object(sr, "LOG_DIR", Path(logs)), tempfile.TemporaryDirectory() as root:
            stop = threading.Event()
            logger = threading.Thread(target=s.file_logger, args=([root], stop), daemon=True)
            logger.start()
            time.sleep(0.3)
            (Path(root) / "last.txt").write_text("x")
            stop.set()
            logger.join(5)
            self.assertFalse(logger.is_alive())
            records = [json.loads(l) for l in (Path(logs) / "files.jsonl").read_text().splitlines()]
            self.assertIn(("write", str(Path(root) / "last.txt")), {(r.get("action"), r.get("path")) for r in records})

    def test_bootstrap_stops_joins_and_flushes_loggers(self):
        stop = threading.Event()
        stop.seen, stop.threads = {"seen"}, [Mock(), Mock()]
        with tempfile.TemporaryDirectory() as logs, patch.object(sr, "LOG_DIR", Path(logs)), \
                patch.object(s.RUNTIME, "SESSION_CONFIG", Path(logs) / "absent.json"), \
                patch.object(s.os, "getpid", return_value=1), patch.object(s.RUNTIME.ctypes, "CDLL", return_value=Mock(prctl=Mock(return_value=0))), \
                patch.object(s.subprocess, "run"), patch.object(s.subprocess, "Popen"), patch.object(sr, "namespace_state", return_value={}), \
                patch.object(sr, "firejail_command", return_value=["fj"]), patch.object(sr, "wait_child", return_value=7), \
                patch.object(sr, "start_loggers", return_value=stop), patch.object(sr, "poll_denied") as poll:
            self.assertEqual(s.inner_bootstrap(), 7)
        self.assertTrue(stop.is_set())
        for thread in stop.threads:
            thread.join.assert_called_once_with(3)
        poll.assert_called_once_with({"seen"})

    def test_real_dns_stub_answers_nxdomain_and_logs_unpinned(self):
        with tempfile.TemporaryDirectory() as d, patch.object(sr, "LOG_DIR", Path(d)):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            stop = threading.Event()
            logger = threading.Thread(target=s.dns_logger, args=({"api.openai.com"}, port, stop), daemon=True)
            logger.start()
            time.sleep(0.2)
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(3)
            for name in ("evil.example", "api.openai.com"):
                question = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0\0\1\0\1"
                client.sendto(b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + question, ("127.0.0.1", port))
                reply = client.recv(512)
                self.assertEqual(reply[:2], b"\xab\xcd")
                self.assertEqual(reply[3] & 0xF, 3)
            hosts = [json.loads(l)["host"] for l in (Path(d) / "egress-denied.jsonl").read_text().splitlines()]
            self.assertEqual(hosts, ["evil.example"])
            stop.set()
            logger.join(5)
            self.assertFalse(logger.is_alive())

    def test_credential_sync_copies_only_whitelisted_typed_fields(self):
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d) / "sandbox.json", Path(d) / "host.json"
            source.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "new-a", "refreshToken": "new-r", "expiresAt": 2000, "refreshTokenExpiresAt": 9000,
                "scopes": ["user:inference", 7], "subscriptionType": "max", "evil": {"injected": True}}, "other": 1}))
            target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000,
                                                            "subscriptionType": "pro", "scopes": ["user:inference"]},
                                          "mcpOAuth": {"keep": True}}))
            self.assertTrue(s.sync_claude_credentials(source, target))
            document = json.loads(target.read_text())
            self.assertEqual(document["claudeAiOauth"], {"accessToken": "new-a", "refreshToken": "new-r", "expiresAt": 2000,
                                                         "refreshTokenExpiresAt": 9000, "subscriptionType": "pro",
                                                         "scopes": ["user:inference"]})
            self.assertEqual(document["mcpOAuth"], {"keep": True})
            self.assertNotIn("other", document)

    def test_credential_sync_skips_missing_target_directory(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "sandbox.json"
            source.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1}}))
            self.assertFalse(s.sync_claude_credentials(source, Path(d) / "absent/dir/.credentials.json"))

    def test_write_back_failure_keeps_agent_exit_code(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HOME": str(Path(d) / "host")}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", return_value=5), patch.object(s, "claude_writeback", side_effect=OSError("disk full")), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(s.main(["claude", "--workspace", str(Path(d) / "w"), "--offline"]), 5)
            self.assertIn("could not write back the Claude login: disk full", stderr.getvalue())

    def test_existing_log_dir_is_tightened_to_0700(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir(mode=0o755)
            logs.chmod(0o755)
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", return_value=0), contextlib.redirect_stderr(io.StringIO()):
                s.main(["codex", "--workspace", str(Path(d) / "w"), "--offline", "--no-auth", "--log-dir", str(logs)])
            self.assertEqual(logs.stat().st_mode & 0o777, 0o700)

    def test_inotify_watch_exhaustion_is_logged_once(self):
        with tempfile.TemporaryDirectory() as logs, patch.object(sr, "LOG_DIR", Path(logs)), tempfile.TemporaryDirectory() as root:
            for name in "abcde":
                (Path(root) / name).mkdir()
            libc = Mock(inotify_init1=Mock(return_value=7), inotify_add_watch=Mock(return_value=-1))
            with patch.object(s.RUNTIME.ctypes, "CDLL", return_value=libc), patch.object(s.RUNTIME.ctypes, "get_errno", return_value=28), \
                    patch.object(s.os, "read", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    s.file_logger([root])
            records = [json.loads(l) for l in (Path(logs) / "files.jsonl").read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertIn("watch limit", records[0]["error"])

    def test_multiple_workspaces_first_is_always_work(self):
        with tempfile.TemporaryDirectory() as d:
            main, lib, docs = Path(d) / "main", Path(d) / "lib", Path(d) / "docs"
            a = s.parse_args(["codex", "--workspace", f"{main},{lib}", "--workspace", str(docs)])
            s.validate(a)
            self.assertEqual(a.workspace, str(main))
            self.assertEqual(a.workspaces, [str(main), str(lib), str(docs)])
            _, run = s.pod_commands(a, "pod", Path(d), Path(d) / "home")
            binds = [value for value in run if value.startswith("type=bind")]
            self.assertIn(f"type=bind,src={main},dst=/work,rw,nodev", binds)
            self.assertIn(f"type=bind,src={lib},dst=/workspaces/lib,rw,nodev", binds)
            self.assertLess(binds.index(f"type=bind,src={main},dst=/work,rw,nodev"), binds.index(f"type=bind,src={lib},dst=/workspaces/lib,rw,nodev"))
            a.backend = "bwrap"
            cmd = s.bwrap_command(a, Path(d), Path(d) / "home")
            index = cmd.index("/workspaces/docs")
            self.assertEqual(cmd[index - 2:index], ["--bind", str(docs)])

    def test_workspace_ro_applies_to_every_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            a = s.parse_args(["codex", "--workspace-ro", "--workspace", f"{d}/a,{d}/b"])
            s.validate(a)
            _, run = s.pod_commands(a, "pod", Path(d), Path(d) / "home")
            self.assertIn(f"type=bind,src={d}/b,dst=/workspaces/b,ro,nodev", run)
            a.backend = "bwrap"
            cmd = s.bwrap_command(a, Path(d), Path(d) / "home")
            self.assertEqual(cmd[cmd.index("/workspaces/b") - 2], "--ro-bind")

    def test_overlapping_or_duplicate_workspaces_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            for value in [f"{d}/a,{d}/a", f"{d}/a,{d}/a/sub", f"{d}/a/sub,{d}/a", f"{d}/a,", f",{d}/a"]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    s.validate(s.parse_args(["codex", "--workspace", value]))

    def test_main_creates_every_workspace_and_watches_them(self):
        with tempfile.TemporaryDirectory() as d:
            seen = {}
            def inspect(a, session, home):
                seen.update(json.loads((session / "config.json").read_text()))
                return 0
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=inspect), contextlib.redirect_stderr(io.StringIO()):
                s.main(["codex", "--workspace", f"{d}/main,{d}/new/lib", "--offline", "--no-auth",
                        "--log-dir", f"{d}/logs"])
            self.assertTrue((Path(d) / "new/lib").is_dir())
            self.assertEqual(seen["watch"][:2], ["/work", "/workspaces/lib"])
            # Without --log-dir nothing watches file access.
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=inspect), contextlib.redirect_stderr(io.StringIO()):
                s.main(["codex", "--workspace", f"{d}/main", "--offline", "--no-auth"])
            self.assertEqual(seen["watch"], [])

    def test_dry_run_lists_workspace_targets(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as stdout:
            s.main(["codex", "--workspace", f"{d}/main,{d}/lib", "--dry-run"])
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["workspace"], f"{d}/main")
        self.assertEqual(plan["workspaces"], [{"host": f"{d}/main", "sandbox": "/work"}, {"host": f"{d}/lib", "sandbox": "/workspaces/lib"}])
        self.assertFalse((Path(d) / "main").exists())

    def test_log_dir_inside_extra_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "outside"):
                s.validate(s.parse_args(["codex", "--workspace", f"{d}/main,{d}/lib", "--log-dir", f"{d}/lib/logs"]))

    def test_gui_options_validate(self):
        with tempfile.TemporaryDirectory() as d:
            for options in [["--gui-screen", "1920x1080"], ["--gui-screen", "0x0x24"], ["--gui-screen", "99999x1x24"],
                            ["--gui-vnc", d + "/w/vnc"]]:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    s.validate(s.parse_args(["codex", "--workspace", d + "/w", "--gui", *options]))
            a = s.parse_args(["codex", "--workspace", d + "/w", "--gui-vnc", d + "/vnc"])
            s.validate(a)
            self.assertTrue(a.gui)

    def test_gui_environment_and_mounts(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d) / "runtime"
            runtime.mkdir()
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(runtime / "wayland-1"))
            try:
                with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-1"}):
                    a = s.parse_args(["codex", "--workspace", d + "/w", "--gui-vnc", d + "/vnc", "--gui-wayland"])
                    s.validate(a)
                    env = s.agent_environment(a)
                    self.assertEqual(env["DISPLAY"], ":99")
                    self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
                    self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/0")
                    create, run = s.pod_commands(a, "pod", Path(d), Path(d) / "home")
                    self.assertIn("--shm-size=1g", create)
                    self.assertIn(f"type=bind,src={Path(d).resolve() / 'vnc'},dst=/sandbox/vnc,rw,nodev", run)
                    self.assertIn(f"type=bind,src={runtime / 'wayland-1'},dst=/run/user/0/wayland-0,rw,nodev", run)
                    a.backend = "bwrap"
                    cmd = s.bwrap_command(a, Path(d), Path(d) / "home")
                    self.assertEqual(cmd[cmd.index("/dev/shm") - 1], "--tmpfs")
                    index = cmd.index("/run/user/0/wayland-0")
                    self.assertEqual(cmd[index - 2:index], ["--bind", str(runtime / "wayland-1")])
                    self.assertLess(cmd.index("/run/user/0"), index)
            finally:
                server.close()
        plain = s.parse_args(["codex"])
        self.assertNotIn("DISPLAY", s.agent_environment(plain))
        self.assertNotIn("--shm-size=1g", s.pod_commands(plain, "pod", Path("/tmp"), Path("/tmp"))[0])

    def test_gui_wayland_requires_host_socket(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}):
            with self.assertRaisesRegex(ValueError, "WAYLAND_DISPLAY"):
                s.validate(s.parse_args(["codex", "--gui-wayland"]))

    def test_main_writes_gui_config_and_warns_for_wayland(self):
        with tempfile.TemporaryDirectory() as d:
            seen = {}
            def inspect(a, session, home):
                seen["gui"] = json.loads((session / "gui.json").read_text()) if (session / "gui.json").exists() else None
                return 0
            for options, expected in [(["--gui", "--gui-vnc", d + "/vnc"], {"display": ":99", "screen": "1920x1080x24", "vnc": True}), ([], None)]:
                with self.subTest(options=options), patch.object(s.os, "geteuid", return_value=1000), \
                        patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), patch.object(s, "run_pod", side_effect=inspect), \
                        contextlib.redirect_stderr(io.StringIO()):
                    s.main(["codex", "--workspace", d + "/w", "--offline", "--no-auth", *options])
                    self.assertEqual(seen["gui"], expected)
            self.assertEqual((Path(d) / "vnc").stat().st_mode & 0o777, 0o700)

    def test_bootstrap_starts_and_stops_gui(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "gui.json"
            config.write_text(json.dumps({"display": ":99", "screen": "1x1x24", "vnc": False}))
            processes = [Mock(), Mock()]
            with patch.object(sr, "GUI_CONFIG", config), patch.object(sr, "LOG_DIR", Path(d) / "absent"), \
                    patch.object(s.RUNTIME, "SESSION_CONFIG", Path(d) / "absent.json"), \
                    patch.object(s.os, "getpid", return_value=1), patch.object(s.RUNTIME.ctypes, "CDLL", return_value=Mock(prctl=Mock(return_value=0))), \
                    patch.object(s.subprocess, "run"), patch.object(s.subprocess, "Popen"), patch.object(sr, "namespace_state", return_value={}), \
                    patch.object(sr, "firejail_command", return_value=["fj"]), patch.object(sr, "wait_child", return_value=3), \
                    patch.object(sr, "start_gui", return_value=processes) as start, patch.object(sr, "stop_process") as stop:
                self.assertEqual(s.inner_bootstrap(), 3)
            start.assert_called_once_with({"display": ":99", "screen": "1x1x24", "vnc": False})
            self.assertEqual([c.args[0] for c in stop.call_args_list], processes)

    def test_firejail_hides_vnc_directory(self):
        with tempfile.TemporaryDirectory() as d, patch.object(sr, "VNC_DIR", Path(d)), \
                patch.object(s.subprocess, "run", return_value=Mock(stdout="")):
            self.assertIn(f"--blacklist={d}", s.firejail_command({}))

    def test_dry_run_reports_gui(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            s.main(["codex", "--gui", "--dry-run"])
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["gui"], {"display": ":99", "screen": "1920x1080x24", "vnc": False})
        self.assertFalse(plan["gui_wayland"])
        self.assertEqual((plan["devices"], plan["gpu"]), ([], None))

    def test_dry_run_reports_devices(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "CDI_DIRS", (Path(d),)), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            (Path(d) / "nvidia.yaml").write_text("kind: nvidia.com/gpu\n")
            s.main(["codex", "--gpu", "nvidia", "--dry-run"])
        plan = json.loads(stdout.getvalue())
        self.assertEqual((plan["devices"], plan["gpu"]), ([{"path": "nvidia.com/gpu=all", "kind": "cdi"}], "nvidia"))
        self.assertIn("NVIDIA GPUs (CDI)", stderr.getvalue())

    def test_dry_run_lists_planned_auth_copies_by_path_only(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HOME": d}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            (Path(d) / ".claude").mkdir()
            (Path(d) / ".claude/.credentials.json").write_text('{"claudeAiOauth": {"accessToken": "secret-value"}}')
            (Path(d) / ".claude.json").write_text("{}")
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["claude", "--copy-auth", "--dry-run"])
            plan = json.loads(stdout.getvalue())
            self.assertEqual(plan["auth_copies"], [
                {"source": f"{d}/.claude/.credentials.json", "target": "/root/.claude/.credentials.json"},
                {"source": f"{d}/.claude.json", "target": "/root/.claude.json"}])
            self.assertNotIn("secret-value", stdout.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["claude", "--auth-file", f"{d}/.claude/.credentials.json", "--dry-run"])
            self.assertEqual(json.loads(stdout.getvalue())["auth_copies"],
                             [{"source": f"{d}/.claude/.credentials.json", "target": "/root/.claude/.credentials.json"}])

    def test_start_reports_each_auth_copy_on_stderr(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HOME": d + "/host"}):
            os.environ.pop("CODEX_HOME", None)
            (Path(d) / "host/.codex").mkdir(parents=True)
            (Path(d) / "host/.codex/auth.json").write_text('{"tokens": {"access_token": "secret-value"}}')
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", return_value=0), contextlib.redirect_stderr(io.StringIO()) as stderr:
                s.main(["codex", "--workspace", d + "/w", "--offline", "--copy-auth"])
            self.assertIn(f"agent-sandbox: auth copied: {d}/host/.codex/auth.json -> /root/.codex/auth.json", stderr.getvalue())
            self.assertNotIn("secret-value", stderr.getvalue())

    def test_tmux_mode_requires_terminal_and_host_tmux_for_bwrap(self):
        with tempfile.TemporaryDirectory() as d:
            base = ["codex", "--workspace", d + "/w", "--offline", "--no-auth", "--tmux"]
            common = [patch.object(s.os, "geteuid", return_value=1000), patch.object(s, "run_pod", return_value=0),
                      patch.object(s, "run_bwrap", return_value=0), contextlib.redirect_stderr(io.StringIO())]
            with contextlib.ExitStack() as stack:
                for item in common:
                    stack.enter_context(item)
                stack.enter_context(patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n))
                stack.enter_context(patch.object(s.sys.stdin, "isatty", return_value=False))
                with self.assertRaisesRegex(ValueError, "terminal"):
                    s.main(base)
            with contextlib.ExitStack() as stack:
                for item in common:
                    stack.enter_context(item)
                stack.enter_context(patch.object(s.shutil, "which", side_effect=lambda n: None if n == "tmux" else "/usr/bin/" + n))
                stack.enter_context(patch.object(s.sys.stdin, "isatty", return_value=True))
                with self.assertRaisesRegex(ValueError, "tmux"):
                    s.main([*base, "--backend", "bwrap"])

    def test_tmux_flag_reaches_session_config_and_dry_run(self):
        # In a terminal a tmux start runs as a detachable session: a supervisor runs the sandbox in detached
        # tmux (config "detached"), and this terminal attaches.
        with tempfile.TemporaryDirectory() as d:
            with patch.object(s, "load_sessions_module") as load, patch.object(s.sys.stdin, "isatty", return_value=True), \
                    patch.object(s.os, "geteuid", return_value=1000), contextlib.redirect_stderr(io.StringIO()):
                load.return_value.start.return_value = 0
                self.assertEqual(s.main(["claude", "--workspace", d + "/w", "--offline", "--no-auth", "--tmux"]), 0)
            a, argv = load.return_value.start.call_args.args
            self.assertTrue(a.tmux)
            self.assertRegex(a.start_name, r"^[a-z]{3,8}-[a-z]{3,8}-[a-z]{3,8}$")  # a codename (codenames.py)
            seen = {}
            def inspect(a, session, home):
                seen.update(json.loads((session / "config.json").read_text()))
                return 0
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=inspect), patch.object(s.sys.stdin, "isatty", return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()):
                s.main(["claude", "--workspace", d + "/w", "--offline", "--no-auth", "--tmux-socket-dir", d + "/tmux"])
            self.assertEqual(seen["tmux"], "detached")
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["claude", "--tmux", "--dry-run"])
            plan = json.loads(stdout.getvalue())
            self.assertTrue(plan["tmux"] and plan["detachable"])

    def test_claude_can_reach_its_oauth_refresh_endpoints(self):
        hosts = s.allowed_hosts(s.parse_args(["claude"]))
        for host in ("api.anthropic.com", "console.anthropic.com", "platform.claude.com", "claude.ai"):
            self.assertIn(host, hosts)
        self.assertIn("console.anthropic.com", s.FRONTIER)

    def test_claude_token_validity_is_reported_without_contents(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "creds.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret-a", "refreshToken": "secret-r",
                                                          "expiresAt": (time.time() + 2.5 * 3600) * 1000}}))
            self.assertEqual(s.claude_token_status(path), "Claude access token valid for 2.5 h")
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000}}))
            self.assertIn("expired; Claude refreshes it on first use", s.claude_token_status(path))
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}))
            self.assertIn("no usable Claude login", s.claude_token_status(path))
            self.assertNotIn("secret", s.claude_token_status(path))

    def test_session_internal_options_bind_tmux_and_name_container(self):
        with tempfile.TemporaryDirectory() as d:
            tmux = Path(d) / "tmux"
            a = s.parse_args(["codex", "--workspace", d + "/w", "--tmux-socket-dir", str(tmux), "--pod-name", "agent-session-x"])
            s.validate(a)
            self.assertEqual(s.agent_environment(a)["TMUX_TMPDIR"], "/sandbox/tmux")
            _, run = s.pod_commands(a, "agent-session-x", Path(d), Path(d) / "home")
            self.assertIn(f"type=bind,src={tmux.resolve()},dst=/sandbox/tmux,rw,nodev", run)
            self.assertEqual(run[run.index("--name") + 1], "agent-session-x-agent")
            a.backend = "bwrap"
            cmd = s.bwrap_command(a, Path(d), Path(d) / "home")
            index = cmd.index("/sandbox/tmux")
            self.assertEqual(cmd[index - 2:index], ["--bind", str(tmux.resolve())])
            with patch.object(s.subprocess, "run"), patch.object(s.subprocess, "Popen"), patch.object(sr, "wait_child", return_value=0):
                a.backend = "podman"
                s.run_pod(a, Path(d), Path(d) / "home")
                create = s.subprocess.run.call_args_list[1].args[0]
                self.assertEqual(create[create.index("--name") + 1], "agent-session-x")
        plain = s.parse_args(["codex"])
        self.assertNotIn("TMUX_TMPDIR", s.agent_environment(plain))
        self.assertNotIn("--name", s.pod_commands(plain, "pod", Path("/tmp"), Path("/tmp"))[1])

    def test_session_internal_config_selects_detached_tmux(self):
        with tempfile.TemporaryDirectory() as d:
            seen = {}
            def inspect(a, session, home):
                seen.update(json.loads((session / "config.json").read_text()))
                return 0
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=inspect), patch.object(s.sys.stdin, "isatty", return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()):
                s.main(["claude", "--workspace", d + "/w", "--offline", "--no-auth", "--tmux-socket-dir", d + "/tmux"])
            self.assertEqual(seen["tmux"], "detached")

    def test_session_option_validation_and_delegation(self):
        with tempfile.TemporaryDirectory() as d:
            for options in [["--session", "bad name"], ["--session", "x" * 41], ["--session", "ok", "--state-dir", d],
                            ["--session", "ok", "--probe"], ["--session", "../x"]]:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    s.validate(s.parse_args(["codex", "--workspace", d + "/w", *options]))
            with patch.object(s, "load_sessions_module") as load:
                load.return_value.start.return_value = 0
                self.assertEqual(s.main(["codex", "--workspace", d + "/w", "--session", "demo-1", "--", "exec", "x"]), 0)
            load.return_value.start.assert_called_once()
            args, argv = load.return_value.start.call_args.args
            self.assertEqual(args.session, "demo-1")
            self.assertEqual(argv, ["codex", "--workspace", d + "/w", "--session", "demo-1", "--", "exec", "x"])

    def test_allow_tcp_opens_exactly_one_port(self):
        a = s.parse_args(["opencode", "--allow-tcp", "192.168.1.20:11434", "--allow-tcp", "[2001:db8::5]:8000"])
        s.validate(a)
        rules = s.firewall_rules([], [], tcp=s.tcp_targets(a))
        self.assertIn("ip daddr 192.168.1.20 tcp dport 11434 accept", rules)
        self.assertIn("ip6 daddr 2001:db8::5 tcp dport 8000 accept", rules)
        for bad in ["192.168.1.20", "192.168.1.20:0", "192.168.1.20:70000", "host.example:80", "1.2.3.4:80; accept"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                s.validate(s.parse_args(["opencode", "--allow-tcp", bad]))
        with self.assertRaises(ValueError):
            s.validate(s.parse_args(["opencode", "--offline", "--allow-tcp", "192.168.1.20:11434"]))

    def write_project_config(self, directory, document, trust=True):
        path = Path(directory) / ".sandbox-ai.json"
        path.write_text(json.dumps(document))
        if trust:
            s.trust_config(path)
        return path

    def test_project_config_is_loaded_from_main_workspace(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            work = Path(d) / "proj"
            work.mkdir()
            self.write_project_config(work, {"version": 1, "agent": "opencode",
                                              "options": ["--frontier", "--log-dir", d + "/logs"], "agent_args": ["run", "x"]})
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["opencode", "--workspace", str(work), "--dry-run"])
            plan = json.loads(stdout.getvalue())
            self.assertIn("api.anthropic.com", plan["https_hosts"])
            self.assertEqual(plan["log_dir"], d + "/logs")
            self.assertEqual(plan["agent_args"], ["run", "x"])
            self.assertEqual(plan["project_config"], str(work / ".sandbox-ai.json"))
            # Command-line arguments win over the file; explicit agent args replace the saved ones.
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["opencode", "--workspace", str(work), "--log-dir", d + "/other", "--dry-run", "--", "run", "y"])
            plan = json.loads(stdout.getvalue())
            self.assertEqual((plan["log_dir"], plan["agent_args"]), (d + "/other", ["run", "y"]))
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["opencode", "--workspace", str(work), "--no-config", "--dry-run"])
            self.assertIsNone(json.loads(stdout.getvalue())["project_config"])

    def test_untrusted_or_changed_project_config_is_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            path = self.write_project_config(d, {"version": 1, "agent": "codex", "options": ["--egress-all-allow"]}, trust=False)
            with self.assertRaisesRegex(ValueError, "not trusted"):
                s.main(["codex", "--workspace", d, "--dry-run"])
            s.trust_config(path)
            path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--egress-all-allow", "--gui-wayland"]}))
            with self.assertRaisesRegex(ValueError, "changed since it was trusted"):
                s.main(["codex", "--workspace", d, "--dry-run"])

    def test_project_config_content_is_validated(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            for document, message in [({"version": 1, "agent": "claude", "options": []}, "for claude"),
                                      ({"version": 2, "agent": "codex", "options": []}, "version"),
                                      ({"version": 1, "agent": "codex", "options": ["--pod-name", "agent-x"]}, "not allowed"),
                                      ({"version": 1, "agent": "codex", "options": ["--config", "x"]}, "not allowed"),
                                      ({"version": 1, "agent": "codex", "options": [1]}, "strings"),
                                      ({"version": 1, "agent": "codex", "options": ["--", "x"]}, "not allowed")]:
                with self.subTest(document=document):
                    self.write_project_config(d, document)
                    with self.assertRaisesRegex(ValueError, message):
                        s.main(["codex", "--workspace", d, "--dry-run"])

    def test_explicit_config_path(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            path = Path(d) / "elsewhere.json"
            path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--dns"]}))
            s.trust_config(path)
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["codex", "--workspace", d + "/w", "--config", str(path), "--dry-run"])
            self.assertEqual(json.loads(stdout.getvalue())["nameservers"], ["1.1.1.1", "8.8.8.8"])

    def test_session_accepts_tmux_from_project_config(self):
        # A session is tmux already; --tmux (e.g. from the wizard) is redundant, not an error.
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            work = Path(d) / "w"
            work.mkdir()
            self.write_project_config(work, {"version": 1, "agent": "claude", "options": ["--tmux"]})
            with patch.object(s, "load_sessions_module") as load:
                load.return_value.start.return_value = 0
                self.assertEqual(s.main(["claude", "--workspace", str(work), "--session", "demo"]), 0)
            # The supervisor runs without a terminal: --tmux plus the session socket means detached tmux.
            seen = {}
            def inspect(a, session, home):
                seen.update(json.loads((session / "config.json").read_text()))
                return 0
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=inspect), patch.object(s.sys.stdin, "isatty", return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()):
                s.main(["claude", "--workspace", str(work), "--offline", "--no-auth", "--tmux-socket-dir", d + "/tmux",
                        "--state-dir", d + "/home"])
            self.assertEqual(seen["tmux"], "detached")

    def test_allow_presets_add_only_their_hosts(self):
        expected = {"--allow-pypi": ["pypi.org", "files.pythonhosted.org"],
                    "--allow-github": ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
                                       "objects.githubusercontent.com", "release-assets.githubusercontent.com"],
                    "--allow-gitlab": ["gitlab.com", "registry.gitlab.com"],
                    "--allow-kernel-org": ["kernel.org", "www.kernel.org", "git.kernel.org", "cdn.kernel.org",
                                           "mirrors.kernel.org", "lore.kernel.org"],
                    "--allow-duckduckgo": ["duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com",
                                           "links.duckduckgo.com", "api.duckduckgo.com"]}
        base = s.allowed_hosts(s.parse_args(["codex"]))
        for flag, hosts in expected.items():
            with self.subTest(flag=flag):
                self.assertEqual(s.allowed_hosts(s.parse_args(["codex", flag])), base + hosts)
        both = s.allowed_hosts(s.parse_args(["codex", "--no-provider-defaults", "--allow-pypi", "--allow-gitlab"]))
        self.assertEqual(both, expected["--allow-pypi"] + expected["--allow-gitlab"])
        with self.assertRaises(ValueError):
            s.validate(s.parse_args(["codex", "--offline", "--allow-github"]))
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            s.main(["codex", "--allow-duckduckgo", "--dry-run"])
        self.assertIn("duckduckgo.com", json.loads(stdout.getvalue())["https_hosts"])

    def test_prepare_home_sets_claude_defaults_only_for_claude(self):
        for agent, expected in [("claude", True), ("codex", False), ("opencode", False)]:
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as d:
                home = Path(d) / "home"
                home.mkdir()
                s.prepare_home(s.parse_args([agent, "--no-auth"]), home)
                self.assertEqual((home / ".claude/settings.json").exists(), expected)

    def test_review_abbreviated_options_are_rejected(self):
        # argparse prefix matching must not reach internal or forbidden options.
        for argv in (["codex", "--tmux-s", "/x"], ["codex", "--sess", "x"], ["codex", "--no-conf"],
                     ["codex", "--pod-n", "agent-x"], ["codex", "--dry"], ["codex", "--gui-w"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                s.parse_args(argv)
        self.assertIn("--mcp-socket-dir", s.CONFIG_FORBIDDEN)

    def test_review_symlinked_project_config_is_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            trusted_project, attacker = Path(d) / "b", Path(d) / "a"
            trusted_project.mkdir()
            attacker.mkdir()
            self.write_project_config(trusted_project, {"version": 1, "agent": "codex", "options": ["--egress-all-allow"]})
            (attacker / ".sandbox-ai.json").symlink_to(trusted_project / ".sandbox-ai.json")
            with self.assertRaisesRegex(ValueError, "symlink"):
                s.main(["codex", "--workspace", str(attacker), "--dry-run"])

    def test_review_config_is_hashed_and_parsed_from_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            path = self.write_project_config(d, {"version": 1, "agent": "codex", "options": ["--dns"]})
            evil = json.dumps({"version": 1, "agent": "codex", "options": ["--egress-all-allow"]})
            real_read = s.os.read
            def swap_after_first_read(swap):
                calls = []
                def read(fd, size):
                    data = real_read(fd, size)
                    if not calls:
                        calls.append(1)
                        swap()
                    return data
                return read
            def atomic():
                (Path(d) / "evil.tmp").write_text(evil)
                os.replace(Path(d) / "evil.tmp", path)
            # Atomic swap (the reviewed attack): the checked bytes are the ones applied.
            with patch.object(s.os, "read", side_effect=swap_after_first_read(atomic)):
                _, document = s.load_project_config(s.parse_args(["codex", "--workspace", d]))
            self.assertEqual(document["options"], ["--dns"])
            # In-place rewrite mixes contents mid-read: the hash no longer matches, so it is refused.
            path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--dns"]}))
            s.trust_config(path)
            with patch.object(s.os, "read", side_effect=swap_after_first_read(lambda: path.write_text(evil + " " * 70000))):
                with self.assertRaisesRegex(ValueError, "changed since it was trusted"):
                    s.load_project_config(s.parse_args(["codex", "--workspace", d]))

    def test_review_missing_trusted_config_is_an_error(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            path = self.write_project_config(d, {"version": 1, "agent": "codex", "options": ["--offline"]})
            path.unlink()
            with self.assertRaisesRegex(ValueError, "trusted project configuration .* is missing"):
                s.main(["codex", "--workspace", d, "--dry-run"])
            with contextlib.redirect_stdout(io.StringIO()):
                s.main(["codex", "--workspace", d, "--no-config", "--dry-run"])

    def test_review_cli_auth_choice_replaces_config_auth_choice(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            self.write_project_config(d, {"version": 1, "agent": "codex", "options": ["--no-auth", "--dns"]})
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                s.main(["codex", "--workspace", d, "--copy-auth", "--dry-run"])
            plan = json.loads(stdout.getvalue())
            self.assertEqual(plan["auth_mode"], "copy")
            self.assertEqual(plan["nameservers"], ["1.1.1.1", "8.8.8.8"])

    def test_review_mcp_socket_appears_only_once_listening(self):
        # Regression: bind() creates the file before listen(); an early client was refused.
        refused = 0
        base = Path(tempfile.mkdtemp())  # kept: server threads outlive each iteration
        for index in range(60):
            sock = base / f"s{index}.sock"
            threading.Thread(target=s.mcp_serve, args=(str(sock), ["true"]), kwargs={"cwd": str(base)}, daemon=True).start()
            while not sock.exists():
                pass
            client = socket.socket(socket.AF_UNIX)
            try:
                client.connect(str(sock))
            except ConnectionRefusedError:
                refused += 1
            finally:
                client.close()
        self.assertEqual(refused, 0)

    def claude_host(self, d, expires=1000):
        host = Path(d) / "host"
        (host / ".claude").mkdir(parents=True)
        (host / ".claude/.credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "a", "refreshToken": "host", "expiresAt": expires}}))
        return host

    def run_claude(self, d, host, agent_writes, options=()):
        def run(a, session, home):
            agent_writes(home)
            return 5
        with patch.dict(os.environ, {"HOME": str(host)}), patch.object(s.os, "geteuid", return_value=1000), \
                patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), patch.object(s, "run_pod", side_effect=run), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            code = s.main(["claude", "--workspace", str(Path(d) / "w"), "--offline", *options])
        return code, stderr.getvalue()

    def test_review_one_state_dir_one_run(self):
        with tempfile.TemporaryDirectory() as d:
            host, state = self.claude_host(d), Path(d) / "state"
            seen = []

            def agent(home):  # the lock is held while the agent runs
                with self.assertRaisesRegex(ValueError, "in use by another run"), s.state_dir_lock(home):
                    pass
                seen.append(home)
            code, _ = self.run_claude(d, host, agent, ["--state-dir", str(state)])
            self.assertEqual((code, seen), (5, [state]))
            with s.state_dir_lock(state):
                with self.assertRaisesRegex(ValueError, f"--state-dir {state} is in use by another run"):
                    self.run_claude(d, host, lambda home: self.fail("started on a locked HOME"),
                                    ["--state-dir", str(state)])

    def test_review_writeback_survives_hostile_sandbox_files(self):
        hostile = {"nested": lambda c: c.write_text("[" * 100000),
                   "fifo": lambda c: (c.unlink(), os.mkfifo(c)),
                   "huge": lambda c: c.write_text('{"claudeAiOauth": {"x": "' + "y" * (2 << 20) + '"}}')}
        for name, plant in hostile.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                host = self.claude_host(d)
                before = (host / ".claude/.credentials.json").read_text()
                code, _ = self.run_claude(d, host, lambda home: plant(home / ".claude/.credentials.json"))
                self.assertEqual(code, 5)
                self.assertEqual((host / ".claude/.credentials.json").read_text(), before)

    def test_review_sandbox_symlinks_never_reach_host_files(self):
        with tempfile.TemporaryDirectory() as d:
            host = self.claude_host(d)
            other = Path(d) / "other.json"
            other.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "stolen", "expiresAt": 2000}}))
            def plant(home):
                creds = home / ".claude/.credentials.json"
                creds.unlink()
                creds.symlink_to(other)
            self.run_claude(d, host, plant)
            self.assertEqual(json.loads((host / ".claude/.credentials.json").read_text())["claudeAiOauth"]["refreshToken"], "host")

    def test_review_symlinked_claude_dir_in_reused_state_is_not_written(self):
        with tempfile.TemporaryDirectory() as d:
            host = self.claude_host(d, expires=9000)
            state, outside = Path(d) / "state", Path(d) / "outside"
            state.mkdir(mode=0o700)
            outside.mkdir()
            (state / ".claude").symlink_to(outside)
            with patch.dict(os.environ, {"HOME": str(host)}):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                with self.assertRaises(ValueError):
                    s.prepare_home(s.parse_args(["claude", "--state-dir", str(state)]), state)
            self.assertEqual(list(outside.iterdir()), [])

    def test_review_implausible_expiry_is_not_written_back(self):
        with tempfile.TemporaryDirectory() as d:
            host = self.claude_host(d, expires=1000)
            def plant(home):
                (home / ".claude/.credentials.json").write_text(json.dumps(
                    {"claudeAiOauth": {"accessToken": "a", "refreshToken": "forged", "expiresAt": 1e18}}))
            self.run_claude(d, host, plant)
            self.assertEqual(json.loads((host / ".claude/.credentials.json").read_text())["claudeAiOauth"]["refreshToken"], "host")
            # A genuine refresh is still written back: Anthropic names the account the host's ~/.claude.json names.
            (host / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acc", "organizationUuid": "org"}}))
            def refresh(home):
                (home / ".claude/.credentials.json").write_text(json.dumps(
                    {"claudeAiOauth": {"accessToken": "a2", "refreshToken": "rotated", "expiresAt": (time.time() + 3600) * 1000}}))
            with patch.object(s, "claude_profile_account", side_effect=lambda token, timeout=None: ("acc", "org")):
                self.run_claude(d, host, refresh)
            self.assertEqual(json.loads((host / ".claude/.credentials.json").read_text())["claudeAiOauth"]["refreshToken"], "rotated")

    def test_review_a_login_of_another_account_is_not_written_back(self):
        with tempfile.TemporaryDirectory() as d:
            host = self.claude_host(d, expires=(time.time() + 600) * 1000)
            (host / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acc", "organizationUuid": "org"}}))
            before = (host / ".claude/.credentials.json").read_bytes()
            def relogin(home):
                (home / ".claude/.credentials.json").write_text(json.dumps(
                    {"claudeAiOauth": {"accessToken": "other-access", "refreshToken": "other-refresh",
                                       "expiresAt": (time.time() + 3600) * 1000}}))
            with patch.object(s, "claude_profile_account", side_effect=lambda token, timeout=None: ("other", "org")):
                _, err = self.run_claude(d, host, relogin)
            self.assertEqual((host / ".claude/.credentials.json").read_bytes(), before)
            self.assertIn("another account", err)
            self.assertNotIn("other-access", err)
            self.assertNotIn("other-refresh", err)

    def test_review_sigterm_during_startup_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            seen = {}
            def run(a, session, home):
                seen["session"] = session
                os.kill(os.getpid(), signal.SIGTERM)  # stop arrives before wait_child installs its handler
                return 0
            previous = signal.getsignal(signal.SIGTERM)
            with patch.object(s.os, "geteuid", return_value=1000), patch.object(s.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                    patch.object(s, "run_pod", side_effect=run), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    s.main(["codex", "--workspace", d + "/w", "--offline", "--no-auth"])
            self.assertEqual(raised.exception.code, 143)
            self.assertFalse(seen["session"].exists(), "temporary session directory leaked")
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_review_pod_is_removed_even_if_creation_is_interrupted(self):
        a = s.parse_args(["codex", "--offline"])
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            if "create" in cmd:
                raise SystemExit(143)
            return Mock()
        with patch.object(s.subprocess, "run", side_effect=run), self.assertRaises(SystemExit):
            s.run_pod(a, Path("/tmp/session"), Path("/tmp/home"))
        self.assertTrue(any(cmd[-5:-1] == ["rm", "--force", "--time", "0"] for cmd in calls))

    def test_mount_with_spaces_and_ipv6_allow(self):
        with tempfile.TemporaryDirectory(prefix="space dir ") as d:
            self.assertEqual(s.parse_mount(d + ":/mnt/data:ro"), (str(Path(d).resolve()), "/mnt/data", "ro"))
        a = s.parse_args(["codex", "--allow", "2001:db8::/32"])
        s.validate(a)

    def test_mount_rejects_protected_paths_and_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            for target in ["/", "/etc", "/proc/x", "/root", "/sandbox", "/opt/../etc", "/mnt/../../etc", "relative"]:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    s.parse_mount(f"{d}:{target}:rw")
            with self.assertRaises(ValueError):
                s.parse_mount(f"{d}:/mnt/data:invalid")
            with self.assertRaises(ValueError):
                s.parse_mount("/does-not-exist:/mnt/data:ro")

    def test_mount_rejects_special_file(self):
        # Socket creation is forbidden by the host sandbox; a FIFO exercises
        # the same regular-file/directory check without requiring networking.
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "pipe")
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                s.parse_mount(path + ":/mnt/pipe:rw")

    def test_invalid_network_input(self):
        for args in (["--allow", "1.2.3.4;accept"], ["--allow-host", "example.com\naccept"], ["--network", "host"], ["--network", "container:abc"], ["--ip", "192.0.2.4"], ["--backend", "bwrap", "--network", "lab"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                s.validate(s.parse_args(["codex", *args]))

    def test_rules_deny_by_default_for_both_families(self):
        rules = s.firewall_rules(["198.51.100.0/24", "2001:db8::/32"], ["203.0.113.2", "2001:db8:1::1"])
        self.assertIn("table inet agent_sandbox", rules)
        self.assertEqual(rules.count("policy drop"), 3)
        self.assertIn("ip daddr 198.51.100.0/24 accept", rules)
        self.assertIn("ip6 daddr 2001:db8::/32 accept", rules)
        self.assertIn("ip daddr 203.0.113.2 tcp dport 443 accept", rules)
        self.assertNotIn("dport 53", rules)
        self.assertNotIn("flush ruleset", rules)

    def test_failed_dns_is_not_silently_allowed(self):
        with patch.object(s.socket, "getaddrinfo", side_effect=s.socket.gaierror("failed")):
            with self.assertRaises(ValueError):
                s.resolve_hosts(["api.openai.com"])

    def test_dns_results_are_pinned(self):
        with patch.object(s.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("203.0.113.4", 443))]):
            result = s.resolve_hosts(["api.openai.com"])
        self.assertEqual(result, {"api.openai.com": ["203.0.113.4"]})
        self.assertIn("203.0.113.4 api.openai.com", s.hosts_file(result))

    def test_auth_is_private_copy_and_host_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "auth.json"
            source.write_text('{"test":true}')
            home = Path(d) / "home"
            home.mkdir()
            s.copy_private(source, home, ".codex/auth.json")
            target = home / ".codex/auth.json"
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            target.write_text("changed")
            self.assertEqual(source.read_text(), '{"test":true}')

    def test_copy_rejects_traversal_symlink_and_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "auth.json"
            source.write_text("{}")
            home = root / "home"
            home.mkdir()
            (home / "link").symlink_to(root, target_is_directory=True)
            for target in ["../escape", "/absolute", "link/auth.json"]:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    s.copy_private(source, home, target)
            s.copy_private(source, home, "auth.json")
            with self.assertRaises(ValueError):
                s.copy_private(source, home, "auth.json")

    def test_auth_discovery_for_all_agents(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for name in [".codex/auth.json", ".claude/.credentials.json", ".claude.json", ".local/share/opencode/auth.json"]:
                p = home / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}")
            with patch.dict(os.environ, {"HOME": d, "CODEX_HOME": str(home / ".codex"), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "XDG_DATA_HOME": str(home / ".local/share")}):
                self.assertEqual(len(s.auth_sources("codex")), 1)
                self.assertEqual(len(s.auth_sources("claude")), 2)
                self.assertEqual(len(s.auth_sources("opencode")), 1)

    def test_guard_rejects_firejail_passthrough(self):
        state = {"mnt": "mnt:[1]", "pid": "pid:[2]", "CapEff": "0", "CapPrm": "0", "CapBnd": "0", "CapInh": "0", "CapAmb": "0", "NoNewPrivs": "1", "Seccomp": "2"}
        with self.assertRaises(RuntimeError):
            s.verify_inner(state, state)
        current = dict(state, mnt="mnt:[3]", pid="pid:[4]")
        s.verify_inner(state, current)
        for key, val in [("CapEff", "1"), ("CapBnd", "1"), ("CapInh", "1"), ("CapAmb", "1"), ("NoNewPrivs", "0"), ("Seccomp", "0")]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                s.verify_inner(state, dict(current, **{key: val}))

    def test_dry_run_has_no_auth_read_or_runtime_dependency(self):
        for agent in ["codex", "claude", "opencode"]:
            result = subprocess.run([str(ROOT / "sandbox-ai"), agent, "--dry-run", "--copy-auth", "--", "--version"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["agent"], agent)
            self.assertTrue(plan["copy_auth"])
            self.assertEqual(plan["agent_args"], ["--version"])
            self.assertNotIn("token", result.stdout.lower())

    def test_pod_uses_owned_namespace_and_verified_bootstrap(self):
        a = s.parse_args(["codex", "--network", "research", "--ip", "198.51.100.2"])
        create, run = s.pod_commands(a, "owned-pod", Path("/tmp/session"), Path("/tmp/home"))
        self.assertIn("--userns=host", create)
        self.assertFalse(any("keep-id" in token for token in create))  # Podman < 5.2: broken gid_map
        self.assertEqual(create[create.index("--infra-image") + 1], a.image)
        self.assertEqual(create[create.index("--infra-command") + 1], "/usr/bin/sleep infinity")
        self.assertEqual(create[create.index("--network") + 1], "research")
        self.assertIn("--read-only", run)
        self.assertIn("--cap-drop=ALL", run)
        self.assertIn("--pull=never", run)
        self.assertEqual(run[-1], "--internal-bootstrap")
        self.assertNotIn("--privileged", run)
        self.assertNotIn("--pid=host", run)

    def test_pod_cleanup_after_failed_agent(self):
        a = s.parse_args(["codex", "--offline"])
        with patch.object(s.subprocess, "run") as run, patch.object(s.subprocess, "Popen") as popen, patch.object(sr, "wait_child", return_value=7):
            self.assertEqual(s.run_pod(a, Path("/tmp/session"), Path("/tmp/home")), 7)
        self.assertEqual(run.call_args_list[-1].args[0][:4], ["podman", "pod", "rm", "--force"])
        name = run.call_args_list[1].args[0][4]
        self.assertEqual(run.call_args_list[-1].args[0][-1], name)
        self.assertTrue(name.startswith("agent-codex-"))

    def test_failed_pod_creation_does_not_delete_unowned_pod(self):
        a = s.parse_args(["codex", "--offline"])
        with patch.object(s.subprocess, "run", side_effect=[Mock(), subprocess.CalledProcessError(1, "create")]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                s.run_pod(a, Path("/tmp/session"), Path("/tmp/home"))
        self.assertEqual(run.call_count, 2)

    def test_bwrap_never_shares_host_network_or_root(self):
        a = s.parse_args(["codex", "--backend", "bwrap"])
        cmd = s.bwrap_command(a, Path("/tmp/session"), Path("/tmp/home"))
        self.assertIn("--unshare-all", cmd)
        self.assertIn("--unshare-user", cmd)
        self.assertIn("--as-pid-1", cmd)
        self.assertIn("--clearenv", cmd)
        self.assertNotIn("--share-net", cmd)
        self.assertNotIn("--ro-bind / /", " ".join(cmd))
        self.assertNotIn("/etc/shadow", cmd)

    def test_runtime_cannot_expose_host_proc_or_control_paths(self):
        for directory in ["/proc", "/etc", "/dev", "/tmp", "/", "/run"]:
            a = s.parse_args(["codex", "--backend", "bwrap", "--runtime", directory])
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                s.bwrap_command(a, Path("/tmp/session"), Path("/tmp/home"))

    def test_environment_is_opt_in_and_values_not_in_plan(self):
        with patch.dict(os.environ, {"TEST_AGENT_SECRET": "secret-value"}):
            a = s.parse_args(["codex", "--env", "TEST_AGENT_SECRET"])
            s.validate(a)
            self.assertNotIn("TEST_AGENT_SECRET", s.base_env())
            for name in ["HOME", "PATH", "CODEX_HOME", "XDG_DATA_HOME"]:
                with patch.dict(os.environ, {name: "/tmp/test"}), self.subTest(name=name), self.assertRaises(ValueError):
                    s.validate(s.parse_args(["codex", "--env", name]))

    def test_missing_auth_has_clear_error(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "auth_sources", return_value=[(Path(d) / "missing", ".codex/auth.json")]):
            with self.assertRaisesRegex(ValueError, "Auth JSON missing"):
                s.prepare_home(s.parse_args(["codex", "--copy-auth"]), Path(d))

    def test_slirp_setup_must_finish_before_releasing_agent(self):
        a = s.parse_args(["codex", "--backend", "bwrap"])
        child, slirp = Mock(), Mock()
        with patch.object(s.subprocess, "Popen", side_effect=[child, slirp]) as popen, \
             patch.object(s, "read_ready", side_effect=[{"child-pid": 12345}, RuntimeError("network failed")]), \
             patch.object(s, "configure_user_maps", create=True), \
             patch.object(s.os, "write") as write, patch.object(sr, "stop_process") as stop:
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                s.run_bwrap(a, Path("/tmp/session"), Path("/tmp/home"))
        # Only the UID/GID gate may open; the network gate must stay closed.
        self.assertEqual(write.call_count, 1)
        self.assertEqual(stop.call_count, 2)
        slirp_cmd = popen.call_args_list[1].args[0]
        self.assertIn("--disable-host-loopback", slirp_cmd)
        self.assertIn("12345", slirp_cmd)

    def test_cli_invalid_option_fails_before_start(self):
        result = subprocess.run([str(ROOT / "sandbox-ai"), "codex", "--network", "host", "--dry-run"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network", result.stderr)

    def test_mount_cannot_follow_workspace_symlink_to_control_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / "work"
            workspace.mkdir()
            (workspace / "escape").symlink_to("/sandbox")
            a = s.parse_args(["codex", "--workspace", str(workspace), "--mount", f"{d}:/work/escape/session:rw"])
            with self.assertRaisesRegex(ValueError, "symlink"):
                s.validate(a)

    def test_custom_claude_metadata_in_config_directory(self):
        with tempfile.TemporaryDirectory(suffix=".profile") as d:
            metadata = Path(d) / ".claude.json"
            metadata.write_text("{}")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                self.assertEqual(s.auth_sources("claude")[1][0], metadata)

    def test_claude_uses_default_home_config_location_inside_sandbox(self):
        self.assertNotIn("CLAUDE_CONFIG_DIR", s.base_env())

    def test_offline_bwrap_does_not_require_slirp(self):
        def which(name):
            return None if name == "slirp4netns" else "/usr/bin/" + name
        with tempfile.TemporaryDirectory() as d, \
             patch.object(s.os, "geteuid", return_value=1000), \
             patch.object(s.shutil, "which", side_effect=which), \
             patch.object(s, "run_bwrap", return_value=0):
            self.assertEqual(s.main(["codex", "--backend", "bwrap", "--offline", "--probe", "--workspace", d, "--no-auth"]), 0)

    def test_starters_work_through_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "sandbox-ai"
            link.symlink_to(ROOT / "sandbox-ai")
            for agent in ["codex", "claude", "opencode"]:
                result = subprocess.run([str(link), agent, "--dry-run"], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_user_mapping_preserves_setgroups_for_firejail(self):
        with patch.object(s, "subordinate_range", create=True, side_effect=[(100000, 65536), (200000, 65536)]), \
             patch.object(s.subprocess, "run") as run, \
             patch.object(s.os, "getuid", return_value=1000), patch.object(s.os, "getgid", return_value=1000), \
             patch.object(s.Path, "read_text", return_value="allow\n"):
            s.configure_user_maps(12345)
        self.assertEqual(run.call_args_list[0].args[0], ["newuidmap", "12345", "0", "1000", "1", "1", "100000", "65536"])
        self.assertEqual(run.call_args_list[1].args[0], ["newgidmap", "12345", "0", "1000", "1", "1", "200000", "65536"])

    def test_denied_setgroups_fails_before_namespace_release(self):
        with patch.object(s, "subordinate_range", create=True, return_value=(100000, 65536)), \
             patch.object(s.subprocess, "run"), patch.object(s.Path, "read_text", return_value="deny\n"):
            with self.assertRaisesRegex(RuntimeError, "setgroups"):
                s.configure_user_maps(12345)

    def test_subordinate_range_selects_only_own_sufficient_delegation(self):
        entries = "other:100000:65536\nresearcher:200000:10\nresearcher:300000:65536\n"
        with patch.object(s.pwd, "getpwuid", return_value=Mock(pw_name="researcher")), \
             patch.object(s.Path, "read_text", return_value=entries):
            self.assertEqual(s.subordinate_range("gid"), (300000, 65536))

    def test_missing_subordinate_delegation_is_explicit(self):
        with patch.object(s.Path, "read_text", return_value=""):
            with self.assertRaisesRegex(ValueError, "65536"):
                s.subordinate_range("uid")

    def test_ipv6_neighbor_discovery_is_allowed_without_general_icmp_egress(self):
        rules = s.firewall_rules([], [])
        self.assertIn("nd-neighbor-solicit", rules)
        self.assertIn("ip6 hoplimit 255", rules)
        self.assertNotIn("icmpv6 accept", rules)

    def test_podman_socket_applies_to_create_run_and_cleanup(self):
        a = s.parse_args(["codex", "--podman-url", "unix:///example/podman.sock", "--offline"])
        prefix = ["podman", "--remote", "--url", a.podman_url]
        with patch.object(s.subprocess, "run") as run, patch.object(s.subprocess, "Popen") as popen, patch.object(sr, "wait_child", return_value=0):
            s.run_pod(a, Path("/tmp/session"), Path("/tmp/home"))
        for call in run.call_args_list + popen.call_args_list:
            self.assertEqual(call.args[0][:4], prefix)

    def test_pod_dns_uses_same_engine_and_pins_validated_answers(self):
        a = s.parse_args(["codex", "--podman-url", "unix:///example/podman.sock"])
        with patch.object(s.subprocess, "run", return_value=Mock(stdout='{"api.openai.com":["203.0.113.4"]}')) as run:
            answer = s.resolve_pod_hosts(a, ["api.openai.com"])
        self.assertEqual(answer, {"api.openai.com": ["203.0.113.4"]})
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["podman", "--remote", "--url", a.podman_url])
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--pull=never", command)
        self.assertIn("pasta:--no-map-gw,-t,none,-u,none,-T,none,-U,none", command)

    def test_pod_dns_rejects_missing_or_invalid_addresses(self):
        a = s.parse_args(["codex"])
        for output in ['{}', '{"api.openai.com":[]}', '{"api.openai.com":["accept;"]}']:
            with self.subTest(output=output), patch.object(s.subprocess, "run", return_value=Mock(stdout=output)), self.assertRaises(ValueError):
                s.resolve_pod_hosts(a, ["api.openai.com"])

    def test_firejail_shell_option_only_when_supported(self):
        for help_text, expected in [("--shell=program", True), ("--noprofile --seccomp", False)]:
            with self.subTest(help=help_text), patch.object(s.subprocess, "run", return_value=Mock(stdout=help_text)):
                command = s.firejail_command({"mnt": "1", "pid": "2"})
                self.assertEqual("--shell=none" in command, expected)
                self.assertEqual(command[:4], ["setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--"])

    def test_capability_failure_reports_measured_mask(self):
        before = {"mnt": "1", "pid": "2"}
        current = {"mnt": "3", "pid": "4", "CapEff": "10", "CapPrm": "10", "CapBnd": "0", "CapInh": "0", "CapAmb": "0", "NoNewPrivs": "1", "Seccomp": "2"}
        with self.assertRaisesRegex(RuntimeError, "CapEff=10"):
            s.verify_inner(before, current)

    def test_codex_oauth_can_be_imported_as_opencode_private_copy(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 2000000000}).encode()).decode().rstrip("=")
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "auth.json"
            document = {"tokens": {"access_token": "fixture." + payload + ".signature", "refresh_token": "fixture-refresh", "account_id": "fixture-account"}}
            source.write_text(json.dumps(document))
            home = Path(d) / "home"
            home.mkdir()
            a = s.parse_args(["opencode", "--import-codex-auth", str(source)])
            s.validate(a)
            s.prepare_home(a, home)
            target = home / ".local/share/opencode/auth.json"
            result = json.loads(target.read_text())["openai"]
            self.assertEqual(result["type"], "oauth")
            self.assertEqual(result["expires"], 2000000000000)
            self.assertEqual(result["accountId"], "fixture-account")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(source.read_text()), document)

    def test_codex_auth_import_rejects_wrong_agent_conflicts_and_invalid_tokens(self):
        for argv in [["codex", "--import-codex-auth", "/tmp/auth"], ["opencode", "--copy-auth", "--import-codex-auth", "/tmp/auth"]]:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                s.validate(s.parse_args(argv))
        for document in [{}, {"tokens": {"access_token": "bad", "refresh_token": "x", "account_id": "y"}}]:
            with tempfile.TemporaryDirectory() as d:
                source = Path(d) / "auth.json"
                source.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    s.opencode_auth_from_codex(source)

    def test_opencode_model_catalog_is_copied_without_overwriting_existing_state(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache"
            (cache / "opencode").mkdir(parents=True)
            source = cache / "opencode/models.json"
            source.write_text('{"openai":{"models":{}}}')
            home = Path(d) / "home"
            home.mkdir()
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                s.prepare_home(s.parse_args(["opencode", "--no-auth"]), home)
                target = home / ".cache/opencode/models.json"
                self.assertEqual(target.read_bytes(), source.read_bytes())
                target.write_text('{"existing":true}')
                s.prepare_home(s.parse_args(["opencode", "--no-auth"]), home)
                self.assertEqual(target.read_text(), '{"existing":true}')


    def test_default_auth_is_automatically_copied_for_each_agent(self):
        for agent in ["codex", "claude", "opencode"]:
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                source = root / "host-auth.json"
                login = {"accessToken": "a", "refreshToken": "r", "expiresAt": 4102444800000}
                source.write_text(json.dumps({"claudeAiOauth": login, "mcpOAuth": {"srv": {"accessToken": "m"}}})
                                  if agent == "claude" else '{"fixture":"host credentials"}')
                home = root / "home"
                home.mkdir()
                sources = [(source, s.AUTH_TARGETS[agent])]
                if agent == "claude":
                    metadata = root / "metadata.json"
                    metadata.write_text(json.dumps({"oauthAccount": {"emailAddress": "a@example.org"},
                                                    "mcpServers": {"db": {"env": {"API_KEY": "secret"}}},
                                                    "projects": {"/home/me/secret-project": {}}}))
                    sources.append((metadata, ".claude.json"))
                with patch.object(s, "auth_sources", return_value=sources):
                    s.prepare_home(s.parse_args([agent]), home)
                for src, dest in sources:
                    if dest == ".claude.json":
                        # Only the account part crosses; MCP secrets and host project paths stay home.
                        copied = json.loads((home / dest).read_text())
                        self.assertEqual(copied["oauthAccount"], {"emailAddress": "a@example.org"})
                        self.assertNotIn("mcpServers", copied)
                        # Host project paths stay home; only the sandbox's own workspace is marked trusted.
                        self.assertEqual(copied["projects"], {"/work": {"hasTrustDialogAccepted": True}})
                        continue
                    if agent == "claude":
                        # The login crosses; OAuth tokens of the host's MCP servers do not.
                        self.assertEqual(json.loads((home / dest).read_text()), {"claudeAiOauth": login})
                        continue
                    self.assertEqual((home / dest).read_bytes(), src.read_bytes())
                    self.assertEqual((home / dest).stat().st_mode & 0o777, 0o600)

    def test_missing_default_auth_does_not_prevent_start(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with patch.object(s, "auth_sources", return_value=[(home / "missing", ".codex/auth.json")]):
                s.prepare_home(s.parse_args(["codex"]), home)
            self.assertFalse((home / ".codex/auth.json").exists())

    def test_automatic_auth_preserves_existing_refreshed_sandbox_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            source = Path(d) / "host-auth.json"
            source.write_text('{"fixture":"older host credentials"}')
            target = home / ".codex/auth.json"
            target.parent.mkdir()
            target.write_text('{"fixture":"refreshed sandbox credentials"}')
            with patch.object(s, "auth_sources", return_value=[(source, ".codex/auth.json")]):
                s.prepare_home(s.parse_args(["codex"]), home)
            self.assertEqual(target.read_text(), '{"fixture":"refreshed sandbox credentials"}')

    def test_no_auth_disables_automatic_auth_discovery(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "auth_sources", side_effect=AssertionError("must not discover host credentials")):
            s.prepare_home(s.parse_args(["codex", "--no-auth"]), Path(d))

    def test_explicit_auth_copy_never_mixes_in_default_auth(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "selected.json"
            source.write_text('{"fixture":"selected credentials"}')
            for options in [["--auth-file", str(source)], ["--copy", f"{source}:.codex/auth.json"]]:
                with self.subTest(options=options), tempfile.TemporaryDirectory() as home, patch.object(s, "auth_sources", side_effect=AssertionError("must not mix accounts")):
                    s.prepare_home(s.parse_args(["codex", *options]), Path(home))
                    self.assertEqual((Path(home) / ".codex/auth.json").read_bytes(), source.read_bytes())

    def test_no_auth_rejects_conflicting_explicit_auth_options(self):
        for option in [["--copy-auth"], ["--auth-file", "/tmp/selected"], ["--import-codex-auth", "/tmp/selected"]]:
            with self.subTest(option=option), self.assertRaises(ValueError):
                s.validate(s.parse_args(["opencode", "--no-auth", *option]))

    def test_dry_run_reports_auto_auth_without_reading_credentials(self):
        with patch.object(s, "auth_sources", side_effect=AssertionError("dry-run must not discover credentials")), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(s.main(["codex", "--dry-run"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["auth_mode"], "auto")


if __name__ == "__main__":
    unittest.main()
