"""Review round 6 of the app: host choosers follow a hosts file changed outside the app (M-H2), a (?) never covers a
column title (L2), a paused run on this machine is resumed (A-N1), a stopped session's MCP servers can be taken out
(A-N2), remote starts get --workspaces-as-given from the host's known version (A-N3), a damaged team is shown and
deleted (A-N4), Add marketplace offers no folder (N2), Test connection sends the CA (N3), another host's web
interface is reached through an SSH tunnel (N4), Update from here keeps the installation's prefix."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app import wizard_llm as WL  # noqa: E402

from test_projects import close, make_window  # noqa: E402

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def wait_for(qapp, condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


def choices(widget):
    combo = getattr(widget, "box", widget)
    return [combo.itemData(i) for i in range(combo.count())]


# -- M-H2: hosts changed with sandbox-ai hosts (or the web interface) while the window is open --


@needs_sai
def test_host_choosers_follow_a_hosts_file_changed_outside_the_app(qapp, isolated, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts")
    hosts = sai.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"}),
                "ct2": hosts.check("ct2", {"ssh": "admin@192.0.2.11"})})
    window = make_window(monkeypatch)
    try:
        page = window
        choosers = {"activity": page.activity.hosts, "statistics": page.statistics.hosts, "images": page.images.hosts,
                    "kits": page.kits.hosts, "launch": page.launch.host_choice}
        for name, chooser in choosers.items():
            assert choices(chooser) == ["local", "ct1", "ct2"], (name, choices(chooser))
        # What `sandbox-ai hosts add ct3 ...; hosts remove ct2` writes (os.replace, as the CLI does): no page asked.
        hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"}),
                    "ct3": hosts.check("ct3", {"ssh": "admin@192.0.2.12"})})
        assert wait_for(qapp, lambda: all(choices(c) == ["local", "ct1", "ct3"] for c in choosers.values())), \
            {name: choices(c) for name, c in choosers.items()}
        assert wait_for(qapp, lambda: sorted(window.hosts.table.row_keys) == ["ct1", "ct3", "local"]), \
            "the Hosts page's own list follows too"
        # A file that appears later (no hosts yet at the start) is seen as well.
        hosts.config_file().unlink()
        assert wait_for(qapp, lambda: choices(choosers["kits"]) == ["local"])
        hosts.save({"ct4": hosts.check("ct4", {"ssh": "admin@192.0.2.13"})})
        assert wait_for(qapp, lambda: choices(choosers["kits"]) == ["local", "ct4"])
    finally:
        close(window)


# -- L2: the (?) of a table never covers a column's title, also when the table scrolls sideways --


@needs_sai
def test_a_tables_help_button_covers_no_column_title(qapp, isolated, monkeypatch):
    from sandbox_ai_app import help_view
    sai = backend.SandboxAI()
    if sai.hosts_supported():
        hosts = sai.module("hosts")
        hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"})})
    window = make_window(monkeypatch)
    try:
        window.resize(1100, 760)
        window.show()
        found = 0
        for key in ("hosts", "files", "sandboxes"):
            window.show_page(key, raise_window=False)
            for _ in range(5):
                qapp.processEvents()
            for button in getattr(window, key).findChildren(help_view.HelpButton):
                corner = getattr(button, "corner", None)
                if corner is None or corner.header is None or not button.isVisible():
                    continue
                header = corner.header
                end = header.mapTo(corner.target, header.viewport().geometry().topRight()).x()
                assert button.x() > end, (key, corner.target.accessibleName(), button.x(), end)
                found += 1
        assert found >= 2
    finally:
        close(window)


# -- A-N1: a run without a kept start, paused on this machine --


def test_a_paused_run_reads_as_paused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    assert backend.run_state({"directory": str(run)}) == "running"
    (run / "paused").touch()
    assert backend.run_state({"directory": str(run)}) == "paused"
    assert backend.run_state({}) == "running" and backend.run_state(None) == "running"


@needs_sai
def test_a_paused_run_is_offered_resume_not_what_its_supervisor_would_refuse(qapp, isolated, monkeypatch):
    sai = backend.SandboxAI()
    run = isolated / "run-dir"
    run.mkdir()
    (run / "paused").touch()
    record = {"run": "claude-1234abcd", "agent": "claude", "backend": "podman", "workspace": "/srv/w",
              "directory": str(run), "pid": os.getpid(), "started": "2026-11-09T10:00:00Z"}
    monkeypatch.setattr(sai, "runs", lambda: {"claude-1234abcd": record})
    monkeypatch.setattr(sai, "sessions", lambda: [])
    found, _ = sai.instances(proc=isolated / "no-proc")
    paused = next(i for i in found if i.run == "claude-1234abcd")
    assert paused.kind == "run" and paused.state == "paused"
    window = make_window(monkeypatch)
    try:
        page = window.sandboxes
        monkeypatch.setattr(page, "current", lambda: paused)
        page.update_buttons()
        assert page.session_buttons["resume"].isEnabled() and not page.session_buttons["pause"].isEnabled()
        for key in ("allow", "add-workspace"):
            assert not page.run_named[key].isEnabled(), key
            assert "paused" in page.run_named[key].toolTip()
    finally:
        close(window)


# -- A-N2: Remove MCP server… of a stopped session on this machine --


@needs_sai
def test_a_stopped_sessions_mcp_servers_are_offered_for_removal(qapp, isolated, monkeypatch):
    from sandbox_ai_app import start_tree
    sai = backend.SandboxAI()
    sessions = sai.module("sessions")
    directory = sessions.session_dir("web")
    for path in (directory, directory / "home"):
        path.mkdir(parents=True, exist_ok=True)
    sessions.save("web", {"agent": "claude", "options": ["--mcp", "files", "--mcp=search", "-M", "files"],
                          "agent_args": [], "backend": "podman", "workspace": "/srv/app", "cwd": "/",
                          "created": "2026-11-01T10:00:00Z", "pod": "agent-session-web"})
    found, problems = sai.instances(proc=isolated / "no-proc")
    web = next(i for i in found if i.kind == "session")
    assert web.state == "stopped" and web.record.get("mcp") == ["files", "search"], (web.record, problems)
    assert start_tree.reached(web) == [("files", False), ("search", False)]
    assert backend.mcp_option_names(["--mcp", "-x", "--mcp=a b", "-M"]) == []


# -- A-N3: a start on another host gets --workspaces-as-given from the host's known version --


@needs_sai
def test_a_remote_start_takes_the_hosts_known_version(isolated, monkeypatch):
    sai = backend.SandboxAI()
    hosts = sai.module("hosts")
    if not hasattr(hosts, "ask_version"):
        pytest.skip("this sandbox-ai cannot ask a host's version")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"}),
                "old": hosts.check("old", {"ssh": "admin@192.0.2.11"})})
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, *args])
    asked = []

    def host_run(name, *args, timeout=120, input=None):
        asked.append((name, args))
        if args == ("version", "--verbose"):
            return 0, {"ct1": "sandbox-ai 0.1.83\n", "old": "sandbox-ai 0.1.77\n"}[name], ""
        return 0, "  --workspace DIR\n", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    # Not known yet: the window's thread asks nobody, so the start goes as it is.
    assert "--workspaces-as-given" not in sai.host_launch_argv("ct1", "claude", ["--workspace", "/srv/x"])
    assert asked == []
    # The options read in a worker (when the host is chosen) learn its version on the way.
    sai.host_launch_options("ct1", "claude")
    sai.host_launch_options("old", "claude")
    assert ("ct1", ("version", "--verbose")) in asked
    assert sai.host_launch_argv("ct1", "claude", ["--workspace", "/srv/x"])[-1] == "--workspaces-as-given"
    assert "--workspaces-as-given" not in sai.host_launch_argv("old", "claude", ["--workspace", "/srv/x"])
    assert "--workspaces-as-given" not in sai.host_launch_argv("ct1", "claude", ["--mode", "high"])
    # The option is hidden from --help: the version alone tells (as the web asks it).
    assert sai.host_knows_as_given("ct1") and not sai.host_knows_as_given("old")


# -- A-N4: a team whose record is damaged --


@needs_sai
def test_a_damaged_team_is_listed_and_only_deleted(qapp, isolated, monkeypatch):
    sai = backend.SandboxAI()
    listing = ("review               claude, codex            /srv/project\n"
               "bad                  (damaged)                remove it with: sandbox-ai team delete bad --yes\n")
    monkeypatch.setattr(sai, "host_run", lambda host, *args, timeout=120, input=None: (0, listing, ""))
    teams = sai.teams("local")
    assert teams == [{"name": "review", "members": ["claude", "codex"], "workspace": "/srv/project"},
                     {"name": "bad", "members": [], "workspace": "", "damaged": True}]
    window = make_window(monkeypatch)
    try:
        page = window.team
        page.listed(page.host(), page.token, teams, None)
        assert sorted(page.table.row_keys) == ["bad", "review"]
        page.table.selectRow(widgets.find_row(page.table, "bad"))
        qapp.processEvents()
        assert page.delete_button.isEnabled()
        for button in (page.attach_button, page.post_button, page.stop_button):
            assert not button.isEnabled() and "damaged" in button.toolTip()
        assert "damaged" in page.where.text() and "bad-claude" in page.where.text()
        deleted = []
        monkeypatch.setattr(page.sai, "team_delete", lambda host, name: deleted.append(name) or (0, "", ""))
        page.delete()
        assert wait_for(qapp, lambda: deleted == ["bad"])
        assert "damaged" in widgets.SHOWN[-1][1] or any("damaged" in str(s) for s in widgets.SHOWN)
    finally:
        close(window)


# -- N2: a marketplace's source is never a folder (sandbox-ai refuses one) --


def test_add_marketplace_offers_no_folder(qapp):
    from sandbox_ai_app import kits_page
    dialog = kits_page.MarketplaceDialog(None)
    assert not [b for b in dialog.findChildren(QtWidgets.QPushButton) if b.text().startswith("Folder")]
    assert "new-skill" not in kits_page.WIDENS, "a template skill is written without a question, as in the web"
    dialog.deleteLater()


# -- N3: Test connection sends the server's CA --


def test_test_connection_sends_the_ca():
    argv = WL.probe_argv("vllm", "https://gw.example.org", ca="/etc/ssl/gw-ca.pem")
    assert argv[argv.index("--ca") + 1] == "/etc/ssl/gw-ca.pem" and "--ca-of" not in argv
    argv = WL.probe_argv("vllm", "https://gw.example.org", ca_of="gw")
    assert argv[argv.index("--ca-of") + 1] == "gw"
    assert "--ca" not in WL.probe_argv("vllm", "http://gw.example.org", ca="/x.pem"), "only for https"
    for bad in ({"ca": "--json"}, {"ca_of": "-x"}):
        with pytest.raises(ValueError):
            WL.probe_argv("vllm", "https://gw.example.org", **bad)
    assert WL.tls_untrusted("Not reachable: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert not WL.tls_untrusted("connection refused")


# -- N4: another host's web interface, opened here --


@needs_sai
def test_another_hosts_web_interface_is_forwarded_through_ssh(isolated):
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts")
    hosts = sai.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10", "port": 2222})})
    argv, port = sai.host_web_tunnel_argv("ct1", None, local_port=18443)
    assert port == 18443 and argv[0] == "ssh" and argv[-2:] == ["--", "admin@192.0.2.10"]
    assert argv[argv.index("-L") + 1] == "127.0.0.1:18443:127.0.0.1:8443" and "-N" in argv
    assert argv[argv.index("-p") + 1] == "2222" and "BatchMode=yes" in argv
    assert sai.host_web_tunnel_argv("ct1", "[::1]:9000", local_port=19000)[0][argv.index("-L") + 1] == \
        "127.0.0.1:19000:[::1]:9000"
    with pytest.raises(ValueError):
        sai.host_web_tunnel_argv("local")
    assert backend.web_service_url("admin@ct1.lan", "0.0.0.0:8443") == "https://ct1.lan:8443/"
    assert backend.web_service_url("admin@ct1.lan", "192.0.2.10:8443") == "https://192.0.2.10:8443/"
    assert backend.web_service_url(None, "127.0.0.1:8443", local=True) == "https://localhost:8443/"
    assert backend.web_service_url("u@h", "[::]:8443") == "https://h:8443/"


@needs_sai
def test_run_the_service_on_another_host_says_where_to_open_it(qapp, isolated, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts")
    hosts = sai.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"})})
    window = make_window(monkeypatch)
    try:
        panel = window.hosts.web
        opened = []
        panel.in_terminal = lambda argv, title: opened.append((title, argv))
        panel.set_host("ct1")
        panel.serve()
        titles = [title for title, _ in opened]
        assert titles == ["ct1: web serve", "ct1: web tunnel"], titles
        tunnel = opened[1][1]
        assert tunnel[tunnel.index("-L") + 1].endswith(":127.0.0.1:8443")
        assert panel.service_url.startswith("https://localhost:") and not panel.open_button.isHidden()
        assert "web tunnel" in panel.status.text() and panel.service_url in panel.status.text()
        assert not [s for s in widgets.SHOWN if "Other machines" in str(s)], "the loopback: nothing to ask"
        panel.set_host("local")
        assert panel.service_url is None and panel.open_button.isHidden()
    finally:
        close(window)


# -- Update from here, sudo asking for its password: the prefix the update found, not the cache's --


@needs_sai
def test_a_from_here_update_in_a_terminal_keeps_the_installations_prefix(qapp, isolated, monkeypatch):
    window = make_window(monkeypatch)
    try:
        page = window.hosts
        calls = []
        monkeypatch.setattr(page.sai, "host_install_terminal",
                            lambda name, mode, prefix, sudo, source: calls.append((name, mode, prefix)) or ["x"])
        page.in_terminal = lambda argv, title: None
        page.versions = {}  # nothing cached about ct1
        page.queue, page.results, page.queue_from_here = ["ct1"], [], True
        monkeypatch.setattr(page, "next_update", lambda sudo=None: None)
        page.updated("ct1", {"name": "ct1", "needs_password": True, "kind": "system", "mode": "system",
                             "prefix": "/opt/sai", "error": "sudo wants a password"}, None)
        assert calls == [("ct1", "system", "/opt/sai")]
    finally:
        close(window)


# -- W-F2 (app): mcp detach on a host whose sandbox-ai is older says "update it", not argparse's usage --


@needs_sai
def test_mcp_detach_on_an_older_host_says_update_it(isolated, monkeypatch):
    sai = backend.SandboxAI()
    usage = ("usage: sandbox-ai mcp [-h] {list,start,stop,attach} ...\nsandbox-ai mcp: error: argument command: "
             "invalid choice: 'detach' (choose from 'list', 'start', 'stop', 'attach')")
    monkeypatch.setattr(sai, "host_run", lambda host, *args, timeout=120, input=None: (2, "", usage))
    with pytest.raises(backend.HostTooOld) as refused:
        sai.host_mcp_detach("ct1", "web", "files")
    assert "Update the host" in str(refused.value) and "detach" in str(refused.value)
    monkeypatch.setattr(sai, "host_run", lambda host, *args, timeout=120, input=None:
                        (1, "", "sandbox-mcp: web is not kept\nsandbox-mcp: see sandbox-ai list"))
    with pytest.raises(ValueError) as failed:
        sai.host_mcp_detach("local", "web", "files")
    assert str(failed.value) == "web is not kept\nsee sandbox-ai list", "lines, not \\x0a"
