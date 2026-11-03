"""Independent happy/sad/edge checks of --git-credentials FILE (git credential-store format).

Complements the git_credentials tests in test_agent_sandbox_contracts.py: percent-encoding,
normalisation, file-level limits and checks, agent-writable trees, project configuration,
dry-run output, and parity with real git (git credential-store / git credential fill, no network).
The parity tests cover three parsing bugs found and fixed here: hosts with a port as git's store
writes them (HOST%3aPORT), lines git ignores ('@' in the host part) and '?'/'#' in a stored path.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.parse

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load_module("sandbox_git_credentials", "sandbox/agent_sandbox.py")

TOKEN = "github_pat_" + "A1b2" * 10
GIT_TOKEN = "0123456789abcdef" * 2 + "01234567"
# Shell and printf metacharacters, quotes, leading dash, spaces at both ends, non-ASCII.
NASTY_USER = "first last$HOME`id`'\"\\:@/%s-n"
NASTY_PASSWORD = " -n %s%n\\n$(touch pwned)`id`'\"; * ä€ "
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(not GIT, reason="git not installed")


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TERM"):
        monkeypatch.delenv(name, raising=False)


def q(text):
    return urllib.parse.quote(text, safe="")


def login(user, password, server):
    """One credential-store line, percent-encoded as git writes it."""
    scheme, rest = server.split("://", 1)
    return f"{scheme}://{q(user)}:{q(password)}@{rest}"


def write(directory, content, mode=0o600, name="git-credentials"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(mode)
    return path


def lines(directory, *rows, newline="\n"):
    return write(directory, newline.join(rows) + newline)


def parsed(tmp_path, *options):
    return s.parse_args(["claude", "--workspace", str(tmp_path / "work"), *options])


def validated(tmp_path, *options):
    a = parsed(tmp_path, *options)
    s.validate(a)
    return a


def entries(a):
    return [(c["url"], c["user"], password) for c, password in s.git_credentials_file(a)[0]]


def notes(a):
    return s.git_credentials_file(a)[1]


def refused(tmp_path, match, *options):
    with pytest.raises(ValueError, match=match) as error:
        validated(tmp_path, *options)
    return str(error.value)


def dry_run(tmp_path, monkeypatch, capsys, *options):
    monkeypatch.setattr(s, "check_login", lambda a: None)
    assert s.launch(["claude", "--workspace", str(tmp_path / "work"), "--no-auth", "--dry-run", *options]) in (0, None)
    out, err = capsys.readouterr()
    return json.loads(out), err


# --------------------------------------------------------------------------------------------- happy


def test_happy_percent_decoding_matches_git():
    """Every escape git writes is decoded; '+' stays '+' (no form decoding); raw UTF-8 is kept."""
    cases = {"%3A%40%2F%25%20%2B%3F%23%26%3D": ":@/% +?#&=", "a+b": "a+b", "%C3%A4%E2%82%AC": "ä€",
             "j%C3%BCrgen": "jürgen", "jürgen": "jürgen"}
    for encoded, plain in cases.items():
        server, user, password = s.git_credentials_line(f"https://{encoded}:{encoded}@git.example.org")
        assert (server["url"], user, password) == ("https://git.example.org", plain, plain)


def test_happy_invalid_escapes_stay_literal_like_git():
    # git's url_decode keeps a '%' that does not start two hex digits.
    for encoded in ("%ZZ", "%", "abc%", "%4", "%%41"):
        assert s.git_credentials_line(f"https://u:{encoded}@h.example")[2] == urllib.parse.unquote(encoded)
    assert s.git_credentials_line("https://u:%%41@h.example")[2] == "%A"


def test_happy_normalisation_default_ports_case_paths():
    expected = {
        "https://u:p@Git.Example.ORG:443/": "https://git.example.org",
        "https://u:p@git.example.org:00443": "https://git.example.org",
        "https://u:p@git.example.org/owner/repo.git": "https://git.example.org",
        "https://u:p@git.example.org:8443/x": "https://git.example.org:8443",
        "http://u:p@LOCALHOST:80": "http://localhost",
        "http://u:p@127.0.0.1:3000/": "http://127.0.0.1:3000",
        "http://u:p@[::1]:3000/x": "http://[::1]:3000",
        "https://u:p@[::1]": "https://[::1]",
        "https://u:a:b:c@h.example": "https://h.example",  # git splits at the first ':'
    }
    for line, url in expected.items():
        assert s.git_credentials_line(line)[0]["url"] == url, line
    assert s.git_credentials_line("https://u:a:b:c@h.example")[2] == "a:b:c"


def test_happy_network_allowances(tmp_path):
    """IPv6 and IPv4 loopback ports forward, github.com (any case) is the preset, others are pinned names."""
    path = lines(tmp_path / "private", login("a", TOKEN, "https://GitHub.COM/owner/repo"),
                 login("b", GIT_TOKEN, "http://[::1]:3001"), login("c", GIT_TOKEN, "http://127.0.0.1:3002"),
                 login("d", GIT_TOKEN, "https://Git.Example.org:443"))
    a = validated(tmp_path, "--git-credentials", str(path))
    assert sorted(a.allow_host_port) == ["3001", "3002"] and "github" in a.presets
    assert a.allow_host == ["git.example.org"]
    env = s.git_environment(a)
    assert env["GH_TOKEN"] == TOKEN
    assert [env[f"SANDBOX_GIT_USER_{i}"] for i in range(4)] == ["a", "b", "c", "d"]


def test_happy_limits_exact(tmp_path):
    """A file of exactly 64 KiB is read; a user of 256 and a password of 4096 characters are accepted."""
    head = login("u" * 256, "p" * 4096, "https://git.example.org") + "\n"
    filler = 65536 - len(head)
    path = write(tmp_path / "private", head + " " * (filler - 1) + "\n")
    assert path.stat().st_size == 65536
    a = validated(tmp_path, "--git-credentials", str(path))
    assert entries(a) == [("https://git.example.org", "u" * 256, "p" * 4096)]


def test_happy_strict_modes_accepted(tmp_path):
    for mode in (0o600, 0o400):
        path = write(tmp_path / f"m{mode:o}", login("u", GIT_TOKEN, "https://git.example.org"), mode=mode)
        assert entries(validated(tmp_path, "--git-credentials", str(path)))[0][2] == GIT_TOKEN


def test_happy_tilde_and_relative_on_command_line(tmp_path, monkeypatch):
    lines(Path.home(), login("u", GIT_TOKEN, "https://git.example.org"))
    assert entries(validated(tmp_path, "--git-credentials", "~/git-credentials"))[0][1] == "u"
    monkeypatch.chdir(Path.home())
    a = validated(tmp_path, "--git-credentials", "git-credentials")
    assert s.git_credentials_file(a)[0][0][0]["credentials"] == str(Path.home() / "git-credentials")


def test_happy_ro_mount_holding_the_file_is_fine(tmp_path):
    """Only agent-writable trees are refused: a read-only mount may contain the file."""
    shared = tmp_path / "shared"
    path = lines(shared, login("u", GIT_TOKEN, "https://git.example.org"))
    a = validated(tmp_path, "--mount", f"{shared}:/mnt/shared:ro", "--git-credentials", str(path))
    assert entries(a)[0][2] == GIT_TOKEN


def test_happy_token_options_win_for_equivalent_urls(tmp_path, monkeypatch):
    """A --git-token-env/-file for the same server, written differently (case, :443, user), wins."""
    monkeypatch.setenv("GT_ENV", TOKEN)
    token_file = write(tmp_path / "tok", GIT_TOKEN, name="token")
    path = lines(tmp_path / "private", login("x", "secret-a", "https://git.example.org"),
                 login("y", "secret-b", "https://other.example:8443"), login("z", "secret-c", "https://third.example"))
    a = validated(tmp_path, "--git-credentials", str(path),
                  "--git-token-env", "https://me@GIT.example.org:443/=GT_ENV",
                  "--git-token-file", f"https://other.example:8443={token_file}")
    assert entries(a) == [("https://third.example", "z", "secret-c")]
    assert [n.split(" skipped: ")[1] for n in notes(a)] == [
        "https://git.example.org has a --git-token-* option", "https://other.example:8443 has a --git-token-* option"]
    assert [(c["url"], c["user"], t) for c, t in s.git_tokens(a)] == [
        ("https://git.example.org", "me", TOKEN), ("https://other.example:8443", "x-access-token", GIT_TOKEN),
        ("https://third.example", "z", "secret-c")]


def test_happy_dry_run_shows_no_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s, "load_project_config", lambda a: (None, None))
    path = lines(tmp_path / "private", "garbage-secret-1", login("me@example.org", "secret-2", "https://git.example.org"),
                 login("dup", "secret-3", "https://git.example.org:443/x"), "http://u:secret-4@plain.example")
    plan, err = dry_run(tmp_path, monkeypatch, capsys, "--git-credentials", str(path))
    assert plan["git_credentials"] == [{"url": "https://git.example.org", "user": "me@example.org",
                                        "credentials": str(path), "line": 2}]
    assert "git.example.org" in plan["https_hosts"]
    assert [f"line {n} skipped" in err for n in (1, 3, 4)] == [True] * 3
    assert "secret" not in json.dumps(plan) + err


def test_happy_project_config_with_tilde_path(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    lines(Path.home(), login("u", "secret-p", "https://git.example.org"))
    config = work / ".sandbox-ai.json"
    config.write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--git-credentials", "~/git-credentials"]}))
    s.trust_config(config)
    plan, _ = dry_run(tmp_path, monkeypatch, capsys)
    assert plan["git_credentials"][0]["credentials"] == str(Path.home() / "git-credentials")
    # The command line wins over the file.
    other = lines(tmp_path / "other", login("v", "secret-q", "https://other.example"))
    plan, _ = dry_run(tmp_path, monkeypatch, capsys, "--git-credentials", str(other))
    assert [c["url"] for c in plan["git_credentials"]] == ["https://other.example"]


def helper_fill(env, url, home):
    """Username and password real git gets for URL through the launcher's GIT_CONFIG_* variables."""
    run = subprocess.run([GIT, "credential", "fill"], input=f"url={url}\n\n", capture_output=True, text=True,
                         env={"HOME": str(home), "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0",
                              "GIT_ASKPASS": "", "SSH_ASKPASS": "", **env})
    if run.returncode:
        return None
    fields = dict(row.split("=", 1) for row in run.stdout.splitlines() if "=" in row)
    return fields.get("username"), fields.get("password")


