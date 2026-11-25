"""A new project's folder on a host of `sandbox-ai hosts` (the Board's New project…): read, and made after asking,
over SFTP as the remote chooser reaches the host. Slow (ssh): only ever called in a thread (widgets.background);
a helper module of the app (tests/test_references.py HELPER_MODULES, BLOCKING_HELPERS)."""
from __future__ import annotations

import posixpath
import stat

from .files import Refused, printable
from .i18n import tr

def project_folder(sai, host, path, create=False):
    """The folder PATH (absolute) on HOST for a new project (the Board's New project…, todos.txt board 10), over SFTP
    as the chooser reaches it: "exists" (a folder there), "missing" (not there, the folder above it is), "created"
    (CREATE: made now - one folder, never its parents, never through a link: RemoteRoot walks with LSTAT). Refused
    (in words) for a path that is a file or a link, or whose folder above is not there. Nothing is made on this
    machine, and nothing at all without CREATE. Blocking: run it in the background."""
    from . import remote, remote_chooser as RC
    path = str(path or "").strip()
    if not path.startswith("/") or any(c in path for c in "\0\n\r"):
        raise Refused(tr("Not an absolute path: {path}", path=printable(path)))
    path = posixpath.normpath(path)
    parent, name = posixpath.split(path)
    if not name:
        raise Refused(tr("Choose a folder below /, not / itself."))
    root = remote.RemoteRoot(RC.host_target(sai, host), "/", ssh=RC.SSH, known_hosts=RC.KNOWN_HOSTS,
                             config=RC.SSH_CONFIG)
    try:
        try:
            info = root.stat_entry(path.lstrip("/"))
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISDIR(info.mode):
                return "exists"
            raise Refused(tr("{path} on {host} is no folder (a file, or a symbolic link: not followed).",
                             path=printable(path), host=printable(host)))
        try:
            root.walk(parent.lstrip("/"), last=True)
        except FileNotFoundError:
            raise Refused(tr("{path} is not on {host}: choose a folder that is there, or make it first (New "
                             "folder…).", path=printable(parent), host=printable(host))) from None
        if not create:
            return "missing"
        root.make_folder(parent.lstrip("/"), name)
        return "created"
    finally:
        root.close()

