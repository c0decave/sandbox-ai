#!/usr/bin/env python3
"""Screenshots of the desktop app for the learning videos, rendered offscreen.

    python3 docs/video/capture_app.py --app projects/sandbox-ai-app --out DIR/app-shots [--lang en|de|both]

Needs PySide6 (any Python that can `import PySide6`) and the app (projects/sandbox-ai-app). The app runs with
QT_QPA_PLATFORM=offscreen in a throwaway HOME and XDG folders (nothing of yours is read or changed), in its
default dark theme, against this checkout's sandbox-ai. A staged demo fills the pages: a project (~/src/shop) with
a few files, a staged run with two open questions and its logs (requests, refusals, a refused push, the agents'
timeline), a host, a kit, a secret, an identity and a model server, all made with this checkout's own commands
where one exists. The videos mark these screenshots as "staged demo data".

Besides one picture per page (PAGE-LANG.png) it takes the states listed in STATES (STATE-LANG.png): the board
with a preset, the preset gallery, the file manager's editor, context menu and a remote system in a pane (served by
the app's own test SFTP server, tests/fake_sftp_server.py, over this machine's files: no SSH connection is made),
the kill switch, a secret given to a running sandbox as a Git login, the settings, the log window and others. A dialog or menu is drawn over the window where the app would show it. A state the
app of this checkout cannot show (renamed, not there yet) is skipped with a warning; the video then falls back to
the page's own picture. make_video.py picks the files up from OUT/app-shots (or --app-shots DIR).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PAGES = ["launch", "board", "sandboxes", "approvals", "terminals", "files", "kits", "mcp", "llm", "images", "credentials",
         "team", "connections", "activity", "statistics", "system", "hosts", "help"]
# The extra states: name -> the page they are on. How each is made is in Capture.state_NAME (dashes as _).
STATES = {
    "launch-network": "launch", "board-preset": "board", "board-gallery": "board", "sandboxes-cut": "sandboxes",
    "approvals-blocked": "approvals", "connections-record": "connections", "files-markdown": "files",
    "mcp-properties": "mcp", "llm-parameters": "llm", "images-yours": "images",
    "activity-safeguards": "activity", "files-editor": "files",
    "files-menu": "files", "files-properties": "files", "kits-marketplaces": "kits", "images-extension": "images",
    "credentials-secrets": "credentials", "credentials-give": "credentials", "credentials-vpn": "credentials",
    "files-remote": "files", "system-settings": "system",
    "system-log": "system", "hosts-defaults": "hosts", "help-section": "help",
}
RUN = "claude-3f9a2b1c"
NAME = "brisk-otter-lantern"


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def stage(root, home=None):
    """The throwaway HOME (HOME, else ROOT/home/me) with a demo project, a staged run that asks two questions and
    the logs of that run."""
    home = Path(home) if home else root / "home" / "me"
    project = home / "src" / "shop"
    (project / "shop").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "README.md").write_text(
        "# shop\n\nA small web shop: `python -m shop` serves it on port 8080.\n\n## Prices\n\n"
        "| Item | Price |\n|---|---|\n| apple | 0.50 |\n| pear | 0.70 |\n| plum | 0.30 |\n\n## To do\n\n"
        "- [x] serve the prices\n- [ ] a price check, with a test\n- [ ] ~~a database~~ not yet\n\n"
        "## Run it\n\n```bash\npip install -r requirements.txt\npython -m pytest -q\npython -m shop\n```\n")
    (project / "shop" / "__init__.py").write_text("")
    (project / "shop" / "app.py").write_text(
        '"""The shop\'s web app."""\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n\n'
        'PRICES = {"apple": 0.5, "pear": 0.7, "plum": 0.3}\n\n\nclass Shop(BaseHTTPRequestHandler):\n'
        '    def do_GET(self):\n        item = self.path.strip("/")\n        price = PRICES.get(item)\n'
        '        self.send_response(200 if price is not None else 404)\n        self.end_headers()\n'
        '        self.wfile.write(f"{item}: {price}\\n".encode())\n\n\ndef main(port=8080):\n'
        '    HTTPServer(("127.0.0.1", port), Shop).serve_forever()\n')
    (project / "tests" / "test_app.py").write_text(
        "from shop.app import PRICES\n\n\ndef test_prices_are_positive():\n"
        "    assert all(price > 0 for price in PRICES.values())\n")
    (project / "requirements.txt").write_text("pytest==8.3.3\n")
    state = home / ".local" / "state"
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    logs = state / "sandbox-ai" / "logs" / f"{project.name}-{digest}"  # sandbox/logs.py default_log_dir
    run_dir = root / "runtime" / "sandbox-ai" / "runs" / RUN
    (run_dir / "approvals").mkdir(parents=True)
    os.chmod(root / "runtime", 0o700)
    (run_dir / "run.json").write_text(json.dumps({
        "run": RUN, "name": NAME, "agent": "claude", "backend": "podman", "workspace": str(project),
        "pid": os.getpid(), "session": None, "session_dir": str(root / "session"), "log_dir": str(logs),
        "started": stamp(600), "proxy": True}))

    def ask(ident, seconds_ago, **question):
        (run_dir / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "time": stamp(seconds_ago),
                                                                         "timeout": 60, **question}))

    ask("1762506000-41-1", 12, kind="connect", destination="203.0.113.9:443", address="203.0.113.9", port=443,
        host="api.example.com", protocol="tls")
    ask("1762506000-41-2", 4, kind="browser", url="https://login.example.com/device?code=WDJB-MJHT")
    stage_logs(logs, project)
    return home, project


