"""The devices a host has, offered where a device is chosen (user 2026-11-19: "ob beim Hinzufügen von Geräten lokale
oder Remote-Geräte erkannt werden und im Dropdown angezeigt werden"): `sandbox-ai devices scan --json` of that host -
this machine's, or another host's over SSH, never this machine's for a host - read in the background and kept
CACHE_SECONDS, read again on Refresh.

- The Board's device field (BoardPage.value_provider("--device"), list_fields): the devices grouped by kind with
  readable labels ("FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0"), what the launcher refuses left out; a GPU
  kind is offered as GPU_PREFIX + KIND and becomes the part's --gpu. A path or usb:VENDOR:PRODUCT typed by hand goes
  as before.
- A running sandbox (the Board's live Devices part, Sandboxes & sessions' Add device): the same minus what the run
  has (`devices list RUN --json`); a GPU kind is shown and not offered: --gpu cannot change while it runs."""
from __future__ import annotations

import threading
import time

from . import backend, widgets
from .files import printable, printable_text
from .i18n import N_, tr

KINDS = backend.DEVICE_SCAN_KINDS
TITLES = {"usb": N_("USB devices"), "serial": N_("Serial ports"), "video": N_("Cameras and video"),
          "render": N_("Render nodes"), "kvm": N_("Virtual machines"), "sound": N_("Sound"), "gpu": N_("GPU (--gpu)"),
          "input": N_("Input devices")}
CACHE_SECONDS = 30
GPU_PREFIX = "--gpu "
LIVE_STATES = ("attached", "held", "gone")  # (devices.active: not "removed")
_cache, _lock = {}, threading.Lock()
_pending = {}  # (sai, host) -> [done] of a read in the background (GUI thread only)


def host_name(host):
    return tr("this computer") if host in (None, "", "local") else printable(host)


def scan(sai, host, refresh=False):
    """HOST's devices (SandboxAI.device_scan), from the cache when read in the last CACHE_SECONDS (not REFRESH).
    Blocking: in the background."""
    key = (id(sai), host)
    with _lock:
        hit = _cache.get(key)
    if hit is not None and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    entries = sai.device_scan(host)
    with _lock:
        _cache[key] = (time.monotonic(), entries)
    return entries


def cached(sai, host):
    """HOST's devices as read in the last CACHE_SECONDS, or None."""
    with _lock:
        hit = _cache.get((id(sai), host))
    return hit[1] if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS else None


def forget(host=None):
    """The cache of HOST (every host: None) dropped: the next read asks again (Refresh, a device plugged in)."""
    with _lock:
        for key in [key for key in _cache if host is None or key[1] == host]:
            _cache.pop(key, None)
    if host is None:
        _pending.clear()


def read(sai, host, done, owner=None, refresh=False):
    """done(entries, error) with HOST's devices: at once from the cache, else from a read in the background (one at a
    time per host: a field that asks again meanwhile gets the same answer)."""
    hit = None if refresh else cached(sai, host)
    if hit is not None:
        done(hit, None)
        return
    key = (id(sai), host)
    waiting = _pending.get(key)
    if waiting is not None and not refresh:
        waiting.append(done)
        return
    _pending[key] = (waiting or []) + [done]  # (a refresh meanwhile: read anew, the ones waiting answered too)

    def answered(entries, error):
        for callback in _pending.pop(key, None) or [done]:
            callback(entries, error)
    widgets.background(lambda: scan(sai, host, refresh), owner=owner, timeout=90, work=False, done=answered)


def gpu_value(kind):
    return GPU_PREFIX + kind


def gpu_of(value):
    """The GPU kind VALUE (GPU_PREFIX + KIND, the Board's device field) names, or None."""
    value = str(value or "")
    kind = value[len(GPU_PREFIX):] if value.startswith(GPU_PREFIX) else None
    return kind if kind in backend.GPU_KINDS else None


def check_board_value(value):
    """Why the Board's device field refuses VALUE, or None: a path under /dev, usb:VENDOR:PRODUCT[:SERIAL] or a GPU
    kind of the list."""
    if backend.valid_device(value) or gpu_of(value):
        return None
    return tr("A device is a path under /dev or usb:VENDOR:PRODUCT[:SERIAL]: {value}", value=printable(value))


