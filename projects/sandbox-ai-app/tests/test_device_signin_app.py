"""Codex's device sign-in in a terminal tab of the app (terminal.py, device_window.py): the page opens by itself in
the system browser - through the app's own way of opening a URL, which the tests record (conftest.OPENED_URLS: no real
browser) - and a small window shows the one-time code, large, with Copy and Open again; when the terminal's sign-in
ends well the window closes. For `sandbox-ai login codex`, on this machine or over SSH, and for a Codex start whose
sandbox has no login (sandbox-ai runs Codex's device sign-in there first).

User (2026-11-19): "kannst du für Codex eine Art Auto-Sign-in für Device-Auth bauen, dass bei der Anmeldung für Codex,
wenn nichts anderes konfiguriert ist, direkt Device-Auth aufgerufen wird, diese auch direkt im Systembrowser gecallt
wird und man dann dort auch den Code eingeben kann?"

Codex's lines are its own: the device code prompt of codex-rs' login as the @openai/codex 0.166.0 binary holds it (the
version and the hints in gray, the link and the code in blue)."""
import sys

from test_terminal_tools import open_view, screen, settings, terminal, web  # noqa: F401 - the tabs' fixtures

GRAY, BLUE, RESET = "\x1b[90m", "\x1b[94m", "\x1b[0m"
URL = "https://auth.openai.com/codex/device"


def codex_device_prompt(code="ABCD-12345", url=URL, version="0.166.0"):
    return (f"\nWelcome to Codex [v{GRAY}{version}{RESET}]\n{GRAY}OpenAI's command-line coding agent{RESET}\n"
            "\nFollow these steps to sign in with ChatGPT using device code authorization:\n"
            f"\n1. Open this link in your browser and sign in to your account\n   {BLUE}{url}{RESET}\n"
            f"\n2. Enter this one-time code {GRAY}(expires in 15 minutes){RESET}\n   {BLUE}{code}{RESET}\n"
            f"\n{GRAY}Continue only if you started this login in Codex. If a website or another person gave you this "
            f"code, cancel.{RESET}\n")


def script(tmp_path, words, body):
    """A stand-in for sandbox-ai's main.py that prints what the real command would (BODY: Python)."""
    path = tmp_path / "main.py"
    path.write_text("import sys, time\n" + body)
    return [sys.executable, "-I", str(path), *words]


def opened_since(before):
    from conftest import OPENED_URLS
    return [url.toString() for url in OPENED_URLS[before:]]


def test_the_code_and_page_from_codex_real_output():
    from sandbox_ai_app import terminal_text as T
    watch = T.LoginWatch()
    found = watch.feed(codex_device_prompt().encode())
    assert found == [URL] and watch.code == "ABCD-12345" and T.flow_of(URL).kind == "device"
    # Qwen OAuth: the code is in the link.
    qwen = "https://chat.qwen.ai/authorize?user_code=AB12-CD34&client=qwen-code"
    assert T.flow_of(qwen).kind == "device"
    watch = T.LoginWatch()
    watch.feed(f"Please visit the following URL in your browser to authorize:\n\n{qwen}\n\n".encode())
    assert watch.code == "AB12-CD34"
    assert T.flow_of("https://chat.qwen.ai/authorize?user_code=x%0Ay").kind == "page"


def test_the_sign_in_codex_draws_on_its_own_screen_is_read_too():
    """Codex's own sign-in screen (its menu's "Sign in with Device Code", in tmux): the same lines as its binary holds
    them, drawn row by row with the cursor moved between them - no line ends. Read as rows, the link does not run
    into the line under it."""
    from sandbox_ai_app import terminal_text as T
    rows = ["Finish signing in via your browser", "  1. Open this link in your browser and sign in",
            f"  {URL}", "  2. Enter this one-time code after you are signed in (expires in 15 minutes)",
            "  ABCD-12345", "  Continue only if you started this login in Codex. If a website or another person gave "
            "you this code, cancel."]
    drawn = "".join(f"\x1b[{row + 3};1H\x1b[K\x1b[1m{text.strip()}\x1b[0m" for row, text in enumerate(rows)) + "\r\n"
    screen_watch = T.LoginWatch(rows=True)
    assert screen_watch.feed(drawn.encode()) == [URL] and screen_watch.code == "ABCD-12345"
    plain_watch = T.LoginWatch()
    plain_watch.feed(drawn.encode())
    assert plain_watch.code is None, "without rows the link and the next line are one word"


