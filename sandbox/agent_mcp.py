#!/usr/bin/env python3
"""An agent as an MCP server (stdio, standard library only): the helper of another agent.

Runs in the helper's own sandbox (`--helper AGENT` on the lead's start): its login, its provider's hosts, its
kits, rules and hooks. The lead calls one tool:

  ask(prompt, continue=false)   the helper does the task in /work and answers with its final message

`ask` runs the agent's non-interactive mode: `claude -p`, `codex exec`, `opencode run`. With `continue` it goes on
in the helper's last conversation. One task at a time (a lock in HOME, whichever connection asks); a task that runs
longer than the limit is stopped. The lead shares /work: Claude reads only its own and the managed settings and no
project MCP servers there (--setting-sources user, --strict-mcp-config), Codex does not trust the folder and
OpenCode reads no project configuration (the launcher sets OPENCODE_DISABLE_PROJECT_CONFIG); Git runs no fsmonitor,
repository hooks or .gitattributes filters (the launcher's GIT_CONFIG_* and GIT_ATTR_SOURCE). That makes planting
code harder, not impossible: other Git settings in /work/.git/config and the instructions in /work still reach the
helper (README: helpers, limits).

    python3 agent_mcp.py AGENT [--limit SECONDS] [--skip-permissions]
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading

VERSION = "1.0.0"
PROTOCOL = "2025-06-18"
AGENTS = ("claude", "codex", "opencode")
LIMIT = 1800          # seconds a task may run
LOCK = Path(os.environ.get("HOME") or "/root") / ".cache" / "sandbox-ai-helper.lock"
ANSWER_LIMIT = 200 << 10  # characters of an answer; the rest is cut, and the answer says so
PROMPT_LIMIT = 256 << 10
WORK = "/work"
# The agents' own switch to work without asking: given only with --skip-permissions, which the launcher passes
# when the lead was started with --dangerously-skip-permissions (the helper's sandbox holds either way).
SKIP = {"claude": ["--dangerously-skip-permissions"], "codex": ["--dangerously-bypass-approvals-and-sandbox"],
        "opencode": []}


def tools(agent):
    return [{"name": "ask",
             "description": f"Give {agent} (a helper agent in a sandbox of its own, working in /work) a task; it "
                            "answers with its final message when done. Set continue to go on in its last "
                            "conversation.",
             "inputSchema": {"type": "object", "required": ["prompt"],
                             "properties": {"prompt": {"type": "string", "description": "The task"},
                                            "continue": {"type": "boolean",
                                                         "description": "Go on in the last conversation"}}}}]


def command(agent, prompt, resume=False, skip=False, last_message=None):
    """argv of AGENT's non-interactive run of PROMPT. The prompt is one argument after "--" where the agent takes
    it, so a prompt starting with "-" is never an option."""
    extra = SKIP[agent] if skip else []
    if agent == "claude":
        return ["claude", "-p", "--output-format", "json", "--setting-sources", "user", "--strict-mcp-config",
                *(["--continue"] if resume else []), *extra, "--", prompt]
    if agent == "codex":
        # Codex's own sandbox cannot set up inside sandbox-ai's (as for every Codex the launcher starts): the
        # helper's sandbox is the boundary. exec's own options before its subcommand (resume), the prompt last.
        extra = extra or ["--sandbox", "danger-full-access"]
        return ["codex", "exec", "--skip-git-repo-check", *extra,
                *(["--output-last-message", last_message] if last_message else []),
                *(["resume", "--last"] if resume else []), "--", prompt]
    if agent == "opencode":
        return ["opencode", "run", *(["--continue"] if resume else []), *extra, "--", prompt]
    raise ValueError(f"unknown agent {agent!r}")


def answer_of(agent, out, last_message=None):
    """The final message of a run: Claude's JSON "result", Codex's last message file, OpenCode's output."""
    if agent == "claude":
        try:
            document = json.loads(out)
        except ValueError:
            return out.strip()
        if isinstance(document, dict) and isinstance(document.get("result"), str):
            return document["result"]
        return out.strip()
    if agent == "codex" and last_message:
        try:
            text = Path(last_message).read_text(errors="replace").strip()
        except OSError:
            text = ""
        return text or out.strip()
    return out.strip()


