"""The doctor's results for a human: grouped by area, status icons in the theme's colours, the fix command copyable;
from the doctor module, from --json and from an older sandbox-ai's text output."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import doctor_view as D  # noqa: E402

RESULTS = [("ok", "Python", "3.12", ""), ("fail", "User namespaces", "user.max_user_namespaces is 0",
                                           "sudo sysctl user.max_user_namespaces=15000"),
           ("warn", "Image claude", "localhost/agent-claude missing", "sandbox-ai build claude"),
           ("fail", "Podman", "podman info failed", "check `podman info`; for a remote engine pass --podman-url"),
           ("warn", "Login codex", "~/.codex: no login", "sandbox-ai login codex (or log in on the host)"),
           ("fail", "Ordinary user", "running as root", "run sandbox-ai as your normal user, never with sudo"),
           ("ok", "Security mode", "medium", "")]


def test_the_fix_command_is_found_in_the_hint():
    assert D.fix_command("sandbox-ai build claude") == "sandbox-ai build claude"
    assert D.fix_command("sandbox-ai login codex (or log in on the host)") == "sandbox-ai login codex"
    assert D.fix_command("check `podman info`; for a remote engine pass --podman-url") == "podman info"
    assert D.fix_command("sudo modprobe tun") == "sudo modprobe tun"
    assert D.fix_command("run sandbox-ai as your normal user, never with sudo") == "", "advice, not a command"
    assert D.fix_command("install the package uidmap") == "" and D.fix_command("") == ""


def test_results_are_grouped_by_area_with_problems_first(qapp):
    view = D.DoctorView()
    view.set_results(RESULTS)
    groups = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
    assert [g.text(0).split("  ")[0] for g in groups] == ["System", "Engine (Podman, Bubblewrap, images)",
                                                          "Agents (logins, kits, plugins)", "Project and security mode"]
    system = groups[0]
    assert [system.child(i).text(0) for i in range(system.childCount())] == \
        ["✘  User namespaces", "✘  Ordinary user", "✔  Python"], "problems first"
    assert "2 problem(s), 0 warning(s)" in system.text(0) and "all fine" in groups[3].text(0)
    assert view.summary.text() == "3 problem(s), 2 warning(s): each says how to fix it."
    assert view.count() == len(RESULTS) and view.rowCount() == len(RESULTS)
    python = system.child(2)
    assert python.text(2) == "" and python.toolTip(0) == "Fine", "nothing to fix where all is well"
    view.only_problems.setChecked(True)
    assert all(item.text(0)[0] in "✘!" for item in view.items()) and len(view.items()) == 5
    view.deleteLater()


def test_the_fix_command_is_copied_never_run(qapp):
    view = D.DoctorView()
    view.set_results(RESULTS)
    copied = []
    view.copied.connect(copied.append)
    namespaces = next(item for item in view.items() if "User namespaces" in item.text(0))
    view.tree.setCurrentItem(namespaces)
    assert view.copy_button.isEnabled()
    assert view.copy_selected() == "sudo sysctl user.max_user_namespaces=15000"
    assert qapp.clipboard().text() == "sudo sysctl user.max_user_namespaces=15000" and copied == [copied[0]]
    root = next(item for item in view.items() if "Ordinary user" in item.text(0))
    view.tree.setCurrentItem(root)
    assert not view.copy_button.isEnabled(), "advice in words has no command"
    view.deleteLater()


def test_json_of_another_host_and_the_text_of_an_older_sandbox_ai(qapp):
    view = D.DoctorView()
    view.set_results([{"status": "warn", "check": "Image codex", "detail": "missing", "hint": "sandbox-ai build codex",
                       "group": "Engine"}, {"status": "weird", "check": "x\x1b[31m", "detail": "", "hint": ""}])
    assert [i.text(0) for i in view.items()] == ["!  Image codex", "–  x\\x1b[31m"], "unknown status: not checked"
    text = """
System
  ok    Python                 3.12
  FAIL  User namespaces        user.max_user_namespaces is 0
                               -> sudo sysctl user.max_user_namespaces=15000

Agents
  WARN  Login claude           ~/.claude: no login
                               -> sandbox-ai login claude (or log in on
                                  the host)

