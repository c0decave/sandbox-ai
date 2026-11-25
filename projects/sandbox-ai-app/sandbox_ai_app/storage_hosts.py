"""The storage check of the configured hosts (user 2026-11-19): what the app does for this machine since 0.1.157 -
`sandbox-ai gc --check` in the background at its start and then as Settings › Storage says, the storage rules applied
there, each start's size recorded, a notice above the threshold - also for each host of `sandbox-ai hosts`, over
SSH, one host after the other; the notice names the host, the Hosts page shows each host's last result.

Per host one setting (Hosts page, "Check its storage in the background"; the app's settings.json "storage_hosts":
{host: true|false}, a host's own choice only once one is made): on by default only for the hosts the app uses - a
host on which the Sessions page found starts (running or kept), remembered (state.json "storage_hosts_used") so that
it holds after a restart. The schedule is the host's own storage_check (its defaults, as its last check said them:
off, weekly, daily), else this machine's; a host that did not answer is asked again after RETRY_SECONDS. What the
app keeps of a check (state.json "storage_hosts_checked"): its time, how much could be freed, the threshold, whether
it was above, the starts that grew, its notice or why it did not answer - numbers, names and printable words only.
No Qt."""
from __future__ import annotations

import time

from .files import printable, printable_text

SETTING = "storage_hosts"  # settings.json: {host: true|false}, the choices made on the Hosts page
USED = "storage_hosts_used"  # state.json: [host] where the app found starts
CHECKED = "storage_hosts_checked"  # state.json: {host: what the last check said}
EVERY = {"daily": 86400, "weekly": 7 * 86400, "off": None}
RETRY_SECONDS = 6 * 3600  # a host that did not answer: asked again after this long
KEPT_GREW = 5  # starts that grew, kept per host


def choices(settings):
    """{host: bool} of the choices made (SETTINGS: the app's settings), damaged values left out."""
    found = settings.get(SETTING) if isinstance(settings, dict) else None
    return {str(host): value for host, value in (found or {}).items() if isinstance(value, bool)} \
        if isinstance(found, dict) else {}


def is_on(host, used, settings):
    """Whether the app checks HOST's storage: its choice when one was made, else whether the app uses it (USED)."""
    chosen = choices(settings).get(host)
    return chosen if chosen is not None else host in set(used or ())


def interval(last, local_every):
    """Seconds between two checks of a host: its own storage_check as its LAST check said (settings), else
    LOCAL_EVERY's word (this machine's); None: off."""
    settings = (last or {}).get("settings") if isinstance(last, dict) else None
    word = settings.get("storage_check") if isinstance(settings, dict) else None
    return EVERY.get(word if word in EVERY else local_every if local_every in EVERY else "weekly", 7 * 86400)


def due(last, local_every, now=None):
    """Whether a host whose last check is LAST (None: never) is due now."""
    now = time.time() if now is None else now
    if not isinstance(last, dict) or not isinstance(last.get("time"), (int, float)):
        return interval(last, local_every) is not None
    if last.get("error"):
        return now - last["time"] >= RETRY_SECONDS
    every = interval(last, local_every)
    return every is not None and now - last["time"] >= every


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def kept(result, error=None, now=None):
    """What the app keeps of a host's check: RESULT (gc --check --json there) or ERROR (why it did not answer)."""
    now = time.time() if now is None else now
    if error is not None or not isinstance(result, dict):
        return {"time": now, "error": printable_text(error if error is not None else "no answer")[-300:]}
    settings = result.get("settings") if isinstance(result.get("settings"), dict) else {}
    grew = [{"name": printable(str(entry.get("name")))[:80], "bytes": number(entry.get("bytes")) or 0}
            for entry in result.get("grew") or [] if isinstance(entry, dict)][:KEPT_GREW]
    return {"time": now, "total": number(result.get("total")) or 0, "threshold": number(result.get("threshold")),
            "above": result.get("above") is True, "grew": grew, "said": len([e for e in result.get("said") or []
                                                                            if isinstance(e, dict)]),
            "settings": {"storage_check": settings.get("storage_check")} if settings.get("storage_check") in EVERY
            else {}}
