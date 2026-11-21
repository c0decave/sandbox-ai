"""Credentials that lay in plain files beside the secret store move under it (docs/SECRETS_DESIGN.md, "Sealed
files"): OpenVPN profiles (NAME.ovpn, multi-line, up to 1 MiB) are sealed as files with the store's data key (or, with
the system's keyring or Keychain, a key kept there), their name bound in; the wizard's push tokens
(~/.config/sandbox-ai/tokens) become secrets, and the file a project names (--git-token-file URL=FILE) a reference to
it. Existing plain files are moved when the primary password is set, at the next unlock and when they are used; no
plain text stays behind. A locked store says how to unlock; old project files keep working."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ss = load("secret_store_sealed_files_tests", "secret_store.py")
launcher = load("agent_sandbox_sealed_files_tests", "agent_sandbox.py")
v = launcher.load_vpn_module()
wizard = load("wizard_sealed_files_tests", "wizard.py")

PASSWORD = "correct horse battery"
# (made at run time: no command line of whatever ran these tests can hold them)
KEY_CANARY = "OVPNKEYCANARY" + os.urandom(8).hex()      # a line of the client key in the .ovpn
TOKEN_CANARY = "tokcanary" + os.urandom(12).hex()       # a push token
PWCANARY = "PWCANARY-" + os.urandom(8).hex()
# a PEM key block's armour (put together here: no checker of what is published mistakes this file for a key)
BEGIN_KEY, END_KEY = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"

OPENVPN = f"""client
dev tun
proto udp
remote vpn1.example.org 1194
nobind
cipher AES-256-GCM
dhcp-option DNS 10.8.0.1
<ca>
-----BEGIN CERTIFICATE-----
MIIBfake
-----END CERTIFICATE-----
</ca>
<key>
{BEGIN_KEY}
{KEY_CANARY}
{END_KEY}
</key>
"""


@pytest.fixture(autouse=True)
def private(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir(mode=0o700)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_SECRETS_PLAIN", raising=False)
    monkeypatch.setattr(ss, "SCRYPT", {"n": 2 ** 12, "r": 8, "p": 1})
    monkeypatch.setattr(ss, "terminal", lambda: False)
    monkeypatch.setattr(launcher, "load_secret_store_module", lambda: ss)
    monkeypatch.setattr(v, "secret_store", lambda: ss)
    ss.forget_keys()
    yield
    ss.forget_keys()
    with open(os.devnull, "w") as null:
        ss.main(["lock"], out=lambda *a: None, err=null)


def secrets_dir(tmp_path):
    return tmp_path / "data/sandbox-ai/secrets"


def vpn_dir(tmp_path):
    return tmp_path / "config/sandbox-ai/vpn"


def tokens_dir(tmp_path):
    return tmp_path / "config/sandbox-ai/tokens"


def tree_bytes(root):
    return b"".join(p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file() and not p.is_symlink())


def plain_file(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def unlocked():
    ss.remember_key(secrets_dir(Path(os.environ["XDG_DATA_HOME"]).parent), ss.unlock_key(PASSWORD))


def openvpn_profile(tmp_path, name="work"):
    """A profile as `vpn add --openvpn` wrote it before 0.1.98: the configuration in a plain NAME.ovpn."""
    plain_file(vpn_dir(tmp_path) / f"{name}.ovpn", OPENVPN)
    profile = {"version": 1, "name": name, "type": "openvpn", "provider": None, "created": "2026-11-01T00:00:00Z",
               "openvpn": {"config_file": f"{name}.ovpn", "remotes": [{"host": "vpn1.example.org", "port": 1194,
                                                                       "proto": "udp"}],
                           "auth": False, "auth_secret": None, "auth_secret_owned": False, "full": True}}
    plain_file(vpn_dir(tmp_path) / f"{name}.json", json.dumps(profile))
    return vpn_dir(tmp_path) / f"{name}.ovpn"


# --- the file-sealing API -----------------------------------------------------------------------------------------

def test_a_file_is_sealed_with_the_stores_data_key_its_name_bound_in(tmp_path):
    key = ss.set_password(PASSWORD)
    ss.remember_key(secrets_dir(tmp_path), key)
    path = vpn_dir(tmp_path) / "work.ovpn"
    assert ss.write_file(path, "vpn/work.ovpn", OPENVPN.encode()) is True
    data = path.read_bytes()
    ident = ss.read_header()["id"]
    assert data.startswith(f"sandbox-ai-sealed-file-v1 primary:{ident}\n".encode())
    assert KEY_CANARY.encode() not in data and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ss.read_file(path, "vpn/work.ovpn") == (OPENVPN.encode(), True)
    # the same construction as a secret's (docs/SECRETS_DESIGN.md), its associated data: "file", the name, the key
    sealed = base64.b64decode(data.split(b"\n", 1)[1])
    aad = b"file\0vpn/work.ovpn\0primary:" + ident.encode()
    assert ss.unseal(key, aad, sealed) == OPENVPN.encode()
    # moved to another name, or changed: refused
    other = plain_file(vpn_dir(tmp_path) / "other.ovpn", "")
    other.write_bytes(data)
    with pytest.raises(ValueError, match="damaged or changed"):
        ss.read_file(other, "vpn/other.ovpn")
    flipped = bytearray(data)
    flipped[-10] ^= 1
    path.write_bytes(bytes(flipped))
    with pytest.raises(ValueError, match="damaged or changed"):
        ss.read_file(path, "vpn/work.ovpn")


def test_large_files_and_their_limit(tmp_path):
    ss.remember_key(secrets_dir(tmp_path), ss.set_password(PASSWORD))
    big = os.urandom(ss.FILE_MAX)
    path = tmp_path / "config/big.ovpn"
    ss.write_file(path, "vpn/big.ovpn", big)
    assert ss.read_file(path, "vpn/big.ovpn") == (big, True)
    with pytest.raises(ValueError, match="larger than"):
        ss.write_file(path, "vpn/big.ovpn", big + b"x")
    for bad in ("work.ovpn", "vpn/../x", "vpn/", "VPN/x", "vpn/a b"):
        with pytest.raises(ValueError):
            ss.write_file(tmp_path / "x", bad, b"data")


def test_through_the_agent_and_locked(tmp_path):
    """Unlocked by `secrets unlock`, the agent seals and opens files (the key stays there); locked, a file is not
    opened and the message says how to unlock."""
    ss.set_password(PASSWORD)
    assert ss.main(["unlock", "--password-stdin"], stdin=io.StringIO(PASSWORD + "\n"), out=lambda *a: None) == 0
    ss.forget_keys()
    path = vpn_dir(tmp_path) / "work.ovpn"
    big = OPENVPN.encode() + os.urandom(600 * 1024)
    assert ss.write_file(path, "vpn/work.ovpn", big) is True
    ss.forget_keys()
    assert ss.read_file(path, "vpn/work.ovpn") == (big, True)
    assert ss.main(["lock"], out=lambda *a: None) == 0
    ss.forget_keys()
    with pytest.raises(ss.Locked, match="sandbox-ai secrets unlock"):
        ss.read_file(path, "vpn/work.ovpn")
    with pytest.raises(ss.Locked, match="sandbox-ai secrets unlock"):
        ss.write_file(path, "vpn/work.ovpn", b"new")


def test_the_agent_seals_files_for_its_own_store_only(tmp_path):
    key, ident = os.urandom(64), "ab" * 16
    agent = ss.Agent(key, ident, tmp_path / "x.sock", 60)
    data = base64.b64encode(b"profile").decode()
    sealed = agent.answer({"op": "seal_file", "name": "vpn/w.ovpn", "keyref": f"primary:{ident}", "data": data})
    assert sealed["ok"] and agent.answer({"op": "open_file", "name": "vpn/w.ovpn", "keyref": f"primary:{ident}",
                                          "data": sealed["data"]})["value"] == data
    for request in ({"op": "open_file", "name": "vpn/w.ovpn", "keyref": "primary:" + "cd" * 16, "data": sealed["data"]},
                    {"op": "open_file", "name": "vpn/x.ovpn", "keyref": f"primary:{ident}", "data": sealed["data"]},
                    {"op": "seal_file", "name": "vpn/w.ovpn", "keyref": "system:SANDBOX_AI_FILE_KEY", "data": data},
                    {"op": "seal_file", "name": "../w", "keyref": f"primary:{ident}", "data": data},
                    {"op": "seal_file", "name": "vpn/w.ovpn", "keyref": f"primary:{ident}", "data": "not base64!"}):
        assert agent.answer(request)["ok"] is False, request


def test_without_a_password_a_new_file_needs_one_or_the_plain_choice(tmp_path, monkeypatch):
    path = vpn_dir(tmp_path) / "work.ovpn"
    with pytest.raises(ValueError, match="sandbox-ai secrets password set"):
        ss.write_file(path, "vpn/work.ovpn", OPENVPN.encode())
    assert not path.exists()
    assert ss.write_file(path, "vpn/work.ovpn", OPENVPN.encode(), plain=True) is False
    assert path.read_text() == OPENVPN and ss.read_file(path, "vpn/work.ovpn") == (OPENVPN.encode(), False)
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")
    assert ss.write_file(path, "vpn/work.ovpn", OPENVPN.encode()) is False


class FakeKeyring:
    """The system's keyring, in memory."""
    name = "keyring"

    def __init__(self):
        self.values = {}

    def names(self):
        return sorted(self.values)

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        return self.values.pop(name, None) is not None


