"""The web interface's running start: its Resources card offers the devices of the start's host (GET
/api/hosts/HOST/device-scan: `sandbox-ai devices scan --json` there), grouped by kind, minus those the run has and
those sandbox-ai refuses; a GPU kind shown, not offered (--gpu cannot change while it runs); one chosen goes into the
Device field and is added as before (asked first). A host that does not answer is said in one line."""
import json
import shutil

import pytest

from test_web import FAKE_DOM, run_driver

SCAN = {"devices": [
    {"kind": "usb", "value": "usb:0403:6001", "option": "--device", "label": "FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0",
     "nodes": ["/dev/bus/usb/001/006", "/dev/ttyUSB0"], "refused": None, "note": None},
    {"kind": "usb", "value": "usb:046d:c52b", "option": "--device", "label": "USB Receiver (usb:046d:c52b)", "nodes": [],
     "refused": "no device node of it can be passed", "note": None},
    {"kind": "serial", "value": "/dev/ttyACM0", "option": "--device", "label": "Pico serial port - /dev/ttyACM0",
     "nodes": ["/dev/ttyACM0"], "refused": None, "note": None},
    {"kind": "kvm", "value": "/dev/kvm", "option": "--device", "label": "KVM (virtual machines) - /dev/kvm",
     "nodes": ["/dev/kvm"], "refused": None, "note": None},
    {"kind": "gpu", "value": "nvidia", "option": "--gpu", "label": "NVIDIA GPU (--gpu nvidia) - /dev/nvidia0",
     "nodes": ["/dev/nvidia0"], "refused": None, "note": None}], "notes": []}
HELD = [{"id": "1", "value": "/dev/ttyACM0", "source": "start", "state": "attached", "nodes": ["/dev/ttyACM0"]}]

DRIVER = FAKE_DOM + r"""
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const byId = (id) => document.getElementById(id);
const SCAN = __SCAN__, HELD = __HELD__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.page = "logs"; state.hosts = [{ name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  let scan = SCAN;
  answer = (method, path, sent) => {
    if (path === "/api/hosts/ct1/device-scan") return scan;
    if (path === "/api/hosts/ct1/devices/web" && method === "GET") return HELD;
    if (path === "/api/hosts/ct1/starts") return [{ name: "web", id: "web-1", kind: "session", state: "running", directory: "/d" }];
    if (method === "GET") return path.includes("/limits/") ? {} : [];
    return { output: "added" };
  };
  vm.runInContext('resourcesCard(__main, { name: "web", id: "web-1", host: "ct1", state: "running", kind: "session", directory: "/d", keep: true })', context);
  await settle();
  const card = byId("resources-card"), select = byId("dev-detected");
  result.asked = calls.filter(c => c.path.endsWith("/device-scan")).map(c => c.path);
  result.groups = select.children.filter(n => n.tagName === "optgroup").map(g => [g.attributes.label, g.children.map(o => [o.value, o.textContent, o.disabled || o.attributes.disabled !== undefined])]);
  result.note = card.all().find(n => n.className === "hint" && n.textContent.includes("detected on")).textContent;
  select.value = "usb:0403:6001"; select.fire("change");
  result.field = byId("dev-value").value;
  button(card, "Add device").fire("click");
  button(card, "Yes, hand it over").fire("click"); await settle();
  result.posts = posts();
  // a host that does not answer: said in one line; the field stays
  scan = new Error("ssh: connect to host ct1 port 22: Connection refused");
  button(card, "Refresh").fire("click"); await settle();
  result.unreachable = [select.hidden, card.all().some(n => n.className === "hint" && n.textContent.startsWith("The devices of ct1 could not be read"))];
  vm.runInContext('setLanguage("de")', context);
  result.german = vm.runInContext('[t("Detected on {host}", { host: "ct1" }), t("Serial ports"), t("restart needed")]', context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_resources_card_offers_the_hosts_detected_devices(tmp_path):
    source = DRIVER.replace("__SCAN__", json.dumps(SCAN)).replace("__HELD__", json.dumps(HELD))
    result = run_driver(tmp_path, source, "devices_scan")
    assert result["asked"] == ["/api/hosts/ct1/device-scan"], "that host's devices, never this machine's"
    groups = dict((label, rows) for label, rows in result["groups"])
    assert list(groups) == ["USB devices", "Virtual machines", "GPU (--gpu)"], "held and refused ones not offered"
    assert groups["USB devices"] == [["usb:0403:6001", "FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0", False]]
    assert groups["GPU (--gpu)"] == [["", "NVIDIA GPU (--gpu nvidia) - /dev/nvidia0 (restart needed)", True]]
    assert result["note"] == "3 detected on ct1; 1 the run has already; 1 refused by sandbox-ai (input devices, " \
                             "keyboards ...), not offered"
    assert result["field"] == "usb:0403:6001"
    assert result["posts"] == [["/api/hosts/ct1/devices/web", {"device": "usb:0403:6001", "confirm": True}]]
    assert result["unreachable"] == [True, True]
    assert result["german"] == ["Erkannt auf ct1", "Serielle Schnittstellen", "Neustart nötig"]
