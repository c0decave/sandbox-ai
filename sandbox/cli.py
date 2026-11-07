"""Console entry points of the PyPI package.

Each command runs its module as a script in isolated mode (python -I), exactly
like the wrapper scripts of a checkout: the launcher loads runtime.py by path and mounts
it into the sandbox, so the modules must run from their files.
"""
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent


def _python(script, *prefix):
    path = str(HERE / script)
    os.execv(sys.executable, [sys.executable, "-I", path, *prefix, *sys.argv[1:]])


def deprecated(old, new):
    """The single commands (claude-sandbox, sandbox-session, ...) are deprecated since 0.1.30 and go in a later
    version: one line on stderr names the sandbox-ai equivalent, unless SANDBOX_AI_NO_DEPRECATION=1."""
    if os.environ.get("SANDBOX_AI_NO_DEPRECATION") != "1":
        print(f"{old}: deprecated, to be removed in a later version; use: {new} (SANDBOX_AI_NO_DEPRECATION=1 hides "
              "this note)", file=sys.stderr)


def claude():
    deprecated("claude-sandbox", "sandbox-ai claude")
    _python("agent_sandbox.py", "claude")


def codex():
    deprecated("codex-sandbox", "sandbox-ai codex")
    _python("agent_sandbox.py", "codex")


def opencode():
    deprecated("opencode-sandbox", "sandbox-ai opencode")
    _python("agent_sandbox.py", "opencode")


def session():
    deprecated("sandbox-session", "sandbox-ai list|attach|pause|resume|stop|delete")
    _python("sessions.py")


def mcp():
    deprecated("sandbox-mcp", "sandbox-ai mcp")
    _python("mcp.py")


def wizard():
    deprecated("sandbox-wizard", "sandbox-ai wizard")
    _python("wizard.py")


def main():
    _python("main.py")


def build_image():
    deprecated("sandbox-build-image", "sandbox-ai build")
    bash = shutil.which("bash")
    if not bash:
        sys.exit("sandbox-build-image: bash is required")
    os.environ["SANDBOX_AI_PYTHON"] = sys.executable  # the extension layers are built by images.py
    os.execv(bash, [bash, str(HERE / "build_image.sh"), *sys.argv[1:]])
