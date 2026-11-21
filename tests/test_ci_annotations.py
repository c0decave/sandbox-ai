"""A failed CI step's annotations (tests/ci_annotations.py): what the next run of a red job must say, without its log.

The jobs killswitch, nested and services were red for three releases while their one annotation named no test and
ended in the middle of the first assertion (60 lines, 300 characters each; -rs lists only the skipped tests). Here:
pytest's own output of a run with failures, parsed (each failed test by name, its assertion in full, the same words
once); the parts' size and number within what GitHub keeps; the tests' reports; the command as the workflow runs it;
and the workflow's steps that use it."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import ci_annotations as ci

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

FAILING = '''
import json

import pytest

LONG = {"tcp internet": "connected", "tcp lan": "refused", **{f"attempt {n}": "x" * 40 for n in range(120)}}


@pytest.fixture(scope="module")
def probe():
    raise AssertionError("the probe failed (3): 100% of it\\nits second line: a, b")


def test_passes():
    assert True


def test_lookup():
    assert "failed: no answer from 192.168.50.1" == ["198.51.100.7"], json.dumps(LONG)


def test_lookup_with_the_results_themselves():
    assert "failed: no answer from 192.168.50.1" == ["198.51.100.7"], LONG


def test_first_of_the_module(probe):
    pass


def test_second_of_the_module(probe):
    pass


@pytest.mark.parametrize("mode", ["all", "local"])
def test_cut(mode):
    pytest.fail(f"net cut {mode}:\\n" + "\\n".join(f"line {n} of {mode}" for n in range(5)))


@pytest.mark.skip(reason="needs nft and a namespace")
def test_skipped():
    pass
'''


@pytest.fixture(scope="module")
def output(tmp_path_factory):
    """pytest's own output of a run as CI's steps start it (-vv -rfEs), with failures, errors and a skip. (-vv:
    with -v pytest shortens the two sides an assertion compares.)"""
    folder = tmp_path_factory.mktemp("failing")
    (folder / "test_failing.py").write_text(FAILING)
    done = subprocess.run([sys.executable, "-m", "pytest", "-vv", "-rfEs", "-p", "no:cacheprovider", "test_failing.py"],
                          cwd=folder, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "PYTEST_ADDOPTS": "", "COLUMNS": "120"})
    assert done.returncode == 1, done.stdout + done.stderr
    return done.stdout


def message(line):
    """(level, title, the message unescaped) of one workflow command."""
    head, _, text = line[2:].partition("::")
    level, _, title = head.partition(" title=")
    unescape = lambda value: value.replace("%0A", "\n").replace("%0D", "\r").replace(  # noqa: E731
        "%3A", ":").replace("%2C", ",").replace("%25", "%")
    return level, unescape(title), unescape(text)


def test_every_failed_test_is_named_with_its_whole_assertion(output):
    found = dict(ci.failures(output))
    assert set(found) == {"test_lookup", "test_lookup_with_the_results_themselves",
                          "ERROR at setup of test_first_of_the_module",
                          "ERROR at setup of test_second_of_the_module", "test_cut[all]", "test_cut[local]"}
    lookup = found["test_lookup"]
    assert '"attempt 119": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"' in lookup  # (not cut after 300 characters)
    assert "assert 'failed: no answer from 192.168.50.1' == ['198.51.100.7']" in lookup
    assert lookup.startswith('AssertionError: {"tcp internet": "connected", "tcp lan": "refused", "attempt 0": ')
    # Why the live tests give their assertions a text (json.dumps), never the results themselves: pytest writes an
    # object given as an assertion's message shortened (reprlib: four entries of a dict and "...") - at every
    # verbosity. That was all CI's kill switch job said of the probe's eleven attempts.
    shortened = found["test_lookup_with_the_results_themselves"].splitlines()[0]
    assert shortened.startswith("AssertionError: {'") and shortened.endswith(", ...}"), shortened
    assert "'attempt 119'" not in shortened and len(shortened) < 400
    assert not any(line.startswith("E ") for line in lookup.splitlines()), "pytest's prefix is not repeated"
    assert found["test_cut[local]"].splitlines()[:2] == ["Failed: net cut local:", "line 0 of local"]
    assert found["test_cut[local]"].splitlines()[-1] == "line 4 of local"
    assert "the probe failed (3): 100% of it" in found["ERROR at setup of test_first_of_the_module"]
    assert "its second line: a, b" in found["ERROR at setup of test_first_of_the_module"]


def test_the_outcome_names_the_failed_and_the_skipped_tests(output):
    said = ci.outcome(output)
    for expected in ("FAILED test_failing.py::test_lookup", "FAILED test_failing.py::test_cut[all]",
                     "ERROR test_failing.py::test_first_of_the_module", "SKIPPED [1] test_failing.py",
                     "needs nft and a namespace"):
        assert expected in said, said
    assert said.splitlines()[-1].startswith("4 failed, 1 passed, 1 skipped, 2 errors in "), said
    assert "test_passes" not in said
    assert all(len(line) <= 300 for line in said.splitlines())
    # Without a summary (the step was killed, pytest crashed): the output's last lines.
    killed = "\n".join(f"tests/test_x.py::test_{n} PASSED" for n in range(40)) + "\nFatal Python error: Aborted"
    assert ci.outcome(killed).splitlines()[0].startswith("no summary of pytest's")
    assert ci.outcome(killed).splitlines()[-1] == "Fatal Python error: Aborted"
    assert len(ci.outcome(killed).splitlines()) == 16
    assert ci.outcome("").startswith("no summary")


def test_the_same_words_are_said_once(output):
    names = [name for name, _ in ci.merged(ci.failures(output))]
    assert "ERROR at setup of test_first_of_the_module (and 1 more with the same words)" in names
    assert not any("test_second_of_the_module" in name for name in names)
    assert len(names) == 5
    assert ci.merged([]) == [] and ci.merged([("a", "x"), ("b", "y")]) == [("a", "x"), ("b", "y")]


def test_the_annotations_of_a_failed_step(output):
    lines = ci.annotations("kill switch in the kernel failed", output, [("mcp-vpn", "== rules ==\ntable inet x {\n}")])
    told = [message(line) for line in lines]
    assert all(line.startswith(("::error title=", "::warning title=", "::notice title=")) for line in lines)
    assert all("\n" not in line and "\r" not in line for line in lines), "one command, one line"
    assert all(len(line.partition("::")[2].partition("::")[2].encode()) <= ci.LIMIT for line in lines)
    # First the outcome, under the step's own title (as before: the one annotation every reader knows).
    assert told[0][:2] == ("error", "kill switch in the kernel failed")
    assert "FAILED test_failing.py::test_lookup" in told[0][2]
    errors = [entry for entry in told if entry[0] == "error"]
    assert 6 <= len(errors) <= ci.PER_LEVEL
    # Every failure is there with its first part; the long one goes on right after it, and is whole when put together.
    titles = [title for _, title, _ in errors[1:]]
    assert len([title for title in titles if "(1/" in title]) == 5, titles
    long = [index for index, title in enumerate(titles) if ": test_lookup (" in title]
    assert len(long) >= 2 and long == list(range(long[0], long[0] + len(long))), titles
    assert [titles[index].rpartition(" ")[2] for index in long] == [f"({n + 1}/{len(long)})" for n in range(len(long))]
    whole = "".join(text for _, title, text in errors if ": test_lookup (" in title)  # (its one long line, cut inside)
    assert whole.replace("\n", "") == dict(ci.failures(output))["test_lookup"].replace("\n", "")
    assert "the probe failed (3): 100% of it\nits second line: a, b" in "\n".join(text for _, _, text in errors)
    assert told[-1] == ("warning", "kill switch in the kernel failed: mcp-vpn (1/1)", "== rules ==\ntable inet x {\n}")


def test_no_more_annotations_than_github_keeps_and_each_failure_is_heard():
    """30 failures, each of 10 KB, and 12 reports: 10 errors (the outcome and nine failures' first parts), 10
    warnings and 10 notices (each report's first part, and second parts of as many as there is room for, each
    report's parts together) - never one failure's parts alone."""
    blocks = "".join(f"______ test_{n} ______\n" + "".join(f"E   test {n} line {m} {'y' * 90}\n" for m in range(100))
                     for n in range(30))
    text = ("============ FAILURES ============\n" + blocks + "======= short test summary info =======\n"
            + "".join(f"FAILED tests/test_x.py::test_{n} - AssertionError\n" for n in range(30))
            + "======= 30 failed in 1.00s =======\n")
    reports = [(f"report-{n:02d}", f"report {n}\n" + "z" * 9000) for n in range(12)]
    lines = ci.annotations("nested containers failed", text, reports)
    levels = [message(line)[0] for line in lines]
    assert levels == ["error"] * 10 + ["warning"] * 10 + ["notice"] * 10
    assert all(len(line.partition("::")[2].partition("::")[2].encode()) <= ci.LIMIT for line in lines)
    titles = [message(line)[1] for line in lines]
    assert [title.split(": ")[1] for title in titles[1:10]] == [f"test_{n} (1/3)" for n in range(9)]
    assert [title.split(": ")[1] for title in titles[10:]] == [
        f"report-{n:02d} ({part}/3)" for n in range(12) for part in ((1, 2) if n < 8 else (1,))]
    assert "... and 0 more" not in message(lines[0])[2] and "30 failed in 1.00s" in message(lines[0])[2]
    # Nothing failed by name (a skip alone turns these steps red): the outcome alone.
    skipped = ("===== short test summary info =====\nSKIPPED [7] tests/test_x.py:3: needs nft\n"
               "===== 7 skipped in 0.1s =====")
    assert [message(line) for line in ci.annotations("t", skipped)] == [
        ("error", "t", "SKIPPED [7] tests/test_x.py:3: needs nft\n7 skipped in 0.1s")]


@pytest.mark.parametrize("text, limit", [
    ("", 100), ("one line", 100), ("a\n" * 500, 100), ("x" * 1000, 100), ("50% of\r\nit\n" * 40, 64),
    ("ä" * 300 + "\n" + "€uro " * 100, 50), ("short\n" + "y" * 250 + "\nshort again", 100)])
def test_parts_are_small_enough_and_lose_nothing(text, limit):
    found = ci.parts(text, limit)
    assert all(ci.size(part) <= limit for part in found), [ci.size(part) for part in found]
    assert all(part for part in found)
    # Put together they are the text again (a line longer than a part is cut inside, without a line end added).
    lines = text.splitlines()
    assert "".join(found).replace("\n", "") == "".join(lines).replace("\n", "")
    if all(ci.size(line) <= limit for line in lines):
        assert "\n".join(found) == "\n".join(lines)


def test_a_command_cannot_be_ended_by_what_it_says():
    line = ci.command("error", "nested: a, b", "100%\nsecond line\r::warning::forged")
    assert line == "::error title=nested%3A a%2C b::100%25%0Asecond line%0D::warning::forged"
    assert "\n" not in line
    assert message(line) == ("error", "nested: a, b", "100%\nsecond line\r::warning::forged")
    assert ci.escape_property("a%3Ab") == "a%253Ab"


def test_a_report_is_kept_only_where_ci_asks_for_it(tmp_path, monkeypatch):
    monkeypatch.delenv(ci.REPORTS, raising=False)
    assert ci.report("mcp-vpn", "nothing") is None and ci.read_reports(None) == []
    monkeypatch.setenv(ci.REPORTS, str(tmp_path / "reports"))
    path = ci.report("mcp vpn/../up", "== rules ==\ntable inet x\n")
    assert path == str(tmp_path / "reports" / "mcp-vpn-..-up.txt") and Path(path).read_text().startswith("== rules")
    assert Path(path).parent == tmp_path / "reports", "a name is no path"
    ci.report("b", {"not": "text"})
    assert ci.read_reports(str(tmp_path / "reports")) == [("b", "{'not': 'text'}"),
                                                          ("mcp-vpn-..-up", "== rules ==\ntable inet x\n")]
    assert ci.read_reports(str(tmp_path / "none")) == []
    # A report that cannot be written does not fail the test that wrote it.
    (tmp_path / "file").write_text("")
    assert ci.report("x", "y", directory=str(tmp_path / "file" / "below")) is None


def test_a_reports_sections_put_what_matters_first():
    text = ci.sections({"before": {"results": {"tcp": "connected"}}, "rules": "table inet x {\n}\n", "up": [1, 2]},
                       order=("up", "missing", "rules"))
    assert text == '== up ==\n[1, 2]\n== rules ==\ntable inet x {\n}\n== before ==\n{"results": {"tcp": "connected"}}\n'
    assert ci.sections({}) == "\n"


def test_the_command_as_the_workflow_runs_it(tmp_path, output):
    """python tests/ci_annotations.py TITLE OUTPUT REPORTS: the workflow's own call, as a process."""
    (tmp_path / "killswitch.txt").write_text(output)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "mcp-vpn.txt").write_text("== up ==\n" + json.dumps({"dns lan resolver": "failed: no answer"}) + "\n")
    (reports / "empty.txt").write_text("\n")
    done = subprocess.run([sys.executable, str(ROOT / "tests" / "ci_annotations.py"),
                           "kill switch in the kernel failed", str(tmp_path / "killswitch.txt"), str(reports)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stderr == "", done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].startswith("::error title=kill switch in the kernel failed::FAILED test_failing.py::test_lookup")
    assert lines[-1] == ('::warning title=kill switch in the kernel failed%3A mcp-vpn (1/1)::== up ==%0A'
                         '{"dns lan resolver": "failed: no answer"}')
    assert not any("empty" in line for line in lines)
    # An output that is not there is said, not a traceback; a wrong call says how it is called.
    missing = subprocess.run([sys.executable, str(ROOT / "tests" / "ci_annotations.py"), "t", str(tmp_path / "none")],
                             capture_output=True, text=True, timeout=120, env={**os.environ, ci.REPORTS: ""})
    assert missing.returncode == 0 and missing.stdout.startswith("::error title=t::no summary of pytest's")
    assert "could not be read" in missing.stdout
    wrong = subprocess.run([sys.executable, str(ROOT / "tests" / "ci_annotations.py")], capture_output=True,
                           text=True, timeout=120)
    assert wrong.returncode == 2 and wrong.stderr.startswith("Usage: python tests/ci_annotations.py TITLE")


def job(name):
    """The text of the workflow's job NAME (up to the next job)."""
    text = WORKFLOW.read_text()
    start = text.index(f"\n  {name}:\n")
    following = [text.index(f"\n  {other}:\n", start + 1) for other in
                 ("nested", "services", "browser", "agents-live", "package", "macos", "app", "files-remote")
                 if f"\n  {other}:\n" in text[start + 1:]]
    return text[start:min(following)] if following else text[start:]


@pytest.mark.parametrize("name, title, steps", [
    ("killswitch", "kill switch in the kernel failed", 2), ("nested", "nested containers failed", 1),
    ("services", "services failed", 1)])
def test_the_red_jobs_say_what_failed_in_full(name, title, steps):
    """The three jobs that stayed red: every pytest step lists the failed tests in its summary (-rfEs: FAILED lines,
    not only SKIPPED) and shows the two sides an assertion compares unshortened (-vv), keeps the tests' reports
    and hands both to ci_annotations.py - with the old one-line annotation still there should the script fail."""
    text = job(name)
    runs = [line for line in text.splitlines() if "python -m pytest" in line]
    assert len(runs) == steps and all(" -vv -rfEs " in line and " -rs " not in line for line in runs), runs
    assert text.count("SANDBOX_AI_CI_REPORTS: ${{ runner.temp }}/ci-reports") == steps
    calls = [line.strip() for line in text.splitlines() if "tests/ci_annotations.py" in line]
    assert len(calls) == steps and all('"$SANDBOX_AI_CI_REPORTS" || {' in line for line in calls), calls
    assert f'python tests/ci_annotations.py "{title}"' in text or name == "killswitch"
    assert text.count("echo \"::error title=") >= steps, "the fallback annotation"
    assert "exit 1" in text


def test_the_killswitch_job_can_list_its_connections():
    """conntrack shows where a redirected lookup went and with which address it left (the probe's facts)."""
    assert "wireguard-tools passt uidmap conntrack" in job("killswitch")


def test_a_hung_test_of_any_pytest_step_prints_where_it_waits():
    """CI 0.1.187: the app job hung at 80 % until its timeout killed it, and its annotation said only the test's name -
    its pytest ran without faulthandler_timeout, unlike the core tests'. Every step that runs the test suites under a
    timeout prints the stacks of a test that runs longer than 5 minutes, and the app step's annotation carries them."""
    text = WORKFLOW.read_text()
    runs = [line for line in text.splitlines() if "-m pytest" in line and "timeout " in line]
    assert len(runs) >= 2, runs  # (the core tests' parts and the app's)
    for line in runs:
        step = text[text.index(line):text.index(line) + 400]
        assert "faulthandler_timeout=300" in step, line
    app = text[text.index("name: Tests (offscreen, Qt required)"):]
    app = app[:app.index("exit \"$status\"")]
    assert "grep -E -A20000 '^Timeout \\(' tests.txt" in app and '"$hung"' in app