def test_with_the_keyring_a_file_is_sealed_with_a_key_kept_there(tmp_path, monkeypatch):
    """Where the keyring or the Keychain keeps the secrets there is no primary password: a file is sealed with a
    random key kept in the keyring (SANDBOX_AI_FILE_KEY), made once; without it the file is not opened."""
    keyring = FakeKeyring()
    monkeypatch.setattr(ss, "store", lambda: keyring)
    path = vpn_dir(tmp_path) / "work.ovpn"
    assert ss.write_file(path, "vpn/work.ovpn", OPENVPN.encode()) is True
    assert path.read_bytes().startswith(b"sandbox-ai-sealed-file-v1 system:SANDBOX_AI_FILE_KEY\n")
    assert KEY_CANARY.encode() not in tree_bytes(tmp_path)
    first = keyring.values["SANDBOX_AI_FILE_KEY"]
    assert len(first) == 128 and int(first, 16) >= 0
    ss.write_file(vpn_dir(tmp_path) / "two.ovpn", "vpn/two.ovpn", b"two")
    assert keyring.values["SANDBOX_AI_FILE_KEY"] == first, "one key, made once"
    assert ss.read_file(path, "vpn/work.ovpn") == (OPENVPN.encode(), True)
    out = []
    monkeypatch.setattr(ss, "store", lambda: keyring)
    assert ss.main(["delete", "SANDBOX_AI_FILE_KEY"], out=out.append, err=io.StringIO()) == 1, \
        "the key of sealed files is not deleted while files need it"
    del keyring.values["SANDBOX_AI_FILE_KEY"]
    with pytest.raises(ValueError, match="SANDBOX_AI_FILE_KEY"):
        ss.read_file(path, "vpn/work.ovpn")


