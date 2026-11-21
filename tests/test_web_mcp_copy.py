"""The web interface's "From another host" in a running start's resources card (0.1.98): MCP servers of another host
copied to the start's host and attached to it now - `sandbox-ai mcp copy ... --json` run by the service's machine,
which reaches both hosts (sandbox-ai hosts). A check first (--dry-run: what is copied, changed, and what the target
cannot take as it is), the copy asked first (mcp-copy); every value one fixed argument, never an option."""
import json
from pathlib import Path
import re
import subprocess

import pytest

import test_web as tw
from test_web import home, logged_in  # noqa: F401  (fixtures)

w = tw.w
ROOT = Path(__file__).resolve().parents[1]


def answer(logged_in, result, status=0, err=""):  # noqa: F811
    logged_in.runner.answers[("mcp", "copy")] = (status, json.dumps(result) if result is not None else "", err)


PLAN = {"from": "lab1", "to": "ct1", "servers": [{"server": "files", "problems": [], "paths": []}], "attached": [],
        "written": False, "dry_run": True}


def test_a_check_is_a_dry_run_of_mcp_copy_on_this_machine(logged_in):  # noqa: F811
    answer(logged_in, PLAN)
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-copy", {"from": "lab1", "names": ["files"], "dry_run": True})
    assert status == 200 and out == PLAN
    assert logged_in.runner.calls[-1] == ("local", ["mcp", "copy", "files", "--from", "lab1", "--to", "ct1", "--json",
                                                    "--dry-run"])


def test_the_copy_is_asked_first_and_every_choice_is_one_argument(logged_in):  # noqa: F811
    body = {"from": "local", "names": ["files", "web-search"], "paths": {"/home/me/data": "/srv/data"},
            "drop_paths": ["/home/me/cache"], "secrets_later": True, "copy_image": True, "no_vpn": True,
            "current_pin": True, "unsigned": True, "attach": "web"}
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-copy", body)
    assert status == 400 and out["confirm"] == "mcp-copy"
    assert not [c for c in logged_in.runner.calls if c[1][:2] == ["mcp", "copy"]]
    answer(logged_in, {**PLAN, "written": True, "dry_run": False})
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-copy", {**body, "confirm": True})
    assert status == 200 and out["written"]
    assert logged_in.runner.calls[-1] == ("local", [
        "mcp", "copy", "files", "web-search", "--from", "local", "--to", "ct1", "--json", "--path=/home/me/data=/srv/data",
        "--drop-path=/home/me/cache", "--secrets-later", "--copy-image", "--no-vpn", "--current-pin", "--unsigned",
        "--attach=web"])
    # One server under another name; a name it has replaced (asked: the same confirmation, which says so).
    status, _ = logged_in("POST", "/api/hosts/ct1/mcp-copy", {"from": "lab1", "names": ["files"], "as": "files2",
                                                             "replace": True, "pull_image": True, "confirm": True})
    assert status == 200
    assert "--as=files2" in logged_in.runner.calls[-1][1] and "--pull-image" in logged_in.runner.calls[-1][1]
    assert logged_in.runner.calls[-1][1][-2:] == ["--replace", "--yes"]


@pytest.mark.parametrize("body", [
    {"from": "lab1", "names": ["-x"], "dry_run": True},
    {"from": "lab1", "names": [], "dry_run": True},
    {"from": "lab1", "names": "files", "dry_run": True},
    {"from": "--to", "names": ["files"], "dry_run": True},
    {"from": "lab1", "names": ["files"], "as": "-h", "dry_run": True},
    {"from": "lab1", "names": ["a", "b"], "as": "c", "dry_run": True},
    {"from": "lab1", "names": ["files"], "paths": {"relative": "/x"}, "dry_run": True},
    {"from": "lab1", "names": ["files"], "paths": {"/a": "x"}, "dry_run": True},
    {"from": "lab1", "names": ["files"], "paths": ["/a=/b"], "dry_run": True},
    {"from": "lab1", "names": ["files"], "drop_paths": ["x"], "dry_run": True},
    {"from": "lab1", "names": ["files"], "attach": "--all", "dry_run": True},
    {"from": "lab1", "names": ["files"], "copy_image": "yes", "dry_run": True},
    {"from": "lab1", "names": ["files"], "copy_image": True, "pull_image": True, "dry_run": True},
    {"from": "lab1", "names": ["files"], "dry_run": "yes"},
    {"from": "ct1", "names": ["files"], "dry_run": True},  # (the same host: --as needed, said by the copy itself)
])
def test_what_is_no_server_host_or_folder_is_refused_before_anything_runs(logged_in, body):  # noqa: F811
    status, _ = logged_in("POST", "/api/hosts/ct1/mcp-copy", body)
    if body.get("from") == "ct1":
        assert status == 400
        return
    assert status == 400
    assert not [c for c in logged_in.runner.calls if c[1][:2] == ["mcp", "copy"]]


def test_an_older_sandbox_ai_there_is_said_as_the_copy_says_it(logged_in):  # noqa: F811
    answer(logged_in, None, 1, "sandbox-ai mcp: the sandbox-ai on ct1 does not know `sandbox-ai mcp import` yet: it "
                               "needs 0.1.98 or later (this machine has 0.1.98); update it from here: sandbox-ai hosts "
                               "update ct1 --from-here, then copy again")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-copy", {"from": "lab1", "names": ["files"], "dry_run": True})
    assert status >= 400 and "hosts update ct1 --from-here" in out["error"]


def test_the_page_offers_servers_of_the_other_hosts_and_says_the_plan_in_german():
    """The resources card: "From another host" with the other hosts' servers (read in the background), Check, the
    plan's folders as fields (a folder there, or left out), Copy and add now… asked first; every text in German."""
    page = (ROOT / "sandbox/web/app.js").read_text()
    for needed in ("/mcp-copy", "copyBody(true)", "drop_paths", "secrets_later", "copy_image", "body.attach = name",
                   "From another host"):
        assert needed in page, needed
    i18n = (ROOT / "sandbox/web/i18n.js").read_text()
    for text in ("From another host", "Check", "Copy and add now…", "leave out"):
        assert re.search(r'"' + re.escape(text) + r'"\s*:', i18n), text
