#!/usr/bin/env python3
"""A team's task board as an MCP server (stdio, standard library only): `sandbox-ai team`.

Each member of a team (one sandbox per agent, on a shared workspace) reaches the board through its relay as the
MCP server team-NAME. The board hands out tasks and carries the answers back:

  post(to, task, sender="")       a task for the member TO; returns its id
  wait_for_task(agent, seconds)   the next open task for AGENT (it is then "working"), or none within SECONDS
  done(id, answer)                the task's answer; it is "done"
  wait_for_answer(id, seconds)    the answer of task ID once it is done, or its state within SECONDS
  board()                         every task: id, from, to, state, the start of its text

The server sandbox starts one instance per connection, so the board lives in a file (BOARD in the working
directory, the server's private /work), changed under a lock. `sandbox-ai team post` writes the same file from
the host.

    python3 team_board.py --members claude,codex[,opencode] [--dir DIRECTORY]
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

VERSION = "1.0.0"
PROTOCOL = "2025-06-18"
BOARD, LOCK = "board.json", "board.lock"
TASK_LIMIT = 64 << 10      # characters of a task
ANSWER_LIMIT = 200 << 10   # characters of an answer
TASKS = 1000               # tasks the board keeps (the oldest done ones go first; open ones never)
BOARD_LIMIT = 48 << 20     # bytes of the board file; READ_LIMIT stays above it
READ_LIMIT = 64 << 20
WAIT_LIMIT = 300           # seconds a wait may last (an MCP call must come back)
WAIT_DEFAULT = 50          # below the 60 s an agent waits for an MCP call by default
MEMBER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
STATES = ("open", "working", "done")


def tools(members):
    who = {"type": "string", "enum": list(members)}
    seconds = {"type": "integer", "minimum": 0, "maximum": WAIT_LIMIT,
               "description": f"How long to wait (default {WAIT_DEFAULT})"}
    return [
        {"name": "post", "description": "Give a member of the team a task; returns its id.",
         "inputSchema": {"type": "object", "required": ["to", "task"],
                         "properties": {"to": who, "task": {"type": "string"},
                                        "sender": {"type": "string", "description": "Who posts it (you)"}}}},
        {"name": "wait_for_task", "description": "Wait for your next task (agent: your own name). It is yours then: "
                                                 "do it and report with done. A task of yours that is not done "
                                                 "yet comes again.",
         "inputSchema": {"type": "object", "required": ["agent"], "properties": {"agent": who, "seconds": seconds}}},
        {"name": "done", "description": "Report the answer of a task you did.",
         "inputSchema": {"type": "object", "required": ["id", "answer"],
                         "properties": {"id": {"type": "integer"}, "answer": {"type": "string"}}}},
        {"name": "wait_for_answer", "description": "Wait until a task you posted is done; its answer.",
         "inputSchema": {"type": "object", "required": ["id"],
                         "properties": {"id": {"type": "integer"}, "seconds": seconds}}},
        {"name": "board", "description": "Every task of the team with its state.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


class Board:
    def __init__(self, members, directory=".", clock=time.monotonic, sleep=time.sleep):
        members = [m for m in members if MEMBER.fullmatch(m)]
        if len(members) < 2 or len(set(members)) != len(members):
            raise ValueError("a team has at least two members, each named once")
        self.members, self.dir = members, Path(directory)
        self.clock, self.sleep = clock, sleep

    @contextlib.contextmanager
    def changed(self):
        """The board, locked; what the block leaves in it is written back (only if it changed)."""
        fd = os.open(self.dir / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            board = read(self.dir)
            before = json.dumps(board, sort_keys=True)
            yield board
            if json.dumps(board, sort_keys=True) != before:
                write(self.dir, board)
        finally:
            os.close(fd)

    def post(self, to, task, sender=""):
        if to not in self.members:
            raise ValueError(f"to must be one of {', '.join(self.members)}")
        text(task, "task", TASK_LIMIT)
        if not isinstance(sender, str) or len(sender) > 64:
            raise ValueError("sender must be a short string")
        with self.changed() as board:
            board["next"] += 1
            board["tasks"].append({"id": board["next"], "to": to, "from": sender, "task": task, "state": "open",
                                   "answer": None, "posted": stamp(), "updated": stamp()})
            prune(board)
            return {"id": board["next"]}  # (write() refuses a board over BOARD_LIMIT: nothing is lost then)

    def wait_for_task(self, agent, seconds=WAIT_DEFAULT):
        if agent not in self.members:
            raise ValueError(f"agent must be one of {', '.join(self.members)}")
        deadline = self.clock() + wait_time(seconds)
        while True:
            with self.changed() as board:
                # A task it took but never reported comes back first: the answer that handed it out may have
                # been lost (a call that ran past the agent's own timeout).
                for task in board["tasks"]:
                    if task["to"] == agent and task["state"] == "working":
                        return {"id": task["id"], "from": task["from"], "task": task["task"], "again": True}
                for task in board["tasks"]:
                    if task["to"] == agent and task["state"] == "open":
                        task.update(state="working", updated=stamp())
                        return {"id": task["id"], "from": task["from"], "task": task["task"]}
            if self.clock() >= deadline:
                return {"id": None, "task": None, "note": "no task yet; wait again"}
            self.sleep(1.0)

    def done(self, ident, answer):
        text(answer, "answer", ANSWER_LIMIT)
        with self.changed() as board:
            task = find(board, ident)
            if task["state"] == "done":
                raise ValueError(f"task {ident} is done already")
            task.update(state="done", answer=answer, updated=stamp())
            return {"id": ident, "state": "done"}

    def wait_for_answer(self, ident, seconds=WAIT_DEFAULT):
        deadline = self.clock() + wait_time(seconds)
        while True:
            with self.changed() as board:
                task = find(board, ident)
                if task["state"] == "done":
                    return {"id": ident, "state": "done", "answer": task["answer"]}
                state = task["state"]
            if self.clock() >= deadline:
                return {"id": ident, "state": state, "note": "not done yet; wait again"}
            self.sleep(1.0)

    def board(self):
        with self.changed() as board:
            return {"members": self.members,
                    "tasks": [{"id": t["id"], "from": t["from"], "to": t["to"], "state": t["state"],
                               "task": t["task"][:200]} for t in board["tasks"]]}

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if name == "post":
            return self.post(arguments.get("to"), arguments.get("task"), arguments.get("sender", ""))
        if name == "wait_for_task":
            return self.wait_for_task(arguments.get("agent"), arguments.get("seconds", WAIT_DEFAULT))
        if name == "done":
            return self.done(arguments.get("id"), arguments.get("answer"))
        if name == "wait_for_answer":
            return self.wait_for_answer(arguments.get("id"), arguments.get("seconds", WAIT_DEFAULT))
        if name == "board":
            return self.board()
        raise ValueError(f"Unknown tool: {name}")

    def handle(self, message):
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            return None
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                      "serverInfo": {"name": "sandbox-ai-team-board", "version": VERSION}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools(self.members)}
        elif method == "tools/call":
            try:
                text_, failed = json.dumps(self.call(params.get("name"), params.get("arguments") or {}), indent=2), False
            except (OSError, ValueError) as exc:
                text_, failed = str(exc), True
            result = {"content": [{"type": "text", "text": text_}], "isError": failed}
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout):
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


def text(value, what, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"the {what} is longer than {limit} characters")


def wait_time(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= WAIT_LIMIT:
        raise ValueError(f"seconds must be a whole number from 0 to {WAIT_LIMIT}")
    return seconds


def find(board, ident):
    if isinstance(ident, bool) or not isinstance(ident, int):
        raise ValueError("id must be a task's number")
    for task in board["tasks"]:
        if task["id"] == ident:
            return task
    raise ValueError(f"no task {ident}")


def prune(board):
    """At most TASKS tasks: the oldest done ones go; a task not done is never dropped, the board is full then."""
    while len(board["tasks"]) > TASKS:
        done = next((t for t in board["tasks"] if t["state"] == "done"), None)
        if done is None:
            raise ValueError(f"the board is full: {TASKS} tasks are not done yet")
        board["tasks"].remove(done)


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read(directory):
    """The board in DIRECTORY; an empty one when there is none. A damaged file is never trusted in part."""
    try:
        fd = os.open(Path(directory) / BOARD, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"version": 1, "next": 0, "tasks": []}
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(READ_LIMIT + 1)
    if len(data) > READ_LIMIT:
        raise ValueError("the board file is too large")
    board = json.loads(data.decode("utf-8")) if data.strip() else {"version": 1, "next": 0, "tasks": []}
    if not isinstance(board, dict) or not isinstance(board.get("tasks"), list) or not isinstance(board.get("next"), int):
        raise ValueError("the board file is damaged")
    for task in board["tasks"]:
        if not isinstance(task, dict) or task.get("state") not in STATES or not isinstance(task.get("id"), int) \
                or not isinstance(task.get("task"), str) or not isinstance(task.get("to"), str):
            raise ValueError("the board file is damaged")
    return board


def write(directory, board):
    """BOARD into DIRECTORY in one step; refused (nothing written) when it would grow past BOARD_LIMIT, so a
    board that was read can always be read again."""
    data = json.dumps(board, ensure_ascii=False).encode("utf-8")
    if len(data) > BOARD_LIMIT:
        raise ValueError(f"the board is full ({BOARD_LIMIT >> 20} MiB): report or remove done tasks first")
    path = Path(directory) / BOARD
    temporary = path.with_name(f".{BOARD}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.replace(temporary, path)


def main(argv):
    members, directory, rest = None, ".", list(argv)
    while rest:
        if rest[0] == "--members" and len(rest) > 1:
            members, rest = rest[1].split(","), rest[2:]
        elif rest[0] == "--dir" and len(rest) > 1:
            directory, rest = rest[1], rest[2:]
        else:
            print(f"team_board.py: unknown argument {rest[0]!r}", file=sys.stderr)
            return 2
    try:
        board = Board(members or [], directory)
    except ValueError as exc:
        print(f"team_board.py: {exc}", file=sys.stderr)
        return 2
    board.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
