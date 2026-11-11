#!/usr/bin/env python3
"""A local model server that answers with canned text, for --llm tests without a GPU or an account: the OpenAI API
(/v1/models, /v1/chat/completions, /v1/completions, streamed as server-sent events or not) and Ollama's (/api/tags,
/api/version, /api/chat, /api/generate, streamed as JSON lines or not). Standard library only.

  python3 tests/devtools/model_mock.py [--bind ADDR] [--port PORT] [--model NAME]... [--reply TEXT]
                                       [--chunks N] [--delay S] [--idle-close S] [--close-after N] [--log FILE]

--idle-close S closes a kept-alive connection once it has been idle for S seconds, without a word to the client (as
some servers and proxies do): a client that sends its next request on it sees "the socket connection was closed
unexpectedly" (OpenCode did). --close-after N closes each connection after N answers, also without saying so.
Each request is a line in --log (JSONL): method, path, model, stream, the number of messages.
"""
import argparse
import http.server
import json
import socket
import socketserver
import sys
import threading
import time

LOCK = threading.Lock()


class Options:
    models = ("mock-model",)
    reply = None
    chunks = 4
    delay = 0.0
    idle_close = None
    close_after = None
    log = None


def text_for(options, messages):
    """The answer: --reply, else what the last user message said, echoed."""
    if options.reply is not None:
        return options.reply
    last = next((m for m in reversed(messages or []) if isinstance(m, dict) and m.get("role") == "user"), {})
    content = last.get("content", "")
    if isinstance(content, list):  # OpenAI's parts
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return f"mock reply to: {str(content)[:200]}"


