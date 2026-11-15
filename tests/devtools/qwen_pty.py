"""The pinned Qwen Code in a terminal (a pty) against a scripted model that asks for one shell command (touch a
marker in the work folder): whether Qwen Code asked before running it, ran it, and showed YOLO mode. For
tests/test_qwen.py (SANDBOX_AI_QWEN_CLI: the cli-entry.js of an `npm ci` of sandbox/Containerfile.qwen.lock).
Standard library only; Linux and macOS (pty)."""
import http.server
import json
import os
import pty
import re
import select
import socketserver
import struct
import threading
import time

ASKED = re.compile(r"Allow (once|execution)|Yes, allow|Do you want to proceed")
ESCAPES = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]")


def merge(base, over):
    result = dict(base)
    for key, value in over.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) \
            else value
    return result


def model_server(marker):
    """(server, url): an OpenAI-compatible stand-in whose answer to a prompt is one run_shell_command call, and to
    the call's result a line of text."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            data = json.dumps({"object": "list", "data": [{"id": "live-model", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            messages = body.get("messages") or []
            names = [tool.get("function", {}).get("name") for tool in body.get("tools") or []]
            last_user = max([i for i, m in enumerate(messages) if m.get("role") == "user"] or [-1])
            answered = any(m.get("role") == "tool" for m in messages[last_user + 1:])
            shell = next((name for name in names if name and "shell" in name), None)
            ident, created = f"c{time.time_ns()}", int(time.time())

            def chunk(delta, finish=None):
                return {"id": ident, "object": "chat.completion.chunk", "created": created, "model": "live-model",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if shell and not answered:
                arguments = json.dumps({"command": f"touch {marker}", "description": "make the marker"})
                events = [chunk({"role": "assistant", "content": None, "tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": shell, "arguments": arguments}}]}), chunk({}, "tool_calls")]
            else:
                events = [chunk({"role": "assistant", "content": "DONE-ANSWER"}), chunk({}, "stop")]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for event in events:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def run(cli, settings, args, tmp, user_settings=None, wait=60):
    """Run CLI (Qwen Code's cli-entry.js) with SETTINGS as its system settings (plus the scripted model as its
    provider) and ARGS in a terminal in TMP/work, type a prompt; {"asked", "ran", "yolo", "screen"}."""
    work, home = tmp / "work", tmp / "home"
    work.mkdir(parents=True, exist_ok=True)
    (home / ".qwen").mkdir(parents=True, exist_ok=True)
    if user_settings is not None:
        (home / ".qwen/settings.json").write_text(json.dumps(user_settings))
    marker = work / "MARKER"
    server, url = model_server(marker)
    provider = {"security": {"auth": {"selectedType": "openai"}}, "model": {"name": "live-model"},
                "modelProviders": {"openai": [{"id": "live-model", "name": "live-model", "baseUrl": url,
                                               "envKey": "OPENAI_API_KEY"}]}}
    (tmp / "settings.json").write_text(json.dumps(merge(settings, provider)))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TERM": "xterm-256color",
           "QWEN_CODE_SYSTEM_SETTINGS_PATH": str(tmp / "settings.json"), "QWEN_SANDBOX": "false",
           "SANDBOX": "sandbox-ai", "QWEN_CODE_MODELS_DEV_REFRESH": "off", "QWEN_USAGE_STATISTICS_ENABLED": "false",
           "QWEN_TELEMETRY_ENABLED": "false", "OTEL_SDK_DISABLED": "true", "DO_NOT_TRACK": "1",
           "OPENAI_API_KEY": "sk-placeholder", "OPENAI_BASE_URL": url, "OPENAI_MODEL": "live-model"}
    pid, fd = pty.fork()
    if pid == 0:  # the child: Qwen Code in the terminal
        os.chdir(work)
        os.execvpe("node", ["node", cli, *args], env)
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
    seen = bytearray()

    def screen():
        return ESCAPES.sub(b"", bytes(seen)).decode("utf-8", "replace")

    def pump(seconds, until):
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                seen.extend(data)
                if b"\x1b[6n" in data:  # where the cursor is
                    os.write(fd, b"\x1b[1;1R")
            if until():
                return
    try:
        pump(wait, lambda: "Type your message" in screen() or "Do you trust" in screen())
        if "Type your message" in screen():
            for char in "run the command please":
                os.write(fd, char.encode())
                time.sleep(0.02)
            time.sleep(0.5)
            os.write(fd, b"\r")
            pump(wait, lambda: marker.exists() or ASKED.search(screen()))
            pump(2, lambda: False)
        text = screen()
        return {"asked": bool(ASKED.search(text)) or "Do you trust" in text, "ran": marker.exists(),
                "yolo": "YOLO mode" in text, "screen": text}
    finally:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass
        os.close(fd)
        server.shutdown()
