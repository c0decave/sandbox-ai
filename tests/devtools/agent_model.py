#!/usr/bin/env python3
"""A scripted model server for the live checks of the agents (tests/test_*_live.py, scripts/live_check.py --agents):
an OpenAI-compatible stand-in that drives one tool call, so a real agent CLI can be checked end to end without a
provider, a key or a GPU. Standard library only.

  python3 tests/devtools/agent_model.py [--bind ADDR] [--port PORT] [--model NAME] [--tool WORD] [--log FILE]

The OpenAI Chat Completions API (POST /v1/chat/completions, streamed as server-sent events or not; GET /v1/models),
which Hermes, Qwen Code, DSH and Goose use with --llm, and Anthropic's Messages API (POST /v1/messages, streamed or
not; /v1/messages/count_tokens), which Claude Code uses (Cairn's workers):

- a chat request that offers a tool whose name holds --tool (default "version"; one that names "demo" too is
  preferred: sandbox-ai's demo MCP server, as each agent names its tools) and carries no tool result yet is answered
  with a call of that tool, without arguments;
- Letta Code (0.34.36) offers MCP tools not as tools of the model but through its shell: a system reminder "MCP
  servers with available tools: NAME, ..." and `letta mcp call mcp__SERVER__TOOL` (src/cli/subcommands/mcp.ts). A
  request without such a tool whose messages carry that reminder and that offers Letta's shell (exec_command, or
  Bash) is answered with a shell call of `letta mcp call mcp__SERVER__WORD --args '{}'` (SERVER: the listed one
  that holds "demo", else the first);
- every other request is answered with text: ANSWER, then what the last tool result said (its first 300 characters)
  or "no tool".

Each request is a line of --log (JSONL): path, model, stream, the tools offered, the tool called, whether it carried a
tool result and that result's text (cut), and whether its body named --token. GET /live/seen answers
{"requests": N, "messages": N (Messages API requests), "token": true when a request named --token}: a script in a
sandbox (which reaches the server through --llm) can wait for what it caused.
"""
import argparse
import http.server
import json
import re
import socketserver
import sys
import threading
import time

ANSWER = "LIVE-CHECK-ANSWER"
LOCK = threading.Lock()


class Options:
    model = "live-model"
    tool = "version"
    token = None
    log = None
    seen = None  # {"requests", "messages", "token"}: shared by the server's handlers (start makes it)


