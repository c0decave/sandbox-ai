"""The Board's devices: detected on the board's host and offered in a drop-down (user 2026-11-19: "ob beim Hinzufügen von
Geräten lokale oder Remote-Geräte erkannt werden und im Dropdown angezeigt werden"), and changed while a start of the
board's project runs ("add feature for add device to board during runtime"). SandboxAI.host_run is a stand-in here
that records (host, args) and answers from a table: this machine is "local", the host ct1 is read over SSH - never this
machine's devices for ct1."""
import json

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend, i18n, widgets  # noqa: E402
from sandbox_ai_app import device_picker as DP  # noqa: E402
from sandbox_ai_app import list_fields as LF  # noqa: E402
from sandbox_ai_app import live_devices as LD  # noqa: E402

from test_remote_parity import MADE, SESSION, answer, asked, cleaned, env, sai  # noqa: E402,F401 - shared fixtures

SCAN = ["devices", "scan", "--json"]


def entry(kind, value, label, nodes=(), option="--device", refused=None, note=None, usb=None):
    return {"kind": kind, "value": value, "label": label, "nodes": list(nodes) or [value], "option": option,
            "usb": usb, "product": None, "manufacturer": None, "links": [], "refused": refused, "note": note}


CT1 = {"devices": [
    entry("usb", "usb:0403:6001", "FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0",
          ["/dev/bus/usb/001/006", "/dev/ttyUSB0"], usb="0403:6001"),
    entry("usb", "usb:2e8a:000a", "Pico (usb:2e8a:000a) - /dev/ttyACM0", ["/dev/bus/usb/001/005", "/dev/ttyACM0"],
          usb="2e8a:000a"),
    entry("usb", "usb:046d:c52b", "USB Receiver (usb:046d:c52b)", [], refused="no device node of it can be passed"),
    entry("serial", "/dev/ttyUSB0", "FTDI FT232R USB UART serial port (usb:0403:6001) - /dev/ttyUSB0"),
    entry("video", "/dev/video0", "UVC Camera (usb:046d:0825) - /dev/video0", note="a camera"),
    entry("kvm", "/dev/kvm", "KVM (virtual machines) - /dev/kvm"),
    entry("gpu", "nvidia", "NVIDIA GPU (--gpu nvidia) - /dev/nvidia0", ["/dev/nvidia0"], option="--gpu"),
    entry("input", "/dev/input/event5", "USB Receiver - /dev/input/event5", refused="keyboards")],
    "notes": []}
LOCAL_SCAN = {"devices": [entry("serial", "/dev/ttyACM7", "Here-only serial port - /dev/ttyACM7")], "notes": []}
RUN = "wild-dromouse"
LISTING = [{"id": 1, "value": "/dev/ttyUSB0", "source": "start", "state": "attached", "nodes": ["/dev/ttyUSB0"],
            "device": {"usb": "0403:6001", "product": "FT232R USB UART"}},
           {"id": 2, "value": "usb:2e8a:000a", "source": "added", "state": "attached",
            "nodes": ["/dev/bus/usb/001/005", "/dev/ttyACM0"], "device": {"usb": "2e8a:000a", "product": "Pico"}}]


@pytest.fixture(autouse=True)
def no_cache():
    DP.forget()
    yield
    DP.forget()


@pytest.fixture
def board(qapp, sai, monkeypatch, tmp_path):
    from sandbox_ai_app.board_view import BoardPage
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    answer(sai, SCAN, CT1)
    answer(sai, SCAN, LOCAL_SCAN, host="local")
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    MADE.append(page)
    assert page.host_choice.choose("ct1")
    yield page
    page.deleteLater()


def inspect(page, kind="devices"):
    if page.board.first(kind) is None:
        page.add_node(kind)
    node = page.board.first(kind)
    page.inspect_timer.stop()
    page.inspected = node.id
    page.inspect(page.board.node(node.id))
    return page.board.node(node.id)


def field(page):
    found = page.option_fields["--device"]
    assert isinstance(found, LF.ListFields)
    return found


def offered(combo):
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


# -- the Board's device field: the board's host's devices --

