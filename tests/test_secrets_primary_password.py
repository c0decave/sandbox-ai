"""sandbox-ai secrets: the primary password of the private files (docs/SECRETS_DESIGN.md) - the construction, the
store's file format, setting, changing and removing it, unlocking (the agent and its socket), and that neither a
password nor a value ever reaches a command line, an environment or a log."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import stat
import subprocess
import sys
import time

import importlib.util
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("secret_store_primary_tests", ROOT / "sandbox/secret_store.py")
ss = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ss)

PASSWORD = "correct horse battery"
# (made at run time: no command line of whatever ran these tests can hold them)
CANARY = "CANARYVALUE" + os.urandom(8).hex()          # a secret's value: never anywhere but encrypted
PWCANARY = "PWCANARY-" + os.urandom(8).hex()          # a primary password: never anywhere at all


@pytest.fixture(autouse=True)
def private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir(mode=0o700)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_SECRETS_PLAIN", raising=False)
    monkeypatch.setattr(ss, "SCRYPT", {"n": 2 ** 12, "r": 8, "p": 1})  # (the real cost: the e2e tests below)
    monkeypatch.setattr(ss, "terminal", lambda: False)
    ss.forget_keys()
    yield
    ss.forget_keys()
    with open(os.devnull, "w") as null:
        ss.main(["lock"], out=lambda *a: None, err=null)


def short_runtime_dir(monkeypatch):
    """XDG_RUNTIME_DIR in a short folder of /tmp (removed after the test): a socket's path there always fits."""
    import shutil
    import tempfile
    run = Path(tempfile.mkdtemp(prefix="sai-run-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    import atexit
    atexit.register(shutil.rmtree, run, True)
    return run


def directory(tmp_path):
    return tmp_path / "data/sandbox-ai/secrets"


def plain_store(names=("GH_PAT",), value=CANARY):
    store = ss.FileStore()
    for name in names:
        store.set(name, value, plain=True)
    return store


def tree_bytes(root):
    return b"".join(path.read_bytes() for path in sorted(Path(root).rglob("*")) if path.is_file())


# --- the construction ----------------------------------------------------------------------------------------

def test_sealing_is_authenticated_randomised_and_hides_the_length():
    key = bytes(range(64))
    sealed = ss.seal(key, b"secret\0A", CANARY.encode())
    assert ss.unseal(key, b"secret\0A", sealed) == CANARY.encode()
    assert CANARY.encode() not in sealed and sealed != ss.seal(key, b"secret\0A", CANARY.encode())
    assert len(ss.seal(key, b"a", b"x")) == len(ss.seal(key, b"a", b"y" * 40)), "a short token's length is hidden"
    assert ss.unseal(key, b"", ss.seal(key, b"", b"")) == b""
    long = os.urandom(5000)
    assert ss.unseal(key, b"a", ss.seal(key, b"a", long)) == long
    for position in (0, 10, 24, 30, len(sealed) - 33, len(sealed) - 1):  # the nonce, the text, the tag
        damaged = bytearray(sealed)
        damaged[position] ^= 1
        with pytest.raises(ss.Damaged):
            ss.unseal(key, b"secret\0A", bytes(damaged))
    with pytest.raises(ss.Damaged):
        ss.unseal(key, b"secret\0B", sealed)  # another name
    with pytest.raises(ss.Damaged):
        ss.unseal(bytes(64), b"secret\0A", sealed)  # another key
    for short in (b"", sealed[:10], sealed[:ss.NONCE + ss.TAG - 1], sealed[:-1]):
        with pytest.raises(ss.Damaged):
            ss.unseal(key, b"secret\0A", short)


def test_the_format_is_fixed_by_a_known_answer():
    """Version 1 of the format never changes silently: this text was sealed by the first implementation."""
    key = bytes(range(64))
    sealed = ss.seal(key, b"secret\0NAME", b"value", nonce=bytes(range(100, 124)))
    assert sealed.hex() == KNOWN_ANSWER
    assert ss.unseal(key, b"secret\0NAME", bytes.fromhex(KNOWN_ANSWER)) == b"value"


KNOWN_ANSWER = (
    "6465666768696a6b6c6d6e6f707172737475767778797a7b7f8a0e3e75c94ab62f0526705c5f8e16f5bd176e0bad5aaf68e975196b"
    "b851027b8b2533fe64716c47c92da256c774affe472b3113bf1ae79bf3add8365f8a33f9d026cabf007107fb6005e77a7b8ee6fceb"
    "a11e19dd6dee67b84c94918eaa88"
)


def test_the_key_comes_from_scrypt_with_the_stores_own_salt(monkeypatch):
    monkeypatch.undo()
    assert ss.SCRYPT == {"n": 2 ** 17, "r": 8, "p": 1}, "the documented cost (docs/SECRETS_DESIGN.md)"
    kdf = {"name": "scrypt", "n": 2 ** 12, "r": 8, "p": 1, "salt": base64.b64encode(b"s" * 32).decode()}
    import hashlib
    assert ss.derive("pass word", kdf) == hashlib.scrypt(b"pass word", salt=b"s" * 32, n=2 ** 12, r=8, p=1, dklen=64)
    assert ss.derive("pass word", {**kdf, "salt": base64.b64encode(b"t" * 32).decode()}) != ss.derive("pass word", kdf)
    assert ss.derive("\u00e9", kdf) == ss.derive("e\u0301", kdf), "one password, however the keyboard composes it"
    for bad in ({**kdf, "n": 2 ** 12 + 1}, {**kdf, "n": 2 ** 30}, {**kdf, "r": 0}, {**kdf, "p": 99},
                {**kdf, "name": "md5"}, {**kdf, "salt": "AAAA"}, {"name": "scrypt"}):
        with pytest.raises(ValueError):
            ss.derive("x", bad)
    slow = {"name": "pbkdf2-sha512", "rounds": 1000, "salt": kdf["salt"]}  # (a Python without scrypt)
    assert ss.derive("pw", slow) == hashlib.pbkdf2_hmac("sha512", b"pw", b"s" * 32, 1000, 64)
    monkeypatch.setattr(ss, "has_scrypt", lambda: False)
    assert ss.new_kdf()["name"] == "pbkdf2-sha512" and ss.new_kdf()["rounds"] >= 1_000_000


# --- setting, changing, removing -------------------------------------------------------------------------------

def test_setting_the_password_encrypts_every_plain_file_and_leaves_no_plain_text(tmp_path):
    store = plain_store(("GH_PAT", "OPENAI_KEY"))
    assert CANARY.encode() in tree_bytes(tmp_path)
    key = ss.set_password(PASSWORD)
    assert CANARY.encode() not in tree_bytes(tmp_path), "no plain file, no temporary file, no backup"
    assert sorted(p.name for p in directory(tmp_path).iterdir()) == [".lock", ".primary.json", "GH_PAT", "OPENAI_KEY"]
    header = directory(tmp_path) / ".primary.json"
    assert stat.S_IMODE(header.stat().st_mode) == 0o600 and PASSWORD not in header.read_text()
    assert json.loads(header.read_text())["kdf"]["name"] == "scrypt" and json.loads(header.read_text())["version"] == 1
    for name in ("GH_PAT", "OPENAI_KEY"):
        path = directory(tmp_path) / name
        assert path.read_text().startswith(ss.MAGIC) and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.names() == ["GH_PAT", "OPENAI_KEY"]
    ss.remember_key(directory(tmp_path), key)
    assert ss.get("GH_PAT") == CANARY and ss.FileStore().get("OPENAI_KEY") == CANARY
    ss.FileStore().set("NEW", "another-" + CANARY)
    assert CANARY.encode() not in tree_bytes(tmp_path) and ss.get("NEW") == "another-" + CANARY
    with pytest.raises(ValueError, match="already set.*secrets password change"):
        ss.set_password("another password")


def test_the_old_plain_file_is_overwritten_before_it_goes(tmp_path, monkeypatch):
    """The old file's blocks are written over (through a descriptor kept open across the atomic replace), so a
    plain copy does not stay in the freed blocks of a file system that writes in place."""
    plain_store()
    old = os.open(directory(tmp_path) / "GH_PAT", os.O_RDONLY)  # the old inode, as an undelete would find it
    ss.set_password(PASSWORD)
    content = os.pread(old, 4096, 0)
    os.close(old)
    assert CANARY.encode() not in content and len(content) >= len(CANARY)


def test_a_wrong_password_is_detected_and_attempts_slow_down(tmp_path, monkeypatch):
    plain_store()
    ss.set_password(PASSWORD)
    now = [1000.0]
    monkeypatch.setattr(ss, "clock", lambda: now[0])
    for _ in range(ss.FREE_ATTEMPTS):
        with pytest.raises(ss.WrongPassword, match="wrong primary password"):
            ss.unlock_key("not the password")
    with pytest.raises(ss.Throttled, match="wait"):
        ss.unlock_key(PASSWORD)  # even the right one waits: the attempts are counted, not the answers
    now[0] += 1.01
    with pytest.raises(ss.WrongPassword):
        ss.unlock_key("still wrong")
    now[0] += 1.01
    with pytest.raises(ss.Throttled):
        ss.unlock_key(PASSWORD)
    now[0] += 2.01
    key = ss.unlock_key(PASSWORD)
    assert len(key) == 64
    assert ss.unlock_key(PASSWORD) == key, "a right password ends the slowing down"
    assert ss.attempt_wait(20) == ss.ATTEMPT_WAIT_MAX and ss.attempt_wait(ss.FREE_ATTEMPTS - 1) == 0


def test_guesses_made_at_once_are_counted_one_after_the_other(tmp_path, monkeypatch):
    """Regression (security review 1): the attempts were read before the key derivation and written after it, so
    guesses sent at once (eight requests of a web session, eight `secrets unlock`) all found "no wrong attempt yet",
    were all checked, and counted as one. One attempt at a time: of eight at once, FREE_ATTEMPTS are checked and
    the rest wait, as eight in a row would."""
    import threading
    plain_store()
    ss.set_password(PASSWORD)
    derive, checked = ss.derive, []

    def slow(password, kdf):
        checked.append(password)
        time.sleep(0.05)  # (the real derivation takes 0.3 s: the window every parallel guess passed through)
        return derive(password, kdf)
    monkeypatch.setattr(ss, "derive", slow)
    outcomes = []

    def guess(index):
        try:
            ss.unlock_key(f"wrong guess {index}")
        except ValueError as exc:
            outcomes.append(type(exc).__name__)
    threads = [threading.Thread(target=guess, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes) == ["Throttled"] * (8 - ss.FREE_ATTEMPTS) + ["WrongPassword"] * ss.FREE_ATTEMPTS
    assert len(checked) == ss.FREE_ATTEMPTS and ss._attempts(directory(tmp_path))[0] == ss.FREE_ATTEMPTS


def test_a_tampered_or_swapped_file_is_detected(tmp_path):
    plain_store(("A_KEY",), "value-a")
    plain_store(("B_KEY",), "value-b")
    ss.remember_key(directory(tmp_path), ss.set_password(PASSWORD))
    a, b = directory(tmp_path) / "A_KEY", directory(tmp_path) / "B_KEY"
    text = a.read_text()
    raw = bytearray(base64.b64decode(text[len(ss.MAGIC):]))
    raw[30] ^= 4
    a.write_text(ss.MAGIC + base64.b64encode(bytes(raw)).decode() + "\n")
    with pytest.raises(ValueError, match="A_KEY.*damaged or changed"):
        ss.get("A_KEY")
    a.write_text(b.read_text())  # B's file under A's name
    with pytest.raises(ValueError, match="damaged or changed"):
        ss.get("A_KEY")
    a.write_text(ss.MAGIC + "not base64 at all!\n")
    with pytest.raises(ValueError, match="damaged or changed"):
        ss.get("A_KEY")
    assert ss.get("B_KEY") == "value-b"


def test_a_tampered_header_is_detected(tmp_path):
    plain_store()
    ss.set_password(PASSWORD)
    path = directory(tmp_path) / ".primary.json"
    header = json.loads(path.read_text())
    for change in ({"id": "0" * 32}, {"kdf": {**header["kdf"], "n": 2 ** 10}},
                   {"key": base64.b64encode(os.urandom(120)).decode()}):
        path.write_text(json.dumps({**header, **change}))
        with pytest.raises(ss.WrongPassword):
            ss.unlock_key(PASSWORD)
        (directory(tmp_path) / ".attempts").unlink()
    for garbage in ("{", "[]", "[" * 100000, json.dumps({**header, "version": 2}), json.dumps({"version": 1})):
        path.write_text(garbage)
        with pytest.raises(ValueError, match="primary password"):
            ss.unlock_key(PASSWORD)
    path.write_text(json.dumps(header))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ss.unlock_key(PASSWORD)
    path.chmod(0o600)
    path.unlink()
    with pytest.raises(ValueError, match="encrypted.*\\.primary\\.json.*missing"):
        ss.get("GH_PAT")


def test_changing_the_password_keeps_every_secret(tmp_path):
    plain_store()
    ss.set_password(PASSWORD)
    with pytest.raises(ss.WrongPassword):
        ss.change_password("wrong one here", "new password 1")
    ss.change_password(PASSWORD, "new password 1")
    with pytest.raises(ss.WrongPassword):
        ss.unlock_key(PASSWORD)
    (directory(tmp_path) / ".attempts").unlink()
    ss.remember_key(directory(tmp_path), ss.unlock_key("new password 1"))
    assert ss.get("GH_PAT") == CANARY and CANARY.encode() not in tree_bytes(tmp_path)


def test_removing_the_password_gives_plain_files_back(tmp_path):
    plain_store(("GH_PAT", "OTHER"))
    ss.set_password(PASSWORD)
    with pytest.raises(ss.WrongPassword):
        ss.remove_password("wrong one here")
    assert (directory(tmp_path) / ".primary.json").exists()
    assert ss.remove_password(PASSWORD) == 2
    assert not (directory(tmp_path) / ".primary.json").exists()
    assert (directory(tmp_path) / "GH_PAT").read_text() == CANARY and ss.FileStore().get("OTHER") == CANARY
    with pytest.raises(ValueError, match="no primary password"):
        ss.remove_password(PASSWORD)
    with pytest.raises(ValueError, match="no primary password"):
        ss.change_password(PASSWORD, "something else")


@pytest.mark.parametrize("bad", ["", "short", "x" * 2000, "two\nlines here"])
def test_a_password_must_be_usable(bad):
    with pytest.raises(ValueError):
        ss.set_password(bad)


def test_a_half_done_encryption_is_finished_at_the_next_unlock(tmp_path, monkeypatch, capsys):
    """The header is written first: a power cut after it leaves plain files beside it, which stay readable (with
    the warning) and are encrypted by the next unlock."""
    plain_store(("ONE", "TWO"))
    calls = []
    real = ss.replace_wiping

    def failing(path, data):
        calls.append(path.name)
        if len(calls) == 3:  # the header, ONE, then the power goes
            raise OSError("power cut")
        return real(path, data)
    monkeypatch.setattr(ss, "replace_wiping", failing)
    with pytest.raises(OSError):
        ss.set_password(PASSWORD)
    monkeypatch.setattr(ss, "replace_wiping", real)
    assert (directory(tmp_path) / "TWO").read_text() == CANARY and ss.protection()["plain"] == ["TWO"]
    assert ss.FileStore().get("TWO") == CANARY, "never locked out of your own data"
    assert ss.get("TWO") == CANARY and "The next unlock encrypts it: sandbox-ai secrets unlock" in capsys.readouterr().err
    ss.remember_key(directory(tmp_path), ss.unlock_key(PASSWORD))
    assert ss.encrypt_plain_files() == ["TWO"]
    assert CANARY.encode() not in tree_bytes(tmp_path) and ss.get("ONE") == ss.get("TWO") == CANARY


# --- locked, plain, and what the commands say --------------------------------------------------------------------

def test_a_locked_store_says_how_to_unlock_and_reads_nothing(tmp_path, capsys):
    plain_store()
    ss.set_password(PASSWORD)
    with pytest.raises(ss.Locked, match="sandbox-ai secrets unlock"):
        ss.get("GH_PAT")
    with pytest.raises(ss.Locked, match="sandbox-ai secrets unlock"):
        ss.FileStore().set("NEW", "v")
    assert ss.get("NOT_THERE") is None, "a secret that is not there needs no password"
    assert ss.FileStore().names() == ["GH_PAT"] and ss.FileStore().delete("GH_PAT") is True


def test_a_new_secret_needs_the_primary_password_first(tmp_path, capsys, monkeypatch):
    out = []
    assert ss.main(["set", "GH_PAT"], stdin=io.StringIO(CANARY + "\n"), out=out.append) == 1
    err = capsys.readouterr().err
    assert "sandbox-ai secrets password set" in err and "--plain" in err and not directory(tmp_path).exists() or \
        not list(directory(tmp_path).glob("GH_PAT"))
    assert ss.main(["set", "GH_PAT", "--plain"], stdin=io.StringIO(CANARY + "\n"), out=out.append) == 0
    assert out == ["GH_PAT kept (file, not encrypted: sandbox-ai secrets password set)"]
    assert (directory(tmp_path) / "GH_PAT").read_text() == CANARY
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")  # the same choice for a machine without anyone to ask
    assert ss.main(["set", "OTHER"], stdin=io.StringIO("v\n"), out=out.append) == 0


def test_a_plain_store_keeps_working_with_one_warning(tmp_path, capsys, monkeypatch):
    plain_store(("GH_PAT", "OTHER"))
    assert ss.get("GH_PAT") == CANARY and ss.get("OTHER") == CANARY
    err = capsys.readouterr().err
    assert err.count("warning") == 1 and "not encrypted" in err and "sandbox-ai secrets password set" in err
    assert CANARY not in err
    ss.forget_keys()
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")
    assert ss.get("GH_PAT") == CANARY and capsys.readouterr().err == ""


def test_backend_and_status_say_which_protection_is_in_force(tmp_path, capsys):
    out = []
    plain_store(("FIRST",))
    ss.set_password(PASSWORD)
    assert ss.main(["status"], out=out.append) == 0 and ss.main(["lock"], out=out.append) == 0
    assert not (tmp_path / "run" / "sandbox-ai").exists(), "asking makes no folder: only an agent's start does"
    ss.remove_password(PASSWORD)
    ss.FileStore().delete("FIRST")
    out.clear()
    assert ss.main(["backend"], out=out.append) == 0 and out[0].split()[0] == "file"
    assert "no secrets yet" in out[0] and "primary password" in out[0]
    plain_store()
    assert ss.main(["backend"], out=out.append) == 0
    assert out[-1] == "file (not encrypted: 1 secret in plain files; sandbox-ai secrets password set)"
    assert ss.main(["status", "--json"], out=out.append) == 0
    assert json.loads(out[-1]) == {"backend": "file", "protection": "none", "password": False, "locked": None,
                                   "left": None, "plain": ["GH_PAT"], "encrypted": 0, "kdf": None,
                                   "plain_choice": False, "plain_files": []}
    ss.set_password(PASSWORD)
    assert ss.main(["backend"], out=out.append) == 0
    assert out[-1] == "file (encrypted with the primary password; locked: sandbox-ai secrets unlock)"
    assert ss.main(["status", "--json"], out=out.append) == 0
    status = json.loads(out[-1])
    assert status["protection"] == "primary-password" and status["locked"] is True and status["plain"] == [] and \
        status["encrypted"] == 1 and status["kdf"] == "scrypt n=4096 r=8 p=1"
    assert ss.main(["status"], out=out.append) == 0 and "locked" in out[-1]
    assert ss.main(["list"], out=out.append) == 0 and "GH_PAT  file" in out[-1], "names need no password"


def test_the_keyring_reports_the_systems_protection(monkeypatch):
    monkeypatch.setattr(ss, "store", lambda: ss.KeyringStore(run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")))
    out = []
    assert ss.main(["backend"], out=out.append) == 0
    assert out == ["keyring (encrypted by the system's keyring)"]
    assert ss.protection()["protection"] == "keyring"


# --- the command: passwords from standard input or a descriptor, never an argument ------------------------------

def test_password_commands_read_standard_input_or_a_descriptor(tmp_path, capsys):
    plain_store()
    out = []
    assert ss.main(["password", "set", "--password-stdin", "--locked"], stdin=io.StringIO(PASSWORD + "\n"),
                   out=out.append) == 0
    assert "1 secret encrypted" in out[-1] and CANARY.encode() not in tree_bytes(tmp_path)
    assert ss.main(["password", "set", "--password-stdin"], stdin=io.StringIO(PASSWORD + "\n"), out=out.append) == 1
    assert "already set" in capsys.readouterr().err
    assert ss.main(["password", "change", "--password-stdin"], stdin=io.StringIO(f"{PASSWORD}\nnew password 22\n"),
                   out=out.append) == 0
    assert ss.main(["password", "change", "--password-stdin"], stdin=io.StringIO("only one line\n"),
                   out=out.append) == 1
    assert "two lines" in capsys.readouterr().err
    read, write = os.pipe()
    os.write(write, b"new password 22\n")
    os.close(write)
    assert ss.main(["password", "remove", "--password-fd", str(read)], out=out.append) == 0
    assert (directory(tmp_path) / "GH_PAT").read_text() == CANARY and "not encrypted" in out[-1]
    assert ss.main(["password", "set"], stdin=io.StringIO(""), out=out.append) == 1
    assert "--password-stdin" in capsys.readouterr().err, "without a terminal: said, never a hidden default"
    for argv in (["password", "set", PASSWORD], ["unlock", PASSWORD], ["unlock", "--password", PASSWORD]):
        with pytest.raises(SystemExit):
            ss.main(argv)  # no way to give a password as an argument
    capsys.readouterr()


def test_the_sockets_folder_falls_back_when_a_path_is_too_long(tmp_path, monkeypatch):
    """XDG_RUNTIME_DIR first; a path too long for a socket (104 bytes on macOS) goes to sandbox-ai-UID in TMPDIR,
    then in /tmp - each a folder of this user's alone."""
    ident = "ab" * 16
    run = short_runtime_dir(monkeypatch)  # (tmp_path/run may be too long for a socket under a long --basetemp)
    assert str(ss.agent_socket(ident, make=True)).startswith(str(run / "sandbox-ai" / "secrets"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("long" * 30)))
    short = short_runtime_dir(monkeypatch) / "t"  # (a TMPDIR short enough for a socket, whatever --basetemp)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("long" * 30)))
    monkeypatch.setenv("TMPDIR", str(short))
    short.mkdir()
    found = ss.agent_folders()
    assert [str(folders[-1][0]) for folders in found] == [
        str(tmp_path / ("long" * 30) / "sandbox-ai" / "secrets"),
        str(short / f"sandbox-ai-{os.getuid()}" / "secrets"), f"/tmp/sandbox-ai-{os.getuid()}/secrets"]
    path = ss.agent_socket(ident, make=True)
    assert path == short / f"sandbox-ai-{os.getuid()}" / "secrets" / f"agent-{ident[:16]}.sock"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert [f[0][0] for f in ss.agent_folders("darwin")][0] == Path.home() / "Library" / "Caches" / "sandbox-ai"


def test_duration():
    assert [ss.duration(text) for text in ("90s", "30m", "8h", "2d", "15")] == [90, 1800, 28800, 172800, 900]
    for bad in ("", "0", "-1h", "1w", "h", "31d", "1.5h", "1e3"):
        with pytest.raises(ValueError):
            ss.duration(bad)


# --- the agent --------------------------------------------------------------------------------------------------

def unlocked(tmp_path, *args):
    out = []
    assert ss.main(["unlock", "--password-stdin", *args], stdin=io.StringIO(PASSWORD + "\n"), out=out.append) == 0
    return out


def agent_pid(tmp_path):
    answer = ss.agent_call(ss.read_header(directory(tmp_path))["id"], {"op": "status"})
    return answer["pid"] if answer else None


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # (ended and not yet collected by its parent)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


needs_proc = pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")


def test_unlock_keeps_the_key_in_an_agent_until_lock(tmp_path, capsys, monkeypatch):
    run = short_runtime_dir(monkeypatch)  # (tmp_path/run may be too long for a socket under a long --basetemp)
    plain_store()
    ss.set_password(PASSWORD)
    out = unlocked(tmp_path, "--for", "10m")
    assert out == ["unlocked for 10 minutes (sandbox-ai secrets lock ends it)"]
    sock = ss.agent_socket(ss.read_header(directory(tmp_path))["id"])
    assert stat.S_ISSOCK(sock.lstat().st_mode) and stat.S_IMODE(sock.lstat().st_mode) == 0o600
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700 and str(sock).startswith(str(run))
    ss.forget_keys()
    assert ss.get("GH_PAT") == CANARY
    ss.FileStore().set("NEW", "new-value")
    assert ss.get("NEW") == "new-value" and (directory(tmp_path) / "NEW").read_text().startswith(ss.MAGIC)
    status = ss.protection()
    assert status["locked"] is False and 590 <= status["left"] <= 600
    assert ss.main(["backend"], out=out.append) == 0 and "unlocked for" in out[-1]
    assert ss.main(["lock"], out=out.append) == 0 and out[-1] == "locked"
    ss.forget_keys()
    with pytest.raises(ss.Locked):
        ss.get("GH_PAT")
    assert not sock.exists()
    assert ss.main(["lock"], out=out.append) == 0 and out[-1] == "locked (it was already)"
    assert ss.main(["unlock", "--password-stdin"], stdin=io.StringIO("a wrong password\n"), out=out.append) == 1
    assert "wrong primary password" in capsys.readouterr().err and ss.protection()["locked"] is True


def test_the_agent_ends_after_its_time_and_after_being_idle(tmp_path):
    plain_store()
    ss.set_password(PASSWORD)
    unlocked(tmp_path, "--for", "2s")
    assert ss.protection()["locked"] is False
    deadline = time.time() + 10
    while ss.protection()["locked"] is False and time.time() < deadline:
        time.sleep(0.2)
    assert ss.protection()["locked"] is True
    unlocked(tmp_path, "--for", "10m", "--idle", "2s")
    time.sleep(1.2)
    ss.forget_keys()
    assert ss.get("GH_PAT") == CANARY  # use: the idle time starts again
    time.sleep(1.2)
    assert ss.protection()["locked"] is False, "asking for the status is no use, a read is"
    deadline = time.time() + 10
    while ss.protection()["locked"] is False and time.time() < deadline:
        time.sleep(0.2)
    assert ss.protection()["locked"] is True


def test_unlock_finishes_a_plain_leftover_and_replaces_a_running_agent(tmp_path):
    plain_store()
    ss.set_password(PASSWORD)
    unlocked(tmp_path)
    first = agent_pid(tmp_path)
    (directory(tmp_path) / "LEFT").write_text("left-" + CANARY)  # (an older sandbox-ai, a file copied in)
    (directory(tmp_path) / "LEFT").chmod(0o600)
    out = unlocked(tmp_path, "--for", "1h")
    assert out[0] == "1 plain file encrypted: LEFT" and out[1].startswith("unlocked for 1 hour")
    assert CANARY.encode() not in tree_bytes(tmp_path / "data")
    deadline = time.time() + 5
    while alive(first) and time.time() < deadline:
        time.sleep(0.1)
    assert not alive(first) and agent_pid(tmp_path) not in (None, first), "one agent per store"


def received(client):
    """What the agent answered; nothing when it closed the connection instead."""
    client.settimeout(2)
    try:
        return client.recv(4096)
    except ConnectionResetError:
        return b""


def test_the_agent_answers_only_its_own_user(tmp_path, monkeypatch):
    """The socket is 0600 in a 0700 folder; a connection whose peer is another user (a folder opened by mistake, a
    root-squashed mount) is closed without an answer, whatever it asks."""
    key = os.urandom(64)
    agent = ss.Agent(key, "ab" * 16, tmp_path / "run" / "a.sock", lifetime=60)
    agent.bind()
    answers = {}

    def ask(uid):
        monkeypatch.setattr(ss, "peer_uid", lambda conn: uid)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(tmp_path / "run" / "a.sock"))
        client.sendall(b'{"op": "status"}\n')
        agent.step(timeout=1)
        answers[uid] = received(client)
        client.close()
    ask(os.getuid())
    ask(os.getuid() + 1)
    assert json.loads(answers[os.getuid()])["ok"] is True and answers[os.getuid() + 1] == b""

    def failing(conn):
        raise OSError("no credentials")
    monkeypatch.setattr(ss, "peer_uid", failing)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(tmp_path / "run" / "a.sock"))
    client.sendall(b'{"op": "status"}\n')
    agent.step(timeout=1)
    assert received(client) == b"", "a peer that cannot be identified is refused"
    client.close()
    agent.close()
    assert not (tmp_path / "run" / "a.sock").exists()


def can_become_nobody():
    if os.getuid() != 0 or not sys.platform.startswith("linux"):
        return False
    code = "import os; os.setgroups([]); os.setgid(65534); os.setuid(65534)"
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True).returncode == 0


@pytest.mark.skipif(not can_become_nobody(), reason="needs a root that can become another user")
def test_another_user_gets_nothing_from_a_real_agent(tmp_path, monkeypatch):
    """Really another user (nobody), with the folders opened for it on purpose: the peer check alone refuses."""
    import shutil
    import tempfile
    reachable = Path(tempfile.mkdtemp(prefix="sandbox-ai-test-peer-", dir="/tmp"))  # (a place nobody can walk to)
    try:
        os.chmod(reachable, 0o711)
        (reachable / "run").mkdir(mode=0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(reachable / "run"))
        plain_store()
        ss.set_password(PASSWORD)
        unlocked(tmp_path)
        sock = ss.agent_socket(ss.read_header(directory(tmp_path))["id"])
        request = json.dumps({"op": "open", "name": "GH_PAT",
                              "data": (directory(tmp_path) / "GH_PAT").read_text().strip()})
        code = ("import os, socket, sys\n"
                "os.setgroups([]); os.setgid(65534); os.setuid(65534)\n"
                "s = socket.socket(socket.AF_UNIX); s.settimeout(5)\n"
                "try:\n    s.connect(sys.argv[1])\nexcept OSError as exc:\n    print('refused', exc); sys.exit(0)\n"
                "try:\n    s.sendall(sys.stdin.buffer.read() + b'\\n'); print('answer', s.recv(65536))\n"
                "except (ConnectionResetError, BrokenPipeError):\n    print('answer', b'')\n")
        argv = [sys.executable, "-I", "-c", code, str(sock)]
        closed = subprocess.run(argv, input=request, capture_output=True, text=True, cwd="/")
        assert closed.stdout.startswith("refused"), closed.stdout + closed.stderr  # 0700 folders, a 0600 socket
        opened = [(path, stat.S_IMODE(path.stat().st_mode)) for path in (sock, sock.parent, sock.parent.parent,
                                                                         reachable / "run")]
        for path, _ in opened:
            os.chmod(path, 0o777)
        try:
            answer = subprocess.run(argv, input=request, capture_output=True, text=True, cwd="/")
        finally:
            for path, mode in opened:
                os.chmod(path, mode)
        assert answer.stdout.strip() == "answer b''", answer.stdout + answer.stderr
        assert CANARY not in answer.stdout
        ss.forget_keys()
        assert ss.get("GH_PAT") == CANARY, "its own user still gets it"
    finally:
        ss.main(["lock"], out=lambda line: None)
        shutil.rmtree(reachable, ignore_errors=True)


def test_peer_credentials_of_linux_and_macos():
    import struct
    xucred = struct.pack("=IIh", 0, 501, 1) + b"\0" * 66
    assert ss.xucred_uid(xucred) == 501
    with pytest.raises(OSError):
        ss.xucred_uid(struct.pack("=IIh", 7, 501, 1) + b"\0" * 66)  # another version of the structure
    with pytest.raises(OSError):
        ss.xucred_uid(b"\0\0")

    class Fake:
        def getsockopt(self, level, option, size):
            return {(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17)): struct.pack("3i", 4242, 1000, 1000),
                    (0, 1): xucred}[(level, option)]
    assert ss.peer_uid(Fake(), platform="linux") == 1000
    assert ss.peer_uid(Fake(), platform="darwin") == 501
    with pytest.raises(OSError):
        ss.peer_uid(Fake(), platform="win32")


def test_a_client_trusts_only_its_own_users_socket(tmp_path, monkeypatch):
    plain_store()
    ss.set_password(PASSWORD)
    unlocked(tmp_path)
    ident = ss.read_header(directory(tmp_path))["id"]
    assert ss.agent_call(ident, {"op": "status"})["ok"] is True
    monkeypatch.setattr(ss, "peer_uid", lambda conn: os.getuid() + 1)  # someone else's process answers there
    assert ss.agent_call(ident, {"op": "status"}) is None
    monkeypatch.undo()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    sock = ss.agent_socket(ident)
    sock.parent.chmod(0o755)  # a folder others could have put a socket into
    assert ss.agent_socket(ident) is None and ss.agent_call(ident, {"op": "status"}) is None
    sock.parent.chmod(0o700)


def test_the_agent_comes_back_when_its_socket_was_removed(tmp_path):
    """A login session's end removes XDG_RUNTIME_DIR (and the socket in it) on a host managed over SSH; the agent
    keeps its key for its time and listens again as soon as the folder is back."""
    plain_store()
    ss.set_password(PASSWORD)
    unlocked(tmp_path)
    sock = ss.agent_socket(ss.read_header(directory(tmp_path))["id"])
    import shutil
    shutil.rmtree(tmp_path / "run")
    (tmp_path / "run").mkdir(mode=0o700)
    deadline = time.time() + 10
    while time.time() < deadline and ss.protection()["locked"]:
        time.sleep(0.2)
    ss.forget_keys()
    assert ss.get("GH_PAT") == CANARY and sock.exists()


def test_the_agent_gets_the_key_through_a_pipe(tmp_path, monkeypatch):
    """Nothing of the key in the agent's command line or in an environment made for it: one line on its standard
    input. (Its /proc entries cannot be read back: the agent switches that off for itself.)"""
    started = []
    real = subprocess.Popen

    class Recording(real):
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))
            super().__init__(argv, **kwargs)
    monkeypatch.setattr(ss.subprocess, "Popen", Recording)
    key = os.urandom(64)
    assert ss.start_agent(key, "ef" * 16, lifetime=30) is True
    try:
        (argv, kwargs), = started
        assert argv[-2:] == [str(ROOT / "sandbox/secret_store.py"), "agent"] and "env" not in kwargs
        assert kwargs["stdin"] == subprocess.PIPE and kwargs["start_new_session"] is True
        for text in (key.hex(), base64.b64encode(key).decode()):
            assert text not in " ".join(argv) and all(text not in value for value in os.environ.values())
        status = ss.agent_call("ef" * 16, {"op": "status"})
        assert status["ok"] and status["pid"] != os.getpid()
        if sys.platform.startswith("linux") and os.getuid() != 0:
            with pytest.raises(OSError):
                Path(f"/proc/{status['pid']}/environ").read_bytes()  # not dumpable
    finally:
        ss.agent_call("ef" * 16, {"op": "lock"})