def test_login_codex_opens_the_page_in_the_system_browser_and_shows_the_code(terminal, web, wait, settings,
                                                                               monkeypatch, tmp_path):
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window, login_browser
    from sandbox_ai_app.terminal_tools import clipboard
    in_app = []
    monkeypatch.setattr(login_browser, "open_login", lambda url, hosts, **kw: in_app.append(url))
    gate = tmp_path / "signed-in"
    argv = script(tmp_path, ["login", "codex"],
                  f"sys.stdout.write({codex_device_prompt()!r})\nsys.stdout.flush()\n"
                  f"import os\nwhile not os.path.exists({str(gate)!r}):\n    time.sleep(0.05)\n"
                  "print('Successfully logged in')\n")
    assert terminal.login_of(argv) == "codex"
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: device_window.window_for(view) is not None, 20), view.bar.text()
        window = device_window.window_for(view)
        assert opened_since(before) == [URL], "the page, in the system browser, by itself - once"
        assert in_app == [], "not in the app's login browser (the setting's default: system)"
        assert window.code_label.text() == "ABCD-12345" and window.code_label.font().pointSizeF() >= 20
        assert window.url_label.text() == URL and window.windowTitle() == "Sign in to Codex"
        assert "opened in your browser" in window.lead.text()
        assert window.open_button.text() == "Open again" and window.isVisible() and not window.isModal()
        window.copy_button.click()
        assert clipboard().text() == "ABCD-12345", "copied on request"
        window.open_button.click()
        assert opened_since(before) == [URL, URL]
        window.link_button.click()
        assert clipboard().text() == URL
        assert "The sign-in page opened in your browser" in view.bar.text() and "ABCD-12345" not in view.bar.text(), \
            "the line above the terminal goes to the log: it never holds the code"
        assert view.bar.choices()[:2] == ["Copy the code", "Open again"]
        gate.touch()  # the code was entered in the browser: Codex's login ends well
        assert wait(lambda: not view.running, 20)
        assert wait(lambda: device_window.window_for(view) is None, 10), "the window closes with the login"
        assert view.bar.isHidden()
    finally:
        view.close_terminal()
        device_window.close_all()


def test_a_login_that_fails_leaves_the_window_saying_so(terminal, web, wait, settings, tmp_path):
    from sandbox_ai_app import device_window
    argv = script(tmp_path, ["login", "codex"], f"sys.stdout.write({codex_device_prompt()!r})\nsys.stdout.flush()\n"
                                                "time.sleep(0.5)\nsys.exit(1)\n")
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: device_window.window_for(view) is not None, 20)
        window = device_window.window_for(view)
        assert wait(lambda: not view.running, 20)
        assert wait(lambda: "The login in the terminal ended (exit status 1)." in window.state.text(), 10)
        assert window.isVisible() and not window.copy_button.isEnabled()
    finally:
        view.close_terminal()
        device_window.close_all()


def test_a_login_on_another_host_opens_this_machines_browser(terminal, web, wait, settings, tmp_path):
    """Remote hosts: the code comes from that host's terminal, the browser is this machine's."""
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window
    ssh = tmp_path / "ssh"
    ssh.write_text(f"#!{sys.executable}\nimport sys, time\nsys.stdout.write({codex_device_prompt('WXYZ-98765')!r})\n"
                   "sys.stdout.flush()\ntime.sleep(30)\n")
    ssh.chmod(0o755)
    argv = [str(ssh), "-tt", "--", "me@ct1.example", "if command -v sandbox-ai >/dev/null; then exec sandbox-ai "
            "login codex; fi"]
    assert terminal.login_of(argv) == "codex"
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: device_window.window_for(view) is not None, 20)
        window = device_window.window_for(view)
        assert opened_since(before) == [URL] and window.code_label.text() == "WXYZ-98765"
        assert "The code comes from the terminal on another host; the browser is this computer's." in \
            window.lead.text()
    finally:
        view.close_terminal()
        device_window.close_all()


def test_the_apps_login_browser_is_the_alternative(terminal, web, wait, settings, monkeypatch, tmp_path):
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window, login_browser
    settings["device_signin"] = "app"
    in_app = []
    monkeypatch.setattr(login_browser, "open_login", lambda url, hosts, **kw: in_app.append((url, kw.get("code"))))
    argv = script(tmp_path, ["login", "codex"], f"sys.stdout.write({codex_device_prompt()!r})\nsys.stdout.flush()\n"
                                                "time.sleep(30)\n")
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: in_app, 20), view.bar.text()
        assert in_app[0][0] == URL and opened_since(before) == [] and device_window.window_for(view) is None
        assert wait(lambda: "One-time code: ABCD-12345" in view.bar.text(), 10)
    finally:
        view.close_terminal()
        device_window.close_all()


