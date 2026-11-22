#!/usr/bin/env python3
"""A session with its state to another system: exported into an archive, imported from one, duplicated here, or
copied and moved to a host of `sandbox-ai hosts` in one command.

  sandbox-ai session export NAME OUT.tar.gz [--with-workspace] [--no-checkpoints] [--include-secrets [--yes]]
  sandbox-ai session import FILE|- [--as NAME] [--map LOCAL=REMOTE]... [--no-workspace] [--check] [--replace]
                            [--grant-approvals] [--yes] [--json]
  sandbox-ai session duplicate NAME NEW [--with-workspace] [--map LOCAL=REMOTE]... [--no-checkpoints]
                            [--include-secrets] [--grant-approvals] [--yes] [--json]
  sandbox-ai session copy|move NAME HOST [--as NAME] [--map LOCAL=REMOTE]... [--with-workspace] [--no-checkpoints]
                            [--include-secrets] [--grant-approvals] [--yes] [--check] [--replace] [--config-only]

The state of a session: its configuration (the options it starts with, its trusted project file's merged in, as
`session copy` always took it), its HOME with the agent's own session files - so that the agent continues its
conversation at the other end (`sandbox-ai resume NAME`) -, the project's agent memory (CLAUDE.md, AGENTS.md, ...,
Claude's auto memory: the live copy in the HOME while it runs, else the project's memory store), the checkpoints of
its workspaces (`sandbox-ai checkpoint`; --no-checkpoints leaves them out), and the network approvals it was given:
what you allowed with `sandbox-ai net allow` and the approvals answered for its run, and what was allowed "always" for
its project. Never in it unless asked each time: logins, keys and files with a placeholder of a secret
(--include-secrets, asked: whoever has the archive can log in as you). Never at all: the canaries planted in its HOME
(the sandbox at the other end gets its own), its supervisor's sockets and records of this machine.

The workspace (/work: a folder of this machine) is mapped by default: the import starts it on the folder of the same
path there, or where --map LOCAL=REMOTE says. --with-workspace takes its files along into the archive (links,
sockets and devices are left out and listed); the import puts them where the workspace is mapped to - a folder that
is not there yet or is empty (never mixed into one that has files).

A running session is paused (frozen: sandbox-ai pause) for the moment of the snapshot, so that its HOME, memory and
workspace are consistent; a copy or duplicate thaws it right after, a move keeps it paused until it runs at the other
end and then stops it here (a move that fails there thaws it again).

The archive is untrusted input to `session import`, wherever it came from: read as a stream, never extracted by tar
itself - only folders and regular files below its one top folder (no absolute path, no '..', no link, hard link,
device, FIFO or sparse file: the whole archive is refused), every file written by this process with O_EXCL and
O_NOFOLLOW into a private staging folder, its mode cut to the owner's read/write (and execute) bits (no setuid,
setgid, sticky bit), within the export's limits (one file, all files, their number, folders deep, tar headers),
keeping room on the disk; its manifest.json (schema, version) must list every file with its size and SHA-256, and
every file must match it. The checkpoints' content is hashed again into the store (an object whose content is not
its name refuses the archive). Its configuration is checked like a start's: the agent is one sandbox-ai has, the
options parse, options a session never saves (its HOME, sockets, pod, name) refuse it, the folders are mapped, and
the launcher's own dry run (session check_start: the administrator's policy, the security mode, private locations,
devices, the network) must pass before anything is kept. Options that give the sandbox secrets of this machine
(--api-key, --env, --git-token-*, --auth-file, ...) or folders of it other than its workspace are listed and asked
first (--yes where nothing can ask). Its network approvals are shown and granted again only when asked
(--grant-approvals, asked; --yes): as options of that session (--allow-host, --allow-tcp), never silently and never
for the project.

Over SSH (copy, move) the archive goes as the standard input of `sandbox-ai session import -` there: the host needs
only sshd and a sandbox-ai with `session import` (0.1.193 on), no rsync or scp; it checks what arrives as above, then
`session resume` starts it there. --config-only copies only the configuration, as before 0.1.193.

Standard library only.
"""
import contextlib
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat
import sys
import tarfile
import time
import zlib

HERE = Path(__file__).resolve().parent
ARCHIVE_SCHEMA = "sandbox-ai-session-archive"
ARCHIVE_VERSION = 2
CHUNK = 1 << 20
HEADER_LIMIT = 1 << 20      # bytes of one tar header record (a PAX header, a GNU long name) an import reads
MANIFEST_LIMIT = 256 << 20  # bytes of the manifest (500 000 files and their checksums)
RECORD_LIMIT = 4 << 20      # bytes of session.json read
NAME_LIMIT = 4096           # characters of a member's name
PART_LIMIT = 255            # bytes of one part of it
RESERVE = 512 << 20         # bytes an import leaves free on the disk of its staging folder
SPACE_STEP = 64 << 20       # bytes written between two looks at the free space
OPTIONS_LIMIT = 2000        # words of a configuration
# Options a session's saved configuration never holds (the launcher adds them at every start: its HOME, sockets,
# pod, name; or they are commands, not configuration): an archive that names one is refused.
NEVER_SAVED = ("--state-dir", "--start-id", "--tmux-socket-dir", "--pod-name", "--mcp-socket-dir", "--helper-of",
               "--mcp-approvals-dir", "--session", "--name", "--project-name", "--keep", "--no-keep", "--dry-run",
               "--probe", "--version", "--help", "--config")
# Options that give the sandbox (or its proxy) a secret or login of the machine that starts it: asked at an import.
SECRET_OPTIONS = ("--env", "--secret-env", "--api-key", "--git-token-env", "--git-token-file", "--git-token-secret",
                  "--git-credentials", "--auth-file", "--copy", "--copy-auth", "--auth-from-session",
                  "--import-codex-auth", "--publish", "--local-config")
APPROVAL_KINDS = ("dns", "connect")  # the run's decisions that become start options again (--allow-host, --allow-tcp)

_modules = {}


def module(name):
    """sandbox-ai's NAME.py, loaded once by path (session_inspect, kits, checkpoints, net, approvals, hosts ...)."""
    if name not in _modules:
        loaded = sys.modules.get(f"sandbox_ai_{name}")
        if loaded is None:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_state", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def si():
    return module("session_inspect")


def printable(text):
    return si().one_line(text)


def human(size):
    return si().human(size)


class ImportRefused(ValueError):
    """The archive is not one this import takes (damaged, made otherwise, or hostile): nothing of it was kept."""


# --- the state's parts in an export --------------------------------------------------------------------------------

def start_plan(s, key):
    """{"agent", "options", "agent_args", "cwd", "problems"} the session KEY starts with elsewhere (sessions.py's
    transfer_plan: its saved options with its trusted project file's merged in; --no-config at the other end)."""
    agent, options, agent_args, problems = s.transfer_plan(key)
    data = s.load(key)
    return {"agent": agent, "options": options, "agent_args": agent_args, "cwd": data.get("cwd"),
            "workspace": data.get("workspace"), "project": data.get("project"),
            "problems": [str(p) for p in problems]}


def add_memory(exporter, prefix, found):
    """The project's agent memory into PREFIX/memory (its paths as in a HOME: kits.memory_paths): the HOME's live copy
    while the start runs or is paused, else the project's store. {"source", "files"} or None (none)."""
    if not found.get("memory") and found.get("home") is None:
        return None
    try:
        source, root = si().memory_source(found, "auto")
    except ValueError:
        return None
    files, notes, _ = module("kits").collect(root, found["agent"])
    exporter.where = "memory/"
    taken = 0
    for relative, data in sorted(files.items()):
        if not exporter.include_secrets:
            why = si().secret_name(relative) or si().secret_content(data[:si().SCAN_BYTES])
            if why:
                exporter.leave(relative, why, secret=True)
                continue
        exporter.add_bytes(f"{prefix}/memory/{relative}", data)
        taken += 1
    return {"source": source, "files": taken, "notes": [printable(n) for n in notes[:20]]}


