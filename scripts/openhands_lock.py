#!/usr/bin/env python3
"""Make sandbox/Containerfile.openhands-python.lock: the hash-pinned Python side of the reviewed OpenHands release.

  scripts/openhands_lock.py [--out FILE]

The packages and their versions come from sandbox/openhands.py (PYTHON_PACKAGES: the agent server, the SDK, its tools
and workspace at SERVER_VERSION, the automation backend at AUTOMATION_VERSION; PYTHON, LOCK_BEFORE; the review:
docs/OPENHANDS_REVIEW.md). uv resolves their dependency ranges from pypi.org with --exclude-newer LOCK_BEFORE, so every
version in the lock was published at least 14 days before the review (sandbox-ai's rule for third-party releases, as
scripts/agent_npm_lock.py's --before for npm), for every platform at once (--universal), with the SHA-256 of each of
its files; only the requirements that apply to Linux (x86_64 and aarch64) on CPython PYTHON are kept (the filters are
hermes_lock.py's), so the image's `uv pip install --require-hashes` never meets one it cannot hash. Nothing is
installed and nothing of the packages runs. Needs uv and the packaging module.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sandbox" / "Containerfile.openhands-python.lock"
HEADER = """\
# OpenHands' Python packages (agent server, SDK, tools, workspace, automation backend) and every
# dependency, hash-pinned: the image installs exactly these (uv pip install --require-hashes) from
# pypi.org. Made by scripts/openhands_lock.py from sandbox/openhands.py (PYTHON_PACKAGES, LOCK_BEFORE;
# docs/OPENHANDS_REVIEW.md): versions published before LOCK_BEFORE only, Linux on CPython 3.12 only.
# Do not edit by hand: a new release is reviewed, then this is made anew.
"""


def openhands_module():
    spec = importlib.util.spec_from_file_location("sandbox_ai_openhands", ROOT / "sandbox" / "openhands.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hermes_lock(python):
    """scripts/hermes_lock.py: the requirement filters (blocks, applies, keep), shared."""
    spec = importlib.util.spec_from_file_location("hermes_lock", Path(__file__).resolve().parent / "hermes_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HEADER = HEADER
    module.PLATFORMS = [{"python_version": python, "python_full_version": python + ".0",
                         "implementation_name": "cpython",
                         "platform_python_implementation": "CPython", "sys_platform": "linux", "os_name": "posix",
                         "platform_system": "Linux", "platform_machine": machine, "platform_release": "6.12.0"}
                        for machine in ("x86_64", "aarch64")]
    return module


def compile_lock(module, workdir):
    """uv pip compile's requirements (with hashes, no comments) for MODULE's packages."""
    requirements = Path(workdir) / "requirements.in"
    requirements.write_text("\n".join(module.PYTHON_PACKAGES) + "\n")
    command = ["uv", "pip", "compile", str(requirements), "--universal", "--python-version", module.PYTHON,
               "--exclude-newer", module.LOCK_BEFORE, "--generate-hashes", "--no-header", "--no-annotate",
               "--index-url", "https://pypi.org/simple"]
    # The caller's environment (a proxy and its CA, where it has them), without a uv configuration of its own.
    env = {**os.environ, "UV_NO_CONFIG": "1", "UV_CACHE_DIR": str(Path(workdir) / "cache")}
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True, env=env).stdout


def check(text, module):
    """TEXT (the lock) pins every package of MODULE.PYTHON_PACKAGES at its version, each requirement hashed;
    ValueError names what is wrong. Returns the number of requirements."""
    lines = [line for line in text.splitlines() if line and not line.startswith((" ", "\t", "#"))]
    pinned = {line.split(" ", 1)[0].split(";", 1)[0].strip() for line in lines}
    missing = [package for package in module.PYTHON_PACKAGES if package not in pinned]
    if missing:
        raise ValueError(f"the lock does not pin {', '.join(missing)}")
    if any("==" not in line for line in lines):
        raise ValueError("a requirement without an exact version")
    return len(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    module = openhands_module()
    with tempfile.TemporaryDirectory(prefix="openhands-lock-") as workdir:
        text = hermes_lock(module.PYTHON).keep(compile_lock(module, workdir))
    check(text, module)
    args.out.write_text(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
