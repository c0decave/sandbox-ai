#!/usr/bin/env python3
"""sandbox-ai approvals: answer what running sandboxes ask.

  sandbox-ai approvals list                      open questions of every running sandbox
  sandbox-ai approvals allow ID [--scope once|run|always]
  sandbox-ai approvals deny ID
  sandbox-ai approvals history [RUN]             questions and their answers

A sandbox asks (with --ask, the default) when its agent connects to a destination the options do not
allow, looks up a name they do not pin, or pushes commits the privacy check has warnings about. Without
an answer within 60 seconds the question is refused. Scopes: once (this connection), run (until the
sandbox ends), always (also every later start of this project; kept in
~/.config/sandbox-ai/approved.json). A name allowed after a lookup is reached by the agent's next try.

Standard library only.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ID = re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}")
REQUEST_LIMIT = 8192  # one browser request from the sandbox: {"url": ...}


def net():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_net_for_approvals", HERE / "net.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def approved_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "approved.json"


def approved_for(workspace):
    """{"hosts": [...], "tcp": ["IP:PORT", ...]} always allowed for the project WORKSPACE."""
    try:
        data = json.loads(approved_file().read_text())
    except (OSError, ValueError):
        return {"hosts": [], "tcp": []}
    entry = data.get(str(Path(workspace).resolve())) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return {"hosts": [], "tcp": []}
    hosts = [h for h in entry.get("hosts", []) if isinstance(h, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", h)]
    tcp = [t for t in entry.get("tcp", []) if isinstance(t, str) and re.fullmatch(r"[0-9A-Fa-f:.\[\]]{2,45}:[0-9]{1,5}", t)]
    return {"hosts": hosts, "tcp": tcp}


def remember(workspace, host=None, tcp=None):
    path = approved_file()
    try:
        data = json.loads(path.read_text())
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    entry = data.setdefault(str(Path(workspace).resolve()), {"hosts": [], "tcp": []})
    if host and host not in entry["hosts"]:
        entry["hosts"].append(host)
    if tcp and tcp not in entry["tcp"]:
        entry["tcp"].append(tcp)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(".approved.json")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def questions(runs=None, answered=False):
    """[(run, record, question)] of the running sandboxes; open ones only unless ANSWERED."""
    found = []
    for name, record in (runs if runs is not None else net().running()).items():
        directory = Path(record["directory"]) / "approvals"
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in files:
            if not ID.fullmatch(path.stem):
                continue
            try:
                question = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            done = path.with_suffix(".done")
            if done.exists():
                if not answered:
                    continue
                try:
                    question["result"] = json.loads(done.read_text())
                except (OSError, ValueError):
                    pass
            elif path.with_suffix(".answer").exists() and not answered:
                continue
            found.append((name, record, question))
    return found


def describe(question):
    kind = question.get("kind")
    if kind == "connect":
        host = question.get("host")
        return f"connect to {question.get('destination')}" + (f" ({host})" if host else "")
    if kind == "dns":
        return f"look up {question.get('name')}"
    if kind == "push":
        return f"push {', '.join(question.get('refs', []))} to {question.get('url')} despite privacy warnings"
    if kind == "browser":
        return f"open {question.get('url')} in your browser"
    return str(kind)


def resolve(name):
    infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def answer(ident, decision, scope="once", runs=None, resolver=resolve, opener=None, out=print):
    """Write the answer to question IDENT; side effects the supervisor cannot do itself happen here:
    a name is resolved and pinned, a browser is opened, an "always" is remembered for the project."""
    if decision not in ("allow", "deny") or scope not in ("once", "run", "always"):
        raise ValueError("decision allow|deny, scope once|run|always")
    for name, record, question in questions(runs):
        if question.get("id") != ident:
            continue
        reply = {"decision": decision, "scope": scope}
        kind = question.get("kind")
        if decision == "allow" and kind == "dns":
            host = question["name"]
            addresses = resolver(host)
            reply["addresses"] = addresses
            net().update_hosts(record, {host: addresses})
            if scope == "always":
                remember(record["workspace"], host=host)
        elif decision == "allow" and kind == "connect" and scope == "always":
            host, port = question.get("host"), int(question.get("port", 0))
            if host and port == 443:
                remember(record["workspace"], host=host)
            else:
                address = question.get("address", "")
                remember(record["workspace"], tcp=f"[{address}]:{port}" if ":" in address else f"{address}:{port}")
        elif decision == "allow" and kind == "browser":
            url = question.get("url", "")
            if not re.fullmatch(r"https?://[^\s\x00-\x1f\x7f]{1,4000}", url):
                raise ValueError("only http(s) URLs are opened")
            (opener or open_browser)(url)
        path = Path(record["directory"]) / "approvals" / f"{ident}.answer"
        temporary = path.with_name(f".{ident}.answer")
        temporary.write_text(json.dumps(reply))
        os.replace(temporary, path)
        if kind == "browser":  # nobody waits for these in the sandbox
            path.with_name(f"{ident}.done").write_text(json.dumps({**reply, "reason": "answered"}))
        out(f"{name}: {decision} ({scope}): {describe(question)}")
        return reply
    raise ValueError(f"no open question {ident}; see sandbox-ai approvals list")


def open_browser(url):
    """Open URL in the host's browser, never through a shell."""
    for program in ("xdg-open", "open", "sensible-browser"):
        if shutil.which(program):
            subprocess.Popen([program, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    raise ValueError("no program to open a browser (xdg-open)")


def watch(directory, stop, notify=None, interval=1.0):
    """Notify (notify-send) about every new question in DIRECTORY until STOP is set (the launcher runs this
    while its sandbox runs: the agent's terminal is busy with the agent)."""
    notify = notify or desktop_notification
    seen = set()
    while not stop.wait(interval):
        try:
            files = sorted(Path(directory).glob("*.json"))
        except OSError:
            continue
        for path in files:
            if path.name in seen or not ID.fullmatch(path.stem) or path.with_suffix(".done").exists():
                continue
            seen.add(path.name)
            try:
                question = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            notify("sandbox-ai asks", f"{describe(question)}\nsandbox-ai approvals allow {question.get('id')}")


def desktop_notification(title, text):
    """notify-send; TEXT names what the agent asked for, so markup in it is escaped (some notification
    daemons render <b>, <a> or <img> in the body) and control characters are dropped."""
    import html
    text = html.escape("".join(c if c == "\n" or c.isprintable() else "?" for c in text[:1000]), quote=False)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=sandbox-ai", "--urgency=critical", title, text],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       check=False)


def serve_browser(socket_path, approvals_dir, stop, notify=None, limit=20, listen=None):
    """Take the sandbox's requests to open a URL in your browser (its xdg-open) on SOCKET_PATH and turn each
    into a question in APPROVALS_DIR; at most LIMIT open at a time. Runs on the host while the sandbox runs."""
    import importlib.util
    import itertools
    if listen is None:
        spec = importlib.util.spec_from_file_location("runtime_for_browser", HERE / "runtime.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        listen = runtime.listen_unix
    notify = notify or desktop_notification
    server = listen(socket_path)
    server.settimeout(0.5)
    counter = itertools.count(1)
    while not stop.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with connection:
            try:
                connection.settimeout(5)
                line = connection.makefile("r", encoding="utf-8", errors="replace").readline(REQUEST_LIMIT + 1)
                if len(line) > REQUEST_LIMIT:
                    raise ValueError("request too long")
                request = json.loads(line or "{}")
                url = request.get("url", "") if isinstance(request, dict) else ""
                if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s\x00-\x1f\x7f]{1,4000}", url):
                    reply = {"ok": False, "message": "sandbox-ai: only http(s) URLs can be opened"}
                elif len([p for p in Path(approvals_dir).glob("*.json") if not p.with_suffix(".done").exists()]) >= limit:
                    reply = {"ok": False, "message": "sandbox-ai: too many open questions; open the URL yourself"}
                else:
                    ident = f"{int(time.time())}-{os.getpid()}-{next(counter)}"
                    question = {"id": ident, "kind": "browser", "url": url,
                                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    path = Path(approvals_dir) / f"{ident}.json"
                    path.parent.mkdir(mode=0o700, exist_ok=True)
                    path.with_name(f".{ident}.json").write_text(json.dumps(question))
                    os.replace(path.with_name(f".{ident}.json"), path)
                    notify("sandbox-ai: open a URL?", f"{url[:300]}\nsandbox-ai approvals allow {ident}")
                    reply = {"ok": True, "message": "sandbox-ai: asked the human to open it in their browser "
                                                    f"(question {ident}); nothing opens without their answer"}
            except (OSError, ValueError) as exc:
                reply = {"ok": False, "message": f"sandbox-ai: {exc}"}
            with contextlib.suppress(OSError):
                connection.sendall((json.dumps(reply) + "\n").encode())
    server.close()


def main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai approvals", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="open questions")
    history = commands.add_parser("history", help="questions with their answers")
    history.add_argument("run", nargs="?")
    for name in ("allow", "deny"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "allow":
            command.add_argument("-s", "--scope", choices=["once", "run", "always"], default="once")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            pending = questions()
            if not pending:
                out("no open questions")
            for name, _, question in pending:
                out(f"{question.get('id')}  {name}  {describe(question)}")
            return 0
        if args.command == "history":
            for name, _, question in questions(answered=True):
                if args.run and name != args.run:
                    continue
                result = question.get("result", {})
                out(f"{question.get('time')}  {name}  {describe(question)}  -> {result.get('decision', 'open')}"
                    f" ({result.get('reason', '')})")
            return 0
        answer(args.id, args.command, getattr(args, "scope", "once"), out=out)
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai approvals: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
