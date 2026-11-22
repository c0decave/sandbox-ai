"""The web interface's offer to build an MCP server its catalog has no signed image of yet (mcp add --build): the add
refused for that says so by its code (mcp-unpublished); asked to build, the service reads what would be built
(--build-plan) and asks with it (mcp-build-asked: the commit, the source archive's SHA-256, unsigned); only with
confirm_build and that commit and archive is exactly that built (--yes). A resume (the web's start) the launcher
refused naming `mcp add NAME --build` says which servers (mcp-unpublished-start). End to end too: the service against
the real ./sandbox-ai with Podman faked (test_mcp_build.BuildHost)."""
import json
import subprocess

import pytest

import test_web as tw
from test_web import home, logged_in  # noqa: F401  (fixtures)
from test_mcp_build import BuildHost, FETCHER, JADX, TIME, catalog_bytes, git_catalog, server_files  # noqa: F401

w = tw.w
COMMIT, ARCHIVE = "1" * 40, "a" * 64
PLAN = {"entry": "mcp:jadx", "repository": "https://github.com/c0decave/sandbox-ai-mcp", "commit": COMMIT,
        "path": "servers/jadx", "archive_sha256": ARCHIVE, "catalog_label": "sandbox-ai-mcp (github.com/c0decave)",
        "signature": "good", "question": "..."}


def test_an_unpublished_entry_is_offered_to_be_built_asked_with_its_commit(logged_in):
    logged_in.runner.answers[("mcp", "list")] = (0, "[]", "")
    logged_in.runner.answers[("mcp", "add")] = (1, "", "sandbox-ai mcp: jadx: the catalog has no digest for it yet "
                                                       "(not published): sandbox-ai mcp add jadx --build builds it\n")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {"entry": "jadx"})
    assert status == 502 and out["code"] == "mcp-unpublished" and out["params"] == {"host": "ct1", "entry": "jadx"}
    logged_in.runner.answers = {("mcp", "list"): (0, "[]", ""), ("mcp", "add"): (0, json.dumps(PLAN), "")}
    body = {"entry": "jadx", "build": True, "mounts": [{"target": "/work", "host": "/srv/apps", "mode": "ro"}]}
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", body)
    assert status == 400 and out["code"] == "mcp-build-asked" and out["confirm"] == "mcp-build"
    assert out["params"]["commit"] == COMMIT and out["params"]["archive_sha256"] == ARCHIVE
    assert COMMIT in out["error"] and "added unsigned" in out["error"]
    assert logged_in.runner.calls[-1] == ("ct1", ["mcp", "add", "--mount=/work=/srv/apps:ro", "--build-plan", "jadx"])
    # a yes without the commit and archive it named is asked again; those without the yes are refused: nothing built
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {**body, "confirm_build": True})
    assert out["code"] == "mcp-build-asked" and "--build-plan" in logged_in.runner.calls[-1][1]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {**body, "commit": COMMIT, "source_sha256": ARCHIVE})
    assert status == 400 and out["confirm"] == "mcp-build" and "--build" not in logged_in.runner.calls[-1][1]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {**body, "commit": COMMIT, "source_sha256": ARCHIVE,
                                                         "confirm_build": True})
    assert status == 200, out
    assert logged_in.runner.calls[-1] == ("ct1", ["mcp", "add", "--mount=/work=/srv/apps:ro", "--build",
                                                  f"--commit={COMMIT}", f"--source-sha256={ARCHIVE}", "--yes", "jadx"])
    status, out = logged_in("POST", "/api/hosts/ct1/mcp", {**body, "no_pull": True})
    assert status == 400


