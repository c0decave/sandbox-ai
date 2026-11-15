#!/usr/bin/env python3
"""DeepSeek Harness' chat in the agent's terminal window: sandbox-ai's, the default of `sandbox-ai dsh` (dsh.py).

DSH 0.2.1-rc.5 ships no chat in the terminal (its profiles: web, headless, acp, sdk, sdk-minimal). Its headless
profile runs one task per call and keeps a conversation going between calls: `--session-id ID` adopts the persisted
Session with that id (an unknown one is an error), and `--json` writes the run as newline-delimited events - first
`session` with the id the run used, then `status`, `thinking`, `text`, `tool_call`, `tool_result` as each step
commits, last `final` (or `error`). This window is a line-based chat over exactly that (@deepseek-ai/dsh-headless
0.2.1-rc.5: lib/startup.js, lib/index.js, lib/json-stream):

  - each message runs `COMMAND --json [--session-id ID] -` (COMMAND: the launcher's `dsh headless --patch ...`, so
    the telemetry pins, the model, the MCP servers and the permission mode of the start hold), the message on
    standard input (never an option, whatever it starts with);
  - the first run's `session` event names the conversation, every later message continues it; /new starts another;
  - DSH's events are shown as they come (per step: DSH's --json has no token stream), its `dsh:` diagnostics too;
  - Ctrl+C cancels the running answer (SIGINT to DSH's own process group: DSH shuts down cleanly, exit 130; then
    SIGTERM, SIGKILL if it does not), never the chat; at the prompt it drops the line;
  - readline editing, the history in DSH's kept home ($DSH_HOME/sandbox-ai-chat-history);
  - a longer text: end a line with a backslash to go on in the next one, or put \"\"\" on a line of its own before and
    after it;
  - DSH's thinking dimmed above its answer; --thinking hide (sandbox-ai --show-thinking off) leaves it out, and
    /thinking on|off switches it in the chat;
  - with --web ARGV (a start with --browser): DSH's web UI as well, in the background through the web UI's window
    script (web.py beside this one, --open: it hands the launch URL to the session browser), its log in
    $DSH_HOME/sandbox-ai-web.log (the token masked), its notices in this window.

Approvals: headless has no one to ask (@deepseek-ai/dsh-user-approval: no answerer resolves `unavailable`, fail
closed), so an action DSH would ask about is refused and shown as a failed tool result; with -D (danger-full-access)
it asks nothing.

The launcher writes this file into the session directory; it runs read-only as /etc/dsh/chat.py. Standard library
only: it runs in the agent's container with the image's python3.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

HERE = Path(__file__).resolve().parent
PREFIX = "sandbox-ai: "
PROMPT, MORE = "dsh> ", "...> "
BLOCK = '"""'
CANCEL_GRACE = 5  # seconds DSH has after Ctrl+C (SIGINT) before SIGTERM, and as much again before SIGKILL
TOOL_LINES, LINE_WIDTH = 8, 240
NOT_THERE = re.compile(r'session "[^"]*" does not exist')
HELP = ("Type a message and press Enter: DeepSeek Harness answers in this window, and every message continues the "
        "same conversation. A longer text: end a line with \\ to go on in the next one, or put \"\"\" on a line of "
        "its own before and after it.\n"
        "  /new      a new conversation from the next message\n"
        "  /session  the conversation's id\n"
        "  /thinking on|off  show or hide DeepSeek Harness' thinking (alone: says which)\n"
        "  /help     this text\n"
        "  /exit     end the chat (Ctrl+D too)\n"
        "Ctrl+C cancels the running answer (the chat goes on); at the prompt it drops the line.\n"
        "Approvals cannot be answered here: an action DeepSeek Harness would ask about is refused (shown as a failed "
        "tool result); a start with -D asks nothing, the web UI (--browser) asks you.")


def dsh_home():
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")