@needs_git
def test_happy_real_git_gets_exact_login(tmp_path):
    """git credential fill (no network): shell metacharacters, spaces at both ends, '%s', a leading dash and
    non-ASCII reach git unchanged; ports, IPv6, case, default port and paths in the remote URL all match;
    helpers and usernames the agent planted in HOME do not interfere."""
    path = lines(tmp_path / "private", login(NASTY_USER, NASTY_PASSWORD, "https://Git.Example.org:443/a/b"),
                 login("v6", "pw-v6 $x", "http://[::1]:3000"), login("port", "pw-port", "https://h3.example:8443"))
    a = validated(tmp_path, "--git-credentials", str(path))
    env = s.git_environment(a)
    home = tmp_path / "agent-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[credential]\n\tuseHttpPath = true\n\tusername = evil\n\thelper = !echo password=planted #\n"
        '[credential "https://git.example.org/x"]\n\thelper = !echo password=planted2 #\n'
        '[credential "https://*.example.org"]\n\thelper = !echo password=planted3 #\n')
    nasty = (NASTY_USER, NASTY_PASSWORD)
    assert helper_fill(env, "https://git.example.org/x/y.git", home) == nasty
    assert helper_fill(env, "https://GIT.EXAMPLE.ORG:443/other", home) == nasty
    assert helper_fill(env, "https://someone@git.example.org/z", home) == nasty
    assert helper_fill(env, "http://[::1]:3000/r.git", home) == ("v6", "pw-v6 $x")
    assert helper_fill(env, "https://h3.example:8443/r", home) == ("port", "pw-port")
    # Other ports, schemes and hosts get nothing from the launcher (the planted global helper aside).
    (home / ".gitconfig").write_text("")
    assert helper_fill(env, "https://h3.example/r", home) is None
    assert helper_fill(env, "http://git.example.org/r", home) is None
    assert not (tmp_path / "pwned").exists() and not (Path.cwd() / "pwned").exists()


