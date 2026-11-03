#!/usr/bin/env python3
"""sandbox-ai: one command for everything.

  sandbox-ai claude|codex|opencode [options] [-- agent arguments]   start an agent (= claude-sandbox ...)
  sandbox-ai session list|attach|pause|resume|stop|delete ...       detachable sessions
  sandbox-ai mcp list|start|stop ...                                MCP server sandboxes
  sandbox-ai login claude|codex|opencode | --status                  log in inside a sandbox; kept for all sandboxes
  sandbox-ai plugins [claude [--update] | --status]                  install the plugins the kits declare
  sandbox-ai wizard [new|edit|trust|forget|models] ...               project setup
  sandbox-ai build [--pin VERSION] AGENT|all ...                     build the images
  sandbox-ai doctor [--backend podman|bwrap|both]                    check the host
  sandbox-ai logs [DIR|--project PATH]                               blocked egress and file access, with suggestions
  sandbox-ai privacy-check [PATH ...]                                what the next push would publish that should stay private
  sandbox-ai net list|show|allow|revoke ...                          change what a running sandbox may reach
  sandbox-ai approvals list|allow|deny|history ...                   answer what running sandboxes ask
  sandbox-ai secrets list|set|delete|backend ...                     API keys and tokens in your keyring
  sandbox-ai images [packages AGENT] [--json]                        the agent images and their contents
  sandbox-ai update [--check]                                        update an installation made by install.sh
  sandbox-ai version

The older commands (claude-sandbox, sandbox-session, ...) remain as shortcuts.
"""
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
PYTHON_COMMANDS = {"claude": ("agent_sandbox.py", "claude"), "codex": ("agent_sandbox.py", "codex"),
                   "opencode": ("agent_sandbox.py", "opencode"), "session": ("sessions.py",),
                   "mcp": ("mcp.py",), "wizard": ("wizard.py",), "login": ("login.py",), "plugins": ("plugins.py",), "doctor": ("doctor.py",), "logs": ("logs.py",),
                   "privacy-check": ("privacy.py", "check"), "net": ("net.py",),
                   "approvals": ("approvals.py",), "secrets": ("secret_store.py",),
                   "images": ("images.py",)}


def version():
    for line in (HERE / "agent_sandbox.py").read_text().splitlines():
        if line.startswith("__version__ = "):
            return line.split('"')[1]
    return "unknown"


def bash(script, args):
    executable = shutil.which("bash")
    if not executable:
        sys.exit("sandbox-ai: bash is required")
    os.execv(executable, [executable, str(script), *args])


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command in ("version", "--version", "-V"):
        print(f"sandbox-ai {version()}")
        return 0
    if command in PYTHON_COMMANDS:
        script, *prefix = PYTHON_COMMANDS[command]
        os.execv(sys.executable, [sys.executable, "-I", str(HERE / script), *prefix, *rest])
    if command == "build":
        bash(HERE / "build_image.sh", rest)
    if command == "update":
        installer = HERE.parent / "install.sh"
        if not installer.is_file() or (HERE.parent / ".git").exists():
            print("sandbox-ai update: this copy was not installed by install.sh; update a checkout with git, "
                  "a pip installation with: pip install -U sandbox-ai", file=sys.stderr)
            return 1
        bash(installer, rest)
    print(f"sandbox-ai: unknown command {command!r}; see sandbox-ai --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
