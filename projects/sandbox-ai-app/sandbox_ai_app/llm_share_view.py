"""An LLM server for all hosts (sandbox-ai llm share): what the LLM servers page shows of it and its dialog.

`sandbox-ai llm list --json` says per server "shared" (None, or {"key_to": [HOST], "hosts": [{host, copied, in_sync,
key, keys_missing, reachable, reach_error, error, offline, ...}]}) and, for a copy another machine shares,
"shared_from". The page shows it in its column "For all hosts" (share_cell, share_color, share_tooltip); the dialog
For all hosts… (ShareDialog: the server's sharing properties) has the flag, one key tick per host and each host's
state, and gives the command that makes it so (share_argv: `llm share NAME --key-to HOST,...|--no-key`, or
unshare_argv). A key goes to a host only where it is ticked - asked first (llm-share-key) -, and unsharing, which
takes the copies away from the hosts, is asked first too (llm-unshare)."""
from __future__ import annotations

import re

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QVBoxLayout, QWidget

from .files import printable
from .i18n import tr
from .widgets import explanation, plain_label, safe_tooltip

HOST = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # a host of sandbox-ai hosts (hosts.NAME)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")  # an LLM server (llm.NAME)


def shared_of(entry):
    """ENTRY's "shared" when it is shared ({"key_to", "hosts"}, each checked), else None."""
    shared = entry.get("shared") if isinstance(entry, dict) else None
    if not isinstance(shared, dict):
        return None
    hosts = [h for h in shared.get("hosts") or [] if isinstance(h, dict) and isinstance(h.get("host"), str)]
    key_to = [h for h in shared.get("key_to") or [] if isinstance(h, str)]
    return {"key_to": key_to, "hosts": hosts}


def origin_of(entry):
    """The machine a copy is managed from (llm share there), or None for a server of this host's own."""
    origin = entry.get("shared_from") if isinstance(entry, dict) else None
    return origin if isinstance(origin, str) and origin else None


def key_names(entry):
    """The secrets ENTRY names (its API key's, its header secrets'): its key, as llm share sends it."""
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    names = [entry.get("api_key_secret"), *(value for _, value in sorted(headers.items()))]
    return [name for name in dict.fromkeys(n for n in names if isinstance(n, str) and n)]


def share_cell(entry):
    """The column For all hosts: empty, "from HOST" for a copy, or how many hosts have it as it is."""
    origin = origin_of(entry)
    if origin:
        return tr("from {host}", host=printable(origin)[:64])
    shared = shared_of(entry)
    if shared is None:
        return ""
    if not shared["hosts"]:
        return tr("yes (no host yet)")
    synced = sum(1 for h in shared["hosts"] if h.get("in_sync") and h.get("reachable") is not False)
    return tr("yes: {synced} of {count} hosts ready", synced=synced, count=len(shared["hosts"]))


def share_color(entry):
    """"warn" for a shared server a host does not have as it is, or does not reach; else None."""
    shared = shared_of(entry)
    if shared is None:
        return None
    return "warn" if any(not h.get("in_sync") or h.get("reachable") is False or h.get("key") in ("missing", "locked")
                         and h.get("key_allowed") for h in shared["hosts"]) else None


def host_text(found, names=()):
    """One host's state of a shared server, in words."""
    if not found.get("copied"):
        error = printable(found.get("error") or "")[:300]
        return tr("not copied yet: {error}", error=error) if error else tr("not copied yet")
    parts = [tr("copied, in sync") if found.get("in_sync") else
             tr("copied, not in sync: {error}", error=printable(found.get("error") or "")[:300])
             if found.get("error") else tr("copied, not in sync")]
    key = found.get("key")
    secrets = ", ".join(printable(n) for n in names)
    parts.append({"none": tr("no key needed"), "sent": tr("key {names} sent", names=secrets),
                  "there": tr("key {names} there (its own)", names=secrets),
                  "missing": tr("key missing there"), "locked": tr("its secret store is locked"),
                  }.get(key, tr("key not asked yet")))
    if found.get("reachable") is True:
        parts.append(tr("reachable from there"))
    elif found.get("reachable") is False:
        error = printable(found.get("reach_error") or "")[:300]
        parts.append(tr("NOT reachable from there: {error}", error=error) if error else tr("NOT reachable from there"))
    return ", ".join(parts)


