#!/usr/bin/env python3
"""Minimal MCP test server (stdio, standard library only).

Tools:
  list_directory(path="/work")  entries with type and size
  version()                     server version and the environment it runs in,
                                showing that it is confined in its own sandbox

Started by `--mcp demo`; useful to check the MCP plumbing end to end.
"""
import json
import os
from pathlib import Path
import platform
import socket
import sys

VERSION = "1.0.0"
PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "list_directory", "description": "List a directory inside the MCP server's sandbox.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "Default: /work"}}}},
    {"name": "version", "description": "Server version and the sandbox environment it runs in.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def list_directory(path):
    entries = []
    for entry in sorted(os.scandir(path), key=lambda e: e.name):
        if entry.is_symlink():
            kind, size = "symlink", None
        elif entry.is_dir():
            kind, size = "directory", None
        else:
            kind, size = "file", entry.stat().st_size
        entries.append({"name": entry.name, "type": kind, "size": size})
    return {"path": str(path), "entries": entries}


def status_field(name):
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    return None


def version():
    return {"server": "sandbox-ai-demo", "version": VERSION, "protocol": PROTOCOL,
            "python": platform.python_version(), "hostname": socket.gethostname(),
            "kernel": platform.release(), "pid_namespace": os.readlink("/proc/self/ns/pid"),
            "uid": os.getuid(), "container": Path("/run/.containerenv").exists(),
            "seccomp": status_field("Seccomp"), "cwd": os.getcwd()}


def call(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    if name == "list_directory":
        path = arguments.get("path") or "/work"
        if not isinstance(path, str):  # an integer would be taken as a file descriptor
            raise ValueError("path must be a string")
        return list_directory(path)
    if name == "version":
        return version()
    raise ValueError(f"Unknown tool: {name}")


def handle(message):
    """One JSON-RPC message in, the reply (or None for notifications) out."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "sandbox-ai-demo", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text, failed = json.dumps(call(params.get("name"), params.get("arguments") or {}), indent=2), False
        except (OSError, ValueError) as exc:
            text, failed = str(exc), True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    # stdio transport: one JSON-RPC message per line.
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(message) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