# ----------------------------------------------------------------------------------------------- sad


@pytest.mark.parametrize("content", ["", "\n\n", "  \n\t\n\r\n", "\ufeff"], ids=["empty", "newlines", "blanks", "bom"])
def test_sad_nothing_usable_in_blank_files(tmp_path, content):
    path = write(tmp_path / "private", content)
    message = refused(tmp_path, "holds no login the sandbox can use", "--git-credentials", str(path))
    assert content.strip() != "" or "line" not in message


def test_sad_only_unusable_lines_name_lines_not_secrets(tmp_path):
    rows = ["garbage-secret-a", "ssh://u:secret-b@host.example", "https://u@host.example",
            "https://:secret-c@host.example", "https://u:@host.example", "https://u:secret-d@host.example:99999",
            "https://u:secret-e@host.example:0", "http://u:secret-f@plain.example", "https://u:secret-g@ho_st.example",
            "https://u:secret-h%0A@host.example", "https://u:secret-i%00@host.example", "https://u:secret-j%7F@host.example",
            "https://u:secret-k%0D@host.example", "https://u:secret-l%FF@host.example", "HTTPS://u:secret-m@host.example",
            "https://u:secret-p@[2001:db8::1]", "https://u:secret-q@host.example:", "https://u:secret-r@host.example:abc",
            login("u" * 257, "secret-s", "https://host.example"), login("u", "secret-t" + "x" * 4089, "https://host.example")]
    path = lines(tmp_path / "private", *rows)
    message = refused(tmp_path, "no login the sandbox can use", "--git-credentials", str(path))
    for number in range(1, len(rows) + 1):
        assert f"line {number} skipped" in message
    assert "secret" not in message and "uuuuuuuu" not in message


