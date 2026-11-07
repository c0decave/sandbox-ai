"""Release signing keys: SSH signatures over a release's SHA256SUMS (ssh-keygen -Y sign/verify).

sandbox/release-signers lists the keys that may sign releases, in ssh-keygen's ALLOWED SIGNERS format and
always as: release@sandbox-ai namespaces="sandbox-ai-release" TYPE KEY [comment]. scripts/make_dist.py and
scripts/publish_release.py sign and check with this module; `sandbox-ai version --verbose` lists the keys.
install.sh carries its own copy of the parsing and checking (it runs before any tree is installed); keep the two
in step (tests/test_install.py compares them).
"""
import base64
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

NAMESPACE = "sandbox-ai-release"
PRINCIPAL = "release@sandbox-ai"
SIGNERS = Path(__file__).resolve().parent / "release-signers"
KEY_TYPES = (r'(ssh-ed25519|sk-ssh-ed25519@openssh\.com|ecdsa-sha2-nistp(?:256|384|521)|'
             r'sk-ecdsa-sha2-nistp256@openssh\.com|ssh-rsa)\s+([A-Za-z0-9+/]+={0,2})(?:\s.*)?')
KEY_LINE = re.compile(r'release@sandbox-ai\s+namespaces="sandbox-ai-release"\s+' + KEY_TYPES)
# The MCP servers of sandbox-ai-mcp: its catalog.json (image digests, rootfs hashes) is signed by one of these keys
# once each server passed its checks with sandbox-ai under Podman and Bubblewrap (mcp.py checks the signature).
MCP = {"namespace": "sandbox-ai-mcp", "principal": "mcp@sandbox-ai",
       "signers": Path(__file__).resolve().parent / "mcp-signers"}
RELEASE = {"namespace": NAMESPACE, "principal": PRINCIPAL, "signers": SIGNERS}


def key_line(ring):
    return re.compile(re.escape(ring["principal"]) + r'\s+namespaces="' + re.escape(ring["namespace"]) + r'"\s+'
                      + KEY_TYPES)


def parse(text, origin="sandbox/release-signers", ring=RELEASE):
    """[(type, base64 key)] of an allowed signers text; comments and blank lines aside, anything else is an error."""
    keys = []
    pattern = KEY_LINE if ring is RELEASE else key_line(ring)
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        try:
            blob = base64.b64decode(match[2], validate=True) if match else b""
        except ValueError:
            blob = b""
        if not match or blob[4:4 + int.from_bytes(blob[:4], "big")] != match[1].encode():
            raise ValueError(f'{origin}, line {number}: not a key of the form {ring["principal"]} '
                             f'namespaces="{ring["namespace"]}" TYPE KEY')
        keys.append((match[1], match[2]))
    return keys


def load(path=None, ring=RELEASE):
    path = Path(path or ring["signers"])
    return parse(path.read_text(), str(path), ring) if path.is_file() else []


def fingerprint(key):
    """OpenSSH's SHA256 fingerprint of a base64 public key."""
    digest = hashlib.sha256(base64.b64decode(key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def line(kind, key, ring=RELEASE):
    return f'{ring["principal"]} namespaces="{ring["namespace"]}" {kind} {key}'


def normalized(keys, ring=RELEASE):
    """The keys as allowed signers lines without comments (what install.sh embeds and checks with)."""
    return "".join(line(kind, key, ring) + "\n" for kind, key in keys)


def signature_info(signature):
    """(namespace, key fingerprint) of an armored SSH signature, None if it is none."""
    match = re.search(rb"-----BEGIN SSH SIGNATURE-----(.*?)-----END SSH SIGNATURE-----", signature, re.S)
    try:
        blob = base64.b64decode(b"".join(match[1].split()), validate=True) if match else b""
    except ValueError:
        return None
    if blob[:6] != b"SSHSIG":
        return None
    fields, position = [], 10  # magic, version
    for _ in range(2):  # the public key, the namespace
        size = int.from_bytes(blob[position:position + 4], "big")
        position += 4
        if position + size > len(blob):
            return None
        fields.append(blob[position:position + size])
        position += size
    key, namespace = fields
    return namespace.decode(errors="replace"), fingerprint(base64.b64encode(key))


def ssh_keygen():
    path = shutil.which("ssh-keygen")
    if not path:
        raise RuntimeError("signatures need ssh-keygen (OpenSSH 8.1 or newer)")
    return path


def verify(data, signature, keys, ring=RELEASE, what="SHA256SUMS"):
    """('good', fingerprint) when one of the keys signed data in the ring's namespace, else ('bad', reason)."""
    namespace_wanted, principal = ring["namespace"], ring["principal"]
    info = signature_info(signature)
    if info is None:
        return "bad", f"{what}.sig is not an SSH signature"
    namespace, signer = info
    if namespace != namespace_wanted:
        return "bad", f"the signature is for {namespace!r}, not {namespace_wanted!r}"
    if signer not in {fingerprint(key) for _, key in keys}:
        return "bad", f"signed by {signer}, which is not a trusted {'release' if ring is RELEASE else 'MCP'} key"
    with tempfile.TemporaryDirectory() as tmp:
        allowed, sig = Path(tmp, "allowed_signers"), Path(tmp, f"{what}.sig")
        allowed.write_text(normalized(keys, ring))
        sig.write_bytes(signature)
        result = subprocess.run([ssh_keygen(), "-Y", "verify", "-f", str(allowed), "-I", principal, "-n",
                                 namespace_wanted, "-s", str(sig)], input=data, capture_output=True)
    if result.returncode == 0:
        return "good", signer
    detail = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
    return "bad", f"the signature does not match {what} ({detail[0] if detail else 'ssh-keygen failed'})"


def sign(data, key, ring=RELEASE):
    """An SSH signature of data with KEY: a private key file, or the .pub of a key in ssh-agent (or a FIDO key)."""
    result = subprocess.run([ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", ring["namespace"]], input=data,
                            stdout=subprocess.PIPE)
    if result.returncode != 0 or not signature_info(result.stdout):
        raise RuntimeError(f"ssh-keygen -Y sign -f {key} failed")
    return result.stdout
