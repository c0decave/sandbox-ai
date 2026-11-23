"""The pages call only what backend.py defines (no Qt needed): every `backend.NAME`, `from .backend import
NAME`, `sai.NAME` and `self.sai.NAME` in the package must exist in backend.py, as a module-level name or an
attribute of SandboxAI. A merge once left pages calling backend.SECRET_NAME and sai.load_project, which no
longer existed; the Qt tests that would notice cannot run everywhere."""
import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"


def module_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for target in node.targets for t in ast.walk(target) if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
    return names


def class_attributes(tree, name):
    """Methods, properties, class attributes and every self.X assigned in the methods of class NAME."""
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    names = set()
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    for node in ast.walk(cls):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
            node, (ast.AnnAssign, ast.AugAssign)) else []
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                names.add(target.attr)
    return names


def is_sai(node):
    """sai or self.sai (how the pages hold their SandboxAI)."""
    return (isinstance(node, ast.Name) and node.id == "sai") or (
        isinstance(node, ast.Attribute) and node.attr == "sai" and isinstance(node.value, ast.Name)
        and node.value.id == "self")


def references():
    """(file, line, "backend" or "sai", name) of every use in the package outside backend.py."""
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "backend.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "backend":
                    yield path.name, node.lineno, "backend", node.attr
                elif is_sai(node.value):
                    yield path.name, node.lineno, "sai", node.attr
            elif isinstance(node, ast.ImportFrom) and node.module == "backend" and node.level == 1:
                for alias in node.names:
                    yield path.name, node.lineno, "backend", alias.name


def test_pages_use_only_what_backend_defines():
    tree = ast.parse((PACKAGE / "backend.py").read_text())
    known = {"backend": module_names(tree), "sai": class_attributes(tree, "SandboxAI")}
    found = list(references())
    assert any(kind == "sai" for *_, kind, _ in found) and any(kind == "backend" for *_, kind, _ in found)
    missing = [f"{file}:{line} {kind}.{name}" for file, line, kind, name in found if name not in known[kind]]
    assert not missing, "not defined in backend.py:\n" + "\n".join(missing)


def test_the_check_catches_a_missing_name(tmp_path):
    tree = ast.parse("class SandboxAI:\n    def answer(self): pass\nAGENTS = ()\n")
    assert "answer" in class_attributes(tree, "SandboxAI") and "load_project" not in class_attributes(tree, "SandboxAI")
    assert "AGENTS" in module_names(tree) and "SECRET_NAME" not in module_names(tree)


# -- nothing slow on the GUI thread --