def object_secret(store, digest, cache):
    """Why the checkpoint object DIGEST holds a login or key (its first bytes: a private key, a placeholder)."""
    if digest not in cache:
        why = None
        with contextlib.suppress(OSError):
            fd = store.open_object(digest)
            try:
                why = si().secret_content(os.read(fd, si().SCAN_BYTES))
            finally:
                os.close(fd)
        cache[digest] = why
    return cache[digest]


def add_checkpoints(exporter, prefix, found):
    """The checkpoints of the start (sandbox-ai checkpoint list NAME) into PREFIX/checkpoints, laid out as a store
    (meta/, manifests/, objects/AB/REST: each content once). A file that is a login or key - by its name, or by its
    content - is left out of the checkpoint unless secrets are taken in: listed as skipped there, so that a restore
    never touches it. {"ids", "bytes"} or None."""
    cp = module("checkpoints")
    try:
        metas = cp.listing(run=found["id"])
    except (ValueError, OSError):
        return None
    if not metas:
        return None
    exporter.where = "checkpoints/"
    objects, cache, ids = {}, {}, []
    for meta in sorted(metas, key=lambda m: (m.get("created_ts") or 0, m["id"])):
        store = cp.store_by_name(meta["store"])
        try:
            document = store.manifest(meta["id"])
        except ValueError as exc:
            exporter.leave(meta["id"], printable(exc))
            continue
        for root in document["roots"]:
            kept = []
            for entry in root["entries"]:
                if entry[0] == "f" and not exporter.include_secrets:
                    why = si().secret_path(entry[1]) or object_secret(store, entry[5], cache)
                    if why:
                        root.setdefault("skipped", []).append([entry[1], f"{why}: not exported (--include-secrets)"])
                        exporter.leave(f"{meta['id']}/{entry[1]}", why, secret=True)
                        continue
                if entry[0] == "f":
                    objects.setdefault(entry[5], store)
                kept.append(entry)
            root["entries"] = kept
        record = {k: v for k, v in meta.items() if k not in ("store", "pruned")}
        exporter.add_bytes(f"{prefix}/checkpoints/meta/{meta['id']}.json", json.dumps(record).encode())
        exporter.add_bytes(f"{prefix}/checkpoints/manifests/{meta['id']}.json.gz",
                           gzip.compress(json.dumps(document, separators=(",", ":")).encode(), mtime=0))
        ids.append(meta["id"])
    before = exporter.bytes
    for digest, store in sorted(objects.items()):
        try:
            folder = os.open(f"{store.base}/{digest[:2]}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            exporter.leave(f"objects/{digest}", "not in the store")
            continue
        try:
            exporter.add_file(folder, digest[2:], f"{prefix}/checkpoints/objects/{digest[:2]}/{digest[2:]}",
                              f"objects/{digest}", checks=False)
        finally:
            os.close(folder)
    return {"ids": ids, "objects": len(objects), "bytes": exporter.bytes - before}


def add_workspace(exporter, prefix, found):
    """The files of the main workspace into PREFIX/workspace (--with-workspace): what the export takes of a HOME
    (regular files and folders; links, sockets, devices left out and listed; logins and keys unless asked)."""
    workspace = found.get("workspace")
    if not isinstance(workspace, str) or not os.path.isabs(workspace):
        raise ValueError(f"{printable(found['name'])} names no workspace folder of this machine to take along")
    try:
        fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ValueError(f"its workspace {printable(workspace)} cannot be read: {exc.strerror or exc}") from None
    exporter.where = "workspace/"
    before, files = exporter.bytes, exporter.files
    try:
        exporter.add_tree(fd, f"{prefix}/workspace", "")
    finally:
        os.close(fd)
    return {"host": workspace, "files": exporter.files - files, "bytes": exporter.bytes - before}


def tcp_of(details):
    with contextlib.suppress(Exception):  # noqa: BLE001 - not an address: not carried
        return module("net").tcp_text(details["address"], int(details["port"]))
    return None


def collect_approvals(found):
    """{"always": {"hosts", "tcp"} of the project, "run": {"hosts", "tcp"} of its current run (sandbox-ai net allow:
    given.json; the approvals answered for the run: its supervisor's decisions), "not_carried": [what]}: shown and
    granted again at the other end only when asked. Never fails the export (what cannot be read is said)."""
    result = {"always": {"hosts": [], "tcp": []}, "run": {"hosts": [], "tcp": []}, "not_carried": []}
    approvals = module("approvals")
    workspace = found.get("workspace")
    if isinstance(workspace, str) and os.path.isabs(workspace):
        with contextlib.suppress(Exception):  # noqa: BLE001 - no approvals file: none
            result["always"] = approvals.approved_for(workspace)
    entry = None
    with contextlib.suppress(Exception):  # noqa: BLE001
        entry = module("starts").find(found["id"])
    run = entry.get("run") if isinstance(entry, dict) else None
    if found.get("state") not in ("running", "paused") or not run:
        return result
    net = module("net")
    try:
        record = net.running().get(run)
    except Exception:  # noqa: BLE001
        record = None
    if not record:
        return result
    given = net.given(record)
    hosts, tcp = set(given["hosts"]), set(given["tcp"])
    try:
        decided = approvals.decided(record).get("decided") or []
    except Exception as exc:  # noqa: BLE001 - its supervisor gave no answer
        decided = []
        result["not_carried"].append(f"the run's answered approvals could not be read ({printable(exc)[:200]})")
    for item in decided if isinstance(decided, list) else []:
        if not isinstance(item, dict) or item.get("decision") != "allow" or item.get("scope") not in ("run", "always"):
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        if item.get("kind") == "dns" and isinstance(details.get("name"), str):
            hosts.add(details["name"])
        elif item.get("kind") == "connect" and tcp_of(details):
            tcp.add(tcp_of(details))
        else:
            what = " ".join(str(v) for v in details.values())[:200]
            result["not_carried"].append(f"{printable(item.get('kind'))}: {printable(what)} (asked again there)")
    result["run"] = {"hosts": sorted(h for h in hosts if approvals.HOST.fullmatch(h)),
                     "tcp": sorted(t for t in tcp if approvals.valid_tcp(t))}
    return result


def parts_for(s, key, with_workspace=False, checkpoints=True):
    """EXTRA of session_inspect.export: the session's start plan, memory, checkpoints, workspace and approvals."""
    def extra(exporter, prefix, found):
        parts = {"start": start_plan(s, key), "approvals": collect_approvals(found)}
        parts["memory"] = add_memory(exporter, prefix, found)
        parts["checkpoints"] = add_checkpoints(exporter, prefix, found) if checkpoints else None
        parts["workspace"] = add_workspace(exporter, prefix, found) if with_workspace else None
        return parts
    return extra


@contextlib.contextmanager
def snapshot(s, key, keep_paused=False):
    """The session KEY held still for a snapshot: a running one paused (frozen) and thawed again at the end - unless
    KEEP_PAUSED (a move: it stays paused until it runs at the other end). Yields {"state", "paused"}."""
    data = s.load(key)
    state = s.status(key, data)
    held = {"state": state, "paused": False}
    if state == "running":
        s.pause(key)
        held["paused"] = True
    try:
        yield held
    finally:
        if held["paused"] and not keep_paused:
            thaw(s, key)


def thaw(s, key):
    with contextlib.suppress(ValueError, OSError):
        if s.status(key, s.load(key)) == "paused":
            s.resume(key)


def export_state(s, name, out, include_secrets=False, with_workspace=False, checkpoints=True, keep_paused=False,
                 held=None):
    """`sandbox-ai session export`: the archive of the start NAME with its state (session_inspect.export with
    parts_for), a running session paused for the moment (snapshot). HELD: a dict that gets the snapshot's state."""
    found = si().locate(name)
    key = found["id"]
    if with_workspace and isinstance(found.get("workspace"), str):
        target = Path(os.path.abspath(os.path.expanduser(out))).parent
        workspace = Path(os.path.realpath(found["workspace"]))
        real = Path(os.path.realpath(target))
        if real == workspace or workspace in real.parents:
            raise ValueError(f"{printable(out)} lies in the workspace that is taken along: write it elsewhere")
    in_sessions = (s.root() / str(key) / "session.json").is_file()
    context = snapshot(s, key, keep_paused) if in_sessions else contextlib.nullcontext({"state": found["state"]})
    with context as state:
        if held is not None:
            held.update(state)
        result = si().export(name, out, include_secrets, extra=parts_for(s, key, with_workspace, checkpoints)
                             if in_sessions else None)
    result["snapshot"] = state.get("state")
    return result


def export_lines(result):
    lines = si().export_lines(result)
    parts = result.get("parts") or {}
    memory, checkpoints, workspace = parts.get("memory"), parts.get("checkpoints"), parts.get("workspace")
    lines.append("with: its configuration, its HOME and the agent's session files"
                 + (f", {memory['files']} memory files (from its {memory['source']})" if memory else "")
                 + (f", {len(checkpoints['ids'])} checkpoints ({human(checkpoints['bytes'])})" if checkpoints else "")
                 + (f", its workspace ({workspace['files']} files)" if workspace else ""))
    approvals = parts.get("approvals") or {}
    shown = approval_words(approvals)
    if shown:
        lines.append(f"network approvals (granted again at the import only when asked): {' '.join(shown)}")
    for problem in (parts.get("start") or {}).get("problems") or []:
        lines.append(f"note: {printable(problem)}")
    if result.get("snapshot") == "running":
        lines.append("it was paused for the snapshot and runs on")
    return lines


def approval_words(approvals):
    """The network approvals of an archive as start options ("--allow-host H", "--allow-tcp T"), each once."""
    words = []
    for scope in ("run", "always"):
        part = approvals.get(scope) if isinstance(approvals, dict) else None
        part = part if isinstance(part, dict) else {}
        for host in part.get("hosts") or []:
            if isinstance(host, str) and f"--allow-host {host}" not in words:
                words.append(f"--allow-host {host}")
        for target in part.get("tcp") or []:
            if isinstance(target, str) and f"--allow-tcp {target}" not in words:
                words.append(f"--allow-tcp {target}")
    return words


# --- reading an archive (untrusted) --------------------------------------------------------------------------------

class CheckedInfo(tarfile.TarInfo):
    """A tar header whose own records are bounded: a PAX header or a GNU long name larger than HEADER_LIMIT is never
    read into memory (tarfile reads them whole)."""

    def _proc_member(self, tar):
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME,
                         tarfile.GNUTYPE_LONGLINK) and self.size > HEADER_LIMIT:
            raise ImportRefused(f"a tar header of {human(self.size)}: not an archive of sandbox-ai session export")
        return super()._proc_member(tar)


