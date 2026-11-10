"""scripts/live_check.py without a container engine: its report, the reading of pytest's junit XML, the refusal
inside a container, a host without Podman and Bubblewrap (said, nothing started)."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_check_test", ROOT / "scripts/live_check.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_live_check" name="test_live_a_canary_read_is_seen[podman]" time="12.5"/>
<testcase classname="tests.test_live_check" name="test_live_vpn[podman]" time="0.01">
  <skipped message="give --live-vpn-profile NAME">skip</skipped></testcase>
<testcase classname="tests.test_live_e2e" name="test_live_push_is_checked_on_the_network[podman]" time="40">
  <failure message="AssertionError: push was not refused">assert False</failure></testcase>
</testsuite></testsuites>"""


def test_junit_results_are_read(tmp_path):
    path = tmp_path / "x.xml"
    path.write_text(JUNIT)
    results = lc.junit_results(path)
    assert [(name, outcome) for name, outcome, _, _ in results] == [
        ("test_live_check::test_live_a_canary_read_is_seen[podman]", "passed"),
        ("test_live_check::test_live_vpn[podman]", "skipped"),
        ("test_live_e2e::test_live_push_is_checked_on_the_network[podman]", "failed")]
    assert "push was not refused" in results[2][2] and results[2][3] == 40.0
    assert lc.junit_results(tmp_path / "missing.xml") == []


def test_the_report_lists_every_check_and_what_failed(tmp_path, capsys):
    report = lc.Report(tmp_path)
    report.add("sandbox-ai doctor", "passed")
    report.add("podman: test_x", "failed", "AssertionError: a | b\nline 2")
    counts = report.write({"date": "d", "kernel": "k"})
    assert counts == {"passed": 1, "failed": 1}
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["counts"] == counts and data["checks"][1]["detail"].startswith("AssertionError")
    text = (tmp_path / "report.md").read_text()
    assert "| failed | podman: test_x | AssertionError: a \\| b line 2 |" in text and "## What failed" in text
    assert "FAIL  podman: test_x" in capsys.readouterr().out


def test_it_refuses_to_run_in_a_container(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "inside_a_container", lambda: True)
    with pytest.raises(SystemExit, match="run it on the host"):
        lc.main(["--out", str(tmp_path)])


def test_a_host_without_podman_and_bubblewrap(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "inside_a_container", lambda: False)
    monkeypatch.setattr(lc.shutil, "which", lambda name: None)
    ran = []

    def fake_run(command, timeout=600, **kwargs):
        ran.append(command)
        return (0, "sandbox-ai 0.1.92\n", "") if command[0].endswith("sandbox-ai") else (127, "", "not found")
    monkeypatch.setattr(lc, "run", fake_run)
    monkeypatch.setattr(lc, "pytest_python", lambda directory: pytest.fail("no backend: no pytest run"))
    assert lc.main(["--out", str(tmp_path)]) == 1
    data = json.loads((tmp_path / "report.json").read_text())
    assert {c["check"]: c["outcome"] for c in data["checks"]} == {
        "podman installed": "failed", "bwrap installed": "failed"}
    assert data["facts"]["sandbox-ai"] == "sandbox-ai 0.1.92"
    assert not any("pytest" in " ".join(command) for command in ran)
