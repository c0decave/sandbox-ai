"""Independent verification A (2026-11-19, docs/reviews/credentials-verify-A.md): the web interface takes back what it
gives a running start - a Git login and an API key (`sandbox-ai net revoke NAME --git-token-*|--api-key`), on this
machine and on a host of sandbox-ai hosts. Through the real Service and Api down to the command line (test_web_matrix:
only the process is faked)."""
import json

import pytest

from test_web_matrix import Web, home  # noqa: F401 - the isolated XDG folders of the web's tests

BODY = {"allow_host": [], "allow_tcp": [], "host_ports": [], "dns": False}


@pytest.mark.parametrize("host", ("local", "ct1"))
def test_the_web_takes_back_a_git_login_it_gave(host):
    web = Web()
    status, _, data = web.request("POST", f"/api/hosts/{host}/net/web", {
        **BODY, "op": "revoke", "git_login": {"url": "https://github.com", "kind": "env", "ref": "REVOKE"}},
        web.headers("full"))
    assert status == 200, data
    assert web.processes.args() == [(host, ["net", "revoke", "web", "--git-token-env=https://github.com=REVOKE"])]


@pytest.mark.parametrize("host", ("local", "ct1"))
def test_the_web_takes_back_an_api_key_it_gave(host):
    """Finding 3 of credentials-verify-A (fixed 2026-11-19; a strict xfail until then): an API key given during the
    run from the web could not be taken back there - the API refused api_key for revoke ("for allow") and the page's
    row of a given key had no Revoke."""
    web = Web()
    key = {"host": "api.github.com", "name": "GH_TOKEN", "secret": "GH_PUSH"}
    status, _, data = web.request("POST", f"/api/hosts/{host}/net/web", {**BODY, "op": "revoke", "api_key": key},
                                  web.headers("full"))
    assert status == 200, json.loads(data)
    [(where, command)] = web.processes.args()
    assert where == host and command[:3] == ["net", "revoke", "web"]
    assert any(arg.startswith("--api-key") and "GH_TOKEN" in arg for arg in command[3:]), command


def test_a_revoke_of_an_api_key_names_its_variable_and_nothing_else(tmp_path):
    """The row's Revoke sends the variable alone ({name}): `net revoke NAME --api-key=VARIABLE`, never read as another
    option; a name that is none is refused before anything runs. A Git login into the sandbox (a run whose proxy
    cannot log in) is the human's word in the request, for allow only."""
    web = Web()
    status, _, data = web.request("POST", "/api/hosts/local/net/web", {**BODY, "op": "revoke",
                                                                      "api_key": {"name": "OR_KEY"}},
                                  web.headers("full"))
    assert status == 200, data
    assert web.processes.args() == [("local", ["net", "revoke", "web", "--api-key=OR_KEY"])]
    for bad in ({"name": "--keep"}, {"name": "A=B"}, {}, "OR_KEY"):
        web = Web()
        status, _, data = web.request("POST", "/api/hosts/local/net/web", {**BODY, "op": "revoke", "api_key": bad},
                                      web.headers("full"))
        assert status == 400 and web.processes.args() == [], bad
    web = Web()
    login = {"url": "http://localhost:3080", "kind": "secret", "ref": "GT", "in_sandbox": True}
    status, _, data = web.request("POST", "/api/hosts/local/net/web", {**BODY, "op": "allow", "confirm": True,
                                                                      "git_login": login}, web.headers("full"))
    assert status == 200, data
    assert web.processes.args() == [("local", ["net", "allow", "web", "--git-token-secret=http://localhost:3080=GT",
                                               "--git-token-in-sandbox"])]
    web = Web()
    status, _, data = web.request("POST", "/api/hosts/local/net/web", {**BODY, "op": "revoke", "git_login": login},
                                  web.headers("full"))
    assert status == 200 and web.processes.args() == [("local", ["net", "revoke", "web",
                                                                 "--git-token-secret=http://localhost:3080=GT"])]