def member_parts(name):
    """The parts of the member NAME (relative, '/'-separated); ImportRefused for anything else."""
    if not isinstance(name, str) or not name or len(name) > NAME_LIMIT or "\\" in name or \
            any(not c.isprintable() for c in name):
        raise ImportRefused(f"a member's name is not one of an export: {printable(name)[:200]}")
    parts = name.rstrip("/").split("/")
    if name.startswith("/") or any(part in ("", ".", "..") or len(os.fsencode(part)) > PART_LIMIT for part in parts):
        raise ImportRefused(f"a member's path leads outside the archive's folder: {printable(name)[:200]}")
    return parts


def open_dirs(root_fd, parts, created):
    """The descriptor of ROOT_FD/PARTS, each folder made (0700) where missing: only folders this import made (CREATED:
    their paths) are entered, so nothing there is a link."""
    fd = os.dup(root_fd)
    path = ()
    try:
        for part in parts:
            path += (part,)
            if path not in created:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    raise ImportRefused(f"{printable('/'.join(path))} is in the archive twice") from None
                created.add(path)
            inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = inner
        return fd
    except BaseException:
        os.close(fd)
        raise


def free_bytes(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def read_archive(stream, staging):
    """Read the archive STREAM into the empty private folder STAGING, checking every member (see the module's
    text), then its manifest: (manifest, staged folder of its top folder). ImportRefused, OSError otherwise (the
    caller removes STAGING)."""
    limits = si()
    staged, created, total, entries, prefix = {}, set(), 0, 0, None
    root_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    checked_at = 0
    try:
        try:
            tar = tarfile.open(fileobj=stream, mode="r|gz", tarinfo=CheckedInfo)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ImportRefused(f"not a tar.gz archive ({printable(exc)[:200]})") from None
        with tar:
            while True:
                try:
                    member = tar.next()
                except (tarfile.TarError, EOFError, OSError, ValueError, UnicodeError) as exc:
                    raise ImportRefused(f"the archive is damaged ({printable(exc)[:200]})") from None
                if member is None:
                    break
                parts = member_parts(member.name)
                if prefix is None:
                    prefix = parts[0]
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", prefix):
                        raise ImportRefused(f"the archive's folder is not one of an export: {printable(prefix)}")
                elif parts[0] != prefix:
                    raise ImportRefused(f"a member outside the archive's folder {printable(prefix)}: "
                                        f"{printable(member.name)[:200]}")
                entries += 1
                if entries > limits.EXPORT_FILES + 2:
                    raise ImportRefused(f"more than {limits.EXPORT_FILES} files and folders")
                if len(parts) > limits.EXPORT_DEPTH + 8:
                    raise ImportRefused(f"{printable(member.name)[:200]}: nested too deep")
                if member.isdir():
                    os.close(open_dirs(root_fd, parts, created))
                    continue
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.issparse():
                    kind = ("a link" if member.issym() or member.islnk() else
                            "a device" if member.ischr() or member.isblk() else "a FIFO" if member.isfifo()
                            else "not a regular file")
                    raise ImportRefused(f"{printable(member.name)[:200]} is {kind}: an export holds only folders and "
                                        "regular files")
                if member.size > max(limits.EXPORT_FILE_LIMIT, MANIFEST_LIMIT):
                    raise ImportRefused(f"{printable(member.name)[:200]} is larger than {human(limits.EXPORT_FILE_LIMIT)}")
                total += member.size
                if total > limits.EXPORT_TOTAL:
                    raise ImportRefused(f"more than {human(limits.EXPORT_TOTAL)} in all: too large to import")
                if total - checked_at >= SPACE_STEP or member.size >= SPACE_STEP:
                    checked_at = total
                    if free_bytes(staging) < member.size + RESERVE:
                        raise ImportRefused(f"the disk of {printable(staging)} is too full to take it (keeps "
                                            f"{human(RESERVE)} free)")
                key = tuple(parts)
                if key in staged or key in created:
                    raise ImportRefused(f"{printable(member.name)[:200]} is in the archive twice")
                folder = open_dirs(root_fd, parts[:-1], created)
                try:
                    mode = 0o700 if member.mode & 0o100 else 0o600  # (only the owner's bits: no setuid, setgid)
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode,
                                 dir_fd=folder)
                finally:
                    os.close(folder)
                digest, left = hashlib.sha256(), member.size
                try:
                    source = tar.extractfile(member)
                    while left > 0:
                        chunk = source.read(min(CHUNK, left))
                        if not chunk:
                            raise ImportRefused(f"{printable(member.name)[:200]} ends early: the archive is damaged")
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(fd, view):]
                        left -= len(chunk)
                    if member.mtime and 0 < member.mtime < 2 ** 33:
                        os.utime(fd, (member.mtime, member.mtime))  # (the agent's newest conversation is the newest)
                except (tarfile.TarError, EOFError, zlib.error) as exc:
                    raise ImportRefused(f"the archive is damaged ({printable(exc)[:200]})") from None
                finally:
                    os.close(fd)
                staged[key] = [member.size, digest.hexdigest()]
    finally:
        os.close(root_fd)
    if prefix is None:
        raise ImportRefused("the archive is empty")
    return check_manifest(staging, prefix, staged), Path(staging) / prefix