def stage_logs(logs, project):
    """The connection log, the refusals, files, processes and the timeline of the staged run."""
    claude = {"pid": 812, "name": "claude"}
    usage = {"input_tokens": 5210, "output_tokens": 640, "cache_read_input_tokens": 18044}
    requests = []
    for i in range(14):
        requests.append({"time": stamp(560 - i * 30), "event": "http", "host": "api.anthropic.com", "method": "POST",
                         "url": "https://api.anthropic.com/v1/messages", "status": 200, "bytes": 18000 + i * 900,
                         "process": claude, "credentials": ["authorization"],
                         "model": {"model": "claude-opus-4", "usage": usage}})
    requests += [
        {"time": stamp(470), "event": "http", "host": "pypi.org", "method": "GET", "url": "https://pypi.org/simple/pytest/",
         "status": 200, "bytes": 41210, "process": {"pid": 901, "name": "pip"}},
        {"time": stamp(468), "event": "http", "host": "files.pythonhosted.org", "method": "GET",
         "url": "https://files.pythonhosted.org/packages/pytest-8.3.3-py3-none-any.whl", "status": 200,
         "bytes": 342000, "process": {"pid": 901, "name": "pip"}},
        {"time": stamp(400), "event": "http", "host": "github.com", "method": "GET",
         "url": "https://github.com/me/shop.git/info/refs?service=git-upload-pack", "status": 200, "bytes": 812,
         "process": {"pid": 950, "name": "git"}},
        {"time": stamp(300), "event": "denied", "destination": "198.51.100.7:22", "reason": "SSH",
         "process": {"pid": 977, "name": "ssh"}},
        {"time": stamp(250), "event": "http", "host": "api.github.com", "method": "POST",
         "url": "https://api.github.com/gists", "status": 403, "rule": "no-gists", "decision": "deny",
         "process": {"pid": 990, "name": "curl"}},
        {"time": stamp(200), "event": "http", "host": "github.com", "method": "POST",
         "url": "https://github.com/me/shop.git/git-receive-pack", "status": 403,
         "push": {"ok": False, "refs": ["refs/heads/main"], "findings": ["AWS access key in config.env"]},
         "process": {"pid": 1002, "name": "git"}},
    ]
    jsonl(logs / f"connections-1762505400-{RUN}.jsonl", requests)
    egress = [{"time": stamp(330), "host": "telemetry.example.net", "process": "node"},
              {"time": stamp(320), "address": "192.0.2.9", "protocol": "udp", "port": 53}]
    jsonl(logs / "egress-denied.jsonl", egress)
    try:  # what the run's safeguards refused, made by sandbox-ai's own code from the logs above
        import importlib.util
        spec = importlib.util.spec_from_file_location("video_safeguards", REPO / "sandbox" / "safeguards.py")
        safeguards = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(safeguards)
        redact = safeguards.Redactor()
        found = [safeguards.from_egress(e, RUN, redact) for e in egress]
        found += [safeguards.from_connection(e, RUN, redact) for e in requests]
        jsonl(logs / safeguards.FILE, [r for r in found if isinstance(r, dict)])
    except Exception as error:  # noqa: BLE001 - another version's safeguards: the tab stays empty
        print(f"capture_app: warning: no staged safeguards: {error!r}", file=sys.stderr)
    jsonl(logs / "files.jsonl", [{"time": stamp(420), "action": "write", "path": "/work/shop/app.py"},
                                 {"time": stamp(410), "action": "create", "path": "/work/tests/test_app.py"}])
    jsonl(logs / "processes.jsonl", [{"time": stamp(470 - i), "command": c} for i, c in enumerate(
        ["pip install -r requirements.txt", "python -m pytest -q", "git status", "git push origin main",
         "curl -X POST https://api.github.com/gists", "python -m pytest -q"])])

    def event(seconds_ago, kind, summary, **details):
        return {"v": 1, "time": stamp(seconds_ago), "run": RUN, "session": None, "agent": "claude", "kind": kind,
                "summary": summary, "details": details, "source": "sandbox-ai",
                "id": hashlib.sha256(f"{kind}{seconds_ago}{summary}".encode()).hexdigest()[:16]}

    jsonl(logs / "activity.jsonl", [
        event(600, "session", "claude started", event="start"),
        event(580, "prompt", "Add a price check to the shop and a test for it"),
        event(560, "reply", "I'll look at shop/app.py first, then add the test."),
        event(540, "tool", "Read shop/app.py"),
        event(470, "command", "pip install -r requirements.txt (exit 0, 3.1 s)", status=0),
        event(420, "file", "modified /work/shop/app.py"),
        event(410, "file", "created /work/tests/test_app.py"),
        event(400, "command", "python -m pytest -q (exit 0, 0.8 s)", status=0),
        event(300, "network", "denied: 198.51.100.7:22 (SSH) (ssh)", verdict="denied"),
        event(250, "network", "denied: POST https://api.github.com/gists (rule no-gists, curl)", verdict="denied"),
        event(200, "network", "push REFUSED: refs/heads/main (AWS access key in config.env)", verdict="denied"),
        event(12, "approval", "asked: connect 203.0.113.9:443 (api.example.com)"),
        event(10, "tokens", "turn: 5,210 in, 640 out, 18,044 from the cache"),
    ])


