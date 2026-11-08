"""What changed in the HOME of a kept start since its last start, said before it is resumed (no Qt): the app asks
`sandbox-ai resume NAME --review-home --json` (on this machine or a host) and shows what it found in a confirmation.
Whatever wrote there - the agent too - runs at the next start (shell, git, ssh, the agent's settings, hooks...)."""
from __future__ import annotations

from .files import printable
from .i18n import N_, tr

CHANGES = {"added": N_("added"), "removed": N_("removed"), "changed": N_("changed"), "mode": N_("permissions"),
           "type": N_("type")}
REVIEW_LIMIT = 20  # changed files a confirmation names (the rest: counted; every difference is in its details)


def home_review(name, data, host="local"):
    """(text, details) of a confirmation before the kept start NAME (on HOST) is resumed, from `sandbox-ai resume NAME
    --review-home --json` (DATA), or None when nothing is to be said: nothing changed in the start files of its HOME
    and all of them were checked, or there is no record of it yet. Everything is escaped (the agent wrote the HOME)."""
    review = data.get("review") if isinstance(data, dict) else None
    if not isinstance(review, dict):
        return None
    changes = [c for c in review.get("changes") or [] if isinstance(c, dict)]
    complete = review.get("complete") is not False
    if not changes and complete:
        return None
    shown_name = printable(name)
    lines = []
    if changes:
        lines.append(tr("The HOME of {name} changed since its last start ({since}) in files that decide what runs at a "
                        "start (shell, git, ssh, the agent's settings, hooks, plugins, skills):", name=shown_name,
                        since=printable(review.get("since") or "?")))
        for change in changes[:REVIEW_LIMIT]:
            kind = change.get("change")
            lines.append(f"  {tr(CHANGES[kind]) if kind in CHANGES else printable(kind)}  {printable(change.get('path'))}")
        if len(changes) > REVIEW_LIMIT:
            lines.append("  " + tr("… and {count} more", count=len(changes) - REVIEW_LIMIT))
    if not complete:
        lines.append(tr("Its HOME has more files than were checked: not every change is named."))
    command = f"sandbox-ai resume {shown_name} --review-home"
    lines += ["", tr("If you did not make these changes, check them before it starts again ({command}).",
                     command=command if host == "local" else f"sandbox-ai hosts run {printable(host)} -- {command[11:]}")]
    details = []
    for change in changes:
        if change.get("diff") or change.get("note"):
            details.append(f"== {printable(change.get('change'))} {printable(change.get('path'))}"
                           + (f" ({printable(change['note'])})" if change.get("note") else ""))
            details += [printable(line) for line in str(change.get("diff") or "").splitlines()]
    return "\n".join(lines), "\n".join(details)[:200000] or None


def review_refusal(error):
    """True if sandbox-ai's ERROR says the start keeps no HOME (a run: nothing to review, nothing to ask)."""
    return "keeps no HOME" in str(error)