def test_the_device_field_offers_the_boards_hosts_devices_grouped_by_kind(board, sai):
    inspect(board)
    items = offered(field(board).value_box)
    assert ("FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0", "usb:0403:6001") in items
    assert ("NVIDIA GPU (--gpu nvidia) - /dev/nvidia0", "--gpu nvidia") in items
    # Grouped: each kind's name first, a line that cannot be chosen.
    labels = [label for label, _ in items]
    assert labels.index("USB devices") < labels.index("Pico (usb:2e8a:000a) - /dev/ttyACM0") < \
        labels.index("Serial ports") < labels.index("GPU (--gpu)")
    header = field(board).value_box.model().item(labels.index("USB devices"))
    assert not header.isEnabled() and not header.isSelectable()
    # What sandbox-ai refuses is not offered; the line says what was found and what not.
    assert not any(value in ("usb:046d:c52b", "/dev/input/event5") for _, value in items)
    assert field(board).note.text() == "6 detected on ct1; 2 refused by sandbox-ai (input devices, keyboards ...), " \
                                       "not offered"
    # Read on ct1 over SSH, never this machine's.
    assert tuple(SCAN) in asked(sai) and tuple(SCAN) not in asked(sai, "local")
    assert "/dev/ttyACM7" not in [value for _, value in items]


def test_a_device_chosen_is_written_as_its_value_and_a_path_typed_still_goes(board, qapp):
    node = inspect(board)
    box = field(board).value_box
    box.activated.emit(box.findData("usb:0403:6001"))
    assert field(board).add()
    assert board.board.node(node.id).props["--device"] == ["usb:0403:6001"]
    inspect(board)
    assert field(board).add("/dev/ttyS4")
    assert board.board.node(node.id).props["--device"] == ["usb:0403:6001", "/dev/ttyS4"]
    # Not a device: refused with why, nothing written.
    inspect(board)
    assert not field(board).add("ttyS4")
    assert "path under /dev" in field(board).problem.text()
    assert board.board.node(node.id).props["--device"] == ["usb:0403:6001", "/dev/ttyS4"]


def test_a_gpu_kind_chosen_becomes_the_parts_gpu(board, qapp):
    node = inspect(board)
    box = field(board).value_box
    box.activated.emit(box.findData("--gpu nvidia"))
    field(board).add()
    props = board.board.node(node.id).props
    assert props.get("--gpu") == "nvidia" and not props.get("--device")
    board.undo.undo()
    assert not board.board.node(node.id).props.get("--gpu"), "one step of Undo"


def test_another_host_chosen_its_devices_are_offered(board, sai, wait):
    inspect(board)
    assert board.host_choice.choose("local")
    inspect(board)
    items = offered(field(board).value_box)
    assert ("Here-only serial port - /dev/ttyACM7", "/dev/ttyACM7") in items
    assert not any(value == "usb:0403:6001" for _, value in items), "ct1's devices are not this computer's"
    assert field(board).note.text().endswith("detected on this computer")


def test_an_unreachable_host_is_said_in_one_line_and_a_path_typed_still_goes(board, sai):
    answer(sai, SCAN, "", status=255, err="ssh: connect to host 192.0.2.10 port 22: Connection refused")
    node = inspect(board)
    note = field(board).note.text()
    assert note.startswith("The devices of ct1 could not be read: ssh: connect to host") and "Type a path" in note
    assert field(board).value_box.count() == 0
    assert field(board).add("/dev/ttyUSB3")
    assert board.board.node(node.id).props["--device"] == ["/dev/ttyUSB3"]


def test_an_older_sandbox_ai_on_the_host_is_said(board, sai):
    answer(sai, SCAN, "", status=2, err="sandbox-ai devices: error: argument command: invalid choice: 'scan' "
                                        "(choose from 'list', 'add', 'remove')")
    inspect(board)
    assert field(board).note.text().startswith("sandbox-ai on ct1 cannot list its devices yet (update it there)")


def test_nothing_detected_is_said(board, sai):
    answer(sai, SCAN, {"devices": [], "notes": []})
    inspect(board)
    assert field(board).note.text() == "No devices detected on ct1: type a path or usb:VENDOR:PRODUCT[:SERIAL]."