1 problem(s), 1 warning(s)
"""
    checks = D.parse_text(text)
    assert [(c["status"], c["check"]) for c in checks] == [("ok", "Python"), ("fail", "User namespaces"),
                                                           ("warn", "Login claude")]
    assert checks[2]["hint"] == "sandbox-ai login claude (or log in on the host)"
    view.set_text(text)
    assert view.count() == 3 and "1 problem(s), 1 warning(s)" in view.summary.text()
    view.set_results([])
    assert view.summary.text() == "The doctor found nothing to check."
    view.deleteLater()


def test_a_narrow_pane_stays_readable_and_an_unknown_answer_is_shown_as_it_came(qapp):
    """User report: the doctor of an added host was hard to read on the Hosts page - long titles took the room,
    the details were cut, everything was open. The check's column is capped, the others wrap, an area where all is
    fine is folded, the chosen check is shown whole below with its command; an answer that holds no check it can
    read (an older or unknown sandbox-ai) is shown as it came, never as "nothing to check"."""
    view = D.DoctorView()
    view.resize(560, 420)
    long = ("ok", "Delegated UID/GID ranges of a very long user name that goes on and on", "65536 ids", "")
    view.set_results(RESULTS + [long])
    view.show()
    qapp.processEvents()
    assert view.tree.columnWidth(0) <= D.CHECK_COLUMN and view.tree.wordWrap()
    groups = {view.tree.topLevelItem(i).text(0).split("  ")[0]: view.tree.topLevelItem(i)
              for i in range(view.tree.topLevelItemCount())}
    assert groups["System"].isExpanded() and groups["Engine (Podman, Bubblewrap, images)"].isExpanded()
    assert not groups["Project and security mode"].isExpanded(), "all fine: folded"
    namespaces = next(item for item in view.items() if "User namespaces" in item.text(0))
    view.tree.setCurrentItem(namespaces)
    text = view.detail.toPlainText()
    assert text.startswith("Problem: User namespaces") and "user.max_user_namespaces is 0" in text
    assert "Command (double-click the row or Copy copies it): sudo sysctl user.max_user_namespaces=15000" in text
    view.set_text("sandbox-ai doctor: everything is \x1b[32mfine\x1b[0m here\nno table at all")
    assert view.count() == 0 and "does not read" in view.summary.text()
    assert "\\x1b[32mfine" in view.detail.toPlainText() and "no table at all" in view.detail.toPlainText()
    view.set_text("")
    assert view.summary.text() == "The doctor found nothing to check."
    view.deleteLater()


INSTALLED = {"id": "sandbox-ai-version", "status": "ok", "check": "sandbox-ai", "group": "System", "hint": "",
             "detail": "0.1.80, system installation in /usr/local (/usr/local/lib/sandbox-ai/versions/0.1.80); Python 3.12.3",
             "version": "0.1.80", "kind": "system", "prefix": "/usr/local",
             "path": "/usr/local/lib/sandbox-ai/versions/0.1.80", "python": "3.12.3"}


def test_the_sandbox_ai_that_answered_heads_the_checks(qapp):
    """Above the checks: the doctor's first check (sandbox-ai's version, where it is installed, its Python); from an
    older doctor that only says "0.1.70 (PATH)", that; without it, the version the hosts list knows, said so; from the
    doctor module of this machine (tuples), its detail."""
    view = D.DoctorView()
    view.set_results([INSTALLED, {"status": "ok", "check": "Python", "detail": "3.12.3"}], host="ct1",
                     known={"version": "0.1.1"})
    assert view.version_line.text() == ("sandbox-ai 0.1.80 on ct1 — system installation in /usr/local "
                                        "(/usr/local/lib/sandbox-ai/versions/0.1.80), Python 3.12.3")
    assert not view.version_line.isHidden() and view.count() == 2, "the entry is a check as well"
    view.set_results([{"status": "ok", "check": "sandbox-ai", "detail": "0.1.70 (/opt/sandbox-ai)"}], host="ct1")
    assert view.version_line.text() == "sandbox-ai 0.1.70 on ct1 — (/opt/sandbox-ai)"
    view.set_results([{"status": "ok", "check": "Python"}], host="ct1",
                     known={"version": "0.1.60", "kind": "user", "update": True})
    assert view.version_line.text() == "sandbox-ai 0.1.60 on ct1 — the user's own installation (from the hosts list)"
    view.set_text("FAIL  Podman   not found", host="old1", known={"version": "0.1.50"})
    assert view.version_line.text() == "sandbox-ai 0.1.50 on old1 (from the hosts list)"
    view.set_results([("ok", "sandbox-ai", "0.1.79, a checkout (/work); Python 3.11.2", "")])
    assert view.version_line.text() == "sandbox-ai 0.1.79 — a checkout (/work); Python 3.11.2"
    view.set_results([{"status": "ok", "check": "Python"}], host="ct1")
    assert view.version_line.isHidden() and view.version_line.text() == "", "no version known: no line"
