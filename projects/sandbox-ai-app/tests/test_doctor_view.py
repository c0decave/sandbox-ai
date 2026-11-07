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