def test_detect_again_reads_the_host_anew(board, sai):
    inspect(board)
    before = asked(sai).count(tuple(SCAN))
    inspect(board)
    assert asked(sai).count(tuple(SCAN)) == before, "kept a moment: not read at every rebuild"
    answer(sai, SCAN, {"devices": [*CT1["devices"], entry("serial", "/dev/ttyACM9", "New one - /dev/ttyACM9")]})
    button = next(b for b in board.inspector_holder.findChildren(QtWidgets.QPushButton)
                  if b.text() == "Detect devices again")
    button.click()
    assert asked(sai).count(tuple(SCAN)) == before + 1
    assert field(board).value_box.findData("/dev/ttyACM9") >= 0


def test_a_scan_answer_is_checked(sai):
    """What a host answers is shown escaped; a value no --device or --gpu takes is dropped."""
    answer(sai, SCAN, {"devices": [entry("serial", "/dev/ttyUSB0", "Evil\x1b[31m - /dev/ttyUSB0"),
                                   entry("serial", "--privileged", "flag"), entry("gpu", "all", "x", option="--gpu"),
                                   entry("nonsense", "/dev/x", "x"), "not a dict"]})
    found = sai.device_scan("ct1")
    assert [e["value"] for e in found] == ["/dev/ttyUSB0"] and "\x1b" not in found[0]["label"]


# -- the live part: a start of the board's project runs --

def live(board, sai, monkeypatch, listing=LISTING):
    from sandbox_ai_app import board_view
    answer(sai, ["devices", "list", RUN, "--json"], listing)
    answer(sai, ["list", "--json"], [SESSION])
    board.add_node("workspace", path="/srv/work/project")
    node = inspect(board)
    part = board.live_devices_part
    assert isinstance(part, LD.LiveDevices) and part.run == RUN and part.host == "ct1"
    return node, part


def test_the_live_part_shows_what_the_run_has_and_offers_the_rest(board, sai, monkeypatch):
    node, part = live(board, sai, monkeypatch)
    texts = [label.text() for label in part.rows.findChildren(QtWidgets.QLabel)]
    assert any(t.startswith("/dev/ttyUSB0") and "given at the start" in t for t in texts)
    assert any(t.startswith("usb:2e8a:000a") and "Pico" in t and "(attached)" in t for t in texts)
    removes = [b for b in part.rows.findChildren(QtWidgets.QPushButton) if b.text() == "Remove from the run"]
    assert len(removes) == 1, "only a device added during the run can be taken away"
    values = [value for _, value in part.items() if value]
    # The run has the FTDI adapter (its serial node) and the Pico: neither is offered again; refused ones never.
    assert values == ["/dev/video0", "/dev/kvm"]
    gpu = [label for label, value in part.items() if label.startswith("NVIDIA")]
    assert gpu == ["NVIDIA GPU (--gpu nvidia) - /dev/nvidia0  (restart needed)"]
    assert not part.gpu_note.isHidden() and "cannot be given while it runs" in part.gpu_note.text()
    assert part.note.text().startswith("3 detected on ct1; 3 the run has already")
    assert ("devices", "list", RUN, "--json") in asked(sai) and not [e for e in sai.asked if e[0] == "local"]


def test_add_now_gives_the_device_to_the_run_and_the_board_only_when_chosen(board, sai, monkeypatch):
    node, part = live(board, sai, monkeypatch)
    answer(sai, ["devices", "add", RUN, "/dev/kvm"], "added /dev/kvm to wild-dromouse")
    answers = iter([True, False])  # add: yes; also for later starts: no
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    monkeypatch.setattr(widgets, "confirm", lambda parent, title, text, accept, **k: widgets.SHOWN.append(
        (title, text, accept)) or next(answers))
    from sandbox_ai_app import board_view
    monkeypatch.setattr(board_view, "confirm", widgets.confirm)
    monkeypatch.setattr(widgets, "inform", lambda *a, **k: None)
    assert part.choose("/dev/kvm")
    part.add_button.click()
    assert ("devices", "add", RUN, "/dev/kvm") in asked(sai)
    assert widgets.SHOWN[-2][1].startswith("On ct1: Give /dev/kvm to wild-dromouse")
    assert widgets.SHOWN[-1][2] == "Also for later starts"
    assert not board.board.node(node.id).props.get("--device"), "the board unchanged: not chosen"
    # Once more, and this time for later starts too.
    answers = iter([True, True])
    part = board.live_devices_part
    assert part.choose("/dev/video0")
    part.add_button.click()
    assert ("devices", "add", RUN, "/dev/video0") in asked(sai)
    assert board.board.node(node.id).props["--device"] == ["/dev/video0"]


