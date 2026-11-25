"""The Board follows a session: "Show on the Board" on the Sessions page and on a terminal tab, and the Board showing
the tab and project of the session one comes from (user 2026-11-24: "wenn ich von einer aktiven Session, z.B. von
Terminals, auf das Board gehe, soll dort das passende Tab und Projekt angezeigt werden"; "bei Terminal und Sitzungen
einen Button 'Board zeigen'").

show(window, instance): the session's tab among the window's tabs opened (or selected), which shows its project's
board - one with nothing of its own (no project file, no change) built from the options the session was started with
(BoardPage.seed_start) -, the Board page shown and the Agent part selected. A session of another host: that host
chosen on the Board (beside Apply) and its folder opened there, as Open project folder… does for a host.

follow(window, previous): the same, quietly, for the session that was active on the page left - the current terminal
tab's on Terminals, the selected row's on Sessions.

No Qt widgets of its own: it drives the window (ui.MainWindow)."""
from __future__ import annotations

from PySide6.QtCore import QTimer

from . import applog, backend, short_notices
from .files import printable
from .i18n import tr

LOCAL = "local"
SOURCES = ("terminals", "sandboxes")  # the pages a session is active on
KINDS = ("session", "run")  # what has a board: a session, a one-off sandbox
SELECT_WAIT = 20000  # ms a board read from another host may take before its part is no longer selected


def folder_of(instance):
    """The project folder of INSTANCE (on its host): its project, else its run's workspace."""
    record = instance.record if isinstance(getattr(instance, "record", None), dict) else {}
    return str(getattr(instance, "project", "") or record.get("workspace") or "")


def why_not(instance):
    """Why "Show on the Board" cannot act on INSTANCE (None: it can): no start selected, an MCP server's sandbox, one
    without a project folder."""
    if instance is None:
        return tr("Select one session or sandbox first.")
    if getattr(instance, "kind", None) not in KINDS:
        return tr("Only a session or a sandbox has a board (not an MCP server's sandbox).")
    if not folder_of(instance):
        return tr("Its project folder is not known.")
    return None


def active(window, page):
    """The session active on PAGE ("terminals": the current tab's, "sandboxes": the selected row's), or None."""
    built = window.built(page)
    if built is None:
        return None
    try:
        if page == "terminals":
            view = built.tabs.currentWidget()
            return built.instance_of(view) if view is not None and hasattr(view, "tab_tag") else None
        return built.current()
    except (AttributeError, RuntimeError) as exc:  # a page half made, a tab gone meanwhile: nothing to follow
        applog.swallowed("board_follow", "active: nothing to follow", exc)
        return None


def follow(window, previous):
    """Coming from PREVIOUS (a page of SOURCES) to the Board: the tab and project of the session active there are the
    ones shown. Quiet: nothing asked or said, nothing changed when there is none."""
    if getattr(window, "follow_held", False):
        return False
    instance = active(window, previous)
    if why_not(instance) is not None:
        return False
    return show(window, instance, quiet=True)


def select_part(board, kind="agent"):
    """The part of KIND on the board shown, alone selected and in view; False when there is none."""
    node = board.board.first(kind)
    item = board.items.get(node.id) if node is not None else None
    if item is None:
        return False
    board.scene.clearSelection()
    item.setSelected(True)
    board.view.centerOn(item)
    return True


def select_when_read(board):
    """The Agent part selected once the board another host is asked for is shown (BoardPage.board_changed), at the
    latest SELECT_WAIT ms from now (then the wait ends: a later board is not touched)."""
    waiting = {"on": True}

    def changed():
        if waiting["on"] and board.remote_project is not None:
            waiting["on"] = False
            select_part(board)

    def over():
        waiting["on"] = False
        try:
            board.board_changed.disconnect(changed)
        except (RuntimeError, TypeError):  # gone, or not connected any more
            pass
    board.board_changed.connect(changed)
    QTimer.singleShot(SELECT_WAIT, board, over)


def show(window, instance, quiet=False):
    """See the module's text. True when the Board shows (or is reading) the session's project. QUIET: no question,
    no notice, the page as it is (the caller shows it)."""
    title = tr("Show on the Board")
    why = why_not(instance)
    if why is not None:
        if not quiet:
            short_notices.short_notice(window, title, why, warning=True)
        return False
    board, folder = window.board, folder_of(instance)
    host = getattr(instance, "host", LOCAL) or LOCAL
    applog.log("INFO", "board", f"the Board shows {printable(instance.name)}"
               + (f" on {printable(host)}" if host != LOCAL else "") + (" (followed)" if quiet else ""))
    if host == LOCAL:
        if backend.is_folder(folder) is False:
            if not quiet:
                short_notices.short_notice(window, title, tr("Its project folder {path} is not there any more.",
                                                             path=printable(folder)), warning=True)
            return False
        if board.host() != LOCAL:
            board.host_choice.choose(LOCAL)
        from .ui import instance_entry
        window.open_tab(instance_entry(instance))  # its tab: the project's board is the one shown (tab_changed)
        start = window.sai.session_start(instance.ident) if instance.kind == "session" else None
        if start is not None:  # a board with nothing of its own: what the session was started with
            board.seed_start(folder, *start)
        selected = select_part(board)
    else:
        if board.host() != host and not board.host_choice.choose(host):
            if not quiet:
                short_notices.short_notice(window, title, tr("{host} is not among the hosts the Board starts on.",
                                                             host=printable(host)), warning=True)
            return False
        if board.remote_project != (host, folder):
            if quiet and board.dirty_for(None):
                return False  # (its board without a folder has changes: asked only when the human asks for it)
            if not board.open_remote_project(folder):
                return False
            select_when_read(board)
        selected = select_part(board)
    if not quiet:
        window.follow_held = True  # (the page change below is this very command: nothing else to follow)
        try:
            window.show_page("board")
        finally:
            window.follow_held = False
    return bool(selected) or host != LOCAL