class Helper:
    def __init__(self, agent, limit=LIMIT, skip=False, run=subprocess.Popen, cwd=WORK, lock=None):
        if agent not in AGENTS:
            raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
        self.agent, self.limit, self.skip, self.popen, self.cwd = agent, limit, skip, run, cwd
        self.lock_path = Path(lock or LOCK)
        self.lock = threading.Lock()  # this connection's calls; the file lock holds across connections

    def ask(self, prompt, resume=False):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > PROMPT_LIMIT:
            raise ValueError(f"the prompt is longer than {PROMPT_LIMIT} characters")
        if not isinstance(resume, bool):
            raise ValueError("continue must be true or false")
        if not self.lock.acquire(blocking=False):
            raise ValueError(f"{self.agent} is busy with another task; ask again when it is done")
        held = None
        try:
            # Every connection gets a server of its own: the agents' one HOME and one workspace are guarded by a
            # lock file, so a second lead (or a reconnect) waits its turn instead of running beside the first.
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            held = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(f"{self.agent} is busy with another task; ask again when it is done") from None
            with tempfile.TemporaryDirectory(prefix="helper-") as scratch:
                last = os.path.join(scratch, "last-message.txt") if self.agent == "codex" else None
                argv = command(self.agent, prompt, resume, self.skip, last)
                process = self.popen(argv, cwd=self.cwd if os.path.isdir(self.cwd) else None, text=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
                try:
                    out, err = process.communicate(timeout=self.limit)
                except subprocess.TimeoutExpired:
                    stop(process)
                    raise ValueError(f"{self.agent} did not finish within {self.limit} s; the task was stopped")
                text = answer_of(self.agent, out or "", last)
                if process.returncode != 0:
                    detail = (err or "").strip()[-2000:] or text[-2000:]
                    raise ValueError(f"{self.agent} ended with exit status {process.returncode}: {detail}")
        finally:
            if held is not None:
                os.close(held)
            self.lock.release()
        if len(text) > ANSWER_LIMIT:
            text = text[:ANSWER_LIMIT] + f"\n[answer cut at {ANSWER_LIMIT} characters]"
        return text or f"({self.agent} gave no answer)"

    def handle(self, message):
        """One JSON-RPC message in, the reply (or None for notifications) out."""
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            return None
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                      "serverInfo": {"name": f"sandbox-ai-helper-{self.agent}", "version": VERSION}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools(self.agent)}
        elif method == "tools/call":
            arguments = params.get("arguments") or {}
            try:
                if params.get("name") != "ask":
                    raise ValueError(f"Unknown tool: {params.get('name')}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                text, failed = self.ask(arguments.get("prompt"), arguments.get("continue", False)), False
            except (OSError, ValueError) as exc:
                text, failed = str(exc), True
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout):
        """stdio transport: one JSON-RPC message per line. Calls are answered in turn (a second task while one
        runs would be refused anyway)."""
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                reply = self.handle(message) if isinstance(message, dict) else {
                    "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def stop(process):
    """End PROCESS with everything it started (it leads its own session)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            process.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            continue


def main(argv):
    if not argv or argv[0] not in AGENTS:
        print(f"usage: agent_mcp.py {{{','.join(AGENTS)}}} [--limit SECONDS] [--skip-permissions]", file=sys.stderr)
        return 2
    agent, rest, limit, skip = argv[0], argv[1:], LIMIT, False
    while rest:
        if rest[0] == "--limit" and len(rest) > 1 and rest[1].isdigit() and 0 < int(rest[1]) <= 86400:
            limit, rest = int(rest[1]), rest[2:]
        elif rest[0] == "--skip-permissions":
            skip, rest = True, rest[1:]
        else:
            print(f"agent_mcp.py: unknown argument {rest[0]!r}", file=sys.stderr)
            return 2
    Helper(agent, limit, skip).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