def test_a_resume_refused_for_such_a_server_names_it(logged_in):
    said = ("agent-sandbox: Unknown MCP server: jadx; defined: demo; add a server with sandbox-ai mcp add (sandbox-ai "
            "mcp catalog lists the catalogs' servers), or start without it. jadx: its catalog (sandbox-ai-mcp "
            "(github.com/c0decave)) lists it, but has no signed image of it yet - `sandbox-ai mcp add jadx --build "
            "--unsigned --mount /work=/srv/apps:ro` builds it here from its reviewed source (the commit asked first) "
            "and adds it unsigned; then start again\n")
    logged_in.runner.answers[("resume",)] = (1, "", said)
    status, out = logged_in("POST", "/api/hosts/ct1/starts/web/resume", {"mcp": ["jadx"], "confirm": True})
    assert status == 502 and out["code"] == "mcp-unpublished-start" and out["params"]["names"] == "jadx"
    assert json.loads(out["params"]["server0"]) == {"name": "jadx", "unsigned": True, "mounts": [
        {"target": "/work", "host": "/srv/apps", "mode": "ro"}]}
    logged_in.runner.answers[("resume",)] = (1, "", "agent-sandbox: Unknown MCP server: nosuch; defined: demo\n")
    status, out = logged_in("POST", "/api/hosts/ct1/starts/web/resume", {"mcp": ["nosuch"], "confirm": True})
    assert out["code"] == "command-failed"


def test_end_to_end_the_web_builds_it_through_sandbox_ai(tmp_path):
    host = BuildHost(tmp_path / "h")
    commit = git_catalog(host.catalogs / "sandbox-ai-mcp", [JADX, TIME])
    catalog = str(host.catalogs / "sandbox-ai-mcp" / "catalog.json")

    def runner(name, args, timeout=120, input=None):
        run = subprocess.run(host.command(*args), capture_output=True, text=True, env=host.env, timeout=timeout,
                             cwd=host.workspace, stdin=subprocess.DEVNULL)
        return run.returncode, run.stdout, run.stderr
    api = w.Api(runner)
    body = {"entry": "jadx", "catalogs": [catalog], "mounts": [{"target": "/work", "host": str(host.apps)}]}
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/local/mcp", body)
    assert refused.value.code == "mcp-unsigned-catalog"  # (a catalog of your own: not signed, asked as before)
    body.update(unsigned=True, confirm=True)
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/local/mcp", body)
    assert refused.value.code == "mcp-unpublished"
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/local/mcp", {**body, "build": True})
    asked = refused.value.params
    assert refused.value.code == "mcp-build-asked" and asked["commit"] == commit and host.build_calls() == []
    done = api.dispatch("POST", "/api/hosts/local/mcp", {**body, "build": True, "commit": asked["commit"],
                                                         "source_sha256": asked["archive_sha256"],
                                                         "confirm_build": True})
    assert "built here, not signed" in done["output"]
    assert host.definitions()["jadx"]["signed_by"] is None and host.definitions()["jadx"]["built"]["commit"] == commit