def text_of(content):
    """The text of a message's CONTENT (a string, OpenAI's or Anthropic's parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif "content" in part:  # Anthropic's tool_result
                    parts.append(text_of(part["content"]))
        return " ".join(parts)
    return "" if content is None else json.dumps(content)


def tool_names(tools):
    """The names of the tools a request offers (OpenAI's {"type": "function", "function": {"name"}}, Anthropic's
    {"name"})."""
    names = []
    for tool in tools if isinstance(tools, list) else []:
        if isinstance(tool, dict):
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            if isinstance(function.get("name"), str):
                names.append(function["name"])
    return names


def chosen_tool(names, word):
    """The tool to call among NAMES: one that holds WORD and "demo", else one that holds WORD, else None."""
    holding = [name for name in names if word.lower() in name.lower()]
    return next((name for name in holding if "demo" in name.lower()), holding[0] if holding else None)


def tool_result(messages):
    """The text of the last tool result in MESSAGES (OpenAI's role "tool", Anthropic's tool_result part), or None."""
    found = None
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            found = text_of(message.get("content"))
        elif isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    found = text_of(part.get("content"))
    return found


MCP_REMINDER = re.compile(r"MCP servers with available tools: ([^\n<]*)")
SHELLS = {"exec_command": "cmd", "Bash": "command"}  # Letta Code's shell tools: the argument with the command


def mcp_servers_listed(messages):
    """The MCP servers Letta Code's reminder lists in MESSAGES ("MCP servers with available tools: a (2 tools), b"),
    in its order; [] for none ("None") or no reminder."""
    found = []
    for message in messages if isinstance(messages, list) else []:
        if isinstance(message, dict):
            for match in MCP_REMINDER.finditer(text_of(message.get("content"))):
                for entry in match.group(1).split(","):
                    name = entry.strip().split(" (", 1)[0].strip()
                    if name and name != "None" and name not in found:
                        found.append(name)
    return found


def shell_call(names, messages, word):
    """(Letta's shell tool, its arguments) that runs `letta mcp call` of WORD's tool of a server Letta's reminder in
    MESSAGES lists, or None."""
    shell = next((name for name in SHELLS if name in names), None)
    servers = mcp_servers_listed(messages)
    if shell is None or not servers:
        return None
    server = next((name for name in servers if "demo" in name.lower()), servers[0])
    alias = re.sub(r"_{2,}", "_", re.sub(r"[^a-zA-Z0-9_-]", "_", server)) or "tool"  # (Letta's alias of a server)
    command = f"letta mcp call mcp__{alias}__{word} --args '{{}}'"
    return shell, {SHELLS[shell]: command, "description": f"Call the {server} MCP server's {word} tool"}


def decide(body, word):
    """(tool to call or None, its arguments, the answer's text, the tool result seen or None) for a request BODY."""
    result = tool_result(body.get("messages"))
    names = tool_names(body.get("tools"))
    tool = chosen_tool(names, word) if result is None else None
    arguments = {}
    if result is None and tool is None:
        tool, arguments = shell_call(names, body.get("messages"), word) or (None, {})
    text = f"{ANSWER} " + (f"the tool said: {result[:300]}" if result is not None else "no tool")
    return tool, arguments, text, result


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-model"
    options = Options

    def log_message(self, *args):
        pass

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        try:
            value = json.loads(data or b"{}")
        except ValueError:
            value = {}
        return (value if isinstance(value, dict) else {}), data

    def record(self, **fields):
        options = self.options
        with LOCK:
            options.seen["requests"] += 1
            if self.path.split("?")[0].endswith("/messages"):
                options.seen["messages"] += 1
            if fields.get("token"):
                options.seen["token"] = True
            if options.log:
                with open(options.log, "a") as stream:
                    stream.write(json.dumps({"time": time.time(), "path": self.path, **fields}) + "\n")

    def send_json(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_events(self, events):
        """EVENTS ([(event name or None, data)]) as server-sent events, the connection closed after them."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, data in events:
            text = data if isinstance(data, str) else json.dumps(data)
            self.wfile.write(((f"event: {name}\n" if name else "") + f"data: {text}\n\n").encode())
            self.wfile.flush()
        self.close_connection = True

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.endswith("/live/seen"):
            with LOCK:
                return self.send_json(200, dict(self.options.seen))
        if path.endswith("/models"):
            return self.send_json(200, {"object": "list", "data": [{"id": self.options.model, "object": "model",
                                                                     "owned_by": "live-check"}]})
        return self.send_json(404, {"error": {"message": f"no {path} here"}})

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body, raw = self.body()
        token = bool(self.options.token) and self.options.token.encode() in raw
        if path.endswith("/messages/count_tokens"):
            return self.send_json(200, {"input_tokens": 10})
        tool, arguments, text, result = decide(body, self.options.tool)
        self.record(model=body.get("model"), stream=bool(body.get("stream")), tools=tool_names(body.get("tools")),
                    called=tool, arguments=arguments or None, mcp_listed=mcp_servers_listed(body.get("messages")),
                    tool_result=None if result is None else result[:2000], token=token)
        if path.endswith("/messages"):
            return self.anthropic(body, tool, text, arguments)
        if path.endswith("/chat/completions"):
            return self.chat(body, tool, text, arguments)
        return self.send_json(404, {"error": {"message": f"no {path} here (Chat Completions and Messages only)"}})

    def chat(self, body, tool, text, arguments=None):
        ident, created, model = f"chatcmpl-live-{time.time_ns()}", int(time.time()), body.get("model") or \
            self.options.model
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        encoded = json.dumps(arguments or {})
        call = {"id": "call_live_1", "type": "function", "function": {"name": tool, "arguments": encoded}} if tool \
            else None
        if not body.get("stream"):
            message = {"role": "assistant", "content": None if call else text, **({"tool_calls": [call]} if call else {})}
            return self.send_json(200, {"id": ident, "object": "chat.completion", "created": created, "model": model,
                                        "choices": [{"index": 0, "message": message,
                                                     "finish_reason": "tool_calls" if call else "stop"}],
                                        "usage": usage})

        def chunk(delta, finish=None):
            return {"id": ident, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if call:
            events = [chunk({"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": call["id"], "type": "function", "function": {"name": tool, "arguments": ""}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": encoded}}]}), chunk({}, "tool_calls")]
        else:
            half = len(text) // 2
            events = [chunk({"role": "assistant", "content": text[:half]}), chunk({"content": text[half:]}),
                      chunk({}, "stop")]
        if (body.get("stream_options") or {}).get("include_usage"):
            events.append({"id": ident, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [], "usage": usage})
        self.send_events([(None, event) for event in events] + [(None, "[DONE]")])

    def anthropic(self, body, tool, text, arguments=None):
        ident, model = f"msg_live_{time.time_ns()}", body.get("model") or self.options.model
        usage = {"input_tokens": 10, "output_tokens": 5}
        block = {"type": "tool_use", "id": "toolu_live_1", "name": tool, "input": arguments or {}} if tool else \
            {"type": "text", "text": text}
        stop = "tool_use" if tool else "end_turn"
        if not body.get("stream"):
            return self.send_json(200, {"id": ident, "type": "message", "role": "assistant", "model": model,
                                        "content": [block], "stop_reason": stop, "stop_sequence": None,
                                        "usage": usage})
        start = {**block, "input": {}} if tool else {"type": "text", "text": ""}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments or {})} if tool else \
            {"type": "text_delta", "text": text}
        self.send_events([
            ("message_start", {"type": "message_start", "message": {
                "id": ident, "type": "message", "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"})])


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # a client that hung up: nothing to report


def start(bind="127.0.0.1", port=0, model="live-model", tool="version", token=None, log=None):
    """Serve in a thread; (server, port). SERVER.shutdown() ends it."""
    options = type("LiveOptions", (Options,), {"model": model, "tool": tool, "token": token, "log": log,
                                               "seen": {"requests": 0, "messages": 0, "token": False}})
    server = Server((bind, port), type("LiveHandler", (Handler,), {"options": options}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent_model.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--model", default="live-model")
    p.add_argument("--tool", default="version", metavar="WORD", help="the tool to call: its name holds WORD")
    p.add_argument("--token", metavar="TEXT", help="/live/seen says whether a request named it")
    p.add_argument("--log", metavar="FILE")
    a = p.parse_args(argv)
    server, port = start(a.bind, a.port, a.model, a.tool, a.token, a.log)
    print(json.dumps({"port": port, "model": a.model}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