# --- migration: when the password is set, at the next unlock, when used -----------------------------------------

def test_setting_the_password_seals_the_ovpn_files_and_moves_the_tokens(tmp_path):
    ovpn = openvpn_profile(tmp_path)
    token = plain_file(tokens_dir(tmp_path) / "github.com", TOKEN_CANARY + "\n")
    old_ovpn, old_token = os.open(ovpn, os.O_RDONLY), os.open(token, os.O_RDONLY)  # the old inodes
    assert KEY_CANARY.encode() in tree_bytes(tmp_path) and TOKEN_CANARY.encode() in tree_bytes(tmp_path)
    out = []
    assert ss.main(["password", "set", "--password-stdin", "--locked"], stdin=io.StringIO(PASSWORD + "\n"),
                   out=out.append) == 0
    text = "\n".join(out)
    assert "vpn/work.ovpn" in text and "GIT_TOKEN_GITHUB_COM" in text, text
    everything = tree_bytes(tmp_path)
    assert KEY_CANARY.encode() not in everything and TOKEN_CANARY.encode() not in everything, "no plain text left"
    for fd in (old_ovpn, old_token):  # written over before it went (a file system that writes in place)
        assert KEY_CANARY.encode() not in os.pread(fd, 1 << 16, 0) and TOKEN_CANARY.encode() not in os.pread(fd, 1 << 16, 0)
        os.close(fd)
    assert ovpn.read_bytes().startswith(b"sandbox-ai-sealed-file-v1 primary:")
    assert token.read_text().startswith("sandbox-ai-token-moved-to-secret GIT_TOKEN_GITHUB_COM")
    assert sorted(p.name for p in tokens_dir(tmp_path).iterdir()) == ["github.com"], "no temporary file left"
    unlocked()
    assert ss.get("GIT_TOKEN_GITHUB_COM") == TOKEN_CANARY
    assert v.payload(v.load_profile("work"), "check")["secrets"]["openvpn_config"] == OPENVPN