class Out:
    """The terminal: whole lines, one writer at a time, colour only on a terminal (never with NO_COLOR)."""

    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()
        self.colour = stream.isatty() and not os.environ.get("NO_COLOR")
        self.at_prompt = False

    def style(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.colour and text else text

    def write(self, text, code=None):
        text = self.style(code, text) if code else text
        with self.lock:
            if self.at_prompt and self.stream.isatty():
                self.stream.write("\r\033[K")
            self.stream.write(text.rstrip("\n") + "\n")
            self.stream.flush()
            if self.at_prompt:
                redisplay()

    def note(self, text):
        self.write(PREFIX + text, "2")


def redisplay():
    try:
        import readline
        readline.redisplay()
    except (ImportError, AttributeError):
        pass


def clip(text, width=LINE_WIDTH):
    text = " ".join(str(text).split()) if "\n" not in str(text) else str(text)
    return text if len(text) <= width else text[:width - 3] + "..."


def tool_input(value):
    """A tool call's input in one line: a command as it is, anything else as compact JSON."""
    if isinstance(value, dict):
        for key in ("command", "cmd", "path", "file_path", "pattern", "url", "query"):
            if isinstance(value.get(key), str) and len(value) <= 3:
                return clip(" ".join(value[key].split()))
    return clip(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def tool_output(text):
    lines = str(text).rstrip("\n").splitlines()
    shown = [clip(line) for line in lines[:TOOL_LINES]]
    if len(lines) > TOOL_LINES:
        shown.append(f"... ({len(lines) - TOOL_LINES} more lines)")
    return shown


class Turn:
    """One message's run of DSH: its events rendered as they come (render), what the chat needs from it after."""

    def __init__(self, out, thinking=True):
        self.out, self.session, self.tools, self.last_text = out, None, {}, None
        self.thinking = thinking  # DSH's thinking shown (dimmed), or left out
        self.errors, self.diagnostics, self.reason = [], [], None

    def render(self, raw):
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            if line.strip():
                self.out.write(line)
            return
        kind = event.get("type")
        if kind == "session" and isinstance(event.get("sessionId"), str):
            self.session = event["sessionId"]
        elif kind == "thinking" and str(event.get("text") or "").strip() and self.thinking:
            self.out.write("\n".join("  " + row for row in str(event["text"]).strip().splitlines()), "2")
        elif kind == "text" and str(event.get("text") or "").strip():
            self.last_text = str(event["text"])
            self.out.write(self.last_text)
        elif kind == "tool_call":
            name = str(event.get("tool") or "tool")
            self.tools[event.get("callId")] = name
            self.out.write(f"-> {name}: {tool_input(event.get('input'))}", "36")
        elif kind == "tool_result":
            name = self.tools.get(event.get("callId"), "tool")
            failed = event.get("status") == "error"
            rows = tool_output(event.get("result") or "")
            head = f"<- {name} {'failed' if failed else 'done'}" + (":" if rows else "")
            self.out.write("\n".join([head, *("   " + row for row in rows)]), "31" if failed else "2")
        elif kind == "status" and event.get("phase") == "turn_end":
            self.reason = event.get("reason") if isinstance(event.get("reason"), dict) else None
            why = (self.reason or {}).get("kind")
            if why not in (None, "completed", "error"):  # an error: DSH's own dsh: line says it
                self.out.note(f"the answer ended: {why}")
        elif kind == "final":
            text = str(event.get("text") or "")
            if text.strip() and text != self.last_text:
                self.last_text = text
                self.out.write(text)
        elif kind == "error":
            self.errors.append(str(event.get("message") or ""))

    def diagnostic(self, raw):
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line:
            self.diagnostics.append(line)
            self.out.write(line, "31" if line.startswith("dsh:") else "2")

    def finish(self):
        """DSH's error events its stderr did not say already (a usage error writes only the event)."""
        for message in self.errors:
            if f"dsh: {message}" not in self.diagnostics:
                self.out.write(f"dsh: {message}", "31")

    def missing_session(self):
        return any(NOT_THERE.search(text) for text in [*self.errors, *self.diagnostics])


def stop_group(child, number):
    try:
        os.killpg(child.pid, number)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class Chat:
    def __init__(self, command, out, session=None, popen=subprocess.Popen, thinking=True):
        self.command, self.out, self.session, self.popen = list(command), out, session, popen
        self.thinking = thinking
        self.child = None
        self.web = None

    # --- one message ----------------------------------------------------------------------------------------------

    def send(self, message, retry=True):
        argv = [*self.command, "--json", *(["--session-id", self.session] if self.session else []), "-"]
        try:
            child = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
        except OSError as exc:
            self.out.note(f"{argv[0]} did not start: {exc}")
            return None
        self.child, turn, cancels = child, Turn(self.out, self.thinking), 0
        threading.Thread(target=self.feed, args=(child, message), daemon=True).start()
        errors = threading.Thread(target=lambda: [turn.diagnostic(raw) for raw in child.stderr], daemon=True)
        errors.start()
        while True:
            try:
                raw = child.stdout.readline()
                if not raw:
                    break
                turn.render(raw)
            except KeyboardInterrupt:
                cancels += 1
                self.cancel(child, cancels)
        while True:
            try:
                status = child.wait()
                errors.join(5)
                break
            except KeyboardInterrupt:
                cancels += 1
                self.cancel(child, cancels)
        self.child = None
        turn.finish()
        if turn.session and not self.session:
            self.session = turn.session
            self.out.note(f"conversation {turn.session} (/new starts another)")
        if cancels:
            self.out.note("answer cancelled; the conversation goes on")
        elif status != 0 and self.session and turn.missing_session() and retry:
            self.out.note(f"conversation {self.session} is not there (DSH had not saved it): the message goes to a "
                          "new one")
            self.session = None
            return self.send(message, retry=False)
        return status

    @staticmethod
    def feed(child, message):
        try:
            child.stdin.write(message.encode("utf-8", "replace"))
            child.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def cancel(self, child, count):
        """Ctrl+C during an answer: SIGINT to DSH's group (its own clean shutdown), SIGTERM and SIGKILL after it
        unless it ends; a second Ctrl+C ends it at once."""
        if child.poll() is not None:
            return
        if count > 1:
            stop_group(child, signal.SIGKILL)
            return
        self.out.note("cancelling the answer (Ctrl+C again: at once)")
        stop_group(child, signal.SIGINT)

        def escalate():
            for number in (signal.SIGTERM, signal.SIGKILL):
                try:
                    child.wait(CANCEL_GRACE)
                    return
                except subprocess.TimeoutExpired:
                    stop_group(child, number)
        threading.Thread(target=escalate, daemon=True).start()

    # --- the web UI beside it (--browser) ------------------------------------------------------------------------

    def start_web(self, argv, socket_path=None, log_path=None):
        script = HERE / "web.py"
        log_path = Path(log_path) if log_path else dsh_home() / "sandbox-ai-web.log"
        command = [sys.executable, "-I", str(script), "--open", *(["--socket", socket_path] if socket_path else []),
                   "--", *argv]
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(log_path, "a", encoding="utf-8")
        except OSError as exc:
            self.out.note(f"the web UI's log {log_path} cannot be written ({exc}): its lines go nowhere")
            log = None
        try:
            self.web = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            self.out.note(f"the web UI did not start: {exc}")
            return
        self.out.note(f"DeepSeek Harness' web UI starts as well and opens in the session browser (the app's Browser "
                      f"tab); its log: {log_path}")

        def pump(web):
            for raw in web.stdout:
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if log:
                    log.write(text + "\n")
                    log.flush()
                if text.startswith(PREFIX):
                    self.out.write(text, "2")
            status = web.wait()
            if self.web is web:
                self.out.note(f"the web UI ended (status {status}); its log: {log_path}")
            if log:
                log.close()
        threading.Thread(target=pump, args=(self.web,), daemon=True).start()

    def stop(self):
        for child in (self.child, self.web):
            if child is not None and child.poll() is None:
                stop_group(child, signal.SIGTERM)
        self.web = None


# --- reading messages --------------------------------------------------------------------------------------------

def setup_history(path):
    """readline's editing and the history file (in DSH's kept home); None without a terminal or readline."""
    if not sys.stdin.isatty():
        return None
    try:
        import readline
    except ImportError:
        return None
    readline.set_history_length(2000)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if Path(path).exists():
            readline.read_history_file(str(path))
    except OSError:
        pass
    return readline


def read_message(out, history, path):
    """One message (several lines: a trailing backslash, or a \"\"\" block); None at the end of input. Ctrl+C at the
    prompt raises KeyboardInterrupt (the line is dropped)."""
    lines, block, prompt = [], False, PROMPT
    while True:
        out.at_prompt = True
        try:
            line = input(prompt)
        finally:
            out.at_prompt = False
        if history is not None:
            try:
                history.append_history_file(1, str(path))
            except (OSError, AttributeError):
                pass
        if block:
            if line.strip() == BLOCK:
                return "\n".join(lines)
            lines.append(line)
            continue
        if not lines and line.strip() == BLOCK:
            block, prompt = True, MORE
            continue
        if line.endswith("\\"):
            lines.append(line[:-1])
            prompt = MORE
            continue
        lines.append(line)
        return "\n".join(lines)


def parse(argv):
    """Options [--notice TEXT] [--session-id ID] [--first TEXT] [--history PATH] [--web JSON-ARGV] [--socket PATH]
    [--web-log PATH] [--thinking show|hide] -- COMMAND... (--first: the first message, sent as the chat starts)"""
    options, rest = {"notice": None, "session_id": None, "first": None, "history": None, "web": None, "socket": None,
                     "web_log": None, "thinking": "show"}, list(argv)
    names = {"--notice": "notice", "--session-id": "session_id", "--first": "first", "--history": "history",
             "--web": "web", "--socket": "socket", "--web-log": "web_log", "--thinking": "thinking"}
    while rest and rest[0] != "--":
        option = rest.pop(0)
        if option in names and rest:
            options[names[option]] = rest.pop(0)
        else:
            raise SystemExit(f"{PREFIX}dsh_chat.py: unknown option {option!r}")
    if not rest[1:]:
        raise SystemExit(f"{PREFIX}dsh_chat.py: no command after --")
    if options["thinking"] not in ("show", "hide"):
        raise SystemExit(f"{PREFIX}dsh_chat.py: --thinking takes show or hide")
    if options["web"] is not None:
        web = json.loads(options["web"])
        if not (isinstance(web, list) and web and all(isinstance(arg, str) for arg in web)):
            raise SystemExit(f"{PREFIX}dsh_chat.py: --web takes a JSON list of arguments")
        options["web"] = web
    return options, rest[1:]


def main(argv=None, popen=subprocess.Popen):
    options, command = parse(sys.argv[1:] if argv is None else argv)
    out = Out(sys.stdout)
    chat = Chat(command, out, options["session_id"], popen, thinking=options["thinking"] == "show")

    def ended(number, _frame):
        raise SystemExit(128 + number)
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, ended)
    if options["notice"]:
        out.note(options["notice"])
    out.write("Type a message and Enter; /help shows the rest, /exit ends the chat.", "2")
    if options["session_id"]:
        out.note(f"conversation {options['session_id']}: your next message continues it")
    path = Path(options["history"]) if options["history"] else dsh_home() / "sandbox-ai-chat-history"
    history = setup_history(path)
    try:
        if options["web"]:
            chat.start_web(options["web"], options["socket"], options["web_log"])
        if options["first"] and options["first"].strip():
            # The start's first message (a team member's role): shown as yours would be, then sent.
            out.write("> " + " ".join(options["first"].split())[:LINE_WIDTH * 4], "2")
            chat.send(options["first"])
        interrupted = False
        while True:
            try:
                message = read_message(out, history, path)
            except KeyboardInterrupt:
                out.write("" if interrupted else "^C (/exit or Ctrl+D ends the chat)")
                interrupted = True
                continue
            except EOFError:
                out.write("")
                break
            interrupted = False
            word = message.strip()
            if not word:
                continue
            if word in ("/exit", "/quit"):
                break
            if word == "/help":
                out.write(HELP)
            elif word == "/new":
                chat.session = None
                out.note("a new conversation starts with your next message")
            elif word.split()[0] == "/thinking" and len(word.split()) <= 2:
                choice = word.split()[1].lower() if len(word.split()) == 2 else None
                if choice in ("on", "show"):
                    chat.thinking = True
                elif choice in ("off", "hide"):
                    chat.thinking = False
                elif choice is not None:
                    out.note("/thinking on or /thinking off")
                    continue
                out.note("DeepSeek Harness' thinking is " + ("shown" if chat.thinking else "hidden")
                         + " (/thinking " + ("off" if chat.thinking else "on") + " switches it)")
            elif word == "/session":
                out.note(f"conversation {chat.session}" if chat.session else "no conversation yet: your next "
                         "message starts one")
            else:
                chat.send(message)
    finally:
        chat.stop()
        if history is not None:
            try:
                history.write_history_file(str(path))
            except OSError:
                pass
    if chat.session:
        out.note(f"conversation {chat.session}; to continue it in a later start of this workspace: "
                 f"sandbox-ai dsh -- chat --session-id {chat.session}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
