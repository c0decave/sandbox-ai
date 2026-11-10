"""Safeguards in the web interface: one fixed command per request (sandbox-ai safeguards --json), the page, the
toasts of new records (grouped and rate-limited in app.js as safeguards.py does it) and the mute setting."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "sandbox" / "web"
spec = importlib.util.spec_from_file_location("test_web_for_safeguards", ROOT / "tests" / "test_web.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)
w = T.w

RECORDS = [
    {"v": 1, "time": "2026-11-07T10:00:05Z", "run": "claude-1", "kind": "firewall", "level": "warning",
     "what": "tcp to 203.0.113.7:443", "why": "not allowed", "fix": "sandbox-ai net allow claude-1 --allow-tcp "
     "203.0.113.7:443", "stays": False, "count": 1, "target": "203.0.113.7", "id": "a1"},
    {"v": 1, "time": "2026-11-07T10:01:00Z", "run": "claude-1", "kind": "oom", "level": "error",
     "what": "the kernel ended 1 process", "why": "out of memory", "fix": "--memory 16g", "stays": False, "count": 1,
     "id": "b2"},
]


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setattr(w, "SCRYPT", T.FAST)
    secret = T.add_user()
    runner = T.FakeRunner({("safeguards",): (0, "".join(json.dumps(r) + "\n" for r in RECORDS) + "not json\n", "")})
    svc = T.service(runner)
    _, headers, body = T.login(svc, secret)
    ok = {**T.HEADERS, "Cookie": T.cookie_of(headers), "X-CSRF-Token": body["csrf"]}

    def call(method, path, data=None):
        status, _, out = svc.handle(method, path, ok, json.dumps(data).encode() if data is not None else b"",
                                    "192.0.2.1")
        return status, json.loads(out)
    call.runner = runner
    return call


def test_the_records_are_one_fixed_command(logged_in):
    status, out = logged_in("GET", "/api/hosts/ct1/safeguards")
    assert status == 200 and out == RECORDS  # JSON lines; a damaged line is skipped
    assert logged_in.runner.calls[-1] == ("ct1", ["safeguards", "--json", "--tail", str(w.SAFEGUARDS_TAIL), "--all"])
    status, out = logged_in("GET", "/api/hosts/ct1/safeguards/claude-1")
    assert status == 200 and logged_in.runner.calls[-1][1][-2:] == ["--", "claude-1"]
    calls = len(logged_in.runner.calls)
    for bad in ("--all", "-h", "a%20b"):
        assert logged_in("GET", f"/api/hosts/ct1/safeguards/{bad}")[0] == 400
    assert len(logged_in.runner.calls) == calls, "nothing refused reaches the host"
    logged_in.runner.answers[("safeguards",)] = (2, "", "invalid choice: 'safeguards'")
    status, out = logged_in("GET", "/api/hosts/ct1/safeguards")
    assert status == 502 and "invalid choice" in out["error"]
    # The mute setting goes through the defaults command like every other key.
    status, _ = logged_in("POST", "/api/hosts/ct1/defaults", {"key": "safeguard_mute", "value": ["dns"]})
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["defaults", "set", "safeguard_mute", '["dns"]'])


def test_the_page_lists_them_with_the_fix_and_offers_the_mute_setting():
    script = (WEB / "app.js").read_text()
    assert '["safeguards", N_("Safeguards")]' in script
    page = script.split("pages.safeguards = ")[1].split("pages.settings")[0]
    assert 'hostPath("/safeguards")' in page and "status-bad" in page and "status-warn" in page
    assert "stays refused" in page and "navigator.clipboard.writeText(r.fix)" in page
    settings = script.split("pages.settings = ")[1].split("pages.hosts")[0]
    assert "safeguard_kinds" in settings and 'save("safeguard_mute"' in settings
    assert ".innerHTML" not in script and "confirm(" not in page
    css = (WEB / "app.css").read_text()
    assert ".safeguard.warning td:first-child { border-left: 3px solid var(--warn); }" in css
    # The kinds and their titles are sandbox-ai's.
    S = importlib.util.module_from_spec(importlib.util.spec_from_file_location("sg_for_web", ROOT / "sandbox/safeguards.py"))
    S.__spec__.loader.exec_module(S)
    for kind, (_, title, grouped) in S.KINDS.items():
        assert f'"{title}"' in script.split("const SAFEGUARD_TITLES")[1].split("};")[0], kind


DRIVER_END = r"""
let clock = 1000000;
const shown = [];
const Notifier = vm.runInContext("SafeguardNotifier", context);  // a class is no property of the global
const n = new Notifier(x => shown.push(x), () => clock);
const fw = { run: "claude-1", kind: "firewall", level: "warning", what: "tcp to 203.0.113.7:443", why: "not allowed",
  fix: "sandbox-ai net allow claude-1 --allow-tcp 203.0.113.7:443", target: "203.0.113.7", id: "a" };
for (let i = 0; i < 12; i++) n.add(Object.assign({}, fw, { id: `f${i}` }));
n.add({ run: "claude-1", kind: "canary", level: "error", what: "a decoy", announced: true });
n.muted = new Set(["dns"]);
n.add({ run: "claude-1", kind: "dns", level: "warning", what: "lookup of x.example", target: "x.example" });
n.flush();
const first = shown.length;
clock += 60000;
n.flush();
for (let i = 0; i < 6; i++) n.add(Object.assign({}, fw, { target: `198.51.100.${i}` }));
n.add({ run: "claude-1", kind: "oom", level: "error", what: "the kernel ended 1 process", why: "out of memory", fix: "--memory 16g" });
const beforeSummary = shown.length;
clock += 60000;
n.flush();
console.log(JSON.stringify({ first, shown, beforeSummary }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_new_records_become_grouped_rate_limited_toasts(tmp_path):
    driver = tmp_path / "safeguards.js"
    driver.write_text(T.TOAST_DRIVER.split("const box = () =>")[0] + DRIVER_END)
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), str(WEB / "themes.json")], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    shown = result["shown"]
    assert result["first"] == 1, "twelve equal refusals: one toast at once; the canary told itself; dns is muted"
    assert shown[0]["title"] == "claude-1: connection dropped by the firewall" and shown[0]["level"] == "warning"
    assert shown[0]["text"].split("\n") == ["tcp to 203.0.113.7:443", "Why: not allowed",
                                            "To allow it: sandbox-ai net allow claude-1 --allow-tcp 203.0.113.7:443"]
    assert shown[1]["title"] == "claude-1: 12 connections to 203.0.113.7 dropped by the firewall in the last minute"
    # A new minute: four warnings at most (the grouped one counted), the out-of-memory error always, the rest summed up.
    window = shown[2:result["beforeSummary"]]
    assert [s["kind"] for s in window] == ["firewall"] * 3 + ["oom"] and window[-1]["level"] == "error"
    assert shown[-1]["kind"] == "summary" and shown[-1]["title"] == "claude-1: 3 more safeguard events"
