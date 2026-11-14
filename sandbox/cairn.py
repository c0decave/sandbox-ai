#!/usr/bin/env python3
"""Cairn (oritera/Cairn, AGPL-3.0) in sandbox-ai: what was reviewed (docs/CAIRN_REVIEW.md) and how it runs.

Cairn is not an agent of sandbox-ai's own: it is a blackboard (Facts, Intents, Hints) with a server and a dispatcher
that hands tasks to coding agents. sandbox-ai builds it into an image on top of the team image (sandbox-ai build cairn:
Containerfile.cairn, its dependencies in Containerfile.cairn.lock) and offers it through the preset
cairn-pentest-board: Claude Code leads, Cairn's server listens on the sandbox's loopback, its dispatcher runs in
Cairn's local execution mode, so its workers are the claude and codex CLIs of the same sandbox, behind the same
firewall, proxy, approvals and rules. Cairn's Docker mode (worker containers through a Docker socket) is never used:
a Docker socket is the host. The constants here are what the image, the preset, the lock script and the tests share.

Standard library only.
"""

REPOSITORY = "https://github.com/oritera/Cairn"
# main, not a release: the local execution mode (commit 233f5e8505b9, 2026-07-15) came after v0.2.1 (2026-05-10);
# 8e7e0ea6 (2026-09-07, "docs: add logo") is main's newest commit and more than 14 days old.
COMMIT = "8e7e0ea67552383851dfcabfba0c4e9c8d007878"
COMMIT_DATE = "2026-09-07"
VERSION = "0.2.1+8e7e0ea6"  # pyproject.toml's version at that commit, with the commit
LICENSE = "AGPL-3.0"
PYTHON = "3.12"
IMAGE = "localhost/cairn-sandbox:latest"
SERVER = "http://127.0.0.1:8000"  # cairn serve's default address: the sandbox's loopback
DISPATCH_CONFIG = "/opt/cairn/dispatch.yaml"  # sandbox/recipes/cairn/dispatch.yaml in the image
PRESET = "cairn-pentest-board"
WORKERS = ("claude", "codex")  # Cairn's claudecode and codex drivers; its third, pi, is no agent of sandbox-ai's