def test_the_next_unlock_seals_what_came_later_and_removing_the_password_gives_plain_files_back(tmp_path):
    ss.set_password(PASSWORD)
    ovpn = openvpn_profile(tmp_path)  # (an older sandbox-ai, a restore)
    plain_file(tokens_dir(tmp_path) / "git.example.org-3000", TOKEN_CANARY + "\n")
    out = []
    assert ss.main(["unlock", "--password-stdin"], stdin=io.StringIO(PASSWORD + "\n"), out=out.append) == 0
    assert KEY_CANARY.encode() not in tree_bytes(tmp_path) and TOKEN_CANARY.encode() not in tree_bytes(tmp_path)
    assert any("vpn/work.ovpn" in line for line in out)
    ss.forget_keys()
    assert ss.get("GIT_TOKEN_GIT_EXAMPLE_ORG_3000") == TOKEN_CANARY
    moved = []
    assert ss.remove_password(PASSWORD, moved=moved) == 1 and moved == ["vpn/work.ovpn"]
    assert ovpn.read_text() == OPENVPN, "a file sealed with the primary password is plain again"
    assert ss.get("GIT_TOKEN_GIT_EXAMPLE_ORG_3000") == TOKEN_CANARY, "the token stays a secret (plain now)"


def test_a_plain_ovpn_is_sealed_when_used_and_read_plain_while_locked(tmp_path, capsys):
    ss.set_password(PASSWORD)
    ovpn = openvpn_profile(tmp_path)
    # locked, nobody to ask: the plain file still works (it is plain already), with the warning
    assert v.payload(v.load_profile("work"), "check")["secrets"]["openvpn_config"] == OPENVPN
    assert "plain file" in capsys.readouterr().err and ovpn.read_text() == OPENVPN
    unlocked()
    assert v.payload(v.load_profile("work"), "check")["secrets"]["openvpn_config"] == OPENVPN
    assert KEY_CANARY.encode() not in tree_bytes(tmp_path), "sealed at its first use while unlocked"
    ss.forget_keys()
    with pytest.raises(ValueError, match="sandbox-ai secrets unlock"):
        v.payload(v.load_profile("work"), "check")


def test_vpn_add_seals_and_remove_wipes(tmp_path, capsys):
    ss.remember_key(secrets_dir(tmp_path), ss.set_password(PASSWORD))
    source = plain_file(tmp_path / "home/provider.ovpn", OPENVPN)
    assert v.main(["add", "work", "--openvpn", str(source)]) == 0
    source.unlink()
    assert KEY_CANARY.encode() not in tree_bytes(tmp_path)
    config = v.payload(v.load_profile("work"), "serve")["secrets"]["openvpn_config"]
    assert f"<key>\n{BEGIN_KEY}\n{KEY_CANARY}\n" in config
    ovpn = vpn_dir(tmp_path) / "work.ovpn"
    old = os.open(ovpn, os.O_RDONLY)
    assert v.main(["remove", "work"]) == 0
    assert not ovpn.exists() and os.pread(old, 64, 0) == bytes(64), "written over before it went"
    os.close(old)