def grouped(entries, offered):
    """[(entry, group title)] of ENTRIES that OFFERED(entry) wants, in the order of KINDS."""
    return [(entry, tr(TITLES[kind])) for kind in KINDS for entry in entries if entry["kind"] == kind and offered(entry)]


def board_items(entries):
    """(label, value, group) of ENTRIES for the Board's device field: what the launcher refuses left out, a GPU kind
    as GPU_PREFIX + KIND (it becomes --gpu)."""
    return [(entry["label"], gpu_value(entry["value"]) if entry["option"] == "--gpu" else entry["value"], group)
            for entry, group in grouped(entries, lambda entry: not entry["refused"])]


def problem(host, error):
    """The line said when HOST's devices could not be read (manual entry still goes)."""
    if isinstance(error, backend.DeviceScanMissing):
        return tr("sandbox-ai on {host} cannot list its devices yet (update it there): type a path or "
                  "usb:VENDOR:PRODUCT[:SERIAL].", host=host_name(host))
    return tr("The devices of {host} could not be read: {error}. Type a path or usb:VENDOR:PRODUCT[:SERIAL].",
              host=host_name(host), error=printable_text(str(error))[:300])


def note(host, entries, error=None, held_count=0):
    """One line below the field: what was found on HOST, what is not offered; nothing found or not read: so."""
    if error is not None:
        return problem(host, error)
    offered = [entry for entry in entries if not entry["refused"]]
    refused = len(entries) - len(offered)
    if not offered:
        return tr("No devices detected on {host}: type a path or usb:VENDOR:PRODUCT[:SERIAL].", host=host_name(host))
    text = tr("{count} detected on {host}", count=len(offered) - held_count, host=host_name(host))
    if held_count:
        text += "; " + tr("{count} the run has already", count=held_count)
    if refused:
        text += "; " + tr("{count} refused by sandbox-ai (input devices, keyboards ...), not offered", count=refused)
    return text


def held(listing):
    """(values, nodes) of the devices the run has now (SandboxAI.devices_listed): what is not offered again."""
    active = [entry for entry in listing or [] if entry.get("state") in LIVE_STATES]
    return ({entry.get("value") for entry in active if entry.get("value")},
            {node for entry in active for node in entry.get("nodes") or []})


def has(entry, values, nodes):
    """Whether the run has ENTRY already: its value, or one of its nodes (`devices add` would refuse it so)."""
    return entry["value"] in values or bool(set(entry["nodes"]) & nodes)


def live_items(entries, listing):
    """[(entry, group, why not)] of ENTRIES for a running sandbox: what the launcher refuses and what the run has
    already (LISTING) left out; a GPU kind listed with why it is not offered (--gpu cannot change while it runs)."""
    values, nodes = held(listing)
    found = []
    for entry, group in grouped(entries, lambda entry: not entry["refused"] and not has(entry, values, nodes)):
        why = tr("--gpu cannot change while it runs: set it on the part and start again") \
            if entry["option"] == "--gpu" else ""
        found.append((entry, group, why))
    return found


def held_count(entries, listing):
    values, nodes = held(listing)
    return sum(1 for entry in entries if not entry["refused"] and has(entry, values, nodes))


def listing_label(entry):
    """A device the run has, as the live part shows it: its value, what it is, its nodes and its state."""
    what = " ".join(filter(None, [entry.get("product") and f'"{entry["product"]}"',
                                  entry.get("usb") and f"usb:{entry['usb']}"]))
    nodes = ", ".join(entry.get("nodes") or [])
    text = entry.get("value") or "?"
    if what and what not in text:
        text += f" ({what})"
    if nodes and nodes != entry.get("value"):
        text += f" - {nodes}"
    return text


def state_label(entry):
    if entry.get("source") != "added":
        return tr("given at the start")
    return {"attached": tr("attached"), "held": tr("held: a question waits"), "gone": tr("unplugged"),
            "removed": tr("removed")}.get(entry.get("state"), printable(entry.get("state") or ""))
