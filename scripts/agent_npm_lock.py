#!/usr/bin/env python3
"""Make sandbox/Containerfile.AGENT.lock: the hash-pinned npm install of a reviewed Node.js agent.

  scripts/agent_npm_lock.py dsh|qwen|openhands|letta|pi [--out FILE]

The agent's package and version come from its module (sandbox/dsh.py, sandbox/qwen.py, sandbox/openhands.py,
sandbox/letta.py, sandbox/pi.py: PACKAGE, VERSION, LOCK_BEFORE; the reviews: docs/DSH_REVIEW.md,
docs/QWEN_CODE_REVIEW.md, docs/OPENHANDS_REVIEW.md, docs/LETTA_REVIEW.md, docs/PI_REVIEW.md). npm resolves the package's dependency ranges with
`--before LOCK_BEFORE`, so every version in the lock was published at least 14 days before the review (sandbox-ai's
rule for third-party releases), and records each package's sha512 integrity; nothing is installed and no script
runs (--package-lock-only --ignore-scripts). The image then installs exactly this lock with `npm ci --ignore-scripts`
(sandbox/Containerfile.AGENT), whose package.json is lock_package_json() of the same values. Needs npm.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("dsh", "qwen", "openhands", "letta", "pi")


def agent_module(agent):
    spec = importlib.util.spec_from_file_location(f"sandbox_ai_{agent}", ROOT / "sandbox" / f"{agent}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_json(agent, package, version):
    """The package.json the lock belongs to (the image writes the same one: lock_package_json)."""
    return {"name": f"sandbox-ai-{agent}", "private": True, "dependencies": {package: version}}


def check(lock, agent, package, version):
    """LOCK (a parsed package-lock.json) is the lock of exactly PACKAGE@VERSION with an integrity for every package;
    ValueError names what is wrong."""
    if not isinstance(lock, dict) or lock.get("lockfileVersion") != 3:
        raise ValueError("not an npm lock of lockfileVersion 3")
    packages = lock.get("packages") or {}
    root = packages.get("", {})
    if root.get("dependencies") != {package: version} or lock.get("name") != f"sandbox-ai-{agent}":
        raise ValueError(f"the lock is not of {package}@{version}")
    if packages.get(f"node_modules/{package}", {}).get("version") != version:
        raise ValueError(f"the lock does not hold {package}@{version}")
    # A bundled dependency (inBundle) ships inside its parent's tarball: the parent's integrity covers it.
    missing = sorted(path for path, entry in packages.items()
                     if path and not entry.get("link") and not entry.get("inBundle")
                     and not str(entry.get("integrity", "")).startswith("sha512-"))
    if missing:
        raise ValueError(f"no sha512 integrity for {', '.join(missing[:5])}")
    return len(packages) - 1


def make(agent, workdir):
    module = agent_module(agent)
    directory = Path(workdir)
    (directory / "package.json").write_text(json.dumps(package_json(agent, module.PACKAGE, module.VERSION)) + "\n")
    subprocess.run(["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund",
                    f"--before={module.LOCK_BEFORE}"], cwd=directory, check=True)
    lock = json.loads((directory / "package-lock.json").read_text())
    check(lock, agent, module.PACKAGE, module.VERSION)
    return json.dumps(lock, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("agent", choices=AGENTS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    out = args.out or ROOT / "sandbox" / f"Containerfile.{args.agent}.lock"
    with tempfile.TemporaryDirectory(prefix=f"{args.agent}-lock-") as workdir:
        out.write_text(make(args.agent, workdir))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
