"""Console entry point of the PyPI package: sandbox-ai.

It runs main.py as a script in isolated mode (python -I), exactly like the sandbox-ai script of a checkout: the
launcher loads runtime.py by path and mounts it into the sandbox, so the modules must run from their files. The
older single commands (claude-sandbox, sandbox-session, sandbox-mcp, sandbox-wizard, sandbox-build-image, ...)
were removed after their deprecation (0.1.30): every one of them is a command of sandbox-ai.
"""
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def main():
    path = str(HERE / "main.py")
    os.execv(sys.executable, [sys.executable, "-I", path, *sys.argv[1:]])
