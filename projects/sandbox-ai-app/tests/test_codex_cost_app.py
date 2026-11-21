"""The System monitor's cost of a Codex run on a ChatGPT plan, end to end (user 2026-11-20: "systemmonitor erkennt nicht
codex subscription bei kosten"): Codex's real WebSocket and HTTP+SSE requests through the checkout's real connection
proxy to a fake chatgpt.com (tests/test_codex_subscription_cost.py), its spend log where the run's would be, then the
page offscreen against the checkout's `sandbox-ai top`, `stats usage` and `spend run`: the list's cost column and the
chosen run's Model spend box say "subscription" - an API-key run beside it says what it cost."""
import os
from pathlib import Path
import shutil
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend  # noqa: E402
from sandbox_ai_app.monitor_page import MonitorPage  # noqa: E402

REPO = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
CORE = REPO / "tests" / "test_codex_subscription_cost.py"
pytestmark = pytest.mark.skipif(not CORE.is_file() or not shutil.which("openssl") or
                                not sys.platform.startswith("linux"),
                                reason="needs the sandbox-ai checkout with its tests (SANDBOX_AI_HOME), openssl, Linux")
if CORE.is_file():
    sys.path.append(str(REPO / "tests"))
    import test_codex_subscription_cost as core  # noqa: E402
    from test_codex_subscription_cost import pki  # noqa: E402,F401 - a fixture

from test_monitor_page import GIB, env, live, register  # noqa: E402,F401 - env: a fixture


def test_the_monitor_says_subscription_for_a_codex_plan_run(qapp, env, pki, wait, tmp_path):  # noqa: F811
    register(env, "codex-api1", "api", "/p/api", agent="codex")
    register(env, "codex-key1", "key", "/p/key", agent="codex")
    live(env, "codex-api1", {"cpu": 0.5, "mem": 3 * GIB, "pids": 5})
    live(env, "codex-key1", {"cpu": 0.1, "mem": GIB, "pids": 4})
    live(env, "host", {"cpu_pct": 10.0, "cpus": 8, "load1": 0.5, "mem_total": 32 * GIB, "mem_avail": 20 * GIB})
    proxy, server = core.plan_run(tmp_path, pki, run="codex-api1", name="api", fallback=False)  # WebSocket only
    proxy.close()
    server.close()
    proxy, server, (status, completed) = core.api_run(tmp_path, pki, run="codex-key1", name="key", turns=1)
    proxy.close()
    server.close()
    assert " 101 " in status and len(completed) == 1
    page = MonitorPage(backend.SandboxAI())
    try:
        page.show()
        assert wait(lambda: page.history is not None and page.data is not None, timeout=60), page.status.text()
        items = {}

        def rows():
            items.clear()
            for index in range(page.tree.topLevelItemCount()):
                group = page.tree.topLevelItem(index)
                for child in range(group.childCount()):
                    items[group.child(child).text(0)] = group.child(child)
            return {name: item.text(9) for name, item in items.items()}
        assert wait(lambda: rows() == {"api": "subscription", "key": "USD 0.01"}, timeout=30), rows()
        page.tree.setCurrentItem(items["api"])
        assert wait(lambda: "so far" in page.spend_line.text(), timeout=60), page.spend_line.text()
        assert page.spend_line.text().startswith("api: subscription so far - 2 model requests"), \
            page.spend_line.text()
        page.tree.setCurrentItem(items["key"])
        assert wait(lambda: page.spend_line.text().startswith("key: USD 0.01 so far - 1 model requests"),
                    timeout=60), page.spend_line.text()
    finally:
        page.close()
        page.deleteLater()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
