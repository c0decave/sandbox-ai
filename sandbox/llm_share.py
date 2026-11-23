#!/usr/bin/env python3
"""sandbox-ai llm share: an LLM server of this machine for every host of `sandbox-ai hosts`.

  sandbox-ai llm share NAME [--key-to HOST,... | --no-key]
                                  copy NAME's entry to every host of sandbox-ai hosts and keep it in sync; its key
                                  (the secrets it names) only to the hosts named
  sandbox-ai llm unshare NAME     take the copies away again, and the keys that were sent
  sandbox-ai llm sync [NAME]      copy again where a copy is missing or out of date, check each host now

A shared entry is copied as it is kept here, nothing secret in it (it names its secrets, as llm.json does), its CA
with it (a certificate, no secret), to every host of `sandbox-ai hosts` - a host added later gets it too - over the
hosts' SSH connection, into that host's own llm.json (`sandbox-ai llm receive` there, the entry on its standard input).
A session there reaches the server directly, from that host's network: after each copy the host checks whether the
server answers from there, and says so (`llm list` here shows it per host). A copy is marked there as managed from
this machine: a change there says so, and the next change here replaces it; an entry of that host's own of the same
name is never replaced. `llm add --replace`, `llm set` and `llm remove` of a shared entry here copy the change (or
remove the copies) at once. A host that does not answer is tried again at the next `sandbox-ai llm` or
`sandbox-ai hosts` command (at most every RETRY seconds; `llm sync` at once), which says so.

The key goes only to the hosts named with --key-to (a host that leaves `sandbox-ai hosts` leaves --key-to too: one
added again under its name gets the key only when it is named again): each secret the entry names (its API key, its header secrets) is
read here and written to the standard input of `sandbox-ai secrets set` on that host (secrets send: over SSH, never
on a command line, in a file or a log), kept there in that host's own secret store. A secret of that name the host
keeps already (its own) is used as it is and never replaced. A host without the key says so, with how to give it
one. A host taken off --key-to, `llm unshare` and `llm remove` delete the keys that were sent (only those) and the
copies there; `hosts remove HOST` does it for HOST before it leaves the list. Each copy, removal and key sent or deleted is a line of launcher.log (names only, never a value).
`llm share NAME` again sends the keys again (after `secrets set` of a new value here).

The state is kept in ~/.config/sandbox-ai/llm-shares.json: per server the hosts its key goes to, and per host what
was copied (a digest of the entry), the keys sent, whether the server was reachable from there, the last error.

Not shared: an entry on this machine's loopback (127.0.0.1 or localhost is the other host itself there: add the
server with its LAN address), and `local` (this machine). An entry with --vpn names a VPN profile that host needs
as well (`sandbox-ai vpn` there): its check says so when it is missing.

Standard library only; llm.py loads it by its path and hands itself over (bind)."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import time

HERE = Path(__file__).resolve().parent
RETRY = 300               # seconds before a host that did not answer is tried again by itself (llm sync: at once)
RECEIVE_TIMEOUT = 120     # seconds a host may take for `llm receive` (the check of the server included)
KEY_TIMEOUT = 60          # seconds per secret sent or deleted
REACH_TIMEOUT = 5         # seconds the host waits for the server's answer
LIMIT = 1 << 20           # bytes of llm-shares.json, and of an entry on `llm receive`'s standard input
ORIGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# What a copy carries: the entry's settings. What a server listed (its models, context windows) is learnt there.
COPIED = ("kind", "url", "api_key_secret", "headers", "vpn", "model", "anthropic", "insecure_http", "params", "forced",
          "loop_guard", "evict", "memory_gb")
LEARNED = ("models", "checked", "contexts", "vision")
OFFLINE = (124, 127, 255)  # hosts.run: no answer, no ssh, SSH did not reach the host

_llm = None


def bind(module):
    """llm.py (the module that loaded this one): its store, checks and clients are used here."""
    global _llm
    _llm = module


def llm():
    """llm.py: the one bound, else loaded by its path (hosts.py loads this module on its own)."""
    global _llm
    if _llm is None:
        spec = importlib.util.spec_from_file_location("sandbox_llm_for_share", HERE / "llm.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _llm = loaded
    return _llm


_hosts = None


def hosts_module():
    """hosts.py, loaded once (sandbox-ai hosts: the list, and each command over SSH)."""
    global _hosts
    if _hosts is None:
        _hosts = llm().load_hosts_module()
    return _hosts


def printable(text):
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"


def shares_path():
    return config_dir() / "llm-shares.json"


def now_text():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(level, message):
    llm().LOG.log(level, "llm", message)


# --- the state ---------------------------------------------------------------------------------------------------

def load_shares(path=None):
    """{"version": 1, "origin": NAME, "shares": {SERVER: {"key_to": [HOST], "since", "removing", "hosts": {HOST:
    state}}}}; empty without a file. A file that cannot be used is an error, never half read."""
    path = Path(path or shares_path())
    try:
        data = llm().read_private(path, LIMIT, "The shared LLM servers file")
    except FileNotFoundError:
        return {"version": 1, "shares": {}}
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ValueError(f"{path} is not valid JSON") from None
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("shares"), dict):
        raise ValueError(f"{path}: expected version 1 with an object \"shares\"")
    hosts_ = hosts_module()
    for name, share in list(document["shares"].items()):
        if not (isinstance(name, str) and llm().NAME.fullmatch(name) and isinstance(share, dict)):
            raise ValueError(f"{path}: {printable(name)!r} is not a shared LLM server")
        key_to = share.get("key_to", [])
        if not isinstance(key_to, list) or not all(isinstance(h, str) and hosts_.NAME.fullmatch(h) for h in key_to):
            raise ValueError(f"{path}: {name}: key_to is a list of hosts")
        if not isinstance(share.get("hosts", {}), dict):
            raise ValueError(f"{path}: {name}: hosts is an object")
        key_for = share.get("key_for", {})
        if not isinstance(key_for, dict) or not all(isinstance(h, str) and (v is None or isinstance(v, str))
                                                    for h, v in key_for.items()):
            raise ValueError(f"{path}: {name}: key_for is an object of hosts and their addresses")
        share.setdefault("hosts", {})
        share["key_to"] = key_to
        for host, state in list(share["hosts"].items()):
            if not (isinstance(host, str) and hosts_.NAME.fullmatch(host) and isinstance(state, dict)):
                share["hosts"].pop(host)
    origin = document.get("origin")
    if origin is not None and not (isinstance(origin, str) and ORIGIN.fullmatch(origin)):
        raise ValueError(f"{path}: origin is this machine's name")
    return document


def shown_shares(note=None):
    """The file as llm list and show use it: None without one; a damaged one is said (NOTE) and shown as none -
    the listing itself never fails for it."""
    if not shares_path().exists():
        return None
    try:
        return load_shares()
    except (OSError, ValueError) as exc:
        if note is not None:
            note(f"note: shared LLM servers: {printable(exc)}")
        return None


def save_shares(document, path=None):
    llm().write_private(Path(path or shares_path()), (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def lock(timeout=None):
    """The lock of llm-shares.json, held from reading it to writing it back - over the SSH commands of a sync too:
    TIMEOUT 0 (a sync by itself, at another command) gives up at once when another holds it."""
    return llm().settings_lock(shares_path(), timeout)


def origin_name(document):
    """This machine's name as the copies on the hosts are marked with ("managed from ..."): its host name, kept in
    the file at the first share (a host name that changes later leaves the copies this machine's)."""
    if not document.get("origin"):
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", socket.gethostname() or "").strip("-._")[:64]
        document["origin"] = name if ORIGIN.fullmatch(name or "") else "sandbox-ai"
    return document["origin"]


def shared_names():
    """The names of the servers shared now (none without the file: nothing loaded, nothing asked)."""
    if not shares_path().exists():
        return []
    try:
        return sorted(name for name, share in load_shares()["shares"].items() if not share.get("removing"))
    except (OSError, ValueError):
        return []


# --- what is copied ----------------------------------------------------------------------------------------------

def key_names(endpoint):
    """The secrets ENDPOINT names: its API key's, then its header secrets' (each once)."""
    names = [endpoint.get("api_key_secret")] + [secret for _, secret in sorted((endpoint.get("headers") or {}).items())]
    return list(dict.fromkeys(name for name in names if name))


def not_shared(endpoint):
    """Why ENDPOINT cannot be copied to another host, or None."""
    if endpoint["parsed"]["loopback"]:
        return (f"{endpoint['url']} is this machine's loopback: on another host it is that host itself; add the server "
                f"with its LAN address (sandbox-ai llm add {endpoint['name']} --replace --url http://ADDRESS:"
                f"{endpoint['parsed']['port']}, the server listening there)")
    return None


def payload(endpoint):
    """{"name", "record", "ca"}: what a host gets - the entry's settings (secret names, never values) and its CA."""
    record = {key: endpoint[key] for key in COPIED if endpoint.get(key) not in (None, False, {}, [], "")}
    if record.get("evict") == "never":
        record.pop("evict")
    return {"name": endpoint["name"], "record": record, "ca": llm().ca_text(endpoint)}


def digest(found):
    return hashlib.sha256(json.dumps(found, sort_keys=True).encode()).hexdigest()[:24]


# --- this machine's side -----------------------------------------------------------------------------------------

def host_names():
    """The hosts of sandbox-ai hosts (not this machine), sorted."""
    return sorted(hosts_module().load())


def host_addresses():
    """{host: "USER@HOST:PORT"} of sandbox-ai hosts: the machine a name stands for now (what a key was given to)."""
    return {name: f"{entry.get('ssh')}:{entry.get('port', 22)}" for name, entry in hosts_module().load().items()}


class Sync:
    """One round over the shared servers and the hosts: copies, removals, keys; OUT says each host's outcome."""

    def __init__(self, document, out=print, hosts=None, secrets=None, force=False):
        self.document, self.out, self.force = document, out, force
        self.hosts = hosts or hosts_module()
        self._secrets = secrets
        self.origin = origin_name(document)

    @property
    def secrets(self):
        """secret_store.py, loaded when a key is sent (not at every command that finds nothing to do)."""
        if self._secrets is None:
            self._secrets = llm().load_secret_store_module()
        return self._secrets

    # -- one host --

    def run(self, host, args, input=None, timeout=RECEIVE_TIMEOUT):
        return self.hosts.run(host, args, input=input, timeout=timeout)

    def answer(self, host, args, input=None):
        """The JSON `sandbox-ai ARGS` printed on HOST, or a ValueError (OFFLINE: .offline set)."""
        status, said, err = self.run(host, args, input)
        if status in OFFLINE:
            error = ValueError(f"{host} did not answer over SSH ({printable((err or '').strip()[-200:]) or status})")
            error.offline = True
            raise error
        if status == 2 and "invalid choice" in (err or ""):
            error = ValueError(f"the sandbox-ai of {host} is too old for shared LLM servers: update it there (sandbox-ai "
                               f"hosts update {host})")
            error.too_old = True
            raise error
        try:
            found = json.loads(said)
        except (ValueError, RecursionError):
            raise ValueError(f"{host}: sandbox-ai llm receive: exit status {status}: "
                             f"{printable((err or said).strip()[-300:])}") from None
        if not isinstance(found, dict):
            raise ValueError(f"{host}: sandbox-ai llm receive answered no object")
        if not found.get("ok"):
            error = ValueError(f"{host}: " + printable(found.get("error") or f"refused (exit status {status})"))
            error.own = found.get("own") is True
            raise error
        return found

    def copy(self, name, endpoint, share, host, state):
        """NAME's entry on HOST, its keys as --key-to says; STATE (the host's) updated."""
        found = payload(endpoint)
        wanted = digest(found)
        state["tried_at"] = time.time()
        answer = self.answer(host, ["llm", "receive", "--from", self.origin], json.dumps(found))
        first = not state.get("copied")
        state.update(copied=True, digest=wanted, pending=None, error=None, synced_at=now_text())
        self.reached(state, answer)
        if answer.get("changed") or first:
            log("INFO", f"share {name}: copied to {host}" + (" (changed)" if not first else ""))
        if self.keys(name, endpoint, share, host, state, state["keys"]):
            self.reached(state, self.answer(host, ["llm", "receive", "--from", self.origin, "--check", name]))

    def reached(self, state, answer):
        keys = answer.get("keys") if isinstance(answer.get("keys"), dict) else {}
        state["keys"] = {str(k): str(v) for k, v in keys.items() if k and v in ("there", "missing", "locked")}
        state["reachable"] = bool(answer.get("reachable"))
        state["http"] = answer.get("http") if isinstance(answer.get("http"), int) else None
        state["reach_error"] = printable(answer.get("reach_error"))[:400] if answer.get("reach_error") else None
        state["checked_at"] = now_text()

    def keys(self, name, endpoint, share, host, state, there):
        """The secrets of ENDPOINT on HOST when --key-to names it (sent unless that host keeps its own of that name),
        the ones sent earlier deleted when it does not (any longer); how many were sent."""
        wanted = key_names(endpoint) if host in share.get("key_to", []) else []
        sent_before = list(state.get("keys_sent") or [])
        sent = 0
        for secret in wanted:
            ours = secret in sent_before
            if there.get(secret) == "there" and not ours:
                continue  # the host's own secret of that name: used as it is, never replaced
            if ours and there.get(secret) == "there" and not self.force:
                continue
            if self.secrets.get(secret) is None:
                raise ValueError(f"no secret {secret} on this machine to send to {host} (sandbox-ai secrets set "
                                 f"{secret})")
            self.secrets.send(secret, host, lambda line: None, replace=ours or there.get(secret) == "there",
                              yes=True, hosts=self.hosts)
            log("INFO", f"share {name}: key {secret} sent to {host}")
            sent += 1
            if not ours:
                sent_before.append(secret)
                state["keys_sent"] = sent_before
        for secret in [s for s in sent_before if s not in wanted]:
            self.delete_key(name, host, secret)
            sent_before.remove(secret)
            state["keys_sent"] = sent_before
        state["keys_sent"] = sent_before
        return sent

    def delete_key(self, name, host, secret):
        status, said, err = self.run(host, ["secrets", "delete", secret], timeout=KEY_TIMEOUT)
        if status in OFFLINE:
            error = ValueError(f"{host} did not answer over SSH: the key {secret} sent there is not deleted yet")
            error.offline = True
            raise error
        if status != 0:
            raise ValueError(f"{host}: the key {secret} sent there could not be deleted: "
                             f"{printable((err or said).strip()[-300:])}")
        log("INFO", f"share {name}: key {secret} deleted on {host}")

    def remove(self, name, host, state):
        """The copy of NAME on HOST removed, and the keys sent there deleted."""
        state["tried_at"] = time.time()
        state["pending"] = "remove"
        try:  # (also where no copy is known: a copy whose answer was lost on the way is removed too)
            answer = self.answer(host, ["llm", "receive", "--from", self.origin, "--remove", name])
        except ValueError as exc:
            # A sandbox-ai too old to have taken a copy has none; an entry of the host's own is not this share's.
            if not (getattr(exc, "too_old", False) and not state.get("copied") or getattr(exc, "own", False)):
                raise
            answer = {}
        if answer.get("removed"):
            log("INFO", f"share {name}: copy removed on {host}")
        state["copied"] = False
        for secret in list(state.get("keys_sent") or []):
            self.delete_key(name, host, secret)
            state["keys_sent"].remove(secret)

    # -- the round --

    def due(self, state, auto):
        """Whether a host whose last try failed is tried now (by itself: RETRY seconds after that try)."""
        return not auto or not state.get("error") or time.time() - float(state.get("tried_at") or 0) >= RETRY

    def round(self, names=None, only=None, auto=False):
        """Every shared server (NAMES: those) on every host (ONLY: those): copied where missing or out of date
        (every one again when forced), removed where the share or the server is gone. Returns [(name, host,
        what, error)] of what was done or tried."""
        llm_ = llm()
        store = llm_.load_store()["endpoints"]
        hosts_now = host_names()
        addresses = {host: f"{entry.get('ssh')}:{entry.get('port', 22)}" for host, entry in self.hosts.load().items()}
        done = []
        for name, share in sorted(self.document["shares"].items()):
            if names is not None and name not in names:
                continue
            record = store.get(name)
            endpoint = problem = None
            if not share.get("removing") and record is not None:
                try:
                    endpoint = llm_.check_record(name, record)
                    problem = not_shared(endpoint)
                except ValueError as exc:
                    problem = f"unusable here: {printable(exc)}"
            removing = share.get("removing") or record is None
            for host in list(share["hosts"]):
                if host not in hosts_now:  # forgotten by sandbox-ai hosts: not asked any more
                    share["hosts"].pop(host)
            # A host gone from the list is no host the key goes to: one added later under its name (another machine,
            # an address that changed hands) gets the key only when it is given again (llm share --key-to). The key
            # was given to the machine the name stood for then (key_for: its address): a name that stands for
            # another machine now is taken off key_to, and that machine is a new host to this share.
            key_for = share.setdefault("key_for", {})
            for host in list(share.get("key_to", [])):
                if host not in hosts_now:
                    share["key_to"].remove(host)
                    key_for.pop(host, None)
                elif key_for.setdefault(host, addresses.get(host)) != addresses.get(host):
                    share["key_to"].remove(host)
                    key_for.pop(host, None)
                    share["hosts"].pop(host, None)  # (what was sent went to the other machine: nothing to delete here)
                    log("WARNING", f"share {name}: {host} is {addresses.get(host)} now, not the machine its key was "
                                   "given to: the key goes there only when it is named again (--key-to)")
                    self.out(f"note: {name}: {host} is another machine now ({printable(addresses.get(host))}): its "
                             f"key goes there only when you name it again (sandbox-ai llm share {name} --key-to ...)")
            for host in [h for h in key_for if h not in share.get("key_to", [])]:
                key_for.pop(host)
            for host in hosts_now if not removing else sorted(share["hosts"]):
                if only is not None and host not in only:
                    continue
                state = share["hosts"].setdefault(host, {})
                if removing:
                    if not self.due(state, auto):
                        continue
                    try:
                        self.remove(name, host, state)
                        share["hosts"].pop(host)
                        done.append((name, host, "removed", None))
                    except ValueError as exc:
                        state["error"] = printable(exc)[:600]
                        done.append((name, host, "remove", state["error"]))
                    continue
                if problem:
                    if not self.due(state, auto):
                        continue  # (said again every RETRY seconds, not at every command)
                    state.update(error=problem[:600], pending="copy", tried_at=time.time())
                    done.append((name, host, "copy", problem))
                    continue
                wanted = digest(payload(endpoint))
                keys_wanted = key_names(endpoint) if host in share.get("key_to", []) else []
                keys_fresh = set(state.get("keys_sent") or []) <= set(keys_wanted) and \
                    all((state.get("keys") or {}).get(k) == "there" for k in keys_wanted)
                fresh = state.get("copied") and state.get("digest") == wanted and not state.get("pending") \
                    and not state.get("error") and keys_fresh
                if fresh and not self.force:
                    continue
                if not self.due(state, auto):
                    continue
                try:
                    self.copy(name, endpoint, share, host, state)
                    done.append((name, host, "copied", None))
                except ValueError as exc:
                    state.update(error=printable(exc)[:600], pending=state.get("pending") or "copy",
                                 offline=bool(getattr(exc, "offline", False)))
                    done.append((name, host, "copy", state["error"]))
                else:
                    state.pop("offline", None)
            if removing and not share["hosts"]:
                self.document["shares"].pop(name)
        return done


def report(done, out, auto=False):
    """Lines for what a round did: each host's outcome; a round by itself (AUTO) says only what it did or tried."""
    for name, host, what, error in done:
        if what == "copied":
            out(f"{name}: copied to {host}")
        elif what == "removed":
            out(f"{name}: copy removed on {host}")
        elif what == "copy":
            out(f"note: {name} is not on {host} as it is here yet: {error}" +
                f" (tried again at the next sandbox-ai llm or hosts command, or now: sandbox-ai llm sync {name})")
        else:
            out(f"note: {name}: its copy on {host} is not removed yet: {error} (tried again at the next sandbox-ai "
                "llm or hosts command)")


def pending(out=print, err=None):
    """At a command of sandbox-ai llm or hosts: what is left to do (a host that did not answer, a change not copied
    yet), done now when it is due - quietly when there is nothing. A sync of another command running now is left
    to it. Notes go to ERR (stderr) where the command prints JSON."""
    say = err or out
    if not shares_path().exists():
        return []
    try:
        with lock(timeout=0):
            document = load_shares()
            if not document["shares"]:
                return []
            sync = Sync(document, say)
            done = sync.round(auto=True)
            if done:
                save_shares(document)
    except (OSError, ValueError) as exc:
        if "being changed by another command" not in str(exc):
            say(f"note: shared LLM servers: {printable(exc)}")
        return []
    report(done, say, auto=True)
    return done


def share(name, key_to=None, no_key=False, out=print):
    """sandbox-ai llm share NAME [--key-to HOST,...|--no-key]."""
    llm_ = llm()
    endpoint = llm_.endpoint(name)
    if endpoint.get("shared_from"):
        raise ValueError(f"{name} is managed from {endpoint['shared_from']} (shared from there): share it there")
    problem = not_shared(endpoint)
    if problem:
        raise ValueError(f"{name} is not shared: {problem}")
    hosts_now = host_names()
    if key_to is not None:
        unknown = [h for h in key_to if h not in hosts_now]
        if unknown:
            raise ValueError(f"--key-to: no host {', '.join(unknown)} (sandbox-ai hosts: "
                             f"{', '.join(hosts_now) or 'none yet'})")
    keys = key_names(endpoint)
    with lock(timeout=RECEIVE_TIMEOUT * 4):
        document = load_shares()
        entry = document["shares"].get(name) or {"hosts": {}, "key_to": [], "since": now_text()}
        entry.pop("removing", None)
        if no_key:
            entry["key_to"] = []
        elif key_to is not None:
            entry["key_to"] = sorted(set(key_to))
        if no_key or key_to is not None:  # (the machines the key is given to now: their addresses, see Sync.round)
            addresses = host_addresses()
            entry["key_for"] = {host: addresses.get(host) for host in entry["key_to"]}
        document["shares"][name] = entry
        log("INFO", f"share {name}: for all hosts ({', '.join(hosts_now) or 'none yet'}), key to "
                    f"{', '.join(entry['key_to']) or 'none'}")
        sync = Sync(document, out, force=True)
        try:
            done = sync.round(names={name})
        finally:
            save_shares(document)
    if entry["key_to"] and not keys:
        out(f"note: {name} names no key: nothing to send")
    if not hosts_now:
        out(f"{name} is shared with every host of sandbox-ai hosts: none yet (a host added gets it: sandbox-ai hosts "
            "add NAME USER@HOST)")
    report(done, out)
    for line in host_lines(name, endpoint, document["shares"][name]):
        out(line)
    return 0 if all(error is None for *_, error in done) else 1


def unshare(name, out=print, removed=False):
    """sandbox-ai llm unshare NAME (REMOVED: llm remove NAME took the server itself): every copy and every key sent
    removed from the hosts; a host that does not answer later (said)."""
    if not shares_path().exists():
        if removed:
            return 0
        raise ValueError(f"{name} is not shared")
    with lock(timeout=RECEIVE_TIMEOUT * 4):
        document = load_shares()
        entry = document["shares"].get(name)
        if entry is None:
            if removed:
                return 0
            raise ValueError(f"{name} is not shared")
        entry["removing"] = True
        log("INFO", f"share {name}: not for all hosts any more" + (" (removed)" if removed else ""))
        done = Sync(document, out).round(names={name})
        save_shares(document)
    report(done, out)
    left = [host for _, host, what, _ in done if what != "removed"]
    if not left:
        out(f"{name} is not shared any more" + (": its copies and the keys sent are removed from the hosts"
                                                if done else ""))
    return 0 if not left else 1


def sync_now(name=None, out=print, only=None):
    """sandbox-ai llm sync [NAME]: every shared server (or NAME) copied again to every host (ONLY: those), each
    checked there; what was removed meanwhile taken away."""
    if not shares_path().exists():
        out("no shared LLM servers (sandbox-ai llm share NAME)")
        return 0
    with lock(timeout=RECEIVE_TIMEOUT * 4):
        document = load_shares()
        if name is not None and name not in document["shares"]:
            raise ValueError(f"{name} is not shared (sandbox-ai llm share {name})")
        done = Sync(document, out, force=True).round(names=None if name is None else {name}, only=only)
        save_shares(document)
    report(done, out)
    store = llm().load_store()["endpoints"]
    for shared, entry in sorted(document["shares"].items()):
        if name is not None and shared != name or shared not in store:
            continue
        with contextlib.suppress(ValueError):
            for line in host_lines(shared, llm().check_record(shared, store[shared]), entry):
                out(line)
    if not document["shares"]:
        out("no shared LLM servers (sandbox-ai llm share NAME)")
    return 0 if all(error is None for *_, error in done) else 1


def changed(name, out=print):
    """After llm add --replace or llm set of NAME: the change copied to the hosts now, when NAME is shared."""
    if name not in shared_names():
        return
    with contextlib.suppress(ValueError, OSError):
        with lock(timeout=RECEIVE_TIMEOUT * 2):
            document = load_shares()
            done = Sync(document, out).round(names={name})
            save_shares(document)
        report(done, out)


def host_added(host, out=print):
    """After sandbox-ai hosts add HOST: every shared server copied there now."""
    if not shared_names():
        return []
    try:
        with lock(timeout=RECEIVE_TIMEOUT * 2):
            document = load_shares()
            done = Sync(document, out).round(only={host})
            save_shares(document)
    except (OSError, ValueError) as exc:
        out(f"note: shared LLM servers: {printable(exc)}")
        return []
    report(done, out)
    return done


def host_leaving(host, out=print):
    """Before sandbox-ai hosts remove HOST: a shared server is for the hosts of the list, so its copy on HOST is
    removed now, and the keys sent there deleted; what HOST does not let go (it did not answer) is said, with how to
    take it away there. Then HOST is not asked any more."""
    if not shares_path().exists():
        return
    try:
        with lock(timeout=RECEIVE_TIMEOUT * 2):
            document = load_shares()
            sync, left = Sync(document, out), []
            for name, entry in document["shares"].items():
                # (the key goes to the hosts named, not to a name: a host added again later is asked about again)
                entry["key_to"] = [other for other in entry.get("key_to", []) if other != host]
                (entry.get("key_for") or {}).pop(host, None)
                state = entry["hosts"].pop(host, None)
                if state is None:
                    continue
                try:
                    sync.remove(name, host, state)
                    out(f"{name}: copy removed on {host}")
                except ValueError as exc:
                    if state.get("copied") or state.get("keys_sent"):
                        left.append((name, state.get("keys_sent") or [], printable(exc)))
            for name in [n for n, e in document["shares"].items() if e.get("removing") and not e["hosts"]]:
                document["shares"].pop(name)
            save_shares(document)
    except (OSError, ValueError) as exc:
        out(f"note: shared LLM servers: {printable(exc)}")
        return
    for name, keys, why in left:
        log("WARNING", f"share {name}: its copy stays on {host}, which leaves sandbox-ai hosts: {why}")
        out(f"note: the copy of the shared LLM server {name} stays on {host} ({why})" +
            (f", and the key{'s' if len(keys) > 1 else ''} {', '.join(keys)} sent there" if keys else "") +
            f": take it away there with: sandbox-ai llm remove {name}" +
            "".join(f"; sandbox-ai secrets delete {key}" for key in keys))


# --- what is shown -----------------------------------------------------------------------------------------------

def host_view(endpoint, entry, host, state):
    """One host's state of a shared server, as llm list --json and the app show it."""
    names = key_names(endpoint)
    keys = state.get("keys") or {}
    sent = state.get("keys_sent") or []
    if not names:
        key = "none"
    elif any(keys.get(n) == "locked" for n in names):
        key = "locked"
    elif all(keys.get(n) == "there" for n in names):
        key = "sent" if all(n in sent for n in names) else "there"
    elif state.get("copied") and keys:
        key = "missing"
    else:
        key = "unknown"
    try:
        current = digest(payload(endpoint)) if not not_shared(endpoint) else None
    except ValueError:
        current = None
    copied = bool(state.get("copied"))
    return {"host": host, "copied": copied,
            "in_sync": copied and current is not None and state.get("digest") == current and not state.get("pending"),
            "key": key, "key_allowed": host in entry.get("key_to", []), "keys_sent": list(sent),
            "keys_missing": [n for n in names if keys.get(n) == "missing"],
            "reachable": state.get("reachable") if copied else None, "http": state.get("http"),
            "reach_error": state.get("reach_error"), "error": state.get("error"),
            "offline": bool(state.get("offline")), "pending": state.get("pending"),
            "synced_at": state.get("synced_at"), "checked_at": state.get("checked_at"),
            "tried_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state["tried_at"]))
            if isinstance(state.get("tried_at"), (int, float)) else None}


def view(endpoint, document=None, hosts_now=None):
    """The "shared" of an endpoint for llm list/show --json: None when it is not shared, else {"key_to", "since",
    "hosts": [host_view]} with every host of sandbox-ai hosts (one not asked yet: not copied)."""
    if document is None:
        if not shares_path().exists():
            return None
        try:
            document = load_shares()
        except (OSError, ValueError):
            return None
    entry = document["shares"].get(endpoint["name"])
    if entry is None or entry.get("removing"):
        return None
    if hosts_now is None:
        try:
            hosts_now = host_names()
        except (OSError, ValueError):
            hosts_now = sorted(entry["hosts"])
    return {"key_to": list(entry.get("key_to", [])), "since": entry.get("since"),
            "hosts": [host_view(endpoint, entry, host, entry["hosts"].get(host, {})) for host in hosts_now]}


def host_text(found, names, name="NAME", key_to=()):
    """One host's state in words (host_view's FOUND) of the shared server NAME, whose key goes to KEY_TO."""
    host = found["host"]
    if not found["copied"]:
        return f"{host}: not copied yet" + (f": {found['error']}" if found.get("error") else "")
    parts = ["copied", "in sync" if found["in_sync"] else "not in sync" + (f" ({found['error']})"
                                                                           if found.get("error") else "")]
    secrets = ", ".join(names)
    parts.append({"none": "no key needed", "sent": f"key {secrets} sent",
                  "there": f"key {secrets} there (its own)", "locked": "its secret store is locked (sandbox-ai hosts "
                  f"run {host} -- secrets unlock)", "unknown": "key not asked yet"}.get(found["key"]) or
                 f"key missing there ({', '.join(found['keys_missing']) or secrets}: sandbox-ai hosts run {host} -- "
                 f"secrets set {(found['keys_missing'] or names)[0]}, or sandbox-ai llm share {name} --key-to "
                 f"{','.join(sorted(set(key_to) | {host}))})")
    if found["reachable"]:
        parts.append("reachable from there" + (f" (HTTP {found['http']} without a key)"
                                               if found.get("http") in (401, 403) and found["key"] == "missing" else ""))
    elif found["reachable"] is False:
        parts.append("NOT reachable from there" + (f": {found['reach_error']}" if found.get("reach_error") else ""))
    return f"{host}: " + ", ".join(parts)


def host_lines(name, endpoint, entry, document=None):
    """The lines of llm list/show for a shared server: whom its key goes to, then one per host."""
    shown = view(endpoint, document={"shares": {name: entry}})
    if shown is None:
        return []
    names = key_names(endpoint)
    lines = [f"  for all hosts (llm share): key to {', '.join(shown['key_to']) or 'no host'}"]
    if not shown["hosts"]:
        lines.append("    no host yet (sandbox-ai hosts add NAME USER@HOST: it gets the copy)")
    lines += ["    " + host_text(found, names, name, shown["key_to"]) for found in shown["hosts"]]
    return [printable(line) for line in lines]


def managed_text(endpoint):
    """The line of a copy (on the host that received it)."""
    return (f"  managed from {endpoint['shared_from']} (shared from there with llm share): a change here is replaced "
            "by its next change there; to keep one of your own, add it under another name")


def key_here(endpoint, get=None):
    """On the host a copy is on: "none" (it names no key), "there", "missing" or "locked" - whether this host keeps
    the secrets it names (never a value)."""
    names = key_names(endpoint)
    if not names:
        return "none"
    states = key_states(endpoint, get or llm().load_secret_store_module().get)
    return "locked" if "locked" in states.values() else "there" if all(v == "there" for v in states.values()) \
        else "missing"


def key_here_text(endpoint, state):
    """The line of a copy whose key this host does not keep, with how to give it one; None when it has it."""
    if state not in ("missing", "locked"):
        return None
    missing = key_names(endpoint)
    if state == "locked":
        return "  its key: this host's secret store is locked (sandbox-ai secrets unlock)"
    return (f"  its key is not kept on this host: sandbox-ai secrets set {missing[0]}" +
            (f" (and {', '.join(missing[1:])})" if len(missing) > 1 else "") +
            f", or on {endpoint['shared_from']}: sandbox-ai llm share {endpoint['name']} --key-to HOST,...")


# --- the other host's side: sandbox-ai llm receive --------------------------------------------------------------

def key_states(endpoint, get):
    """{secret: "there"|"missing"|"locked"} of the secrets ENDPOINT names, on this host (never a value)."""
    states = {}
    for name in key_names(endpoint):
        try:
            states[name] = "there" if get(name, ask=False) is not None else "missing"
        except ValueError:  # (Locked: the encrypted files without their key)
            states[name] = "locked"
    return states


def reach(endpoint, states, get):
    """(reachable, HTTP status or None, error or None): whether the server answers from this host, as a session
    here would reach it (its VPN, its CA); with its key where this host has it, else without (an answer that refuses
    a missing key is an answer)."""
    llm_ = llm()
    usable = endpoint if all(state == "there" for state in states.values()) else \
        dict(endpoint, api_key_secret=None, headers={})
    path = "/api/tags" if endpoint["kind"] == "ollama" else "/v1/models"
    import ssl
    try:
        with llm_.through_vpn(endpoint) as tunnel:
            client = llm_.VPNClient(usable, tunnel, lambda name: get(name, ask=False), timeout=REACH_TIMEOUT) \
                if tunnel else llm_.Client(usable, lambda name: get(name, ask=False), timeout=REACH_TIMEOUT)
            status, _ = client.call(path)
    except (OSError, ValueError, ssl.SSLError) as exc:
        return False, None, printable(getattr(exc, "reason", exc))[:400]
    if 300 <= status < 400:
        return True, status, f"HTTP {status}: a redirect (not followed)"
    if status in (401, 403) and usable is endpoint:
        return True, status, f"HTTP {status}: the server refused the key kept here"
    return True, status, None


def answer_of(endpoint, get):
    states = key_states(endpoint, get)
    reachable, status, error = reach(endpoint, states, get)
    return {"keys": states, "reachable": reachable, "http": status, "reach_error": error}


def receive(origin, remove=None, check=None, stdin=None, out=print, get=None):
    """sandbox-ai llm receive --from ORIGIN [--remove NAME | --check NAME]: on this host, the copy of a server ORIGIN
    shares (its entry on standard input, as JSON) kept in llm.json, marked as managed from ORIGIN - never in place of
    an entry of this host's own -; or removed (REMOVE), or checked (CHECK). One JSON object answers."""
    llm_ = llm()
    if not ORIGIN.fullmatch(origin or ""):
        raise ValueError("--from names the machine that shares the server")
    get = get or llm_.load_secret_store_module().get

    def said(result):
        out(json.dumps(result))
        return 0 if result.get("ok") else 1
    name = remove or check
    if name is not None:
        if not llm_.NAME.fullmatch(name):
            raise ValueError(llm_.name_refusal(name))
        with llm_.settings_lock(llm_.store_path()):
            store = llm_.load_store()
            record = store["endpoints"].get(name)
            if record is None:
                return said({"ok": True, "present": False, "removed": False})
            if not isinstance(record, dict) or record.get("shared_from") != origin:
                return said({"ok": False, "present": True, "own": True,
                             "error": f"it has an LLM server {name} of its own (not shared from {origin}): it is left "
                                      "as it is"})
            if remove:
                store["endpoints"].pop(name)
                llm_.save_store(store)
                (llm_.ca_dir() / f"{name}.pem").unlink(missing_ok=True)
                log("INFO", f"{name}: the copy shared from {origin} removed")
                return said({"ok": True, "present": False, "removed": True})
        return said({"ok": True, "present": True, **answer_of(llm_.check_record(name, record), get)})
    data = (stdin or sys.stdin).read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("the entry is larger than 1 MiB")
    try:
        sent = json.loads(data)
    except (ValueError, RecursionError):
        raise ValueError("the entry on standard input is not JSON") from None
    if not isinstance(sent, dict) or not isinstance(sent.get("record"), dict) or \
            not isinstance(sent.get("name"), str) or not llm_.NAME.fullmatch(sent["name"]):
        raise ValueError("the entry on standard input is {\"name\", \"record\", \"ca\"}")
    name, record, ca = sent["name"], dict(sent["record"]), sent.get("ca")
    unknown = set(record) - set(COPIED)
    if unknown:
        raise ValueError(f"the entry has fields a copy does not take: {', '.join(sorted(map(str, unknown)))}")
    if ca is not None:
        if not isinstance(ca, str) or len(ca) > llm_.CA_LIMIT or not ca.isascii():
            raise ValueError("ca is the PEM text of the server's CA")
        llm_.check_ca(ca, "of the shared entry")
        record["ca"] = f"{name}.pem"
    record["shared_from"] = origin
    with llm_.settings_lock(llm_.store_path()):
        store = llm_.load_store()
        old = store["endpoints"].get(name)
        if old is not None and (not isinstance(old, dict) or old.get("shared_from") != origin):
            return said({"ok": False, "own": True,
                         "error": f"it has an LLM server {name} of its own (not shared from {origin}): it is left as "
                                  "it is; rename one of them"})
        old = old or {}
        if old.get("url") == record.get("url") and old.get("kind") == record.get("kind"):
            record.update({key: old[key] for key in LEARNED if key in old})  # what that server listed here, kept
        problem = not_shared_here(record)
        if problem:
            return said({"ok": False, "error": problem})
        llm_.check_record(name, {key: value for key, value in record.items() if key != "ca"})  # before anything is kept
        before = {key: old.get(key) for key in (*COPIED, "ca")}
        changed_ = before != {key: record.get(key) for key in (*COPIED, "ca")} or \
            (ca or None) != (old_ca(llm_, name) if old.get("ca") else None)
        if ca is not None:
            llm_.write_private(llm_.ca_dir() / f"{name}.pem", ca.encode())
        else:
            (llm_.ca_dir() / f"{name}.pem").unlink(missing_ok=True)
        store["endpoints"][name] = record
        llm_.save_store(store)
    if changed_:
        log("INFO", f"{name}: copy shared from {origin} kept" + (" (changed)" if old else ""))
    return said({"ok": True, "changed": changed_, **answer_of(llm_.check_record(name, record), get)})


def old_ca(llm_, name):
    try:
        return llm_.read_private(llm_.ca_dir() / f"{name}.pem", llm_.CA_LIMIT, "CA").decode()
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def not_shared_here(record):
    """A copy that names this host's loopback is refused (it would be another server here)."""
    try:
        parsed = llm().parse_url(record.get("url"))
    except ValueError as exc:
        return f"url: {exc}"
    return "the entry names a loopback address: here that is this host itself" if parsed["loopback"] else None