def test_sad_size_limit_one_byte_more(tmp_path):
    head = login("u", "secret-x", "https://git.example.org") + "\n"
    path = write(tmp_path / "private", head + " " * (65537 - len(head)))
    assert path.stat().st_size == 65537
    message = refused(tmp_path, "larger than 64 KiB", "--git-credentials", str(path))
    assert "secret" not in message


def test_sad_not_utf8_file(tmp_path):
    path = write(tmp_path / "private", login("u", "secret-x", "https://git.example.org").encode() + b"\n\xff\xfe\n")
    message = refused(tmp_path, "not UTF-8 text", "--git-credentials", str(path))
    assert "secret" not in message


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o660, 0o644, 0o666, 0o610])
def test_sad_not_private_modes(tmp_path, mode):
    path = write(tmp_path / "private", login("u", "secret-x", "https://git.example.org"), mode=mode)
    message = refused(tmp_path, "chmod 600", "--git-credentials", str(path))
    assert "secret" not in message


@pytest.mark.skipif(os.geteuid() != 0, reason="chown to another user needs root")
def test_sad_owned_by_someone_else(tmp_path):
    path = write(tmp_path / "private", login("u", "secret-x", "https://git.example.org"))
    try:
        os.chown(path, 4242, 4242)
    except PermissionError:
        pytest.skip("no CAP_CHOWN here (user namespace)")
    refused(tmp_path, "must belong to you", "--git-credentials", str(path))


def test_sad_not_a_regular_file(tmp_path):
    (tmp_path / "dir").mkdir()
    refused(tmp_path, "not a regular file", "--git-credentials", str(tmp_path / "dir"))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    refused(tmp_path, "not a regular file", "--git-credentials", str(fifo))  # and does not block
    refused(tmp_path, "No such file", "--git-credentials", str(tmp_path / "missing"))
    real = lines(tmp_path / "private", login("u", "secret-x", "https://git.example.org"))
    (tmp_path / "link").symlink_to(real)
    refused(tmp_path, "symlink", "--git-credentials", str(tmp_path / "link"))
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    refused(tmp_path, "symlink", "--git-credentials", str(tmp_path / "dangling"))


