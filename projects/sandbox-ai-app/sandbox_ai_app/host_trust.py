"""Review and trust a project file (.sandbox-ai.json) on this machine or on a host of `sandbox-ai hosts`, from the
Settings (trusted project configurations) and from the Start page with another host chosen.

The question is project_trust's, as for a file of this machine: every key that will be trusted, what weakens the
sandbox marked (against the defaults of the host the file is on), and the changes against the version the app saw
trusted LAST - never against the file in the folder, which whoever can write that folder (the agent too) wrote.
It is asked first (project-trust, Cancel the default), and exactly what was shown is trusted:

- this machine: read once with the launcher's bounded reader (backend.review_project), those bytes trusted
  (backend.trust_project);
- another host: its sandbox-ai reads it (`sandbox-ai wizard trust --show --json -- PATH` there) and trusts it only
  if it is still that version (`wizard trust --digest SHA256 -- PATH`). Both are 0.1.78 options: a host without
  them is told to update first (backend.require_options), nothing is sent.
"""
from __future__ import annotations

import os

from . import backend
from .files import printable
from .host_choice import host_label, on_host
from .i18n import N_, tr
from . import project_trust as PT
from .widgets import ask_first, background, inform, warn

LOCAL = "local"
TITLE = N_("Review the project configuration")


def intro(path, changed):
    state = (tr("{path} changed since you trusted it.", path=printable(path)) if changed else
             tr("{path} is not trusted yet.", path=printable(path)))
    return state + "\n\n" + tr("Trust exactly this content (see the details: the changes against the version you "
                               "trusted last, and the whole file)? The launcher refuses the file if it changes "
                               "afterwards.")


def review_and_trust(parent, sai, host, path, done=None, source=None):
    """Review the project file PATH (absolute: a folder's .sandbox-ai.json) on HOST and trust it after a yes. DONE(
    trusted, document) once it is decided: trusted True when the file is trusted now (as it was, or after the yes),
    DOCUMENT its settings (None when not read). SOURCE: the button(s) disabled meanwhile."""
    done = done or (lambda trusted, document: None)
    if host in (None, LOCAL):
        review_here(parent, sai, path, done, source)
        return
    label = host_label(host)

    def work():
        shown = sai.host_project_show(host, path)
        if shown.get("error") or shown.get("problem") or shown.get("trusted"):
            return shown, None, None
        return shown, sai.host_trust_defaults(host), sai.shown_baseline(shown)

    def reviewed(result, error):
        if error is not None:
            warn(parent, tr(TITLE), on_host(host, printable(str(error))))
            done(False, None)
            return
        shown, defaults, baseline = result
        if shown.get("error"):
            warn(parent, tr("No project configuration"), on_host(host, tr(
                "{path} cannot be read: {error}", path=printable(shown["path"]), error=printable(shown["error"]))))
            done(False, None)
            return
        if shown.get("problem"):
            warn(parent, tr("This project configuration cannot be used"), on_host(host, printable(shown["problem"])),
                 details=backend.shown_text(shown["document"]) if isinstance(shown.get("document"), dict)
                 else None)
            done(False, None)
            return
        document = shown.get("document")
        if not isinstance(document, dict):
            warn(parent, tr(TITLE), on_host(host, tr("{path} cannot be read: {error}", path=printable(shown["path"]),
                                                     error="no JSON object")))
            done(False, None)
            return
        if shown.get("trusted"):
            done(True, document)
            return
        question, details = PT.host_question(sai, shown, intro(shown["path"], bool(shown.get("trusted_digest"))),
                                             baseline, defaults)
        # Trusting outlives every start: each later start of that project there gets the file's options.
        if not ask_first(parent, "project-trust", tr(TITLE), on_host(host, question), tr("Trust"), details=details):
            done(False, document)
            return
        background(lambda: sai.host_project_trust(host, shown["path"], shown["digest"], document), owner=parent,
                   source=source, timeout=150,
                   done=lambda result, error: trusted(shown["path"], document, result, error))

    def trusted(where, document, result, error):
        status, out, err = result if error is None else (1, "", str(error))
        if status != 0:
            warn(parent, tr("Not trusted"), on_host(host, printable((err or out).strip()[-1500:]) or tr(
                "Failed (exit status {status}).", status=status)))
            done(False, document)
            return
        inform(parent, tr("Project configuration"), tr("Trusted on {host}: {path}", host=label,
                                                       path=printable(where)), modal=False)
        done(True, document)

    background(work, owner=parent, source=source, timeout=210, done=reviewed)


def review_here(parent, sai, path, done, source=None):
    """review_and_trust for a file of this machine: its bytes read once and trusted exactly (as the Start page's Open
    project folder does)."""
    directory = os.path.dirname(path)

    def reviewed(review, error):
        if isinstance(error, FileNotFoundError):
            warn(parent, tr("No project configuration"), tr("{path} has no .sandbox-ai.json.",
                                                            path=printable(directory)))
            done(False, None)
            return
        if error is not None:
            warn(parent, tr("No project configuration"), printable(str(error)))
            done(False, None)
            return
        if not review.usable:
            warn(parent, tr("This project configuration cannot be used"),
                 "\n".join(printable(problem) for problem in review.problems), details=review.text())
            done(False, None)
            return
        if review.trusted:
            done(True, review.document)
            return
        question, details = PT.question(sai, directory, review.document, intro(review.path, review.changed),
                                        new_text=review.data.decode("utf-8", "replace"))
        if not ask_first(parent, "project-trust", tr(TITLE), on_host(LOCAL, question), tr("Trust"), details=details):
            done(False, review.document)
            return
        try:
            sai.trust_project(review)
        except (OSError, ValueError) as exc:
            warn(parent, tr("Not trusted"), printable(exc))
            done(False, review.document)
            return
        inform(parent, tr("Project configuration"), tr("Trusted on {host}: {path}", host=host_label(LOCAL),
                                                       path=printable(review.path)), modal=False)
        done(True, review.document)

    background(sai.review_project, directory, owner=parent, source=source, timeout=90, done=reviewed)