def test_the_agents_requests_are_checked(tmp_path):
    key = os.urandom(64)
    agent = ss.Agent(key, "cd" * 16, tmp_path / "run" / "b.sock", lifetime=60)
    holder = ss.LocalKey(key)
    sealed = holder.seal("NAME", "value")
    assert agent.answer({"op": "open", "name": "NAME", "data": sealed}) == {"ok": True, "value": "value"}
    assert agent.answer({"op": "open", "name": "OTHER", "data": sealed})["ok"] is False
    assert agent.answer({"op": "open", "name": "../x", "data": sealed})["ok"] is False
    again = agent.answer({"op": "seal", "name": "NAME", "value": "v2"})
    assert holder.open("NAME", again["data"]) == "v2"
    for bad in ({"op": "key"}, {"op": "seal", "name": "NAME"}, {"op": "seal", "name": "NAME", "value": 5}, {}, [], "x",
                {"op": "open", "name": "NAME", "data": 7}):
        answer = agent.answer(bad)
        assert answer["ok"] is False and key.hex() not in json.dumps(answer)
    # a request nested deeper than Python reads, or no JSON at all: an answer, and the agent lives on
    agent.bind()
    for junk in (b"[" * 100000 + b"\n", b"not json\n", b"\xff\xfe\n"):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(tmp_path / "run" / "b.sock"))
        client.sendall(junk)
        agent.step(timeout=1)
        assert json.loads(received(client))["ok"] is False
        client.close()
    agent.close()
    status = agent.answer({"op": "status"})
    assert status["ok"] and status["id"] == "cd" * 16 and 0 < status["left"] <= 60
    assert key.hex() not in json.dumps(status), "the key itself is never given out"


