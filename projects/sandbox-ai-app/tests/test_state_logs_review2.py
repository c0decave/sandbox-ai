"""Review 2 of the logs (docs/reviews/state-logs-review-2.md), the app's side: a connection log's body written once and
then as a reference ("same_as") is shown in full on the Connections page - from the records read, else from the log
file -, and the storage overview says what compressing saves as gc --usage measured it."""
import gzip
import json

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend  # noqa: E402

BODY = "system prompt and tools " * 400


def exchange(number, body):
    return {"time": f"2026-11-16T10:00:{number:02d}Z", "connection": number, "event": "http", "method": "POST",
            "url": "https://api.anthropic.com/v1/messages", "status": 200,
            "request": {"headers": [], "body": body}, "response": {"headers": [], "body": {"size": 2, "text": "ok"}}}


def first():
    return exchange(1, {"size": 9600, "truncated": False, "text": BODY, "digest": "d" * 32})


def again(number=2):
    return exchange(number, {"size": 9600, "truncated": False,
                             "same_as": {"connection": 1, "time": "2026-11-16T10:00:01Z", "digest": "d" * 32}})


def test_a_reference_is_filled_from_the_records_read():
    shown, left = backend.with_bodies(again(), [first(), again()])
    assert left == 0 and shown["request"]["body"]["text"] == BODY
    assert "connection 1" in shown["request"]["body"]["same_as"]["note"]
    assert "text" not in again()["request"]["body"]  # the record itself is left as it was
    plain = exchange(3, {"size": 2, "text": "hi"})
    assert backend.with_bodies(plain, []) == (plain, 0)


def test_a_reference_beyond_what_was_read_is_found_in_the_log_file(tmp_path):
    for name, opener in (("connections-20261116-100000-r.jsonl", open),
                         ("connections-20261116-100000-r.jsonl.1.gz", gzip.open)):
        path = tmp_path / name
        with opener(path, "wt") as stream:
            for record in (first(), *(exchange(n, {"size": 1, "text": "x"}) for n in range(5, 50)), again(60)):
                stream.write(json.dumps(record) + "\n")
        shown, left = backend.with_bodies(again(60), [again(60)], path)
        assert left == 0 and shown["request"]["body"]["text"] == BODY
    missing, left = backend.with_bodies(again(), [], tmp_path / "connections-20261116-100000-other.jsonl")
    assert left == 1 and "not found there" in missing["request"]["body"]["same_as"]["note"]


def test_the_connections_page_shows_the_body_a_reference_names(qapp, wait, monkeypatch, tmp_path):
    from sandbox_ai_app import ui
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "connections-20261116-100000-r.jsonl"
    filler = [exchange(n, {"size": 1, "text": "x" * 40000}) for n in range(5, 5 + (backend.LOG_BYTES // 40000) + 20)]
    log.write_text("".join(json.dumps(r) + "\n" for r in (first(), *filler, again(1000))))  # the first record is
    # beyond what the page reads (its newest LOG_BYTES)
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "log_directories", lambda: [logs])
    page = ui.ConnectionsPage(sai)
    page.show()
    assert wait(lambda: page.table.rowCount() > 0)
    assert all(record.get("connection") != 1 for record in page.records)
    page.table.selectRow(page.table.rowCount() - 1)
    page.details()
    assert wait(lambda: BODY[:100] in page.detail.toPlainText())
    assert "logged once, with connection 1" in page.detail.toPlainText()
    page.close()


def test_the_storage_overview_says_what_compressing_saves():
    from sandbox_ai_app import ui
    if not hasattr(ui, "usage_lines"):
        pytest.skip("no storage overview in this app")
    lines = "\n".join(ui.usage_lines({"would_free": {"compress": {"bytes": 4 << 30, "saves": 3 << 30}}}))
    assert "4 GiB of logs compressed, about 3 GiB freed" in lines and "quarter" not in lines
