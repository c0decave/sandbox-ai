#!/usr/bin/env python3
"""sandbox-ai approvals: answer what running sandboxes ask.

  sandbox-ai approvals list                      open questions of every running sandbox
  sandbox-ai approvals allow ID [--scope once|run|always]
  sandbox-ai approvals deny ID [--scope run|once]
  sandbox-ai approvals history [RUN]             questions and their answers

A sandbox asks (with --ask, the default) when its agent connects to a destination the options do not
allow, looks up a name they do not pin, or pushes commits the privacy check has warnings about. Without
an answer within 60 seconds the question is refused. Scopes: once (this connection), run (until the
sandbox ends), always (also every later start of this project; kept in
~/.config/sandbox-ai/approved.json). A name allowed after a lookup is reached by the agent's next try
(for lookups once means run: the name stays pinned); only its public addresses are opened. A deny counts
for the rest of the run (--scope once: the agent may ask again). The name in a connection question is the
one the agent sends: it is not verified, and what is allowed is the address.

Standard library only.
"""
import argparse
import contextlib
import ipaddress
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
URL = re.compile(r"https?://[!-~]{1,4000}")  # printable ASCII only: no spaces, controls, look-alike letters
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{1,63}")
TCP = re.compile(r"(?:[0-9.]{7,15}|\[[0-9a-f:.]{2,45}\]):[0-9]{1,5}")


def shown(value, limit=300):
    """VALUE as it may be printed: what the agent chose (names, URLs, refs) with every character outside
    printable ASCII escaped, so it cannot add lines, colours or look-alike text to what you read."""
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


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
    hosts = [h for h in entry.get("hosts", []) if isinstance(h, str) and HOST.fullmatch(h)]
    tcp = [t for t in entry.get("tcp", []) if isinstance(t, str) and valid_tcp(t)]
    return {"hosts": hosts, "tcp": tcp}


def valid_tcp(value):
    if not TCP.fullmatch(value):
        return False
    address, port = value.rsplit(":", 1)
    try:
        ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return 1 <= int(port) <= 65535


def remember(workspace, host=None, tcp=None):
    path = approved_file()
    try:
        data = json.loads(path.read_text())
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    if host is not None and not HOST.fullmatch(host):
        raise ValueError(f"not a host name to remember: {shown(host)}")
    if tcp is not None and not valid_tcp(tcp):
        raise ValueError(f"not an address to remember: {shown(tcp)}")
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
    """One line about QUESTION; everything the agent chose is escaped (see shown)."""
    kind = question.get("kind")
    if kind == "connect":
        host, protocol = question.get("host"), question.get("protocol")
        text = f"connect to {shown(question.get('destination'))}"
        if protocol and protocol != "tcp":
            text += f" ({shown(protocol, 10)})"
        return text + (f", named {shown(host)} by the agent (not verified)" if host else "")
    if kind == "dns":
        return f"look up {shown(question.get('name'))}"
    if kind == "push":
        refs = question.get("refs") if isinstance(question.get("refs"), list) else []
        return f"push {', '.join(shown(r, 200) for r in refs[:10])} to {shown(question.get('url'), 500)} " \
               "despite privacy warnings"
    if kind == "browser":
        return f"open {shown(question.get('url'), 1000)} in your browser"
    return shown(kind, 40)


def resolve(name):
    infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def public_addresses(name, addresses):
    """The public ones of ADDRESSES; refuses when there are none (a name the agent controls may resolve to
    this host, the LAN or a VPN: those are allowed only explicitly, with sandbox-ai net allow)."""
    public = [a for a in addresses if ipaddress.ip_address(a.split("%")[0]).is_global]
    if not public:
        raise ValueError(f"{shown(name)} resolves only to non-public addresses ({', '.join(addresses) or 'none'}); "
                         "allow them explicitly with sandbox-ai net allow --allow-tcp IP:PORT if you mean it")
    return public