def test_sad_inside_agent_writable_trees(tmp_path):
    work, state, rw = tmp_path / "work", tmp_path / "state", tmp_path / "rw"
    row = login("u", GIT_TOKEN, "https://git.example.org")
    in_work = lines(work / "sub", row)
    refused(tmp_path, "agent can change", "--git-credentials", str(in_work))
    # Second workspace.
    second = tmp_path / "work2"
    in_second = lines(second, row)
    with pytest.raises(ValueError, match="agent can change"):
        s.validate(s.parse_args(["claude", "--workspace", f"{work},{second}", "--git-credentials", str(in_second)]))
    in_state = lines(state, row)
    refused(tmp_path, "agent can change", "--state-dir", str(state), "--git-credentials", str(in_state))
    in_rw = lines(rw, row)
    refused(tmp_path, "agent can change", "--mount", f"{rw}:/mnt/rw:rw", "--git-credentials", str(in_rw))
    # A symlinked parent outside that leads into the workspace.
    (tmp_path / "outside-link").symlink_to(work / "sub")
    refused(tmp_path, "agent can change", "--git-credentials", str(tmp_path / "outside-link" / "git-credentials"))
    # A symlinked parent inside the workspace that leads outside: the agent could retarget it.
    private = lines(tmp_path / "private", row)
    (work / "inside-link").symlink_to(private.parent)
    refused(tmp_path, "agent can change", "--git-credentials", str(work / "inside-link" / "git-credentials"))
    # A '..' detour through the workspace.
    refused(tmp_path, "agent can change", "--git-credentials", str(work / "sub" / ".." / "sub" / "git-credentials"))


def test_sad_offline_and_mcp(tmp_path):
    path = lines(tmp_path / "private", login("u", GIT_TOKEN, "https://git.example.org"))
    refused(tmp_path, "Git tokens need network", "--offline", "--git-credentials", str(path))
    refused(tmp_path, "Git tokens need network", "--profile", "offline", "--git-credentials", str(path))
    mcp = load_module("sandbox_mcp_git_credentials", "sandbox/mcp.py")
    for options in (["--git-credentials", str(path)], [f"--git-credentials={path}"]):
        with pytest.raises(ValueError, match="not allowed for an MCP server"):
            mcp.validated({"srv": {"command": ["python3"], "options": options}}, "test")


def test_sad_loopback_without_podman_default_network(tmp_path):
    path = lines(tmp_path / "private", login("a", GIT_TOKEN, "http://localhost:3000"),
                 login("b", GIT_TOKEN, "http://[::1]:3001"), login("c", GIT_TOKEN, "https://git.example.org"))
    for options in (["--backend", "bwrap"], ["--network", "research"]):
        a = validated(tmp_path, *options, "--git-credentials", str(path))
        assert entries(a) == [("https://git.example.org", "c", GIT_TOKEN)]
        assert [("loopback" in n) for n in notes(a)] == [True, True] and a.allow_host_port == []
    only_loopback = lines(tmp_path / "loop", login("a", GIT_TOKEN, "http://localhost:3000"))
    refused(tmp_path, "no login the sandbox can use", "--backend", "bwrap", "--git-credentials", str(only_loopback))