# SandboxAI methods that run a command, ask the keyring or read big logs (the window must keep polling the
# approvals meanwhile, or questions are refused after a minute). Declared here; test_every_slow_backend_method_is_declared
# finds every method of backend.py that runs a process, sleeps or calls one of these, and wants it here too.
BLOCKING_SAI = {"host_toolchains", "toolchains_of_run", "toolchain_add", "toolchain_attach", "toolchain_detach",
                "host_fix_plan",  # `AGENT ... --fix-plan` on the start's host (the prerequisites' background read)
                "call", "_call", "bridges", "bridge_json", "bridge_plan", "bridge_add", "bridge_show", "bridge_revoke",
                "host_bridges", "bridge_resume", "answer", "dry_run", "doctor", "mcp_catalog", "statistics", "set_secret",
                "mcp_tool_budget",  # mcp_budget.start_report: the profiles, the definitions, the pins read
                "shared_wildcards",  # the Public Suffix List read (sandbox-ai's runtime) at the first call
                "delete_secret", "secrets", "save_token", "login_status", "remembered", "forget_remembered", "question_context",
                "run_log", "devices_added", "device_check", "device_candidates", "add_device", "remove_device",
                "device_scan", "devices_listed",  # sandbox-ai devices scan|list --json: a process, also over SSH
                "set_limits", "workspaces", "add_workspace", "remove_workspace",
                "safeguards",  # sandbox-ai safeguards --json: a process, also over SSH
                # sandbox-ai update mode, --check, --apply, --rollback (Settings › Updates, the Hosts page)
                "update_status", "set_update_mode", "update_check_now", "update_apply", "update_install",
                "update_rollback",
                "report",  # sandbox-ai report: reads the logs bounded, writes the file (seconds)
                "host_report",  # sandbox-ai report on a host, over SSH
                "user_images", "variant_bases", "service_templates",  # sandbox-ai images list --json: a process, also over SSH
                "host_version_known",  # one short SSH command (review 6 A-N3)
                # a start under a session's name, and its agent's login, on this machine or a host (over SSH)
                "session_seen", "session_stop", "host_logins", "host_logged_in",
                # sandbox-ai notify on this machine or a host (over SSH): the phone channels
                "notify_channels", "notify_add", "notify_remove", "notify_test", "notify_link",
                # sandbox-ai schedule on this machine or a host (over SSH): the scheduled runs
                "schedules", "schedule_history", "schedule_report", "schedule_add", "schedule_action",
                "schedule_timer",
                "write_project",
                # over SSH to the hosts of `sandbox-ai hosts` (seconds; a host that does not answer: up to the timeout)
                "host_run", "host_json", "host_instances", "host_questions", "host_test", "host_update",
                "host_file_roots",  # sandbox-ai list --folders --json on a host: the Files page's remote sandboxes
                "limits_shown", "review_home", "host_remembered", "host_net_options", "ended_starts", "start_history",
                "restore_home", "rules_check", "rules_show", "rules_reload",
                "collect", "host_set", "host_add", "host_remove", "collect_as_shown", "state_usage", "home_details",
                "host_devices_added",
                "host_workspaces", "host_secrets", "secret_exists", "host_approvals_history", "session_ids",
                "recent_folders", "host_install", "host_uninstall", "host_uninstall_plan",
                # starts, MCP servers, net keep, the privacy check, version and demo device on a host (or here)
                "host_launch_options", "host_dry_run", "host_mcp_servers", "host_mcp_list", "host_mcp_catalog", "host_mcp_targets",
                "host_mcp_attach", "net_keep", "privacy_check", "version_details", "host_doctor", "demo_device",
                # MCP servers of another host copied to one (sandbox-ai mcp copy here, over SSH to both), and the
                # servers of every other host it offers (mcp list --json on each)
                "host_mcp_copy", "host_mcp_copy_check", "host_mcp_elsewhere", "host_mcp_update",
                # a board or session as one file, and such a file taken in (sandbox-ai board export|import; with
                # images: minutes)
                "board_export", "board_import",
                # a start's workspace checkpoints (sandbox-ai checkpoint, also on a host: a first one copies every
                # file of the workspaces, a restore writes them)
                "checkpoints", "checkpoint_create", "checkpoint_diff", "checkpoint_preview", "checkpoint_restore",
                "checkpoint_delete", "checkpoint_json",
                # the workspace review and the MCP tool pins (sandbox-ai workspace review, mcp tools; also on a host)
                "workspace_reviews", "workspace_review_decide", "mcp_tool_pins", "mcp_tool_pin", "mcp_pin_change",
                # a start's agent memory and session files, its folder as an archive (sandbox-ai session, also on a host)
                "session_memory", "session_files", "session_export", "file_view_json", "session_logins",
                # the storage check and an item of its plan (sandbox-ai gc --check, gc --only ...; also on a host)
                "storage_check", "storage_apply",
                "import_session_login",
                "mcp_servers", "mcp_serving", "mcp_attach", "mcp_attach_targets",
                # the parity section: the Manage pages, Activity, Statistics and Teams on every host
                "host_listing", "host_vpn_profiles", "host_git_logins", "host_publish_logins", "host_identities",
                "host_save_identity", "host_login_status", "host_kits_inventory", "host_kit_command", "revoke_hooks",
                # what the agents on a host have of their own (kits host-list, host-show, host-import)
                "host_agent_parts", "host_agent_part", "host_import_parts", "host_import_memory",
                "plugins_status", "host_plugins", "host_canary_log", "canary_runs", "canary_run", "canary_resume", "host_preset_dirs",
                "host_preset_dir_command", "trusted_projects", "forget_trusted", "host_activity", "host_statistics",
                "teams", "team_show", "team_check", "team_post", "team_stop", "team_delete", "kits_inventory", "kit_command",
                "canary_log", "preset_dirs", "preset_dir_command", "identities", "save_identity", "git_logins",
                "publish_logins", "vpn_check", "vpn_remove", "vpn_add",
                # `sandbox-ai llm list` and `vpn list` (the wizard's Model page, the preset form, the VPN page)
                "llm_endpoints", "vpn_profiles", "listing",
                # MCP servers into a running start and out (--live), a host's model servers and connection logs
                "host_mcp_attach_live", "host_mcp_detach_live", "mcp_live",
                "host_kits_attach", "host_kits_detach", "host_agent_restart", "kits_live", "host_running_starts", "host_llm_endpoints", "host_llm_models",
                "host_llm_servers", "host_connections", "host_log_summary",
                # every plugin marketplace of a host (the Kits page's browser) and its version check over SSH
                "marketplaces", "host_marketplaces", "require_marketplaces",
                # a host's presets, start choices, project files and trust, the web interface's logins and
                # certificate, an answer sent there, MCP servers stopped or detached, starts stopped, secrets' backend,
                # a login added, and the known-options check (`COMMAND --help` there) under each of them
                "host_preset", "host_presets", "host_start_choices", "host_project_show", "host_project_trust",
                "host_trust_defaults", "host_web_users", "host_web_user_remove", "host_web_cert", "host_answer",
                "host_answer_relaying",
                "host_mcp_detach", "stop_mcp", "stop_run", "secrets_backend", "add_login", "checked",
                # the primary password of a host's secrets: sandbox-ai secrets status|unlock|lock|password set there
                "secrets_protection", "secrets_unlock", "secrets_lock", "secrets_new_password",
                "cut_run", "cut_everywhere", "cut_every_host",  # the kill switch: sandbox-ai net cut, here or over SSH
                "host_command_options", "require_options", "host_image_variants", "host_agent_images",
                # sandbox-ai's registry of what runs (net.running) asks Podman whether the pod of a run whose launcher
                # is gone still runs (up to its timeout) and removes the directories of runs that ended; the sessions'
                # rows ask Podman about stopped ones: so does everything that reads them
                "runs", "sessions", "instances", "run_for", "file_roots", "known_projects", "sandbox_folders", "sandbox_paths",
                "rw_mounts", "autostart_risk",
                # a session to another host: sandbox-ai session copy|move (a dry run and a start over SSH, minutes),
                # and its plan (the session's files and its project file, read in the session's directory)
                "session_copy", "session_move", "session_transfer_plan",
                # a session's state: duplicated, imported (an archive read and checked, the launcher's dry run)
                "session_duplicate", "session_import", "session_state_json"}
# (approvals_snapshot and questions read the registry with live_runs: files and /proc only, never Podman.)
# (host_statuses(cached=True) reads what `sandbox-ai hosts list` kept, without SSH: the Hosts page lists it at once.)
# Slow, but on the GUI thread on purpose (each call site says so with a comment "on the GUI thread on purpose"):
# stop_commands only when quitting, bounded by one grace period; end_groups is its part.
ON_PURPOSE = {"stop_commands", "end_groups"}
ON_PURPOSE_MARK = "on the GUI thread on purpose"
# The only functions whose lines may carry the mark (module, function): the app's start before it has a window (the
# single instance's bounded probes) and its end. Nothing a page does while the window is shown - round 1 left the
# Start's `podman image exists` marked so (prerequisites.agent_images_built, up to 5 s); round 2 moved it into the
# background (look_at_agent_image) and took it off this list.
ON_PURPOSE_SITES = {("single_instance.py", "ask"), ("single_instance.py", "tell"), ("single_instance.py", "meet"),
                    ("single_instance.py", "claim"), ("instance_probe.py", "command_line"), ("ui.py", "stop_work")}
