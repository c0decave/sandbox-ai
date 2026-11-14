"""An MCP server that runs as PID 1 itself ("pid1": "server" in its catalog entry or definition), found by the
independent review of the exec server: under Podman `--init` puts catatonit at PID 1, under Bubblewrap bwrap's own
reaper is PID 1, and either holds the server's MCP pipes - stdin and stdout of the connection - which every process of
the server can open through /proc/1/fd. For a server that runs arbitrary commands (exec) a command could write MCP
messages to the client. With "pid1": "server" the server is PID 1 (Podman without --init, bwrap --as-pid-1) and
reaps its children itself; every other server keeps the default."""
import json

import pytest

import test_mcp as tm
import test_mcp_rootfs as trf
from test_mcp import isolated, mcp_key, trusted_mcp_key  # noqa: F401  (fixtures)
from test_mcp_rootfs import data_home  # noqa: F401  (fixture)

m, r, s, DIGEST = tm.m, trf.r, tm.s, tm.DIGEST
IMAGE = f"ghcr.io/c0decave/sandbox-ai-mcp-exec@{DIGEST}"
EXEC = {"id": "mcp:exec", "name": "exec", "title": "exec", "kind": "oci", "image": "ghcr.io/c0decave/sandbox-ai-mcp-exec",
        "digest": DIGEST, "network": "none", "pid1": "server"}


def image(**fields):
    return m.validated_image({"image": IMAGE, "network": "none", **fields}, "exec")


def test_the_field_is_checked_like_the_others():
    assert image(pid1="server")["pid1"] == "server"
    for bad in ("init", "", "SERVER", True, 1, None, ["server"]):
        with pytest.raises(ValueError, match='pid1 must be "server"'):
            image(pid1=bad)
    with pytest.raises(ValueError, match="allowed keys"):  # a command server runs in the launcher's sandbox
        m.validated({"x": {"command": ["sh"], "pid1": "server"}}, "test")
    assert m.validated({"exec": {"image": IMAGE, "pid1": "server"}}, "mcp.json")["exec"]["pid1"] == "server"


def test_podman_run_lines_with_and_without_it():
    plain = m.image_command(m.server_network("t", image()))
    own = m.image_command(m.server_network("t", image(pid1="server")))
    assert "--init" in plain, "the default stays: catatonit reaps a server's children"
    assert "--init" not in own and not any(flag.startswith("--init") for flag in own)
    # Nothing else of the hardening changes.
    assert [flag for flag in plain if flag != "--init"] == own
    for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--network=none", "--rm", "-i"):
        assert flag in own, flag


def test_behind_a_vpn_too():
    definition = image(pid1="server", network="full", vpn="work")
    command = m.image_command({"kind": "image", "definition": definition, "server": "t", "vpn": "work",
                               "vpn_container": "sandbox-ai-vpn-mcp-t", "network": "--network=container:sandbox-ai-vpn-mcp-t"})
    assert "--init" not in command and "--network=container:sandbox-ai-vpn-mcp-t" in command


def test_bwrap_args_with_and_without_it(tmp_path):
    plain = r.bwrap_args(trf.definition(tmp_path), {})
    own = r.bwrap_args({**trf.definition(tmp_path), "pid1": "server"}, {})
    assert "--as-pid-1" not in plain and "--as-pid-1" in own
    assert "--unshare-pid" in own, "--as-pid-1 needs its own PID namespace"
    assert [arg for arg in own if arg != "--as-pid-1"] == plain


def test_hostile_a_definition_cannot_bring_init_back_or_drop_the_namespace(tmp_path):
    """Whatever else a definition says, "pid1": "server" gives no --init and bwrap --as-pid-1 with its PID namespace;
    and a value that is not "server" is refused, never read as the default or as on."""
    own = image(pid1="server", memory="1g", pids=64, tmp="32m")
    command = m.image_command(m.server_network("t", own))
    assert command.count("--init") == 0 and command[-1] == IMAGE
    args = r.bwrap_args({**trf.definition(tmp_path, network="full"), "pid1": "server"}, {})
    assert args.count("--as-pid-1") == 1 and "--unshare-pid" in args and "--unshare-net" in args
    for bad in ("server ", "Server", "init"):
        with pytest.raises(ValueError):
            image(pid1=bad)


def test_mcp_add_carries_it_from_the_catalog(tmp_path):
    sources = tm.catalog(tmp_path, [EXEC, {**tm.BINWALK}])
    definition = m.add("exec", catalogs=sources, pull=False, out=[].append)
    assert definition["pid1"] == "server" and m.definitions()["exec"]["pid1"] == "server"
    project = tmp_path / "project"  # (a project folder: tmp_path itself holds sandbox-ai's state, refused as a mount)
    project.mkdir()
    assert "pid1" not in m.add("binwalk", mounts=[f"/work={project}"], catalogs=sources, pull=False, out=[].append)
    # One that is not "server" is refused by add, as a definition's other values are.
    sources = tm.catalog(tmp_path, [{**EXEC, "pid1": "catatonit"}])
    with pytest.raises(ValueError, match='pid1 must be "server"'):
        m.add("exec", catalogs=sources, pull=False, out=[].append)


def test_it_is_part_of_the_identity_of_an_instance():
    assert m.definition_digest(image(), "podman") != m.definition_digest(image(pid1="server"), "podman")


def test_mcp_set_keeps_it(tmp_path):
    sources = tm.catalog(tmp_path, [EXEC])
    m.add("exec", catalogs=sources, pull=False, out=[].append)
    _, new = m.set_definition("exec", {"memory": "1g"})
    assert new["pid1"] == "server" and json.loads(m.config_file().read_text())["exec"]["pid1"] == "server"
