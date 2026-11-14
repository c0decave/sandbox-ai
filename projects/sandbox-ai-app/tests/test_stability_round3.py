"""Stability round 3 (0.1.95): what a long use of the app met - each test failed before its fix."""
import os

import pytest

from sandbox_ai_app import backend

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def test_an_update_read_with_the_event_it_replaces_takes_its_row(qapp):
    """A tool call shown as running and its end (an update of the same id) read in one go - the whole log of a run
    when the Activity page is opened, the app restarted, a page left alone a while: each such call showed twice,
    as running and as ended, and both stayed for the rest of the window's life."""
    from sandbox_ai_app import activity_page
    model = activity_page.ActivityModel()
    running = {"id": "t1", "kind": "tool", "summary": "Bash ls · running", "time": "2026-11-13T10:00:00Z"}
    ended = {"id": "t1", "kind": "tool", "summary": "Bash ls", "update": True, "time": "2026-11-13T10:00:00Z"}
    other = {"id": "t2", "kind": "tool", "summary": "Read a.txt", "time": "2026-11-13T10:00:01Z"}
    model.add([running, other, ended])
    assert model.rowCount() == 2
    assert [model.record_at(row)["summary"] for row in range(2)] == ["Bash ls", "Read a.txt"]
    model.add([{**ended, "summary": "Bash ls · error"}])  # a later update still replaces it
    assert model.rowCount() == 2 and model.record_at(0)["summary"] == "Bash ls · error"
