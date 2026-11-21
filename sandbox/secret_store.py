#!/usr/bin/env python3
"""sandbox-ai secrets: API keys and tokens for sandboxes, kept in your system keyring.

  sandbox-ai secrets list                  names (never values) and where they are kept
  sandbox-ai secrets set NAME [--plain]    read the value from stdin (or --from-env VARIABLE)
  sandbox-ai secrets delete NAME
  sandbox-ai secrets rename OLD NEW        another name for a secret, in everything that names it (never its value)
  sandbox-ai secrets send NAME HOST [--as NAME] [--replace] [--yes]
                                           keep this machine's secret NAME on a host of sandbox-ai hosts as well
  sandbox-ai secrets backend               keyring, keychain (macOS) or file, and which protection is in force
  sandbox-ai secrets status [--json]       the same with the numbers: encrypted, plain, locked or unlocked
  sandbox-ai secrets password set|change|remove [--password-stdin | --password-fd N]
                                           the primary password that encrypts the private files
  sandbox-ai secrets unlock [--for DURATION] [--idle DURATION] [--password-stdin | --password-fd N]
  sandbox-ai secrets lock
  sandbox-ai secrets give RUN NAME --api-key HOST [--as VARIABLE] [--keep]
                                           a running sandbox gets the secret NAME as an API key for HOST, now
  sandbox-ai secrets revoke RUN NAME       take back a key given during the run (NAME: the secret or its variable)
  sandbox-ai secrets list --run RUN [--json]   the API keys a running sandbox was given during the run

Secrets go to the Secret Service (GNOME Keyring, KWallet, KeePassXC; through secret-tool) when it is there, on macOS
to the login Keychain (through security), else to private files (0600) in ~/.local/share/sandbox-ai/secrets.
Sandboxes get them only when named: --secret-env NAME (an environment variable for the agent), --git-token-secret
URL=NAME, or an MCP server's variable (sandbox-ai mcp add ... --env NAME). Values are never printed. A value is
replaced by keeping the name again (`secrets set NAME`); `secrets rename OLD NEW` gives it another name and
carries the name through what sandbox-ai keeps: the Git and publish logins, the identities, VPN profiles, the
options of the trusted project files (.sandbox-ai.json, trusted again as written) and of the kept starts; it says
what still names the old one (an LLM endpoint, an MCP server, a project file changed since you trusted it).

The private files are encrypted with a primary password (as a browser's password manager has one): the first
secret kept in them asks you to set it (sandbox-ai secrets password set; typed twice, never shown; for scripts on
standard input or a descriptor, never as an argument). Without it nobody reads a secret from the files: not a copy
of the disk, not a backup, not another program that reads your HOME. It is asked once per session: `secrets unlock`
keeps the key in memory, in a small process of your user (8 hours unless --for says otherwise: 30m, 8h, 2d; --idle
ends it after that long without use; `secrets lock` ends it now), and a start that needs a secret while locked asks
on its terminal. On another host: sandbox-ai hosts run HOST -- secrets unlock (the password typed there, or
--password-stdin). A store without a password keeps working, with a warning; `set --plain` (SANDBOX_AI_SECRETS_PLAIN=1)
is your choice to keep plain files. The keyring and the Keychain are encrypted by the system and need none.
OpenVPN profiles (~/.config/sandbox-ai/vpn/NAME.ovpn) are encrypted as files with the same key (with the keyring: a
key kept there, SANDBOX_AI_FILE_KEY), and push tokens typed in the wizard are secrets (GIT_TOKEN_HOST); plain ones
from before move when the password is set, at the next unlock, or when used.
docs/SECRETS_DESIGN.md has the construction and what it does and does not protect from.

A sandbox that runs already gets a secret as an API key with `give` (RUN: its run id or its start's name, as
`sandbox-ai list` shows it): its connection proxy puts the key in on the wire for HTTPS requests to HOST - in a
credential header, also as the password of a Basic login (git over HTTPS) - where the agent sends the placeholder
that `export VARIABLE=...` (said by give) stands for; the agent never sees the key, and HOST is allowed too. It is
the same step as the older form `sandbox-ai net allow RUN --api-key HOST=VARIABLE=NAME`, which keeps working. For
`git push` to a Git server, a Git login is meant: sandbox-ai git-logins give RUN URL --secret NAME.

A sandbox on another host of `sandbox-ai hosts` is given that host's secrets (sandbox-ai hosts run HOST -- secrets
give RUN NAME --api-key ...): a secret of this machine never goes there by itself. `secrets send NAME HOST` is
your own step that copies one: asked first (or --yes), its value goes over the SSH connection to HOST on standard
input, straight into `sandbox-ai secrets set` there - never in a command line, a log or a temporary file, here or
there - and is kept in that host's secret store from then on (delete it there: sandbox-ai hosts run HOST -- secrets
delete NAME). A secret of that name kept there already is replaced only with --replace.

Standard library only.
"""
import argparse
import base64
import binascii
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import types
import unicodedata

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
LIMIT = 8192



def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"a secret's name is letters, digits and _ (up to 64), not starting with a digit: {name!r}")
    return name


def files_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "secrets"


