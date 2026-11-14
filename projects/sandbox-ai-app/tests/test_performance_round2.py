"""Performance round 2 (0.1.95) of the app: what a long Activity timeline no longer asks Qt for at every read - counted,
never timed, and compared with what Qt's own calls give."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from sandbox_ai_app import activity_page, tables  # noqa: E402
from sandbox_ai_app.activity_page import ActivityFilter, ActivityModel  # noqa: E402


def events(count):
    kinds = ["command", "tool", "reply", "file", "prompt", "network", "odd-kind"]
    return [{"time": f"2026-11-13T10:{i // 60 % 60:02d}:{i % 60:02d}Z", "kind": kinds[i % len(kinds)],
             "agent": "claude", "summary": f"step {i} " + "x" * (i % 37), "details": {"n": i}, "id": f"e{i}"}
            for i in range(count)]


def timeline(count):
    model = ActivityModel()
    model.add(events(count))
    proxy = ActivityFilter()
    proxy.setSourceModel(model)
    return model, proxy


@pytest.mark.parametrize("kinds,text", [(None, None), ({"command", "tool"}, None), (None, "step 1"),
                                        ({"reply"}, "x"), (set(), None), ({"command", "tool", "reply", "file",
                                                                         "prompt", "network"}, "")])
def test_the_filters_texts_are_what_its_cells_show(qapp, kinds, text):
    model, proxy = timeline(300)
    proxy.set_filter(kinds, text)
    for row in range(proxy.rowCount()):
        for column in range(proxy.columnCount()):
            assert proxy.display_text(row, column) == proxy.data(proxy.index(row, column), Qt.ItemDataRole.DisplayRole)
    assert proxy.display_text(proxy.rowCount(), 0) is None and proxy.display_text(0, 9) is None


def test_a_long_timeline_is_fitted_without_asking_for_each_cell(qapp, monkeypatch):
    """The Activity page fits its columns after every read: the 200 rows sampled were asked for through the filter, an
    index in the filter and one in the timeline for each of the 800 cells (a third of the time 100,000 events took to
    read). Now the filter says the texts (display_text) and only the longest of each column are asked for through Qt,
    for their font; the widths are the same."""
    model, proxy = timeline(5000)
    view = QtWidgets.QTableView()
    view.setModel(proxy)
    tables.setup(view, stretch=3, compact=(0, 1, 2))
    view.resize(900, 400)
    asked = []
    data = ActivityModel.data
    monkeypatch.setattr(ActivityModel, "data", lambda self, index, role=Qt.ItemDataRole.DisplayRole:
                        asked.append(role) or data(self, index, role))
    tables.fit_columns(view, force=True)
    widths = [view.columnWidth(column) for column in range(4)]
    assert asked.count(Qt.ItemDataRole.DisplayRole) <= 4 * tables.LONGEST + 8, len(asked)
    monkeypatch.delattr(ActivityFilter, "display_text")  # as Qt alone would measure them
    tables.fit_columns(view, force=True)
    assert [view.columnWidth(column) for column in range(4)] == widths
    view.deleteLater()


def test_every_kind_is_one_the_filter_knows():
    assert activity_page.ALL_KINDS == frozenset(activity_page.KINDS)


def test_the_llm_traffic_list_is_fitted_from_its_texts(qapp, monkeypatch):
    """The LLM traffic page fits its columns after every read too: 200 rows of 8 columns, each cell asked through Qt.
    Now TrafficModel says its texts (display_text) - the ones data() gives - and the widths stay."""
    from sandbox_ai_app.traffic_page import COLUMNS, TrafficModel
    model = TrafficModel()
    model.add([{"n": i, "run": "r", "time": f"2026-11-13T10:00:{i % 60:02d}.250Z", "model": "m" * (i % 30 + 1),
                "tokens_in": i, "tokens_out": i % 7, "ms": i * 13, "status": 200 if i % 9 else 500, "new": 1,
                "messages": i, "loop": ["repeat"] if i % 11 == 0 else []} for i in range(3000)])
    for row in range(0, model.rowCount(), 7):
        for column in range(len(COLUMNS)):
            assert model.display_text(row, column) == model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)
    assert model.display_text(model.rowCount(), 0) is None and model.display_text(0, len(COLUMNS)) is None
    view = QtWidgets.QTableView()
    view.setModel(model)
    tables.setup(view, stretch=2)
    view.resize(900, 400)
    asked = []
    data = TrafficModel.data
    monkeypatch.setattr(TrafficModel, "data", lambda self, index, role=Qt.ItemDataRole.DisplayRole:
                        asked.append(role) or data(self, index, role))
    tables.fit_columns(view, force=True)
    widths = [view.columnWidth(column) for column in range(len(COLUMNS))]
    assert asked.count(Qt.ItemDataRole.DisplayRole) <= len(COLUMNS) * (tables.LONGEST + 1), len(asked)
    monkeypatch.delattr(TrafficModel, "display_text")
    tables.fit_columns(view, force=True)
    assert [view.columnWidth(column) for column in range(len(COLUMNS))] == widths
    view.deleteLater()
