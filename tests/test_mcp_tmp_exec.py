"""An MCP server whose /tmp may run programs ("tmp_exec": true in its signed catalog entry or definition). Catalog CI
45014379428 (2026-11-22): smali's apktool could not build or decode an APK under Podman - apktool chmods its aapt2
helper before every run (on the read-only image that fails, even for its owner) or unpacks it into its temporary
folder and runs it there, and sandbox-ai mounts every MCP server's /tmp noexec under Podman. The user chose "/tmp
with exec for smali only": the entry says so, signed like every entry; every other server keeps noexec (Bubblewrap's
/tmp always allowed it)."""
import pytest

import test_mcp as tm
from test_mcp import isolated, mcp_key, trusted_mcp_key  # noqa: F401  (fixtures)

m, DIGEST = tm.m, tm.DIGEST
IMAGE = f"ghcr.io/c0decave/sandbox-ai-mcp-smali@{DIGEST}"
SMALI = {"id": "mcp:smali", "name": "smali", "title": "smali", "kind": "oci", "image": "ghcr.io/c0decave/sandbox-ai-mcp-smali",
         "digest": DIGEST, "network": "none", "tmp_exec": True}


def image(**fields):
    return m.validated_image({"image": IMAGE, "network": "none", **fields}, "smali")


def tmp_flag(command):
    (flag,) = [part for part in command if part.startswith("--tmpfs=/tmp:")]
    return flag.split(":", 1)[1].split(",")


def test_only_true_is_taken():
    assert image(tmp_exec=True)["tmp_exec"] is True
    for bad in (False, "true", 1, "yes", None, [True]):
        with pytest.raises(ValueError, match="tmp_exec must be true"):
            image(tmp_exec=bad)
    with pytest.raises(ValueError, match="allowed keys"):  # a command server runs in the launcher's own sandbox
        m.validated({"x": {"command": ["sh"], "tmp_exec": True}}, "test")


def test_the_podman_run_line_drops_noexec_for_tmp_and_nothing_else():
    plain = m.image_command(m.server_network("t", image()))
    own = m.image_command(m.server_network("t", image(tmp_exec=True)))
    assert "noexec" in tmp_flag(plain), "every other server keeps a noexec /tmp"
    assert "noexec" not in tmp_flag(own) and "exec" in tmp_flag(own)
    assert {"nosuid", "nodev"} <= set(tmp_flag(own)), "no setuid program and no device node there either way"
    assert [part for part in plain if not part.startswith("--tmpfs=/tmp:")] == \
        [part for part in own if not part.startswith("--tmpfs=/tmp:")], "nothing else of the hardening changes"
    sized = m.image_command(m.server_network("t", image(tmp_exec=True, tmp="256m")))
    assert "size=256m" in tmp_flag(sized) and "noexec" not in tmp_flag(sized)


def test_it_is_part_of_the_servers_identity():
    assert m.definition_digest(image(), "podman") != m.definition_digest(image(tmp_exec=True), "podman")


def test_mcp_add_carries_it_from_the_catalog(tmp_path):
    sources = tm.catalog(tmp_path, [SMALI, {**tm.BINWALK}])
    definition = m.add("smali", catalogs=sources, pull=False, out=[].append)
    assert definition["tmp_exec"] is True and m.definitions()["smali"]["tmp_exec"] is True