# --- the launcher -------------------------------------------------------------------------------------------------

def test_a_start_that_needs_a_locked_secret_says_how_to_unlock(tmp_path, monkeypatch):
    lspec = importlib.util.spec_from_file_location("launcher_for_primary_password", ROOT / "sandbox/agent_sandbox.py")
    launcher = importlib.util.module_from_spec(lspec)
    lspec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "load_secret_store_module", lambda: ss)
    plain_store(("BRAVE_API_KEY",), "brave-value")
    ss.set_password(PASSWORD)
    a = launcher.parse_args(["codex", "--secret-env", "BRAVE_API_KEY"])
    with pytest.raises(ValueError, match="locked.*sandbox-ai secrets unlock"):
        launcher.secret_environment(a)
    unlocked(tmp_path)
    ss.forget_keys()
    assert launcher.secret_environment(a) == {"BRAVE_API_KEY": "brave-value"}


def test_on_a_terminal_a_locked_secret_asks_for_the_password_once(tmp_path, monkeypatch, capsys):
    plain_store(("A_KEY", "B_KEY"))
    ss.set_password(PASSWORD)
    asked = []
    monkeypatch.setattr(ss, "terminal", lambda: True)

    def typed(prompt):
        asked.append(prompt)
        return ["a wrong password", PASSWORD][min(len(asked), 2) - 1]
    monkeypatch.setattr(ss, "ask_hidden", typed)
    assert ss.get("A_KEY") == CANARY and ss.get("B_KEY") == CANARY
    assert len(asked) == 2 and "Primary password" in asked[0], "a wrong one is asked again, then once per session"
    err = capsys.readouterr().err
    assert "wrong primary password" in err and "unlocked for 8 hours" in err
    ss.forget_keys()
    assert ss.get("A_KEY") == CANARY and len(asked) == 2, "the agent holds it for the next command"
    assert ss.main(["lock"], out=lambda line: None) == 0
    ss.forget_keys()
    monkeypatch.setattr(ss, "ask_hidden", lambda prompt: "")  # Enter alone: not now
    with pytest.raises(ss.Locked):
        ss.get("A_KEY")


