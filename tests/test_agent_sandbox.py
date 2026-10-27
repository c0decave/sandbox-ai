import importlib.util
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_sandbox", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
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
            self.assertIn("Warnung", stderr.getvalue())
            self.assertIn("existiert bereits", stderr.getvalue())
            self.assertEqual(marker.read_text(), "operator content")
            self.assertEqual(workspace.stat().st_mode, previous_mode)

    def test_workspace_file_and_invalid_path_are_rejected_without_creation(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "file"
            file.write_text("keep")
            for workspace in [file, Path(d) / "invalid,path"]:
                with self.subTest(workspace=str(workspace)), self.assertRaises(ValueError):
                    s.validate(s.parse_args(["codex", "--workspace", str(workspace)]))
            self.assertEqual(file.read_text(), "keep")
            self.assertFalse((Path(d) / "invalid,path").exists())

    def test_workspace_permission_error_aborts(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s.Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                s.prepare_workspace(Path(d) / "new")

    def test_missing_workspace_dry_run_never_creates_directory(self):
        with tempfile.TemporaryDirectory() as d:
            for agent in ["codex", "claude", "opencode"]:
                workspace = Path(d) / agent / "workspace"
                result = subprocess.run([str(ROOT / f"{agent}-sandbox"), "--workspace", str(workspace), "--dry-run"], capture_output=True, text=True)
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
                    self.assertIn("existiert bereits", stderr.getvalue())
                    self.assertEqual((workspace / "keep").read_text(), "preserved")

    def test_provider_defaults_and_no_host_network(self):
        a = s.parse_args(["codex", "--dry-run"])
        self.assertIn("api.openai.com", s.allowed_hosts(a))
        self.assertIn("api.anthropic.com", s.allowed_hosts(a))
        self.assertIn("chatgpt.com", s.allowed_hosts(a))
        self.assertIn("platform.claude.com", s.allowed_hosts(a))
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
            result = subprocess.run([str(ROOT / f"{agent}-sandbox"), "--dry-run", "--copy-auth", "--", "--version"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["agent"], agent)
            self.assertTrue(plan["copy_auth"])
            self.assertEqual(plan["agent_args"], ["--version"])
            self.assertNotIn("token", result.stdout.lower())

    def test_pod_uses_owned_namespace_and_verified_bootstrap(self):
        a = s.parse_args(["codex", "--network", "research", "--ip", "198.51.100.2"])
        create, run = s.pod_commands(a, "owned-pod", Path("/tmp/session"), Path("/tmp/home"))
        self.assertIn("--userns=keep-id:uid=0,gid=0", create)
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
        with patch.object(s.subprocess, "run") as run, patch.object(s.subprocess, "Popen") as popen, patch.object(s, "wait_child", return_value=7):
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
             patch.object(s.os, "write") as write, patch.object(s, "stop_process") as stop:
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                s.run_bwrap(a, Path("/tmp/session"), Path("/tmp/home"))
        # Only the UID/GID gate may open; the network gate must stay closed.
        self.assertEqual(write.call_count, 1)
        self.assertEqual(stop.call_count, 2)
        slirp_cmd = popen.call_args_list[1].args[0]
        self.assertIn("--disable-host-loopback", slirp_cmd)
        self.assertIn("12345", slirp_cmd)

    def test_cli_invalid_option_fails_before_start(self):
        result = subprocess.run([str(ROOT / "codex-sandbox"), "--network", "host", "--dry-run"], capture_output=True, text=True)
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
            for name in ["codex-sandbox", "claude-sandbox", "opencode-sandbox"]:
                link = Path(d) / name
                link.symlink_to(ROOT / name)
                result = subprocess.run([str(link), "--dry-run"], text=True, capture_output=True)
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
        with patch.object(s.subprocess, "run") as run, patch.object(s.subprocess, "Popen") as popen, patch.object(s, "wait_child", return_value=0):
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
                source.write_text('{"fixture":"host credentials"}')
                home = root / "home"
                home.mkdir()
                sources = [(source, s.AUTH_TARGETS[agent])]
                if agent == "claude":
                    metadata = root / "metadata.json"
                    metadata.write_text('{"fixture":"account metadata"}')
                    sources.append((metadata, ".claude.json"))
                with patch.object(s, "auth_sources", return_value=sources):
                    s.prepare_home(s.parse_args([agent]), home)
                for src, dest in sources:
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