def pieces(text, count):
    """TEXT in COUNT pieces at most (the stream's chunks)."""
    count = max(1, min(count, len(text) or 1))
    size = -(-len(text) // count) if text else 1
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # kept-alive connections, as model servers keep them
    server_version = "model-mock"
    options = Options

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.answers = 0
        if self.options.idle_close:
            self.connection.settimeout(self.options.idle_close)  # handle() ends quietly on the timeout

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError):
            self.close_connection = True

    def record(self, body):
        if self.options.log:
            entry = {"time": time.time(), "method": self.command, "path": self.path, "model": body.get("model"),
                     "stream": body.get("stream"), "messages": len(body.get("messages") or [])}
            with LOCK, open(self.options.log, "a") as handle:
                handle.write(json.dumps(entry) + "\n")

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def send_json(self, status, value):
        data = (json.dumps(value) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream(self, kind, lines):
        """LINES (bytes each) as a chunked answer of KIND, with --delay between them."""
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for line in lines:
            if self.options.delay:
                time.sleep(self.options.delay)
            self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def finish_answer(self):
        self.answers += 1
        if self.options.close_after and self.answers >= self.options.close_after:
            self.close_connection = True  # no "Connection: close": the client learns it when it writes again

    def do_GET(self):
        self.record({})
        path = self.path.split("?", 1)[0]
        if path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "mock"}
                                                             for m in self.options.models]})
        elif path == "/api/tags":
            self.send_json(200, {"models": [{"name": m, "model": m, "size": 1, "details": {"family": "mock"}}
                                            for m in self.options.models]})
        elif path == "/api/version":
            self.send_json(200, {"version": "0.0.0-mock"})
        elif path in ("/", "/health"):
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": {"message": f"no {path}"}})
        self.finish_answer()

    def do_POST(self):
        body = self.body()
        self.record(body)
        path = self.path.split("?", 1)[0]
        model = body.get("model") or self.options.models[0]
        if model not in self.options.models:
            self.send_json(404, {"error": {"message": f"model {model} not found", "type": "invalid_request_error"}})
            return self.finish_answer()
        created = int(time.time())
        if path in ("/v1/chat/completions", "/v1/completions"):
            chat = path.endswith("chat/completions")
            text = text_for(self.options, body.get("messages") if chat else
                            [{"role": "user", "content": body.get("prompt", "")}])
            usage = {"prompt_tokens": 3, "completion_tokens": len(text.split()), "total_tokens": 3 + len(text.split())}
            if body.get("stream"):
                lines = []
                for piece in pieces(text, self.options.chunks):
                    choice = {"index": 0, "delta": {"role": "assistant", "content": piece}} if chat else \
                        {"index": 0, "text": piece}
                    lines.append(b"data: " + json.dumps({"id": "mock-1", "object": "chat.completion.chunk",
                                                          "created": created, "model": model,
                                                          "choices": [choice]}).encode() + b"\n\n")
                end = {"index": 0, "delta": {}, "finish_reason": "stop"} if chat else \
                    {"index": 0, "text": "", "finish_reason": "stop"}
                lines.append(b"data: " + json.dumps({"id": "mock-1", "object": "chat.completion.chunk",
                                                      "created": created, "model": model, "choices": [end],
                                                      "usage": usage}).encode() + b"\n\n")
                lines.append(b"data: [DONE]\n\n")
                self.stream("text/event-stream", lines)
            else:
                choice = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"} \
                    if chat else {"index": 0, "text": text, "finish_reason": "stop"}
                self.send_json(200, {"id": "mock-1", "object": "chat.completion" if chat else "text_completion",
                                     "created": created, "model": model, "choices": [choice], "usage": usage})
        elif path in ("/api/chat", "/api/generate"):
            chat = path == "/api/chat"
            text = text_for(self.options, body.get("messages") if chat else
                            [{"role": "user", "content": body.get("prompt", "")}])
            stream = body.get("stream", True)  # Ollama streams unless told not to
            if stream:
                lines = [json.dumps({"model": model, "done": False,
                                     **({"message": {"role": "assistant", "content": p}} if chat else {"response": p})
                                     }).encode() + b"\n" for p in pieces(text, self.options.chunks)]
                lines.append(json.dumps({"model": model, "done": True, "done_reason": "stop",
                                         **({"message": {"role": "assistant", "content": ""}} if chat
                                            else {"response": ""})}).encode() + b"\n")
                self.stream("application/x-ndjson", lines)
            else:
                self.send_json(200, {"model": model, "done": True, "done_reason": "stop",
                                     **({"message": {"role": "assistant", "content": text}} if chat
                                        else {"response": text})})
        else:
            self.send_json(404, {"error": {"message": f"no {path}"}})
        self.finish_answer()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a client gone is no error of the server's
            super().handle_error(request, client_address)


class Server6(Server):
    address_family = socket.AF_INET6


def start(bind="127.0.0.1", port=0, **settings):
    """The server in a thread: (server, port). SETTINGS: the Options above (models, reply, chunks, delay,
    idle_close, close_after, log)."""
    options = type("Settings", (Options,), {key: value for key, value in settings.items() if value is not None})
    handler = type("MockHandler", (Handler,), {"options": options})
    server = (Server6 if ":" in bind else Server)((bind, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main(argv=None):
    p = argparse.ArgumentParser(prog="model_mock.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11434)
    p.add_argument("--model", action="append", metavar="NAME", help="a model it serves (default: mock-model)")
    p.add_argument("--reply", metavar="TEXT")
    p.add_argument("--chunks", type=int, default=4)
    p.add_argument("--delay", type=float, default=0.0, help="seconds between streamed chunks")
    p.add_argument("--idle-close", type=float, metavar="S")
    p.add_argument("--close-after", type=int, metavar="N")
    p.add_argument("--log", metavar="FILE")
    a = p.parse_args(argv)
    server, port = start(a.bind, a.port, models=tuple(a.model or Options.models), reply=a.reply, chunks=a.chunks,
                         delay=a.delay, idle_close=a.idle_close, close_after=a.close_after, log=a.log)
    print(json.dumps({"port": port, "models": list(a.model or Options.models)}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
