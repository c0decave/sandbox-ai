#!/usr/bin/env python3
"""Pictures of the Help as the app shows it, for reviewing its look: every section (or those named) in a window made
offscreen from a demo state - a few sessions, questions, MCP and LLM servers made up here, no real data, no home
paths, no secrets - in a theme, a language and a scale.

  SANDBOX_AI_HOME=CHECKOUT python3 scripts/help_previews.py --out DIR [--theme light|dark] [--language en|de]
      [--scale 1|2] [--reading-font atkinson|opendyslexic|lexend] [--section ID ...] [--pages] [--full]

Each picture is DIR/ID-THEME-LANGUAGE-SCALEx.png: the Help page as the window shows it (--full: the section's whole
text, every fold closed). --pages takes the window's pages themselves instead (to check a reading font's layout).
Needs PySide6; runs with QT_QPA_PLATFORM=offscreen. Nothing is written but the pictures (and a temporary folder,
removed at the end).
"""
import argparse
import atexit
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

APP = Path(__file__).resolve().parents[1]


def demo_state(root):
    """A made-up state under ROOT: XDG folders, two runs (one a session) with open questions, a project folder."""
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        os.environ[name] = str(root / name.split("_")[1].lower())
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(root / "home")
    (root / "home").mkdir(exist_ok=True)
    os.environ["SANDBOX_AI_SECRETS"] = "file"
    os.environ["SANDBOX_AI_SECRETS_PLAIN"] = "1"
    os.environ["SANDBOX_AI_MCP_NO_IMAGE_CHECK"] = "1"
    project = root / "demo-project"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "app.py").write_text("print('hello')\n")
    (project / "README.md").write_text("# demo project\n")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for name, agent, session in (("claude-demo1", "claude", "web-shop"), ("codex-demo2", "codex", None)):
        run = Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs" / name
        (run / "approvals").mkdir(parents=True, exist_ok=True)
        (run / "run.json").write_text(json.dumps({
            "run": name, "agent": agent, "backend": "podman", "workspace": str(project), "pid": os.getpid(),
            "session": session, "session_dir": str(root / "s" / name), "started": now, "proxy": True}))
    questions = (("claude-demo1", "1700000000-1-1", {"kind": "connect", "destination": "198.51.100.7:443",
                                                     "address": "198.51.100.7", "port": 443,
                                                     "host": "registry.npmjs.org"}),
                 ("claude-demo1", "1700000000-1-2", {"kind": "dns", "name": "api.github.com"}),
                 ("codex-demo2", "1700000000-2-1", {"kind": "connect", "destination": "198.51.100.9:443",
                                                    "address": "198.51.100.9", "port": 443, "host": "pypi.org"}))
    for run, ident, question in questions:
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs" / run / "approvals" / f"{ident}.json"
        path.write_text(json.dumps({"id": ident, "time": now, "timeout": 300, **question}))
    return project


def patch_demo(backend):
    """The lists the pages show that are read from sandbox-ai's own commands: made up here."""
    servers = [("search", "running", "ghcr.io/example/search-mcp@sha256:…"),
               ("files", "stopped", "npx @modelcontextprotocol/server-filesystem /work"),
               ("browser", "stopped", "ghcr.io/example/browser-mcp@sha256:…")]
    llms = [{"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434", "model": "qwen3-coder:30b"},
            {"name": "studio", "kind": "lmstudio", "url": "http://192.0.2.10:1234", "model": "gpt-oss-20b"}]
    backend.launcher_processes = lambda *a, **k: []  # never the sandboxes that really run on this computer
    backend.SandboxAI.mcp_servers = lambda self: list(servers)
    backend.SandboxAI.llm_endpoints = lambda self: [dict(e) for e in llms]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--theme", default="light")
    parser.add_argument("--language", default="en", choices=("en", "de"))
    parser.add_argument("--scale", default="1")
    parser.add_argument("--reading-font", default="")
    parser.add_argument("--section", action="append", default=[])
    parser.add_argument("--pages", action="store_true", help="the window's pages instead of the Help")
    parser.add_argument("--full", action="store_true", help="the section's whole text instead of the window")
    parser.add_argument("--size", default="1280x880")
    parser.add_argument("--targets", action="store_true", help="list the controls on screen per page")
    args = parser.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    os.environ["SANDBOX_AI_APP_LANGUAGE"] = args.language
    # (not below a home folder: the pictures show the demo's paths)
    root = Path(tempfile.mkdtemp(prefix="sai-demo-", dir="/tmp" if os.access("/tmp", os.W_OK) else None))
    try:
        demo_state(root)
        sys.path.insert(0, str(APP))
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        from sandbox_ai_app import backend, fonts, help_content, i18n, reading_fonts, terminal, theme, widgets
        from sandbox_ai_app.ui import PAGES as NAV, MainWindow
        PAGES = [key for key, _title in NAV]
        i18n.set_language(args.language)
        terminal.available = lambda web=None: (False, "previews")
        widgets.AUTO_ANSWER = True
        patch_demo(backend)
        reading_fonts.register()
        settings = {"reading_font": args.reading_font}
        fonts.use(settings, app)
        theme.apply(args.theme, app)
        window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
        width, height = (int(n) for n in args.size.split("x"))
        window.resize(width, height)
        window.notify = lambda *a, **k: None
        window.show()
        app.processEvents()
        window.approvals.poll()  # the made-up questions on the Approvals page (read in the background)
        until = time.time() + 5
        while time.time() < until and not window.approvals.open.rowCount():
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        args.out.mkdir(parents=True, exist_ok=True)
        tag = f"{args.theme}-{args.language}-{args.scale}x" + (f"-{args.reading_font}" if args.reading_font else "")
        if args.targets:  # which controls of each page are on screen in the demo state
            from sandbox_ai_app import help_shots, tours
            for key in PAGES:
                named = sorted({t for t in [s.target for s in tours.AREA_TOURS.get(key, ())]
                                + [s.target for tour in tours.TASK_TOURS.values() for s in tour.steps
                                   if s.page == key] if t})
                _pixmap, _size, rects = help_shots.grab_page(window, key, named)
                print(key, "on screen:", sorted(rects), "not:", sorted(set(named) - set(rects)))
            return 0
        if args.pages:
            for key in args.section or PAGES:
                window.show_page(key)
                for _ in range(3):
                    app.processEvents()
                window.grab().save(str(args.out / f"screen-{key}-{tag}.png"))
            print(f"{len(args.section or PAGES)} pages in {args.out}")
            return 0
        ids = args.section or [s.id for s in help_content.SECTIONS]
        for ident in ids:
            window.show_help(ident)
            for _ in range(3):
                app.processEvents()
            name = args.out / f"{ident}-{tag}.png"
            if args.full:
                text = window.help.text
                document = text.document()
                ratio = float(args.scale)
                size = document.size()
                image = QImage(int(size.width() * ratio), int(size.height() * ratio), QImage.Format.Format_RGB32)
                image.setDevicePixelRatio(ratio)
                image.fill(text.palette().color(text.palette().ColorRole.Base))
                painter = QPainter(image)
                document.drawContents(painter)
                painter.end()
                image.save(str(name))
            else:
                window.grab().save(str(name))
        print(f"{len(ids)} sections in {args.out}")
        return 0
    finally:
        if "window" in locals():
            window.shutdown()
            for _ in range(60):  # its commands in the background end (they write their logs into the demo state)
                app.processEvents()
                time.sleep(0.05)
        shutil.rmtree(root, ignore_errors=True)
        # (and once more at the very end: commands still finishing in the background write their logs there)
        atexit.register(shutil.rmtree, root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