MCP_PAGE_DRIVER = tw.FAKE_DOM + r"""
const byId = (id) => body.all().find(n => n.id === id);
(async () => {
  const result = { sent: [] };
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.hosts = [{ name: "ct1", kind: "ssh" }];', context);
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  answer = (method, path, sent) => {
    if (method === "GET") return [];
    const body = JSON.parse(sent || "{}");
    result.sent.push(body);
    if (path.endsWith("/mcp") && !body.build) return { __status: 502, __error: "sandbox-ai mcp on ct1: jadx: the catalog has no digest for it yet (not published)", __code: "mcp-unpublished", __params: { host: "ct1", entry: "jadx" } };
    if (path.endsWith("/mcp") && !body.commit) return { __status: 400, __confirm: "mcp-build", __error: "jadx on ct1: built from ...", __code: "mcp-build-asked",
      __params: { host: "ct1", entry: "mcp:jadx", repository: "https://github.com/c0decave/sandbox-ai-mcp", commit: "1".repeat(40), path: "servers/jadx", archive_sha256: "a".repeat(64), catalog_label: "sandbox-ai-mcp (github.com/c0decave)", signature: "good" } };
    return { output: "sandbox-ai mcp: jadx added (localhost/sandbox-ai-mcp-jadx@sha256:..., built here, not signed)" };
  };
  vm.runInContext('state.page = "mcp";', context);
  await vm.runInContext("pages.mcp(__main)", context); await settle();
  for (const id of ["mcp-as", "mcp-env", "mcp-memory", "mcp-pids", "mcp-catalogs", "mcp-add-vpn", "mcp-network", "mcp-add-backend"]) if (byId(id) && byId(id).value === undefined) byId(id).value = "";
  byId("mcp-entry").value = "jadx"; byId("mcp-mounts").value = "/work=/srv/apps";
  button(main, process.argv[3] === "de" ? "Den Server hinzufügen" : "Add the server").fire("click"); await settle();
  result.question = main.textContent;
  const build = button(main, process.argv[3] === "de" ? "Bauen, unsigniert" : "Build it, unsigned");
  result.offered = Boolean(build);
  if (build) { build.fire("click"); await settle(); }
  result.toasts = toasts();
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="needs node")
@pytest.mark.parametrize("language", ["en", "de"])
def test_the_mcp_page_offers_the_build_and_asks_with_the_commit(tmp_path, language):
    driver = tmp_path / f"mcp_build_{language}.js"
    driver.write_text(MCP_PAGE_DRIVER)
    run = subprocess.run(["node", str(driver), str(tw.ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["offered"], result["question"]
    first, plan, built = result["sent"]
    assert first["entry"] == "jadx" and not first.get("build")
    assert plan["build"] is True and "commit" not in plan
    assert built["commit"] == "1" * 40 and built["source_sha256"] == "a" * 64 and built["confirm_build"] is True
    assert built["mounts"] == [{"target": "/work", "host": "/srv/apps", "mode": "ro"}]
    if language == "en":
        assert "Build it on ct1 from its reviewed source and add it UNSIGNED?" in result["question"]
        assert "1" * 40 in result["question"] and "a" * 64 in result["question"]
        assert any("built and added on ct1 (not signed)" in toast for toast in result["toasts"])
    else:
        assert "aus seinem geprüften Quelltext bauen und UNSIGNIERT hinzufügen?" in result["question"]
        assert any("gebaut und hinzugefügt (nicht signiert)" in toast for toast in result["toasts"])


RESUME_DRIVER = tw.FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
(async () => {
  const result = { sent: [] };
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "logs"; state.hosts = [{ name: "ct1", kind: "ssh" }];', context);
  let built = false;
  answer = (method, path, sent) => {
    if (method === "GET") return [];
    const body = JSON.parse(sent || "{}");
    result.sent.push([path, body]);
    if (path.endsWith("/resume") && !built) return { __status: 502, __error: "agent-sandbox: Unknown MCP server: jadx ...", __code: "mcp-unpublished-start",
      __params: { host: "ct1", names: "jadx", server0: JSON.stringify({ name: "jadx", unsigned: false, mounts: [{ target: "/work", host: "/srv/apps", mode: "ro" }] }) } };
    if (path.endsWith("/mcp") && !body.commit) return { __status: 400, __confirm: "mcp-build", __error: "asked", __code: "mcp-build-asked",
      __params: { host: "ct1", entry: "mcp:jadx", repository: "https://github.com/c0decave/sandbox-ai-mcp", commit: "1".repeat(40), path: "servers/jadx", archive_sha256: "a".repeat(64), catalog_label: "x", signature: "good" } };
    if (path.endsWith("/mcp")) { built = true; return { output: "built" }; }
    return { output: "resumed" };
  };
  vm.runInContext('moreCard(__main, { name: "web", id: "web-1", host: "ct1", state: "paused", keep: true })', context);
  for (const id of ["more-workspaces", "more-mounts", "more-mcp"]) if (byId(id)) byId(id).value = "";
  byId("more-mcp").value = "jadx";
  byId("more-restart").checked = true;
  button(main, "Restart with these…").fire("click");
  button(main, "Yes, resume it").fire("click"); await settle();
  result.question = main.textContent;
  button(main, "Build it, unsigned").fire("click"); await settle();
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="needs node")
def test_a_resume_with_such_a_server_offers_its_build_and_resumes_again(tmp_path):
    driver = tmp_path / "resume_build.js"
    driver.write_text(RESUME_DRIVER)
    run = subprocess.run(["node", str(driver), str(tw.ROOT / "sandbox/web/app.js"), "en"], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    paths = [path for path, _ in result["sent"]]
    assert paths == ["/api/hosts/ct1/starts/web/resume", "/api/hosts/ct1/mcp", "/api/hosts/ct1/mcp",
                     "/api/hosts/ct1/starts/web/resume"], paths
    assert "Its folders: /work=/srv/apps:ro." in result["question"] and "1" * 40 in result["question"]
    build = result["sent"][2][1]
    assert build["entry"] == "jadx" and build["confirm_build"] is True and build["commit"] == "1" * 40
    assert build["mounts"] == [{"target": "/work", "host": "/srv/apps", "mode": "ro"}]