def demo_commands(env, cwd):
    """What the pages list besides the run: made with this checkout's own commands (offline ones only)."""
    sai = str(REPO / "sandbox-ai")
    steps = [
        ([sai, "hosts", "add", "ct1", "me@ct1.lan", "--description", "Proxmox 101"], None),
        ([sai, "kits", "new", "review"], None),
        ([sai, "kits", "new-skill", "review", "lint-fix", "--description", "Fixes lint errors; use after edits"], None),
        ([sai, "secrets", "set", "GITHUB_TOKEN"], "ghp_demo-not-a-real-token\n"),
        ([sai, "secrets", "set", "OLLAMA_KEY"], "demo-key\n"),
        ([sai, "identities", "add", "work", "--name", "Me", "--email", "me@example.org",
          "--description", "commits at work"], None),
        ([sai, "llm", "add", "gpu-box", "-k", "ollama", "-u", "http://192.168.178.20:11434", "-m", "qwen3-coder:30b"],
         None),
    ]
    for argv, stdin in steps:
        done = subprocess.run(argv, env=env, cwd=cwd, input=stdin, text=True, capture_output=True, timeout=120)
        if done.returncode != 0:
            print(f"capture_app: warning: {' '.join(argv[1:4])}: {(done.stderr or done.stdout).strip()[:200]}",
                  file=sys.stderr)