def check_manifest(staging, prefix, staged):
    """The manifest of the staged archive, checked against STAGED ({parts: [size, sha256]}): every file in it, of its
    size and checksum, and nothing else."""
    path = Path(staging) / prefix / si().MANIFEST
    if (prefix, si().MANIFEST) not in staged:
        raise ImportRefused("the archive has no manifest.json: it was not made by `sandbox-ai session export` of "
                            "0.1.193 or later (export it again there)")
    if path.stat().st_size > MANIFEST_LIMIT:
        raise ImportRefused("its manifest.json is too large")
    try:
        manifest = json.loads(path.read_bytes())
    except (ValueError, RecursionError, UnicodeError):
        raise ImportRefused("its manifest.json is no JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != ARCHIVE_SCHEMA:
        raise ImportRefused("its manifest.json is not one of a session's archive")
    version = manifest.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 2:
        raise ImportRefused("its manifest.json has no version this import knows")
    if version > ARCHIVE_VERSION:
        raise ImportRefused(f"it was made by a newer sandbox-ai ({printable(manifest.get('sandbox_ai'))}, archive "
                            f"version {version}): update this one (sandbox-ai update)")
    if manifest.get("prefix") != prefix:
        raise ImportRefused("its manifest.json names another folder")
    members = manifest.get("members")
    if not isinstance(members, dict):
        raise ImportRefused("its manifest.json lists no files")
    listed = {}
    for name, value in members.items():
        if not (isinstance(value, list) and len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str)):
            raise ImportRefused("its manifest.json is damaged")
        listed[tuple(member_parts(name))] = value
    files = {key: value for key, value in staged.items() if key != (prefix, si().MANIFEST)}
    for key, value in files.items():
        if key not in listed:
            raise ImportRefused(f"{printable('/'.join(key))[:200]} is not in its manifest")
        if listed[key] != value:
            raise ImportRefused(f"{printable('/'.join(key))[:200]} does not match its manifest (size or SHA-256): "
                                "the archive is damaged or was changed")
    missing = [key for key in listed if key not in files]
    if missing:
        raise ImportRefused(f"{printable('/'.join(missing[0]))[:200]} is in its manifest but not in the archive "
                            f"({len(missing)} missing)")
    return manifest


# --- the configuration it brings, checked here ----------------------------------------------------------------------

def words(value, what):
    if not isinstance(value, list) or len(value) > OPTIONS_LIMIT or not all(
            isinstance(v, str) and "\0" not in v and len(v) <= 8192 for v in value):
        raise ImportRefused(f"its {what} are no list of words")
    return list(value)


def checked_start(s, manifest):
    """(agent, options, agent arguments, cwd, workspace) of the archive's start plan, checked: an agent sandbox-ai
    has, options that parse and that a saved configuration holds, every value a word."""
    plan = (manifest.get("parts") or {}).get("start")
    if not isinstance(plan, dict):
        raise ImportRefused("its manifest names no configuration to start with")
    agent = plan.get("agent")
    if agent not in s.CONTINUE_ARGS:
        raise ImportRefused(f"its agent {printable(agent)[:60]} is not one of this sandbox-ai's")
    options, agent_args = words(plan.get("options"), "options"), words(plan.get("agent_args") or [], "agent arguments")
    for value in options:
        name = value.split("=", 1)[0]
        if name in NEVER_SAVED or name.startswith("--internal"):
            raise ImportRefused(f"its options name {printable(name)}, which a session's configuration never holds")
    l = s.launcher()
    try:
        l.parse_args([agent, *options])
    except SystemExit:
        raise ImportRefused("its options are not ones this sandbox-ai takes (an older one? see what "
                            f"`sandbox-ai {agent} --dry-run {shlex.join(options)[:300]}` says)") from None
    except ValueError as exc:
        raise ImportRefused(f"its options are refused: {printable(exc)[:300]}") from None
    cwd, workspace = plan.get("cwd"), plan.get("workspace")
    for value, what in ((cwd, "folder it starts in"), (workspace, "workspace")):
        if value is not None and not (isinstance(value, str) and os.path.isabs(value) and "\0" not in value
                                      and "\n" not in value):
            raise ImportRefused(f"its {what} is no absolute path")
    return agent, options, s.leading_options(agent, agent_args), cwd, workspace


def asked_options(s, options, maps, workspace=None, cwd=None, files=False):
    """What of the archive's configuration (OPTIONS and its WORKSPACE and CWD: as the archive names them, before
    MAPS) is asked before an import keeps it: options that give the sandbox a secret or login of this machine, and
    every folder or file of this machine it names that no --map of yours placed - the archive chose it, not you (the
    workspace too: the sandbox works in it; FILES: the archive's workspace files are written there). Each once."""
    found = []
    for index, value in enumerate(options):
        name = value.split("=", 1)[0]
        if name in SECRET_OPTIONS:
            given = value.split("=", 1)[1] if "=" in value else (
                options[index + 1] if index + 1 < len(options) and not options[index + 1].startswith("-") else "")
            found.append(f"{name} {given}".strip() + ": gives the sandbox a secret or login of this machine")
    # (the folder it starts in is not asked: the launcher starts there with --no-config and its workspace named, so
    # it reads nothing of it and gives the sandbox nothing of it)
    named = [path for path in (workspace,) if isinstance(path, str)] + list(s.option_paths(options))
    for path in dict.fromkeys(os.path.normpath(p) for p in named):
        if s.mapped(path, maps) != path:
            continue  # (you said where it lies here)
        why = ("the workspace it works in" + (", its files from the archive written there" if files else "")
               if path == (os.path.normpath(workspace) if workspace else None) else
               "a folder or file it mounts, reads or writes")
        found.append(f"{path}: {why} - as the archive names it (--map {path}=FOLDER to choose another)")
    return found


# --- importing -------------------------------------------------------------------------------------------------------

def staging_root(s):
    folder = s.root()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def ask_yes(question, ask=None):
    """The human's yes to QUESTION at a terminal (ASK: the function that asks, for tests); None when nothing can ask."""
    if ask is None:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            return None
        ask = lambda text: (sys.stderr.write(text), sys.stderr.flush(), sys.stdin.readline())[2]  # noqa: E731
    return str(ask(question) or "").strip().lower() in ("y", "yes")


def mapped_path(s, path, maps):
    return s.mapped(path, maps) if isinstance(path, str) else path


