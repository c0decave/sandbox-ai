#!/usr/bin/env python3
"""A local model server that answers with canned text, for --llm tests without a GPU or an account: the OpenAI API
(/v1/models, /v1/chat/completions, /v1/completions, streamed as server-sent events or not) and Ollama's (/api/tags,
/api/version, /api/chat, /api/generate, streamed as JSON lines or not). Standard library only.

  python3 tests/devtools/model_mock.py [--bind ADDR] [--port PORT] [--model NAME]... [--reply TEXT]
                                       [--chunks N] [--delay S] [--idle-close S] [--close-after N] [--log FILE]

--idle-close S closes a kept-alive connection once it has been idle for S seconds, without a word to the client (as
some servers and proxies do): a client that sends its next request on it sees "the socket connection was closed
unexpectedly" (OpenCode did). --close-after N closes each connection after N answers, also without saying so.
Each request is a line in --log (JSONL): method, path, model, stream, the number of messages (with --log-bodies the
request's whole JSON body too). --lmstudio answers LM Studio's own REST API as well (GET /api/v1/models with each
model's loaded instances, POST /api/v1/models/load, which loads one - a second load of a model is a second instance,
MODEL:2, as LM Studio does - and refuses keys it does not know, POST /api/v1/models/unload {instance_id}); a chat
request to a model not loaded loads it JIT (an instance with remaining_ttl_seconds, as LM Studio 0.4.26 reports
one). For the tests that no listing loads a model, that a start loads one explicitly, and of llm loaded|load|unload.
--ollama-ps answers Ollama's GET /api/ps (the loaded models with size, size_vram, context_length, expires_at), and
/api/generate or /api/chat without a prompt (or messages) loads the model (done_reason "load"; keep_alive in seconds,
negative: until unloaded; options.num_ctx its context), with keep_alive 0 unloads it ("unload").
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
    log_bodies = False
    lmstudio = False
    max_context = 131072
    # The LM Studio-like loaded instances, given as {model: context_length} or {model: {"context": N, "ttl": S}} (an
    # instance with an idle TTL: JIT); made into instances [{"id", "model", "context", "ttl"}] (shared by the server's
    # handlers) at the start.
    loaded = None
    instances = None
    jit_ttl = 3600      # the remaining_ttl_seconds of an instance JIT-loaded by a chat request
    ollama_ps = False
    running = None      # the Ollama-like loaded models: {model: {"context", "expires_at"}}
    size = 4 << 30      # what an Ollama-like loaded model takes in memory (all in VRAM)


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
                     "stream": body.get("stream"), "messages": len(body.get("messages") or []),
                     **({"body": body} if self.options.log_bodies else {})}
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

    def add_instance(self, model, context, ttl):
        """A loaded instance of MODEL (under LOCK): its id the model's key, MODEL:2 ... for a further one."""
        taken = {i["id"] for i in self.options.instances}
        number, name = 1, model
        while name in taken:
            number += 1
            name = f"{model}:{number}"
        self.options.instances.append({"id": name, "model": model, "context": context, "ttl": ttl})
        return name

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
        elif path == "/api/v1/models" and self.options.lmstudio:
            with LOCK:
                instances = [dict(i) for i in self.options.instances]
            self.send_json(200, {"models": [
                {"key": m, "type": "llm", "max_context_length": self.options.max_context, "size_bytes": 1 << 30,
                 "loaded_instances": [{"id": i["id"], "config": {"context_length": i["context"]},
                                       **({"remaining_ttl_seconds": i["ttl"]} if i.get("ttl") is not None else {})}
                                      for i in instances if i["model"] == m]}
                for m in self.options.models]})
        elif path == "/api/ps" and self.options.ollama_ps:
            with LOCK:
                running = {m: dict(v) for m, v in self.options.running.items()}
            self.send_json(200, {"models": [
                {"name": m, "model": m, "size": self.options.size, "size_vram": self.options.size,
                 "digest": "0" * 64, "details": {"family": "mock"}, "expires_at": v["expires_at"],
                 "context_length": v["context"]} for m, v in running.items()]})
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
        if path == "/api/v1/models/unload" and self.options.lmstudio:
            with LOCK:
                found = [i for i in self.options.instances if i["id"] == body.get("instance_id")]
                for instance in found:
                    self.options.instances.remove(instance)
            if found:
                self.send_json(200, {"instance_id": body["instance_id"]})
            else:
                self.send_json(404, {"error": {"message": f"no instance {body.get('instance_id')}"}})
            return self.finish_answer()
        model = body.get("model") or self.options.models[0]
        if model not in self.options.models:
            self.send_json(404, {"error": {"message": f"model {model} not found", "type": "invalid_request_error"}})
            return self.finish_answer()
        created = int(time.time())
        if path == "/api/v1/models/load" and self.options.lmstudio:
            unknown = sorted(set(body) - {"model", "context_length", "eval_batch_size", "flash_attention",
                                          "num_experts", "offload_kv_cache_to_gpu", "echo_load_config"})
            if unknown:  # as LM Studio 0.4 answers a key it does not know ("ttl" too)
                self.send_json(400, {"error": {"message": f"Unrecognized key(s) in object: {', '.join(unknown)}",
                                               "type": "invalid_request", "code": "unrecognized_keys"}})
                return self.finish_answer()
            with LOCK:
                instance = self.add_instance(model, body.get("context_length") or 4096, None)
            self.send_json(200, {"type": "llm", "instance_id": instance, "load_time_seconds": 0.1, "status": "loaded"})
            return self.finish_answer()
        if self.options.lmstudio and path in ("/v1/chat/completions", "/v1/completions") and body.get("model") and \
                (body.get("messages") or body.get("prompt")):  # (an empty probe loads nothing)
            with LOCK:  # JIT loading: the first request to a model not loaded loads it, with an idle TTL
                if not any(i["model"] == model for i in self.options.instances):
                    self.add_instance(model, 4096, self.options.jit_ttl)
        if self.options.ollama_ps and path in ("/api/generate", "/api/chat") and body.get("model"):
            keep = body.get("keep_alive", 300)
            empty = not body.get("prompt") if path == "/api/generate" else not body.get("messages")
            with LOCK:
                if keep == 0 and empty:
                    self.options.running.pop(model, None)
                    reason = "unload"
                else:
                    context = (body.get("options") or {}).get("num_ctx") or \
                        (self.options.running.get(model) or {}).get("context") or 4096
                    seconds = keep if isinstance(keep, (int, float)) else 300
                    expires = "2318-01-01T00:00:00Z" if seconds < 0 else \
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))
                    self.options.running[model] = {"context": context, "expires_at": expires}
                    reason = "load"
            if empty:
                self.send_json(200, {"model": model, "created_at": "2026-11-12T00:00:00Z", "done": True,
                                     "done_reason": reason, **({"message": {"role": "assistant", "content": ""}}
                                                               if path == "/api/chat" else {"response": ""})})
                return self.finish_answer()
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
    options.instances = []
    for model, value in (options.loaded or {}).items():
        value = value if isinstance(value, dict) else {"context": value}
        options.instances.append({"id": model, "model": model, "context": value.get("context") or 4096,
                                  "ttl": value.get("ttl")})
    options.running = dict(options.running or {})
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
    p.add_argument("--log-bodies", action="store_true", help="each request's JSON body in --log too")
    p.add_argument("--lmstudio", action="store_true",
                   help="LM Studio's /api/v1/models, /api/v1/models/load and /unload too, and JIT loading")
    p.add_argument("--ollama-ps", action="store_true", help="Ollama's /api/ps, and loading and unloading models")
    a = p.parse_args(argv)
    server, port = start(a.bind, a.port, models=tuple(a.model or Options.models), reply=a.reply, chunks=a.chunks,
                         delay=a.delay, idle_close=a.idle_close, close_after=a.close_after, log=a.log,
                         log_bodies=a.log_bodies or None, lmstudio=a.lmstudio or None, ollama_ps=a.ollama_ps or None)
    print(json.dumps({"port": port, "models": list(a.model or Options.models)}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