class Capture:
    """The app's window, offscreen, and the pictures of its pages and states."""

    def __init__(self, app_dir, out, lang, size):
        os.environ.update({"QT_QPA_PLATFORM": "offscreen", "SANDBOX_AI_APP_LANGUAGE": lang,
                           "SANDBOX_AI_APP_NO_INTRO": "1", "QTWEBENGINE_DISABLE_SANDBOX": "1"})
        sys.path.insert(0, str(app_dir))
        from PySide6.QtWidgets import QApplication
        self.qapp = QApplication.instance() or QApplication([])
        from sandbox_ai_app import backend, i18n, terminal, theme
        i18n.set_language(lang)
        terminal.available = lambda web=None: (False, "not in the video")
        theme.apply("dark")
        self.theme = theme
        from sandbox_ai_app import widgets
        if hasattr(widgets, "window_close_controls"):  # every extra window with its [x] at the top right (0.1.94)
            widgets.window_close_controls(self.qapp)
        self.app_dir = Path(app_dir)
        from sandbox_ai_app.ui import MainWindow
        self.window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        if self.window.tray is not None:
            self.window.tray.hide()
            self.window.tray = None
        self.window.resize(*size)
        self.window.show()
        self.out, self.lang = out, lang
        self.project = Path(os.environ["HOME"]) / "src" / "shop"
        self.window.set_project(str(self.project))
        self.extra = []  # windows to close after a state
        self.cleanup = []  # folders to remove at the end

    def settle(self, seconds=1.0):
        end = time.time() + seconds
        while time.time() < end:
            self.qapp.processEvents()
            time.sleep(0.02)

    def page(self, key):
        window = self.window
        window.nav.setCurrentRow(window.nav_rows[key])
        self.settle(0.8)
        return getattr(window, key)

    def save(self, name, image=None):
        self.window.statusBar().clearMessage()
        self.settle(0.3)
        image = image or self.window.grab().toImage()
        image.save(str(self.out / f"{name}-{self.lang}.png"))

    def over(self, widget, at=None, title=None):
        """The window's picture with WIDGET (a dialog, a menu) drawn over it: at AT (a QPoint in the window), else
        centred, with a title bar when TITLE is given (offscreen windows have no frame)."""
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QColor, QFont, QPainter
        self.settle(0.6)
        base = self.window.grab().toImage()
        # sizes in the window's own (logical) pixels: the pictures have devicePixelRatio more (--scale)
        width, height = self.window.width(), self.window.height()
        if title is not None:
            widget.resize(min(widget.width(), width - 60), min(widget.height(), height - 90))
            self.settle(0.3)
        shot = widget.grab().toImage()
        w, h = round(shot.width() / shot.devicePixelRatio()), round(shot.height() / shot.devicePixelRatio())
        bar = 30 if title is not None else 0
        if at is None:
            at = QPoint(max(20, (width - w) // 2), max(20, (height - h - bar) // 2))
        painter = QPainter(base)
        painter.fillRect(QRect(0, 0, width, height), QColor(0, 0, 0, 90 if title is not None else 0))
        painter.fillRect(QRect(at.x() + 8, at.y() + 10, w, h + bar), QColor(0, 0, 0, 120))
        if bar:
            colours = self.theme.colors()
            painter.fillRect(QRect(at.x(), at.y(), w, bar), QColor(colours.get("nav", "#121418")))
            font = QFont(painter.font())
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(colours.get("text", "#e6e8eb")))
            painter.drawText(QRect(at.x() + 14, at.y(), w - 28, bar), 0x0081, title)  # AlignLeft|VCenter
        painter.drawImage(QRect(at.x(), at.y() + bar, w, h), shot)
        painter.setPen(QColor("#6b727e"))
        painter.drawRect(QRect(at.x(), at.y(), w - 1, h + bar - 1))
        painter.end()
        return base

    def close_extra(self):
        for widget in self.extra:
            try:
                widget.close()
            except RuntimeError:
                pass
        self.extra = []
        self.settle(0.3)

    def run(self, only=None):
        """Every page and state; ONLY (names of pages and states): just these (a page also for its states)."""
        window = self.window
        self.settle(1.5)
        window.approvals.poll()
        (getattr(window.sandboxes, "refresh_soon", None) or window.sandboxes.refresh)()
        self.settle(3.0)
        made = 0
        for key in PAGES:
            if only and key not in only and not any(STATES.get(name) == key for name in only):
                continue
            if key not in window.nav_rows:
                print(f"capture_app: warning: the app has no page {key}", file=sys.stderr)
                continue
            page = self.page(key)
            prepare = getattr(self, f"page_{key}", None)
            if prepare:
                try:
                    prepare(page)
                except Exception:  # a page of another version: its plain picture still helps
                    traceback.print_exc()
            self.save(key)
            made += 1
            for name in [n for n, p in STATES.items() if p == key and (not only or n in only or key in only)]:
                try:
                    image = getattr(self, "state_" + name.replace("-", "_"))(self.page(key))
                    self.save(name, image)
                    made += 1
                except Exception as error:  # noqa: BLE001 - skipped, said, and the page's picture stands in
                    print(f"capture_app: warning: state {name} skipped: {error!r}", file=sys.stderr)
                finally:
                    self.close_extra()
        window.shutdown()
        for folder in self.cleanup:
            shutil.rmtree(folder, ignore_errors=True)
        print(f"capture_app: {self.lang}: {made} pictures in {self.out}")

    # -- the pages as they first show --

    def page_approvals(self, page):
        page.poll()
        self.settle(0.5)
        if page.open.rowCount():
            page.open.selectRow(0)
            self.settle(0.3)

    def page_sandboxes(self, page):
        self.select_run(page)

    def page_help(self, page):
        pass

    # -- the states --

    def select_run(self, page):
        from PySide6.QtWidgets import QAbstractItemView
        tree = page.table
        model = tree.model()
        for row in range(model.rowCount()):
            parent = model.index(row, 0)
            for child in range(model.rowCount(parent)):
                index = model.index(child, 0, parent)
                texts = " ".join(str(model.index(child, c, parent).data() or "") for c in range(model.columnCount(parent)))
                if RUN in texts or NAME in texts:
                    tree.setCurrentIndex(index)
                    tree.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
                    self.settle(0.5)
                    return True
        return False

    def tab(self, widget, number):
        widget.setCurrentIndex(number)
        self.settle(0.6)

    def state_launch_network(self, page):
        self.tab(page.form, 1)
        image = self.window.grab().toImage()
        self.tab(page.form, 0)
        return image

    def state_board_preset(self, page):
        page.new_from_preset("development")
        self.settle(1.5)
        agent = next((node for node in page.scene.items() if type(node).__name__ == "NodeItem"
                      and "agent" in str(getattr(node, "kind", "") or getattr(getattr(node, "part", None), "kind", ""))),
                     None)
        if agent is not None:
            page.scene.clearSelection()
            agent.setSelected(True)
            self.settle(0.5)
            show = getattr(page, "show_inspector", None)
            if show:
                show()
            self.settle(0.8)
        return self.window.grab().toImage()

    def state_board_gallery(self, page):
        from sandbox_ai_app.preset_gallery import PresetGallery
        dialog = PresetGallery(page.sai, directory=str(self.project), parent=self.window)
        self.extra.append(dialog)
        dialog.resize(1400, 780)
        dialog.show()
        self.settle(2.0)
        return self.over(dialog, title=dialog.windowTitle())

    def state_sandboxes_cut(self, page):
        from PySide6.QtCore import QPoint
        self.select_run(page)
        page.cut_button.setEnabled(True)
        menu = page.cut_menu
        menu.adjustSize()
        menu.ensurePolished()
        corner = page.cut_button.mapTo(self.window, QPoint(0, page.cut_button.height()))
        x = min(corner.x(), self.window.width() - menu.sizeHint().width() - 10)
        y = corner.y() if corner.y() + menu.sizeHint().height() < self.window.height() else \
            corner.y() - page.cut_button.height() - menu.sizeHint().height()
        menu.resize(menu.sizeHint())
        return self.over(menu, at=QPoint(x, y))

    def state_approvals_allowed(self, page):
        self.tab(page.sections, 1)
        image = self.window.grab().toImage()
        self.tab(page.sections, 0)
        return image

    def state_approvals_blocked(self, page):
        """The page scrolled down to the list of what the run was refused."""
        from PySide6.QtWidgets import QScrollArea
        self.page_approvals(page)
        area, widget = None, page
        while widget is not None and area is None:
            widget = widget.parentWidget()
            area = widget if isinstance(widget, QScrollArea) else None
        if area is None:
            raise LookupError("the Approvals page does not scroll")
        bar = area.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.settle(1.0)
        image = self.window.grab().toImage()
        bar.setValue(0)
        return image

    def state_connections_record(self, page):
        """The refused push selected: its whole record below the list."""
        model = page.table.model()
        for row in range(model.rowCount() - 1, -1, -1):
            texts = " ".join(str(model.index(row, c).data() or "") for c in range(model.columnCount()))
            if "REFUSED" in texts or "receive-pack" in texts:
                page.table.selectRow(row)
                break
        else:
            raise LookupError("no refused push in the connection log")
        self.settle(0.8)
        return self.window.grab().toImage()

    def dialog_of(self, action, seconds=3.0):
        """The dialog ACTION opens (with exec(): shown instead, so that nothing waits for an answer), once it has
        read what it reads in the background."""
        from PySide6.QtWidgets import QApplication, QDialog
        before = {id(w) for w in QApplication.topLevelWidgets()}
        original = QDialog.exec
        QDialog.exec = lambda dialog: (dialog.show(), 0)[1]
        try:
            action()
            end = time.time() + seconds + 90  # what it reads first runs in the background (sandbox-ai, slow when busy)
            found = None
            while time.time() < end and found is None:
                self.settle(0.3)
                found = next((w for w in QApplication.topLevelWidgets() if id(w) not in before and
                              isinstance(w, QDialog) and w.isVisible()), None)
        finally:
            QDialog.exec = original
        if found is None:
            raise LookupError("no dialog opened" + (f" ({self.window.statusBar().currentMessage()})"
                                                    if self.window.statusBar().currentMessage() else ""))
        self.extra.append(found)
        self.settle(seconds)
        return self.over(found, title=found.windowTitle())

    def select_named(self, table, name):
        model = table.model()
        for row in range(model.rowCount()):
            if str(model.index(row, 0).data() or "") == name:
                table.selectRow(row)
                self.settle(0.5)
                return
        raise LookupError(f"no {name} in the list")

    def state_mcp_properties(self, page):
        self.select_named(page.servers, "demo")
        return self.dialog_of(lambda: page.properties("demo"))

    def state_llm_parameters(self, page):
        self.select_named(page.servers, "gpu-box")
        made = []
        # it deletes the dialog once exec() returns: here the dialog is only shown
        page.open_params = lambda dialog, host, name: (made.append(dialog), dialog.show())
        page.parameters()
        end = time.time() + 90
        while not made and time.time() < end:
            self.settle(0.3)
        if not made:
            raise LookupError(f"no Parameters dialog: {page.status.text()}")
        dialog = made[0]
        self.extra.append(dialog)
        self.settle(2.0)
        return self.over(dialog, title=dialog.windowTitle())

    def state_images_yours(self, page):
        for number in range(page.tabs.count()):
            if page.tabs.tabText(number) in ("Your images", "Eigene Images"):
                self.tab(page.tabs, number)
                self.settle(1.0)
                image = self.window.grab().toImage()
                self.tab(page.tabs, 0)
                return image
        raise LookupError("no tab Your images")

    def state_files_markdown(self, page):
        pane, entry = self.files_entry(page, "README.md")
        page.activate(pane)
        before = list(page.editors)
        pane.open_entry(entry)
        self.settle(1.5)
        viewer = next(w for w in page.editors if w not in before)
        self.extra.append(viewer)
        viewer.resize(1100, 680)
        self.settle(0.8)
        return self.over(viewer, title=viewer.windowTitle())

    def state_activity_safeguards(self, page):
        self.tab(page.tabs, 2)
        image = self.window.grab().toImage()
        self.tab(page.tabs, 0)
        return image

    def files_entry(self, page, name):
        pane = page.left
        for _ in range(30):
            if pane.entries:
                break
            self.settle(0.2)
        for row in range(pane.table.model().rowCount()):
            entry = pane.entry_at(row)
            if entry is not None and entry.name == name:
                pane.table.selectRow(row)
                self.settle(0.3)
                return pane, entry
        raise LookupError(f"no {name} in the sandbox pane")

    def state_files_editor(self, page):
        pane, entry = self.files_entry(page, "shop")
        pane.open_entry(entry)
        self.settle(1.0)
        pane, entry = self.files_entry(page, "app.py")
        page.activate(pane)
        before = list(page.editors)
        pane.open_entry(entry)
        self.settle(1.2)
        editor = next(w for w in page.editors if w not in before)
        self.extra.append(editor)
        editor.resize(1100, 640)
        self.settle(0.6)
        bar = getattr(editor, "find_bar", None)
        if bar is not None:  # the find bar (0.1.94): a word found, and what replaces it
            bar.find.setText("price")
            bar.open(replace=True)
            if hasattr(bar, "replacement"):
                bar.replacement.setText("cost")
            self.settle(0.6)
        image = self.over(editor, title=editor.windowTitle())
        pane.go("")
        self.settle(0.8)
        return image

    def state_files_menu(self, page):
        from PySide6.QtCore import QPoint
        pane, entry = self.files_entry(page, "README.md")
        page.activate(pane)
        menu = page.menu(pane)
        self.extra.append(menu)
        menu.adjustSize()
        menu.resize(menu.sizeHint())
        rect = pane.table.visualRect(pane.table.model().index(pane.table.currentIndex().row(), 0))
        at = pane.table.viewport().mapTo(self.window, rect.center() + QPoint(40, 0))
        at.setY(min(at.y(), self.window.height() - menu.height() - 10))
        return self.over(menu, at=at)

    def state_files_properties(self, page):
        pane, entry = self.files_entry(page, "shop")  # a folder: its size is counted (0.1.94)
        dialog = page.properties(pane, [entry])
        self.extra.append(dialog)
        self.settle(1.5)
        return self.over(dialog, title=dialog.windowTitle())

    def state_files_remote(self, page):
        """A remote system in the sandbox pane: the app's test SFTP server (tests/fake_sftp_server.py) serves a staged
        folder of this machine as the remote HOME of deploy@web1.example.org; ssh is never run."""
        from PySide6 import QtWidgets
        server = self.app_dir / "tests" / "fake_sftp_server.py"
        if not hasattr(page, "remote_dialog") or not server.is_file():
            raise LookupError("the file manager of this app has no remote panes")
        home = Path(tempfile.mkdtemp(prefix="deploy-"))
        self.cleanup.append(home)
        (home / "shop" / "releases" / "2026-11-10").mkdir(parents=True)
        (home / "shop" / "releases" / "2026-11-12").mkdir()
        (home / "shop" / "logs").mkdir()
        (home / "shop" / "logs" / "access.log").write_text("".join(
            f"203.0.113.{i} - - [12/Nov/2026:10:{i:02d}:00 +0000] \"GET /apple HTTP/1.1\" 200 12\n" for i in range(40)))
        (home / "shop" / "shop.service").write_text("[Unit]\nDescription=shop\n\n[Service]\nExecStart=/usr/bin/python3 -m shop\n")
        (home / "shop" / "README.md").write_text("# shop on web1\n\nDeployed by hand: copy a release, restart.\n")
        (home / "shop" / "current").symlink_to("releases/2026-11-12")
        (home / "backup.tar.gz").write_bytes(bytes(range(256)) * 300)
        os.environ["FAKE_SFTP_HOME"] = str(home)
        page.ssh_program = [sys.executable, "-I", str(server)]
        original = QtWidgets.QInputDialog.getItem
        QtWidgets.QInputDialog.getItem = staticmethod(lambda *a, **k: ("deploy@web1.example.org", True))
        try:
            if not page.remote_dialog():
                raise LookupError(f"no connection: {page.message.text()}")
        finally:
            QtWidgets.QInputDialog.getItem = original
        left = page.left
        end = time.time() + 30
        while time.time() < end and not (page.remotes and left.remote() and left.listing is None and left.entries):
            self.settle(0.3)
        if not left.remote():
            raise LookupError(f"not connected: {page.message.text()}")
        left.go_path(str(home / "shop"))
        page.right.go_path(str(self.project))
        self.settle(1.5)
        image = self.window.grab().toImage()
        for root in page.remotes.values():
            root.close()
        return image

    def state_kits_marketplaces(self, page):
        self.tab(page.tabs, 1)
        self.settle(1.5)
        image = self.window.grab().toImage()
        self.tab(page.tabs, 0)
        return image

    def state_images_extension(self, page):
        self.tab(page.tabs, 1)
        image = self.window.grab().toImage()
        self.tab(page.tabs, 0)
        return image

    def credentials_tab(self, page, words):
        for number in range(page.tabs.count()):
            if any(word in page.tabs.tabText(number) for word in words):
                self.tab(page.tabs, number)
                image = self.window.grab().toImage()
                self.tab(page.tabs, 0)
                return image
        raise LookupError(f"no tab {words}")

    def state_credentials_secrets(self, page):
        return self.credentials_tab(page, ("Secrets", "Geheimnisse"))

    def state_credentials_give(self, page):
        """A secret given to the staged run: for a Git server's host the dialog offers it as a Git login."""
        from types import SimpleNamespace
        from sandbox_ai_app.credentials_page import GiveDialog
        for number in range(page.tabs.count()):
            if any(word in page.tabs.tabText(number) for word in ("Secrets", "Geheimnisse")):
                self.tab(page.tabs, number)
        run = SimpleNamespace(run=RUN, name=NAME, agent="claude", state="running", kind="run")
        dialog = GiveDialog(self.window, [run], secret="GITHUB_TOKEN", git_hosts=("github.com",))
        self.extra.append(dialog)
        dialog.host.setText("github.com")
        dialog.show()
        self.settle(0.5)
        # The note above the choice wraps; its row became visible after the dialog's layout was made, so it is given
        # the height its lines need (offscreen it was cut to one line).
        from PySide6.QtWidgets import QLabel
        for label in dialog.findChildren(QLabel):
            if label.wordWrap() and label.isVisible() and label.width() > 0:
                label.setMinimumHeight(label.heightForWidth(label.width()))
        self.settle(0.3)
        dialog.adjustSize()
        self.settle(0.8)
        image = self.over(dialog, title=dialog.windowTitle())
        self.tab(page.tabs, 0)
        return image

    def state_credentials_vpn(self, page):
        return self.credentials_tab(page, ("VPN",))

    def state_system_settings(self, page):
        dialog = self.window.settings_dialog()
        self.extra.append(dialog)
        dialog.show()
        self.settle(1.0)
        # Since 0.1.94 the settings are in tabs: the one with the notifications (off by default) and the terminals
        # reopened at start, scrolled to them.
        from PySide6.QtWidgets import QCheckBox, QScrollArea
        from sandbox_ai_app.i18n import tr
        tabs = getattr(dialog, "tabs", None)
        boxes = {box.text(): box for box in dialog.findChildren(QCheckBox)}
        box = boxes.get(tr("Show notifications (popups)"))
        if tabs is not None and box is not None:
            dialog.resize(max(dialog.width(), 980), max(dialog.height(), 780))
            for number in range(tabs.count()):
                scroll = tabs.widget(number)
                if isinstance(scroll, QScrollArea) and scroll.widget() is not None and \
                        scroll.widget().isAncestorOf(box):
                    tabs.setCurrentIndex(number)
                    self.settle(0.5)
                    scroll.verticalScrollBar().setValue(max(0, box.mapTo(scroll.widget(), box.rect().topLeft()).y() - 40))
                    break
            self.settle(0.8)
        return self.over(dialog, title=dialog.windowTitle())

    def state_system_log(self, page):
        from sandbox_ai_app import applog
        for level, where, text in (("INFO", "sandboxes", f"started claude in a terminal tab: {NAME}"),
                                   ("INFO", "kits", "sandbox-ai plugins claude: 3 plugins installed into the store"),
                                   ("WARNING", "images", "podman is not installed here: the images cannot be listed"),
                                   ("INFO", "approvals", "allowed once: connect 203.0.113.9:443 (api.example.com)")):
            try:
                applog.log(level, where, text)
            except Exception:  # noqa: BLE001 - another app's log: shown as it is
                break
        window = self.window.open_log_window()
        self.extra.append(window)
        window.resize(1100, 520)
        self.settle(0.8)
        return self.over(window, title=window.windowTitle())

    def state_hosts_defaults(self, page):
        if page.table.model().rowCount():
            page.table.selectRow(0)
        self.settle(0.5)
        self.tab(page.tabs, 2)
        image = self.window.grab().toImage()
        self.tab(page.tabs, 0)
        return image

    def state_help_section(self, page):
        page.show_section("page-files")
        self.settle(0.8)
        return self.window.grab().toImage()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", required=True, help="the app: projects/sandbox-ai-app of this checkout")
    parser.add_argument("--out", required=True, help="folder for the PNG files (created)")
    parser.add_argument("--lang", choices=["en", "de", "both"], default="both")
    parser.add_argument("--size", default="1600x900", help="window size (default 1600x900)")
    parser.add_argument("--scale", default="1.5", help="device pixels per pixel of the window (QT_SCALE_FACTOR; "
                        "default 1.5: 2400 x 1350 pictures, sharp when a video zooms into a part)")
    parser.add_argument("--home", help="the demo HOME to show in the pages, e.g. /home/me (must not exist; removed "
                                       "afterwards); default: inside a temporary folder")
    parser.add_argument("--only", help="only these pages and states, comma-separated (e.g. files,files-editor); the "
                                       "others in --out stay as they are")
    parser.add_argument("--one", help=argparse.SUPPRESS)  # a child process: one language, environment prepared
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    size = tuple(int(v) for v in args.size.lower().split("x"))
    if args.one:
        Capture(Path(args.app).resolve(), out, args.one, size).run(set(args.only.split(",")) if args.only else None)
        return 0
    status = 0
    for lang in (["en", "de"] if args.lang == "both" else [args.lang]):
        root = Path(tempfile.mkdtemp(prefix="sai-demo-"))
        try:
            if args.home and Path(args.home).exists():
                raise SystemExit(f"capture_app: {args.home} exists already; give a demo HOME that does not")
            home, project = stage(root, args.home)
            env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_STATE_HOME": str(home / ".local" / "state"), "XDG_DATA_HOME": str(home / ".local" / "share"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "XDG_RUNTIME_DIR": str(root / "runtime"),
                   "SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_HOME": str(REPO), "SANDBOX_AI_NO_DEPRECATION": "1",
                   "PATH": f"{REPO}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
                   "QT_SCALE_FACTOR": args.scale}
            demo_commands(env, str(home))
            # the child must not see this process's run directory as its own (it stages its pid there)
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--app", str(Path(args.app).resolve()),
                                    "--out", str(out), "--size", args.size, "--one", lang]
                                   + (["--only", args.only] if args.only else []), env=env, cwd=str(home))
            status = status or child.returncode
        finally:
            shutil.rmtree(root, ignore_errors=True)
            if args.home:
                shutil.rmtree(args.home, ignore_errors=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