def import_memory(staged, workspace, agent):
    """The archive's memory files into the project's memory store of WORKSPACE here: added where the store has none
    of that path; one the store has differently stays (the store is the authority: each start puts it into the
    HOME) and is named. {"added", "kept_here"}."""
    kits = module("kits")
    if not staged.is_dir():
        return {"added": 0, "kept_here": []}
    files, _, _ = kits.collect(staged, agent)  # (only the memory's paths; links and others are not read)
    store = kits.memory_store(workspace, agent)
    added, kept = 0, []
    with kits.locked(store.parent):
        if store.is_symlink():
            raise ValueError(f"{store} is a symlink; refusing to use it")
        existing = kits.collect(store, agent)[0] if store.is_dir() else {}
        store.mkdir(mode=0o700, exist_ok=True)
        for relative, data in sorted(files.items()):
            if relative not in existing:
                kits.write_at(store, relative, data)
                added += 1
            elif existing[relative] != data:
                kept.append(relative)
    return {"added": added, "kept_here": kept, "store": str(store)}


def verify_checkpoints(staged):
    """Before anything is kept: every checkpoint's record and manifest of the archive readable (checkpoints.py's own
    checks: paths, types, digests), and every content object's SHA-256 its name. [(meta, document)]."""
    cp = module("checkpoints")
    if not (staged / "meta").is_dir():
        return []
    found = []
    for path in sorted((staged / "meta").iterdir()):
        meta = cp.read_meta(path) if path.name.endswith(".json") and path.is_file() else None
        if meta is None or path.name != f"{meta['id']}.json" or not all(
                isinstance(r, dict) and isinstance(r.get("host"), str) for r in meta["roots"]):
            raise ImportRefused(f"the checkpoint record {printable(path.name)} is damaged")
        manifest = staged / "manifests" / f"{meta['id']}.json.gz"
        if not manifest.is_file():
            raise ImportRefused(f"the checkpoint {meta['id']} has no manifest in the archive")
        try:
            document = cp.read_manifest(manifest, meta["id"])
        except ValueError as exc:
            raise ImportRefused(str(exc)) from None
        found.append((meta, document))
    objects = staged / "objects"
    for folder in sorted(objects.iterdir()) if objects.is_dir() else []:
        if not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", folder.name):
            raise ImportRefused(f"the checkpoints' content holds {printable(folder.name)[:70]}, not a content folder")
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.is_symlink():
                raise ImportRefused(f"the checkpoints' content holds {printable(path.name)[:70]}, not a file")
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != folder.name + path.name:
                raise ImportRefused(f"content of a checkpoint is not what its name says (SHA-256): "
                                    f"{printable(folder.name + path.name)[:70]} - the archive was changed")
    return found


def import_checkpoints(staged, project, maps_of, key, name, agent, folders=None):
    """The archive's checkpoints (a store's layout below STAGED) into the store of PROJECT here, each a new id, its
    folders mapped (MAPS_OF(path)), of the start KEY (NAME); every object hashed again on its way into the store.
    {"ids", "new_bytes"}."""
    cp = module("checkpoints")
    found = verify_checkpoints(staged)
    if not found:
        return None
    store = cp.Store.of(project)
    ids, new_bytes, roots, newest = [], 0, [], None
    with store.locked():
        for meta, document in sorted(found, key=lambda item: (item[0].get("created_ts") or 0, item[0]["id"])):
            # Only folders of the imported configuration (its workspace, its mounts): a restore writes into a root,
            # and an archive never chooses another folder of this machine that way.
            for root in document["roots"]:
                root["host"] = os.path.normpath(maps_of(root["host"])) if os.path.isabs(root["host"]) else ""
            document["roots"] = [root for root in document["roots"] if folders is None or root["host"] in folders]
            if not document["roots"]:
                continue
            for root in document["roots"]:
                for entry in root["entries"]:
                    if entry[0] != "f" or store.has(entry[5]):
                        continue
                    source = staged / "objects" / entry[5][:2] / entry[5][2:]
                    try:
                        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    except OSError:
                        raise ImportRefused(f"the checkpoint {meta['id']} names content the archive does not hold"
                                            ) from None
                    try:
                        digest, added = store.put(fd, os.fstat(fd).st_size)
                    finally:
                        os.close(fd)
                    if digest != entry[5]:
                        raise ImportRefused(f"content of the checkpoint {meta['id']} is not what its name says "
                                            "(SHA-256): the archive was changed")
                    new_bytes += added
            created = meta.get("created_ts") if isinstance(meta.get("created_ts"), (int, float)) else None
            ident = cp.new_id(created)
            while store.meta_path(ident).exists():
                ident = cp.new_id(created)
            document["id"] = ident
            cp.write_json(store.manifest_path(ident), document, compress=True)
            kept = {root["host"] for root in document["roots"]}
            summaries = [{**summary, "host": os.path.normpath(maps_of(summary["host"]))} for summary in meta["roots"]
                         if isinstance(summary, dict) and isinstance(summary.get("host"), str)
                         and os.path.isabs(summary["host"]) and os.path.normpath(maps_of(summary["host"])) in kept]
            record = {**{k: v for k, v in meta.items() if k not in ("store", "pruned")}, "id": ident,
                      "project": project, "start": key, "name": name, "agent": agent, "run": None,
                      "roots": summaries, "imported": {"id": meta["id"], "at": si().iso(time.time())}}
            cp.write_json(store.meta_path(ident), record)
            ids.append(ident)
            roots, newest = [{k: r.get(k) for k in ("host", "sandbox", "kind")} for r in summaries], (ident, record)
        usage = store.usage()
        if usage is not None:
            store.set_usage(usage + new_bytes)
    if newest is not None:
        cp.record_run(key, {"run": None, "start": key, "name": name, "agent": agent, "project": project,
                            "roots": roots, "exclude": newest[1].get("exclude"), "checkpoint": newest[0], "every": 0})
    return {"ids": ids, "new_bytes": new_bytes}


def empty_or_missing(path):
    try:
        return not any(os.scandir(path))
    except FileNotFoundError:
        return True
    except NotADirectoryError:
        return False


def place_tree(source, target):
    """The staged tree SOURCE (folders and regular files this import wrote) moved to TARGET (missing, or an empty
    folder): renamed on the same file system, else copied (no link followed: there is none in it)."""
    target = Path(target)
    if target.is_dir() and not target.is_symlink():
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except OSError:
        shutil.copytree(source, target, symlinks=True)
        shutil.rmtree(source, ignore_errors=True)