def answer(ident, decision, scope="once", runs=None, resolver=resolve, opener=None, out=print):
    """Write the answer to question IDENT; side effects the supervisor cannot do itself happen here:
    a name is resolved and pinned, a browser is opened, an "always" is remembered for the project."""
    if decision not in ("allow", "deny") or scope not in ("once", "run", "always"):
        raise ValueError("decision allow|deny, scope once|run|always")
    if decision == "deny" and scope == "always":
        raise ValueError("a deny lasts at most for the run (--scope run or once)")
    matches = [entry for entry in questions(runs) if entry[2].get("id") == ident]
    if len(matches) > 1:
        raise ValueError(f"{shown(ident)} names questions of several sandboxes "
                         f"({', '.join(shown(m[0]) for m in matches)}); answer it in the app or wait")
    for name, record, question in matches:
        kind = question.get("kind")
        notes = []
        if decision == "allow" and kind == "push" and scope == "always":
            scope, notes = "run", ["pushes are approved at most for the run"]
        if decision == "allow" and kind == "dns" and scope == "once":
            scope, notes = "run", ["a name stays pinned for the run"]
        reply = {"decision": decision, "scope": scope}
        if decision == "allow" and kind == "dns":
            host = question.get("name")
            if not isinstance(host, str) or not HOST.fullmatch(host):
                raise ValueError(f"not a host name: {shown(host)}")
            resolved = resolver(host)
            addresses = public_addresses(host, resolved)
            if len(addresses) < len(resolved):
                notes.append("not opened (not public): " + ", ".join(a for a in resolved if a not in addresses))
            reply["addresses"] = addresses
            net().update_hosts(record, {host: addresses})
            notes.append(f"{host} -> {', '.join(addresses)}")
            if scope == "always":
                remember(record["workspace"], host=host)
        elif decision == "allow" and kind == "connect" and scope == "always":
            # What was approved is the address (the name came from the agent): that is remembered.
            address, port = str(question.get("address", "")), int(question.get("port", 0))
            remember(record["workspace"], tcp=f"[{address}]:{port}" if ":" in address else f"{address}:{port}")
        elif decision == "allow" and kind == "browser":
            url = question.get("url", "")
            if not isinstance(url, str) or not URL.fullmatch(url):
                raise ValueError("only http(s) URLs are opened")
            (opener or open_browser)(url)
        path = Path(record["directory"]) / "approvals" / f"{ident}.answer"
        temporary = path.with_name(f".{ident}.answer")
        temporary.write_text(json.dumps(reply))
        os.replace(temporary, path)
        if kind == "browser":  # nobody waits for these in the sandbox
            path.with_name(f"{ident}.done").write_text(json.dumps({**reply, "reason": "answered"}))
        out(f"{shown(name)}: {decision} ({scope}): {describe(question)}"
            + "".join(f"\n  {shown(note, 1000)}" for note in notes))
        return reply
    raise ValueError(f"no open question {ident}; see sandbox-ai approvals list")


def open_browser(url):
    """Open URL in the host's browser, never through a shell."""
    for program in ("xdg-open", "sensible-browser"):  # not "open": that is openvt on Debian
        if shutil.which(program):
            subprocess.Popen([program, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    raise ValueError("no program to open a browser (xdg-open)")


def watch(directory, stop, notify=None, interval=1.0, burst=3):
    """Notify (notify-send) about every new question in DIRECTORY until STOP is set (the launcher runs this
    while its sandbox runs: the agent's terminal is busy with the agent). More than BURST new questions at
    once give one notification; browser requests are announced by serve_browser."""
    notify = notify or desktop_notification
    seen = set()
    while not stop.wait(interval):
        try:
            files = sorted(Path(directory).glob("*.json"))
        except OSError:
            continue
        fresh = []
        for path in files:
            if path.name in seen or not ID.fullmatch(path.stem) or path.with_suffix(".done").exists():
                continue
            seen.add(path.name)
            try:
                question = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(question, dict) and question.get("kind") != "browser":
                fresh.append(question)
        seen &= {path.name for path in files}
        if len(fresh) > burst:
            notify("sandbox-ai asks", f"{len(fresh)} new questions\nsandbox-ai approvals list")
            continue
        for question in fresh:
            notify("sandbox-ai asks", f"{describe(question)}\nsandbox-ai approvals allow {shown(question.get('id'))}")


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
                line = read_line(connection, REQUEST_LIMIT, deadline=3.0)
                request = json.loads(line or "{}")
                url = request.get("url", "") if isinstance(request, dict) else ""
                if not isinstance(url, str) or not URL.fullmatch(url):
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
            except (OSError, ValueError, RecursionError) as exc:
                reply = {"ok": False, "message": f"sandbox-ai: {str(exc)[:200]}"}
            with contextlib.suppress(OSError):
                connection.sendall((json.dumps(reply) + "\n").encode())
    server.close()


def read_line(connection, limit, deadline):
    """One line of at most LIMIT bytes, read within DEADLINE seconds in all (a slow sender cannot hold the
    listener)."""
    end, data = time.monotonic() + deadline, b""
    while b"\n" not in data:
        left = end - time.monotonic()
        if left <= 0:
            raise ValueError("request too slow")
        connection.settimeout(left)
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("request too long")
    return data.split(b"\n", 1)[0].decode("utf-8", "replace")


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
        else:
            command.add_argument("-s", "--scope", choices=["run", "once"], default="run",
                                 help="run (default): not asked again in this run; once: may be asked again")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            pending = questions()
            if not pending:
                out("no open questions")
            for name, _, question in pending:
                out(f"{shown(question.get('id'))}  {shown(name)}  {describe(question)}")
            return 0
        if args.command == "history":
            for name, _, question in questions(answered=True):
                if args.run and name != args.run:
                    continue
                result = question.get("result", {})
                out(f"{shown(question.get('time'))}  {shown(name)}  {describe(question)}  -> "
                    f"{shown(result.get('decision', 'open'))} ({shown(result.get('reason', ''))})")
            return 0
        answer(args.id, args.command, args.scope, out=out)
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai approvals: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