def test_add_now_asked_and_declined_adds_nothing(board, sai, monkeypatch):
    node, part = live(board, sai, monkeypatch)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert part.choose("/dev/kvm")
    part.add_button.click()
    assert not [args for args in asked(sai) if args[:2] == ("devices", "add")]


def test_a_gpu_live_says_a_restart_is_needed(board, sai, monkeypatch):
    node, part = live(board, sai, monkeypatch)
    told = []
    monkeypatch.setattr(widgets, "inform", lambda parent, title, text, **k: told.append(text))
    part.picker.setEditText("--gpu nvidia")
    part.add_button.click()
    assert told and "restart" in told[-1]
    assert not [args for args in asked(sai) if args[:2] == ("devices", "add")]


def test_remove_from_the_run_asks_and_takes_the_added_one_away(board, sai, monkeypatch):
    node, part = live(board, sai, monkeypatch)
    answer(sai, ["devices", "remove", RUN, "2"], "removed")
    remove = next(b for b in part.rows.findChildren(QtWidgets.QPushButton) if b.text() == "Remove from the run")
    remove.click()
    assert ("devices", "remove", RUN, "2") in asked(sai)
    assert widgets.SHOWN[-1][1].startswith("Take usb:2e8a:000a away from wild-dromouse")


def test_a_usb_device_plugged_in_reads_the_part_again(qapp, sai, monkeypatch):
    """This machine: its USB devices looked at; a change reads the run's and the host's devices again."""
    answer(sai, SCAN, LOCAL_SCAN, host="local")
    answer(sai, ["devices", "list", RUN, "--json"], [], host="local")
    usb = [("1-1",)]
    monkeypatch.setattr(LD, "usb_signature", lambda: usb[0])
    part = LD.LiveDevices(sai, RUN, "local", poll=False)
    MADE.append(part)
    part.show()
    assert [value for _, value in part.items() if value] == ["/dev/ttyACM7"]
    reads = len(sai.asked)
    part.poll()
    assert len(sai.asked) == reads, "nothing plugged: nothing read"
    answer(sai, SCAN, {"devices": [*LOCAL_SCAN["devices"], entry("serial", "/dev/ttyUSB5", "New - /dev/ttyUSB5")]},
           host="local")
    usb[0] = ("1-1", "1-2")
    part.poll()
    assert [value for _, value in part.items() if value] == ["/dev/ttyACM7", "/dev/ttyUSB5"]


def test_the_live_part_on_this_computer_reads_here(board, sai, monkeypatch):
    answer(sai, ["devices", "list", RUN, "--json"], [], host="local")
    monkeypatch.setattr(sai, "run_for", lambda path: RUN)
    assert board.host_choice.choose("local")
    board.add_node("workspace", path="/srv/work/project")
    inspect(board)
    part = board.live_devices_part
    assert part.host == "local" and [value for _, value in part.items() if value] == ["/dev/ttyACM7"]
    assert ("devices", "list", RUN, "--json") in asked(sai, "local") and \
        tuple(SCAN) not in asked(sai), "ct1 is not asked for this computer's run"


# -- German --

def test_the_new_texts_are_german_too():
    for text in ("USB devices", "Serial ports", "GPU (--gpu)", "Detect devices again", "Add device now…",
                 "Remove from the run", "Also for later starts", "Only this run", "restart needed",
                 "{count} detected on {host}", "No devices detected on {host}: type a path or "
                 "usb:VENDOR:PRODUCT[:SERIAL].", "given at the start"):
        assert i18n.TEXTS[text]["de"] and i18n.TEXTS[text]["de"] != text, text


def test_a_gpu_the_policy_forbids_is_not_offered(board):
    board.forbidden = {"--gpu": {None}}
    inspect(board)
    assert field(board).value_box.findData("--gpu nvidia") < 0
    assert field(board).value_box.findData("usb:0403:6001") >= 0