# --- the primary password of the private files (docs/SECRETS_DESIGN.md) -----------------------------------------
#
# Format version 1. The store's folder holds .primary.json (the header: the key derivation's parameters with the
# store's own salt, and the store's data key sealed with the key derived from the password) and one file per
# secret: MAGIC, then base64 of nonce | ciphertext | tag. Sealing is encrypt-then-MAC from the standard library
# alone (it has no AES): per message, an encryption key and an authentication key are derived from the key and a
# random 192-bit nonce with HMAC-SHA-512; the text is XORed with HMAC-SHA-512(encryption key, counter) blocks (a
# PRF in counter mode), and HMAC-SHA-512 over the associated data, the nonce and the ciphertext, cut to 256 bits,
# is the tag, compared in constant time before anything is decrypted. The text is padded to 64-byte steps, so a
# token's length does not show. A secret's name is its associated data: a file moved to another name is refused.
MAGIC = "sandbox-ai-secret-v1:"
HEADER = ".primary.json"
VERSION = 1
KEY, NONCE, TAG, BLOCK, PAD = 64, 24, 32, 64, 64
# scrypt, the cost of one guess: 128 * n * r bytes of memory (128 MiB) and about 0.3 s of one core. A Python built
# without scrypt (LibreSSL) takes PBKDF2-HMAC-SHA-512 instead; the header says which, so each store is read anywhere.
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
SCRYPT_MEMORY_MAX = 1 << 30  # 128 * n * r of a header's parameters at most (the store's own: 128 MiB)
PBKDF2_ROUNDS = 1_200_000
PASSWORD_MIN, PASSWORD_MAX = 8, 1024
READ_LIMIT = 65536
FREE_ATTEMPTS, ATTEMPT_WAIT_MAX = 3, 300  # wrong passwords: three at once, then 1, 2, 4 ... 300 s between attempts
DEFAULT_LIFETIME = 8 * 3600
LIFETIME_MAX = 30 * 86400
SOCKET_LIMIT = 100  # a socket's path: 104 bytes on macOS, 108 on Linux
PLAIN_CHOICE = "SANDBOX_AI_SECRETS_PLAIN"
SET_PASSWORD = "sandbox-ai secrets password set"
UNLOCK = "sandbox-ai secrets unlock"
# Sealed files (an OpenVPN profile: multi-line, up to 1 MiB, more than a secret's one line of 8 KiB): the first line
# says the format and which key sealed it (FILE_KEYREF), the second is base64 of seal(key, "file\0" + NAME + "\0" +
# KEYREF, the file's bytes) - the construction of a secret's, the file's name (vpn/work.ovpn) and the key's reference
# bound in. The key: the store's data key ("primary:" + the store's id) where the private files keep the secrets,
# else a random key kept in the system's keyring or Keychain as the secret FILES_KEY ("system:" + its name).
FILE_MAGIC = b"sandbox-ai-sealed-file-v1 "
FILE_MAX = 1 << 20
FILE_NAME = re.compile(r"[a-z]{1,16}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
FILE_KEYREF = re.compile(r"primary:[0-9a-f]{32}|system:[A-Za-z_][A-Za-z0-9_]{0,63}")
FILES_KEY = "SANDBOX_AI_FILE_KEY"
WIRE_LIMIT = 4 * FILE_MAX  # one request to the agent or its answer: a file's base64 in JSON
# The wizard's push tokens (~/.config/sandbox-ai/tokens/LABEL) moved into the store: the file holds this line and the
# secret's name, so the --git-token-file URL=FILE of existing project files keeps working.
TOKEN_REF = "sandbox-ai-token-moved-to-secret "


class Damaged(ValueError):
    """A sealed text that is not what was sealed under this key and name."""


class WrongPassword(ValueError):
    pass


class Throttled(ValueError):
    pass


class Locked(ValueError):
    """The secret is encrypted and nothing here holds the key."""


def clock():
    return time.time()


def _prf(key, *parts):
    return hmac.new(key, b"".join(parts), hashlib.sha512).digest()


def _message_keys(key, nonce):
    return (_prf(key, b"sandbox-ai secrets v1 encrypt\0", nonce), _prf(key, b"sandbox-ai secrets v1 authenticate\0", nonce))


def _stream(key, data):
    """DATA XOR HMAC-SHA-512(KEY, counter 0) | (KEY, counter 1) | ... (the XOR of two whole numbers: a file of 1 MiB
    in a fraction of a second, the same bytes as block by block)."""
    blocks = -(-len(data) // BLOCK)
    pad = b"".join(_prf(key, index.to_bytes(8, "big")) for index in range(blocks))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(len(data), "big")


def _tag(key, aad, nonce, ciphertext):
    return _prf(key, len(aad).to_bytes(8, "big"), aad, nonce, ciphertext)[:TAG]


def seal(key, aad, plaintext, nonce=None):
    """nonce | ciphertext | tag of PLAINTEXT (bytes) under KEY (64 bytes), bound to AAD (bytes)."""
    if len(key) != KEY:
        raise ValueError("a key of 64 bytes")
    nonce = os.urandom(NONCE) if nonce is None else nonce
    padded = len(plaintext).to_bytes(4, "big") + plaintext
    padded += bytes(-len(padded) % PAD)
    encrypt, authenticate = _message_keys(key, nonce)
    ciphertext = _stream(encrypt, padded)
    return nonce + ciphertext + _tag(authenticate, aad, nonce, ciphertext)


def unseal(key, aad, sealed):
    """The plaintext of seal(); Damaged when the tag does not fit KEY, AAD and the text (checked first)."""
    if len(key) != KEY or len(sealed) < NONCE + PAD + TAG or (len(sealed) - NONCE - TAG) % PAD:
        raise Damaged("damaged or changed")
    nonce, ciphertext, tag = sealed[:NONCE], sealed[NONCE:-TAG], sealed[-TAG:]
    encrypt, authenticate = _message_keys(key, nonce)
    if not hmac.compare_digest(tag, _tag(authenticate, aad, nonce, ciphertext)):
        raise Damaged("damaged or changed")
    padded = _stream(encrypt, ciphertext)
    length = int.from_bytes(padded[:4], "big")
    if length > len(padded) - 4:
        raise Damaged("damaged or changed")
    return padded[4:4 + length]


def has_scrypt():
    return hasattr(hashlib, "scrypt")


def new_kdf():
    salt = base64.b64encode(os.urandom(32)).decode()
    if has_scrypt():
        return {"name": "scrypt", **SCRYPT, "salt": salt}
    return {"name": "pbkdf2-sha512", "rounds": PBKDF2_ROUNDS, "salt": salt}


def kdf_text(kdf):
    if kdf.get("name") == "scrypt":
        return f"scrypt n={kdf.get('n')} r={kdf.get('r')} p={kdf.get('p')}"
    return f"{kdf.get('name')} rounds={kdf.get('rounds')}"


def _whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def derive(password, kdf):
    """The 64-byte key of PASSWORD under the header's KDF (its parameters are checked: a changed header never makes
    this process take gigabytes). The password counts as its NFKC form, so it is the same from every keyboard."""
    data = unicodedata.normalize("NFKC", password).encode("utf-8")
    try:
        salt = base64.b64decode(kdf["salt"], validate=True)
        name = kdf["name"]
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise ValueError("the primary password's file is damaged (its key derivation)") from None
    if len(salt) < 16:
        raise ValueError("the primary password's file is damaged (its salt)")
    if name == "scrypt":
        n, r, p = kdf.get("n"), kdf.get("r"), kdf.get("p")
        if not (_whole(n, 2, 2 ** 22) and n & (n - 1) == 0 and _whole(r, 1, 32) and _whole(p, 1, 16)) or \
                128 * n * r > SCRYPT_MEMORY_MAX:
            raise ValueError("the primary password's file is damaged (its scrypt parameters)")
        if not has_scrypt():
            raise ValueError("this Python has no scrypt (hashlib.scrypt), which this store's primary password needs: "
                             "use a Python built with OpenSSL")
        return hashlib.scrypt(data, salt=salt, n=n, r=r, p=p, dklen=KEY, maxmem=128 * n * r * (p + 1) + 2 ** 20)
    if name == "pbkdf2-sha512":
        if not _whole(kdf.get("rounds"), 1, 10 ** 8):
            raise ValueError("the primary password's file is damaged (its rounds)")
        return hashlib.pbkdf2_hmac("sha512", data, salt, kdf["rounds"], KEY)
    raise ValueError(f"the primary password's file names a key derivation this sandbox-ai does not know: {name!r}")


def check_password(password):
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX or \
            any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError(f"a primary password is one line of {PASSWORD_MIN}-{PASSWORD_MAX} characters")
    return password


# --- the store's folder ------------------------------------------------------------------------------------------

def _folder(directory=None):
    return Path(directory) if directory else files_dir()


def make_folder(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


@contextlib.contextmanager
def store_lock(directory):
    """One writer of the store's whole state at a time (setting, changing, removing the password, encrypting)."""
    make_folder(directory)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def replace_wiping(path, data):
    """PATH gets DATA (bytes) atomically, through a new 0600 file of its own that is synced first; what PATH held
    before is then written over with zeros through a descriptor opened before the replace, so its blocks do not
    keep the old text on a file system that writes in place (not on copy-on-write ones and not below an SSD's
    mapping: docs/SECRETS_DESIGN.md)."""
    path = Path(path)
    try:
        old = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        old = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        with contextlib.suppress(OSError):
            folder = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        if old is not None:
            with contextlib.suppress(OSError):
                wipe(old)
    finally:
        if old is not None:
            os.close(old)


def wipe(fd):
    size = os.fstat(fd).st_size
    if size and stat.S_ISREG(os.fstat(fd).st_mode):
        os.pwrite(fd, bytes(size), 0)
        os.fsync(fd)


def read_private(path, limit=READ_LIMIT):
    """The bytes of PATH (yours, 0600, never through a symbolic link), or None when it is not there."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"{path} must be yours and private (chmod 600)")
        return stream.read(limit + 1)


def read_header(directory=None):
    """The checked header of the store (a primary password is set), or None. ValueError: it is there and not
    readable as one - then nothing is decrypted, and nothing is treated as a store without a password."""
    path = _folder(directory) / HEADER
    data = read_private(path)
    if data is None:
        return None
    damaged = ValueError(f"the primary password's file is damaged: {path}")
    try:
        header = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):  # (nested deeper than Python reads: damaged too)
        raise damaged from None
    if not isinstance(header, dict):
        raise damaged
    if _whole(header.get("version"), VERSION + 1, 10 ** 6):
        raise ValueError(f"the primary password's file was written by a newer sandbox-ai (format {header['version']}; "
                         f"this one reads {VERSION}): update sandbox-ai")
    if header.get("version") != VERSION or not isinstance(header.get("id"), str) or \
            not re.fullmatch(r"[0-9a-f]{32}", header["id"]) or not isinstance(header.get("kdf"), dict) or \
            not isinstance(header.get("key"), str):
        raise damaged
    return header


def _header_aad(header):
    bound = {"version": header["version"], "id": header["id"], "kdf": header["kdf"]}
    return b"primary\0" + json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()


def new_header(password, key, ident=None):
    header = {"format": "sandbox-ai secrets", "version": VERSION, "id": ident or os.urandom(16).hex(),
              "kdf": new_kdf(), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    header["key"] = base64.b64encode(seal(derive(password, header["kdf"]), _header_aad(header), key)).decode()
    return header


def write_header(directory, header):
    replace_wiping(directory / HEADER, (json.dumps(header, indent=1) + "\n").encode())


def attempt_wait(failed):
    """Seconds to wait after FAILED wrong passwords in a row before the next attempt."""
    return 0 if failed < FREE_ATTEMPTS else min(2 ** (failed - FREE_ATTEMPTS), ATTEMPT_WAIT_MAX)


def _attempts(directory):
    try:
        data = json.loads((read_private(directory / ".attempts") or b"{}").decode("utf-8"))
        return (data["failed"], float(data["last"])) if _whole(data.get("failed"), 0, 10 ** 9) else (0, 0.0)
    except (ValueError, KeyError, TypeError, RecursionError):
        return 0, 0.0


def _note_attempt(directory, ok):
    path = directory / ".attempts"
    if ok:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    failed = _attempts(directory)[0] + 1
    with contextlib.suppress(OSError):
        replace_wiping(path, json.dumps({"failed": failed, "last": clock()}).encode())


@contextlib.contextmanager
def attempt_lock(directory):
    """One attempt at the password at a time, across processes (a lock file of its own: an unlock may be asked for
    under the store's lock)."""
    fd = os.open(directory / ".attempts.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def unlock_key(password, directory=None):
    """The store's data key, opened with PASSWORD. WrongPassword; Throttled while wrong attempts are being slowed
    down (counted in the store, so every way in - a terminal, the app, the web interface, SSH - shares the count).
    One attempt at a time: the count is read, the password checked and the count written under attempt_lock, so
    guesses sent at once are attempts in a row (each used to find the count as it was before any of them)."""
    directory = _folder(directory)
    if read_header(directory) is None:
        raise ValueError(f"no primary password is set ({SET_PASSWORD})")
    with attempt_lock(directory):
        return _unlock_key(password, directory)


def _unlock_key(password, directory):
    header = read_header(directory)
    if header is None:
        raise ValueError(f"no primary password is set ({SET_PASSWORD})")
    failed, last = _attempts(directory)
    left = attempt_wait(failed) - max(0.0, clock() - last)
    if left > 0:
        raise Throttled(f"too many wrong passwords: wait {int(left) + 1} s before the next attempt")
    if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
        raise WrongPassword("wrong primary password")
    try:
        key = unseal(derive(password, header["kdf"]), _header_aad(header), base64.b64decode(header["key"]))
    except (Damaged, binascii.Error):
        _note_attempt(directory, False)
        raise WrongPassword("wrong primary password") from None
    if len(key) != KEY:
        raise ValueError(f"the primary password's file is damaged: {directory / HEADER}")
    _note_attempt(directory, True)
    return key


class LocalKey:
    """The data key in this process."""

    def __init__(self, key):
        self.key = key

    def seal(self, name, value):
        return MAGIC + base64.b64encode(seal(self.key, b"secret\0" + check_name(name).encode(), value.encode())).decode()

    def open(self, name, text):
        try:
            if not text.startswith(MAGIC):
                raise Damaged("damaged or changed")
            sealed = base64.b64decode(text[len(MAGIC):].strip(), validate=True)
            return unseal(self.key, b"secret\0" + check_name(name).encode(), sealed).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise Damaged("damaged or changed") from None

    def seal_file(self, name, keyref, data):
        if len(data) > FILE_MAX:
            raise ValueError(f"a file to seal is at most {FILE_MAX >> 20} MiB")
        return seal(self.key, file_aad(name, keyref), data)

    def open_file(self, name, keyref, sealed):
        return unseal(self.key, file_aad(name, keyref), sealed)


def check_file_name(name):
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        raise ValueError(f"a sealed file's name is KIND/NAME (vpn/work.ovpn): {name!r}")
    return name


def file_aad(name, keyref):
    """What a sealed file's text is bound to: its name and the reference of the key that sealed it."""
    if not isinstance(keyref, str) or not FILE_KEYREF.fullmatch(keyref):
        raise ValueError(f"a sealed file's key is primary:ID or system:NAME: {keyref!r}")
    return b"file\0" + check_file_name(name).encode() + b"\0" + keyref.encode()


class AgentKey:
    """The data key in the agent (secrets unlock): it seals and opens, the key stays there."""

    def __init__(self, ident):
        self.ident = ident

    def ask(self, request):
        answer = agent_call(self.ident, request)
        if answer is None:
            raise Locked(locked_text())
        if not answer.get("ok"):
            if answer.get("error") == "not understood" and request.get("op", "").endswith("_file"):
                raise ValueError("the secrets agent that runs is from an older sandbox-ai and seals no files: "
                                 f"sandbox-ai secrets lock, then {UNLOCK}")
            raise Damaged("damaged or changed")
        return answer

    def seal(self, name, value):
        return self.ask({"op": "seal", "name": check_name(name), "value": value})["data"]

    def open(self, name, text):
        return self.ask({"op": "open", "name": check_name(name), "data": text.strip()})["value"]

    def seal_file(self, name, keyref, data):
        file_aad(name, keyref)
        if len(data) > FILE_MAX:
            raise ValueError(f"a file to seal is at most {FILE_MAX >> 20} MiB")
        answer = self.ask({"op": "seal_file", "name": name, "keyref": keyref, "data": base64.b64encode(data).decode()})
        return base64.b64decode(answer["data"])

    def open_file(self, name, keyref, sealed):
        file_aad(name, keyref)
        answer = self.ask({"op": "open_file", "name": name, "keyref": keyref,
                           "data": base64.b64encode(sealed).decode()})
        return base64.b64decode(answer["value"])


def locked_text():
    return f"the secrets are locked (encrypted with the primary password): {UNLOCK}"


def _shared():
    """What every copy of this module in one process shares (the launcher, llm.py, vpn.py ... load their own): the
    keys opened here, and that the plain-files warning was said."""
    module = sys.modules.get("sandbox_ai_secret_keys")
    if module is None:
        module = types.ModuleType("sandbox_ai_secret_keys")
        module.keys, module.warned = {}, False
        module = sys.modules.setdefault("sandbox_ai_secret_keys", module)
    return module


def forget_keys():
    shared = _shared()
    shared.keys.clear()
    shared.warned = shared.warned_files = shared.warned_tokens = False


def remember_key(directory, key):
    header = read_header(directory)
    holder = LocalKey(key)
    _shared().keys[header["id"]] = holder
    return holder


def terminal():
    """A terminal to ask on."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def ask_hidden(prompt):
    import getpass  # typed at the terminal itself: not shown, not in the scrollback
    try:
        return getpass.getpass(prompt)
    except EOFError:
        return ""


def say(text):
    print(f"sandbox-ai: {text}", file=sys.stderr)


def ask_new_password():
    """A new primary password, typed twice at the terminal."""
    for _ in range(3):
        first = ask_hidden(f"New primary password ({PASSWORD_MIN} characters or more, not shown): ")
        if not first:
            raise ValueError("no primary password was set")
        try:
            check_password(first)
        except ValueError as exc:
            say(str(exc))
            continue
        if ask_hidden("New primary password again: ") == first:
            return first
        say("the passwords do not match")
    raise ValueError("no primary password was set")


def ask_unlock(directory):
    """The data key, the password asked at the terminal (three attempts; Enter alone: not now)."""
    for _ in range(3):
        password = ask_hidden("Primary password (sandbox-ai secrets, not shown): ")
        if not password:
            break
        try:
            return unlock_key(password, directory)
        except WrongPassword as exc:
            say(str(exc))
    raise Locked(locked_text())


def holder_of(directory, header, ask=True):
    """What seals and opens for the store: the key opened in this process, else the agent, else - on a terminal -
    the password is asked, and the agent keeps the key from then on (once per session). Locked otherwise."""
    ident = header["id"]
    cached = _shared().keys.get(ident)
    if cached is not None:
        return cached
    status = agent_call(ident, {"op": "status"})
    if status and status.get("ok") and status.get("id") == ident:
        return AgentKey(ident)
    if ask and terminal():
        key = ask_unlock(directory)
        holder = _shared().keys[ident] = LocalKey(key)
        with contextlib.suppress(ValueError, OSError, subprocess.SubprocessError):
            if start_agent(key, ident):
                say(f"secrets unlocked for {duration_text(DEFAULT_LIFETIME)} (sandbox-ai secrets lock ends it)")
        return holder
    raise Locked(locked_text())


def is_sealed(data):
    return data.startswith(MAGIC.encode())


# --- sealed files and moved tokens (docs/SECRETS_DESIGN.md, "Sealed files") ----------------------------------------

def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"


def is_sealed_file(data):
    return data.startswith(FILE_MAGIC)


def sealed_file_bytes(holder, keyref, name, data):
    return FILE_MAGIC + keyref.encode() + b"\n" + base64.b64encode(holder.seal_file(name, keyref, data)) + b"\n"


def parse_sealed_file(data, path):
    """(keyref, sealed bytes) of a sealed file's DATA; ValueError: not one this sandbox-ai reads."""
    first, _, rest = data.partition(b"\n")
    try:
        keyref = first[len(FILE_MAGIC):].decode("ascii")
        if not FILE_KEYREF.fullmatch(keyref):
            raise ValueError(keyref)
        return keyref, base64.b64decode(rest.strip(), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError(f"{path} is damaged or changed (not a sealed file of this sandbox-ai)") from None


def file_key(value, name=FILES_KEY):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{128}", value):
        raise ValueError(f"the secret {name} is not the key of sealed files (128 hex digits): it was changed")
    return bytes.fromhex(value)


def system_file_key(backend):
    """The key of sealed files kept in the keyring or the Keychain BACKEND (FILES_KEY): made once, at random."""
    value = backend.get(FILES_KEY)
    if value is None:
        with store_lock(files_dir()):  # (two first uses at once make one key)
            value = backend.get(FILES_KEY)
            if value is None:
                backend.set(FILES_KEY, os.urandom(KEY).hex())
                value = backend.get(FILES_KEY)
                if value is None:
                    raise ValueError(f"the {backend.name} did not keep the key of sealed files ({FILES_KEY})")
    return LocalKey(file_key(value))


def file_sealer(ask=True, plain=False):
    """(holder, keyref) that seals a new file here, or None: plain, as a new secret would be. The keyring or the
    Keychain: a key kept there. The private files with a primary password: its data key (locked: asked on a
    terminal when ASK, else Locked). Without one: ASK - set one first on a terminal, None when PLAIN or
    SANDBOX_AI_SECRETS_PLAIN=1 chose plain files, else ValueError with the command; not ASK - None."""
    backend = store()
    if backend.name != "file":
        return system_file_key(backend), f"system:{FILES_KEY}"
    header = read_header(backend.directory)
    if header is None:
        if not ask:
            return None
        holder = backend.ready(plain)
        if holder is None:
            return None
        header = read_header(backend.directory)
    else:
        holder = holder_of(backend.directory, header, ask)
    return holder, f"primary:{header['id']}"


def file_holder(keyref, path, ask=True):
    """What opens a file sealed with KEYREF."""
    kind, _, ref = keyref.partition(":")
    if kind == "primary":
        directory = files_dir()
        header = read_header(directory)
        if header is None or header["id"] != ref:
            raise ValueError(f"{path} is encrypted with the primary password of another secrets store (its {HEADER} "
                             f"in {directory} is {'missing' if header is None else 'another one'})")
        return holder_of(directory, header, ask)
    value = get(ref)
    if value is None:
        raise ValueError(f"{path} is encrypted with a key kept as the secret {ref} in the system's keyring or "
                         "Keychain, which is not there: the keyring does not answer here (a login over SSH without "
                         "the desktop's keyring?), or the secret was deleted")
    return LocalKey(file_key(value, ref))


def read_file(path, name, ask=True):
    """(bytes, sealed) of the file PATH kept as NAME (vpn/work.ovpn): opened when sealed - in this process's memory
    only, never written anywhere; a plain file as it is (seal_on_use seals it). ValueError: not there, not private,
    damaged or moved to another name; Locked (a ValueError too) with the command that unlocks."""
    check_file_name(name)
    data = read_private(path, WIRE_LIMIT)
    if data is None:
        raise ValueError(f"cannot read {path}: it is not there")
    if not is_sealed_file(data):
        if len(data) > FILE_MAX:
            raise ValueError(f"{path} is larger than {FILE_MAX} bytes")
        return data, False
    keyref, sealed = parse_sealed_file(data, path)
    holder = file_holder(keyref, path, ask)
    try:
        return holder.open_file(name, keyref, sealed), True
    except Damaged:
        raise ValueError(f"{path} is damaged or changed (it does not fit its key and its name {name})") from None


def write_file(path, name, data, plain=False):
    """PATH (mode 0600, atomically, what it held before written over) gets DATA (bytes) sealed - True - or plain -
    False: a store without a primary password where PLAIN or SANDBOX_AI_SECRETS_PLAIN=1 chose that. As a new secret:
    without a password a terminal sets one first, else ValueError names the command; locked: asked or Locked."""
    check_file_name(name)
    if len(data) > FILE_MAX:
        raise ValueError(f"{name} is larger than {FILE_MAX} bytes (1 MiB)")
    sealer = file_sealer(ask=True, plain=plain)
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    replace_wiping(path, data if sealer is None else sealed_file_bytes(*sealer, name, data))
    return sealer is not None


def wipe_file(path):
    """PATH written over with zeros, then removed (never through a symbolic link). Whether it was there."""
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            wipe(fd)
        finally:
            os.close(fd)
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def seal_in_place(path, name, sealer):
    """A plain file sealed where it is (the plain text written over). Whether it was plain."""
    data = read_private(path, FILE_MAX)
    if data is None or is_sealed_file(data) or len(data) > FILE_MAX:
        return False
    replace_wiping(path, sealed_file_bytes(*sealer, name, data))
    return True


def sealable_files():
    """[(path, name)] of the files kept sealed: the OpenVPN profiles, ~/.config/sandbox-ai/vpn/NAME.ovpn."""
    try:
        entries = sorted((config_home() / "vpn").iterdir())
    except OSError:
        return []
    return [(path, f"vpn/{path.name}") for path in entries if path.suffix == ".ovpn" and
            FILE_NAME.fullmatch(f"vpn/{path.name}") and path.is_file() and not path.is_symlink()]


def warn_plain_file(path):
    """Once per process, when a key was read from a plain file that could not be sealed now: said, never refused."""
    shared = _shared()
    if getattr(shared, "warned_files", False) or os.environ.get(PLAIN_CHOICE) == "1":
        return
    shared.warned_files = True
    try:
        password = read_header() is not None
    except (ValueError, OSError):
        password = False
    say(f"warning: {path} is a plain file, not encrypted: whoever reads your files or a backup of them has the key "
        "in it. " + (f"The next unlock encrypts it: {UNLOCK}" if password else f"Set a primary password: {SET_PASSWORD}"))


def seal_on_use(path, name):
    """A plain file NAME that was just read: sealed now when that needs nobody to type anything (the keyring, or the
    files unlocked); else it stays as it is, with the warning. Whether it was sealed."""
    try:
        sealer = file_sealer(ask=False)
        if sealer is not None:
            with store_lock(files_dir()):
                if seal_in_place(path, name, sealer):
                    say(f"{path}: encrypted now (it was a plain file)")
            return True
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    warn_plain_file(path)
    return False


def token_folder():
    """Where the wizard kept typed push tokens before 0.1.98 (one file per server, mode 0600)."""
    return config_home() / "tokens"


def token_secret_name(label):
    """The secret of a push token for LABEL (github.com, a server's URL, a token file's name): GIT_TOKEN_GITHUB_COM."""
    text = re.sub(r"^https?(?:://|-)", "", str(label))
    name = "GIT_TOKEN_" + (re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or "SERVER")
    if len(name) > 64:
        name = f"{name[:55]}_{hashlib.sha256(text.encode()).hexdigest()[:8].upper()}"
    return name


def token_reference(data):
    """The name of the secret a moved token file's DATA names, or None (a token, or anything else)."""
    if isinstance(data, str):
        data = data.encode()
    if not data or not data.startswith(TOKEN_REF.encode()):
        return None
    name = data.split(b"\n", 1)[0][len(TOKEN_REF):].decode("ascii", "replace").strip()
    return name if NAME.fullmatch(name) else None


def reference_bytes(name, said=False):
    return (f"{TOKEN_REF}{name}\n# The token that was here is kept in sandbox-ai secrets as {name}; what names this "
            f"file keeps working. Name the secret instead: --git-token-secret URL={name}\n"
            + ("said\n" if said else "")).encode()


def moved_token_name(path):
    """The secret a moved token file PATH names, or None (not one, not there, not readable)."""
    try:
        return token_reference(read_private(path, 4096) or b"")
    except (ValueError, OSError):
        return None


def token_files():
    try:
        entries = sorted(token_folder().iterdir())
    except OSError:
        return []
    return [path for path in entries if not path.name.startswith(".") and path.is_file() and not path.is_symlink()]


def token_value(data):
    """The token of a plain token file's bytes (one line of printable characters), or None."""
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return text if text and len(text) <= LIMIT and all(32 < ord(c) < 127 for c in text) else None


def file_store_access(directory, holder):
    """(lookup, keep) of the private files' secrets with HOLDER (the key, open in this process or the agent)."""
    def lookup(name):
        data = read_private(directory / name)
        if data is None:
            return None
        if not is_sealed(data):
            return data.decode("utf-8", "replace").rstrip("\n")
        return holder.open(name, data.decode("ascii", "replace"))

    def keep(name, value):
        replace_wiping(directory / name, (holder.seal(name, value) + "\n").encode())
    return lookup, keep


def token_access(ask=False):
    """(lookup, keep) of where a push token goes: the keyring or the Keychain, else the private files with a primary
    password (unlocked; ASK: asked on a terminal, else Locked); None: they have no password."""
    backend = store()
    if backend.name != "file":
        return backend.get, backend.set
    header = read_header(backend.directory)
    if header is None:
        return None
    return file_store_access(backend.directory, holder_of(backend.directory, header, ask))


def move_token(path, lookup, keep, said=False):
    """The wizard's token file PATH moved into the store: its token kept as a secret (a name of its own when that
    name holds another token), the file replaced by a reference to it, what it held written over. The secret's name,
    or None (not a plain token file)."""
    data = read_private(path, 4096)
    if data is None or token_reference(data) is not None:
        return None
    value = token_value(data)
    if value is None:
        return None
    base = token_secret_name(path.name)
    for name in [base] + [f"{base[:62]}_{number}" for number in range(2, 10)]:
        existing = lookup(name)
        if existing == value:
            break
        if existing is None:
            keep(name, value)
            break
    else:
        return None
    replace_wiping(path, reference_bytes(name, said))
    return name


def moved_note(option, path, name):
    say(f"note: {option} {path}: the token is kept as the secret {name} now (sandbox-ai secrets) and this keeps "
        f"working; the project file can name the secret itself: --git-token-secret URL={name} (sandbox-ai wizard "
        "edit does it)")


def move_token_on_use(path, option="--git-token-file"):
    """A token file of the wizard's (in its folder) that was just read plain: moved into the store when that needs
    nobody to type anything (the keyring, or the files unlocked), and said once. The secret's name, or None."""
    path = Path(path)
    try:
        if path.resolve().parent != token_folder().resolve():
            return None
        access = token_access(ask=False)
        if access is None:
            return None
        with store_lock(files_dir()):
            name = move_token(path, *access, said=True)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    if name:
        moved_note(option, path, name)
    return name


def read_moved_token(path, data, option="--git-token-file"):
    """The token a moved token file PATH (its DATA) names: read from the store (Locked says how to unlock); the first
    time said once, with what the project file can name instead."""
    name = token_reference(data)
    value = get(name)
    if value is None:
        raise ValueError(f"{option}: {path} names the secret {name} (its token was moved there), which is not kept: "
                         f"sandbox-ai secrets set {name}")
    if b"\nsaid\n" not in data:
        moved_note(option, path, name)
        with contextlib.suppress(OSError):
            replace_wiping(path, reference_bytes(name, said=True))
    return value


def warn_plain_token(path, option="--git-token-file"):
    """Once per process, for a token read from a plain file of the user's own: said, never refused."""
    shared = _shared()
    if getattr(shared, "warned_tokens", False) or os.environ.get(PLAIN_CHOICE) == "1":
        return
    shared.warned_tokens = True
    try:
        wizards = Path(path).resolve().parent == token_folder().resolve()
        password = read_header() is not None
    except (ValueError, OSError):
        wizards = password = False
    if wizards:  # (the wizard's own: it moves into the store by itself)
        fix = (f"It moves into sandbox-ai secrets at the next unlock: {UNLOCK}" if password else
               f"It moves into sandbox-ai secrets once a primary password is set: {SET_PASSWORD}")
    else:
        fix = (f"Keep it as a secret instead: sandbox-ai secrets set NAME < {path}, name it with --git-token-secret "
               "URL=NAME, and delete the file")
    say(f"warning: {option} {path} is a plain file, not encrypted: whoever reads your files or a backup of them has "
        f"the token. {fix}")


def plain_files():
    """The paths of OpenVPN profiles and wizard token files that are still plain."""
    found = []
    for path, _ in sealable_files():
        with contextlib.suppress(ValueError, OSError):
            data = read_private(path, len(FILE_MAGIC))
            if data is not None and not is_sealed_file(data):
                found.append(str(path))
    for path in token_files():
        with contextlib.suppress(ValueError, OSError):
            data = read_private(path, len(TOKEN_REF))
            if data and not data.startswith(TOKEN_REF.encode()) and token_value(read_private(path, 4096)):
                found.append(str(path))
    return found


def move_plain_files(directory, header, holder):
    """The OpenVPN profiles sealed and the wizard's token files moved into the private files (with a primary
    password, HOLDER its key): what was done, in words. Only for the store whose secrets these are (the files')."""
    try:
        if Path(directory).resolve() != files_dir().resolve() or store().name != "file":
            return []
    except OSError:
        return []
    done, sealer = [], (holder, f"primary:{header['id']}")
    for path, name in sealable_files():
        try:
            if seal_in_place(path, name, sealer):
                done.append(f"{name} (encrypted)")
        except (ValueError, OSError) as exc:
            say(f"{path} was not encrypted: {exc}")
    lookup, keep = file_store_access(directory, holder)
    for path in token_files():
        try:
            name = move_token(path, lookup, keep)
            if name:
                done.append(f"tokens/{path.name} (now the secret {name})")
        except (ValueError, OSError) as exc:
            say(f"{path} was not moved: {exc}")
    return done


def encrypt_plain_files(directory=None, holder=None, moved=None):
    """Every plain file of a store with a password, encrypted in place (after `password set`, and what an
    interrupted one, an older sandbox-ai or a copy left behind: at every unlock). The names. MOVED (a list) gets
    what was done to the OpenVPN profiles and the wizard's token files beside the store (move_plain_files)."""
    directory = _folder(directory)
    done = []
    with store_lock(directory):
        header = read_header(directory)
        if header is None:
            return done
        holder = holder or holder_of(directory, header)
        for name in FileStore(directory).names():
            data = read_private(directory / name)
            if data is not None and not is_sealed(data):
                value = data.decode("utf-8", "replace").rstrip("\n")
                replace_wiping(directory / name, (holder.seal(name, value) + "\n").encode())
                done.append(name)
        files = move_plain_files(directory, header, holder)
    if moved is not None:
        moved += files
    return done


def set_password(password, directory=None, moved=None):
    """A primary password for the store: the header first (a new random data key, sealed with the password's
    key), then every plain file encrypted (MOVED: encrypt_plain_files). The data key."""
    directory = _folder(directory)
    check_password(password)
    with store_lock(directory):
        if read_header(directory) is not None:
            raise ValueError("a primary password is already set (change it: sandbox-ai secrets password change)")
        key = os.urandom(KEY)
        write_header(directory, new_header(password, key))
        _note_attempt(directory, True)
    encrypt_plain_files(directory, LocalKey(key), moved)
    return key


def change_password(old, new, directory=None):
    """The data key sealed anew with NEW's key (a new salt): no secret is rewritten, an agent stays unlocked."""
    directory = _folder(directory)
    if read_header(directory) is None:
        raise ValueError(f"no primary password is set ({SET_PASSWORD})")
    key = unlock_key(old, directory)
    check_password(new)
    with store_lock(directory):
        header = read_header(directory)
        write_header(directory, new_header(new, key, ident=header["id"]))
    return key


def remove_password(password, directory=None, moved=None):
    """Plain files again: every secret decrypted (all of them first, so a damaged one stops it before anything
    changed), then the header written over and removed, the agent ended. How many secrets were decrypted; MOVED (a
    list) gets the sealed files made plain again (the OpenVPN profiles sealed with this store's key)."""
    directory = _folder(directory)
    header = read_header(directory)
    if header is None:
        raise ValueError(f"no primary password is set ({SET_PASSWORD})")
    holder = LocalKey(unlock_key(password, directory))
    with store_lock(directory):
        values = {}
        for name in FileStore(directory).names():
            data = read_private(directory / name)
            if data is not None and is_sealed(data):
                try:
                    values[name] = holder.open(name, data.decode("ascii", "replace"))
                except Damaged:
                    raise ValueError(f"the secret {name} is damaged or changed (delete it first: sandbox-ai secrets "
                                     f"delete {name}); nothing was changed") from None
        files = {}
        if directory.resolve() == files_dir().resolve():  # (sealed with this store's key: plain again too)
            for path, name in sealable_files():
                data = read_private(path, WIRE_LIMIT)
                if data is not None and is_sealed_file(data):
                    keyref, sealed = parse_sealed_file(data, path)
                    if keyref == f"primary:{header['id']}":
                        try:
                            files[path] = holder.open_file(name, keyref, sealed)
                        except Damaged:
                            raise ValueError(f"{path} is damaged or changed (remove it first); nothing was "
                                             "changed") from None
        for name, value in values.items():
            replace_wiping(directory / name, value.encode())
        for path, data in files.items():
            replace_wiping(path, data)
        with contextlib.suppress(OSError):
            fd = os.open(directory / HEADER, os.O_RDWR | os.O_NOFOLLOW)
            try:
                wipe(fd)
            finally:
                os.close(fd)
        (directory / HEADER).unlink()
        _note_attempt(directory, True)
    agent_call(header["id"], {"op": "lock"})
    _shared().keys.pop(header["id"], None)
    if moved is not None:
        moved += [f"{path.parent.name}/{path.name}" for path in files]
    return len(values)


def warn_plain(name, directory=None):
    """Once per process, when a secret was read from a plain file: said, never refused."""
    shared = _shared()
    if shared.warned or os.environ.get(PLAIN_CHOICE) == "1":
        return
    shared.warned = True
    try:
        left_over = read_header(directory) is not None
    except (ValueError, OSError):
        left_over = False
    say(f"warning: the secret {name} is kept in a plain file, not encrypted: whoever reads your files or a backup "
        "of them has it. " + (f"The next unlock encrypts it: {UNLOCK}" if left_over else
                              f"Set a primary password: {SET_PASSWORD}"))


def protection(directory=None, backend=None):
    """What protects the secrets here: {"backend", "protection": keyring | keychain | primary-password | none,
    "password": one is set for the files, "locked" (None without one), "left": seconds the agent still holds the
    key, "plain": the names kept in plain files, "encrypted": how many are not, "kdf", "plain_choice": plain files
    are this environment's choice (SANDBOX_AI_SECRETS_PLAIN=1: a new secret is kept without asking for one)}."""
    directory = _folder(directory)
    backend = backend or store()
    header = read_header(directory)
    plain, encrypted = [], 0
    for name in FileStore(directory).names():
        try:
            data = read_private(directory / name, limit=len(MAGIC))
        except ValueError:
            data = None
        if data is not None and is_sealed(data):
            encrypted += 1
        elif data is not None:
            plain.append(name)
    locked = left = None
    if header is not None:
        status = agent_call(header["id"], {"op": "status"})
        if status and status.get("ok") and status.get("id") == header["id"]:
            locked, left = False, status.get("left")
        else:
            locked = header["id"] not in _shared().keys
    kind = backend.name if backend.name != "file" else "primary-password" if header is not None else "none"
    return {"backend": backend.name, "protection": kind, "password": header is not None, "locked": locked,
            "left": left, "plain": plain, "encrypted": encrypted, "kdf": kdf_text(header["kdf"]) if header else None,
            "plain_choice": os.environ.get(PLAIN_CHOICE) == "1", "plain_files": plain_files()}


def plain_files_text(info):
    """The OpenVPN profiles and token files still plain, and what moves them (secrets status, the doctor); or ""."""
    found = info.get("plain_files") or []
    if not found:
        return ""
    if info["backend"] != "file":
        how = "encrypted when next used"
    elif info["password"]:
        how = f"encrypted at the next unlock: {UNLOCK}" if info["locked"] else "encrypted when next used"
    else:
        how = f"encrypted once a primary password is set: {SET_PASSWORD}"
    return f"{_count(len(found), 'plain file')} with a key or token beside the store ({how}): {', '.join(found)}"


def _count(number, word):
    return f"{number} {word}{'' if number == 1 else 's'}"


def files_text(info):
    """The private files' protection in words (a part of `secrets backend`)."""
    plain = len(info["plain"])
    if not info["password"]:
        if not plain:
            return "no secrets yet; the first one asks for a primary password"
        return f"not encrypted: {_count(plain, 'secret')} in plain files; {SET_PASSWORD}"
    if info["locked"]:
        text = f"encrypted with the primary password; locked: {UNLOCK}"
    else:
        left = f" for {duration_text(info['left'])}" if info["left"] else ""
        text = f"encrypted with the primary password; unlocked{left}: sandbox-ai secrets lock"
    return text + (f"; {_count(plain, 'plain file')} left, encrypted at the next unlock" if plain else "")


def protection_text(info):
    """One line: where the secrets are kept and what protects them (`secrets backend`, the doctor)."""
    if info["backend"] == "file":
        return f"file ({files_text(info)})"
    system = "the system's keyring" if info["backend"] == "keyring" else "the macOS Keychain"
    text = f"encrypted by {system}"
    if info["plain"] or info["encrypted"]:
        text += f"; {_count(len(info['plain']) + info['encrypted'], 'more secret')} in private files, {files_text(info)}"
    return f"{info['backend']} ({text})"


def duration(text):
    """Seconds of 90s, 30m, 8h, 2d (a bare number: minutes); up to 30 days."""
    match = re.fullmatch(r"([1-9][0-9]{0,6})([smhd]?)", text or "")
    seconds = int(match[1]) * {"s": 1, "m": 60, "": 60, "h": 3600, "d": 86400}[match[2]] if match else 0
    if not 0 < seconds <= LIFETIME_MAX:
        raise ValueError(f"a duration is a number with s, m, h or d (30m, 8h, 2d; at most 30d): {text!r}")
    return seconds


def duration_text(seconds):
    seconds = max(1, int(seconds))
    units = (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))
    for index, (word, size) in enumerate(units):
        if seconds >= size:
            text = _count(seconds // size, word)
            rest = seconds % size
            if rest and index + 1 < len(units) and rest // units[index + 1][1]:
                text += " " + _count(rest // units[index + 1][1], units[index + 1][0])
            return text
    return "1 second"


# --- the agent: the data key in memory, behind a socket of this user's ------------------------------------------

def private_folder(folder, forbidden, make=True):
    """FOLDER is a folder of this user's (no symbolic link) without the mode bits FORBIDDEN; made 0700 first when
    MAKE and it is not there."""
    if make:
        with contextlib.suppress(FileExistsError):
            os.mkdir(folder, 0o700)
    info = os.lstat(folder)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & forbidden


def temp_root(base=None, make=True):
    """BASE/sandbox-ai-UID (BASE: $TMPDIR, else /tmp) when it is this user's folder (made 0700 when MAKE; not a link,
    nobody else's, no bit for others) - else, when another user made it first or it is a link, the user's own
    $XDG_CACHE_HOME/sandbox-ai/run (~/.cache: nobody else can make that; made 0700 when MAKE): never a folder another
    user owns. (hosts.temp_root, secret_store.temp_root and remotefs.temp_root are the same.)"""
    temporary = Path(base or os.environ.get("TMPDIR") or "/tmp") / f"sandbox-ai-{os.getuid()}"
    try:
        if make:
            with contextlib.suppress(FileExistsError):
                os.mkdir(temporary, 0o700)
        info = os.lstat(temporary)
    except FileNotFoundError:
        return temporary  # (not made yet: a question makes nothing)
    except OSError:
        info = None
    if info is not None and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
        return temporary
    cache = os.environ.get("XDG_CACHE_HOME") or ""
    fallback = Path(cache if os.path.isabs(cache) else Path.home() / ".cache") / "sandbox-ai" / "run"
    if make:
        with contextlib.suppress(OSError):
            fallback.mkdir(mode=0o700, parents=True, exist_ok=True)
    return fallback


def agent_folders(platform=None, make=True):
    """The candidates [[(folder, mode bits it must not have), ...]] for the agent's socket folder, as hosts.py
    keeps its SSH connections: XDG_RUNTIME_DIR/sandbox-ai/secrets, else (macOS) ~/Library/Caches/sandbox-ai/secrets,
    and $TMPDIR/sandbox-ai-UID/secrets (also when the first path is too long for a socket)."""
    platform = platform or sys.platform
    found = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        found.append((Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai", 0o002))
    elif platform == "darwin":
        found.append((Path.home() / "Library" / "Caches" / "sandbox-ai", 0o022))
    found.append((temp_root(make=make), 0o077))  # (another user's sandbox-ai-UID is never used: temp_root)
    if Path(os.environ.get("TMPDIR") or "/tmp") != Path("/tmp"):  # (a TMPDIR too long for a socket: macOS's always is)
        found.append((temp_root("/tmp", make=make), 0o077))
    return [[(top, forbidden), (top / "secrets", 0o077)] for top, forbidden in found]


def agent_socket(ident, make=False):
    """The socket of the store IDENT's agent, in a folder that is this user's alone, or None. MAKE (the agent's
    start): the folder is made if need be; a question to the agent makes nothing."""
    for folders in agent_folders(make=make):
        path = folders[-1][0] / f"agent-{ident[:16]}.sock"
        if len(os.fsencode(path)) > SOCKET_LIMIT:
            continue
        try:
            if make and sys.platform == "darwin":
                folders[0][0].parent.mkdir(parents=True, exist_ok=True)
            if all(private_folder(folder, forbidden, make) for folder, forbidden in folders):
                return path
        except OSError:
            pass
        return None  # (never a second place when the first is there but not private: one agent, one socket)
    return None


def xucred_uid(data):
    """The user of macOS's struct xucred (LOCAL_PEERCRED): its version, then the uid."""
    if len(data) < 8:
        raise OSError("no peer credentials")
    version, uid = struct.unpack_from("=II", data)
    if version != 0:
        raise OSError("peer credentials of an unknown form")
    return uid


def peer_uid(connection, platform=None):
    """The user at the other end of a Unix socket, from the kernel (SO_PEERCRED on Linux, LOCAL_PEERCRED on macOS
    and the BSDs). OSError where it cannot be asked: such a peer is refused."""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        data = connection.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), struct.calcsize("3i"))
        return struct.unpack("3i", data)[1]
    if platform == "darwin" or "bsd" in platform:
        return xucred_uid(connection.getsockopt(0, 1, 76))  # SOL_LOCAL, LOCAL_PEERCRED
    raise OSError("no peer credentials on this system")


def agent_call(ident, request, timeout=5.0):
    """The agent's answer to REQUEST, or None: no agent, or what answers there is not this user's (the socket's
    folder, the socket, the process behind it)."""
    try:
        path = agent_socket(ident)
        if path is None:
            return None
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            if peer_uid(client) != os.getuid():
                return None
            client.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n") and len(data) < WIRE_LIMIT:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        answer = json.loads(data.decode("utf-8"))
        return answer if isinstance(answer, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


class Agent:
    """Holds one store's data key until its time is over, it was idle too long or `lock`; seals and opens for
    connections of its own user, and never gives the key out."""

    def __init__(self, key, ident, path, lifetime, idle=None):
        self.holder, self.ident, self.path = LocalKey(key), ident, Path(path)
        self.lifetime, self.idle = lifetime, idle
        self.started, self.started_wall = time.monotonic(), time.time()  # (the wall clock: a suspended laptop)
        self.used = self.started
        self.server, self.inode, self.done = None, None, False

    def left(self):
        now = time.monotonic()
        left = min(self.lifetime - (now - self.started), self.lifetime - (time.time() - self.started_wall))
        if self.idle:
            left = min(left, self.idle - (now - self.used))
        return left

    def bind(self):
        self.close(remove=False)
        if not all(private_folder(folder, 0o077 if folder == self.path.parent else 0o002)
                   for folder in (self.path.parent.parent, self.path.parent)):
            raise OSError(f"{self.path.parent} is not a private folder")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            server.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        server.listen(16)
        info = os.lstat(self.path)
        self.server, self.inode = server, (info.st_dev, info.st_ino)

    def listening(self):
        """Still behind its socket; bound again when the folder went away with a login session and is back."""
        try:
            info = os.lstat(self.path)
            if self.server is not None and (info.st_dev, info.st_ino) == self.inode:
                return True
            if self.server is not None and stat.S_ISSOCK(info.st_mode):
                self.done = True  # another agent took the place (a new unlock)
                return False
        except OSError:
            pass
        try:
            self.bind()
            return True
        except OSError:
            self.server = None
            return False

    def close(self, remove=True):
        if self.server is not None:
            if remove:
                with contextlib.suppress(OSError):
                    info = os.lstat(self.path)
                    if (info.st_dev, info.st_ino) == self.inode:
                        os.unlink(self.path)
            self.server.close()
            self.server = None

    def answer(self, request):
        try:
            op = request.get("op") if isinstance(request, dict) else None
            if op == "status":
                return {"ok": True, "id": self.ident, "left": max(0, int(self.left())), "pid": os.getpid()}
            if op == "lock":
                self.done = True
                return {"ok": True}
            if op == "open" and isinstance(request.get("data"), str):
                self.used = time.monotonic()
                return {"ok": True, "value": self.holder.open(request.get("name"), request["data"])}
            if op == "seal" and isinstance(request.get("value"), str) and len(request["value"]) <= LIMIT:
                self.used = time.monotonic()
                return {"ok": True, "data": self.holder.seal(request.get("name"), request["value"])}
            if op in ("seal_file", "open_file") and isinstance(request.get("data"), str) and \
                    request.get("keyref") == f"primary:{self.ident}":  # (this store's key, for nothing else)
                self.used = time.monotonic()
                data = base64.b64decode(request["data"], validate=True)
                if op == "seal_file":
                    sealed = self.holder.seal_file(request.get("name"), request["keyref"], data)
                    return {"ok": True, "data": base64.b64encode(sealed).decode()}
                opened = self.holder.open_file(request.get("name"), request["keyref"], data)
                return {"ok": True, "value": base64.b64encode(opened).decode()}
        except ValueError:
            return {"ok": False, "error": "damaged or changed"}
        return {"ok": False, "error": "not understood"}

    def serve(self, connection):
        with connection:
            try:
                if peer_uid(connection) != os.getuid():
                    return
                connection.settimeout(2.0)
                data = b""
                while not data.endswith(b"\n") and len(data) < WIRE_LIMIT:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                try:
                    request = json.loads(data.decode("utf-8"))
                except (ValueError, RecursionError):  # (a request nested too deep never ends the agent)
                    request = None
                connection.sendall(json.dumps(self.answer(request)).encode() + b"\n")
            except OSError:
                return

    def step(self, timeout=1.0):
        if not self.listening():
            time.sleep(min(timeout, 0.5))
            return
        ready, _, _ = select.select([self.server], [], [], timeout)
        if ready:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.serve(connection)

    def run(self):
        try:
            while not self.done and self.left() > 0:
                self.step(timeout=max(0.05, min(1.0, self.left())))
        finally:
            self.close()


def harden():
    """No core file and (Linux) no ptrace or /proc/PID/mem by another process of this user: the key stays in this
    process's memory as far as the system lets a process say so."""
    with contextlib.suppress(Exception):
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux"):
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0


def agent_main(stdin=None):
    """`secret_store.py agent`: one line of JSON on standard input ({"key", "id", "socket", "lifetime", "idle"} -
    the key never on a command line or in an environment), then "ready" on standard output once it listens."""
    harden()
    request = json.loads((stdin or sys.stdin).readline())
    if os.fork():
        os._exit(0)  # (the starter waits for this one only; the agent is nobody's child)
    agent = Agent(bytes.fromhex(request["key"]), request["id"], request["socket"], request["lifetime"],
                  request.get("idle"))
    del request
    agent.bind()

    def end(*_):
        agent.done = True
    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.write(1, b"ready\n")
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    agent.run()
    return 0


def start_agent(key, ident, lifetime=DEFAULT_LIFETIME, idle=None):
    """A new agent for the store IDENT (one running already ends first). True when it listens. The key goes to it
    through a pipe."""
    path = agent_socket(ident, make=True)
    if path is None:
        raise ValueError("no private folder for the agent's socket (XDG_RUNTIME_DIR, or sandbox-ai-UID in TMPDIR, "
                         "must be yours alone)")
    if agent_call(ident, {"op": "lock"}):
        for _ in range(50):
            if not os.path.lexists(path):
                break
            time.sleep(0.05)
    process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "agent"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, cwd="/")
    try:
        process.stdin.write(json.dumps({"key": key.hex(), "id": ident, "socket": str(path), "lifetime": lifetime,
                                        "idle": idle}).encode() + b"\n")
        process.stdin.close()
        ready, _, _ = select.select([process.stdout], [], [], 20)
        return bool(ready) and process.stdout.readline().strip() == b"ready"
    finally:
        process.stdout.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=20)


class FileStore:
    """Private files: one per secret, mode 0600, in a 0700 directory; encrypted once the store has a primary
    password (.primary.json beside them), plain before."""
    name = "file"

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else files_dir()
        self.plain_read = False

    def names(self):
        try:
            return sorted(p.name for p in self.directory.iterdir() if NAME.fullmatch(p.name) and p.is_file()
                          and not p.is_symlink())
        except OSError:
            return []

    def get(self, name, ask=True):
        path = self.directory / check_name(name)
        data = read_private(path)
        self.plain_read = False
        if data is None:
            return None
        if not is_sealed(data):
            self.plain_read = True
            return data[:LIMIT + 1].decode("utf-8", "replace").rstrip("\n")
        header = read_header(self.directory)
        if header is None:
            raise ValueError(f"the secret {name} is encrypted, but the primary password's file ({HEADER}) beside it "
                             f"is missing: {self.directory}")
        try:
            return holder_of(self.directory, header, ask=ask).open(name, data.decode("ascii", "replace"))
        except Damaged:
            raise ValueError(f"the secret {name} is damaged or changed (its file does not fit the primary "
                             f"password's key and its name): {path}") from None

    def ready(self, plain=False):
        """What seals a new secret here, or None (a plain file: PLAIN, your choice). A store without a primary
        password asks to set one on a terminal, and says the command without one."""
        header = read_header(self.directory)
        if header is not None:
            return holder_of(self.directory, header)
        if plain or os.environ.get(PLAIN_CHOICE) == "1":
            return None
        if not terminal():
            # (said for every command that keeps a secret - vpn add, llm add ... too, which have no --plain)
            raise ValueError("no primary password is set: these private files would hold the secret unencrypted. "
                             f"Set one first: {SET_PASSWORD}, then repeat the command; or keep plain files, your "
                             f"choice: sandbox-ai secrets set NAME --plain, or {PLAIN_CHOICE}=1 in front of the "
                             "command")
        say("secrets are kept in private files here (no system keyring): a primary password encrypts them. It is "
            "asked once per session; without it nobody reads them, you neither - there is no way to recover it.")
        key = set_password(ask_new_password(), self.directory)
        holder = remember_key(self.directory, key)
        with contextlib.suppress(ValueError, OSError, subprocess.SubprocessError):
            if start_agent(key, read_header(self.directory)["id"]):
                say(f"primary password set; secrets unlocked for {duration_text(DEFAULT_LIFETIME)} (sandbox-ai "
                    "secrets lock ends it)")
        return holder

    def set(self, name, value, plain=False):
        check_name(name)
        holder = self.ready(plain)
        make_folder(self.directory)
        # A new temporary file of its own (O_EXCL, 0600): two writes of NAME at once never share one, and nothing
        # left behind passes its permissions on.
        text = value if holder is None else holder.seal(name, value) + "\n"
        with store_lock(self.directory):
            # The store as it was when the value was sealed (or kept plain): a password set, changed to a new store
            # or removed meanwhile (another terminal, the app) would leave a file nothing opens, or a plain one in an
            # encrypted store.
            header = read_header(self.directory)
            other = header is not None and holder is not None and getattr(holder, "ident", None) != header["id"] \
                and _shared().keys.get(header["id"]) is not holder
            if (header is None) != (holder is None) or other:
                raise ValueError(f"the primary password was set, changed or removed meanwhile: {name} was not kept; "
                                 "keep it again")
            replace_wiping(self.directory / name, text.encode())
        return holder is not None

    def delete(self, name):
        """The name goes first, then the file's blocks are written over through a descriptor opened before: a delete
        cut off at any step leaves the secret as it was or gone - never a file of zeros, read as its value."""
        path = self.directory / check_name(name)
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            fd = None
        try:
            os.unlink(path)
            if fd is not None:
                with contextlib.suppress(OSError):
                    wipe(fd)
            return True
        except FileNotFoundError:
            return False
        finally:
            if fd is not None:
                os.close(fd)


class KeyringStore:
    """The Secret Service through secret-tool; entries carry service=sandbox-ai and name=NAME."""
    name = "keyring"

    def __init__(self, run=subprocess.run):
        self.run = run

    def tool(self, *args, data=None):
        return self.run(["secret-tool", *args], input=data, capture_output=True, text=True, timeout=30,
                        stdin=None if data is not None else subprocess.DEVNULL)

    def names(self):
        result = self.tool("search", "--all", "service", "sandbox-ai")
        found = set()
        for line in (result.stdout + result.stderr).splitlines():
            match = re.match(r"\s*attribute\.name\s*=\s*(\S+)", line)
            if match and NAME.fullmatch(match[1]):
                found.add(match[1])
        return sorted(found)

    def get(self, name):
        result = self.tool("lookup", "service", "sandbox-ai", "name", check_name(name))
        return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout else None

    def set(self, name, value):
        result = self.tool("store", f"--label=sandbox-ai {check_name(name)}", "service", "sandbox-ai", "name", name,
                           data=value)
        if result.returncode != 0:
            raise ValueError(f"the keyring refused the secret: {result.stderr.strip()[:200]} (keep it in sandbox-ai's "
                             f"private files instead: SANDBOX_AI_SECRETS=file sandbox-ai secrets set {name})")

    def delete(self, name):
        return self.tool("clear", "service", "sandbox-ai", "name", check_name(name)).returncode == 0


class KeychainStore:
    """macOS: the login Keychain through security(1); generic passwords of service sandbox-ai, account NAME. The
    value never goes on a command line (other users' `ps` would show it): `security -i` reads the command that adds
    it from stdin, the value in it as hex (-X)."""
    name = "keychain"
    SERVICE = "sandbox-ai"

    def __init__(self, run=subprocess.run):
        self.run = run

    def tool(self, *args, data=None):
        return self.run(["/usr/bin/security", *args], input=data, capture_output=True, text=True, timeout=30,
                        stdin=None if data is not None else subprocess.DEVNULL)

    def names(self):
        """The accounts of this service, from `security dump-keychain` (attributes only, never the values)."""
        result = self.tool("dump-keychain")
        found = set()
        for item in result.stdout.split("keychain: ")[1:]:
            account = re.search(r'"acct"<blob>="([^"]*)"', item)
            service = re.search(r'"svce"<blob>="([^"]*)"', item)
            if account and service and service[1] == self.SERVICE and NAME.fullmatch(account[1]):
                found.add(account[1])
        return sorted(found)

    def get(self, name):
        result = self.tool("find-generic-password", "-s", self.SERVICE, "-a", check_name(name), "-w")
        return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout else None

    def set(self, name, value):
        command = f"add-generic-password -U -s {self.SERVICE} -a {check_name(name)} -X {value.encode().hex()}\n"
        result = self.tool("-i", data=command)
        if result.returncode != 0 or "error" in result.stderr.lower() or self.get(name) != value:
            raise ValueError(f"the Keychain refused the secret: {result.stderr.strip()[:200]} (keep it in "
                             f"sandbox-ai's private files instead: SANDBOX_AI_SECRETS=file sandbox-ai secrets set "
                             f"{name})")

    def delete(self, name):
        return self.tool("delete-generic-password", "-s", self.SERVICE, "-a", check_name(name)).returncode == 0


def keychain_usable():
    """macOS with its security tool."""
    return sys.platform == "darwin" and os.path.exists("/usr/bin/security")


def keyring_usable(run=subprocess.run):
    """True if secret-tool is there and a Secret Service answers on the session bus."""
    if not shutil.which("secret-tool") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not \
            Path(f"/run/user/{os.getuid()}/bus").exists():
        return False
    try:
        result = run(["secret-tool", "search", "service", "sandbox-ai-probe"], capture_output=True, text=True,
                     timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # A search that finds nothing ends with 1 and says nothing; any message on standard error is the Secret
    # Service failing ("not provided by any .service files", "The name is not activatable", "Cannot autolaunch
    # D-Bus", a locked or missing collection): then the private files are used.
    return result.returncode == 0 or (result.returncode == 1 and not result.stderr.strip())


def store():
    """The keyring (macOS: the Keychain) if it works here, else the private files (SANDBOX_AI_SECRETS=file forces
    the files)."""
    if os.environ.get("SANDBOX_AI_SECRETS") != "file" and keychain_usable():
        return KeychainStore()
    if os.environ.get("SANDBOX_AI_SECRETS") != "file" and keyring_usable():
        return KeyringStore()
    return FileStore()


def get(name, ask=True):
    """A secret's value from the keyring or the files (both are looked at), or None. Locked (a ValueError): it is
    in the encrypted files and nothing holds their key (no agent, no terminal to ask on; ASK false: the password is
    never asked for on the terminal - a thread beside an agent that has it)."""
    check_name(name)
    first = store()
    for backend in ([first] if isinstance(first, FileStore) else [first, FileStore()]):
        value = backend.get(name, ask=False) if not ask and isinstance(backend, FileStore) else backend.get(name)
        if value is not None:
            log = sys.modules.get("sandbox_ai_launchlog")  # (loaded by the launcher: a value read never reaches it)
            if log is not None:
                log.secret(value)
            if getattr(backend, "plain_read", False):
                warn_plain(name, backend.directory)
            return value
    return None


_net = None


def net_module():
    """net.py, which gives a running sandbox a key (`give` and `revoke` are its `net allow/revoke --api-key`: one
    code path). Loaded only for those: the other commands stay standard library alone."""
    global _net
    if _net is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_ai_net_for_secrets", Path(__file__).resolve().parent
                                                      / "net.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _net = loaded
    return _net


def given_main(args, out):
    """give, revoke and list --run: net.py's allow, revoke and given credentials of the run."""
    net = net_module()
    if args.command == "list":
        return net.list_given(args.run, "api_keys", out, as_json=args.json, prog="secrets")
    try:
        check_name(args.name)
        if args.command == "give":
            variable = args.variable or args.name
            check_name(variable)
            argv = ["allow", args.run, "--api-key", f"{args.host}={variable}={args.name}",
                    *(["--keep"] if args.keep else [])]
        else:
            # NAME: the variable a key was given as, or the secret it came from (each key given of that secret)
            record = net.choose(net.running(), args.run, command="secrets revoke")
            given = net.given_credentials(record)["api_keys"]
            names = [e["name"] for e in given if e["name"] == args.name] or \
                [e["name"] for e in given if e.get("secret") == args.name]
            if not names:
                raise ValueError(f"no API key {args.name} was given during this run (sandbox-ai secrets list --run "
                                 f"{args.run})")
            argv = ["revoke", args.run, *[token for name in names for token in ("--api-key", name)]]
    except ValueError as exc:
        print(f"sandbox-ai secrets: {exc}", file=sys.stderr)
        return 1
    return net.main(argv, out=out, brief=True, prog="secrets", command=f"secrets {args.command}")


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    import importlib.util
    from pathlib import Path
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle",
                                                      Path(__file__).resolve().parent / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def read_passwords(args, stdin, count):
    """COUNT passwords for a command without a terminal: the lines of standard input (--password-stdin) or of a
    descriptor (--password-fd N); None when they are to be asked at the terminal. Never an argument, never a
    variable: both show in `ps` and in what a process passes on."""
    if args.password_fd is not None:
        with os.fdopen(args.password_fd, "r", encoding="utf-8") as stream:
            text = stream.read(4 * PASSWORD_MAX * count + 16)
    elif args.password_stdin:
        text = stdin.read(4 * PASSWORD_MAX * count + 16)
    elif stdin is sys.stdin and terminal():
        return None
    else:
        raise ValueError("no terminal to ask for the primary password on: give it on standard input "
                         "(--password-stdin) or on a descriptor (--password-fd N)")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != count:
        raise ValueError("expected one line, the primary password" if count == 1 else
                         "expected two lines: the current primary password, then the new one")
    return [line.rstrip("\r") for line in lines]


def password_options(parser):
    parser.add_argument("--password-stdin", action="store_true",
                        help="read the password from standard input (one line) instead of asking at the terminal")
    parser.add_argument("--password-fd", type=int, metavar="N", help="read it from this open descriptor")


def unlock_options(parser):
    parser.add_argument("--for", dest="lifetime", metavar="DURATION", type=duration, default=DEFAULT_LIFETIME,
                        help="how long the key is kept in memory: 30m, 8h, 2d (default: 8h; at most 30d)")
    parser.add_argument("--idle", metavar="DURATION", type=duration,
                        help="also lock after this long without a secret being read or kept")


def unlocked_agent(key, ident, args, out):
    """The agent started with KEY; said."""
    if not start_agent(key, ident, args.lifetime, args.idle):
        raise ValueError("the agent that keeps the key did not start")
    idle = f", or {duration_text(args.idle)} without use" if args.idle else ""
    out(f"unlocked for {duration_text(args.lifetime)}{idle} (sandbox-ai secrets lock ends it)")


def password_main(args, stdin, out):
    """password set|change|remove, unlock, lock, status."""
    directory = files_dir()
    if args.command == "status":
        info = protection(directory)
        if args.json:
            out(json.dumps(info))
        else:
            out(f"secrets in private files: {info['encrypted']} encrypted, {len(info['plain'])} plain"
                + (f" ({info['kdf']})" if info["kdf"] else ""))
            out(protection_text(info))
            if plain_files_text(info):
                out(plain_files_text(info))
        return 0
    header = read_header(directory)
    if args.command == "lock":
        was = bool(header and agent_call(header["id"], {"op": "lock"}))
        if header:
            _shared().keys.pop(header["id"], None)
            for _ in range(40):
                if not was or agent_call(header["id"], {"op": "status"}) is None:
                    break
                time.sleep(0.05)
        out("locked" if was else "locked (it was already)" if header else
            f"nothing to lock: no primary password is set ({SET_PASSWORD})")
        return 0
    if args.command == "unlock":
        if header is None:
            raise ValueError(f"no primary password is set ({SET_PASSWORD})")
        given = read_passwords(args, stdin, 1)
        key = unlock_key(given[0], directory) if given else ask_unlock(directory)
        moved = []
        done = encrypt_plain_files(directory, LocalKey(key), moved)
        if done:
            out(f"{_count(len(done), 'plain file')} encrypted: {', '.join(done)}")
        if moved:
            out(f"moved under the primary password: {', '.join(moved)}")
        unlocked_agent(key, header["id"], args, out)
        return 0
    if args.action == "set":
        if header is not None:
            raise ValueError("a primary password is already set (change it: sandbox-ai secrets password change)")
        given = read_passwords(args, stdin, 1)
        before = len(protection(directory)["plain"])
        moved = []
        key = set_password(given[0] if given else ask_new_password(), directory, moved)
        out(f"primary password set: {_count(before, 'secret')} encrypted. It is asked once per session; there is no "
            "way to recover it")
        if moved:
            out(f"moved under the primary password: {', '.join(moved)}")
        if not args.locked:
            unlocked_agent(key, read_header(directory)["id"], args, out)
    elif args.action == "change":
        if header is None:
            raise ValueError(f"no primary password is set ({SET_PASSWORD})")
        given = read_passwords(args, stdin, 2)
        if given:
            change_password(given[0], given[1], directory)
        else:
            key = ask_unlock(directory)
            new = ask_new_password()
            with store_lock(directory):
                write_header(directory, new_header(new, key, ident=header["id"]))
        out("primary password changed")
    else:
        if header is None:
            raise ValueError(f"no primary password is set ({SET_PASSWORD})")
        given = read_passwords(args, stdin, 1)
        moved = []
        if given:
            count = remove_password(given[0], directory, moved)
        else:
            password = ask_hidden("Primary password (sandbox-ai secrets, not shown): ")
            count = remove_password(password, directory, moved)
        out(f"primary password removed: {_count(count, 'secret')} now in plain files, not encrypted ({SET_PASSWORD} "
            "encrypts them again)")
        if moved:
            out(f"plain again: {', '.join(moved)}")
    return 0


_credrefs = None


def credrefs():
    """credrefs.py (what names a secret, and a rename carried through it); loaded only for `rename` and `delete`."""
    global _credrefs
    if _credrefs is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_ai_credrefs_for_secrets", Path(__file__).resolve().parent
                                                      / "credrefs.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _credrefs = loaded
    return _credrefs


def rename(backend, old, new, out):
    """`secrets rename OLD NEW`: the value kept as NEW where OLD is kept (the keyring, the Keychain or the private
    files - encrypted as OLD was), OLD deleted, then every reference sandbox-ai keeps (credrefs). The value is never
    printed, logged or passed to another process. ValueError: no such secret, a name taken, a key a VPN profile
    keeps under its own name."""
    check_name(old)
    check_name(new)
    if old == new:
        raise ValueError("OLD and NEW are the same name")
    if old == FILES_KEY:
        raise ValueError(f"{FILES_KEY} is the key of sandbox-ai's sealed files: it keeps its name")
    stores = [backend] if backend.name == "file" else [backend, FileStore()]
    holder, value, plain = None, None, False
    for candidate in stores:
        value = candidate.get(old)
        if value is not None:
            # (now: the look at NEW below reads the store again, and a FileStore's plain_read is of its last read)
            holder, plain = candidate, bool(getattr(candidate, "plain_read", False))
            break
    if holder is None:
        raise ValueError(f"no secret {old} (sandbox-ai secrets list)")
    if any(candidate.get(new) is not None for candidate in stores):
        raise ValueError(f"a secret {new} is kept already: choose another name, or delete it first (sandbox-ai "
                         f"secrets delete {new})")
    refs = credrefs()
    own = [ref["what"] for ref in refs.secret_references(old) if ref["kind"] == "vpn-own"]
    if own:
        raise ValueError(f"{old} is a key of {own[0]}: it is named after the profile and goes with it (sandbox-ai "
                         "vpn remove NAME); not renamed")
    if holder.name == "file":
        holder.set(new, value, plain=plain)
    else:
        holder.set(new, value)
    if holder.get(new) != value:  # never delete the only copy of a value that did not arrive
        raise ValueError(f"{new} was not kept as {old} is ({holder.name}): {old} stays as it is")
    value = None  # noqa: F841 - held no longer than the move
    holder.delete(old)
    out(f"{old} is now {new} ({holder.name})")
    changed, left = refs.rename_secret_references(old, new)
    for text in changed:
        out(f"now names {new}: {refs.printable(text)}")
    for text in left:
        out(f"note: still names {old}: {refs.printable(text)}")
    if not changed and not left:
        out(f"nothing sandbox-ai keeps named {old}")
    return 0


def hosts_module():
    """hosts.py (a command on another host over SSH); loaded only for `send`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_ai_hosts_for_secrets", Path(__file__).resolve().parent
                                                  / "hosts.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def remote_names(hosts, host):
    """The names of the secrets kept on HOST (`sandbox-ai secrets list` there: its table, names only). ValueError:
    that host did not answer, or its sandbox-ai knows no secrets."""
    status, said, err = hosts.run(host, ["secrets", "list"], timeout=60)
    if status != 0:
        raise ValueError(f"{host}: {(err or said).strip()[-300:] or f'exit status {status}'}")
    lines = said.splitlines()
    return {line.split()[0] for line in lines[1:] if line.split() and NAME.fullmatch(line.split()[0])} \
        if lines and lines[0].split() == ["NAME", "STORE"] else set()


def send(name, host, out, remote_name=None, replace=False, yes=False, hosts=None, ask=None):
    """`secrets send NAME HOST`: this machine's secret NAME kept on HOST as well (as REMOTE_NAME there). The value
    is read from the store into this process and written to the standard input of `sandbox-ai secrets set` on HOST
    (hosts.run: over its SSH connection) - never an argument, never a file, never printed or logged. Asked first
    (ASK(question), default: the terminal) unless YES; a secret of that name there is replaced only with REPLACE."""
    check_name(name)
    remote_name = remote_name or name
    check_name(remote_name)
    hosts = hosts or hosts_module()
    if host in (None, "", "local", getattr(hosts, "LOCAL", "local")):
        raise ValueError("HOST is another host of sandbox-ai hosts (this machine has the secret already)")
    if host not in hosts.load():
        known = ", ".join(sorted(key for key in hosts.load() if key != getattr(hosts, "LOCAL", "local"))) or "none yet"
        raise ValueError(f"no host {host} (sandbox-ai hosts: {known})")
    value = get(name)
    if value is None:
        raise ValueError(f"no secret {name} on this machine (sandbox-ai secrets list)")
    if "\n" in value or not value or len(value) > LIMIT or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} is not one line of 1-{LIMIT} characters (a key file, bridge lines): sandbox-ai "
                         "secrets set takes one line, so it is not sent")
    there = remote_names(hosts, host)
    if remote_name in there and not replace:
        raise ValueError(f"{host} keeps a secret {remote_name} already: add --replace to replace its value there "
                         "(everything that names it on that host gets the new one), or send it under another name "
                         "(--as NAME)")
    if not yes:
        question = (f"Send the secret {name} to {host}" + (f" (kept there as {remote_name})" if remote_name != name
                                                           else "") +
                    "? Its value goes over SSH into that host's secret store and is kept there from then on: "
                    "whoever controls that host can use it" + ("; the secret of that name there is replaced"
                                                               if remote_name in there else "") + ".")
        if ask is None:
            if not (sys.stdin.isatty() and sys.stderr.isatty()):
                raise ValueError("there is no terminal to ask: add --yes to send it")
            print(question + " [y/N] ", end="", file=sys.stderr, flush=True)
            agreed = sys.stdin.readline().strip().lower() in ("y", "yes", "j", "ja")
        else:
            agreed = ask(question)
        if not agreed:
            out("Not sent.")
            return 1
    status, said, err = hosts.run(host, ["secrets", "set", remote_name], input=value + "\n", timeout=120)
    value = None  # noqa: F841 - held no longer than the send
    if status != 0:
        raise ValueError(f"{host} did not keep it: {(err or said).strip()[-400:] or f'exit status {status}'}")
    out(f"{name} sent to {host}: {said.strip().splitlines()[-1] if said.strip() else remote_name + ' kept'}")
    out(f"give it to a sandbox there: sandbox-ai hosts run {host} -- secrets give RUN {remote_name} --api-key HOST "
        f"(an API key), or sandbox-ai hosts run {host} -- git-logins give RUN URL --secret {remote_name} (a Git "
        f"login); take it away there: sandbox-ai hosts run {host} -- secrets delete {remote_name}")
    return 0


def main(argv, stdin=sys.stdin, out=print, err=None):
    if argv[:1] == ["agent"]:  # (the process `unlock` starts: not a command of its own)
        return agent_main()
    parser = argparse.ArgumentParser(prog="sandbox-ai secrets", description=__doc__.split("\n\n")[0],
                                     formatter_class=help_style,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="names (never values) and where they are kept")
    commands.add_parser("backend", help="where secrets are kept (keyring, keychain or file) and what protects them")
    status = commands.add_parser("status", help="the protection in force: encrypted, plain, locked or unlocked")
    status.add_argument("--json", action="store_true", help="as JSON, for scripts")
    setting = commands.add_parser("set", help="keep a secret: its value from stdin (or --from-env)")
    setting.add_argument("name")
    setting.add_argument("-e", "--from-env", metavar="VARIABLE", help="take the value from this variable")
    setting.add_argument("--plain", action="store_true",
                         help="private files without a primary password: keep it unencrypted (your choice)")
    password = commands.add_parser("password", allow_abbrev=False,
                                   help="the primary password that encrypts the private files: set, change, remove",
                                   description="The primary password of sandbox-ai's private files (used where no "
                                               "system keyring is): set encrypts every secret kept there, change "
                                               "takes the current one and a new one, remove decrypts them again. "
                                               "Asked at the terminal, typed twice, never shown; for scripts one "
                                               "line per password on standard input or a descriptor (change: the "
                                               "current one, then the new one). There is no way to recover it.")
    password.add_argument("action", choices=["set", "change", "remove"])
    password_options(password)
    password.add_argument("--locked", action="store_true", help="set: do not keep the key in memory afterwards")
    unlock_options(password)
    unlock = commands.add_parser("unlock", allow_abbrev=False,
                                 help="ask for the primary password once and keep the key in memory",
                                 description="Unlock the encrypted private files for this session: the key is kept "
                                             "in memory by a small process of your user (never on disk), which "
                                             "ends after --for, after --idle without use, or with secrets lock.")
    password_options(unlock)
    unlock_options(unlock)
    commands.add_parser("lock", help="forget the key now: the next secret asks for the primary password again")
    # remove: as vpn, llm and hosts say
    commands.add_parser("delete", aliases=["remove"], help="forget a secret").add_argument("name")
    sending = commands.add_parser("send", help="keep this machine's secret NAME on another host as well",
                                  description="Copy this machine's secret NAME to HOST (a host of sandbox-ai hosts): "
                                              "its value goes over SSH on standard input into sandbox-ai secrets "
                                              "set there - never in a command line, a log or a temporary file - "
                                              "and is kept in that host's secret store from then on, where its "
                                              "sandboxes can be given it (secrets give, git-logins give). Asked "
                                              "first; whoever controls that host can use what is kept there.")
    sending.add_argument("name", metavar="NAME")
    sending.add_argument("host", metavar="HOST")
    sending.add_argument("--as", dest="remote_name", metavar="NAME", help="the name it is kept as there (default: "
                                                                          "its name here)")
    sending.add_argument("--replace", action="store_true", help="replace a secret of that name kept there already")
    sending.add_argument("--yes", action="store_true", help="send it without asking")
    renaming = commands.add_parser("rename", help="another name for a secret, in everything that names it",
                                   description="Give the secret OLD the name NEW (its value is moved, never shown) "
                                               "and carry the name through what sandbox-ai keeps: Git and publish "
                                               "logins, identities, VPN profiles, the options of trusted project "
                                               "files and of kept starts. What still names OLD is said. A sandbox "
                                               "that runs already has the value: nothing changes for it.")
    renaming.add_argument("old", metavar="OLD")
    renaming.add_argument("new", metavar="NEW")
    listing.add_argument("--run", metavar="RUN",
                         help="the API keys the running sandbox RUN was given during the run (secrets give)")
    listing.add_argument("--json", action="store_true", help="with --run: as JSON, for scripts")
    given = commands.add_parser("give", help="give a running sandbox the secret NAME as an API key, now",
                                description="Give the running sandbox RUN the secret NAME as an API key for HTTPS "
                                            "requests to HOST: its connection proxy puts the key in on the wire where "
                                            "the agent sends the placeholder (export VARIABLE=..., said here); the "
                                            "agent never sees it. The older form is sandbox-ai net allow RUN "
                                            "--api-key HOST=VARIABLE=NAME.")
    given.add_argument("run")
    given.add_argument("name", help="the secret")
    given.add_argument("--api-key", required=True, metavar="HOST", dest="host",
                       help="the HTTPS server the key is for (a host name); the sandbox may reach it from now on")
    given.add_argument("--as", metavar="VARIABLE", dest="variable",
                       help="the variable whose placeholder the agent sends (default: NAME)")
    given.add_argument("--keep", action="store_true",
                       help="also for the next starts: into the project's trusted .sandbox-ai.json, as a reference")
    taken = commands.add_parser("revoke", help="take back an API key given during the run")
    taken.add_argument("run")
    taken.add_argument("name", help="the secret, or the variable it was given as")
    args = parser.parse_args(argv)
    if args.command in ("give", "revoke") or (args.command == "list" and args.run):
        return given_main(args, out)
    try:
        if args.command in ("password", "unlock", "lock", "status"):
            return password_main(args, stdin, out)
        backend = store()
        if args.command == "backend":
            out(protection_text(protection(backend=backend)))
        elif args.command == "list":
            names = sorted(set(backend.names()) | (set(FileStore().names()) if backend.name != "file" else set()))
            in_backend = set(backend.names())
            rows = [[name, backend.name if name in in_backend else "file"] for name in names]
            if rows:  # values are never shown, only where each is kept
                widths = [max(len(r[i]) for r in rows + [["NAME", "STORE"]]) for i in range(2)]
                for row in [["NAME", "STORE"]] + rows:
                    out(f"{row[0].ljust(widths[0])}  {row[1]}")
            else:
                out("no secrets")
        elif args.command == "rename":
            return rename(backend, args.old, args.new, out)
        elif args.command == "send":
            return send(args.name, args.host, out, remote_name=args.remote_name, replace=args.replace, yes=args.yes)
        elif args.command == "set":
            check_name(args.name)
            if backend.name == "file":  # the primary password first (asked or said), then the value
                backend.ready(args.plain)
            elif args.plain:
                raise ValueError(f"--plain is for the private files; here the secrets go to the {backend.name}")
            if args.from_env:
                if args.from_env not in os.environ:
                    raise ValueError(f"the variable {args.from_env} is not set")
                value = os.environ[args.from_env]
            elif stdin is sys.stdin and sys.stdin.isatty():
                import getpass  # typed at a terminal: not shown, not in the scrollback
                value = getpass.getpass(f"value of {args.name} (not shown): ")
            else:
                text = stdin.read(LIMIT + 2)
                value = text[:-1] if text.endswith("\n") else text
                if "\n" in value:  # a PEM key, JSON over lines: refused, never cut to its first line
                    raise ValueError("the value must be one line (it had several; a multi-line value such as a key "
                                     "file: keep it as a file of mode 0600 instead)")
            if not value or len(value) > LIMIT or any(ord(c) < 32 for c in value):
                raise ValueError("the value must be one line of 1-8192 characters")
            if backend.name == "file":
                encrypted = backend.set(args.name, value, plain=args.plain)
                out(f"{args.name} kept (file, encrypted)" if encrypted else
                    f"{args.name} kept (file, not encrypted: {SET_PASSWORD})")
            else:
                backend.set(args.name, value)
                out(f"{args.name} kept ({backend.name})")
        else:
            if args.name == FILES_KEY:
                needing = [str(path) for path, _ in sealable_files() if
                           (read_private(path, 128) or b"").startswith(FILE_MAGIC + f"system:{FILES_KEY}".encode())]
                if needing:
                    raise ValueError(f"{FILES_KEY} is the key of sealed files that need it ({', '.join(needing)}): "
                                     "remove those first (sandbox-ai vpn remove NAME), else they can never be read")
            found = backend.delete(args.name) | (FileStore().delete(args.name) if backend.name != "file" else False)
            out(f"{args.name} deleted" if found else f"{args.name} was not there")
            if found:  # (what still names it does without it from now on: a start leaves it out)
                with contextlib.suppress(Exception):
                    refs = credrefs()
                    for ref in refs.secret_references(args.name):
                        out(f"note: still names {args.name}: {refs.printable(ref['what'])}")
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"sandbox-ai secrets: {exc}", file=err or sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