def share_tooltip(entry):
    """Each host's state, one line each (the column's tooltip)."""
    origin = origin_of(entry)
    if origin:
        text = tr("Managed from {host} (shared from there): a change here is replaced by its next change there; to "
                  "keep one of your own, add it under another name.", host=printable(origin)[:64])
        if entry.get("key_here") == "missing":
            text += "\n" + tr("Its key is not kept on this host: keep it as the secret {name} here (Secrets), or "
                              "tick this host for the key on {host}.", name=", ".join(key_names(entry)),
                              host=printable(origin)[:64])
        elif entry.get("key_here") == "locked":
            text += "\n" + tr("Its key: this host's secret store is locked.")
        return text
    shared = shared_of(entry)
    if shared is None:
        return tr("Not shared: only this host has it. For all hosts… copies it to every host of sandbox-ai hosts.")
    names = key_names(entry)
    return "\n".join([tr("Key to: {hosts}", hosts=", ".join(shared["key_to"]) or tr("no host"))] +
                     [f"{printable(h['host'])}: {host_text(h, names)}" for h in shared["hosts"]])


def share_argv(name, key_to):
    """`sandbox-ai llm share NAME --key-to HOST,...` (KEY_TO empty: --no-key); ValueError for a name or a host that
    could not be passed on."""
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        raise ValueError(tr("Refused: {name} is not a server's name that can be passed on.", name=printable(name)))
    hosts = sorted(set(key_to))
    if any(not (isinstance(h, str) and HOST.fullmatch(h)) or h == "local" for h in hosts):
        raise ValueError(tr("Refused: a host's name that cannot be passed on."))
    return ["llm", "share", name, "--key-to", ",".join(hosts)] if hosts else ["llm", "share", name, "--no-key"]


def unshare_argv(name):
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        raise ValueError(tr("Refused: {name} is not a server's name that can be passed on.", name=printable(name)))
    return ["llm", "unshare", name]


class ShareDialog(QDialog):
    """The sharing properties of the server ENTRY on HOST_LABEL: For all hosts (the flag), one key tick per host of
    HOSTS (the hosts of that host's sandbox-ai hosts: what llm list --json named, else the app's own list for this
    machine) and each host's state. wanted() gives (shared, [hosts the key goes to])."""

    def __init__(self, parent, entry, host_label, hosts):
        super().__init__(parent)
        self.entry = entry
        name = printable(entry.get("name") or "")
        self.setWindowTitle(tr("For all hosts: {name} on {host}", name=name, host=host_label))
        layout = QVBoxLayout(self)
        shared = shared_of(entry)
        self.names = key_names(entry)
        layout.addWidget(explanation(tr(
            "For all hosts copies this server's entry to every host of sandbox-ai hosts and keeps it in sync: a "
            "session there reaches the server directly, and each host checks that it can. Nothing secret is copied; "
            "its key goes only to the hosts ticked, into their own secret stores.")))
        self.flag = QCheckBox(tr("For all hosts"))
        self.flag.setAccessibleName(tr("For all hosts"))
        self.flag.setToolTip(tr("sandbox-ai llm share: the entry on every host of sandbox-ai hosts (a host added later "
                                "too); off: llm unshare takes the copies and the keys sent away again"))
        self.flag.setChecked(shared is not None)
        layout.addWidget(self.flag)
        grid = QGridLayout()
        self.ticks, self.states = {}, {}
        states = {h["host"]: h for h in (shared or {}).get("hosts", [])}
        key_to = set((shared or {}).get("key_to", []))
        for row, host in enumerate(hosts):
            label = QLabel(printable(host))
            tick = QCheckBox(tr("its key"))
            tick.setAccessibleName(tr("Send the key to {host}", host=printable(host)))
            tick.setToolTip(safe_tooltip(tr("The key ({names}) kept in the secret store of {host} as well (over SSH, "
                                            "never on a command line); a host that keeps a secret of that name "
                                            "already uses its own", names=", ".join(self.names),
                                            host=printable(host)) if self.names else tr("This server needs no key.")))
            tick.setChecked(host in key_to)
            state = plain_label(host_text(states[host], self.names) if host in states else
                                tr("gets the copy when it is shared") if shared is None else tr("not copied yet"),
                                role="secondary", selectable=True)
            state.setAccessibleName(tr("State on {host}", host=printable(host)))
            grid.addWidget(label, row, 0)
            grid.addWidget(tick, row, 1)
            grid.addWidget(state, row, 2)
            self.ticks[host], self.states[host] = tick, state
        box = QWidget()
        box.setLayout(grid)
        layout.addWidget(box)
        if not hosts:
            layout.addWidget(plain_label(tr("No host in sandbox-ai hosts yet: a host added later gets the copy (and "
                                            "its key once it is ticked here)."), role="secondary"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.flag.toggled.connect(lambda *_: self.update_ticks())
        self.update_ticks()

    def update_ticks(self):
        for tick in self.ticks.values():
            tick.setEnabled(self.flag.isChecked() and bool(self.names))

    def wanted(self):
        """(shared, [the hosts ticked for the key]) as chosen."""
        shared = self.flag.isChecked()
        return shared, sorted(h for h, tick in self.ticks.items() if shared and self.names and tick.isChecked())