def test_vpn_add_on_a_store_without_password_says_what_to_do(tmp_path, monkeypatch):
    source = plain_file(tmp_path / "home/provider.ovpn", OPENVPN)
    with pytest.raises(ValueError, match="sandbox-ai secrets password set"):
        v.main(["add", "work", "--openvpn", str(source)])
    assert not (vpn_dir(tmp_path) / "work.ovpn").exists() and not (vpn_dir(tmp_path) / "work.json").exists()
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")  # (the choice of a machine without anyone to ask)
    assert v.main(["add", "work", "--openvpn", str(source)]) == 0
    assert (vpn_dir(tmp_path) / "work.ovpn").read_text().startswith("client")


def test_status_counts_the_plain_files(tmp_path):
    openvpn_profile(tmp_path)
    plain_file(tokens_dir(tmp_path) / "github.com", TOKEN_CANARY + "\n")
    info = ss.protection()
    assert info["plain_files"] == [str(vpn_dir(tmp_path) / "work.ovpn"), str(tokens_dir(tmp_path) / "github.com")]
    out = []
    assert ss.main(["status"], out=out.append) == 0 and "2 plain files" in "\n".join(out)


# --- push tokens --------------------------------------------------------------------------------------------------

def test_token_secret_names():
    assert ss.token_secret_name("github.com") == "GIT_TOKEN_GITHUB_COM"
    assert ss.token_secret_name("http://localhost:3080") == "GIT_TOKEN_LOCALHOST_3080"
    assert ss.token_secret_name("https-git.example.org-3000") == "GIT_TOKEN_GIT_EXAMPLE_ORG_3000"
    long = ss.token_secret_name("a" * 200)
    assert len(long) <= 64 and ss.NAME.fullmatch(long) and long != ss.token_secret_name("a" * 199)


def test_a_token_file_of_the_wizard_moves_into_the_store_at_its_first_use(tmp_path, capsys):
    ss.remember_key(secrets_dir(tmp_path), ss.set_password(PASSWORD))
    path = plain_file(tokens_dir(tmp_path) / "github.com", TOKEN_CANARY + "\n")
    assert launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file") == TOKEN_CANARY
    assert TOKEN_CANARY.encode() not in tree_bytes(tmp_path)
    err = capsys.readouterr().err
    assert "GIT_TOKEN_GITHUB_COM" in err and "--git-token-secret" in err
    # the project file that names the old file keeps working; said once
    assert launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file") == TOKEN_CANARY
    assert capsys.readouterr().err == ""
    ss.forget_keys()
    with pytest.raises(ValueError, match="sandbox-ai secrets unlock"):
        launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file")
    unlocked()
    ss.FileStore().delete("GIT_TOKEN_GITHUB_COM")
    with pytest.raises(ValueError, match="GIT_TOKEN_GITHUB_COM"):
        launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file")


def test_a_token_file_elsewhere_keeps_working_with_the_warning(tmp_path, capsys):
    ss.remember_key(secrets_dir(tmp_path), ss.set_password(PASSWORD))
    path = plain_file(tmp_path / "home/my-token", TOKEN_CANARY + "\n")
    assert launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file") == TOKEN_CANARY
    err = capsys.readouterr().err
    assert "plain file" in err and "--git-token-secret" in err
    assert path.read_text() == TOKEN_CANARY + "\n", "a file of the user's own is never changed"


def test_a_wizard_token_file_without_a_password_is_read_plain_and_says_how_it_moves(tmp_path, capsys):
    path = plain_file(tokens_dir(tmp_path) / "github.com", TOKEN_CANARY + "\n")
    assert launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file") == TOKEN_CANARY
    assert "once a primary password is set: sandbox-ai secrets password set" in capsys.readouterr().err
    assert path.read_text() == TOKEN_CANARY + "\n", "nothing to move it into yet"


def test_the_same_name_with_another_token_gets_a_name_of_its_own(tmp_path):
    ss.remember_key(secrets_dir(tmp_path), ss.set_password(PASSWORD))
    ss.FileStore().set("GIT_TOKEN_GITHUB_COM", "an-older-token-1234")
    path = plain_file(tokens_dir(tmp_path) / "github.com", TOKEN_CANARY + "\n")
    assert launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file") == TOKEN_CANARY
    assert ss.get("GIT_TOKEN_GITHUB_COM") == "an-older-token-1234" and ss.get("GIT_TOKEN_GITHUB_COM_2") == TOKEN_CANARY


