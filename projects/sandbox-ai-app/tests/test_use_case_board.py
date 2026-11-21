"""Model and harness per use case on the Board (offscreen; user 2026-11-17, "B6"): beside the agent and on the LLM part
a "Use case" choice lists sandbox-ai's use cases with their status (measured, from other tasks, not tested yet);
choosing one puts its best tested choice among the host's servers' models on the board - the server and model, the
agent, --use-case on the LLM part - as one undo step, says why with the evidence and shows the use case's "What we
know" box. Overridable: another model chosen afterwards stays. The real ./sandbox-ai's dry run takes the board's
--use-case, the profile badge says the use case's settings go over the profile. The table is the test's own
(SANDBOX_AI_MODEL_USECASES): round 5 changes the shipped one."""
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, i18n, labels, model_profile as MP, use_case as UC, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
GB = 1000 ** 3
QCN, MID, BIG = "qwen/qwen3-coder-next", "mid-35b-mlx", "big-122b-mlx"
TABLE = {
    "schema": 1, "source": "the test's own", "rounds": {"round 4": "2026-11-17"},
    "use_cases": [
        {"id": "coding", "label": "Coding and debugging", "about": "fix a bug", "tasks": ["t2n_debug"],
         "unknown": ["servers other than LM Studio: not measured"],
         "recommendations": [
             {"model": "big-?122b", "examples": [BIG], "label": "Big", "harness": "claude", "basis": "measured",
              "why": "solved it 2 of 2", "evidence": "round 4: debug 2/2", "round": "round 4", "runs": 2,
              "solved": 2},
             {"model": "mid-?35b", "examples": [MID], "label": "Mid", "harness": "opencode", "basis": "measured",
              "params": {"temperature": 0.5}, "why": "solved it 2 of 2 in OpenCode",
              "fails": ["Qwen Code: changed the tests"], "evidence": "round 4: debug 2/2", "round": "round 4",
              "runs": 2, "solved": 2}]},
        {"id": "code-review", "label": "Code review", "about": "review a branch", "tasks": ["r3_review_py"],
         "unknown": ["reviews: not tested before round 5"], "recommendations": []},
        {"id": "small-jobs", "label": "Small fast jobs", "about": "commit messages", "tasks": ["j8_jobs"],
         "recommendations": [
             {"model": "qwen-?3[^/]*coder[^/]*next", "examples": [QCN], "label": "QCN", "harness": "opencode",
              "basis": "indirect", "params": {"temperature": 0.2}, "why": "quick answers",
              "evidence": "round 4: speed probe", "round": "round 4"}]},
    ]}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    (tmp_path / "usecases.json").write_text(json.dumps(TABLE))
    monkeypatch.setenv("SANDBOX_AI_MODEL_USECASES", str(tmp_path / "usecases.json"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def servers():
    """The board's servers as backend.host_llm_servers reads them: 64 GB, Big too big, Mid fits, QCN loaded."""
    info = {BIG: {"name": BIG, "size": 70 * GB, "loaded": False}, MID: {"name": MID, "size": 37 * GB, "loaded": False},
            QCN: {"name": QCN, "size": 45 * GB, "loaded": True}}
    return [{"name": "studio", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "model": None,
             "models": [BIG, MID, QCN], "model_info": info, "answered": True, "memory_gb": 64, "params": {},
             "loop_guard_own": {}}]


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    assert sai.module("llm").main(["add", "studio", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234"],
                                  out=lambda line: None) == 0
    sai.module("llm").remember_models("studio", [BIG, MID, QCN])
    view = BoardPage(sai, lambda *a, **kw: None, lambda: {"board_grid": 20})
    view.llm_servers = servers()
    yield view
    view.deleteLater()


def items(combo):
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def choose(page, name):
    combo = page.use_case_combo
    combo.setCurrentIndex(combo.findData(name))
    combo.activated.emit(combo.currentIndex())


def texts(widget):
    return [label.text() for label in widget.findChildren(QtWidgets.QLabel)]


def test_the_agent_offers_every_use_case_with_its_status(page):
    page.inspect(page.board.first("agent"))
    shown = dict(items(page.use_case_combo))
    assert shown[""] == "none: choose the agent and the model yourself"
    assert shown["coding"] == "Coding and debugging — measured on this use case's tasks"
    assert shown["code-review"] == "Code review — not tested yet"
    assert shown["small-jobs"] == "Small fast jobs — from other tasks than this use case's own"
    assert page.use_case_combo.isEnabled()


def test_choosing_a_use_case_puts_its_choice_on_the_board_as_one_undo_step(page):
    page.inspect(page.board.first("agent"))
    before = page.board.state()
    choose(page, "coding")
    llm, agent = page.board.first("llm"), page.board.first("agent")
    assert llm.props["--llm"] == f"studio:{MID}" and llm.props["--use-case"] == "coding"  # Big: too big for 64 GB
    assert agent.props["agent"] == "opencode"
    page.inspect(page.board.first("llm"))
    shown = page.use_case_text.text()
    assert f"Choice: studio:{MID} in OpenCode (rank 2)" in shown and "solved it 2 of 2 in OpenCode" in shown
    assert "Measured: round 4: debug 2/2, 2026-11-17" in shown and "fits: about 46 GB of 48 GB" in shown
    assert "Settings over the model profile: temperature 0.5" in shown
    box = page.use_case_box
    assert box.title() == "What we know: use case Coding and debugging"
    text = box.text()
    assert "✗ Does not work" in text and "Qwen Code: changed the tests" in text
    assert "servers other than LM Studio: not measured" in text
    page.undo.undo()
    assert page.board.state() == before


def test_the_card_and_the_profile_badge_say_the_use_case(page):
    from sandbox_ai_app.board_view import summary
    page.inspect(page.board.first("agent"))
    choose(page, "small-jobs")
    llm = page.board.first("llm")
    assert llm.props["--llm"] == f"studio:{QCN}" and page.board.first("agent").props["agent"] == "opencode"
    assert "use case small-jobs" in summary(llm, "opencode")
    profile, enabled, _ = MP.board_profile(page.sai, page.board, page.llm_servers)
    assert profile["params"]["temperature"] == 0.2 and profile["use_case"]["id"] == "small-jobs"
    assert MP.badge(profile) == "Model profile: qwen3-coder-next × opencode with use case small-jobs (verified)"   # round 8


def test_another_model_chosen_afterwards_stays_and_none_takes_the_use_case_off(page):
    page.inspect(page.board.first("agent"))
    choose(page, "coding")
    page.choose_llm_server({"name": "studio", "model": QCN, "only": True})
    llm = page.board.first("llm")
    assert llm.props["--llm"] == f"studio:{QCN}" and llm.props["--use-case"] == "coding"
    page.inspect(page.board.first("agent"))
    choose(page, "")
    assert "--use-case" not in page.board.first("llm").props


def test_a_use_case_without_a_choice_and_no_server_is_refused_with_words(page, monkeypatch):
    monkeypatch.setattr(page.sai.module("model_profiles"), "support", None, raising=False)  # no nearest either
    page.llm_servers = []
    page.inspect(page.board.first("agent"))
    before = page.board.state()
    choose(page, "code-review")
    assert page.board.state() == before
    assert any("A use case chooses among the models of a server of yours" in str(item) for item in widgets.SHOWN)


def test_the_real_launcher_takes_the_boards_use_case(page):
    page.inspect(page.board.first("agent"))
    choose(page, "coding")
    argv = B.to_argv(page.sai, page.board)
    assert argv[argv.index("--use-case") + 1] == "coding"
    done = subprocess.run([str(ROOT / "sandbox-ai"), "opencode", *argv, "--dry-run"], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)["llm"]
    assert plan["model"] == MID and plan["use_case"]["id"] == "coding" and plan["use_case"]["named"] is True
    assert plan["profile"]["use_case"]["id"] == "coding" and plan["profile"]["values"]["temperature"] == 0.5
    assert "use case coding" in done.stderr
    again = B.from_argv(page.sai, "opencode", argv)
    assert again.first("llm").props.get("--use-case") == "coding", "back from the command line (a project file)"


def test_the_servers_memory_and_this_machines_for_its_loopback(monkeypatch):
    monkeypatch.setattr(UC, "machine_memory", lambda: 32 * GB)
    found = UC.servers_of(servers() + [{"name": "here", "url": "http://127.0.0.1:1234", "models": ["m"]},
                                       {"name": "lan", "url": "http://192.168.1.9:1234", "models": ["m"]}])
    assert [s["memory_bytes"] for s in found] == [64 * GB, 32 * GB, None]
    assert found[0]["models"][2] == {"name": QCN, "size": 45 * GB, "loaded": True}
    assert UC.servers_of(found[1:2] and [{"name": "here", "url": "http://127.0.0.1:1234"}], local=False)[0][
        "memory_bytes"] is None  # another host's loopback is not this machine's


def test_an_ipv6_loopback_server_without_a_port_has_this_machines_memory(monkeypatch):
    """Review 3x-3: the app read a server's host by cutting at the last ':' - http://[::1]/v1 became "[:", not this
    machine's loopback (sandbox-ai's llm.parse_url says it is), so its fit was "not known" on the Board only."""
    monkeypatch.setattr(UC, "machine_memory", lambda: 32 * GB)
    found = UC.servers_of([{"name": "six", "url": "http://[::1]/v1", "models": ["m"]},
                           {"name": "six-port", "url": "http://[::1]:1234", "models": ["m"]},
                           {"name": "named", "url": "http://localhost", "models": ["m"]},
                           {"name": "lan", "url": "http://[fd00::9]:1234", "models": ["m"]}])
    assert [s["memory_bytes"] for s in found] == [32 * GB, 32 * GB, 32 * GB, None]


def test_the_new_texts_are_in_both_languages():
    assert labels.OPTION_LABELS["--use-case"][1].startswith("Einsatzzweck")
    assert "--use-case" in B.KINDS["llm"].options
    for key in ("Use case", "none: choose the agent and the model yourself", "measured on this use case's tasks",
                "Coding and debugging", "What we know: use case {name}", "Choice: {model} in {harness} ({where})"):
        assert i18n.TEXTS[key]["de"], key