BLOCKING_MODULES = {"subprocess"}
BLOCKING_TIME = {"sleep"}  # time.sleep: never in a handler (a QTimer waits instead)
# QProcess's waits (and QThread.wait) block the event loop until the process (thread) is done: AsyncCommand's signals
# instead. Matched by the name alone.
BLOCKING_WAITS = {"waitForFinished", "waitForStarted", "waitForReadyRead", "waitForBytesWritten"}
BLOCKING_BACKEND = {"read_connections"}
# files.py: copies (up to 1 GiB), moves, deletes and owner changes of whole trees, podman unshare (the Files page
# runs them in Task.thread). Called as root.NAME(...) or files.NAME(...): matched by the name alone.
BLOCKING_FILES = {"copy_out", "copy_in", "copy_entry", "move_entry", "delete_entry", "chown_entry", "take_ownership",
                  "read_file", "write_file", "measure",  # (the editor reads and writes whole files; Properties counts)
                  # files.exists_in and entry_infos: a remote root's are requests to its server (SSH: it may stall)
                  "exists_in", "entry_infos"}
# A Root's single steps, called as ROOT.NAME (root, self.root, chosen.root, ...): fast for a local Root, a request to a
# remote system's server for a remote.RemoteRoot (SSH: it may stall) - the Files page hands them to root_job or on_root.
BLOCKING_ROOT = {"rename_entry", "make_folder", "make_file", "chmod_entry", "stat_entry", "readlink", "free_space",
                 "list", "lstat", "read", "exists"}
# wizard_llm's functions that run `sandbox-ai llm list|add|check|models|detect` (called as WL.NAME or
# wizard_llm.NAME), and
# presets' host_documents (`sandbox-ai presets list --json` on a host: PR.NAME or presets.NAME).
BLOCKING_HELPERS = {"WL": {"list_endpoints", "add_and_check", "list_models", "detect"},
                    "wizard_llm": {"list_endpoints", "add_and_check", "list_models", "detect"},
                    "PR": {"host_documents"}, "presets": {"host_documents"},
                    # chromium.check: a child process tries Chromium's namespaces (up to its timeout)
                    "chromium": {"check", "probe"},
                    # name_fields.read_names: a host's secrets or VPN profiles (`secrets list`, `vpn list` there)
                    "name_fields": {"read_names"}, "NF": {"read_names"},
                    # device_picker.scan: a host's devices (`devices scan --json` there, over SSH for a host)
                    "device_picker": {"scan"}, "DP": {"scan"},
                    # remote.py: connecting over SSH (ssh, a handshake), reading a host's keys (ssh-keyscan)
                    "remote": {"RemoteRoot", "scan_host", "fingerprints", "Sftp",
                               # a host's folders looked at and made over SFTP (an MCP server's, Add… on a host)
                               "folder_states", "make_folders"}}
# widgets.background, ThreadCall, Task.thread and call_watched; the Kits page's start_change (its Task.call_watched);
# the Files page's root_job, on_root (at once for a local root, in a thread - Task with Cancel / background - for a
# remote one) and run_job (its Task.thread, the remote roots named for Cancel). Each with the position of its work
# (what runs in the thread): the other arguments - done, the text - are the GUI thread's (round 2: a done= lambda and
# a Task's done counted as work, and what they called slowly was never looked at).
WRAPPERS = {"background": 0, "ThreadCall": 0, "thread": 0, "call_watched": 1, "start_change": 0, "run_each": 0,
            "root_job": 1, "on_root": 1, "run_job": 1,
            "job": 1}  # (file_properties: the Files page's root_job handed in as job=)
WORK_KEYWORDS = {"function", "work"}  # (background's work=True is a flag: only a callable there is work)
PARTIALS = {"partial"}
# Functions of the pages that return a slow callable (functools.partial(sai.host_run, host)) for a dialog of another
# module, which runs it in its own background(): llm_page.LlmDialog's Test connection, llm_loaded_view.LoadedDialog.
HANDED_ON = {("llm_page.py", "runner"), ("llm_page.py", "requirement")}
PROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}  # functools.partial(F, ...): F runs where the partial is called
# The modules not checked call by call: backend.py (what is slow is declared above), widgets.py (background, ThreadCall
# and Task themselves; the waits of the app's end), and the helpers whose slow functions the pages may only hand to a
# wrapper (files.py, wizard_llm.py, presets.py, name_fields.py, chromium.py: BLOCKING_FILES, BLOCKING_HELPERS) or that
# start a terminal's command (pty_bridge.py: a fork and exec, read with a QSocketNotifier).
HELPER_MODULES = {"backend.py", "widgets.py", "files.py", "wizard_llm.py", "presets.py", "pty_bridge.py",
                  "name_fields.py", "chromium.py",
                  "device_picker.py",  # (its scan reads a host's devices: in the pages' threads only, BLOCKING_HELPERS)
                  # remote.py itself runs ssh (in the Files page's threads only); what the pages call of it - a
                  # RemoteRoot's steps, connecting, ssh-keyscan - is checked as files.py's are (BLOCKING_FILES,
                  # BLOCKING_HELPERS["remote"])
                  "remote.py",
                  "crashguard.py"}  # (the kernel's log is read in a thread of its own: Guard(wait=False))


def call_name(node):
    return node.func.id if isinstance(node.func, ast.Name) else \
        node.func.attr if isinstance(node.func, ast.Attribute) else None


def holds_root(node):
    """root, self.root, chosen.root, where.root, ...: a Root of the Files page (local or remote)."""
    return (isinstance(node, ast.Name) and node.id in ("root", "src_root", "dst_root", "target_root")) or \
        (isinstance(node, ast.Attribute) and node.attr in ("root", "src_root", "dst_root", "target_root"))


def holds_sai(node):
    """sai, self.sai, or any X.sai (self.page.sai, self.wizard_.sai, self.parent().sai): a page's SandboxAI."""
    return (isinstance(node, ast.Name) and node.id == "sai") or (isinstance(node, ast.Attribute) and node.attr == "sai")