def test_the_wizard_keeps_a_typed_token_as_a_secret(tmp_path):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    (project / ".git/config").write_text('[remote "origin"]\n\turl = http://localhost:3080/alice/x.git\n')
    pending, lines = [], []
    answers = iter(["", "enter", "y", "", ""])
    secrets = iter(["ghp_" + TOKEN_CANARY, TOKEN_CANARY])
    p = wizard.Prompter(lambda *a: next(answers), lines.append, lambda *a: next(secrets))
    options = wizard.ask_git(p, {"backend": "podman", "network": "default"}, project, pending)
    assert options == ["--git-identity", "--git-token-secret", "https://github.com=GIT_TOKEN_GITHUB_COM",
                       "--git-token-secret", "http://localhost:3080=GIT_TOKEN_LOCALHOST_3080"]
    assert pending == [("GIT_TOKEN_GITHUB_COM", "ghp_" + TOKEN_CANARY), ("GIT_TOKEN_LOCALHOST_3080", TOKEN_CANARY)]
    assert not tokens_dir(tmp_path).exists()


def test_wizard_edit_offers_a_moved_token_file_as_its_secret(tmp_path):
    ss.remember_key(secrets_dir(tmp_path), ss.set_password(PASSWORD))
    path = plain_file(tokens_dir(tmp_path) / "github.com", TOKEN_CANARY + "\n")
    launcher.read_token_file(path, launcher.GIT_TOKEN, "--git-token-file")
    url, option, description = wizard.token_entry("--git-token-file", f"https://github.com={path}")
    assert option == ["--git-token-secret", "https://github.com=GIT_TOKEN_GITHUB_COM"]
    assert "GIT_TOKEN_GITHUB_COM" in description


# --- end to end: ./sandbox-ai with a HOME of its own -----------------------------------------------------------

def cli_env(home, **extra):
    env = {"HOME": str(home), "PATH": f"{home / 'bin'}:{os.environ['PATH']}", "XDG_RUNTIME_DIR": os.readlink(home / "run"),
           "SANDBOX_AI_SECRETS": "file", "LANG": "C.UTF-8", **extra}
    for name in ("config", "data", "state", "cache"):
        env[f"XDG_{name.upper()}_HOME"] = str(home / name)
    return env


def cli(home, *args, input=None, **extra):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], input=input, capture_output=True, text=True,
                          env=cli_env(home, **extra), timeout=180)


# A podman that answers what `vpn check` asks: the VPN container (run) reads its standard input - the payload - and
# says the SHA-256 of the client key's line in the OpenVPN configuration it got, never the key itself; the rest
# ends at once.
FAKE_PODMAN = """#!/usr/bin/env python3
import hashlib, json, sys
if "run" in sys.argv[1:3]:
    data = json.loads(sys.stdin.readline())
    lines = data["secrets"].get("openvpn_config", "").splitlines()
    key = lines[lines.index("-----BEGIN " + "PRIVATE KEY-----") + 1]
    print(json.dumps({"event": "error", "message": "fake key " + hashlib.sha256(key.encode()).hexdigest()}),
          flush=True)
sys.exit(0)
"""


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    root.mkdir(parents=True, exist_ok=True)
    short = tempfile.mkdtemp(prefix="sai-sf-", dir="/tmp")
    (root / "run").symlink_to(short)
    (root / "bin").mkdir()
    (root / "bin/podman").write_text(FAKE_PODMAN)
    (root / "bin/podman").chmod(0o755)
    yield root
    cli(root, "secrets", "lock")
    shutil.rmtree(short, ignore_errors=True)