def test_a_codex_start_without_a_login_signs_in_the_same_way(terminal, web, wait, settings, tmp_path):
    """A Codex start whose sandbox has no login: sandbox-ai runs Codex's device sign-in there first (its line, then
    Codex's prompt). The start's tab opens the page and shows the code; the window goes when the sign-in ended and
    Codex itself starts."""
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window
    gate = tmp_path / "signed-in"
    argv = script(tmp_path, ["codex", "--workspace", str(tmp_path), "--session", "c1"],
                  "print('agent-sandbox: the host has no codex login; log in with `sandbox-ai login codex` (kept for "
                  "all sandboxes) or on the host')\n"
                  "print('agent-sandbox: Codex has no login in this sandbox: its device sign-in starts now - open the "
                  "link it shows and enter the one-time code (Ctrl-C leaves it out: Codex then asks itself)')\n"
                  f"sys.stdout.write({codex_device_prompt()!r})\nsys.stdout.flush()\n"
                  f"import os\nwhile not os.path.exists({str(gate)!r}):\n    time.sleep(0.05)\n"
                  "print(\"agent-sandbox: signed in; this login stays in this sandbox's HOME - for every Codex "
                  "sandbox, log in once on the host: sandbox-ai login codex\")\nsys.stdout.flush()\ntime.sleep(30)\n")
    assert terminal.login_of(argv) is None and terminal.start_of_argv(argv) == "codex"
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: device_window.window_for(view) is not None, 20)
        assert opened_since(before) == [URL] and device_window.window_for(view).code_label.text() == "ABCD-12345"
        gate.touch()
        assert wait(lambda: device_window.window_for(view) is None, 20), "signed in: the window closes"
        assert view.running, "the tab is Codex's from here on"
        assert "signed in" in view.start_tail()
    finally:
        view.close_terminal()
        device_window.close_all()


def test_a_start_that_has_a_login_never_opens_a_page_by_itself(terminal, web, wait, settings, tmp_path):
    """Device codes are a common phishing target: what a sandbox prints is the agent's to choose. Only a start whose
    launcher said - on the host, before the sandbox ran - that its agent has no login has a sign-in of its own; a
    device page and a code that any other Codex start shows (the agent, later, told to by something it read) open
    nothing and show no window."""
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window
    argv = script(tmp_path, ["codex", "--workspace", str(tmp_path), "--session", "c1"],
                  "print('agent-sandbox: session c1 is running. Detach: Ctrl-b d')\n"
                  f"sys.stdout.write({codex_device_prompt('EVIL-00000')!r})\n"
                  "print('agent-sandbox: the host has no codex login; log in with ...')\n"  # (after it: too late)
                  "sys.stdout.flush()\ntime.sleep(30)\n")
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert screen(view, wait, "EVIL-00000")
        wait(lambda: False, 2.0)
        assert opened_since(before) == [] and device_window.window_for(view) is None
        assert view.bar.isHidden()
    finally:
        view.close_terminal()
        device_window.close_all()


def test_a_start_of_another_agent_is_not_read_for_a_sign_in(terminal, web, wait, settings, tmp_path):
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window
    argv = script(tmp_path, ["claude", "--workspace", str(tmp_path)],
                  f"sys.stdout.write({codex_device_prompt()!r})\nsys.stdout.flush()\ntime.sleep(30)\n")
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert screen(view, wait, "ABCD-12345")
        assert view.watch is None and wait(lambda: "ABCD-12345" in view.start_tail(), 10)
        assert opened_since(before) == [] and device_window.window_for(view) is None
    finally:
        view.close_terminal()


def test_a_run_behind_tor_never_opens_its_page_here(terminal, web, wait, settings, monkeypatch, tmp_path):
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window
    argv = script(tmp_path, ["codex", "--vpn", "tor-net"],
                  "print('agent-sandbox: the host has no codex login; log in with `sandbox-ai login codex`')\n"
                  f"sys.stdout.write({codex_device_prompt()!r})\nsys.stdout.flush()\ntime.sleep(30)\n")
    monkeypatch.setattr(terminal.TerminalView, "privacy", lambda self: ("tor", "tor-net"))
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert wait(lambda: device_window.window_for(view) is not None, 20)
        window = device_window.window_for(view)
        assert opened_since(before) == [], "its page would be reached with this computer's own address"
        assert "runs through Tor" in window.lead.text() and not window.open_button.isEnabled()
        assert window.code_label.text() == "ABCD-12345" and "Open again" not in view.bar.choices()
    finally:
        view.close_terminal()
        device_window.close_all()


def test_a_page_that_is_not_the_agents_own_is_never_opened(terminal, web, wait, settings, tmp_path):
    from conftest import OPENED_URLS
    from sandbox_ai_app import device_window
    argv = script(tmp_path, ["login", "codex"],
                  f"sys.stdout.write({codex_device_prompt(url='https://auth.openai.com.evil.example/codex/device')!r})\n"
                  "sys.stdout.flush()\ntime.sleep(30)\n")
    before = len(OPENED_URLS)
    view = open_view(terminal, web, wait, argv)
    try:
        assert screen(view, wait, "ABCD-12345")
        wait(lambda: False, 2.0)
        assert opened_since(before) == [] and device_window.window_for(view) is None
    finally:
        view.close_terminal()
