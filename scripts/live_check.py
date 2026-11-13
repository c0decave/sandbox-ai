#!/usr/bin/env python3
"""Live check of sandbox-ai on this host: what the unit tests can only fake, against the real Podman and Bubblewrap.

Run it on the host, in a checkout (not inside a sandbox: there is no container engine there):

    scripts/live_check.py                        both backends, every live suite that needs no login
    scripts/live_check.py --backend podman       one backend
    scripts/live_check.py --vpn-profile NAME     also a start behind that VPN profile (sandbox-ai vpn add ...)
    scripts/live_check.py --build                build the images first (sandbox-ai build codex, vpn, tor): this
                                                 checks the build's DNS preflight too
    scripts/live_check.py --quick                only the new checks and the end-to-end suite

It starts real sandboxes around small scripts (sandbox-ai codex --no-auth --binary ...): no login, no model, no
key leaves this host. The live suites are tests/test_live_check.py (a workspace added and removed while running,
a ruleset refusing a request, a canary read, a session's whole life, the VPN), tests/test_live_e2e.py (proxy,
push check, network changes, devices, MCP, limits, approvals), tests/test_agent_sandbox_live.py and
tests/test_escape_live.py (namespaces, mounts, egress, an escape probe), without their tests that need a login.

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

    def write(self, facts):
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
        failed = [c for c in self.checks if c["outcome"] in ("failed", "error")]
        if failed:
            lines += ["", "## What failed", ""]
            for check in failed:
                lines += [f"### {check['check']}", "", "```", check["detail"], "```", ""]
        (self.directory / "report.md").write_text("\n".join(lines) + "\n")
        return counts


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