def blocking(node):
    """A description if NODE (a Call) blocks, else None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if holds_sai(func.value) and func.attr in BLOCKING_SAI:
            return f"sai.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id in BLOCKING_MODULES:
            return f"{func.value.id}.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id == "time" and func.attr in BLOCKING_TIME:
            return f"time.{func.attr}()"
        if func.attr in BLOCKING_WAITS:
            return f"{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id == "backend" and func.attr in BLOCKING_BACKEND:
            return f"backend.{func.attr}()"
        if func.attr in BLOCKING_FILES:
            return f"{func.attr}()"
        if func.attr in BLOCKING_ROOT and holds_root(func.value):
            return f"root.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.attr in BLOCKING_HELPERS.get(func.value.id, ()):
            return f"{func.value.id}.{func.attr}()"
    if isinstance(func, ast.Name) and func.id in BLOCKING_BACKEND:
        return f"{func.id}()"
    return None


def work_of(call):
    """The node CALL (a wrapper's call) runs in its thread: its argument at the wrapper's work position, or a WORK_KEYWORDS
    keyword's value that is not a constant; None if CALL is no wrapper's."""
    if not isinstance(call, ast.Call) or call_name(call) not in WRAPPERS:
        return []
    found = [call.args[WRAPPERS[call_name(call)]]] if len(call.args) > WRAPPERS[call_name(call)] else []
    return found + [k.value for k in call.keywords if k.arg in WORK_KEYWORDS and not isinstance(k.value, ast.Constant)]


def at_work(node, parents):
    """True if NODE is a wrapper's work, or the function a functools.partial at a wrapper's work wraps."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword):
        parent = parents.get(parent)
    if any(node is work for work in work_of(parent)):
        return True
    return isinstance(parent, ast.Call) and call_name(parent) in PARTIALS and parent.args and parent.args[0] is node \
        and at_work(parent, parents)


def self_attributes(tree):
    """{NAME: [the self.NAME nodes of the module]} - every reference, a call's or a callback's."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            found.setdefault(node.attr, []).append(node)
    return found


def handed_methods(tree, parents=None):
    """The names of methods handed to a wrapper as its work (background(self.scan, ...)) and never referenced
    otherwise in the module - neither called (self.NAME(...)) nor passed elsewhere (connect(self.NAME), done=self.NAME):
    their body runs in the wrapper's thread."""
    parents = parents or {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    return {name for name, nodes in self_attributes(tree).items() if all(at_work(node, parents) for node in nodes)}


def deferred(node, parents, handed=frozenset(), strict=False):
    """True if NODE lies in what a wrapper runs in its thread: a lambda (or a functools.partial) that is its work, a
    nested function handed to one as its work by its name and not referenced otherwise in the function it is defined
    in, or a method that is so (HANDED: handed_methods, called_only_deferred). A wrapper's answer (done=, a Task's
    done) is the GUI thread's. (STRICT: kept for the callers; the same now.)"""
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(child, (ast.Lambda, ast.Call)) and at_work(child, parents) and \
                (isinstance(child, ast.Lambda) or call_name(child) in PARTIALS):
            return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A nested function handed to a wrapper by its name in the function it is defined in (also inside an
            # if, a with or a loop of that function) - and nowhere else (connect(read): that run is the GUI thread's)
            outer = parent
            while outer is not None and not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                outer = parents.get(outer)
            if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                uses = [n for n in ast.walk(outer) if isinstance(n, ast.Name) and n.id == child.name]
                if uses and all(at_work(n, parents) for n in uses):
                    return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(parent, ast.ClassDef) and \
                child.name in handed:
            return True
        child, parent = parent, parents.get(parent)
    return False


def called_only_deferred(tree, parents, handed):
    """HANDED and the methods of the module referenced (self.NAME) only where that is deferred itself - called in a
    nested function handed to a wrapper, a lambda handed to one, or a method that is so (repeated until nothing is
    added): their bodies run in the wrapper's thread too. A method called once on the GUI thread, or passed as a
    callback anywhere (connect(self.NAME), done=self.NAME), is not."""
    references = self_attributes(tree)
    methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
               for node in node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    work = set(handed)
    while True:
        more = {name for name in methods - work if references.get(name) and all(
            at_work(node, parents) or (isinstance(parents.get(node), ast.Call) and parents[node].func is node
                                       and deferred(node, parents, frozenset(work))) for node in references[name])}
        if not more:
            return frozenset(work)
        work |= more


def passed(node):
    """A description if NODE (an Attribute or Name, not called here) is something slow handed on as a callable -
    connect(self.sai.doctor), QTimer.singleShot(0, partial(subprocess.run, ...)) - else None."""
    if isinstance(node, ast.Attribute):
        if holds_sai(node.value) and node.attr in BLOCKING_SAI:
            return f"sai.{node.attr} (passed)"
        if isinstance(node.value, ast.Name) and node.value.id in BLOCKING_MODULES and node.attr in PROCESS_CALLS:
            return f"{node.value.id}.{node.attr} (passed)"
        if isinstance(node.value, ast.Name) and node.value.id == "time" and node.attr in BLOCKING_TIME:
            return f"time.{node.attr} (passed)"
        if isinstance(node.value, ast.Name) and node.attr in BLOCKING_HELPERS.get(node.value.id, ()):
            return f"{node.value.id}.{node.attr} (passed)"
    return None


def enclosing_function(node, parents):
    parent = parents.get(node)
    while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        parent = parents.get(parent)
    return parent


def kept_in_thread(node, parents, handed, tree, module="", seen=None):
    """True if the callable NODE evaluates to (a slow function passed, not called here) only ever runs in a wrapper's
    thread: it is a wrapper's work, lies in what one runs, or is the value - as it is, in a conditional, a tuple or a
    functools.partial - of a local name every use of which does the same (called in a lambda that is a wrapper's work,
    background(*call), handed on as work), or what a function returns whose every call does (background(
    give_targets(sai, host)))."""
    seen = set() if seen is None else seen
    if node in seen:
        return True
    seen.add(node)
    if at_work(node, parents) or deferred(node, parents, handed) or looked_at(node, parents):
        return True
    child, parent = node, parents.get(node)
    while isinstance(parent, (ast.IfExp, ast.Tuple, ast.List, ast.Dict, ast.Subscript)) or (
            isinstance(parent, ast.Call) and call_name(parent) in PARTIALS and parent.args and parent.args[0] is child):
        if (isinstance(parent, ast.IfExp) and child is parent.test) or \
                (isinstance(parent, ast.Dict) and child not in parent.values) or \
                (isinstance(parent, ast.Subscript) and child is not parent.value):
            return False
        child, parent = parent, parents.get(parent)
    if isinstance(parent, ast.Call) and at_work(parent, parents) and child is not parent.func:
        return True  # (an argument of the factory of a wrapper's work: background(self.on_host(host, sai.X, ...)))
    if isinstance(parent, ast.Assign) and all(isinstance(target, ast.Name) for target in parent.targets):
        scope = enclosing_function(parent, parents)
        names = {target.id for target in parent.targets}
        uses = [n for n in ast.walk(scope if scope is not None else tree) if isinstance(n, ast.Name)
                and n.id in names and isinstance(n.ctx, ast.Load)]
        return bool(uses) and all(used_in_thread(use, parents, handed, tree, module, seen) for use in uses)
    if isinstance(parent, ast.Return):
        function = enclosing_function(parent, parents)
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        if (module, function.name) in HANDED_ON:
            return True
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and call_name(n) == function.name]
        return bool(calls) and all(kept_in_thread(call, parents, handed, tree, module, seen) or (
            isinstance(parents.get(call), ast.Call) and parents[call].func is call
            and deferred(parents[call], parents, handed)) for call in calls)
    return False


def looked_at(node, parents):
    """NODE (a callable) is only looked at there, not run: `if reader is None`, `not read`, callable(read)."""
    parent = parents.get(node)
    return isinstance(parent, (ast.Compare, ast.UnaryOp)) or (
        isinstance(parent, (ast.If, ast.While, ast.IfExp)) and parent.test is node) or (
        isinstance(parent, ast.Call) and call_name(parent) in ("callable", "hasattr", "isinstance"))


def used_in_thread(use, parents, handed, tree, module, seen):
    """A use of a local name that holds a slow callable: called where that is deferred, or the callable handed on."""
    parent = parents.get(use)
    if isinstance(parent, ast.Call) and parent.func is use:
        return deferred(parent, parents, handed)
    if looked_at(use, parents):
        return True
    if isinstance(parent, ast.Starred):  # background(*call, ...): the function and its arguments
        call = parents.get(parent)
        return isinstance(call, ast.Call) and call_name(call) in WRAPPERS and call.args and call.args[0] is parent
    return kept_in_thread(use, parents, handed, tree, module, seen)


def blocking_calls(source, module=""):
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    handed, lines = called_only_deferred(tree, parents, handed_methods(tree, parents)), source.splitlines()
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) == "getattr" and len(node.args) > 1 and \
                holds_sai(node.args[0]) and isinstance(node.args[1], ast.Constant) and node.args[1].value in BLOCKING_SAI:
            # getattr(self.sai, "host_fix_plan", None): the slow method itself, for a backend that may lack it
            what = f"sai.{node.args[1].value} (passed)"
            if kept_in_thread(node, parents, handed, tree, module):
                continue
        elif isinstance(node, ast.Call):
            what = blocking(node)
            if what and deferred(node, parents, handed):
                continue
        elif isinstance(node, ast.Attribute) and not (isinstance(parents.get(node), ast.Call) and
                                                      parents[node].func is node):
            what = None if isinstance(parents.get(node), ast.Attribute) else passed(node)
            if what and kept_in_thread(node, parents, handed, tree, module):
                continue
        else:
            continue
        if what and ON_PURPOSE_MARK not in lines[node.lineno - 1]:
            found.append((node.lineno, what))
    return found


def test_pages_run_commands_and_slow_reads_only_in_the_background():
    """Every module of the app but the helpers (HELPER_MODULES): pages, dialogs, the window, tours, menus."""
    names = sorted(path.name for path in PACKAGE.glob("*.py") if path.name not in HELPER_MODULES)
    assert {"ui.py", "kits_page.py", "hosts_page.py", "tours.py", "intro.py", "main.py"} <= set(names)
    found = [f"{name}:{line} {what}" for name in names
             for line, what in blocking_calls((PACKAGE / name).read_text(), name)]
    assert not found, "on the GUI thread (use widgets.background, ThreadCall or Task):\n" + "\n".join(found)


def marked_sites():
    """{(module, innermost function)} of every line of the package that carries ON_PURPOSE_MARK."""
    found = set()
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text()
        lines = source.splitlines()
        marked = {number for number, line in enumerate(lines, 1) if ON_PURPOSE_MARK in line}
        if not marked:
            continue
        functions = [node for node in ast.walk(ast.parse(source))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for number in marked:
            around = [node for node in functions if node.lineno <= number <= node.end_lineno]
            inner = max(around, key=lambda node: node.lineno) if around else None
            found.add((path.name, inner.name if inner is not None else "<module>"))
    return found


def test_only_the_apps_start_and_end_wait_on_the_gui_thread_on_purpose():
    """The mark is no way out for a page (round 2, 2026-11-22): a line of any other function that says "on the GUI
    thread on purpose" fails here, and the Start's image check is not among them any more."""
    found = marked_sites()
    assert found <= ON_PURPOSE_SITES, f"marked on purpose, not listed: {sorted(found - ON_PURPOSE_SITES)}"
    assert not [site for site in found if site[0] == "prerequisites.py"]
    assert not [line for line in (PACKAGE / "prerequisites.py").read_text().splitlines() if ON_PURPOSE_MARK in line]


def test_a_method_called_only_in_the_background_is_not_flagged_and_one_called_on_the_gui_thread_is():
    """Round 1 (2026-11-22): a helper method that only a nested function handed to background() calls (also one
    defined inside an if) runs in that thread - its sandbox-ai reads are not the GUI thread's; one that is also called
    from a done= callback, or straight from a handler, is (and so is the subprocess it runs)."""
    source = """
class Page:
    def start(self):
        if self.ready:
            def read():
                return self.helper(), self.deeper()
            background(read, owner=self, done=lambda result, error: self.answered(result))

    def helper(self):
        return self.sai.host_json("lab", "x")

    def deeper(self):
        return self.inner()

    def inner(self):
        return self.sai.host_run("lab", "y")

    def answered(self, result):
        return self.on_gui()

    def on_gui(self):
        return self.sai.host_json("lab", "z")

    def clicked(self):
        return self.mixed()

    def mixed(self):
        import subprocess
        return subprocess.run(["podman"])
"""
    found = blocking_calls(source)
    assert [what for _, what in found] == ["sai.host_json()", "subprocess.run()"], found
    assert [line for line, _ in found] == [22, 29]


def test_a_roots_steps_and_what_connects_to_a_remote_system_are_caught_on_the_gui_thread():
    """Regression (review of the remote file manager): remote.py's calls from GUI code were not checked - a remote
    root's rename, new folder, permissions, the "already exists?" check and the first stat of Properties ran on the
    GUI thread and held up the window while a server stalled. They must go through root_job or on_root."""
    source = '''
class Page:
    def bad(self, chosen, root, pane):
        chosen.root.rename_entry("a", "b")
        pane.root.make_folder("", "x")
        root.chmod_entry("a", 0o600)
        self.root.stat_entry("a")
        files.exists_in(self.root, "", ["x"])
        files.entry_infos(root, [])
        remote.RemoteRoot("host")
        remote.scan_host(target)

    def good(self, chosen, root, pane):
        self.root_job(chosen.root, lambda cancelled: chosen.root.rename_entry("a", "b"), done, "text")
        on_root(root, lambda: files.entry_infos(root, []), done)
        background(lambda: remote.RemoteRoot("host"), done=done)
        self.make_folder(pane, "x")  # (the page's own method: root_job inside)
        listing.list()
'''
    found = [what for _, what in blocking_calls(source)]
    assert found == ["root.rename_entry()", "root.make_folder()", "root.chmod_entry()", "root.stat_entry()",
                     "exists_in()", "entry_infos()", "remote.RemoteRoot()", "remote.scan_host()"]


def test_the_blocking_check_sees_callbacks_partials_and_answers_on_the_gui_thread():
    """Round 2 (2026-11-22): what the guard missed - a slow method passed as a callback (connect, QTimer.singleShot) or
    wrapped in functools.partial outside a wrapper's work; an answer (done=lambda, a Task's done) that calls something
    slow (it runs on the GUI thread); a method or nested function handed to background() and also connected to a
    signal (that run is the GUI thread's)."""
    source = """
class Page:
    def bad(self):
        self.button.clicked.connect(self.sai.doctor)
        QTimer.singleShot(0, functools.partial(self.sai.call, "net", "show"))
        QTimer.singleShot(0, partial(subprocess.run, ["podman"]))
        background(self.work, done=lambda result, error: self.sai.host_run("lab", "x"))
        self.task.thread(lambda cancelled: 1, self.answered, "text")
        background(self.scan, done=self.done)
        self.timer.timeout.connect(self.scan)
        def read():
            return self.sai.runs()
        background(read, done=self.done)
        self.again.clicked.connect(read)

    def answered(self, result, error):
        return self.sai.instances()

    def scan(self):
        return self.sai.statistics("x")

    def good(self):
        background(functools.partial(self.sai.call, "net", "show"), done=self.done)
        ThreadCall(partial(self.sai.answer, "1-1-1"), "allow").start()
        self.task.call_watched(self.sai, lambda: self.sai.host_install("h"), self.done, "text")
        on_root(self.root, lambda: self.sai.host_file_roots("h"), self.done)
        background(self.only_here, done=self.done)

    def only_here(self):
        return self.sai.workspaces("x")

    def kept(self, what, host):
        call = {"apply": self.sai.update_apply, "rollback": self.sai.update_rollback}[what]
        background(lambda: call(True), done=self.done)
        background(self.on_host(host, self.sai.remove_device, "run"), done=self.done)
        run = self.sai.call if host == "local" else functools.partial(self.sai.host_run, host)
        held = (run, "net", "show")
        background(*held, done=self.done)

    def held_and_called(self, what):
        call = {"apply": self.sai.update_apply}[what]
        return call(True)

    def looked_up(self, host):
        reader = getattr(self.sai, "host_file_roots", None)
        if reader is None or not callable(reader):
            return
        background(lambda: reader(host), done=self.done)
        plan = getattr(self.sai, "host_fix_plan", None)
        return plan(host, "claude", [])
"""
    found = blocking_calls(source)
    assert sorted(what for _, what in found) == sorted([
        "sai.doctor (passed)", "sai.call (passed)", "subprocess.run (passed)", "sai.host_run()", "sai.instances()",
        "sai.statistics()", "sai.runs()", "sai.update_apply (passed)", "sai.host_fix_plan (passed)"]), found


# What makes a method of backend.py slow by itself: a process (subprocess, the runner of call()), a pipe served until
# its end (Pipes), a sleep.
SLOW_PRIMITIVES = {("subprocess", None), ("time", "sleep"), ("self", "run"), (None, "Pipes")}


def backend_calls(function):
    """{("self", NAME)} of the methods FUNCTION calls on self, {(None, NAME)} of the module's functions it calls, and
    ("subprocess", ATTR) / ("time", ATTR) of what it calls of those modules."""
    found = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            found.add((func.value.id, func.attr))
        elif isinstance(func, ast.Name):
            found.add((None, func.id))
    return found


def slow_methods(tree):
    """The SandboxAI methods that run a process or sleep, or call (through self, or a function of the module) one
    that does or one BLOCKING_SAI declares slow."""
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SandboxAI")
    graph = {("self", n.name): backend_calls(n) for n in cls.body if isinstance(n, ast.FunctionDef)}
    graph.update({(None, n.name): backend_calls(n) for n in tree.body if isinstance(n, ast.FunctionDef)})

    def primitive(call):
        return call in SLOW_PRIMITIVES or (call[0] == "subprocess")
    slow = {key for key, calls in graph.items() if any(primitive(c) for c in calls)}
    slow |= {("self", name) for name in BLOCKING_SAI if ("self", name) in graph}
    while True:
        more = {key for key, calls in graph.items() if key not in slow and calls & slow}
        if not more:
            break
        slow |= more
    return {name for kind, name in slow if kind == "self"}


def test_every_slow_backend_method_is_declared():
    """A method that runs `sandbox-ai ...` (here or over SSH), a process, or reads what asks Podman is declared in
    BLOCKING_SAI (or ON_PURPOSE), so the check above holds the pages to the background for it."""
    tree = ast.parse((PACKAGE / "backend.py").read_text())
    known = class_attributes(tree, "SandboxAI")
    assert not (BLOCKING_SAI | ON_PURPOSE) - known, f"not in backend.py: {sorted((BLOCKING_SAI | ON_PURPOSE) - known)}"
    undeclared = sorted(slow_methods(tree) - BLOCKING_SAI - ON_PURPOSE)
    assert not undeclared, "slow (a process, SSH, Podman), not declared in BLOCKING_SAI:\n" + "\n".join(undeclared)


def test_the_slow_method_check_follows_calls():
    tree = ast.parse('''
class SandboxAI:
    def call(self, *args):
        return subprocess.run(args)
    def wrapper(self):
        return self.call("x")
    def deeper(self):
        return helper(self)
    def fast(self):
        return 1
    def runs(self):
        return self.module("net").running()
    def users(self):
        return self.runs()
def helper(sai):
    return sai.wrapper()
''')
    assert slow_methods(tree) >= {"call", "wrapper", "users", "runs"} and "fast" not in slow_methods(tree)


def test_the_blocking_check_knows_the_file_operations():
    source = '''
class Page:
    def bad(self):
        self.root.delete_entry("x")
        files.copy_entry(a, "x", b, "")

    def good(self):
        def work(cancelled):
            return files.move_entry(a, "x", b, "", cancelled=cancelled)
        self.task.thread(work, self.done, "text", cancellable=True)
'''
    assert [what for _, what in blocking_calls(source)] == ["delete_entry()", "copy_entry()"]


def test_the_blocking_check_tells_direct_calls_from_deferred_ones():
    source = '''
class Page:
    def bad(self):
        status, out, err = self.sai.call("net", "show")
        subprocess.run(["x"])
        records = backend.read_connections(path)
        self.page.sai.file_roots()
        self.process.waitForFinished(5000)
        time.sleep(1)

    def good(self):
        background(self.sai.call, "net", "show", done=self.done)
        background(lambda: self.sai.call("mcp", "add", timeout=900), done=self.done)
        def work(cancelled):
            return self.sai.statistics(directory)
        self.task.thread(work, self.done, "text")
        ThreadCall(self.sai.answer, "1-1-1", "allow", "once").start()
        background(lambda: WL.add_and_check(sai, fields, "opencode"), done=self.done)
        background(self.scan, done=self.done)
        self.task.call_watched(self.sai, lambda: self.sai.host_install("h"), self.done, "text")
        self.sai.stop_commands()  # on the GUI thread on purpose: quitting
        background(PR.host_documents, self.sai, "h", done=self.done)

    def scan(self):
        return self.sai.instances()

    def also_bad(self):
        def helper():
            return self.sai.doctor()
        return helper()

    def models(self):
        return WL.list_models(self.sai, "lmstudio") + PR.host_documents(self.sai, "h")

    def read(self):
        return self.sai.runs()

    def again(self):
        background(self.read, done=self.done)
        return self.read()
'''
    assert sorted(what for _, what in blocking_calls(source)) == sorted([
        "sai.call()", "subprocess.run()", "backend.read_connections()", "sai.file_roots()", "waitForFinished()",
        "time.sleep()", "sai.doctor()", "WL.list_models()", "PR.host_documents()", "sai.runs()"])


# -- one confirmation policy: every call path that ends, removes or weakens something asks first --

# How the pages do each action of backend.NEEDS_CONFIRM: a SandboxAI method, or the words of a sandbox-ai command
# (consecutive string literals of one call or tuple). The functions that issue one must ask first with
# widgets.ask_first(..., ACTION, ...): in themselves or in a function they are nested in.
MUTATIONS = {
    "start-stop": ["stop_run", ("session", "stop")],
    "team-stop": ["team_stop"],
    "mcp-stop": ["stop_mcp"],
    "project-trust": ["trust_project", "host_project_trust"],
    "login-remove": [("identities", "remove"), "save_identity"],
    "login-logout": [("--logout",)],
    "kit-hooks-trust": ["trust_hooks_argv"],
    "gc": ["collect_as_shown"],
    "approval-forget": ["forget_remembered"],
    "defaults-lower": [("defaults", "set"), ("defaults", "unset")],
    "net-allow": [("net", "allow"), ("git-logins", "give"), ("secrets", "give")],
    "net-untaint": [("net", "untaint")],
    "workspace-writable": ["add_workspace"],
    "device-add": ["add_device"],
    "limits-raise": ["set_limits"],
    "host-remove": ["host_remove"],
    "host-edit": ["host_set"],
    "host-install": ["host_install"],
    "host-uninstall": ["host_uninstall"],
    "vpn-remove": ["vpn_remove"],
    "llm-remove": [("llm", "remove")],
    "llm-unload": [("llm", "unload")],
    "secret-delete": ["delete_secret"],
    "secret-replace": ["set_secret", "vpn_add"],
    "canary-tokens": ["canary_run"],
    "canary-resume": ["canary_resume"],
    "project-forget": ["forget_trusted", ("wizard", "forget")],
    "team-delete": ["team_delete"],
    "image-remove": [("images", "remove")],
    "mcp-attach": ["mcp_attach", "host_mcp_attach_live"],
    "mcp-copy": ["host_mcp_copy"],
    "mcp-update": ["host_mcp_update"],
    "kits-attach": ["host_kits_attach"],
    "kits-detach": ["host_kits_detach"],
    "agent-restart": ["host_agent_restart"],
    "mcp-detach": ["host_mcp_detach_live", "host_mcp_detach"],
    "net-keep": ["net_keep"],
    "web-user-remove": ["host_web_user_remove"],
    "web-user-reset": [("reset",)],
    "web-cert-renew": ["host_web_cert"],
    "web-listen": [("serve",)],
    "session-move": ["session_move", ("session", "move")],
    "session-replace": ["session_copy", "session_move"],
    "session-import": ["session_import"],
    "session-grant-approvals": ["session_import", "session_duplicate", "session_copy", "session_move"],
    "home-restore": ["restore_home"],
    "rules-reload": ["rules_reload"],
    "bridge-add": ["bridge_add"],
    "bridge-revoke": ["bridge_revoke"],
    "bridge-resume": ["bridge_resume"],
    "checkpoint-restore": ["checkpoint_restore"],
    "checkpoint-delete": ["checkpoint_delete"],
}

# The actions of the web interface's table (backend.NEEDS_CONFIRM is the same set) that no page of the app does yet:
# the app gets them with the command-line parity work, and asks first there then. Exact: an action a page asks for is
# taken off this list (the test says so), and nothing else may be missing.
WEB_ONLY = set()  # every action of the table is asked for by a page now

def string_runs(node):
    """Every run of consecutive string literals among the arguments of a call or the items of a tuple or list."""
    items = list(node.args) if isinstance(node, ast.Call) else list(getattr(node, "elts", []))
    words = [item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else None for item in items]
    return [tuple(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)
            if all(w is not None for w in words[i:j])]


def issues(node, marker):
    """True if NODE (a Call, Tuple, List or Attribute) is how MARKER is done."""
    if isinstance(marker, str):
        return isinstance(node, ast.Attribute) and node.attr == marker and is_sai(node.value)
    return isinstance(node, (ast.Call, ast.Tuple, ast.List)) and marker in string_runs(node)


def asked_actions(function):
    """The actions FUNCTION (with what is nested in it) asks for: the string literals of ask_first's second argument."""
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ask_first" \
                and len(node.args) > 1:
            found |= {n.value for n in ast.walk(node.args[1]) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return found


def unasked(source):
    """[(line, action)] of the mutating calls in SOURCE whose function (or one it is nested in) does not ask first."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        for action, markers in MUTATIONS.items():
            if not any(issues(node, marker) for marker in markers):
                continue
            chain, parent = [], parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chain.append(parent)
                parent = parents.get(parent)
            if not chain:
                continue  # module level: a table, not a call path
            if not any(action in asked_actions(function) for function in chain):
                found.append((node.lineno, action))
    return found


def test_every_call_that_ends_removes_or_weakens_something_asks_first():
    """The walk over the mutating calls of the pages (review round 3, A5): net allow, raised limits, a replaced or
    deleted secret, kits, MCP attach and restart, defaults lowered, gc, forget, untaint, devices, workspaces, hosts,
    stops - each asks through widgets.ask_first with its action of backend.NEEDS_CONFIRM."""
    import sys
    sys.path.insert(0, str(PACKAGE.parent))
    from sandbox_ai_app import backend
    actions = set(backend.NEEDS_CONFIRM) | set(backend.APP_NEEDS_CONFIRM)
    assert set(MUTATIONS) <= actions
    found, asked = [], set()
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in ("backend.py", "widgets.py"):
            continue
        source = path.read_text()
        found += [f"{path.name}:{line} {action}" for line, action in unasked(source)]
        asked |= asked_actions(ast.parse(source))
    assert not found, "not asked first (widgets.ask_first):\n" + "\n".join(found)
    assert not asked - actions, f"asked for actions the table does not have: {sorted(asked - actions)}"
    assert WEB_ONLY <= actions, f"WEB_ONLY names actions the table does not have: {sorted(WEB_ONLY - actions)}"
    assert not WEB_ONLY & asked, f"asked for now: take them off WEB_ONLY: {sorted(WEB_ONLY & asked)}"
    assert actions - WEB_ONLY <= asked, f"actions of the table no page asks for: {sorted(actions - WEB_ONLY - asked)}"


def test_the_walk_finds_a_call_that_does_not_ask():
    source = '''
class Page:
    def bad(self):
        background(self.sai.host_run, host, "net", "allow", run, done=self.done)
        background(self.sai.set_limits, run, "16g", done=self.done)

    def good(self):
        if not ask_first(self, "net-allow", "t", "x", "Allow"):
            return
        def work():
            return self.sai.host_run(host, "net", "allow", run)
        background(work, done=self.done)

    def unrelated(self):
        if ask_first(self, "gc", "t", "x", "Remove"):
            self.sai.set_limits(run, "16g")
'''
    assert unasked(source) == [(4, "net-allow"), (5, "limits-raise"), (16, "limits-raise")]


def test_no_event_filter_on_the_whole_application():
    """An event filter in Python on the QApplication sees every event of every object, QtWebEngine's own included:
    a terminal tab opened while one was installed ended the app with a segmentation fault (the Board's Start soon
    after the app started, while the pages were still made under an input watch). Filters go on the window's own
    QWindow or on the widgets concerned; keys on QShortcuts."""
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(QApplication\\.instance\\(\\)|\\bapp|qApp|QCoreApplication\\.instance\\(\\))\\.installEventFilter",
                         code):
                found.append(f"{path.name}:{number}: {line.strip()}")
    assert found == []
