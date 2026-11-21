"""Sandboxes & sessions › Add device for a running sandbox: the devices its host has (`sandbox-ai devices scan --json`
there - over SSH for a start on another host, where nothing was listed before and every device had to be typed in),
grouped by kind with readable names; what the run has, what sandbox-ai refuses and a GPU kind (--gpu cannot change
while it runs) greyed out with why."""
import json

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import backend, drafts, ui, widgets  # noqa: E402
from sandbox_ai_app import device_picker as DP  # noqa: E402

from test_board_devices import CT1, LISTING  # noqa: E402
from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures


@pytest.fixture(autouse=True)
def no_cache():
    DP.forget()
    drafts.DRAFTS.clear()
    yield
    DP.forget()
    drafts.DRAFTS.clear()


def rows(dialog):
    found = []
    for i in range(dialog.list.count()):
        item = dialog.list.item(i)
        found.append((item.text(), item.data(Qt.ItemDataRole.UserRole), bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)))
    return found


def test_the_dialog_lists_the_hosts_devices_grouped_with_what_cannot_be_chosen_greyed(qapp):
    dialog = ui.DeviceDialog(None, [], "Add device to web on ct1", host="ct1", scanned=backend_entries(),
                             listing=LISTING)
    found = rows(dialog)
    assert ("USB devices", None, False) in found and ("Serial ports", None, False) in found
    chosen = {value: enabled for _, value, enabled in found if value}
    assert chosen["/dev/kvm"] and chosen["/dev/video0"]
    assert not chosen["usb:2e8a:000a"] and not chosen["/dev/ttyUSB0"], "the run has them"
    assert not chosen["usb:046d:c52b"], "refused"
    assert not chosen["nvidia"]
    assert any(text.endswith("(restart needed)") for text, value, _ in found if value == "nvidia")
    dialog.list.setCurrentRow([value for _, value, _ in found].index("/dev/kvm"))
    assert dialog.path() == "/dev/kvm"
    dialog.deleteLater()


def test_a_host_not_answering_is_said_and_a_path_is_typed(qapp):
    dialog = ui.DeviceDialog(None, [], "Add device to web on ct1", host="ct1", scanned=[],
                             problem=ValueError("ssh: connect to host 192.0.2.10 port 22: Connection refused"))
    assert dialog.list.isHidden() and dialog.problem.text().startswith("The devices of ct1 could not be read")
    dialog.edit.setText("/dev/ttyUSB1")
    assert dialog.path() == "/dev/ttyUSB1"
    dialog.deleteLater()


def backend_entries():
    """CT1 as SandboxAI.device_scan gives it (checked)."""
    sai = backend.SandboxAI.__new__(backend.SandboxAI)
    sai.host_run = lambda name, *args, timeout=120: (0, json.dumps(CT1), "")
    return backend.SandboxAI.device_scan(sai, "ct1")


def test_e2e_add_device_on_another_host_offers_that_hosts_devices(window, monkeypatch, wait):
    page = window.sandboxes
    there = backend.Instance("session", backend.remote_key("ct1", "web"), "web", session="web", agent="claude",
                             state="running", host="ct1", record={})
    monkeypatch.setattr(page, "current", lambda: there)
    asked = []
    answers = {("devices", "scan", "--json"): (0, json.dumps(CT1), ""),
               ("devices", "list", "web", "--json"): (0, json.dumps(LISTING), ""),
               ("devices", "add", "web", "/dev/kvm"): (0, "added /dev/kvm to web", "")}

    def host_run(name, *args, timeout=120, input=None):
        asked.append((name, args))
        return answers.get(tuple(args), (0, "", ""))
    monkeypatch.setattr(window.sai, "host_run", host_run)
    seen = []

    def exec_(dialog):
        seen.append(rows(dialog))
        dialog.edit.setText("/dev/kvm")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.DeviceDialog, "exec", exec_)
    page.add_device()
    assert wait(lambda: ("ct1", ("devices", "add", "web", "/dev/kvm")) in asked and not widgets.busy(page), 15)
    assert ("ct1", ("devices", "scan", "--json")) in asked
    assert not [entry for entry in asked if entry[0] == "local" and entry[1][:2] == ("devices", "scan")], \
        "never this computer's devices for a start on ct1"
    labels = [text for text, value, _ in seen[0] if value]
    assert "FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0  (added already)" in labels
    assert any("Give /dev/kvm of ct1 to web" in text for _, text, _ in widgets.SHOWN), widgets.SHOWN


def test_e2e_the_main_windows_board_reads_this_computers_devices_through_sandbox_ai(window, wait):
    """Through the real entry point: the Board of the app's window, its Devices part, `sandbox-ai devices scan --json`
    of this checkout run in the background; what it lists is offered, nothing found is said."""
    window.show_page("board")
    page = window.board
    page.add_node("devices")
    node = page.board.first("devices")
    page.inspect_timer.stop()
    page.inspect(page.board.node(node.id))
    field = page.option_fields["--device"]
    assert wait(lambda: field.note.text(), 60), "the scan's answer"
    found = window.sai.device_scan("local")
    offered = [entry for entry in found if not entry["refused"]]
    values = [field.value_box.itemData(i) for i in range(field.value_box.count()) if field.value_box.itemData(i)]
    assert values == [DP.gpu_value(e["value"]) if e["option"] == "--gpu" else e["value"]
                      for e, _ in DP.grouped(found, lambda e: not e["refused"])]
    if not offered:
        assert field.note.text() == "No devices detected on this computer: type a path or " \
                                    "usb:VENDOR:PRODUCT[:SERIAL]."
    assert field.add("/dev/ttyUSB9")
    assert wait(lambda: page.board.node(node.id).props.get("--device") == ["/dev/ttyUSB9"], 10)