def import_archive(s, source, new_name=None, maps=(), check=False, replace=False, grant=False, yes=False,
                   no_workspace=False, ask=None, out=print, quiet=False):
    """`sandbox-ai session import`: the archive SOURCE (a path, or a binary stream) as a kept, stopped session of
    this machine that `sandbox-ai resume NAME` continues (see the module's text). CHECK: everything checked and the
    launcher's dry run, nothing kept. The result: {"name", "id", "agent", "options", "cwd", "workspace", "home",
    "memory", "checkpoints", "workspace_files", "approvals", "granted", "asked", "warnings", "checked", "imported"}."""
    maps = s.parse_maps(maps)
    staging = Path(staging_root(s)) / f".import-{secrets.token_hex(6)}"
    staging.mkdir(mode=0o700)
    made_key = made_workspace = None
    try:
        stream = open(source, "rb") if isinstance(source, (str, Path)) else source
        try:
            manifest, top = read_archive(stream, staging)
        finally:
            if isinstance(source, (str, Path)):
                stream.close()
        agent, options, agent_args, cwd, workspace = checked_start(s, manifest)
        verify_checkpoints(top / "checkpoints")
        name = new_name or manifest.get("name")
        if not isinstance(name, str) or not re.fullmatch(s.SESSION_NAME, name):
            raise ValueError(f"not a session name: {printable(name)} (choose one: --as NAME)")
        existing = None
        with contextlib.suppress(ValueError):
            existing = s.resolve(name)
        if existing is not None and not replace:
            raise ValueError(f"there is a session {printable(name)} here already: choose another name (--as NAME) or "
                             "replace it (--replace: it is deleted first, its HOME too)")
        if existing is not None and s.status(existing, s.load(existing)) != "stopped":
            raise ValueError(f"the session {printable(name)} here runs: stop it first (sandbox-ai stop "
                             f"{printable(name)}), or choose another name (--as NAME)")
        parts = manifest.get("parts") or {}
        workspace_here = mapped_path(s, workspace, maps)
        cwd_here = mapped_path(s, cwd, maps)
        options_here = ["--no-config", *[v for v in s.map_options(options, maps) if v != "--no-config"]]
        with_files = isinstance(parts.get("workspace"), dict) and (top / "workspace").is_dir() and not no_workspace
        warnings = [f"cannot go along: {printable(p)}" for p in (parts.get("start") or {}).get("problems") or []]
        if with_files and not empty_or_missing(workspace_here):
            raise ValueError(f"the archive holds its workspace, and {printable(workspace_here)} here has files "
                             "already: put it elsewhere (--map "
                             f"{printable(workspace)}=/NEW/FOLDER), or start on the folder that is there "
                             "(--no-workspace)")
        approvals = parts.get("approvals") if isinstance(parts.get("approvals"), dict) else {}
        offered = approval_words(approvals)
        granted = []
        asked = asked_options(s, options, maps, workspace, cwd, with_files)
        result = {"name": name, "agent": agent, "options": options_here, "agent_args": agent_args, "cwd": cwd_here,
                  "workspace": workspace_here, "from": {"name": manifest.get("name"), "sandbox_ai":
                                                        manifest.get("sandbox_ai"), "created": manifest.get("created")},
                  "include_secrets": bool(manifest.get("include_secrets")), "approvals": offered,
                  "not_carried": [printable(t) for t in approvals.get("not_carried") or []][:50], "granted": [],
                  "asked": asked, "warnings": warnings, "checked": False, "imported": False,
                  "workspace_files": bool(with_files), "replaces": existing is not None}
        if offered and grant and not check:
            if not yes:
                said = ask_yes("Grant these network approvals again, as options of this session? "
                               f"{' '.join(offered)} [y/N] ", ask)
                if said is None:
                    raise ValueError("--grant-approvals: confirm the network approvals with --yes (nothing here can "
                                     f"ask): {' '.join(offered)}")
                grant = said
            if grant:
                granted = [token for word in offered for token in word.split(" ", 1)]
        result["granted"] = list(granted)
        if workspace_here and not any(v in ("--workspace", "-w") or v.startswith("--workspace=") for v in options_here):
            # (a start of the folder it was in: its workspace named, so that the folder it starts in decides nothing)
            options_here = ["--no-config", "--workspace", workspace_here, *options_here[1:]]
        options_here = [*options_here, *granted]
        result["options"] = options_here
        # What the archive chose of this machine is the human's to confirm before anything is written outside the
        # private staging folder (its workspace's files too).
        if asked and not check and not yes:
            said = ask_yes("Its configuration names this of this machine:\n  " + "\n  ".join(asked)
                           + "\nImport it with these options? [y/N] ", ask)
            if said is None:
                raise ValueError("its configuration gives the sandbox secrets or folders of this machine (listed); "
                                 "confirm with --yes, or check it first (--check):\n  " + "\n  ".join(asked))
            if not said:
                raise ValueError("not imported: its configuration was not confirmed")
        # Made here, checked, and only then kept: its folder (no session.json yet: no listing shows it), its HOME,
        # its workspace's files (not at a check); the launcher's dry run.
        with s.names_lock():
            key = s.new_key(name)
            directory = s.session_dir(key)
            directory.mkdir(mode=0o700)
            made_key = key
        if (top / "home").is_dir():
            os.rename(top / "home", directory / "home")
        else:
            (directory / "home").mkdir(mode=0o700)
        os.chmod(directory / "home", 0o700)
        if with_files and not check:
            made_workspace = workspace_here
            place_tree(top / "workspace", workspace_here)
        if not cwd_here or not os.path.isdir(cwd_here):
            cwd_here = workspace_here
        if not cwd_here or not os.path.isdir(cwd_here):
            if not (check and with_files):
                raise ValueError(f"its workspace {printable(workspace_here)} is not on this machine: say where it lies "
                                 "here (--map LOCAL=REMOTE), or take its files along (export --with-workspace)")
        result["cwd"] = cwd_here
        data = {"agent": agent, "options": options_here, "agent_args": agent_args, "backend": None,
                "podman_url": None, "cwd": cwd_here, "created": s.now(), "workspace": workspace_here,
                "ready": True, "name": name, "project": project_label(parts), "keep": True, "mode": "tmux",
                "imported": {"from": printable(manifest.get("name"))[:80], "at": s.now(),
                             "sandbox_ai": printable(manifest.get("sandbox_ai"))[:40]}}
        # (a check of an archive with its workspace's files: the folder is not made yet - the dry run starts in the
        # session's own folder, its workspace named by its options)
        s.check_start(key, data if os.path.isdir(str(cwd_here)) else {**data, "cwd": str(directory)})
        result["checked"] = True
        if check:
            return result
        memory = None
        if "--no-memory" not in options_here and workspace_here:
            memory = import_memory(top / "memory", workspace_here, agent)
        result["memory"] = memory
        folders = {os.path.normpath(p) for p in [workspace_here, *s.option_paths(options_here)] if p}
        result["checkpoints"] = import_checkpoints(top / "checkpoints", workspace_here, lambda p: s.mapped(p, maps),
                                                   key, name, agent, folders) if workspace_here else None
        with s.names_lock():
            again = None
            with contextlib.suppress(ValueError):
                again = s.resolve(name)
            if again is not None and again != existing:
                raise ValueError(f"the name {printable(name)} was taken meanwhile; choose another (--as NAME)")
            if existing is not None:
                s.delete(existing, yes=True)
            s.save(key, data)
        made_key = None  # kept
        s.starts().record(name, "import", key, None, agent=agent, workspace=workspace_here)
        result.update(id=key, imported=True, home=str(directory / "home"))
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if made_key is not None:
            shutil.rmtree(s.session_dir(made_key), ignore_errors=True)
            if made_workspace is not None:
                shutil.rmtree(made_workspace, ignore_errors=True)


def project_label(parts):
    """The project's label of the archive's start plan (shown by list), when it is one: else None (the launcher's)."""
    label = (parts.get("start") or {}).get("project") if isinstance(parts.get("start"), dict) else None
    return label if isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", label) else None