def test_sad_project_config_relative_path(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    for options in (["--git-credentials", "git-credentials"], ["--git-credentials=git-credentials"],
                    ["--git-credentials", "./x/../git-credentials"]):
        config = work / ".sandbox-ai.json"
        config.write_text(json.dumps({"version": 1, "agent": "claude", "options": options}))
        s.trust_config(config)
        with pytest.raises(ValueError, match="use absolute paths"):
            s.load_project_config(parsed(tmp_path))


# ---------------------------------------------------------------------------------------------- edge


def test_edge_first_line_per_server_wins_after_normalisation(tmp_path):
    path = lines(tmp_path / "private", login("first", "pw-1", "https://git.example.org"),
                 login("second", "secret-2", "https://GIT.example.org:443/other/repo"),
                 login("third", "secret-3", "https://git.example.org/"),
                 login("port", "pw-4", "https://git.example.org:8443"),
                 login("plain", "pw-5", "http://localhost"), login("plain80", "secret-6", "http://localhost:80"),
                 login("v4", "pw-7", "http://127.0.0.1"))
    a = validated(tmp_path, "--git-credentials", str(path))
    assert entries(a) == [("https://git.example.org", "first", "pw-1"), ("https://git.example.org:8443", "port", "pw-4"),
                          ("http://localhost", "plain", "pw-5"), ("http://127.0.0.1", "v4", "pw-7")]
    skipped = [n for n in notes(a) if "skipped" in n]
    assert [n.split(" line ")[1].split(" ")[0] for n in skipped] == ["2", "3", "6"]
    assert all("earlier line" in n and "secret" not in n for n in skipped)
    # A remote server on another port than 443 is used, with a hint that its port needs --allow-tcp.
    ports = [n for n in notes(a) if "skipped" not in n]
    assert len(ports) == 1 and " line 4: " in ports[0] and "--allow-tcp IP:8443" in ports[0] and "pw-4" not in ports[0]
    assert a.allow_host_port == ["80"]


def test_edge_line_endings_whitespace_bom(tmp_path):
    row = login("u", GIT_TOKEN, "https://git.example.org")
    for content, line in [(f"{row}\r\n", 1), (f"  \t{row} \t\r\n", 1), (f"\r\n\r\n{row}", 3),
                          (f"\ufeff{row}\n{login('v', GIT_TOKEN, 'https://git.example.org')}\n", 2)]:
        path = write(tmp_path / f"p{len(content)}{line}", content)
        a = validated(tmp_path, "--git-credentials", str(path))
        assert s.git_credentials_file(a)[0][0][0]["line"] == line, repr(content)
    # The BOM line is skipped with a note that names the line only.
    assert notes(a) == [f"{path} line 1 skipped: not a login (https://USER:PASSWORD@HOST[:PORT])"]


def test_edge_harmless_unicode_kept(tmp_path):
    """C1 controls and Unicode line separators cannot break git's credential protocol (it splits on LF only)."""
    for value in ("p\u0085q", "p\u2028q", "p\u200fq"):
        assert s.git_credentials_line(f"https://u:{q(value)}@h.example")[2] == value


def test_edge_file_read_once_per_argument_set(tmp_path):
    path = lines(tmp_path / "private", login("u", "pw-old", "https://git.example.org"))
    a = validated(tmp_path, "--git-credentials", str(path))
    path.write_text(login("u", "pw-new", "https://git.example.org") + "\n")
    assert s.git_tokens(a)[0][1] == "pw-old"
    assert s.git_environment(a)["SANDBOX_GIT_TOKEN_0"] == "pw-old"


def test_edge_github_from_file_and_token_with_metachar_logins(tmp_path, monkeypatch):
    """The github.com token option wins and is GH_TOKEN; file logins with any characters only reach their own
    variables, never a GIT_CONFIG_* value."""
    monkeypatch.setenv("GT_GH", TOKEN)
    path = lines(tmp_path / "private", login(NASTY_USER, NASTY_PASSWORD, "https://git.example.org"),
                 login("gh", "secret-gh", "https://github.com"))
    a = validated(tmp_path, "--git-credentials", str(path), "--git-token-env", "https://github.com=GT_GH")
    env = s.git_environment(a)
    assert env["GH_TOKEN"] == TOKEN and "secret-gh" not in json.dumps(env)
    config = json.dumps({k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")})
    assert NASTY_PASSWORD not in config and json.dumps(NASTY_USER)[1:-1] not in config
    assert env["SANDBOX_GIT_USER_1"] == NASTY_USER and env["SANDBOX_GIT_TOKEN_1"] == NASTY_PASSWORD


def test_edge_wizard_offers_only_a_file(tmp_path, monkeypatch):
    w = load_module("sandbox_wizard_git_credentials", "sandbox/wizard.py")
    monkeypatch.setattr(w, "git_credentials_path", lambda: Path.home() / ".git-credentials")
    questions = []

    def prompter(answers):
        queue = list(answers)
        return w.Prompter(lambda text: questions.append(text) or queue.pop(0), lambda line: None, lambda text: "")
    (Path.home() / ".git-credentials").mkdir()
    assert w.ask_git(prompter(["n", "none", ""]), {"backend": "podman", "network": "default"}, tmp_path / "none") == []
    assert not any("--git-credentials" in text for text in questions)
    (Path.home() / ".git-credentials").rmdir()
    lines(Path.home(), login("u", GIT_TOKEN, "https://git.example.org")).rename(Path.home() / ".git-credentials")
    options = w.ask_git(prompter(["n", "y", "none", ""]), {"backend": "podman", "network": "default"}, tmp_path / "none")
    assert options == ["--git-credentials", "~/.git-credentials"]
    # What the wizard writes is a path the launcher then accepts.
    assert entries(validated(tmp_path, *options))[0][1] == "u"


# ------------------------------------------------------------------------ parity with git's own store


def git_store_get(path, server):
    """(username, password) git credential-store would answer for SERVER from PATH, or None."""
    scheme, host = server.split("://", 1)
    run = subprocess.run([GIT, "credential-store", "--file", str(path), "get"], capture_output=True, text=True,
                         input=f"protocol={scheme}\nhost={host}\n\n", check=True)
    fields = dict(row.split("=", 1) for row in run.stdout.splitlines() if "=" in row)
    return (fields["username"], fields["password"]) if fields else None


def git_store_put(path, server, user, password, repo=None):
    """Let git itself write a line (with useHttpPath when REPO is given)."""
    scheme, host = server.split("://", 1)
    extra = f"path={repo}\n" if repo else ""
    subprocess.run([GIT, "-c", f"credential.useHttpPath={'true' if repo else 'false'}", "credential-store",
                    "--file", str(path), "store"], check=True, text=True,
                   input=f"protocol={scheme}\nhost={host}\n{extra}username={user}\npassword={password}\n\n")


def sandbox_login(tmp_path, path, server):
    path.chmod(0o600)
    a = validated(tmp_path, "--git-credentials", str(path))
    found = [(user, password) for url, user, password in entries(a) if url == server]
    return found[0] if found else None, notes(a)


@needs_git
def test_parity_lines_git_writes(tmp_path):
    """Lines git's own store writes, with every special character, are read back to the same login."""
    path = tmp_path / "private" / "git-credentials"
    path.parent.mkdir()
    git_store_put(path, "https://git.example.org", NASTY_USER, NASTY_PASSWORD.strip())
    git_store_put(path, "https://github.com", "x-access-token", "pw:@/?#%")
    for server in ("https://git.example.org", "https://github.com"):
        assert sandbox_login(tmp_path, path, server)[0] == git_store_get(path, server)


@needs_git
@pytest.mark.parametrize("server", ["http://localhost:3000", "http://[::1]:3000", "https://git.example.org:8443"])
def test_parity_git_written_ports(tmp_path, server):
    path = tmp_path / "private" / "git-credentials"
    path.parent.mkdir()
    git_store_put(path, server, "u", "pw")
    git_store_put(path, "https://git.example.org", "fallback", "pw-fallback")  # keeps the file usable
    assert "%3a" in path.read_text().lower()
    assert git_store_get(path, server) == ("u", "pw")
    assert sandbox_login(tmp_path, path, server)[0] == ("u", "pw")


PARITY_BUGS = {
    # (file rows, server); the sandbox must use the same login git's store answers with.
    "second-at": (["https://u:pa@ss@git.example.org", "https://good:right@git.example.org"], "https://git.example.org"),
    "hash-in-user": (["https://u#x:bad@git.example.org", "https://good:right@git.example.org"],
                     "https://git.example.org"),
    "query-in-password": (["https://u:p?q@git.example.org", "https://good:right@git.example.org"],
                          "https://git.example.org"),
}


@needs_git
@pytest.mark.parametrize("case", list(PARITY_BUGS))
def test_parity_lines_git_ignores(tmp_path, case):
    rows, server = PARITY_BUGS[case]
    path = lines(tmp_path / "private", *rows)
    assert git_store_get(path, server) == ("good", "right")
    assert sandbox_login(tmp_path, path, server)[0] == ("good", "right")


def test_note_never_holds_password_fragment(tmp_path):
    path = lines(tmp_path / "private", "https://user:topsecret@part-of-secret@bad_host.example",
                 login("u", GIT_TOKEN, "https://git.example.org"))
    a = validated(tmp_path, "--git-credentials", str(path))
    assert not any("part-of-secret" in note for note in notes(a))


@needs_git
def test_parity_query_in_stored_path(tmp_path):
    path = tmp_path / "private" / "git-credentials"
    path.parent.mkdir()
    git_store_put(path, "https://git.example.org", "u", "pw", repo="group/repo?x")
    assert "?" in path.read_text()
    assert git_store_get(path, "https://git.example.org") == ("u", "pw")
    assert sandbox_login(tmp_path, path, "https://git.example.org")[0] == ("u", "pw")


def test_sad_hard_linked_file_refused(tmp_path):
    """A second name for the file could lie in a tree the agent can write; such a file is refused."""
    path = lines(tmp_path / "private", login("u", GIT_TOKEN, "https://git.example.org"))
    os.link(path, tmp_path / "private" / "other-name")
    with pytest.raises(ValueError, match="hard links") as info:
        validated(tmp_path, "--git-credentials", str(path))
    assert GIT_TOKEN not in str(info.value)