def test_on_a_terminal_the_first_secret_asks_to_set_the_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ss, "terminal", lambda: True)
    typed = iter([PASSWORD, "a different one", PASSWORD, PASSWORD])
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(typed)
    monkeypatch.setattr(ss, "ask_hidden", ask)
    out = []
    ss.FileStore().set("GH_PAT", CANARY)
    assert CANARY.encode() not in tree_bytes(tmp_path / "data") and ss.get("GH_PAT") == CANARY
    assert len(prompts) == 4 and "again" in prompts[1]
    err = capsys.readouterr().err
    assert "do not match" in err and "primary password" in err
    assert ss.main(["status"], out=out.append) == 0 and "unlocked" in out[-1]


# --- end to end: ./sandbox-ai with a HOME of its own ---------------------------------------------------------------

def cli_env(home):
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": os.readlink(home / "run"),
           "SANDBOX_AI_SECRETS": "file", "LANG": "C.UTF-8"}
    for name in ("config", "data", "state", "cache"):
        env[f"XDG_{name.upper()}_HOME"] = str(home / name)
    return env


def cli(home, *args, input=None, **kwargs):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], input=input, capture_output=True, text=True,
                          env=cli_env(home), timeout=120, **kwargs)


@pytest.fixture
def home(tmp_path):
    """A HOME of its own; its runtime folder short (a socket's path is at most 104 bytes), as a login session's is."""
    import shutil
    import tempfile
    root = tmp_path / "home"
    root.mkdir(parents=True)
    short = tempfile.mkdtemp(prefix="sai-pp-", dir="/tmp")
    (root / "run").symlink_to(short)
    yield root
    cli(root, "secrets", "lock")
    shutil.rmtree(short, ignore_errors=True)