def import_lines(result):
    shown = printable(result["name"])
    origin = result.get("from") or {}
    lines = [f"{'checked' if not result.get('imported') else 'imported'}: {shown} ({printable(result['agent'])}), "
             f"from {printable(origin.get('name'))} (sandbox-ai {printable(origin.get('sandbox_ai'))}, "
             f"{printable(origin.get('created'))})",
             f"  starts with: {printable(shlex.join(result['options']))}",
             f"  in: {printable(result['cwd'])}" + (f" (workspace {printable(result['workspace'])}"
                                                     + (", its files from the archive)" if result.get(
                                                         "workspace_files") else ")") if result.get("workspace")
                                                    else "")]
    if result.get("include_secrets"):
        lines.append("  warning: the archive holds logins and keys (exported with --include-secrets)")
    for item in result.get("asked") or []:
        lines.append(f"  warning: {printable(item)}")
    for item in result.get("warnings") or []:
        lines.append(f"  note: {printable(item)}")
    if result.get("approvals"):
        if result.get("granted"):
            lines.append(f"  network approvals granted again (options of this session): "
                         f"{' '.join(result['approvals'])}")
        else:
            lines.append(f"  network approvals not granted (--grant-approvals grants them, asked): "
                         f"{' '.join(result['approvals'])}")
    for item in result.get("not_carried") or []:
        lines.append(f"  not carried: {printable(item)}")
    memory = result.get("memory")
    if memory:
        lines.append(f"  memory: {memory['added']} files added to the project's memory here"
                     + (f"; kept as they are here (the project's own): {', '.join(map(printable, memory['kept_here']))}"
                        if memory["kept_here"] else ""))
    checkpoints = result.get("checkpoints")
    if checkpoints:
        lines.append(f"  checkpoints: {len(checkpoints['ids'])} (sandbox-ai checkpoint list {shown})")
    if result.get("imported"):
        lines.append(f"continue it (the agent resumes its conversation): sandbox-ai resume {shown}")
    else:
        lines.append("it can start here (checked: nothing was kept)")
    return lines


# --- a duplicate here, a copy or move over SSH ----------------------------------------------------------------------

def transfer_dir(s):
    folder = Path(staging_root(s)) / f".transfer-{secrets.token_hex(6)}"
    folder.mkdir(mode=0o700)
    return folder


def confirm_secrets(include_secrets, yes, ask=None):
    if not include_secrets or yes:
        return
    said = ask_yes("Take the logins, keys and secrets of its HOME along? Whoever has them can log in as you. [y/N] ",
                   ask)
    if said is None:
        raise ValueError("--include-secrets takes logins and keys along: confirm it with --yes (nothing here can ask)")
    if not said:
        raise ValueError("not done: --include-secrets was not confirmed")


def confirm_approvals(offered, grant, yes, ask=None):
    """Whether the network approvals OFFERED are granted at the other end: only with GRANT, confirmed here (YES, or
    a y at the terminal) after they were shown."""
    if not offered or not grant:
        return False
    if yes:
        return True
    said = ask_yes(f"Grant these network approvals again at the other end? {' '.join(offered)} [y/N] ", ask)
    if said is None:
        raise ValueError(f"--grant-approvals: confirm them with --yes (nothing here can ask): {' '.join(offered)}")
    return said


def default_workspace_map(s, key, new_name, maps, with_workspace):
    """MAPS, with the workspace mapped to WORKSPACE-NEW_NAME beside it when its files are taken along and no map
    names it (a duplicate's own copy)."""
    if not with_workspace:
        return list(maps)
    workspace = s.load(key).get("workspace")
    if not isinstance(workspace, str) or s.mapped(workspace, s.parse_maps(maps)) != workspace:
        return list(maps)
    return [*maps, f"{workspace.rstrip('/')}={workspace.rstrip('/')}-{new_name}"]


def duplicate(s, name, new_name, maps=(), with_workspace=False, include_secrets=False, checkpoints=True, grant=False,
              yes=False, ask=None, out=print):
    """`sandbox-ai session duplicate NAME NEW`: a second session of this machine from the state of NAME (a running one
    paused for the moment): its own HOME and name, its workspace shared - or, WITH_WORKSPACE, a copy of it
    (beside it, WORKSPACE-NEW, unless --map says where). Not started: sandbox-ai resume NEW. The import's result."""
    key = s.resolve(name)
    if not re.fullmatch(s.SESSION_NAME, str(new_name)):
        raise ValueError(f"not a session name: {printable(new_name)}")
    confirm_secrets(include_secrets, yes, ask)
    maps = default_workspace_map(s, key, new_name, maps, with_workspace)
    folder = transfer_dir(s)
    try:
        archive = folder / "state.tar.gz"
        exported = export_state(s, key, str(archive), include_secrets, with_workspace, checkpoints)
        offered = approval_words((exported.get("parts") or {}).get("approvals") or {})
        granted = confirm_approvals(offered, grant, yes, ask)
        return import_archive(s, archive, new_name, maps, grant=granted, yes=True, ask=ask, out=out)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def remote_text(stdout, stderr, limit=12):
    return [printable(line) for line in (stderr or stdout or "").strip().splitlines()[-limit:]]


def older(stdout, stderr):
    said = " ".join(remote_text(stdout, stderr, 40))
    return "invalid choice: 'import'" in said or "unrecognized arguments" in said


def unreachable(out, command, there, status, stdout, stderr):
    """What a copy or move says when HOST was not reached (STATUS 124, 127, 255) or has a sandbox-ai that takes no
    session's state; else what it said."""
    if status in (124, 127, 255):
        out(f"sandbox-ai session {command}: {there} " + ("did not answer in time" if status == 124 else
                                                          f"was not reached over SSH (exit status {status})")
            + ": nothing was copied")
        for line in remote_text(stdout, stderr, 6):
            out(f"  {line}")
        out(f"  check the connection: sandbox-ai hosts test {there}")
    elif older(stdout, stderr):
        out(f"sandbox-ai session {command}: the sandbox-ai on {there} is older and cannot take a session's state: "
            f"update it (sandbox-ai hosts update {there}), or copy only its configuration (--config-only)")
    else:
        out(f"sandbox-ai session {command}: {there} gave no answer to `sandbox-ai session import --help` (exit "
            f"{status}): nothing was copied")
        for line in remote_text(stdout, stderr, 6):
            out(f"  {line}")


