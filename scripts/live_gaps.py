#!/usr/bin/env python3
"""Every live check the development sandbox cannot run, in one command, on your own Linux machine or Mac
(docs/LIVE_CHECK.md).

    scripts/live_check.sh                     the default checks (about 15-30 minutes; images must be built)
    scripts/live_check.sh --list              the checks: what each proves, what it needs, what it sends where
    scripts/live_check.sh --only NAME[,NAME]  only these (repeatable; the optional ones too)
    scripts/live_check.sh --build             build the missing images first (codex, claude, opencode)
    scripts/live_check.sh --host NAME         also the remote MCP check on that host of `sandbox-ai hosts`
    scripts/live_check.sh --llm NAME [--yes]  also the local-model smoke runs on that server of `sandbox-ai llm`:
                                              local-llm (three models x Claude Code, OpenCode, Codex) and
                                              harnesses-live (every harness of the launcher, each image built when
                                              missing, Podman and Bubblewrap) and cairn-workers-live (every
                                              harness as a worker of real Cairn, Podman)
    scripts/live_check.sh --only presets-live --llm NAME [--preset-values FILE] [--presets A,B] [--provider-login]
                                              every built-in preset started for real (its first agent; Podman and,
                                              where it can, Bubblewrap); FILE gives the values only you can give
    scripts/live_check.sh --only mcp-live [--mcp-servers A,B] [--mcp-repo DIR] [--mcp-vpn PROFILE]
                                              every server of the signed MCP catalog through a start's path
                                              (mcp add, a session with --mcp, its relay, initialize, tools/list,
                                              its smoke calls; Podman and Bubblewrap; per-workspace isolation);
                                              --mcp-vpn: those with network added behind a Tor profile PROFILE
                                              (mcp add --vpn: its own Tor container, as gowitness-tor runs)
    scripts/live_check.sh --device PATH       the device for devices-live (default: one `devices scan` found)
    scripts/live_check.sh --only artex-live   ARTEX beside an agent (asks first; builds its image when missing)
    scripts/live_check.sh --only burp-live [--burp-url URL] [--burp-jar JAR [--burp-edition E]
                          [--burp-license-file FILE]]
                                              Burp Suite's MCP server: host mode (your Burp with the extension
                                              answers through sandbox-ai's relay), container mode (your jar)
    scripts/live_check.sh --dmg-old A --dmg-new B   on a Mac: also the update of sandbox-ai.app from disk image A to
                                              B and back (each a .dmg, or the folder of a downloaded release)

Run it as yourself on the host (never with root or sudo, never inside a sandbox): rootless Podman and user
namespaces on Linux; on a Mac the macOS checks (macos-*: doctor, devices, the app, its update and rollback) - the
Linux checks are skipped there and say so. Your own sandbox-ai stays untouched: every check runs with a HOME,
configuration, state, data, cache and runtime folder of its own in a temporary folder, its own throwaway workspaces,
and its own sessions, stopped and deleted at the end. Podman alone sees your HOME (a small wrapper first on PATH), so your images are used and your
containers are never touched. No secret store of yours is read: the checks' secrets are plain files of their own.

The report - REPORT-DATE.md and REPORT-DATE.json in live-check/ of this checkout (--out DIR) - says per check
passed, failed or skipped, the exact command, every test's outcome and the last lines of its output, with secrets
and home paths replaced. The exit status is 0 whatever the checks found (2: refused to run, a wrong option).
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
GAPS = "tests/test_live_gaps.py"
TAIL = 60            # lines of a check's output in the report
DETAIL = 3000        # characters of one failing test's message in the report
REPORT_VERSION = 1


def load_live_check():
    """scripts/live_check.py as a module (its junit reader, its pytest venv, its container test)."""
    spec = importlib.util.spec_from_file_location("live_check_for_gaps", Path(__file__).with_name("live_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Check(NamedTuple):
    name: str
    proves: str
    needs: tuple            # prerequisite keys (PREREQUISITES), all of them needed
    tests: tuple            # pytest arguments: node ids or files
    network: str = ""       # what leaves this machine ("" nothing)
    optional: bool = False  # not in a run without --only (or its own option)
    keyword: str = ""       # pytest -k
    podman_only: bool = False
    timeout: int = 1800
    source: str = ""        # where the gap was named


CHECKS = (
    Check("release-gates",
          "Security reviews A and B's release gates in a real sandbox: sandbox-ai's system call filters beside "
          "Firejail's, nosuid/nodev mounts, Firejail's sealed folder and the run's manifest; --mode high starts with "
          "every part of its isolation; the terminal relays cannot be replaced from inside; --untrusted confines a "
          "command",
          ("sandbox", "image:codex"), ("tests/test_agent_sandbox_live.py",),
          keyword="isolation or strict_mode_starts or relays", timeout=2400,
          source="docs/reviews/security-review-fixes-A.md, -B.md"),
    Check("firejail-hides-control",
          "The agent and `sandbox-ai shell` cannot list or read /sandbox/proxy and /sandbox/control (Firejail's "
          "blacklist in effect), while /sandbox/session stays readable",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_firejail_hides_the_proxy_and_control_folders",),
          source="docs/reviews/credentials-verify-A.md, -B.md (read, not run there)"),
    Check("firewall-login-hosts",
          "A Git login's server (--git-token-env https://codeberg.org=...) is opened and no other host is, with the "
          "connection proxy and with the firewall alone (--no-proxy)",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_a_logins_server_is_opened_and_no_other_host",),
          network="HTTPS to codeberg.org (one GET of its front page, a made-up token that is never sent); gitlab.com "
                  "is tried and must be refused before it leaves",
          source="docs/reviews/credentials-verify-B.md (nftables)"),
    Check("git-login-give",
          "A Git login given to a running sandbox (git-logins give --env) makes its git push work against a Git "
          "server that wants a login, and stops working once revoked; the token is in no listing",
          ("podman", "image:codex", "git"),
          (f"{GAPS}::test_live_a_git_login_given_while_it_runs_reaches_git_until_revoked",), podman_only=True,
          source="docs/reviews/credentials-verify-A.md, -B.md (live containers)"),
    Check("hide-workspace-add",
          "--hide applies to a workspace added during the run (sandbox-ai workspace add): the matching files are "
          "empty and read-only in the sandbox, the others readable, the host's files unchanged",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_hide_covers_a_workspace_added_while_it_runs",),
          source="knostic-gaps item 5"),
    Check("untrusted-home",
          "--untrusted (Landlock) under the real kernel: inside a sandbox with a crowded HOME and a credential "
          "folder reached through a link; and on this host with your real HOME (only whether each credential "
          "folder can be opened is printed, never a byte of it)",
          ("sandbox", "image:codex", "linux"),
          (f"{GAPS}::test_live_untrusted_with_a_crowded_and_linked_home_in_a_sandbox",
           f"{GAPS}::test_live_untrusted_with_this_hosts_real_home"),
          source="docs/releases/v0.1.99.md (--untrusted with a crowded or linked HOME)"),
    Check("kits-attach",
          "sandbox-ai kits attach gives a skill and a command to a running Claude Code sandbox: there at once, "
          "read-only, gone after kits detach",
          ("sandbox", "image:claude"), (f"{GAPS}::test_live_kits_attach_reaches_a_running_claude_sandbox",),
          source="runtime-kits"),
    Check("plugins-attach",
          "sandbox-ai plugins attach installs a plugin of the built-in marketplace (sandcastle@sandbox-ai) into "
          "the plugin store and enables it in a running Claude Code sandbox; plugins detach turns it off",
          ("sandbox", "image:claude"), (f"{GAPS}::test_live_plugins_attach_enables_a_plugin_in_a_running_claude",),
          timeout=2400, source="runtime-kits"),
    Check("bridge-live",
          "sandbox-ai bridge between two real sessions (one Podman, one Bubblewrap when both work here): message, "
          "ask (answered), files:ro:src and files:rw:out (the write asked, its diff shown and allowed with "
          "sandbox-ai approvals), watch (activity, changes, terminal) with the observer's alert and hint - each call "
          "made by the session's agent side through its real relay - and revoke; then, with a Python that has "
          "PySide6 (SANDBOX_AI_APP_PYTHON), the app's New bridge… between the same two sessions offscreen",
          ("sandbox", "image:codex"), (f"{GAPS}::test_live_a_bridge_between_two_real_sessions",
                                        f"{GAPS}::test_live_the_apps_new_bridge_between_two_real_sessions"),
          timeout=1800,
          source="the user, 2026-11-21/22: New bridge in the app did nothing; check in depth that bridges work"),
    Check("devices-scan",
          "sandbox-ai devices scan on this machine's real hardware: valid JSON, every listed node exists, every "
          "refusal has a reason; the text form works too",
          ("linux",), (f"{GAPS}::test_live_devices_scan_on_this_machine",), timeout=300,
          source="board-devices"),
    Check("devices-live",
          "A device (--device, or one devices scan offers) added to a running sandbox appears there, devices list "
          "says so, and is gone after devices remove",
          ("sandbox", "image:codex", "linux", "device"),
          (f"{GAPS}::test_live_a_device_added_and_removed_while_it_runs",), source="board-devices"),
    Check("toolchain-live",
          "Build toolchains in a real running session, under Podman and under Bubblewrap: sandbox-ai toolchain add "
          "go and python (the signed catalog; SAI_LIVE_TOOLCHAINS=FILE: a toolchains.json of your own builds, added "
          "unsigned), attach into the running sandbox - mounted read-only, nosuid, nodev at /opt/toolchains/NAME, the "
          "commands found by a shell that was started before - a hello world built and run with go, python3 the "
          "toolchain's, toolchain list --run, detach (the commands and the mount gone); and a start with --toolchain "
          "go. Qt: pyside6 attached (it brings python) - import PySide6 in the shell's python3, a Widgets and a QML "
          "program drawn without a display (offscreen), their pictures not empty, pyside6-uic; and a start with "
          "--toolchain qt: a Widgets and QML program built with CMake and GCC (the image variant gcc, under "
          "Bubblewrap the host's) and run. Each Qt part is skipped while the catalog has no such toolchain. With "
          "--accept-android-sdk-license also android-sdk, the toolchain built on this machine: its recipe of "
          "the signed catalog, Google's files fetched and held to it, a start with --toolchain android-sdk (the jdk "
          "with it), adb and aapt2 run, and a small app assembled with Gradle and the Android Gradle plugin against "
          "the read-only SDK (x86_64; skipped without the option: the license is yours to accept)",
          ("sandbox", "image:codex", "linux"),
          (f"{GAPS}::test_live_toolchains_attached_to_a_running_session",
           f"{GAPS}::test_live_a_start_with_a_toolchain",
           f"{GAPS}::test_live_qt_for_python_attached_to_a_running_session",
           f"{GAPS}::test_live_qt_for_cpp_built_and_run",
           f"{GAPS}::test_live_the_android_sdk_toolchain_assembles_an_app"), timeout=5400,
          network="HTTPS to raw.githubusercontent.com and github.com (the signed toolchains.json and the archives of "
                  "go and python, about 90 MB, of pyside6, about 175 MB, and of qt; nothing with SAI_LIVE_TOOLCHAINS); "
                  "with --accept-android-sdk-license also dl.google.com (about 1.1 GB of the Android SDK), the jdk's "
                  "archive (about 390 MB), and Google's Maven and Maven Central from the sandbox (the Android Gradle "
                  "plugin)",
          source="the user, 2026-11-22: build toolchains for every language, mounted in at run time; 2026-11-24: "
                 "Android as a toolchain; a complete Qt toolchain for Python and C++"),
    Check("android-emulator-live",
          "The Android emulator as a service beside a real sandbox (Podman, a host with /dev/kvm, x86_64): its image "
          "built from sandbox-ai's own Containerfile and Google's downloads (images build-image android-emulator "
          "--builtin android-emulator), a start with --service android-emulator - the agent starts once Android has "
          "booted, reaches it as adb connect android-emulator:5555 (sys.boot_completed 1; adb of the toolchain "
          "android-sdk), and has no /dev/kvm itself; the image is removed again. Only with "
          "--accept-android-sdk-license: the license is yours to accept",
          ("sandbox", "image:codex", "linux", "podman", "kvm", "android-license"),
          (f"{GAPS}::test_live_the_android_emulator_service_boots_beside_the_sandbox",), optional=True,
          timeout=3 * 3600,
          network="HTTPS to dl.google.com (about 2.7 GB: the emulator, one system image, platform-tools, the "
                  "command-line tools; and the Android SDK of toolchain-live, 1.1 GB), deb.debian.org and docker.io "
                  "(the image's base and packages), github.com (the signed catalog, the jdk)",
          source="the user, 2026-11-24: Android as a service"),
    Check("update-live",
          "Signed updates from the real GitHub: the newest release's SHA256SUMS is signed by a key of this "
          "checkout's sandbox/release-signers and lists every kind's file (the bundle's SHA-256 checked); this "
          "checkout's `sandbox-ai update --check` finds it and offers it only when it is newer (never a downgrade); "
          "a copy one version older is offered it. Nothing is installed",
          (), (f"{GAPS}::test_live_update_check_against_github",), timeout=900,
          network="HTTPS to api.github.com (the releases API, read-only, no token) and github.com's release "
                  "downloads (SHA256SUMS, SHA256SUMS.sig and the bundle, about 16 MB)",
          source="user 2026-11-22: \"check if auto update signed code from github is now working\""),
    Check("remote-mcp",
          "A real host of sandbox-ai hosts: hosts test, its MCP servers listed over SSH, its demo server started "
          "and stopped there (only when no instance of it runs)",
          ("host",), (f"{GAPS}::test_live_remote_mcp_on_a_real_host",), optional=True, timeout=1200,
          network="SSH to the host given with --host (sandbox-ai there; its demo MCP server)",
          source="remote MCP (docs/reviews/remote-mcp-board.md)"),
    Check("local-llm",
          "One smoke run (a file written with the agent's own tool, a reply) of each of the three best models "
          "x Claude Code, OpenCode and Codex through the real launcher with --llm",
          ("sandbox", "llm", "image:claude", "image:codex", "image:opencode"),
          (f"{GAPS}::test_live_local_model_smoke_run",), optional=True, timeout=4 * 3600,
          network="prompts to your LLM server given with --llm, nothing else (--no-provider-defaults)",
          source="research-llm/LOCAL_LLM_REPORT.md section 7"),
    Check("harnesses-live",
          "Every harness the launcher knows (claude, codex, opencode, opencode2, hermes, dsh, qwen, goose, openhands, "
          "nanobot, letta, pi) does one smoke run - a file written with its own tool, the nonce replied - through the "
          "real launcher with --llm and nothing else open, with Podman and with Bubblewrap; a missing image is built "
          "first (the report says how long), and a harness whose image cannot be built here, or that this host has "
          "not installed for Bubblewrap, is skipped with the reason",
          ("sandbox", "llm"), (f"{GAPS}::test_live_harness_smoke_run",), optional=True, timeout=12 * 3600,
          network="prompts to your LLM server given with --llm, nothing else (--no-provider-defaults); a missing "
                  "image's packages while it is built (Debian, npm, PyPI, the harness's release)",
          source="the user, 2026-11-22: every harness tested live, not marked \"not live-tested\""),
    Check("presets-live",
          "Every built-in preset (sandbox/presets/*.json) is started for real through the launcher - sandbox-ai AGENT "
          "--preset NAME, its first agent - with a smoke task (a file written with the agent's own tool, the nonce "
          "replied), with Podman and, where the preset can, Bubblewrap; its part is checked as far as it can be "
          "without values of yours (each MCP server it names started, each service ready, each toolchain it brings "
          "added first); the model: --llm's server "
          "(nothing else open), or the agent's own provider with --provider-login. A missing image is built first. "
          "A preset that needs what only you can give (a target, a VPN profile, a device, your display) gets it from "
          "--preset-values FILE or is skipped with the reason and the command to run it yourself",
          ("sandbox", "model", "presets"), (f"{GAPS}::test_live_preset_smoke_run",), optional=True,
          timeout=24 * 3600,
          network="prompts to your LLM server given with --llm (or, with --provider-login, to each agent's own "
                  "provider); the hosts each preset opens itself (PyPI, npm, GitHub ... while the smoke task runs); "
                  "the MCP servers' images from ghcr.io (sandbox-ai mcp add), the toolchains a language preset brings "
                  "(sandbox-ai toolchain add: the signed toolchains.json and its archives from GitHub; "
                  "SAI_LIVE_TOOLCHAINS=FILE: your own builds) and a missing image's packages",
          source="the user, 2026-11-22: \"mir scheint das alle presets auch noch nicht getestet sind\""),
    Check("mcp-live",
          "Every server of the signed MCP catalog (sandbox-ai-mcp) tested for real through the path a start uses: "
          "`sandbox-ai mcp add NAME` with its defaults (the catalog's current digest) in the check's own "
          "configuration, a kept session with --mcp NAME, the server started by sandbox-ai (its supervisor, the "
          "bridge, each connection's container or Bubblewrap sandbox), its connection through the session's relay "
          "(the agent side's socket), MCP initialize, tools/list (against servers/NAME/tools.json of the MCP "
          "repository) and the smoke calls of servers/NAME/smoke.json (the calls its CI makes), with Podman and, where "
          "its entry has a Bubblewrap form, Bubblewrap; one workspace with the smoke files for /work and a second one "
          "to prove that one project's instance never sees the other's files. A server that needs a secret or a "
          "network only you can give is skipped with the reason and the command; one that dies before the handshake "
          "shows its supervisor.log",
          ("sandbox", "mcp"), (f"{GAPS}::test_live_mcp_server",), optional=True, timeout=12 * 3600,
          network="the published catalog (raw.githubusercontent.com), every server's image from ghcr.io (sandbox-ai "
                  "mcp add pulls it by digest; Bubblewrap: its rootfs archive from GitHub's releases), the MCP "
                  "repository's smoke data from github.com when no checkout of it is here (--mcp-repo), and what "
                  "the smoke calls of the servers with network reach (search engines, web pages, APIs answering "
                  "made-up keys with a refusal)",
          source="the user, 2026-11-23: \"Binwalk und Radare2 ... beenden sich, bevor sie den Handshake "
                 "beantworten\"; \"in-depth testing aller mcp server ob sie jetzt funktionieren sollten\""),
    Check("cairn-workers-live",
          "Every harness as one of Cairn's workers (claude, codex, opencode, opencode2, hermes, dsh, qwen, goose, "
          "nanobot, letta, pi): real Cairn in a real pod, the harness the lead with --llm and nothing else open, "
          "--cairn --cairn-workers AGENT and a small project made at the start (a code word to state in lower case); "
          "Cairn's dispatcher hands the task through sandbox-ai's relay to the harness's own one-shot run, and the "
          "board must get the fact. Missing images (the harness's, cairn, browser) are built first; OpenHands is "
          "skipped with the reason (as the lead --cairn is refused, beside another lead it needs its own login). "
          "The report has a row per harness: what it gives sandbox/cairn_workers.json",
          ("podman", "llm"), (f"{GAPS}::test_live_cairn_worker",), optional=True, podman_only=True,
          timeout=12 * 3600,
          network="prompts to your LLM server given with --llm, nothing else (--no-provider-defaults); a missing "
                  "image's packages while it is built (Debian, npm, PyPI, the harness's release; Cairn's source "
                  "from GitHub)",
          source="the user, 2026-11-22: \"why workers only for claude and not all harnesses?\""),
    Check("artex-live",
          "ARTEX beside an agent (--artex): its image built from the reviewed source with sandbox-ai's patches when "
          "missing (sandbox-ai build artex), its console forwarded to this computer's 127.0.0.1 only, the password "
          "of sandbox-ai artex console logs in, POST /api/auth/init and /api/update/* gone, another Host or Origin "
          "and a token in the URL refused; in its container user 1000, the loopback only (PostgreSQL on no TCP port), "
          "no password in ARTEX's environment",
          ("podman", "image:codex", "artex"), (f"{GAPS}::test_live_artex_beside_the_agent",), optional=True,
          podman_only=True, timeout=4 * 3600,
          network="the image's build when it is missing: GitHub's archive of ARTEX's commit, npm's registry, Go's "
                  "module proxy and checksum database, snapshot.debian.org, Docker Hub's pinned base images (about "
                  "2 GB); the run itself reaches nothing (its target 192.0.2.1 is a documentation address)",
          source="the user, 2026-11-22: \"check if we can integrate artex ... we still have to be careful\" "
                 "(docs/ARTEX.md)"),
    Check("burp-live",
          "Burp Suite as an MCP server (sandbox-ai mcp add burp). Host mode: your Burp, with the extension of the "
          "signed image loaded and enabled, answers at its endpoint (--burp-url, default http://127.0.0.1:9876/mcp): "
          "mcp add burp says so, mcp burp check names the edition, and through the server's real relay (mcp start, its "
          "socket, mcp_http.py on this host) initialize and tools/list answer with the extension's tools - 27 with "
          "Professional (scanner, Collaborator), 24 with Community - and url_encode is called. Container mode "
          "(--burp-jar JAR): your jar runs headless in the server's container - the terms accepted, a Professional "
          "licence (--burp-license-file, typed into the container only) activated online once - and the same calls "
          "answer through the relay, the licence in no log line of the check",
          ("burp",), (f"{GAPS}::test_live_burp_host_mode", f"{GAPS}::test_live_burp_container_mode"),
          optional=True, podman_only=True, timeout=3600,
          network="host mode: nothing (your Burp on this host's loopback); container mode: the signed image "
                  "(ghcr.io), portswigger.net for a Professional licence's activation (once)",
          source="the user, 2026-11-24: \"Füge mal einen Burp-MCP hinzu ... die Pro-Variante von Burp zu nutzen, mit "
                 "Code\""),
    Check("live-suites",
          "Every other live suite that needs no login (scripts/live_check.py's: the proxy, push check, network "
          "changes, devices, MCP, limits, approvals, namespaces, an escape probe)",
          ("sandbox", "image:codex"), (), optional=True, timeout=4 * 3600,
          network="what those suites reach: api.github.com and github.com (git ls-remote), the hosts their tests "
                  "allow",
          source="scripts/live_check.py"),
)
MAC = "tests/test_live_macos.py"
# The checks of a real Mac (macOS, podman machine, the app built by CI): Linux skips them and says so.
MAC_CHECKS = (
    Check("macos-doctor",
          "sandbox-ai doctor on this Mac: its macOS set (podman machine in place of user namespaces, subordinate ids, "
          "systemd, pasta, Landlock and Bubblewrap - none of those), every check with a state, and its verdict on "
          "podman machine as `podman machine inspect` says it (running, sharing HOME and $TMPDIR); the same through "
          "the app's command line (macos-app's app)",
          ("macos",), (f"{MAC}::test_live_macos_doctor",), timeout=600,
          source="doctor's macOS checks (tests/test_doctor_macos.py: stand-ins only)"),
    Check("macos-devices",
          "sandbox-ai devices scan on this Mac: valid JSON, every listed node exists, nothing offered for --device "
          "that a start here refuses; a start with --device is refused with the reason (the sandbox runs in podman "
          "machine's Linux VM)",
          ("macos",), (f"{MAC}::test_live_macos_devices",), timeout=300,
          source="board-devices on macOS (stand-ins only)"),
    Check("macos-app",
          "sandbox-ai.app (--app, else the one in --dmg-new): its signature holds (codesign --verify), its command "
          "line says its version and that it is the macOS app, the app starts against the sandbox-ai inside it on "
          "this Mac's own display and quits (--self-test)",
          ("macos", "app"), (f"{MAC}::test_live_macos_app_starts",), timeout=900,
          source="the macOS .app (built by CI, started by no test)"),
    Check("macos-update",
          "sandbox-ai.app of --dmg-old, put into a folder of the check's, updates itself to --dmg-new with "
          "`sandbox-ai update` (hdiutil, ditto: the new app's signature holds) from a release the check serves on "
          "127.0.0.1 - SHA256SUMS signed by the release's key (a downloaded release) or by a key of the check's "
          "(a local build) - and `update --rollback` brings the old one back; then the same with the old app's "
          "own Install now… and Roll back… (Settings > Updates, on its own Python and PySide6)",
          ("macos", "dmgs"), (f"{MAC}::test_live_macos_dmg_update_and_rollback",
                              f"{MAC}::test_live_macos_app_install_now_and_roll_back"), timeout=2400,
          source="0.1.154's updates of the macOS app (tests/test_update_github.py: hdiutil, ditto and codesign "
                 "stand-ins)"),
)
CHECKS = CHECKS + MAC_CHECKS
BY_NAME = {check.name: check for check in CHECKS}
LLM_MODELS = ("qwen3.8-flash-next", "ornith-1.5-35b-a3b-mlx", "ornith-1.5-122b-mlx")
# harnesses-live: each harness the launcher knows and how it runs one task without asking ({prompt}: the task), the
# same one-shot forms as tests/live_agents.py's (a test keeps both, and this list and the launcher's agents, the same).
HARNESSES = {
    "claude": ("-p", "{prompt}"),
    "codex": ("exec", "--skip-git-repo-check", "{prompt}"),
    "opencode": ("run", "{prompt}"),
    "opencode2": ("run", "--", "{prompt}"),
    "hermes": ("--oneshot={prompt}",),
    "dsh": ("headless", "{prompt}"),
    "qwen": ("--prompt={prompt}",),
    "goose": ("run", "--quiet", "--text={prompt}"),
    "openhands": ("task", "--text={prompt}"),
    "nanobot": ("agent", "--no-markdown", "--message={prompt}"),
    "letta": ("-p", "--", "{prompt}"),
    "pi": ("-p", "--", "{prompt}"),
}
LLM_CHECKS = ("local-llm", "harnesses-live", "cairn-workers-live")  # use --llm's server (join a whole run)
LLM_USERS = LLM_CHECKS + ("presets-live",)  # presets-live too, with --only presets-live (a day's runs: never joined)
# cairn-workers-live: the harnesses it runs as Cairn's workers (sandbox/cairn.py WORKERS; a test keeps them the same),
# and those it cannot run with --llm, with the reason.
CAIRN_WORKERS = tuple(HARNESSES)
CAIRN_SKIPPED = {"openhands": "as the lead --cairn is refused (its Agent Canvas holds port 8000), and beside another "
                              "lead (--with openhands) it reaches its own provider with its own login, not --llm"}
IMAGES = ("codex", "claude", "opencode")
DEVICE_KINDS = ("serial", "video", "render", "kvm")  # devices-live's own pick, in this order
# Variables of the user's environment that never reach the checks (their sandbox-ai settings and runs).
KEPT_SANDBOX_AI = ("SANDBOX_AI_PODMAN_URL", "SANDBOX_AI_APP_PYTHON")  # (no secret: paths)
# The folders of the check's own; Podman (the wrapper) gets the user's back.
XDG = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")


# --- the checks' own environment ------------------------------------------------------------------------------------

def one_shot(agent, prompt):
    """The arguments after `--` that make AGENT (HARNESSES) do PROMPT once and end."""
    return [part.replace("{prompt}", prompt) for part in HARNESSES[agent]]


# --- presets-live: each built-in preset, started for real (the user, 2026-11-22) -----------------------------------

PRESETS_DIR = ROOT / "sandbox" / "presets"
# What a preset's value of a kind gets in the check without a value of yours: a folder of the check's own (made by
# the test, with one small file in it), the text below, --llm's server; every other kind needs yours (PRESET_VALUES).
SMOKE_TEXT = "the sandbox-ai live check's smoke task"
TOR_PROFILE = "live-tor"  # a VPN value's profile: Tor, made in the check's own configuration (sandbox-ai vpn add)
# Options whose part needs this machine's desktop, which the check's own runtime folder does not have.
DESKTOP = {"--gui": "this desktop's display (--gui)", "--gui-screen": "this desktop's display (--gui-screen)",
           "--audio": "this desktop's sound (--audio)"}
# Options of a start only Podman takes (pasta forwards the host's port; Bubblewrap has no pod for a service, Cairn,
# ARTEX, a helper team or a VPN tunnel).
PODMAN_ONLY = ("--allow-host-port", "--service", "--cairn", "--artex", "--with", "--vpn")
STRICT_MODES = ("high", "paranoid")  # the security modes a start with --llm refuses to widen for the server
SERVICE_READY = "agent-sandbox: service {name} is ready at localhost:"


def presets_module():
    """sandbox/presets.py as a module (its checks, its expansion of values into options)."""
    spec = importlib.util.spec_from_file_location("presets_for_live_check", ROOT / "sandbox" / "presets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def builtin_presets(module=None):
    """{id: preset} of the built-in presets (sandbox/presets/*.json, not *.rules.json), by name: never yours."""
    module = module or presets_module()
    found = {}
    for path in sorted(PRESETS_DIR.glob("*.json")):
        if not path.name.endswith(".rules.json"):
            preset = module.read(path, origin="built-in")
            found[preset["id"]] = {**preset, "_path": str(path), "_origin": "built-in"}
    return found


def by_hand(preset, agent, values, extra=()):
    """The command that starts PRESET yourself with VALUES (a placeholder for each one of yours)."""
    return shlex.join(["sandbox-ai", agent, "--preset", preset["id"],
                       *(part for name, value in values.items() for part in ("--preset-value", f"{name}={value}")),
                       *extra])


def option_pairs(options, flag):
    """The values of FLAG in OPTIONS (["--mcp", "jadx", ...] -> ["jadx"]; "--flag=value" too)."""
    found = []
    for index, option in enumerate(options):
        if option == flag and index + 1 < len(options):
            found.append(options[index + 1])
        elif option.startswith(flag + "="):
            found.append(option.split("=", 1)[1])
    return found


def preset_plan(preset, given=None, llm=None, provider_login=False, folders=None, module=None, gpu=None):
    """How presets-live starts PRESET: {"preset", "agent", "values" (each value it gives: yours from GIVEN, else a
    folder of the check's own in FOLDERS (a Path; its NAME), SMOKE_TEXT, --llm's LLM "SERVER:MODEL", a Tor profile of
    the check's own for a VPN value: "vpn" names it, to add first), "folders"
    (the folders to make), "options" (the launcher's options after the agent: --preset, its values, the model), "mcp"
    [(server, [target the preset mounts])] to add in the check's configuration first, "toolchains" [name] it
    brings (--toolchain: added to the check's store first), "services" [name], "api_keys"
    [secret name: a made-up one of the check's own, its host closed], "backends" (podman, bwrap: where it can run),
    "parts" (what to see come up), "skip" (why it cannot run here, or None), "by_hand" (the command to run it
    yourself)}. GIVEN: {value: text} of --preset-values for this preset. GPU: whether this machine has one (None:
    looked up)."""
    module = module or presets_module()
    given = dict(given or {})
    agent = (preset.get("agents") or module.AGENTS)[0]
    folders = Path(folders) if folders else Path("/nonexistent")
    values, made, yours = {}, [], {}
    plan = {"preset": preset["id"], "agent": agent, "values": values, "folders": made, "options": [], "mcp": [],
            "toolchains": [], "services": [], "api_keys": [], "backends": ["podman", "bwrap"], "podman_only": [], "parts": [],
            "vpn": None, "skip": None, "by_hand": ""}
    for name, spec in (preset.get("values") or {}).items():
        kind = spec.get("kind", "text")
        if name in given:
            values[name] = str(given[name])
        elif kind == "llm" and (not spec.get("optional") or "default" in spec):
            if not llm:
                plan["by_hand"] = by_hand(preset, agent, {name: "SERVER:MODEL"})
                plan["skip"] = f"needs a model: --llm SERVER (its value {name}); or run: {plan['by_hand']}"
                return plan
            values[name] = llm
        elif spec.get("optional") or spec.get("default") not in (None, "") or kind == "session":
            continue  # its default, or left out (an optional value)
        elif kind == "folder":
            values[name] = str(folders / name)
            made.append(str(folders / name))
        elif kind == "vpn":  # a Tor profile of the check's own (no key in it); yours are never copied
            values[name] = TOR_PROFILE
            plan["vpn"] = TOR_PROFILE
        elif kind == "text":
            values[name] = SMOKE_TEXT
        else:
            yours[name] = kind.upper()
    plan["by_hand"] = by_hand(preset, agent, {**values, **yours})
    if yours:
        names = ", ".join(f"{name} ({(preset['values'][name].get('label') or name)})" for name in yours)
        plan["skip"] = (f"needs a value of yours: {names}; give it in --preset-values FILE "
                        f"({{\"{preset['id']}\": {{\"{next(iter(yours))}\": \"...\"}}}}) or run: {plan['by_hand']}")
        return plan
    try:
        options = module.expand(preset, values)
    except ValueError as exc:
        plan["skip"] = f"its values: {exc}"
        return plan
    for flag, what in DESKTOP.items():
        if flag in options:
            plan["skip"] = f"needs {what}, which the check's own runtime folder does not reach; run: {plan['by_hand']}"
            return plan
    licensed = [value for value in option_pairs(options, "--toolchain") if module.licensed_toolchain(value)] \
        if hasattr(module, "licensed_toolchain") else []
    if licensed:  # (built from its publisher's downloads under a license: never accepted by a check for you)
        plan["skip"] = (f"needs a license of yours: the toolchain {licensed[0]} is built from "
                        f"{module.licensed_toolchain(licensed[0])['by']}'s downloads once you accepted the "
                        f"{module.licensed_toolchain(licensed[0])['title']} (sandbox-ai toolchain add {licensed[0]}; "
                        f"the check toolchain-live does it with --accept-android-sdk-license); run: {plan['by_hand']}")
        return plan
    needs_variant = [need for need in preset.get("needs") or [] if "image variant" in need]
    if needs_variant or "--image-variant" in options:
        plan["skip"] = ("needs an image variant of yours, built once (" + (needs_variant[0] if needs_variant else
                        "--image-variant") + f"); run: {plan['by_hand']}")
        return plan
    if "--gpu" in options and not (gpu if gpu is not None else gpu_found()):
        plan["skip"] = f"needs a GPU (--gpu): none found on this machine; run it where one is: {plan['by_hand']}"
        return plan
    mounts = {}
    for value in option_pairs(options, "--mcp-mount"):
        server, _, rest = value.partition(":")
        mounts.setdefault(server, []).append(rest.split("=", 1)[0])
    plan["mcp"] = [(server, mounts.get(server, [])) for server in dict.fromkeys(option_pairs(options, "--mcp"))]
    plan["toolchains"] = list(dict.fromkeys(option_pairs(options, "--toolchain")))
    plan["services"] = [value.split(":", 1)[0] for value in option_pairs(options, "--service")]
    plan["api_keys"] = [value.split("=", 1)[1] for value in option_pairs(options, "--api-key") if "=" in value]
    plan["podman_only"] = [flag for flag in PODMAN_ONLY if flag in options]
    if plan["podman_only"]:
        plan["backends"] = ["podman"]
    plan["parts"] = [f"MCP server {server}" for server, _ in plan["mcp"]] + \
        [f"service {name}" for name in plan["services"]]
    model = [] if provider_login or "--llm" in options else ["--llm", llm, "--no-provider-defaults"] if llm else None
    if model is None:
        plan["skip"] = "needs a model: --llm SERVER (or --provider-login for the agent's own provider)"
        return plan
    if model and "--model" in options:
        plan["skip"] = ("names its provider's model (--model), which a start with --llm refuses: run it with "
                        f"--provider-login, or yourself: {plan['by_hand']}")
        return plan
    if "paranoid" in option_pairs(options, "--mode"):
        plan["skip"] = ("its --mode paranoid asks you before every action of the agent, which a smoke run without "
                        f"you cannot answer: run it yourself: {plan['by_hand']}")
        return plan
    strict = [mode for mode in option_pairs(options, "--mode") if mode in STRICT_MODES]
    if model and strict:
        plan["skip"] = (f"its --mode {strict[0]} opens no LLM server of yours (the launcher refuses --llm there): run "
                        f"it with --provider-login, or yourself: {plan['by_hand']}")
        return plan
    if model and plan["vpn"]:
        plan["skip"] = (f"its VPN value gets a Tor profile of the check's own ({plan['vpn']}), and Tor does not reach "
                        "an LLM server in your network (the launcher refuses it; only you may let it past Tor): run "
                        f"it with --provider-login, or yourself: {plan['by_hand']}")
        return plan
    others = [flag for flag in ("--helper", "--with") if flag in options]
    if model and others:
        plan["skip"] = (f"its {others[0]} agent reaches its own provider, which --llm keeps out of the run: run it "
                        f"with --provider-login, or yourself: {plan['by_hand']}")
        return plan
    plan["options"] = ["--preset", preset["id"],
                       *(part for name, value in values.items() for part in ("--preset-value", f"{name}={value}")),
                       *model, *([] if provider_login else ["--no-auth"])]
    return plan


def gpu_found(dev="/dev"):
    """Whether this machine has a GPU a start's --gpu can hand over (a DRI render node, an NVIDIA or AMD KFD node)."""
    dev = Path(dev)
    return any(dev.glob("dri/renderD*")) or (dev / "nvidia0").exists() or (dev / "kfd").exists()


def parts_seen(plan, stderr):
    """[(part, seen)] of PLAN's parts in a start's STDERR: each MCP server's "started", each service's "ready"."""
    found = []
    for server, _ in plan["mcp"]:
        found.append((f"MCP server {server}", bool(re.search(
            re.escape("agent-sandbox: MCP server ") + r"\S*" + re.escape(server) + r"\S* started", stderr))))
    for name in plan["services"]:
        found.append((f"service {name}", SERVICE_READY.format(name=name) in stderr))
    return found


def preset_values(path):
    """{preset: {value: text}} of --preset-values FILE (JSON); ValueError says what is wrong."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"--preset-values {path}: {exc}") from None
    if not isinstance(document, dict) or not all(
            isinstance(name, str) and isinstance(found, dict) and
            all(isinstance(key, str) and isinstance(value, (str, int, float)) for key, value in found.items())
            for name, found in document.items()):
        raise ValueError(f"--preset-values {path}: {{\"PRESET\": {{\"VALUE\": \"TEXT\"}}}}")
    return {name: {key: str(value) for key, value in found.items()} for name, found in document.items()}


# --- mcp-live: every server of the signed catalog through a start's path (the user, 2026-11-23) ---------------------

MCP_PREFIX = "mcp"  # the signed catalog of sandbox-ai-mcp (the hub's is axross's)
MCP_REPOSITORY = "https://github.com/c0decave/sandbox-ai-mcp.git"
MCP_VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)\}")
MCP_SECRET_WORDS = re.compile(r"(?i)token|secret|passw|api_?key|credential|cookie|session|hash")
MCP_MARKERS = ("only-in-workspace-a.txt", "only-in-workspace-b.txt")  # the two workspaces' own files


def mcp_repo(given=None, root=ROOT):
    """The MCP repository's checkout whose servers/NAME/smoke.json and tools.json the check takes: GIVEN
    (--mcp-repo: that one only - a wrong path is said, never replaced by another checkout), else
    projects/sandbox-ai-mcp of this checkout; None when it has no servers/ (cloned then, unless GIVEN)."""
    for candidate in [Path(given).expanduser()] if given else [Path(root) / "projects" / "sandbox-ai-mcp"]:
        if (candidate / "servers").is_dir():
            return candidate.resolve()
    return None


def mcp_spec(repo, name):
    """servers/NAME/smoke.json of the MCP checkout REPO ({} without one: initialize and tools/list only)."""
    try:
        spec = json.loads((Path(repo) / "servers" / name / "smoke.json").read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return spec if isinstance(spec, dict) else {}


def mcp_tools(repo, name):
    """The tools servers/NAME/tools.json records (tools/list must offer exactly these), or None without one."""
    try:
        tools = json.loads((Path(repo) / "servers" / name / "tools.json").read_text()).get("tools")
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return [str(tool) for tool in tools] if isinstance(tools, list) else None


def mcp_variables(value):
    """The ${NAME}s VALUE (a spec's part) uses."""
    return sorted(set(MCP_VARIABLE.findall(json.dumps(value))))


def mcp_by_hand(name, backend, extra=()):
    """The commands that test server NAME yourself (its secrets yours): add it, start an agent with it."""
    return (f"sandbox-ai mcp add {name}" + "".join(f" {shlex.quote(part)}" for part in extra)
            + f"; sandbox-ai claude --backend {backend} --mcp {name}")


def mcp_behind(plan, entry, backend, vpn):
    """PLAN (mcp_plan's) for a check behind the Tor profile VPN (--mcp-vpn: the path of a server added with --vpn,
    the user's gowitness-tor, 2026-11-25): `mcp add --vpn VPN` - each connection's container in the namespace of its
    own Tor container (NAME-VPN); skipped where nothing goes through it (no network), under Bubblewrap (no VPN there)
    and where its catalog says it does not work behind Tor; a call about an .onion address (which only a start without
    Tor refuses) is left out."""
    name = plan["server"]
    if str(entry.get("network") or "none") in ("none", "offline"):
        plan["skip"] = f"it reaches no network: nothing of it goes through {vpn} (mcp-live without --mcp-vpn tests it)"
        return plan
    if backend == "bwrap":
        plan["skip"] = "no VPN under Bubblewrap: behind a VPN it runs with Podman only"
        return plan
    tor = entry.get("tor") if isinstance(entry.get("tor"), dict) else {}
    if tor.get("support") == "no":
        plan["skip"] = f"its catalog says it does not work behind Tor: {tor.get('note') or '-'}"
        return plan
    plan["add"] += ["--vpn", vpn]
    plan["vpn"] = vpn
    plan["by_hand"] = mcp_by_hand(name, backend, ["--vpn", vpn])
    kept = []
    for call in plan["calls"]:
        if ".onion" in json.dumps(call.get("arguments") or {}):
            plan["skipped_calls"].append((call.get("tool"), "behind Tor an .onion address is reached: this call checks "
                                                            "its refusal without Tor"))
        else:
            kept.append(call)
    plan["calls"] = kept
    return plan


def mcp_plan(entry, spec, backend, tools=None, vpn=None):
    """How mcp-live tests catalog ENTRY (`mcp catalog --json`'s) with BACKEND from its smoke SPEC (mcp_spec) and its
    recorded TOOLS: {"server", "add" (mcp add's options: --backend both where it has a Bubblewrap form, a folder its
    smoke spec mounts otherwise than its default), "env" ([(VAR, VALUE, secret)]: the spec's made-up values - a secret
    one kept as a secret of the check's own, --mcp-env NAME:VAR@SECRET), "work" ("fixtures", "seed" [items], "empty"
    or None: what its /work gets), "folders" ({target: "fixtures" | [seed items]}: another folder the spec fills),
    "writable" (/work writable, as its spec mounts it), "calls" ([call]: those for this backend's runner), "skipped
    calls" ([(tool, why)]), "expect_tools", "tools", "isolation" (it works on the start's workspace: a second
    workspace proves the other never reaches it), "skip" (why it cannot run here, or None), "by_hand", "vpn"}. VPN:
    the Tor profile it is added behind (--mcp-vpn: mcp_behind)."""
    name = entry.get("name")
    plan = {"server": name, "add": [], "env": [], "work": None, "folders": {}, "writable": False, "calls": [],
            "skipped_calls": [], "expect_tools": [str(t) for t in spec.get("expect_tools") or []], "tools": tools,
            "isolation": any(m.get("target") == "/work" for m in entry.get("mounts") or [] if isinstance(m, dict)),
            "skip": None, "by_hand": mcp_by_hand(name, backend), "start_timeout": spec.get("start_timeout", 60),
            "vpn": None}
    if not entry.get("published"):
        plan["skip"] = f"not published (no signed image yet): sandbox-ai mcp add {name} --build builds it yourself"
        return plan
    if backend == "bwrap" and not entry.get("bubblewrap"):
        plan["skip"] = "its catalog entry has no Bubblewrap form (rootfs): Podman only"
        return plan
    if entry.get("bubblewrap"):
        plan["add"] += ["--backend", "both"]
    yours = [v for v in mcp_variables({"env": spec.get("env") or {}, "calls": spec.get("calls") or []})
             if v != "FIXTURES"]
    declared = {v.get("name"): v for v in entry.get("env") or [] if isinstance(v, dict)}
    given = dict(spec.get("env") or {})
    needed = [var for var, v in declared.items() if v.get("required") and var not in given]
    if yours or needed:
        names = ", ".join(f"{var} ({'a secret' if (declared.get(var) or {}).get('secret') or MCP_SECRET_WORDS.search(var) else 'a value'})"
                          for var in yours + needed)
        plan["by_hand"] = mcp_by_hand(name, backend, [part for var in yours + needed
                                                      for part in ("--env", f"{var}@{var}")])
        plan["skip"] = (f"needs a value of yours: {names}; keep it with sandbox-ai secrets set NAME, then run: "
                        f"{plan['by_hand']}")
        return plan
    for var, value in given.items():
        if not isinstance(value, str) or value == "":
            continue  # (an empty one: left out, as a server without it starts)
        secret = bool(MCP_SECRET_WORDS.search(var)) or bool((declared.get(var) or {}).get("secret"))
        plan["env"].append((var, value, secret))
    modes = {m.get("target"): m.get("mode", "ro") for m in entry.get("mounts") or [] if isinstance(m, dict)}
    for mount in spec.get("mounts") or []:
        target = mount.get("target")
        content = "fixtures" if "${FIXTURES}" in str(mount.get("host", "")) else list(mount.get("seed") or [])
        if target == "/work":
            plan["work"] = "fixtures" if content == "fixtures" else "seed" if content else "empty"
            plan["seed"] = content if isinstance(content, list) else []
            if mount.get("writable") and modes.get("/work") != "rw":
                plan["writable"] = True
                plan["add"] += ["--mount", "/work=@workspace:rw"]  # (as its smoke spec mounts it: the calls write)
        elif content:  # (a folder the spec fills: a folder of the check's own, else its default stays)
            plan["folders"][target] = content
        elif mount.get("writable") and modes.get(target) != "rw":
            plan["folders"][target] = []
    runner = f"sandbox-ai-{backend}"
    for call in spec.get("calls") or []:
        if call.get("runners") and runner not in call["runners"]:
            plan["skipped_calls"].append((call.get("tool"), f"not for {runner}: {call.get('runners_why')}"))
        else:
            plan["calls"].append(call)
    return mcp_behind(plan, entry, backend, vpn) if vpn else plan


def mcp_call_passed(call, answer):
    """(passed, text, limited) of smoke CALL's ANSWER ({"result": ...} or {"error": ...}), as the MCP repository's
    smoke_test.py judges it: its expect in the text and no JSON-RPC error; with expect_error the text alone; LIMITED: the
    upstream_limited text the server wrote when its service refused for its own reasons (rate limits: not this
    server's failure)."""
    if not isinstance(answer, dict):
        return False, "no answer", None
    if "error" in answer:
        text = "JSON-RPC error: " + json.dumps(answer["error"])
    else:
        content = (answer.get("result") or {}).get("content") or []
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    expect = str(call.get("expect") or "")
    passed = expect in text if call.get("expect_error") else expect in text and "error" not in answer
    limited = None if passed or "error" in answer else next(
        (m for m in call.get("upstream_limited") or [] if isinstance(m, str) and m in text), None)
    return passed, text, limited


def mcp_rows(facts):
    """mcp-live per server and backend: [{"server", "backend", "added", "started", "handshake", "tools", "calls",
    "isolation", "seconds", "why", "supervisor"}] from what each test wrote into FACTS (SAI_LIVE_MCP_DIR/
    NAME.BACKEND.json)."""
    rows = []
    for path in sorted(Path(facts).glob("*.json")):
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(found, dict) or not isinstance(found.get("server"), str) or \
                found.get("backend") not in ("podman", "bwrap"):
            continue
        calls = found.get("calls") or {}
        rows.append({"server": found["server"], "backend": found["backend"],
                     "outcome": found.get("outcome") or "?", "vpn": found.get("vpn") or "",
                     "added": found.get("added") or "-", "started": found.get("started") or "-",
                     "handshake": found.get("handshake") or "-", "tools": found.get("tools") or "-",
                     "calls": (f"{calls.get('passed', 0)} passed, {calls.get('failed', 0)} failed"
                               + (f", {calls['skipped']} skipped" if calls.get("skipped") else "")
                               + (f" ({calls['first_failure']})" if calls.get("first_failure") else ""))
                     if calls else "-",
                     "isolation": found.get("isolation") or "-", "seconds": found.get("seconds"),
                     "why": found.get("why") or "", "supervisor": found.get("supervisor") or ""})
    return rows


def mcp_catalog_names(catalog, only=None):
    """The names of the signed catalog's servers (`mcp catalog --json`'s entries of MCP_PREFIX) mcp-live tests: ONLY's
    (--mcp-servers) where given, in the catalog's order."""
    names = [e.get("name") for e in (catalog or {}).get("entries") or []
             if isinstance(e, dict) and e.get("prefix") == MCP_PREFIX and isinstance(e.get("name"), str)]
    return [name for name in names if not only or name in only]


def podman_wrapper(directory, real, podman):
    """DIRECTORY/podman: runs PODMAN with REAL's HOME and XDG folders (unset where REAL has none), so the user's
    image storage, configuration and rootless runtime are Podman's while sandbox-ai runs with the check's own. The
    path of the file written."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/sh", "# scripts/live_gaps.py: Podman with your own HOME, configuration and image storage; the "
             "check's sandbox-ai has its own."]
    for name in ("HOME",) + XDG:
        value = real.get(name)
        lines.append(f"{name}={shlex.quote(value)}; export {name}" if value else f"unset {name}")
    lines.append(f"exec {shlex.quote(str(podman))} \"$@\"")
    path = directory / "podman"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o700)
    return path


def isolated_environment(root, base=None, home=True, podman=None):
    """The environment of a check: BASE (default os.environ) without its SANDBOX_AI_* settings (but
    KEPT_SANDBOX_AI), with HOME (when HOME is true) and every XDG folder in ROOT, secrets kept in plain files of its
    own (never the system keyring), and ROOT/bin first on PATH with the Podman wrapper (PODMAN: the podman to wrap,
    default the one on BASE's PATH; none found: no wrapper)."""
    base = dict(os.environ if base is None else base)
    root = Path(root)
    env = {key: value for key, value in base.items()
           if not key.startswith("SANDBOX_AI_") or key in KEPT_SANDBOX_AI}
    folders = {"HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state",
               "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "run"}
    for name, folder in folders.items():
        if name == "HOME" and not home:
            continue
        path = root / folder
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[name] = str(path)
    podman = podman or shutil.which("podman", path=base.get("PATH"))
    if podman:
        bin_dir = podman_wrapper(root / "bin", base, podman).parent
        env["PATH"] = os.pathsep.join([str(bin_dir), base.get("PATH") or os.defpath])
    env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1", SANDBOX_AI_LIVE_CHECK="1")
    return env


# --- prerequisites ------------------------------------------------------------------------------------------------

class Found(NamedTuple):
    ok: bool
    detail: str


def run_command(command, timeout=60, env=None, cwd=None, input=None):
    """(status, stdout, stderr); 127 when the program is missing, 124 after TIMEOUT."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd,
                              input=input, stdin=None if input is not None else subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr
    except FileNotFoundError:
        return 127, "", f"{command[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"


def read_number(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def user_namespaces(proc="/proc", runner=run_command):
    """Found: whether this user may make user namespaces (rootless Podman and Bubblewrap need them)."""
    proc = Path(proc)
    limit = read_number(proc / "sys/user/max_user_namespaces")
    if limit == 0:
        return Found(False, "user.max_user_namespaces is 0: sudo sysctl -w user.max_user_namespaces=28633 (and in "
                            "/etc/sysctl.d to keep it)")
    clone = read_number(proc / "sys/kernel/unprivileged_userns_clone")
    if clone == 0:
        return Found(False, "kernel.unprivileged_userns_clone is 0: sudo sysctl -w kernel.unprivileged_userns_clone=1")
    restricted = read_number(proc / "sys/kernel/apparmor_restrict_unprivileged_userns") == 1
    status, _, err = runner(["unshare", "--user", "--map-root-user", "true"], timeout=30)
    if status == 127:
        return Found(True, "unshare is not installed: not tried")
    if status != 0 and restricted:
        # Ubuntu 24.04 and later: a program without an AppArmor profile of its own gets none; Podman has its profile.
        return Found(True, "AppArmor restricts unprivileged user namespaces to programs with a profile (Podman has "
                           "one; Bubblewrap needs its own)")
    if status != 0:
        return Found(False, "unshare --user failed: " + (err.strip().splitlines() or ["?"])[-1][:300])
    return Found(True, "unshare --user works")


def subordinate_ids(user, uid, etc="/etc"):
    """Found: the user's ranges in /etc/subuid and /etc/subgid (rootless Podman maps its users with them)."""
    missing = []
    for name in ("subuid", "subgid"):
        try:
            lines = Path(etc, name).read_text().splitlines()
        except OSError:
            lines = []
        if not any(line.split(":")[0] in (user, str(uid)) for line in lines if line.strip()):
            missing.append(name)
    if missing:
        return Found(False, f"no range for {user} in /etc/{' and /etc/'.join(missing)}: sudo usermod --add-subuids "
                            f"100000-165535 --add-subgids 100000-165535 {user}")
    return Found(True, "ranges in /etc/subuid and /etc/subgid")


def podman_facts(env, runner=run_command):
    """(Found, facts) of `podman info`: installed, rootless, its version and cgroup version."""
    status, out, err = runner(["podman", "info", "--format", "json"], timeout=120, env=env)
    if status == 127:
        return Found(False, "podman is not installed (your distribution's package podman)"), {}
    if status != 0:
        return Found(False, "podman info failed: " + (err.strip().splitlines() or ["?"])[-1][:300]), {}
    try:
        info = json.loads(out)
    except ValueError:
        return Found(False, "podman info did not answer JSON"), {}
    host = info.get("host") or {}
    facts = {"podman": (info.get("version") or {}).get("Version"), "cgroup": host.get("cgroupVersion"),
             "rootless": (host.get("security") or {}).get("rootless")}
    if facts["rootless"] is False:
        return Found(False, "podman is not rootless here: run the check as your own user"), facts
    return Found(True, f"podman {facts['podman']}, rootless, cgroup {facts['cgroup']}"), facts


def prerequisites(env, args, system=None, runner=run_command, proc="/proc", etc="/etc", user=None, uid=None,
                  which=shutil.which):
    """{key: Found} of everything a check may need (Check.needs), and facts about this machine. ENV: the checks'
    environment (its PATH has the Podman wrapper)."""
    system = system or platform.system()
    uid = os.getuid() if uid is None else uid
    user = user or os.environ.get("USER") or str(uid)
    found, facts = {}, {"system": system, "kernel": platform.release(), "python": platform.python_version()}
    found["linux"] = Found(system == "Linux", "Linux" if system == "Linux" else
                           f"{system}: this check needs Linux" + (" (on a Mac the macos-* checks run)"
                                                                  if system == "Darwin" else ""))
    found["macos"] = macos_found(system, runner, which, facts)
    if system == "Linux":
        found["userns"] = user_namespaces(proc, runner)
        found["subuid"] = subordinate_ids(user, uid, etc)
    else:
        found["userns"] = found["subuid"] = Found(False, "Linux only")
    podman, podman_info = podman_facts(env, runner)
    facts.update({key: value for key, value in podman_info.items() if value is not None})
    if system == "Darwin":
        found["podman-machine"] = machine_found(env, runner, podman)
        podman = Found(False, "on a Mac Podman runs in podman machine's Linux VM: the Linux checks run on Linux "
                              "(here: the macos-* checks)")
    elif podman.ok and not (found["userns"].ok and found["subuid"].ok):
        podman = Found(False, "rootless Podman needs user namespaces and subordinate ids (above)")
    found["podman"] = podman
    status, out, err = (127, "", "") if system == "Darwin" else runner(["bwrap", "--version"], timeout=30, env=env)
    if system == "Darwin":
        found["bwrap"] = Found(False, "Linux only (Bubblewrap)")
    elif status != 0:
        found["bwrap"] = Found(False, "bwrap is not installed (package bubblewrap)" if status == 127 else
                               f"bwrap --version failed: {(err or out).strip()[-200:]}")
    elif not found["userns"].ok:
        found["bwrap"] = Found(False, f"{out.strip()}: needs user namespaces (above)")
    else:
        version = out.strip()
        status, _, err = runner(["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"], timeout=30, env=env)
        found["bwrap"] = Found(status == 0, version if status == 0 else
                               f"{version} cannot make a sandbox here: {(err.strip().splitlines() or ['?'])[-1][:300]}")
    found["sandbox"] = Found(podman.ok or found["bwrap"].ok, "Podman" if podman.ok else "Bubblewrap only" if
                             found["bwrap"].ok else "neither Podman nor Bubblewrap can start a sandbox here")
    for image in IMAGES:
        if not podman.ok:
            found[f"image:{image}"] = Found(False, "no Podman")
            continue
        status, _, _ = runner(["podman", "image", "exists", f"localhost/{image}-sandbox:latest"], timeout=60, env=env)
        found[f"image:{image}"] = Found(status == 0, f"localhost/{image}-sandbox" if status == 0 else
                                        f"missing: sandbox-ai build {image} (or --build)")
    status, out, _ = runner(["git", "--version"], timeout=30, env=env)
    found["git"] = Found(status == 0, out.strip() if status == 0 else "git is not installed")
    found["host"] = Found(False, "give --host NAME (a host of sandbox-ai hosts)")
    found["llm"] = Found(False, "give --llm NAME (a server of sandbox-ai llm)")
    found["presets"] = Found(True, "no values of yours (--preset-values FILE gives them)")
    found["mcp"] = Found(False, "give --only mcp-live (asked first)")
    found["model"] = Found(False, "give --llm NAME (a server of sandbox-ai llm) or --provider-login")
    found["device"] = Found(False, "give --device PATH")
    kvm = Path("/dev/kvm")
    found["kvm"] = Found(kvm.exists() and os.access(kvm, os.R_OK | os.W_OK),
                         "/dev/kvm" if kvm.exists() and os.access(kvm, os.R_OK | os.W_OK) else
                         "no /dev/kvm your user can open for reading and writing (the emulator needs KVM)")
    accepted = bool(getattr(args, "accept_android_sdk_license", False))
    if accepted:
        env["SAI_LIVE_ANDROID_SDK_LICENSE"] = "accepted"
    found["android-license"] = Found(accepted, "accepted with --accept-android-sdk-license" if accepted else
                                     "give --accept-android-sdk-license: what the check fetches from Google is under "
                                     "the Android Software Development Kit License Agreement "
                                     "(https://developer.android.com/studio/terms), yours to accept")
    found["artex"] = Found(False, "run it with --only artex-live (it asks first: it builds ARTEX's image when missing)")
    found["burp"] = Found(False, "run it with --only burp-live: your Burp with the extension (host mode) and/or "
                                 "--burp-jar JAR (container mode)")
    found["dmgs"] = Found(False, "give --dmg-old DMG --dmg-new DMG (two disk images of sandbox-ai.app, the old one "
                                 "0.1.154 or later: built here or downloaded with the release's SHA256SUMS and "
                                 "SHA256SUMS.sig)")
    found["app"] = Found(False, "give --app PATH (a sandbox-ai.app) or --dmg-new DMG")
    status, out, err = runner([str(SAI), "--version"], timeout=60, env=env)
    facts["sandbox-ai"] = (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {status}"
    status, out, _ = runner(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], timeout=30, env=env)
    if status == 0:
        facts["commit"] = out.strip()
    facts["distribution"] = distribution(etc, system)
    return found, facts


def macos_found(system, runner=run_command, which=shutil.which, facts=None):
    """Found: this is a Mac with what the macOS checks use (hdiutil, ditto, codesign: part of macOS); its version
    and architecture go into FACTS."""
    if system != "Darwin":
        return Found(False, f"{system}: the macOS checks run on a Mac")
    status, out, _ = runner(["sw_vers", "-productVersion"], timeout=30)
    version = out.strip() if status == 0 and out.strip() else "?"
    if facts is not None:
        facts.update(macos=version, arch=platform.machine())
    missing = [tool for tool in ("hdiutil", "ditto", "codesign") if not which(tool)]
    if missing:
        return Found(False, f"macOS {version}: {', '.join(missing)} not found (part of macOS: /usr/bin)")
    # sandbox-ai's starts want bash 4 or later (macOS ships 3.2): the one on PATH or Homebrew's (main.find_bash)
    for bash in dict.fromkeys(filter(None, [which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash"])):
        status, out, _ = runner([bash, "-c", "echo ${BASH_VERSINFO[0]}"], timeout=30)
        if status == 0 and out.strip().isdigit() and int(out.strip()) >= 4:
            return Found(True, f"macOS {version} ({platform.machine()}), hdiutil, ditto, codesign, bash "
                               f"{out.strip()} ({bash})")
    return Found(False, f"macOS {version}: sandbox-ai needs bash 4 or later (macOS ships 3.2): brew install bash")


def machine_found(env, runner=run_command, podman=None):
    """Found: podman machine, the Linux VM Podman runs in on a Mac (what doctor's macOS checks look at; no check
    needs it to run - macos-doctor compares doctor's verdict with it either way)."""
    status, out, err = runner(["podman", "machine", "inspect"], timeout=60, env=env)
    if status == 127:
        return Found(False, "podman is not installed (podman.io's installer or brew install podman)")
    try:
        machines = json.loads(out) if status == 0 else None
    except ValueError:
        machines = None
    if not isinstance(machines, list) or not machines or not isinstance(machines[0], dict):
        return Found(False, "no podman machine: podman machine init; podman machine start" + (
            f" ({(err.strip().splitlines() or [''])[-1][:200]})" if err.strip() else ""))
    machine = machines[0]
    state = machine.get("State") or "?"
    resources = machine.get("Resources") or {}
    detail = f"{machine.get('Name') or '?'} {state}, {resources.get('CPUs') or '?'} CPUs"
    return Found(state == "running", detail if state == "running" else f"{detail}: podman machine start")


def release_assets_module():
    spec = importlib.util.spec_from_file_location("release_assets_for_live_check", ROOT / "sandbox/release_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dmg_file(given, machine=None):
    """(the disk image, None) of GIVEN - a .dmg, or a folder holding one (a release's: this Mac's
    sandbox-ai-ARCH.dmg) - or (None, why not)."""
    path = Path(given).expanduser()
    if path.is_file() and path.suffix == ".dmg":
        return path.resolve(), None
    if not path.is_dir():
        return None, f"{given}: neither a .dmg nor a folder"
    assets = release_assets_module()
    arch = assets.arch_of("macos-app", machine)
    wanted = path / assets.dmg(arch) if arch else None
    if wanted is not None and wanted.is_file():
        return wanted.resolve(), None
    images = sorted(path.glob("*.dmg"))
    if len(images) == 1:
        return images[0].resolve(), None
    return None, f"{given}: " + (f"no {wanted.name} in it" if wanted is not None else "no disk image for this "
                                 "machine in it") + (f" (it has {', '.join(p.name for p in images)})" if images else "")


def signed_release(dmg):
    """Whether the folder of DMG is a release as it was downloaded: SHA256SUMS listing DMG's name with its SHA-256,
    and SHA256SUMS.sig - then the update is checked with the release's own key; else the check signs a SHA256SUMS
    of its own (a local build)."""
    dmg = Path(dmg)
    sums, signature = dmg.parent / "SHA256SUMS", dmg.parent / "SHA256SUMS.sig"
    if not (sums.is_file() and signature.is_file()):
        return False
    listed = release_assets_module().checksums(sums.read_text(errors="replace"))
    return listed.get(dmg.name) == file_sha256(dmg)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_dmgs(args, env, machine=None):
    """Found: macos-update's two disk images (SAI_LIVE_DMG_OLD, SAI_LIVE_DMG_NEW)."""
    if not (args.dmg_old and args.dmg_new):
        return Found(False, "give --dmg-old DMG --dmg-new DMG (two disk images of sandbox-ai.app, the old one 0.1.154 "
                            "or later: built here or downloaded with the release's SHA256SUMS and SHA256SUMS.sig)")
    said = []
    for option, given, variable in (("--dmg-old", args.dmg_old, "SAI_LIVE_DMG_OLD"),
                                    ("--dmg-new", args.dmg_new, "SAI_LIVE_DMG_NEW")):
        dmg, problem = dmg_file(given, machine)
        if problem:
            return Found(False, f"{option} {problem}")
        env[variable] = str(dmg)
        said.append(f"{option} {dmg.name} ({'a signed release' if signed_release(dmg) else 'a local build'})")
    if env["SAI_LIVE_DMG_OLD"] == env["SAI_LIVE_DMG_NEW"]:
        return Found(False, "--dmg-old and --dmg-new are the same disk image")
    return Found(True, "; ".join(said))


def prepare_app(args, env):
    """Found: macos-app's sandbox-ai.app - --app (SAI_LIVE_APP), else the one in --dmg-new."""
    if args.app:
        app = Path(args.app).expanduser()
        if not (app / "Contents" / "MacOS" / "sandbox-ai").is_file():
            return Found(False, f"--app {args.app}: not a sandbox-ai.app (no Contents/MacOS/sandbox-ai)")
        env["SAI_LIVE_APP"] = str(app.resolve())
        return Found(True, f"{app.name} (--app; only read and started)")
    if env.get("SAI_LIVE_DMG_NEW"):
        return Found(True, f"the app in {Path(env['SAI_LIVE_DMG_NEW']).name} (--dmg-new), put into the check's folder")
    return Found(False, "give --app PATH (a sandbox-ai.app) or --dmg-new DMG")


def distribution(etc="/etc", system=None):
    if (system or platform.system()) == "Darwin":
        return "macOS"
    try:
        for line in Path(etc, "os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def missing_for(check, found, backends):
    """The prerequisites CHECK lacks (each "key: detail"); images only where Podman is used."""
    missing = []
    for key in check.needs:
        if key.startswith("image:") and "podman" not in backends:
            continue
        state = found.get(key) or Found(False, "unknown")
        if not state.ok:
            missing.append(f"{key}: {state.detail}")
    if check.podman_only and "podman" not in backends and not any(m.startswith("podman:") for m in missing):
        missing.append("podman: this check runs with Podman only")
    return missing


def backends_of(choice, found):
    """The backends to run with: CHOICE (podman, bwrap, both) or, with auto, each one that works here."""
    if choice == "auto":
        return [name for name in ("podman", "bwrap") if found[name].ok]
    return ["podman", "bwrap"] if choice == "both" else [choice]


# --- the optional sections' inputs --------------------------------------------------------------------------------

def real_config(env=None):
    env = os.environ if env is None else env
    return Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config") / "sandbox-ai"


def llm_endpoint(name, config):
    """(entry, problem) of the LLM endpoint NAME in CONFIG/llm.json (read, never changed): entry {"name", "record",
    "ca"} without anything secret; problem says why it cannot be used by the check (a key or header kept as a secret,
    a VPN profile: never copied into the check's own HOME)."""
    try:
        store = json.loads((Path(config) / "llm.json").read_text())
    except FileNotFoundError:
        return None, "no LLM server is configured (sandbox-ai llm add)"
    except (OSError, ValueError) as exc:
        return None, f"llm.json: {exc}"
    endpoints = store.get("endpoints") if isinstance(store, dict) else None
    if not isinstance(endpoints, dict) or name not in endpoints:
        names = ", ".join(sorted(endpoints or {})) or "none"
        return None, f"no LLM server {name!r} (configured: {names})"
    record = dict(endpoints[name])
    secret = [key for key in ("api_key_secret", "headers", "vpn") if record.get(key)]
    if secret:
        return None, (f"the server {name} uses {', '.join(secret)}: its secrets and VPN profiles are never copied into "
                      "the check's own HOME; run the smoke runs by hand (docs/LIVE_CHECK.md)")
    ca = record.get("ca")
    ca_path = None
    if ca:
        ca_path = Path(config) / "llm" / Path(ca).name if not Path(ca).is_absolute() else Path(ca)
        if not ca_path.is_file():
            return None, f"its CA file {ca} is not there"
    for key in ("checked", "contexts"):
        record.pop(key, None)
    return {"name": name, "record": record, "ca": str(ca_path) if ca_path else None}, None


def llm_names(config):
    try:
        store = json.loads((Path(config) / "llm.json").read_text())
        return sorted((store.get("endpoints") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


def host_files(name, config, target):
    """Copies the host NAME of CONFIG/hosts.json (only it) and sandbox-ai's known_hosts into TARGET; a problem or
    None."""
    try:
        document = json.loads((Path(config) / "hosts.json").read_text())
    except FileNotFoundError:
        return "no host is configured (sandbox-ai hosts add)"
    except (OSError, ValueError) as exc:
        return f"hosts.json: {exc}"
    hosts = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(hosts, dict) or name not in hosts:
        return f"no host {name!r} (configured: {', '.join(sorted(hosts or {})) or 'none'})"
    target = Path(target)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(target / "hosts.json", json.dumps({"version": document.get("version", 1),
                                                     "hosts": {name: hosts[name]}}, indent=2) + "\n")
    known = Path(config) / "known_hosts"
    if known.is_file():
        write_private(target / "known_hosts", known.read_text())
    return None


def write_private(path, text):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def pick_device(scan):
    """The value of `devices add` devices-live uses from a `devices scan --json` document: the first device it
    offers (not refused, given with --device as a path) of DEVICE_KINDS' kinds, in that order; None."""
    entries = [entry for entry in (scan or {}).get("devices") or [] if isinstance(entry, dict)]
    for kind in DEVICE_KINDS:
        for entry in entries:
            value = entry.get("value")
            if entry.get("kind") == kind and not entry.get("refused") and entry.get("option") == "--device" \
                    and isinstance(value, str) and value.startswith("/dev/"):
                return value
    return None


def ask(question, stream_in=None, stream_out=None):
    """True when the human answers yes on a terminal; False without a terminal."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    if not stream_in.isatty():
        return False
    stream_out.write(question + " [y/N] ")
    stream_out.flush()
    return stream_in.readline().strip().lower() in ("y", "yes", "j", "ja")


# --- running a check ------------------------------------------------------------------------------------------------

def pytest_command(python, check, backend, basetemp, junit, suites=None, needs_login=None):
    """The exact pytest command of CHECK with BACKEND ("both", "podman" or "bwrap")."""
    command = [str(python), "-X", "faulthandler", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfEs",
               "--agent-live", "--agent-backend", backend, f"--basetemp={basetemp}", f"--junitxml={junit}"]
    if check.name == "live-suites":
        return command + ["-k", needs_login, *suites]
    if check.keyword:
        command += ["-k", check.keyword]
    return command + list(check.tests)


def run_logged(command, log, timeout, env, cwd):
    """Runs COMMAND in a process group of its own, its output into LOG; (status, seconds). At TIMEOUT the group gets
    SIGTERM, 30 s later SIGKILL (only this process group: what this check started), status 124."""
    begin = time.monotonic()
    with open(log, "wb") as out:
        process = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                   cwd=cwd, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 30)):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            out.write(f"\n[live check: timed out after {timeout} s]\n".encode())
            status = 124
    return status, time.monotonic() - begin


def outcome_of(status, results):
    """passed, failed, skipped or error of a pytest run: its exit STATUS and its tests' RESULTS [(name, outcome,
    ...)]."""
    outcomes = [result[1] for result in results]
    if status == 124:
        return "failed"
    if any(outcome in ("failed", "error") for outcome in outcomes):
        return "failed"
    if not outcomes:
        return "skipped" if status == 5 else "error"
    if status not in (0, 5):
        return "error"  # pytest failed without a failing test: never a passing check
    if all(outcome == "skipped" for outcome in outcomes):
        return "skipped"
    return "passed"


def tail(path, lines=TAIL):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def leftover_sessions(root):
    """[(environment, session name)] of the sessions the checks under ROOT left behind (a check that timed out never
    reached its own cleanup): each ROOT/**/sandbox-ai/sessions/NAME/session.json, with the XDG folders found beside
    the state folder it lies in ({variable: folder})."""
    found = []
    for record in sorted(Path(root).glob("**/sandbox-ai/sessions/*/session.json")):
        state = record.parents[3]
        folders = {"XDG_STATE_HOME": str(state)}
        for name, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                             ("XDG_RUNTIME_DIR", "run")):
            if (state.parent / folder).is_dir():
                folders[name] = str(state.parent / folder)
        found.append((folders, record.parent.name))
    return found


def stop_leftovers(root, env, runner=run_command):
    """Stops and deletes every session the checks under ROOT left behind - only those: each by its name, with the
    folders it was made with."""
    stopped = []
    for folders, name in leftover_sessions(root):
        mine = {**env, **folders}
        runner([str(SAI), "session", "stop", name], timeout=180, env=mine)
        runner([str(SAI), "session", "delete", name, "--yes"], timeout=120, env=mine)
        stopped.append(name)
    return stopped


def remove_tree(path, env, runner=run_command):
    """Removes PATH; what rootless Podman left there under its mapped users goes through `podman unshare`."""
    shutil.rmtree(path, ignore_errors=True)
    if Path(path).exists() and shutil.which("podman", path=env.get("PATH")):
        runner(["podman", "unshare", "rm", "-rf", "--", str(path)], timeout=300, env=env)
    return not Path(path).exists()


# --- redaction and the report ----------------------------------------------------------------------------------------

SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "<private key>"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "<secret>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), "<secret>"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), "<secret>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "<secret>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<secret>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<secret>"),
    (re.compile(r"\b(?:hf|npm|pypi)_[A-Za-z0-9_-]{20,}"), "<secret>"),
    (re.compile(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"',]+"),
     r"\1<secret>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 <secret>"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|passw(?:or)?d|credential)[\w-]*[\"']?\s*[:=]\s*[\"']?)"
                r"(?!<secret>)(?=[^\s\"',;}]*[A-Za-z])[^\s\"',;}]{6,}"), r"\1<secret>"),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1<secret>@"),
)
# A home folder at the start of a path (not one below another folder: <tmp>/home is the checks' own HOME).
HOME_PATTERN = re.compile(r"(?<![\w.>/~-])(/(?:home|Users|var/home|export/home))/[^/\s\"':;,)\]}]+")
SECRET_NAME = re.compile(r"(?i)token|secret|passw|api_?key|credential|auth|cookie|private")


class Redactor:
    """Replaces in a text every literal secret it was given, the checks' temporary folder, the real HOME and any
    other home folder, and what looks like a token, key or password."""

    def __init__(self, home=None, temporary=(), literals=(), environ=None):
        self.home = str(home).rstrip("/") if home else None
        self.temporary = [str(path).rstrip("/") for path in temporary if path]
        environ = os.environ if environ is None else environ
        found = {value for key, value in environ.items()  # (a path, such as SSH_AUTH_SOCK's, is no secret)
                 if SECRET_NAME.search(key) and len(value) >= 8 and not value.startswith("/")}
        self.literals = sorted({value for value in [*literals, *found] if value and len(value) >= 6}, key=len,
                               reverse=True)

    def text(self, text):
        if not isinstance(text, str):
            return text
        for literal in self.literals:
            text = text.replace(literal, "<secret>")
        for path in sorted(self.temporary, key=len, reverse=True):
            text = text.replace(path, "<tmp>")
        if self.home and self.home not in ("", "/"):
            text = re.sub(re.escape(self.home) + r"(?=/|\b|$)", "~", text)
        text = HOME_PATTERN.sub(r"\1/<user>", text)
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def data(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, tuple):
            return [self.data(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key) if isinstance(key, str) else key: self.data(item) for key, item in value.items()}
        return value


def counts_of(checks):
    counts = {}
    for check in checks:
        counts[check["outcome"]] = counts.get(check["outcome"], 0) + 1
    return counts


def markdown(report):
    """The report as Markdown (already redacted)."""
    lines = [f"# sandbox-ai live check, {report['date']}", "",
             "Made by `scripts/live_check.sh` (docs/LIVE_CHECK.md). Secrets and home paths are replaced "
             "(`<secret>`, `~`, `/home/<user>`, `<tmp>`: the checks' own temporary folder).", "",
             "| | |", "|---|---|"]
    lines += [f"| {key} | {cell(value)} |" for key, value in report["facts"].items()]
    lines += ["", "## Prerequisites", "", "| | prerequisite | |", "|---|---|---|"]
    for item in report["prerequisites"]:
        lines.append(f"| {'ok' if item['ok'] else 'missing'} | {item['name']} | {cell(item['detail'])} |")
    if report.get("network"):
        lines += ["", "## What left this machine", "", *(f"- {line}" for line in report["network"])]
    counts = report["counts"]
    lines += ["", "## Results", "", "Counts: " + (", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
                                                or "nothing ran"), "",
              "| outcome | check | seconds | what it proves |", "|---|---|---|---|"]
    for check in report["checks"]:
        seconds = "" if check.get("seconds") is None else f"{check['seconds']:.0f}"
        lines.append(f"| {check['outcome']} | {check['name']} | {seconds} | {cell(check['proves'])} |")
    for check in report["checks"]:
        lines += ["", f"### {check['name']}: {check['outcome']}", ""]
        if check.get("missing"):
            lines += ["Missing: " + "; ".join(check["missing"]), ""]
        if check.get("note"):
            lines += [check["note"], ""]
        if check.get("network"):
            lines += [f"Network: {check['network']}", ""]
        if check.get("command"):
            lines += ["```", check["command"], "```", ""]
        if check.get("presets"):
            lines += ["| preset | agent | podman | bwrap | its part |", "|---|---|---|---|---|",
                      *(f"| {row['preset']} | {row['agent']} | {cell(row['podman'])} | {cell(row['bwrap'])} | "
                        f"{cell(row['part'])} |" for row in check["presets"]), ""]
        if check.get("mcp"):
            lines += ["| server | backend | outcome | added | started | handshake | tools | calls | isolation | seconds |",
                      "|---|---|---|---|---|---|---|---|---|---|",
                      *(f"| {row['server']}{' (behind ' + row['vpn'] + ')' if row.get('vpn') else ''} | "
                        f"{row['backend']} | {cell(row['outcome'])} | {cell(row['added'])} | "
                        f"{cell(row['started'])} | {cell(row['handshake'])} | {cell(row['tools'])} | "
                        f"{cell(row['calls'])} | {cell(row['isolation'])} | "
                        f"{seconds_cell(row.get('seconds'))} |"
                        for row in check["mcp"]), ""]
            for row in check["mcp"]:
                if row.get("why") and row["outcome"] != "passed":
                    lines += [f"- {row['server']} ({row['backend']}): {row['why']}"]
                if row.get("supervisor"):
                    lines += ["", f"  supervisor.log of {row['server']} ({row['backend']}), its last lines:", "",
                              "  ```", *("  " + line for line in str(row["supervisor"]).splitlines()), "  ```", ""]
            lines.append("")
        if check.get("harnesses"):
            lines += ["| harness | image | podman | bwrap |", "|---|---|---|---|",
                      *(f"| {row['agent']} | {cell(row['image'])} | {cell(row['podman'])} | {cell(row['bwrap'])} |"
                        for row in check["harnesses"]), ""]
        if check.get("cairn_workers"):
            lines += ["| harness | image | as Cairn's worker | for sandbox/cairn_workers.json |", "|---|---|---|---|",
                      *(f"| {row['agent']} | {cell(row['image'])} | {cell(row['result'])} | {cell(row['status'])} |"
                        for row in check["cairn_workers"]), ""]
        for test in check.get("tests") or []:
            lines.append(f"- {test['outcome']}: `{test['name']}`" + (f" ({test['seconds']:.0f} s)"
                                                                     if test.get("seconds") else ""))
            if test.get("detail") and test["outcome"] != "passed":
                lines += ["", "  ```", *("  " + line for line in test["detail"].splitlines()), "  ```", ""]
        if check.get("tail"):
            lines += ["", "Last lines of its output:", "", "```", *check["tail"], "```"]
    return "\n".join(lines) + "\n"


def seconds_cell(value):
    """A number of seconds as the report's tables show it ("" for none)."""
    return "" if not isinstance(value, (int, float)) else f"{value:.0f}"


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_report(report, directory, redactor, stamp):
    """REPORT-STAMP.md and .json in DIRECTORY, redacted; their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    clean = redactor.data(report)
    clean["counts"] = counts_of(clean["checks"])
    md, js = directory / f"REPORT-{stamp}.md", directory / f"REPORT-{stamp}.json"
    js.write_text(json.dumps(clean, indent=2) + "\n")
    md.write_text(markdown(clean))
    return md, js


def listing(out=print):
    """--list: every check, what it proves, what it needs, what it sends where."""
    for check in CHECKS:
        out(f"{check.name}{' (optional)' if check.optional else ''}")
        out(f"    proves:   {check.proves}")
        out(f"    needs:    {', '.join(check.needs)}" + (", Podman only" if check.podman_only else ""))
        out(f"    network:  {check.network or 'nothing leaves this machine'}")
        out(f"    timeout:  {check.timeout} s")
        out(f"    why:      {check.source}")


def selected(only):
    """The checks of --only (comma lists, repeatable) or, without it, every check that is not optional; SystemExit
    names an unknown one."""
    if not only:
        return [check for check in CHECKS if not check.optional]
    names = [name.strip() for value in only for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in BY_NAME]
    if unknown:
        raise SystemExit(f"live check: unknown check {', '.join(unknown)}; the checks: {', '.join(BY_NAME)}")
    return [check for check in CHECKS if check.name in names]


# --- main ------------------------------------------------------------------------------------------------------------

def parser():
    parse = argparse.ArgumentParser(prog="scripts/live_check.sh", description=__doc__.split("\n\n")[0],
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parse.add_argument("--list", action="store_true", help="the checks, and exit")
    parse.add_argument("--only", action="append", metavar="NAME[,NAME]", help="only these checks (repeatable)")
    parse.add_argument("--backend", choices=["auto", "podman", "bwrap", "both"], default="auto",
                       help="the sandbox backends (auto: each one that works here)")
    parse.add_argument("--out", type=Path, metavar="DIR", help="where the report goes (default: live-check/ of "
                                                                 "this checkout)")
    parse.add_argument("--build", action="store_true", help="build the missing images first (network: their "
                                                            "packages)")
    parse.add_argument("--host", metavar="NAME", help="also remote-mcp on this host of sandbox-ai hosts")
    parse.add_argument("--llm", metavar="NAME", help="also local-llm, harnesses-live, cairn-workers-live and presets-live "
                                                     "on this server of sandbox-ai llm")
    parse.add_argument("--llm-models", metavar="MODEL[,MODEL]", default=",".join(LLM_MODELS),
                       help="the models of local-llm (default: the three best of the research); "
                            "harnesses-live, cairn-workers-live and presets-live use the first")
    parse.add_argument("--yes", action="store_true", help="answer yes to the questions of remote-mcp, local-llm, "
                                                          "harnesses-live, cairn-workers-live, presets-live, "
                                                          "mcp-live and artex-live")
    parse.add_argument("--preset-values", type=Path, metavar="FILE",
                       help="presets-live: the values only you can give, as JSON {\"PRESET\": {\"VALUE\": \"TEXT\"}} "
                            "(a target of yours, a VPN profile ...); a preset that needs one and has none is skipped "
                            "with the command to run it yourself")
    parse.add_argument("--presets", metavar="NAME[,NAME]", help="presets-live: only these presets (default: every "
                                                                "built-in one)")
    parse.add_argument("--provider-login", action="store_true",
                       help="presets-live: the agents' own providers with your logins (your HOME and data folder "
                            "are read for them; sandbox-ai's configuration and state stay the check's own) - in "
                            "place of or beside --llm")
    parse.add_argument("--mcp-vpn", metavar="PROFILE",
                       help="mcp-live: the servers with network added behind a Tor profile of this name (mcp add "
                            "--vpn), made in the check's own configuration; the others are skipped. A VPN profile of "
                            "yours needs your keys, which a check never copies: sandbox-ai mcp check --vpn PROFILE "
                            "checks with it in your configuration")
    parse.add_argument("--mcp-servers", metavar="NAME[,NAME]",
                       help="mcp-live: only these servers of the signed catalog (default: every one)")
    parse.add_argument("--mcp-repo", type=Path, metavar="DIR",
                       help="mcp-live: the checkout of sandbox-ai-mcp whose servers/NAME/smoke.json and tools.json "
                            "it takes (default: projects/sandbox-ai-mcp of this checkout, else cloned from GitHub)")
    parse.add_argument("--accept-android-sdk-license", action="store_true",
                       help="toolchain-live, android-emulator-live: you accept the Android Software Development Kit "
                            "License Agreement (https://developer.android.com/studio/terms) for what these checks "
                            "fetch from Google (the Android SDK, the emulator); without it those parts are skipped")
    parse.add_argument("--burp-url", metavar="URL", help="burp-live: your Burp's MCP endpoint (default "
                                                         "http://127.0.0.1:9876/mcp)")
    parse.add_argument("--burp-jar", type=Path, metavar="JAR", help="burp-live: your Burp jar for container mode "
                                                                   "(Community or Professional; never copied out)")
    parse.add_argument("--burp-edition", choices=["community", "professional"],
                       help="burp-live: the edition of --burp-jar (default: from its name)")
    parse.add_argument("--burp-license-file", type=Path, metavar="FILE",
                       help="burp-live: a file with your Burp Suite Professional licence key, for container mode "
                            "(the check's own secret store; activated online once)")
    parse.add_argument("--device", metavar="PATH", help="the device of devices-live (default: one devices scan "
                                                        "offers: a serial port, camera, render node or /dev/kvm)")
    parse.add_argument("--dmg-old", metavar="DMG", help="macos-update: the disk image updated from (sandbox-ai.app "
                                                         "0.1.154 or later; a .dmg or a downloaded release's folder)")
    parse.add_argument("--dmg-new", metavar="DMG", help="macos-update: the disk image updated to (a .dmg or a "
                                                         "downloaded release's folder); macos-app's app without --app")
    parse.add_argument("--app", metavar="PATH", help="macos-app: this sandbox-ai.app (only read and started)")
    parse.add_argument("--keep-temp", action="store_true", help="keep the checks' temporary folder (its logs; it "
                                                                "may hold paths of yours)")
    parse.add_argument("--work-dir", type=Path, metavar="DIR", help="where the temporary folder is made (default: "
                                                                    "the system's; keep its path short)")
    return parse


def main(argv=None, out=print, stdin=None):
    args = parser().parse_args(argv)
    if args.list:
        listing(out)
        return 0
    try:
        checks = selected(args.only)
    except SystemExit as exc:
        out(str(exc))
        return 2
    lc = load_live_check()
    if os.geteuid() == 0:
        out("live check: refused - run it as your own user, never with root or sudo (rootless Podman)")
        return 2
    if lc.inside_a_container():
        out("live check: refused - this looks like a container or a sandbox; run it on the host")
        return 2
    config = real_config()
    if not args.only:  # the optional sections join a whole run when they are configured (each asks first)
        if args.host or host_names(config):
            checks.append(BY_NAME["remote-mcp"])
        if args.llm or llm_names(config):
            checks += [BY_NAME[name] for name in LLM_CHECKS]
    names = [check.name for check in checks]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    real_home = os.environ.get("HOME") or str(Path.home())
    temporary = Path(tempfile.mkdtemp(prefix="sai-lc-", dir=args.work_dir or tempfile.gettempdir()))
    env = isolated_environment(temporary, os.environ)
    token = "sai-lc-" + secrets.token_hex(16)
    env.update(SAI_LIVE_TOKEN=token, SAI_LIVE_REAL_HOME=real_home)
    redactor = Redactor(real_home, [temporary, temporary.resolve()], [token])
    out(f"sandbox-ai live check: {', '.join(names)}\nthe checks' own folder (HOME, configuration, state, "
        f"sessions): {temporary}")

    said = {}
    if "remote-mcp" in names:
        said["host"] = prepare_host(args, config, temporary, env, stdin)
    if any(name in names for name in LLM_USERS):
        said["llm"] = prepare_llm(args, config, temporary, env, stdin, [n for n in names if n in LLM_USERS])
    if "presets-live" in names:
        said["presets"] = prepare_presets(args, config, env, stdin)
    if "mcp-live" in names:
        said["mcp"] = prepare_mcp(args, temporary, env, stdin)
    if "artex-live" in names:
        said["artex"] = prepare_artex(args, env, stdin)
    found, facts = prerequisites(env, args)
    ready = {"host": f"host {env.get('SAI_LIVE_HOST')}", "llm": f"server {getattr(args, 'llm_said', '')}",
             "presets": "presets-live's values", "mcp": getattr(args, "mcp_said", "the signed catalog"),
             "artex": "confirmed: its image built when missing, one session started"}
    for key, problem in said.items():  # what the optional sections' preparation found
        found[key] = Found(problem is None, problem or ready[key])
    found["model"] = model_found(found, args)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path(real_home) / ".cache") / "sandbox-ai" / "live-check"
    found["pytest"] = pytest_found(cache)
    backends = backends_of(args.backend, found)
    facts.update({"date": stamp, "checkout": str(ROOT), "backends": ", ".join(backends) or "none"})
    if args.build and found["podman"].ok:
        build_images(found, env, out)
    if "devices-live" in names:
        found["device"] = prepare_device(args, env)
    if "burp-live" in names:
        found["burp"] = prepare_burp(args, env)
    if found["macos"].ok:
        found["dmgs"] = prepare_dmgs(args, env)
        found["app"] = prepare_app(args, env)
    system_keys = ("macos", "podman-machine") if found["macos"].ok else ("linux", "userns", "subuid", "podman", "bwrap")
    for key in (*system_keys, "git", "pytest", *(f"image:{i}" for i in IMAGES if not found["macos"].ok),
                *(key for key in ("device", "host", "llm", "model", "presets", "mcp", "artex", "burp", "dmgs", "app",
                                  "kvm", "android-license")
                  if key in found
                  and key in needed(checks))):
        if key not in found:
            continue
        state = found[key]
        out(f"  {'ok  ' if state.ok else 'MISS'}  {key}: {state.detail}")

    # (what the checks that will run send: a check skipped for what it lacks sends nothing)
    network = [f"{check.name}: {check.network}" for check in checks
               if check.network and not missing_for(check, found, backends)]
    if args.build:
        network.append("--build: the images' packages (Debian, npm, PyPI ...)")
    if not found["pytest"].ok or "PyPI" in found["pytest"].detail:
        network.append("pytest: " + found["pytest"].detail)
    if network:
        out("What these checks send from this machine:")
        for line in network:
            out(f"  - {line}")
    python, results = None, []
    for check in checks:
        missing = missing_for(check, found, backends)
        entry = {"name": check.name, "proves": check.proves, "needs": list(check.needs), "network": check.network,
                 "source": check.source, "outcome": "skipped", "seconds": None, "missing": missing, "command": "",
                 "tests": [], "tail": []}
        results.append(entry)
        if missing:
            out(f"  skip  {check.name}  ({missing[0][:160]})")
            continue
        if python is None:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                python = lc.pytest_python(cache)
            except SystemExit as exc:
                entry.update(outcome="error", note=str(exc))
                out(f"  ERR   {check.name}  ({exc})")
                continue
        run_check(check, entry, python, backends, temporary, env, lc, out)
    stopped = stop_leftovers(temporary, env)
    report = {"version": REPORT_VERSION, "date": stamp, "facts": facts,
              "prerequisites": [{"name": key, "ok": state.ok, "detail": state.detail} for key, state in found.items()],
              "network": network, "checks": results, "counts": counts_of(results),
              "left_behind_and_stopped": stopped}
    md, js = write_report(report, args.out or ROOT / "live-check", redactor, stamp)
    if args.keep_temp:
        out(f"the checks' folder is kept: {temporary} (it may hold paths of yours; remove it when done)")
    elif not remove_tree(temporary, env):
        out(f"could not remove all of {temporary}: podman unshare rm -rf {temporary}")
    counts = counts_of(results)
    out(f"\n{', '.join(f'{key} {value}' for key, value in sorted(counts.items()))}\nreport: {md}\n        {js}")
    return 0


def needed(checks):
    return {key for check in checks for key in check.needs}


def pytest_found(cache):
    """Found: pytest for the checks - this Python's, the venv of an earlier run, or one to install from PyPI."""
    status, _, _ = run_command([sys.executable, "-s", "-c", "import pytest"], timeout=60)  # (as lc.pytest_python)
    if status == 0:
        return Found(True, f"pytest of {sys.executable}")
    if (Path(cache) / "venv" / "bin" / "python").exists():
        return Found(True, f"pytest in {Path(cache) / 'venv'}")
    return Found(True, f"installed now from PyPI into {Path(cache) / 'venv'} (packaging/requirements-ci.txt, hashes "
                       "checked)")


def build_images(found, env, out):
    for image in IMAGES:
        if not found[f"image:{image}"].ok:
            out(f"  ...   sandbox-ai build {image} (its packages come from the network)")
            status, _, err = run_command([str(SAI), "build", image], timeout=3600, env=env)
            found[f"image:{image}"] = Found(status == 0, f"built now: localhost/{image}-sandbox" if status == 0 else
                                            f"sandbox-ai build {image} failed: {err.strip()[-300:]}")


def host_names(config):
    try:
        document = json.loads((Path(config) / "hosts.json").read_text())
        return sorted((document.get("hosts") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


def prepare_host(args, config, temporary, env, stdin=None):
    """remote-mcp's host: --host NAME, or the one configured host after a question; that host's entry and
    sandbox-ai's known_hosts into the checks' folder (SAI_LIVE_HOST, SAI_LIVE_HOSTS_DIR). A problem or None."""
    configured = host_names(config)
    name = args.host or (configured[0] if len(configured) == 1 else None)
    if not name:
        return "give --host NAME" + (f" (configured: {', '.join(configured)})" if configured else
                                     ": no host is configured (sandbox-ai hosts add)")
    question = (f"remote-mcp: over SSH to your host {name}: hosts test, its MCP servers listed, its demo server "
                "started and stopped there if none of it runs. Run it?")
    if not args.host and not (args.yes or ask(question, stdin)):
        return "not run: not confirmed (on a terminal it asks; else --host NAME or --yes)"
    target = temporary / "given" / "hosts"
    problem = host_files(name, config, target)
    if problem:
        return problem
    env.update(SAI_LIVE_HOST=name, SAI_LIVE_HOSTS_DIR=str(target))
    return None


def prepare_llm(args, config, temporary, env, stdin=None, checks=("local-llm",)):
    """The server of local-llm and harnesses-live (CHECKS: those that run): --llm NAME, or the one configured server;
    asked first (on a terminal; else --yes). The endpoint without secrets into the checks' folder
    (SAI_LIVE_LLM_ENDPOINT, SAI_LIVE_LLM_MODELS). A problem or None."""
    configured = llm_names(config)
    name = args.llm or (configured[0] if len(configured) == 1 else None)
    if not name:
        return "give --llm NAME" + (f" (configured: {', '.join(configured)})" if configured else
                                    ": no LLM server is configured (sandbox-ai llm add)")
    entry, problem = llm_endpoint(name, config)
    if problem:
        return problem
    models = [model.strip() for model in args.llm_models.split(",") if model.strip()]
    runs = []
    if "local-llm" in checks:
        runs.append(f"local-llm: {len(models) * 3} smoke runs ({', '.join(models)} x Claude Code, OpenCode, Codex)")
    if "harnesses-live" in checks and models:
        runs.append(f"harnesses-live: one smoke run of each of the {len(HARNESSES)} harnesses with {models[0]}, with "
                    "Podman and with Bubblewrap; a missing image is built first (its packages from the network, "
                    "minutes each)")
    if "presets-live" in checks and models:
        runs.append(f"presets-live: one smoke run of each built-in preset ({len(list(PRESETS_DIR.glob('*.json')))} "
                    f"files) with {models[0]}, with Podman and where it can Bubblewrap; the MCP servers it names are "
                    "added from the catalog (their images from ghcr.io)")
    if "cairn-workers-live" in checks and models:
        runs.append(f"cairn-workers-live: one small task of real Cairn with each of the "
                    f"{len(CAIRN_WORKERS) - len(CAIRN_SKIPPED)} harnesses as its worker with {models[0]}, Podman; "
                    "a missing image is built first")
    question = (f"{'; '.join(runs)} - against your LLM server {name}; it loads each model, which takes a while. "
                "Run them?")
    if not (args.yes or ask(question, stdin)):
        return "not run: not confirmed (on a terminal it asks; else --yes)"
    path = temporary / "given" / "llm.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, json.dumps(entry) + "\n")
    env.update(SAI_LIVE_LLM_ENDPOINT=str(path), SAI_LIVE_LLM_MODELS=",".join(models))
    args.llm_said = f"{name}: {', '.join(models)}"
    return None


def prepare_artex(args, env, stdin=None, runner=run_command):
    """artex-live, asked first (on a terminal; else --yes): ARTEX's image is built when missing (about 2 GB from the
    network, 10-40 minutes), then one session runs with it. A problem or None."""
    status, _, _ = runner(["podman", "image", "exists", "localhost/sandbox-ai-artex:latest"], timeout=60, env=env)
    build = "" if status == 0 else ("ARTEX's image is not built: sandbox-ai build artex builds it first from GitHub's "
                                    "archive of the reviewed commit with sandbox-ai's patches (about 2 GB from the "
                                    "network, 10-40 minutes); then ")
    question = (f"artex-live: {build}one throwaway session starts ARTEX beside an agent and checks its console on "
                "127.0.0.1. Run it?")
    if not (args.yes or ask(question, stdin)):
        return "not run: not confirmed (on a terminal it asks; else --yes)"
    return None


def prepare_burp(args, env):
    """Found: burp-live's Burp - host mode at --burp-url (SAI_LIVE_BURP_URL), container mode with --burp-jar
    (SAI_LIVE_BURP_JAR, SAI_LIVE_BURP_EDITION; --burp-license-file: SAI_LIVE_BURP_LICENSE_FILE, never its text)."""
    env["SAI_LIVE_BURP_URL"] = args.burp_url or "http://127.0.0.1:9876/mcp"
    said = [f"host mode at {env['SAI_LIVE_BURP_URL']}"]
    if args.burp_jar:
        if not args.burp_jar.is_file():
            return Found(False, f"--burp-jar {args.burp_jar}: no such file")
        env["SAI_LIVE_BURP_JAR"] = str(args.burp_jar.resolve())
        said.append(f"container mode with {args.burp_jar.name}")
    if args.burp_edition:
        env["SAI_LIVE_BURP_EDITION"] = args.burp_edition
    if args.burp_license_file:
        if not args.burp_license_file.is_file():
            return Found(False, f"--burp-license-file {args.burp_license_file}: no such file")
        env["SAI_LIVE_BURP_LICENSE_FILE"] = str(args.burp_license_file.resolve())
    return Found(True, "; ".join(said))


def prepare_device(args, env, runner=run_command):
    """Found: devices-live's device - --device, else one `devices scan` offers (SAI_LIVE_DEVICE)."""
    if args.device:
        env["SAI_LIVE_DEVICE"] = args.device
        return Found(True, f"{args.device} (--device)")
    status, out, err = runner([str(SAI), "devices", "scan", "--json"], timeout=120, env=env)
    if status != 0:
        return Found(False, f"devices scan failed: {(err or out).strip()[-300:]}")
    try:
        value = pick_device(json.loads(out))
    except ValueError:
        return Found(False, "devices scan did not answer JSON")
    if not value:
        return Found(False, "devices scan offers no serial port, camera, render node or /dev/kvm to hand over; give "
                            "--device PATH")
    env["SAI_LIVE_DEVICE"] = value
    return Found(True, f"{value} (from devices scan)")


def run_check(check, entry, python, backends, temporary, env, lc, out):
    """Runs CHECK's pytest and fills ENTRY: its command, outcome, tests, last lines."""
    # (the macOS checks use no backend of this host: --agent-backend is pytest's default then)
    backend = "podman" if check.podman_only or not backends else ("both" if len(backends) == 2 else backends[0])
    junit = temporary / "junit" / f"{check.name}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    basetemp = temporary / "p" / check.name.replace("-", "")[:12]
    basetemp.parent.mkdir(parents=True, exist_ok=True)  # (pytest makes --basetemp itself, never its parents)
    command = pytest_command(python, check, backend, basetemp, junit, lc.SUITES, lc.NEEDS_LOGIN)
    entry["command"] = shlex.join(str(part) for part in command)
    log = temporary / "logs" / f"{check.name}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    out(f"  ...   {check.name} (at most {check.timeout // 60} min)")
    facts = temporary / "harnesses"  # harnesses-live: what each harness's image took (test_live_harness_smoke_run)
    if check.name in ("harnesses-live", "cairn-workers-live"):
        facts.mkdir(parents=True, exist_ok=True)
        env = {**env, "SAI_LIVE_HARNESS_DIR": str(facts)}
    found_presets = temporary / "presets"  # presets-live: what each preset's run found (test_live_preset_smoke_run)
    if check.name == "presets-live":
        found_presets.mkdir(parents=True, exist_ok=True)
        env = {**env, "SAI_LIVE_PRESET_DIR": str(found_presets)}
    found_mcp = temporary / "mcp-found"  # mcp-live: what each server's test found (test_live_mcp_server)
    if check.name == "mcp-live":
        found_mcp.mkdir(parents=True, exist_ok=True)
        env = {**env, "SAI_LIVE_MCP_DIR": str(found_mcp)}
    status, seconds = run_logged(command, log, check.timeout, env, ROOT)
    results = lc.junit_results(junit)
    entry["outcome"] = outcome_of(status, results)
    entry["seconds"] = round(seconds, 1)
    entry["status"] = status
    entry["tests"] = [{"name": name, "outcome": outcome, "seconds": round(spent, 1),
                       "detail": detail.strip()[-DETAIL:] if outcome != "passed" else ""}
                      for name, outcome, detail, spent in results]
    entry["tail"] = tail(log)
    if check.name == "harnesses-live":
        entry["harnesses"] = harness_rows(entry["tests"], facts)
    if check.name == "presets-live":
        entry["presets"] = preset_rows(found_presets)
    if check.name == "mcp-live":
        entry["mcp"] = mcp_rows(found_mcp)
    if check.name == "cairn-workers-live":
        entry["cairn_workers"] = cairn_worker_rows(entry["tests"], facts)
    mark = {"passed": "ok  ", "failed": "FAIL", "skipped": "skip", "error": "ERR "}[entry["outcome"]]
    out(f"  {mark}  {check.name}  ({seconds:.0f} s)")


def harness_rows(tests, facts):
    """harnesses-live per harness: [{"agent", "image", "podman", "bwrap"}] from its TESTS' outcomes (the names of
    test_live_harness_smoke_run[...]) and what FACTS (the folder SAI_LIVE_HARNESS_DIR) says of its image."""
    rows = []
    for agent in HARNESSES:
        try:
            image = json.loads((Path(facts) / f"{agent}.json").read_text())
        except (OSError, ValueError):
            image = {}
        if image.get("built"):
            said = f"built now in {image.get('seconds', 0):.0f} s"
        elif image.get("problem"):
            said = f"cannot be built here: {image['problem']}"
        else:
            said = "there before" if image.get("present") else "not used"
        row = {"agent": agent, "image": said, "podman": "not run", "bwrap": "not run"}
        for test in tests:
            parts = set(re.split(r"[\[\]-]", test["name"]))
            if test["name"].split("[")[0].endswith("test_live_harness_smoke_run") and agent in parts:
                backend = "podman" if "podman" in parts else "bwrap" if "bwrap" in parts else None
                if backend:
                    detail = f": {test['detail'].strip().splitlines()[0][:200]}" if (test.get("detail") or "").strip() \
                        and test["outcome"] != "passed" else ""
                    seconds = f" ({test['seconds']:.0f} s)" if test.get("seconds") and test["outcome"] == "passed" \
                        else ""
                    row[backend] = test["outcome"] + seconds + detail
        rows.append(row)
    return rows


def cairn_worker_rows(tests, facts):
    """cairn-workers-live per harness: [{"agent", "image", "result", "status"}] from its TESTS' outcomes (the names of
    test_live_cairn_worker[AGENT]) and what FACTS says of its image; "status" is what the row gives
    sandbox/cairn_workers.json (verified when the task's fact reached the board, failing when it did not; untested
    when it was skipped or not run) - written there by hand, with this report's name as the evidence."""
    rows = []
    for agent in CAIRN_WORKERS:
        try:
            image = json.loads((Path(facts) / f"{agent}.json").read_text())
        except (OSError, ValueError):
            image = {}
        said = f"built now in {image.get('seconds', 0):.0f} s" if image.get("built") else \
            f"cannot be built here: {image['problem']}" if image.get("problem") else \
            ("there before" if image.get("present") else "not used")
        row = {"agent": agent, "image": said, "result": "not run", "status": "untested"}
        for test in tests:
            if test["name"].split("[")[0].endswith("test_live_cairn_worker") and \
                    test["name"].rstrip("]").split("[")[-1] == agent:
                detail = (test.get("detail") or "").strip()
                first = f": {detail.splitlines()[0][:200]}" if detail and test["outcome"] != "passed" else ""
                seconds = f" ({test['seconds']:.0f} s)" if test.get("seconds") and test["outcome"] == "passed" else ""
                row["result"] = test["outcome"] + seconds + first
                row["status"] = {"passed": "verified", "failed": "failing", "error": "failing"}.get(test["outcome"],
                                                                                                   "untested")
        rows.append(row)
    return rows



def model_found(found, args):
    """Found: presets-live's model - --llm's server (prepared), or the agents' own providers (--provider-login)."""
    llm = found.get("llm")
    if llm is not None and llm.ok:
        return Found(True, llm.detail + (" and the agents' own providers (--provider-login)" if args.provider_login
                                         else ""))
    if getattr(args, "provider_login", False):
        return Found(True, "the agents' own providers with your logins (--provider-login)")
    return Found(False, (llm.detail if llm is not None else "give --llm NAME") + "; or --provider-login (confirmed)")


def prepare_presets(args, config, env, stdin=None):
    """presets-live's inputs: --preset-values FILE (checked, SAI_LIVE_PRESET_VALUES), --presets
    (SAI_LIVE_PRESETS), --provider-login after a question (SAI_LIVE_PROVIDER_LOGIN, SAI_LIVE_REAL_DATA_HOME), your
    configuration's folder for your service images (SAI_LIVE_REAL_CONFIG: user-images.json is read, never changed).
    A problem or None."""
    if args.preset_values:
        try:
            values = preset_values(args.preset_values)
        except ValueError as exc:
            return str(exc)
        unknown = sorted(set(values) - {path.stem for path in PRESETS_DIR.glob("*.json")})
        if unknown:
            return f"--preset-values {args.preset_values}: no built-in preset {', '.join(unknown)}"
        env["SAI_LIVE_PRESET_VALUES"] = str(Path(args.preset_values).resolve())
    if args.presets:
        names = [name.strip() for name in args.presets.split(",") if name.strip()]
        unknown = [name for name in names if not (PRESETS_DIR / f"{name}.json").is_file()]
        if unknown:
            return f"--presets: no built-in preset {', '.join(unknown)}"
        env["SAI_LIVE_PRESETS"] = ",".join(names)
    env["SAI_LIVE_REAL_CONFIG"] = str(config)
    if args.provider_login:
        question = ("presets-live with --provider-login: each preset's agent uses its own provider with your login "
                    "(your HOME and data folder are read for it; prompts go to that provider). Run it?")
        if not (args.yes or ask(question, stdin)):
            args.provider_login = False  # (presets-live then runs with --llm alone, if given: model_found says so)
            return None
        real = os.environ.get("XDG_DATA_HOME") or str(Path(os.environ.get("HOME") or Path.home()) / ".local/share")
        env.update(SAI_LIVE_PROVIDER_LOGIN="1", SAI_LIVE_REAL_DATA_HOME=real)
    return None


def prepare_mcp(args, temporary, env, stdin=None, runner=run_command):
    """mcp-live's inputs, asked first (on a terminal; else --yes): the signed catalog as this checkout's sandbox-ai
    reads it (`mcp catalog --json` in the check's own configuration: SAI_LIVE_MCP_CATALOG, its names or --mcp-servers'
    in SAI_LIVE_MCP_SERVERS), the MCP repository's checkout (--mcp-repo, projects/sandbox-ai-mcp, else cloned from
    GitHub after the question: SAI_LIVE_MCP_REPO), its smoke files (tests/fixtures and scripts/fixtures.py's binaries:
    SAI_LIVE_MCP_FIXTURES), and the sandbox-ai home the servers are added to once for every test (SAI_LIVE_MCP_HOME).
    A problem or None."""
    only = [name.strip() for name in (args.mcp_servers or "").split(",") if name.strip()]
    repo = mcp_repo(args.mcp_repo)
    if args.mcp_repo and repo is None:
        return f"--mcp-repo {args.mcp_repo}: no servers/ there (a checkout of sandbox-ai-mcp)"
    vpn = getattr(args, "mcp_vpn", None)
    if vpn is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", vpn):
        return f"--mcp-vpn {vpn}: a profile's name is 1-32 letters, digits, '_' or '-'"
    count = f"{len(only)} of its servers ({', '.join(only)})" if only else "every server"
    question = (f"mcp-live: {count} of the signed MCP catalog, each added in the check's own configuration (its "
                "image pulled from ghcr.io, its rootfs for Bubblewrap from GitHub), started for real through a session "
                "of the check's own with Podman and Bubblewrap, and its smoke calls made (the servers with network "
                "reach the services their calls name)"
                + (f"; those with network behind the Tor profile {vpn}, made in the check's own configuration (their "
                   "calls go through Tor; the others are skipped)" if vpn else "")
                + ("" if repo else f"; the MCP repository's smoke data cloned from {MCP_REPOSITORY}") + ". Run it?")
    if not (args.yes or ask(question, stdin)):
        return "not run: not confirmed (on a terminal it asks; else --yes)"
    status, out, err = runner([str(SAI), "mcp", "catalog", "--json"], timeout=300, env=env)
    try:
        catalog = json.loads(out)
    except ValueError:
        catalog = None
    if status != 0 or not isinstance(catalog, dict):
        return "sandbox-ai mcp catalog --json failed: " + ((err or out).strip()[-300:] or f"exit {status}")
    names = mcp_catalog_names(catalog, only)
    unknown = [name for name in only if name not in names]
    if unknown:
        return f"--mcp-servers: not in the signed catalog: {', '.join(unknown)}"
    if not names:
        return "the signed catalog lists no server (its problems: " + "; ".join(
            str(p) for p in catalog.get("problems") or []) + ")"
    given = temporary / "given"
    given.mkdir(mode=0o700, parents=True, exist_ok=True)
    (given / "mcp-catalog.json").write_text(json.dumps(catalog))
    if repo is None:
        repo = temporary / "sandbox-ai-mcp"
        status, _, err = runner(["git", "clone", "--quiet", "--depth", "1", MCP_REPOSITORY, str(repo)], timeout=900,
                                env=env)
        if status != 0:
            return f"git clone {MCP_REPOSITORY} failed: {err.strip()[-300:]} (give --mcp-repo DIR)"
    fixtures = temporary / "mcp-fixtures"
    if (repo / "tests" / "fixtures").is_dir():
        shutil.copytree(repo / "tests" / "fixtures", fixtures, symlinks=True, dirs_exist_ok=True)
    fixtures.mkdir(parents=True, exist_ok=True)
    if (repo / "scripts" / "fixtures.py").is_file():
        # (its binary inputs, each checked against its SHA-256: as its CI writes them for --var FIXTURES=DIR)
        status, _, err = runner([sys.executable, "-I", str(repo / "scripts" / "fixtures.py"), "--out", str(fixtures)],
                                timeout=300, env=env, cwd=str(temporary))
        if status != 0:
            return f"the MCP repository's scripts/fixtures.py failed: {err.strip()[-300:]}"
    home = temporary / "mcp-home"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    env.update(SAI_LIVE_MCP_CATALOG=str(given / "mcp-catalog.json"), SAI_LIVE_MCP_SERVERS=",".join(names),
               SAI_LIVE_MCP_REPO=str(repo), SAI_LIVE_MCP_FIXTURES=str(fixtures), SAI_LIVE_MCP_HOME=str(home))
    if vpn:
        env["SAI_LIVE_MCP_VPN"] = vpn
    args.mcp_said = f"{len(names)} servers of the signed catalog; smoke data of {repo}" + (
        f"; those with network behind the Tor profile {vpn}" if vpn else "")
    return None


def preset_rows(facts):
    """presets-live per preset: [{"preset", "agent", "podman", "bwrap", "part"}] from what each run wrote into FACTS
    (SAI_LIVE_PRESET_DIR/NAME.BACKEND.json): started (with its seconds), skipped (why), failed (its last line); its
    part: what came up (each MCP server, each service), "-" for a preset without one."""
    rows = {}
    for path in sorted(Path(facts).glob("*.json")):
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        name, backend = found.get("preset"), found.get("backend")
        if not isinstance(name, str) or backend not in ("podman", "bwrap"):
            continue
        row = rows.setdefault(name, {"preset": name, "agent": found.get("agent") or "", "podman": "not run",
                                     "bwrap": "not run", "part": "-", "by_hand": found.get("by_hand") or ""})
        outcome = found.get("outcome")
        if outcome == "passed":
            row[backend] = "started" + (f" ({found['seconds']:.0f} s)" if found.get("seconds") else "")
        else:
            row[backend] = f"{outcome}: {found.get('why') or '?'}"[:400]
        parts = found.get("parts") or []
        if parts and outcome == "passed":
            row["part"] = "came up: " + ", ".join(part for part, seen in parts if seen)
        elif parts and row["part"] == "-":
            row["part"] = "not up: " + ", ".join(part for part, seen in parts if not seen)
    return list(rows.values())


if __name__ == "__main__":
    sys.exit(main())
