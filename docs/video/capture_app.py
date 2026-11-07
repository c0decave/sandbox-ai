#!/usr/bin/env python3
"""Screenshots of the desktop app's pages for the learning video, rendered offscreen.

    python3 docs/video/capture_app.py --app ~/src/sandbox-ai-app --out DIR/app-shots [--lang en|de|both]

Needs PySide6 (any Python that can `import PySide6`) and a checkout of sandbox-ai-app. The app runs with
QT_QPA_PLATFORM=offscreen in a throwaway HOME and XDG folders (nothing of yours is read or changed), in its
default dark theme, against this checkout's sandbox-ai. A staged demo project (~/src/shop) and a staged run
with two open questions fill the Board, the Sandboxes page and the Approvals page; the video marks these
screenshots as "staged demo data". make_video.py picks the files up from OUT/app-shots (or --app-shots DIR).
Without PySide6 the video draws the pages as schematics instead.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PAGES = ["launch", "board", "sandboxes", "approvals", "terminals", "connections", "activity", "statistics", "files",
         "kits", "mcp", "images", "credentials", "system", "hosts"]


def stage(root, home=None):
    """The throwaway HOME (HOME, else ROOT/home/me) with a demo project and a staged run that asks two questions."""
    home = Path(home) if home else root / "home" / "me"
    project = home / "src" / "shop"
    project.mkdir(parents=True)
    (project / "README.md").write_text("# shop\n")
    run_dir = root / "runtime" / "sandbox-ai" / "runs" / "claude-shop-3f9a"
    (run_dir / "approvals").mkdir(parents=True)
    os.chmod(root / "runtime", 0o700)
    (run_dir / "run.json").write_text(json.dumps({
        "run": "claude-shop-3f9a", "agent": "claude", "backend": "podman", "workspace": str(project),
        "pid": os.getpid(), "session": None, "session_dir": str(root / "session"),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600)), "proxy": True}))

    def ask(ident, seconds_ago, **question):
        asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))
        (run_dir / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "time": asked, "timeout": 60,
                                                                         **question}))

    ask("1762506000-41-1", 12, kind="connect", destination="203.0.113.9:443", address="203.0.113.9", port=443,
        host="api.example.com", protocol="tls")
    ask("1762506000-41-2", 4, kind="browser", url="https://login.example.com/device?code=WDJB-MJHT")
    return home, project


def capture(app_dir, out, lang, size):
    os.environ.update({"QT_QPA_PLATFORM": "offscreen", "SANDBOX_AI_APP_LANGUAGE": lang, "SANDBOX_AI_APP_NO_INTRO": "1",
                       "QTWEBENGINE_DISABLE_SANDBOX": "1"})
    sys.path.insert(0, str(app_dir))
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from sandbox_ai_app import backend, i18n, terminal, theme
    i18n.set_language(lang)
    terminal.available = lambda web=None: (False, "not in the video")
    theme.apply("dark")
    from sandbox_ai_app.ui import MainWindow
    window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if window.tray is not None:
        window.tray.hide()
        window.tray = None
    window.resize(*size)
    window.show()
    project = Path(os.environ["HOME"]) / "src" / "shop"
    window.set_project(str(project))

    def settle(seconds=1.0):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    settle(1.5)
    window.approvals.poll()
    window.sandboxes.refresh()
    settle(3.0)
    window.statusBar().clearMessage()
    for key in PAGES:
        if key not in window.nav_rows:
            continue
        window.nav.setCurrentRow(window.nav_rows[key])
        settle(0.8)
        if key == "approvals":
            window.approvals.poll()
            settle(0.5)
            if window.approvals.open.rowCount():
                window.approvals.open.selectRow(0)
                settle(0.3)
        window.statusBar().clearMessage()
        settle(0.2)
        window.grab().save(str(out / f"{key}-{lang}.png"))
    window.shutdown()
    print(f"capture_app: {lang}: {len(PAGES)} pages in {out}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", required=True, help="a checkout of sandbox-ai-app")
    parser.add_argument("--out", required=True, help="folder for the PNG files (created)")
    parser.add_argument("--lang", choices=["en", "de", "both"], default="both")
    parser.add_argument("--size", default="1600x900", help="window size (default 1600x900)")
    parser.add_argument("--home", help="the demo HOME to show in the pages, e.g. /home/me (must not exist; removed "
                                       "afterwards); default: inside a temporary folder")
    parser.add_argument("--one", help=argparse.SUPPRESS)  # a child process: one language, environment prepared
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    size = tuple(int(v) for v in args.size.lower().split("x"))
    if args.one:
        capture(Path(args.app).resolve(), out, args.one, size)
        return 0
    status = 0
    for lang in (["en", "de"] if args.lang == "both" else [args.lang]):
        root = Path(tempfile.mkdtemp(prefix="sai-demo-"))
        try:
            if args.home and Path(args.home).exists():
                raise SystemExit(f"capture_app: {args.home} exists already; give a demo HOME that does not")
            home, _ = stage(root, args.home)
            env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_STATE_HOME": str(home / ".local" / "state"), "XDG_DATA_HOME": str(home / ".local" / "share"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "XDG_RUNTIME_DIR": str(root / "runtime"),
                   "SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_HOME": str(REPO),
                   "PATH": f"{REPO}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"}
            # the child must not see this process's run directory as its own (it stages its pid there)
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--app", str(Path(args.app).resolve()),
                                    "--out", str(out),
                                    "--size", args.size, "--one", lang], env=env, cwd=str(home))
            status = status or child.returncode
        finally:
            shutil.rmtree(root, ignore_errors=True)
            if args.home:
                shutil.rmtree(args.home, ignore_errors=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
