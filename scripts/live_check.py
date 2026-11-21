#!/usr/bin/env python3
"""Live check of sandbox-ai on this host: what the unit tests can only fake, against the real Podman and Bubblewrap.

Run it on the host, in a checkout (not inside a sandbox: there is no container engine there):

    scripts/live_check.py                        both backends, every live suite that needs no login
    scripts/live_check.py --backend podman       one backend
    scripts/live_check.py --vpn-profile NAME     also a start behind that VPN profile (sandbox-ai vpn add ...)
    scripts/live_check.py --build                build the images first (sandbox-ai build codex, vpn, tor): this
                                                 checks the build's DNS preflight too
    scripts/live_check.py --quick                only the new checks and the end-to-end suite
    scripts/live_check.py --agents [NAME,...] [--build]
                                                 the agents and parts not live-tested before (hermes, qwen, dsh,
                                                 goose, openhands, nanobot, letta, pi, opencode2, cairn, browser; default
                                                 all), with
                                                 Podman only: each against a scripted model on this host, one MCP
                                                 tool, its switches' hosts, no telemetry (scripts/live_check_agents.sh
                                                 is the short form)

It starts real sandboxes around small scripts (sandbox-ai codex --no-auth --binary ...): no login, no model, no
key leaves this host. The live suites are tests/test_live_check.py (a workspace added and removed while running,
a ruleset refusing a request, a canary read, a session's whole life, the VPN), tests/test_live_e2e.py (proxy,
push check, network changes, devices, MCP, limits, approvals), tests/test_agent_sandbox_live.py and
tests/test_escape_live.py (namespaces, mounts, egress, an escape probe), without their tests that need a login.
With --agents: tests/test_hermes_live.py, test_qwen_live.py, test_dsh_live.py, test_goose_live.py,
test_nanobot_live.py, test_openhands_live.py, test_letta_live.py, test_opencode2_live.py, test_pi_live.py, test_cairn_live.py and
test_browser_live.py
(tests/live_agents.py says what each checks); --build builds their images first (codex for
the demo MCP server; claude before cairn and browser). The report adds what each agent reached and was
refused (the hosts, the telemetry hosts, the tool it called) - the part to send back.

The result: DIR/report.md and DIR/report.json (default ~/.local/state/sandbox-ai/live-check/DATE-TIME), one line
per check with what failed. Exit status 0 when nothing failed. pytest comes from requirements-ci.txt (hashes
checked) into DIR/venv when this Python has none.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
SUITES = ["tests/test_live_check.py", "tests/test_live_e2e.py", "tests/test_agent_sandbox_live.py",
          "tests/test_escape_live.py"]
QUICK = SUITES[:2]
# Tests that need an agent's login or a model (and send prompts to a provider): not part of this check.
NEEDS_LOGIN = "not authenticated and not practical and not model_response and not provider"


def run(command, timeout=600, **kwargs):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              **kwargs)
        return done.returncode, done.stdout, done.stderr
    except FileNotFoundError:
        return 127, "", f"{command[0]}: not found"
    except subprocess.TimeoutExpired as exc:
        # (bytes even with text=True: what was read before the timeout)
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return 124, out, f"timed out after {timeout} s"


class Report:
    def __init__(self, directory):
        self.directory, self.checks = directory, []

    def add(self, name, outcome, detail="", seconds=None):
        self.checks.append({"check": name, "outcome": outcome, "detail": detail.strip()[-4000:],
                            "seconds": round(seconds, 1) if seconds is not None else None})
        mark = {"passed": "ok  ", "failed": "FAIL", "skipped": "skip", "error": "ERR "}.get(outcome, outcome)
        print(f"  {mark}  {name}" + (f"  ({detail.strip().splitlines()[0][:120]})" if detail.strip() and
                                     outcome != "passed" else ""), flush=True)

    def write(self, facts, extra=()):
        counts = {}
        for check in self.checks:
            counts[check["outcome"]] = counts.get(check["outcome"], 0) + 1
        (self.directory / "report.json").write_text(json.dumps({"facts": facts, "counts": counts,
                                                                "checks": self.checks}, indent=2))
        lines = ["# sandbox-ai live check", "", *(f"- {k}: {v}" for k, v in facts.items()), "",
                 "Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
                 "| outcome | check | detail |", "|---|---|---|"]
        for check in self.checks:
            detail = check["detail"].replace("|", "\\|").replace("\n", " ")[:300]
            lines.append(f"| {check['outcome']} | {check['check']} | {detail} |")
        if extra:
            lines += ["", *extra]
        failed = [c for c in self.checks if c["outcome"] in ("failed", "error")]
        if failed:
            lines += ["", "## What failed", ""]
            for check in failed:
                lines += [f"### {check['check']}", "", "```", check["detail"], "```", ""]
        (self.directory / "report.md").write_text("\n".join(lines) + "\n")
        return counts


# --agents: the suite of each, and the images it needs (built in this order with --build).
AGENT_SUITES = {"hermes": "tests/test_hermes_live.py", "qwen": "tests/test_qwen_live.py",
                "dsh": "tests/test_dsh_live.py", "goose": "tests/test_goose_live.py",
                "openhands": "tests/test_openhands_live.py",
                "nanobot": "tests/test_nanobot_live.py",
                "letta": "tests/test_letta_live.py",
                "opencode2": "tests/test_opencode2_live.py",
                "pi": "tests/test_pi_live.py",
                "cairn": "tests/test_cairn_live.py", "browser": "tests/test_browser_live.py"}
AGENT_BUILDS = {"hermes": ["codex", "hermes"], "qwen": ["codex", "qwen"], "dsh": ["codex", "dsh"],
                "goose": ["codex", "goose"], "openhands": ["codex", "openhands"], "nanobot": ["codex", "nanobot"],
                "letta": ["codex", "letta"], "pi": ["codex", "pi"], "opencode2": ["codex", "opencode2"], "cairn": ["claude", "cairn", "browser"], "browser": ["claude", "browser"]}


def agent_names(value):
    """The agents of --agents VALUE (comma-separated; "all" or none given: every one), in AGENT_SUITES' order.
    SystemExit names an unknown one."""
    wanted = [part.strip() for part in str(value or "all").split(",") if part.strip()]
    if not wanted or "all" in wanted:
        return list(AGENT_SUITES)
    unknown = [name for name in wanted if name not in AGENT_SUITES]
    if unknown:
        raise SystemExit(f"live_check: --agents {', '.join(unknown)}: not one of {', '.join(AGENT_SUITES)}")
    return [name for name in AGENT_SUITES if name in wanted]


def agent_builds(names):
    """The `sandbox-ai build` targets the agents NAMES need, each once, in order."""
    return list(dict.fromkeys(target for name in names for target in AGENT_BUILDS[name]))


def agents_summary(directory):
    """Markdown lines of what the agents' checks saw ($SANDBOX_AI_LIVE_REPORT_DIR: one JSON file per check, written by
    tests/live_agents.py): per check the answer, the tool, the hosts reached and refused, the telemetry hosts."""
    lines = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        parts = []
        for key in ("exit", "answered", "tool_called", "mcp_gateway_calls", "telemetry_reached", "telemetry_refused",
                    "reached", "refused", "lookups", "serve", "listen", "seen", "token_in_a_prompt"):
            if key in data:
                parts.append(f"{key}: {json.dumps(data[key], sort_keys=True)[:600]}")
        lines.append(f"- **{path.stem}**: " + "; ".join(parts))
    return lines


def agents_main(args, directory, report, facts):
    """--agents: build (with --build) and run the live checks of the agents and parts not live-tested before, with
    Podman; the report adds what each check saw."""
    names = agent_names(args.agents)
    facts["agents"] = ", ".join(names)
    if not shutil.which("podman"):
        report.add("podman installed", "failed", "podman is not on PATH: these checks start Podman's images")
        return report.write(facts)
    status, out, err = run(["podman", "--version"], timeout=60)
    facts["podman"] = (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {status}"
    if args.build:
        for target in agent_builds(names):
            begin = datetime.datetime.now()
            status, out, err = run([str(SAI), "build", target], timeout=3600)
            report.add(f"sandbox-ai build {target}", "passed" if status == 0 else "failed",
                       (out + err)[-3000:] if status else "", (datetime.datetime.now() - begin).total_seconds())
    python = pytest_python(directory)
    seen = directory / "agents"
    seen.mkdir(exist_ok=True)
    junit = directory / "live-agents.xml"
    junit.unlink(missing_ok=True)
    command = [python, "-X", "faulthandler", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfEs", "--agent-live",
               "--agent-backend", "podman", f"--junitxml={junit}", *(AGENT_SUITES[name] for name in names)]
    print(f"  ...   the live checks of {', '.join(names)} (this takes a while)", flush=True)
    begin = datetime.datetime.now()
    status, out, err = run(command, timeout=4 * 3600, cwd=ROOT, env={**os.environ, "SANDBOX_AI_LIVE_REPORT_DIR": str(seen)})
    (directory / "pytest-agents.txt").write_text(out + err)
    results = junit_results(junit)
    if not results or status != 0 and not any(outcome in ("failed", "error") for _, outcome, _, _ in results):
        report.add("pytest (agents)", "error", f"exit status {status}\n" + (out + err)[-3000:],
                   (datetime.datetime.now() - begin).total_seconds())
    for name, outcome, detail, seconds in results:
        report.add(name, outcome, detail if outcome != "passed" else "", seconds)
    lines = agents_summary(seen)
    return report.write(facts, ["## What the agents reached", "", *(lines or ["(no check wrote what it saw)"]), ""])


def pytest_python(directory):
    """A Python with pytest: this one, else a venv in DIRECTORY with requirements-ci.txt (hashes checked)."""
    if run([sys.executable, "-c", "import pytest"])[0] == 0:
        return sys.executable
    venv = directory / "venv"
    python = venv / "bin" / "python"
    if not python.exists():
        print("  ...   pytest into a venv of its own (packaging/requirements-ci.txt)", flush=True)
        status, _, err = run([sys.executable, "-m", "venv", str(venv)])
        if status == 0:
            status, _, err = run([str(python), "-m", "pip", "install", "-q", "--require-hashes", "--only-binary",
                                  ":all:", "-r", str(ROOT / "packaging/requirements-ci.txt")], timeout=900)
        if status != 0:
            raise SystemExit(f"live_check: pytest could not be installed: {err.strip()[-500:]}")
    return str(python)


def junit_results(path):
    """[(name, outcome, detail, seconds)] of a junit XML file."""
    results = []
    try:
        tree = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError):
        return results
    for case in tree.iter("testcase"):
        name = f"{Path(case.get('file') or case.get('classname', '').replace('.', '/')).stem}::{case.get('name')}"
        outcome, detail = "passed", ""
        for kind in ("failure", "error", "skipped"):
            found = case.find(kind)
            if found is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[kind]
                detail = (found.get("message") or "") + "\n" + (found.text or "")
                break
        results.append((name, outcome, detail, float(case.get("time") or 0)))
    return results


def inside_a_container():
    """Podman and Docker mark their containers; a sandbox of sandbox-ai is one (or runs under Bubblewrap with
    /sandbox mounted)."""
    return any(os.path.exists(p) for p in ("/run/.containerenv", "/.dockerenv", "/sandbox/log"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parser.add_argument("--backend", choices=["both", "podman", "bwrap"], default="both")
    parser.add_argument("--vpn-profile", metavar="NAME")
    parser.add_argument("--build", action="store_true", help="build the codex, vpn and tor images first")
    parser.add_argument("--quick", action="store_true", help="only tests/test_live_check.py and test_live_e2e.py")
    parser.add_argument("--image", metavar="IMAGE", help="the agent image for Podman (default: each agent's own)")
    parser.add_argument("--out", type=Path, metavar="DIR")
    parser.add_argument("--agents", nargs="?", const="all", metavar="NAME[,NAME...]",
                        help="the agents and parts not live-tested before, Podman only: "
                             f"{', '.join(AGENT_SUITES)} (default all); with --build their images first")
    args = parser.parse_args(argv)
    if inside_a_container():
        raise SystemExit("live_check: this looks like a container or a sandbox; run it on the host")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    directory = (args.out or state / "sandbox-ai" / "live-check" / stamp).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = Report(directory)
    print(f"sandbox-ai live check -> {directory}", flush=True)

    facts = {"date": stamp, "checkout": str(ROOT), "python": platform.python_version(),
             "kernel": platform.release(), "system": platform.system()}
    if args.agents is not None:
        status, out, err = run([str(SAI), "--version"], timeout=60)
        facts["sandbox-ai"] = (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {status}"
        counts = agents_main(args, directory, report, facts)
        print(f"\n{', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}\nreport: {directory / 'report.md'} "
              "(send it back)")
        for line in agents_summary(directory / "agents"):
            print(line[:400])
        return 1 if counts.get("failed") or counts.get("error") else 0
    for key, command in (("sandbox-ai", [str(SAI), "--version"]), ("podman", ["podman", "--version"]),
                         ("bwrap", ["bwrap", "--version"]), ("git", ["git", "-C", str(ROOT), "rev-parse", "--short",
                                                                     "HEAD"])):
        status, out, err = run(command, timeout=60)
        facts[key] = (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {status}"
    backends = ["podman", "bwrap"] if args.backend == "both" else [args.backend]
    for backend, program in (("podman", "podman"), ("bwrap", "bwrap")):
        if backend in backends and not shutil.which(program):
            report.add(f"{backend} installed", "failed", f"{program} is not on PATH")
            backends.remove(backend)

    for backend in backends:
        status, out, err = run([str(SAI), "doctor", "--json", "--backend", backend], timeout=300)
        report.add(f"sandbox-ai doctor ({backend})", "passed" if status == 0 else "failed",
                   "" if status == 0 else (out + err))

    if args.build:
        for target in ("all", "vpn", "tor"):
            begin = datetime.datetime.now()
            status, out, err = run([str(SAI), "build", target], timeout=3600)
            report.add(f"sandbox-ai build {target}", "passed" if status == 0 else "failed",
                       (out + err)[-3000:] if status else "", (datetime.datetime.now() - begin).total_seconds())
    # The agents whose image is missing: their tests are left out (said), the others run.
    missing = []
    if "podman" in backends and not args.image:
        for agent in ("codex", "claude", "opencode"):
            status, _, _ = run(["podman", "image", "exists", f"localhost/{agent}-sandbox:latest"], timeout=60)
            if status != 0:
                missing.append(agent)
            report.add(f"image localhost/{agent}-sandbox", "passed" if status == 0 else "skipped",
                       "" if status == 0 else f"missing: sandbox-ai build {agent} (or scripts/live_check.py --build); "
                                              f"its tests are left out")
        if "codex" in missing:
            report.add("image localhost/codex-sandbox (every new live check uses it)", "failed",
                       "sandbox-ai build codex")

    python = pytest_python(directory) if backends else None
    suites = QUICK if args.quick else SUITES
    for backend in backends:
        junit = directory / f"live-{backend}.xml"
        command = [python, "-X", "faulthandler", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfEs",
                   "--agent-live", "--agent-backend", backend,
                   "-k", " and ".join([NEEDS_LOGIN] + [f"not {agent}" for agent in missing]), f"--junitxml={junit}",
                   *(["--agent-image", args.image] if args.image else []),
                   *(["--live-vpn-profile", args.vpn_profile] if args.vpn_profile else []), *suites]
        print(f"  ...   the live suites with {backend} (this takes a while)", flush=True)
        begin = datetime.datetime.now()
        # A report of an earlier check in the same --out folder is never read as this one's (pytest killed by the
        # timeout writes none).
        junit.unlink(missing_ok=True)
        status, out, err = run(command, timeout=7200, cwd=ROOT)
        (directory / f"pytest-{backend}.txt").write_text(out + err)
        results = junit_results(junit)
        if not results or status != 0 and not any(outcome in ("failed", "error") for _, outcome, _, _ in results):
            # Nothing reported, or pytest failed without a failing test (an internal error, a crash at the end):
            # never a passing check.
            report.add(f"pytest ({backend})", "error", f"exit status {status}\n" + (out + err)[-3000:],
                       (datetime.datetime.now() - begin).total_seconds())
        for name, outcome, detail, seconds in results:
            report.add(f"{backend}: {name}", outcome, detail if outcome != "passed" else "", seconds)

    counts = report.write(facts)
    print(f"\n{', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}\nreport: {directory / 'report.md'}")
    return 1 if counts.get("failed") or counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