def transfer(s, name, host, new_name=None, maps=(), move=False, check=False, replace=False, include_secrets=False,
             with_workspace=False, checkpoints=True, grant=False, yes=False, run=None, run_file=None, ask=None,
             out=print):
    """`sandbox-ai session copy|move NAME HOST` with its state: snapshot (a running session paused), the archive sent
    as the standard input of `session import -` on HOST (checked there, a dry run there first), then `session resume`
    there; a copy thaws it here right after the snapshot, a move stops it here once it runs there (a failure there
    thaws it again). CHECK: only `session import --check` there. The exit status."""
    hosts = s.hosts()
    run = run or hosts.run
    run_file = run_file or hosts.run_with_file
    key = s.resolve(name)
    data = s.load(key)
    shown, there = printable(s.display(key, data)), printable(host)
    target = new_name or s.display(key, data)
    command = "move" if move else "copy"
    if not re.fullmatch(s.SESSION_NAME, str(target)):
        raise ValueError(f"not a session name: {printable(target)}")
    plan_problems = s.transfer_plan(key)[3]
    for problem in plan_problems:
        out(f"sandbox-ai session {command}: {problem}")
    if plan_problems:
        return 1
    confirm_secrets(include_secrets, yes, ask)
    # Asked first, before a snapshot is taken: whether the host answers, and takes a session's state.
    status, stdout, stderr = run(host, ["session", "import", "--help"], timeout=60)
    if status in (124, 127, 255) or status != 0:
        unreachable(out, command, there, status, stdout, stderr)
        return 1
    folder = transfer_dir(s)
    held = {}
    moved = False
    try:
        archive = folder / "state.tar.gz"
        exported = export_state(s, key, str(archive), include_secrets, with_workspace, checkpoints,
                                keep_paused=move and not check, held=held)
        offered = approval_words((exported.get("parts") or {}).get("approvals") or {})
        granted = confirm_approvals(offered, grant, yes, ask) if not check else False
        out(f"sandbox-ai session {command}: the state of {shown}: {exported['files']} files "
            f"({human(exported['bytes'])})" + (", paused for the snapshot" if held.get("paused") else "")
            + (f"; logins and keys left out: {len(exported['secrets_left_out'])}" if exported["secrets_left_out"]
               else ""))
        if offered and not granted:
            out(f"sandbox-ai session {command}: network approvals not granted there (--grant-approvals, asked): "
                f"{' '.join(offered)}")
        args = ["session", "import", "-", "--as", target, "--json", "--yes",
                *[f"--map={value}" for value in maps], *(["--check"] if check else []),
                *(["--replace"] if replace else []), *(["--grant-approvals"] if granted else [])]
        status, stdout, stderr = run_file(host, args, str(archive), timeout=7200)
        if status in (124, 127, 255) or status != 0 and older(stdout, stderr):
            unreachable(out, command, there, status, stdout, stderr)
            return 1
        if status != 0:
            out(f"sandbox-ai session {command}: {shown} cannot be imported on {there}:")
            for line in remote_text(stdout, stderr):
                out(f"  {line}")
            out("  (folders of this machine that lie elsewhere there: --map LOCAL=REMOTE)")
            return 1
        try:
            result = json.loads(stdout)
        except (ValueError, RecursionError):
            result = {}
        for item in (result.get("asked") or []) + (result.get("warnings") or []):
            out(f"sandbox-ai session {command}: warning: {printable(item)}")
        if check:
            out(f"sandbox-ai session {command}: {shown} can start on {there} with its state (checked, nothing kept "
                "there)")
            return 0
        status, stdout, stderr = run(host, ["session", "resume", target], timeout=900)
        if status != 0:
            out(f"sandbox-ai session {command}: {shown} was imported on {there} as {printable(target)}, but its start "
                f"there failed (exit {status}):")
            for line in remote_text(stdout, stderr):
                out(f"  {line}")
            out(f"  start it there: sandbox-ai hosts run {there} -- resume {printable(target)}"
                + (f"; {shown} stays here as it was" if move else ""))
            return status or 1
        out(f"sandbox-ai session {command}: {shown} runs on {there} as {printable(target)}, its conversation "
            f"continued (attach: sandbox-ai hosts run {there} -- session attach {printable(target)})")
        if move:
            moved = True
            if held.get("state") in ("running", "paused", "orphaned"):
                s.stop(key)
            out(f"sandbox-ai session {command}: {shown} stopped here; its HOME stays until: sandbox-ai session delete "
                f"{shown}")
        return 0
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if move and held.get("paused") and not moved:
            thaw(s, key)


# --- the command line ----------------------------------------------------------------------------------------------

def add_state_options(sub, export=False):
    sub.add_argument("--with-workspace", action="store_true",
                     help="take the workspace's files along too (default: the workspace is mapped: the folder of that "
                          "path there, or where --map says)")
    sub.add_argument("--no-checkpoints", action="store_true", help="leave the workspace's checkpoints out")
    if not export:
        sub.add_argument("--include-secrets", action="store_true",
                         help="take logins, keys and files with a placeholder of a secret along (asked first; whoever "
                              "has them can log in as you)")
        sub.add_argument("--grant-approvals", action="store_true",
                         help="grant its network approvals again at the other end, as options of that session (shown "
                              "and asked first)")
        sub.add_argument("--yes", action="store_true", help="confirm what is asked without asking (for scripts)")


def add_parsers(commands):
    """The subcommands import and duplicate of `sandbox-ai session` (the state options of copy, move and export are
    added where those are defined)."""
    imported = commands.add_parser(
        "import", help="A session's archive (session export) as a kept session here, to resume",
        description="Take a session's archive (sandbox-ai session export) as a stopped session of this machine that "
                    "sandbox-ai resume continues - its configuration, HOME, the agent's session files, memory, "
                    "checkpoints (and its workspace's files, if it holds them). The archive is untrusted: every member "
                    "is checked (only folders and regular files below its folder, sizes, the manifest's checksums), "
                    "its configuration too (the launcher's dry run first). Options that give the sandbox secrets or "
                    "folders of this machine are asked first; its network approvals are granted again only with "
                    "--grant-approvals (asked).")
    imported.add_argument("file", metavar="FILE", help="the archive (a .tar.gz of session export), or - for the "
                                                       "standard input")
    imported.add_argument("--as", dest="new_name", metavar="NAME", help="its name here (default: its name there)")
    imported.add_argument("--map", action="append", default=[], metavar="LOCAL=REMOTE",
                          help="a folder of the machine it was exported on (LOCAL) and where it lies here (REMOTE); "
                               "repeatable")
    imported.add_argument("--no-workspace", action="store_true",
                          help="leave the workspace's files of the archive out: it starts on the folder that is here")
    imported.add_argument("--check", action="store_true", help="only check it (the launcher's dry run): nothing is "
                                                               "kept")
    imported.add_argument("--replace", action="store_true",
                          help="a stopped session of that name here is deleted first (its HOME too)")
    imported.add_argument("--grant-approvals", action="store_true",
                          help="grant its network approvals again, as options of this session (shown and asked first)")
    imported.add_argument("--yes", action="store_true", help="confirm what is asked without asking (for scripts)")
    imported.add_argument("--json", action="store_true", help="what was checked and kept, as JSON")
    duplicated = commands.add_parser(
        "duplicate", help="A second session here from a session's state (its own HOME and name)",
        description="Make a second session of this machine from the state of NAME: its configuration, a copy of its "
                    "HOME with the agent's session files (the copy continues the same conversation), its memory and "
                    "checkpoints. Both can run. The workspace is shared, or copied with --with-workspace (beside it "
                    "as WORKSPACE-NEW, unless --map says where). A running session is paused for the moment of the "
                    "snapshot. Not started: sandbox-ai resume NEW.")
    duplicated.add_argument("name", help="a session (sandbox-ai list)")
    duplicated.add_argument("new", metavar="NEW", help="the new session's name")
    duplicated.add_argument("--map", action="append", default=[], metavar="LOCAL=REMOTE",
                            help="with --with-workspace: where the workspace's copy goes (WORKSPACE=NEW_FOLDER)")
    add_state_options(duplicated)
    duplicated.add_argument("--json", action="store_true", help="what was made, as JSON")


def run_import(s, a, out=print, ask=None):
    source = sys.stdin.buffer if a.file == "-" else os.path.abspath(os.path.expanduser(a.file))
    if source is not sys.stdin.buffer and not os.path.isfile(source):
        raise ValueError(f"no archive {printable(a.file)}")
    result = import_archive(s, source, a.new_name, a.map, check=a.check, replace=a.replace, grant=a.grant_approvals,
                            yes=a.yes, no_workspace=a.no_workspace, ask=ask, out=out)
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in import_lines(result):
            out(line)
    return 0


def run_duplicate(s, a, out=print, ask=None):
    result = duplicate(s, a.name, a.new, a.map, a.with_workspace, a.include_secrets, not a.no_checkpoints,
                       a.grant_approvals, a.yes, ask=ask, out=out)
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in import_lines(result):
            out(line)
    return 0


def run_export(s, a, out=print, ask=None):
    """`session export` (session_inspect's parser): with the state's parts, a running session paused for it."""
    include = getattr(a, "include_secrets", False)
    if include:
        si().confirm_secrets(a, ask)
    if not si().local_host(a.host):
        if not a.out.startswith("/"):
            raise ValueError("with --host OUT is a path of that host: give it in full (/...)")
        result = si().remote(a.host, ["export", a.name, *(["--include-secrets", "--yes"] if include else []),
                                      *(["--with-workspace"] if a.with_workspace else []),
                                      *(["--no-checkpoints"] if a.no_checkpoints else []), "--", a.out], timeout=3600)
    else:
        result = export_state(s, a.name, a.out, include, a.with_workspace, not a.no_checkpoints)
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in export_lines(result):
            out(line)
    return 0
