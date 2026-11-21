"""Deep test round 3 of 0.1.96 in the app (chaos, concurrency and hostile input): the words the app shows for what the
launcher says after an interruption (a broker killed, a connection across hosts gone silent), in German too, and the
rows of `bridge list --json` the launcher writes for such bridges."""
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import bridges as BR, i18n  # noqa: E402


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_bridge_ended_by_a_killed_broker_or_a_silent_connection_is_said_in_german():
    """The reasons the launcher gives now for a bridge whose broker was killed and for a connection across hosts that
    went silent stayed English in a German app (bridge list's reason, the activity's "ended" event)."""
    i18n.set_language("de")
    assert BR.reason_text("its broker had ended without closing it (killed, or the machine restarted)") == \
        "ihr Vermittler hatte geendet, ohne sie zu schließen (beendet, oder der Rechner wurde neu gestartet)"
    assert BR.reason_text("its broker is gone without closing it: sandbox-ai bridge revoke 0123abcd closes what it "
                          "left") == "ihr Vermittler ist weg, ohne sie zu schließen: sandbox-ai bridge revoke " \
                                     "0123abcd schließt, was er hinterlassen hat"
    assert BR.reason_text("nothing was heard from lab1 for 76 s (the connection is gone)") == \
        "von lab1 kam 76 s lang nichts (die Verbindung ist weg)"
    record = {"kind": "bridge", "summary": "bridge 0123abcd ended: nothing was heard from the other host for 80 s "
                                           "(the connection is gone)", "details": {"event": "ended"}}
    assert BR.event_summary(record) == "Brücke 0123abcd beendet: von the other host kam 80 s lang nichts (die " \
                                       "Verbindung ist weg)"


def test_a_row_of_a_bridge_whose_broker_is_gone_is_no_live_bridge():
    """bridge list --json gives such a bridge as ended (with orphaned true): the rows of its sessions are not marked
    as bridged."""
    rows = [{"id": "0123abcd", "state": "ended", "orphaned": True, "a": "alpha", "b": "beta", "a_run": "session-alpha",
             "b_run": "session-beta", "a_to_b": "message", "b_to_a": "-", "reason": "its broker is gone"}]
    assert BR.live(rows) == [] and BR.marker(rows, "session-alpha") == ""