def test_e2e_unlocks_sent_at_once_do_not_get_more_guesses(home):
    """Regression (security review 1), through ./sandbox-ai: six `secrets unlock --password-stdin` with wrong
    passwords started together are six attempts in a row - three checked, three told to wait - not six checks."""
    assert cli(home, "secrets", "password", "set", "--password-stdin", "--locked",
               input=PASSWORD + "\n").returncode == 0
    started = [subprocess.Popen([str(ROOT / "sandbox-ai"), "secrets", "unlock", "--password-stdin"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=cli_env(home)) for _ in range(6)]
    for index, process in enumerate(started):
        process.stdin.write(f"not the password {index}\n")
        process.stdin.close()
    said = []
    for process in started:
        assert process.wait(120) == 1
        said.append(process.stderr.read())
        process.stdout.close()
        process.stderr.close()
    wrong = sum("wrong primary password" in text for text in said)
    waiting = sum("too many wrong passwords" in text for text in said)
    assert (wrong, waiting) == (ss.FREE_ATTEMPTS, 6 - ss.FREE_ATTEMPTS), said


def processes():
    """{pid: (command line, environment)} of every process this user may read."""
    found = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                found[int(entry.name)] = ((entry / "cmdline").read_bytes(), (entry / "environ").read_bytes())
            except OSError:
                continue
    return found


@needs_proc
def test_e2e_no_password_and_no_value_in_arguments_environments_logs_or_files(home):
    """The whole way through ./sandbox-ai, with the real scrypt cost: a canary password and a canary value are
    grepped across the HOME (every file: the store, the logs, the state) and every process's command line and
    environment while the agent runs."""
    first = cli(home, "secrets", "set", "GH_PAT", input=CANARY + "\n")
    assert first.returncode == 1 and "sandbox-ai secrets password set" in first.stderr
    assert cli(home, "secrets", "set", "GH_PAT", "--plain", input=CANARY + "\n").returncode == 0
    warned = cli(home, "secrets", "backend")
    assert warned.stdout.startswith("file (not encrypted")
    done = cli(home, "secrets", "password", "set", "--password-stdin", input=PWCANARY + "\n")
    assert done.returncode == 0, done.stderr
    assert "1 secret encrypted" in done.stdout and "unlocked for 8 hours" in done.stdout
    assert cli(home, "secrets", "set", "OPENAI_KEY", input="second-" + CANARY + "\n").returncode == 0
    listed = cli(home, "secrets", "list")
    assert "GH_PAT" in listed.stdout and "OPENAI_KEY" in listed.stdout
    status = json.loads(cli(home, "secrets", "status", "--json").stdout)
    assert status["protection"] == "primary-password" and status["locked"] is False and status["encrypted"] == 2 and \
        status["kdf"] == "scrypt n=131072 r=8 p=1"
    # another command reads the secret through the agent
    assert cli(home, "git-logins", "add", "https://git.example.org", "--secret", "GH_PAT").returncode == 0
    plan = cli(home, "git-logins", "check")
    assert plan.returncode == 0 and "https://git.example.org: ok" in plan.stdout, plan.stdout + plan.stderr
    # (the agent itself is not dumpable, so its /proc entries are closed to its own user too: how it is started
    # is test_the_agent_gets_the_key_through_a_pipe)
    for pid, (cmdline, environ) in processes().items():
        for canary in (CANARY, PWCANARY):
            assert canary.encode() not in cmdline, f"process {pid}: {cmdline[:200]!r}"
            assert canary.encode() not in environ or pid == os.getpid(), f"process {pid}'s environment"
    everything = tree_bytes(home)
    assert CANARY.encode() not in everything and PWCANARY.encode() not in everything
    assert CANARY not in plan.stdout + plan.stderr
    assert cli(home, "secrets", "lock").stdout.strip() == "locked"
    locked = cli(home, "git-logins", "check")
    assert locked.returncode != 0 and "sandbox-ai secrets unlock" in locked.stderr
    wrong = cli(home, "secrets", "unlock", "--password-stdin", input="not-" + PWCANARY + "x\n")
    assert wrong.returncode == 1 and "wrong primary password" in wrong.stderr
    assert cli(home, "secrets", "unlock", "--password-stdin", "--for", "5m", input=PWCANARY + "\n").stdout.strip() == \
        "unlocked for 5 minutes (sandbox-ai secrets lock ends it)"
    changed = cli(home, "secrets", "password", "change", "--password-stdin", input=f"{PWCANARY}\nnew-{PWCANARY}\n")
    assert changed.returncode == 0, changed.stderr
    removed = cli(home, "secrets", "password", "remove", "--password-stdin", input=f"new-{PWCANARY}\n")
    assert removed.returncode == 0 and "not encrypted" in removed.stdout
    assert (home / "data/sandbox-ai/secrets/GH_PAT").read_text() == CANARY
    assert json.loads(cli(home, "secrets", "status", "--json").stdout)["protection"] == "none"
    everything = tree_bytes(home).replace(CANARY.encode(), b"")
    assert PWCANARY.encode() not in everything, "the password is in no file, whatever was done with it"


def run_on_terminal(argv, env, typed, timeout=60):
    """ARGV on a terminal of its own (its controlling terminal, as getpass wants it), answering each prompt of
    TYPED [(what to wait for, what to type)] in turn. (status, everything it wrote)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(argv[0], argv, env)
    output, deadline, pending = b"", time.time() + timeout, list(typed)
    status = None
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not data:
                break
            output += data
        if pending and pending[0][0].encode() in output.split(b"\x00MARK")[-1]:
            output += b"\x00MARK"
            os.write(fd, pending.pop(0)[1].encode() + b"\n")
    else:
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), output.replace(b"\x00MARK", b"").decode("utf-8", "replace")


@needs_proc
def test_e2e_on_a_terminal_the_first_secret_sets_the_password_and_a_start_asks_for_it(home):
    env = cli_env(home)
    status, output = run_on_terminal(
        [str(ROOT / "sandbox-ai"), "secrets", "set", "GH_PAT"], env,
        [("New primary password", PWCANARY), ("again", PWCANARY), ("value of GH_PAT", CANARY)])
    assert status == 0, output
    assert "GH_PAT kept (file, encrypted)" in output and PWCANARY not in output and CANARY not in output, \
        "nothing typed is shown"
    assert CANARY.encode() not in tree_bytes(home) and PWCANARY.encode() not in tree_bytes(home)
    assert cli(home, "secrets", "lock").stdout.strip() == "locked"
    assert cli(home, "git-logins", "add", "https://git.example.org", "--secret", "GH_PAT").returncode == 1, \
        "locked, and no terminal: said, not asked"
    argv = [str(ROOT / "sandbox-ai"), "git-logins", "add", "https://git.example.org", "--secret", "GH_PAT"]
    status, output = run_on_terminal(argv, env, [("Primary password", "wrong-" + PWCANARY),
                                                 ("Primary password", PWCANARY)])
    assert status == 0 and "wrong primary password" in output and "unlocked for 8 hours" in output, output
    assert PWCANARY not in output and CANARY not in output
    assert json.loads(cli(home, "secrets", "status", "--json").stdout)["locked"] is False
    status, output = run_on_terminal([str(ROOT / "sandbox-ai"), "secrets", "password", "change"], env,
                                     [("Primary password", PWCANARY), ("New primary password", "next-" + PWCANARY),
                                      ("again", "next-" + PWCANARY)])
    assert status == 0 and "changed" in output, output
    assert cli(home, "secrets", "unlock", "--password-stdin", input="next-" + PWCANARY + "\n").returncode == 0


# --- the doctor ---------------------------------------------------------------------------------------------------

def test_the_doctor_says_which_protection_is_in_force(tmp_path, monkeypatch):
    dspec = importlib.util.spec_from_file_location("doctor_for_primary_password", ROOT / "sandbox/doctor.py")
    doctor = importlib.util.module_from_spec(dspec)
    dspec.loader.exec_module(doctor)
    assert doctor.check_secrets(ss) == ("ok", "Secrets", "file (no secrets yet; the first one asks for a primary "
                                                         "password)", "")
    plain_store()
    status, title, text, fix = doctor.check_secrets(ss)
    assert (status, title, fix) == ("warn", "Secrets", "sandbox-ai secrets password set") and "not encrypted" in text
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")
    assert doctor.check_secrets(ss)[0] == "ok" and "your choice" in doctor.check_secrets(ss)[2]
    monkeypatch.delenv("SANDBOX_AI_SECRETS_PLAIN")
    ss.set_password(PASSWORD)
    assert doctor.check_secrets(ss) == ("ok", "Secrets", "file (encrypted with the primary password; locked: "
                                                         "sandbox-ai secrets unlock)", "")
    (directory(tmp_path) / "LEFT").write_text("plain")
    (directory(tmp_path) / "LEFT").chmod(0o600)
    status, _, text, fix = doctor.check_secrets(ss)
    assert status == "warn" and "1 plain file left" in text and fix == "sandbox-ai secrets unlock"
    (directory(tmp_path) / ".primary.json").write_text("{")
    status, _, text, fix = doctor.check_secrets(ss)
    assert status == "fail" and "damaged" in text
    assert "Secrets" in doctor.GROUPS[0][1] and any(title == "Secrets" for _, title, *_ in doctor.checks())


def test_completion_offers_no_start_names_for_a_duration():
    cspec = importlib.util.spec_from_file_location("completion_for_primary_password", ROOT / "sandbox/completion.py")
    completion = importlib.util.module_from_spec(cspec)
    cspec.loader.exec_module(completion)
    parser = completion.parser_of("secrets")
    unlock = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["unlock"]
    action = next(a for a in unlock._actions if "--for" in a.option_strings)
    assert completion.provider("secrets", "unlock", action) == "free"
    assert completion.value_candidates(action, "--for") == []


@needs_proc
def test_e2e_hosts_run_hands_the_password_on_standard_input(home):
    """`sandbox-ai hosts run HOST -- secrets unlock --password-stdin` (what a script, the app and the web interface
    use for another host): the password is hosts run's standard input, handed on - the command line that reaches the
    host holds the words only."""
    assert cli(home, "secrets", "password", "set", "--password-stdin", "--locked", input=PWCANARY + "\n").returncode == 0
    done = cli(home, "hosts", "run", "local", "--", "secrets", "unlock", "--password-stdin", "--for", "5m",
               input=PWCANARY + "\n")
    assert done.returncode == 0 and "unlocked for 5 minutes" in done.stdout, done.stderr
    assert json.loads(cli(home, "secrets", "status", "--json").stdout)["locked"] is False
    hspec = importlib.util.spec_from_file_location("hosts_for_primary_password", ROOT / "sandbox/hosts.py")
    hosts = importlib.util.module_from_spec(hspec)
    hspec.loader.exec_module(hosts)
    path = home / "hosts.json"
    path.write_text(json.dumps({"version": 1, "hosts": {"ct1": {"ssh": "u@192.0.2.10"}}}))
    try:
        command = hosts.argv("ct1", ["secrets", "unlock", "--password-stdin"], path=path)
    except (ValueError, KeyError):
        pytest.skip("hosts.json of another form here")
    assert command[0] == "ssh" and "secrets unlock --password-stdin" in command[-1] and PWCANARY not in " ".join(command)
    assert PWCANARY.encode() not in tree_bytes(home)


def test_a_changed_header_never_makes_scrypt_take_gigabytes(monkeypatch):
    """24h review A (2026-11-17): derive() promises that a changed header never makes the process take gigabytes, and
    took n up to 2**22 with r up to 32 - 128 * n * r = 17 GiB for one attempt at the password (whoever unlocks: a
    terminal, the app, the web interface's service). The parameters' memory is bounded now (1 GiB; the store's own:
    128 MiB)."""
    called = []
    monkeypatch.setattr(ss.hashlib, "scrypt", lambda *a, **k: called.append(k) or b"k" * 64)
    salt = base64.b64encode(b"s" * 32).decode()
    for n, r in ((2 ** 22, 32), (2 ** 22, 8), (2 ** 21, 8), (2 ** 20, 16), (2 ** 19, 32)):
        with pytest.raises(ValueError, match="damaged"):
            ss.derive("x", {"name": "scrypt", "n": n, "r": r, "p": 1, "salt": salt})
    assert called == []
    for n, r in ((2 ** 17, 8), (2 ** 20, 8), (2 ** 18, 32)):  # the store's own cost, and stronger ones within the bound
        ss.derive("x", {"name": "scrypt", "n": n, "r": r, "p": 1, "salt": salt})
    assert [(k["n"], k["r"]) for k in called] == [(2 ** 17, 8), (2 ** 20, 8), (2 ** 18, 32)]
    assert all(128 * k["n"] * k["r"] <= ss.SCRYPT_MEMORY_MAX for k in called)