def test_e2e_password_set_moves_the_files_and_old_project_files_keep_working(home):
    """Through ./sandbox-ai with the real scrypt cost: a plain store with an OpenVPN profile and a token file the
    wizard wrote, named by a trusted project file; `secrets password set` seals and moves them, no canary is left
    anywhere in the HOME, `vpn check` and a start's dry run work unlocked and say how to unlock when locked."""
    import hashlib
    source = home / "provider.ovpn"
    source.write_text(OPENVPN)
    added = cli(home, "vpn", "add", "work", "--openvpn", str(source), "--check-url", "https://127.0.0.1:9/",
                SANDBOX_AI_SECRETS_PLAIN="1")
    assert added.returncode == 0, added.stderr
    source.unlink()
    token = plain_file(home / "config/sandbox-ai/tokens/github.com", TOKEN_CANARY + "\n")
    project = home / "proj"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps(
        {"version": 1, "agent": "codex", "options": ["--git-token-file", f"https://github.com={token}", "--token-proxy"]}))
    trusted = cli(home, "wizard", "trust", "--yes", str(project / ".sandbox-ai.json"))
    assert trusted.returncode == 0, trusted.stdout + trusted.stderr
    assert KEY_CANARY.encode() in tree_bytes(home) and TOKEN_CANARY.encode() in tree_bytes(home)
    done = cli(home, "secrets", "password", "set", "--password-stdin", input=PWCANARY + "\n")
    assert done.returncode == 0, done.stderr
    assert "vpn/work.ovpn" in done.stdout and "GIT_TOKEN_GITHUB_COM" in done.stdout, done.stdout
    everything = tree_bytes(home)
    assert KEY_CANARY.encode() not in everything and TOKEN_CANARY.encode() not in everything
    assert PWCANARY.encode() not in everything
    status = json.loads(cli(home, "secrets", "status", "--json").stdout)
    assert status["plain_files"] == [] and status["locked"] is False
    # vpn check: the configuration reaches the VPN container (its standard input) whole
    checked = cli(home, "vpn", "check", "work")
    assert f"fake key {hashlib.sha256(KEY_CANARY.encode()).hexdigest()}" in checked.stdout + checked.stderr, \
        checked.stdout + checked.stderr
    # the old project file still names the token file: it resolves to the secret, said once
    plan = cli(home, "codex", "--workspace", str(project), "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "GIT_TOKEN_GITHUB_COM" in plan.stderr and TOKEN_CANARY not in plan.stdout + plan.stderr
    again = cli(home, "codex", "--workspace", str(project), "--dry-run")
    assert again.returncode == 0 and "GIT_TOKEN_GITHUB_COM" not in again.stderr
    assert cli(home, "secrets", "lock").returncode == 0
    locked = cli(home, "vpn", "check", "work")
    assert locked.returncode != 0 and "sandbox-ai secrets unlock" in locked.stderr, locked.stdout + locked.stderr
    locked = cli(home, "codex", "--workspace", str(project), "--dry-run")
    assert locked.returncode != 0 and "sandbox-ai secrets unlock" in locked.stderr, locked.stdout + locked.stderr
    assert KEY_CANARY.encode() not in tree_bytes(home) and TOKEN_CANARY.encode() not in tree_bytes(home)


def test_e2e_a_new_vpn_profile_is_sealed_and_a_new_wizard_token_is_a_secret(home):
    assert cli(home, "secrets", "password", "set", "--password-stdin", input=PWCANARY + "\n").returncode == 0
    source = home / "provider.ovpn"
    source.write_text(OPENVPN)
    added = cli(home, "vpn", "add", "work", "--openvpn", str(source))
    assert added.returncode == 0, added.stderr
    source.unlink()
    assert (home / "config/sandbox-ai/vpn/work.ovpn").read_bytes().startswith(b"sandbox-ai-sealed-file-v1 primary:")
    assert KEY_CANARY.encode() not in tree_bytes(home)
    shown = cli(home, "vpn", "show", "work")
    assert shown.returncode == 0 and "vpn1.example.org" in shown.stdout
    assert cli(home, "vpn", "remove", "work").returncode == 0
    assert not (home / "config/sandbox-ai/vpn/work.ovpn").exists()


def test_the_doctor_names_plain_profile_and_token_files(tmp_path):
    doctor = load("doctor_sealed_files_tests", "doctor.py")
    assert doctor.check_secrets(ss)[0] == "ok"
    openvpn_profile(tmp_path)
    status, _, text, fix = doctor.check_secrets(ss)
    assert status == "warn" and "work.ovpn" in text and fix == "sandbox-ai secrets password set"
    ss.set_password(PASSWORD)
    assert ss.protection()["plain_files"] == [], "setting the password sealed it"
    assert doctor.check_secrets(ss)[0] == "ok"
