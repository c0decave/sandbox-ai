#!/usr/bin/env python3
"""Agent logins of sessions: which login each start uses, a session's login as the central login, and a new start's
login taken from a session.

  sandbox-ai login --status --sessions [--json] [--host HOST]
  sandbox-ai login AGENT --from-session NAME [--yes] [--replace] [--dry-run] [--expect FINGERPRINT] [--json]
                   [--host HOST]
  sandbox-ai AGENT --auth-from-session NAME ...
  sandbox-ai login give RUN [AGENT] [--from-session NAME] [--yes] [--restart] [--dry-run] [--expect FINGERPRINT]
                   [--json] [--host HOST]

A session's login file (agent_sandbox.AUTH_TARGETS in its HOME, and Claude's account state ~/.claude.json) was
written by the agent in that sandbox: untrusted. It is read through directory descriptors that never follow a link
(session_inspect's readers: activity.open_file), regular files only, within a size limit, and checked with the same
structure checks `sandbox-ai login` uses (claude_oauth_from, login_document_ok). What it says about its account
(an email, an organization, a plan, an account id - from Claude's .claude.json, from the claims of Codex's id_token,
decoded but not verified) is shown as "as the file says", one line each, escaped. No token value is ever shown,
logged or written into JSON: a login is named by its fingerprint, the first 8 hex characters of the SHA-256 of its
tokens, so two sessions that share one can be seen to.

Importing a session's login as the central one (`sandbox-ai login AGENT --from-session NAME`) is the human's
decision: the account is shown first, then asked (or --yes); a central login of another account (or one whose
account cannot be told) is replaced only with --replace. --expect FINGERPRINT imports only the login that was shown
(the app and the web interface pass it after their confirmation).

--auth-from-session NAME (a start option) copies NAME's login into the new sandbox. Subscription logins rotate their
refresh tokens: two sandboxes that refresh the same login outside the central store can log each other out, so the
start warns once and names the safer way (import it as the central login). When the new sandbox refreshed the login,
its refreshed tokens go back into NAME's kept HOME at the end of the run only when NAME does not run then (stopped,
its HOME kept), NAME's login is still the one that was copied, and the refreshed tokens are of the same account
(merge_refreshed; Claude: Anthropic names the same account). A running (or paused) session's HOME is never written by a write-back.

`sandbox-ai login give RUN [AGENT]` is the human's own step for a sandbox that runs: the central login (or, with
--from-session NAME, another session's) is put into RUN's HOME as a start puts it there - the same checked form, the
same files, mode 0600, written beside the file and renamed over it through directory descriptors (the agent may be
writing its HOME: never through a link). The account is shown first, then asked (or --yes; --expect as above).
Claude Code reads its login file again before its next request; the other agents read theirs when they start, so
the agent must be started again inside the sandbox (asked, or --restart: sandbox-ai kits restart-agent RUN, the
conversation continues). The source keeps its copy: the same rotating-token note as above holds. Nothing changes
in the central store: what a sandbox writes back there is only ever the refreshed tokens of the store's own account.
Standard library only.
"""
import base64
import contextlib
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

HERE = Path(__file__).resolve().parent

# The agents whose logins the central store keeps (login.AGENTS): only these are listed, imported and taken from a
# session. The others have no login sandbox-ai takes in (agent_sandbox.NO_LOGIN_AGENTS) or keep it elsewhere (DSH).
AGENTS = ("claude", "codex", "opencode", "hermes", "qwen", "pi")
LOGIN_LIMIT = 1 << 20          # bytes of a login file read (agent_sandbox.CONFIG_LIMIT)
STATE_LIMIT = 64 << 20         # bytes of Claude's ~/.claude.json read (agent_sandbox.CLAUDE_STATE_LIMIT)
TOKEN_LIMIT = 16 << 10         # characters of a JWT whose claims are decoded
TEXT_LIMIT = 200               # characters of a value the file says that are shown
FINGERPRINT = re.compile(r"[0-9a-f]{8}")
RUNNING = ("running", "paused", "orphaned")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# What a Claude login takes of the session's ~/.claude.json (agent_sandbox.CLAUDE_STATE_KEYS), each with its type:
# anything else, or of another type, is left out.
CLAUDE_STATE_TYPES = {"oauthAccount": dict, "userID": str, "hasCompletedOnboarding": bool,
                      "lastOnboardingVersion": str, "theme": str}
ROTATING_NOTE = ("subscription logins rotate their refresh tokens: two sandboxes that refresh the same login outside "
                 "the central store can log each other out. The safer way: import it as the central login "
                 "(sandbox-ai login {agent} --from-session {name}); every sandbox of {agent} then shares it")

_modules = {}
_LOADING = threading.RLock()


def module(name):
    """sandbox-ai's NAME.py, loaded once by path ("launcher": agent_sandbox.py, unless the launcher gave itself)."""
    with _LOADING:
        if name not in _modules:
            file = "agent_sandbox" if name == "launcher" else name
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{file}_for_session_login", HERE / f"{file}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def launcher():
    return module("launcher")


def inspect():
    """session_inspect.py: the start's HOME (locate), its readers, printable()."""
    return module("session_inspect")


def one_line(text):
    return inspect().one_line(text)


def said(value, limit=TEXT_LIMIT):
    """A value a login file says, for display: a string or number in one escaped line of at most LIMIT characters;
    None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = one_line(str(value)).strip()
    return text[:limit] if text else None


def iso_ms(value):
    """An ISO time (UTC) of the millisecond timestamp VALUE, or None for anything implausible."""
    if not launcher().timestamp_ms(value):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1000))


def iso_s(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 2 ** 40:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


# --- reading a login out of a HOME ------------------------------------------------------------------------------------

def read_file(home, relative, limit):
    """The bytes of the regular file HOME/RELATIVE, opened without following a link anywhere on the way (HOME
    itself included), at most LIMIT of them. FileNotFoundError when it is not there; ValueError (why) for a link,
    a FIFO, a device, a folder or a file over LIMIT."""
    activity = inspect().module("activity")
    try:
        fd, info = activity.open_file(str(home), relative)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):  # a link (not followed) on the way or at the end
            raise ValueError(f"~/{relative} is a link or lies behind one (not followed)") from None
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(relative) from None
        if exc.errno is None:  # (activity.open_file: a FIFO, a device, a folder)
            raise ValueError(f"~/{relative} is not a regular file") from None
        raise ValueError(f"~/{relative} cannot be read ({one_line(exc.strerror or exc)})") from None
    try:
        if info.st_size > limit:
            raise ValueError(f"~/{relative} is larger than {limit} bytes")
        data = activity.read_at(fd, 0, limit + 1)
        if len(data) > limit:
            raise ValueError(f"~/{relative} is larger than {limit} bytes")
        return data
    finally:
        os.close(fd)


def claude_state(data):
    """CLAUDE_STATE_TYPES of the Claude account state DATA (bytes), each of its type (oauthAccount: its plain values
    only), as a dict; {} when DATA is no JSON object."""
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(document, dict):
        return {}
    kept = {}
    for key, kind in CLAUDE_STATE_TYPES.items():
        value = document.get(key)
        if not isinstance(value, kind) or (kind is str and len(value) > 400):
            continue
        if kind is dict:
            value = {k: v for k, v in list(value.items())[:64] if isinstance(k, str) and len(k) <= 100
                     and (v is None or isinstance(v, (bool, int, float)) or isinstance(v, str) and len(v) <= 400)}
        kept[key] = value
    return kept


def parse_login(agent, files):
    """(the login's own document, Claude's account state or {}) of FILES ({relative path: bytes}), checked as
    `sandbox-ai login` checks a login (claude_oauth_from, login_document_ok). ValueError: no usable login."""
    l = launcher()
    target = l.AUTH_TARGETS[agent]
    data = files.get(target)
    if data is None:
        raise ValueError("no login")
    if agent == "claude":
        oauth = l.claude_oauth_from(data)
        if oauth is None:
            raise ValueError("no usable Claude login (not a claudeAiOauth block with its tokens)")
        login = {**l.oauth_fields(oauth, l.OAUTH_INFO_FIELDS), **l.oauth_fields(oauth, l.OAUTH_TOKEN_FIELDS)}
        state = claude_state(files[".claude.json"]) if ".claude.json" in files else {}
        return {"claudeAiOauth": login}, state
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        raise ValueError(f"~/{target} is not JSON") from None
    if not l.login_document_ok(agent, document):
        raise ValueError(f"~/{target} does not hold a {agent} login of the form sandbox-ai takes")
    return document, {}


def read_login(home, agent):
    """{relative path: bytes} of AGENT's login in HOME as the central store keeps it (Claude: its claudeAiOauth block,
    and the account subset of ~/.claude.json when there is one). FileNotFoundError: no login file; ValueError: not a
    usable one (why)."""
    if agent not in AGENTS:
        raise ValueError(f"{agent} has no login sandbox-ai takes from a session (only {', '.join(AGENTS)})")
    target = launcher().AUTH_TARGETS[agent]
    files = {target: read_file(home, target, LOGIN_LIMIT)}
    if agent == "claude":
        with contextlib.suppress(FileNotFoundError, ValueError):
            files[".claude.json"] = read_file(home, ".claude.json", STATE_LIMIT)
    return stored_form(agent, files)


def stored_form(agent, files):
    """FILES as the central store keeps them: the login checked and written out again (nothing else of the files)."""
    document, state = parse_login(agent, files)
    stored = {launcher().AUTH_TARGETS[agent]: json.dumps(document, indent=2).encode()}
    if agent == "claude" and state:
        stored[".claude.json"] = json.dumps(state, indent=2).encode()
    return stored


# --- what a login says about itself (never its tokens) ----------------------------------------------------------------

def fingerprint(agent, document):
    """The first 8 hex characters of the SHA-256 of the login's tokens (Claude: its token fields; the others: the
    whole login document): the same login in two places has the same one."""
    if agent == "claude":
        document = launcher().oauth_fields(document.get("claudeAiOauth") or {}, launcher().OAUTH_TOKEN_FIELDS)
    data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()[:8]


def jwt_claims(token):
    """The claims of the JWT TOKEN, decoded WITHOUT checking its signature (what the file says), or {}."""
    if not isinstance(token, str) or len(token) > TOKEN_LIMIT or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def describe(agent, files):
    """What the login FILES ({relative path: bytes}, stored_form) say about themselves, never a token:
    {"agent", "kind" ("subscription", "api-key", "subscription+api-key"), "account": {"email", "organization",
    "plan", "account_id", "providers"} (as the file says; None where it says nothing), "expires" (the access
    token's, ISO), "refresh_expires", "last_refresh" (as the file says), "fingerprint", "identity" (what tells the
    account apart, or None when it cannot be told)}."""
    document, state = parse_login(agent, files)
    account = {"email": None, "organization": None, "plan": None, "account_id": None, "providers": []}
    info = {"agent": agent, "kind": "subscription", "account": account, "expires": None, "refresh_expires": None,
            "last_refresh": None, "fingerprint": fingerprint(agent, document), "identity": None}
    if agent == "claude":
        oauth = document["claudeAiOauth"]
        owner = state.get("oauthAccount") or {}
        account.update(email=said(owner.get("emailAddress")), organization=said(owner.get("organizationName")),
                       plan=said(oauth.get("subscriptionType")), account_id=said(owner.get("accountUuid")))
        info.update(expires=iso_ms(oauth.get("expiresAt")), refresh_expires=iso_ms(oauth.get("refreshTokenExpiresAt")))
        ids = (owner.get("accountUuid"), owner.get("organizationUuid"))
        if all(isinstance(value, str) and value for value in ids):
            info["identity"] = "claude:" + ":".join(ids)
    elif agent == "codex":
        tokens = document.get("tokens") if isinstance(document.get("tokens"), dict) else {}
        key = isinstance(document.get("OPENAI_API_KEY"), str) and bool(document["OPENAI_API_KEY"])
        oauth = isinstance(tokens.get("refresh_token"), str) and bool(tokens.get("refresh_token"))
        info["kind"] = "subscription+api-key" if key and oauth else "api-key" if key else "subscription"
        claims = jwt_claims(tokens.get("id_token"))
        profile = claims.get("https://api.openai.com/profile")
        auth = claims.get("https://api.openai.com/auth")
        email = claims.get("email")
        if not isinstance(email, str) and isinstance(profile, dict):
            email = profile.get("email")
        account.update(email=said(email),
                       plan=said(auth.get("chatgpt_plan_type")) if isinstance(auth, dict) else None,
                       account_id=said(tokens.get("account_id")))
        info["expires"] = iso_s(jwt_claims(tokens.get("access_token")).get("exp"))
        info["last_refresh"] = said(document.get("last_refresh"), 40)
        if oauth and isinstance(tokens.get("account_id"), str):
            info["identity"] = "codex:" + tokens["account_id"]
    elif agent in ("opencode", "pi"):
        kinds, ends, ids = set(), [], []
        for provider, entry in sorted(document.items())[:32]:
            kind = entry.get("type")
            account["providers"].append(f"{said(provider, 40)} ({said(kind, 20)})")
            if kind == "oauth":
                kinds.add("subscription")
                if launcher().timestamp_ms(entry.get("expires")):
                    ends.append(entry["expires"])
                ids.append((provider, entry.get("accountId") if isinstance(entry.get("accountId"), str) else None))
            else:
                kinds.add("api-key")
                ids.append((provider, None))
            if isinstance(entry.get("accountId"), str) and not account["account_id"]:
                account["account_id"] = said(entry["accountId"])
        info["kind"] = "+".join(sorted(kinds, key=lambda k: k != "subscription")) or "subscription"
        info["expires"] = iso_ms(min(ends)) if ends else None
        if ids and all(ident for _, ident in ids):
            info["identity"] = f"{agent}:" + ",".join(f"{p}={i}" for p, i in ids)
    elif agent == "qwen":
        info["expires"] = iso_ms(document.get("expiry_date"))
        account["providers"] = ["Qwen OAuth"]
        resource = said(document.get("resource_url"))
        account["organization"] = resource
    elif agent == "hermes":
        kinds = set()
        for section in ("providers", "credential_pool"):
            entries = document.get(section) if isinstance(document.get(section), dict) else {}
            for provider, entry in sorted(entries.items())[:32]:
                if entry:
                    account["providers"].append(said(provider, 40))
                    kinds.add("subscription" if section == "providers" else "api-key")
        info["kind"] = "+".join(sorted(kinds, key=lambda k: k != "subscription")) or "subscription"
    return info


def public(info):
    """INFO (describe) without what only compares (its identity)."""
    return {key: value for key, value in info.items() if key != "identity"} if info else info


def same_account(new, old):
    """True: the logins NEW and OLD (describe) are of the same account as their files say; False: of another, or it
    cannot be told."""
    return bool(new and old and new["identity"] and new["identity"] == old["identity"])


def info_lines(info, indent="  "):
    """The lines that show a login (describe), every value one escaped line."""
    account = info.get("account") or {}
    lines = []
    for label, key in (("account", "email"), ("organization", "organization"), ("plan", "plan"),
                       ("account id", "account_id")):
        if account.get(key):
            lines.append(f"{indent}{label + ':':14}{one_line(account[key])} (as the file says)")
    if account.get("providers"):
        lines.append(f"{indent}{'providers:':14}{one_line(', '.join(map(str, account['providers'])))}")
    kind = {"subscription": "subscription (OAuth)", "api-key": "API key",
            "subscription+api-key": "subscription (OAuth) and API key"}.get(info.get("kind"), info.get("kind"))
    lines.append(f"{indent}{'kind:':14}{kind}")
    if info.get("expires"):
        past = info["expires"] <= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines.append(f"{indent}{'expires:':14}{info['expires']}" + (" (expired: refreshed on use)" if past else ""))
    if info.get("refresh_expires"):
        lines.append(f"{indent}{'renewable to:':14}{info['refresh_expires']}")
    if info.get("last_refresh"):
        lines.append(f"{indent}{'last refresh:':14}{one_line(info['last_refresh'])}")
    lines.append(f"{indent}{'fingerprint:':14}{info.get('fingerprint')}")
    return lines


# --- the central store ------------------------------------------------------------------------------------------------

def central_files(agent):
    """{relative path: bytes} of the central store's login of AGENT (sandbox-ai login), or None."""
    l = launcher()
    if not l.store_login(agent):
        return None
    store = l.auth_store(agent)
    files = {}
    for relative in (l.AUTH_TARGETS[agent], *([".claude.json"] if agent == "claude" else [])):
        with contextlib.suppress(OSError, ValueError):
            files[relative] = l.read_config_bytes(store / relative, STATE_LIMIT if relative == ".claude.json"
                                                  else LOGIN_LIMIT)
    return files if l.AUTH_TARGETS[agent] in files else None


def central(agent):
    """describe() of the central login of AGENT, or None."""
    files = central_files(agent)
    if files is None:
        return None
    try:
        return describe(agent, files)
    except ValueError:
        return None


# --- the sessions -----------------------------------------------------------------------------------------------------

def option_value(options, name):
    """The value of the last NAME in the saved OPTIONS (NAME VALUE or NAME=VALUE), or None."""
    found = None
    for index, token in enumerate(options):
        if not isinstance(token, str):
            continue
        if token == name and index + 1 < len(options) and isinstance(options[index + 1], str):
            found = options[index + 1]
        elif token.startswith(name + "="):
            found = token[len(name) + 1:]
    return found


def saved_options(found):
    """The options a start was started with (its session.json), or []."""
    directory = found.get("directory")
    if not directory:
        return []
    with contextlib.suppress(OSError, ValueError, RecursionError):
        data = json.loads((Path(directory) / "session.json").read_text())
        options = data.get("options") if isinstance(data, dict) else None
        if isinstance(options, list):
            return [token for token in options if isinstance(token, str)]
    return []


def source_of(agent, options):
    """(source, session) of the login a start with OPTIONS got: "none" (--no-auth, --llm), "session" (and its name:
    --auth-from-session), "file" (--auth-file, --import-codex-auth, a --copy of a login file), "store" (the central
    login: a stored one now), "host" (this machine's own)."""
    l = launcher()
    shorts = {short: name for name, short in l.SHORT_OPTIONS.items()}
    names = [shorts.get(token.split("=", 1)[0], token.split("=", 1)[0]) for token in options]
    chosen = option_value(options, "--auth-from-session") or option_value(options, l.SHORT_OPTIONS.get(
        "--auth-from-session", "--auth-from-session"))
    if "--no-auth" in names:
        return "none", None
    if chosen:
        return "session", chosen
    if "--auth-file" in names or "--import-codex-auth" in names:
        return "file", None
    targets = {l.AUTH_TARGETS[agent], ".claude.json"}
    copies = [option_value([token], "--copy") or "" for token in options if token.startswith("--copy=")]
    copies += [options[i + 1] for i, token in enumerate(options[:-1]) if token in ("--copy", "-cp")]
    if any(":" in value and os.path.normpath(value.rsplit(":", 1)[1]) in targets for value in copies):
        return "file", None
    if "--llm" in names:
        return "none", None
    return ("store" if l.store_login(agent) else "host"), None


def session_entry(entry, found, shared):
    """One row of the logins of the sessions: ENTRY (starts.entries) with what its HOME's login says.
    SHARED: {agent: the central login's fingerprint}."""
    si = inspect()
    agent = entry.get("agent")
    row = {"name": entry.get("name"), "id": entry.get("id"), "agent": agent, "state": entry.get("state"),
           "kind_of_start": entry.get("kind"), "source": None, "source_session": None, "login": False,
           "problem": None, "info": None, "shares_central": False, "modified": None}
    try:
        located = si.locate(entry.get("id") or entry.get("name"), found)
    except ValueError as exc:
        row["problem"] = one_line(exc)[:300]
        return row
    row["source"], row["source_session"] = source_of(agent, saved_options(located))
    if row["source_session"] is not None:
        row["source_session"] = one_line(row["source_session"])[:128]
    if located["home"] is None:
        row["problem"] = one_line(located["home_why"] or "no HOME here")[:300]
        return row
    target = launcher().AUTH_TARGETS[agent]
    try:
        files = read_login(Path(located["home"]), agent)
        info = describe(agent, files)
    except FileNotFoundError:
        row["problem"] = "no login in its HOME"
        return row
    except ValueError as exc:
        row["problem"] = one_line(exc)[:300]
        return row
    with contextlib.suppress(OSError):
        fd, stat_ = si.module("activity").open_file(located["home"], target)
        os.close(fd)
        row["modified"] = si.iso(stat_.st_mtime)
    if not info["last_refresh"]:
        info["last_refresh"] = row["modified"]
    row.update(login=True, info=public(info), shares_central=shared.get(agent) == info["fingerprint"])
    return row


def sessions():
    """{"sessions": [row per running or kept start of an agent the central store takes], "central": {agent:
    describe() of its central login or None}}: which login each start uses, as its HOME has it."""
    found = inspect().module("starts").entries(True)
    stored = {agent: central(agent) for agent in AGENTS}
    shared = {agent: info["fingerprint"] for agent, info in stored.items() if info}
    rows = [session_entry(entry, found, shared) for entry in found
            if entry.get("kind") != "mcp" and entry.get("agent") in AGENTS and entry.get("state") != "damaged"]
    return {"sessions": rows, "central": {agent: public(info) for agent, info in stored.items()}}


SOURCE_TEXT = {"store": "central login", "host": "this computer's login", "file": "a login file",
               "none": "no login", None: "?"}


def source_text(row):
    if row.get("source") == "session":
        return f"from session {one_line(row.get('source_session') or '?')}"
    return SOURCE_TEXT.get(row.get("source"), one_line(row.get("source")))


def sessions_lines(result):
    rows = result.get("sessions") or []
    if not rows:
        return ["No running or kept session of " + ", ".join(AGENTS) + "."]
    lines = []
    for row in rows:
        head = (f"{one_line(row.get('name') or '?')}  {one_line(row.get('agent') or '?')}  "
                f"{one_line(row.get('state') or '?')}  login: {source_text(row)}")
        lines.append(head)
        if row.get("login") and isinstance(row.get("info"), dict):
            lines += info_lines(row["info"])
            if row.get("shares_central"):
                lines.append("  the same login as the central one")
        else:
            lines.append(f"  {one_line(row.get('problem') or 'no login')}")
    return lines


# --- one session's login: shown, imported as the central login --------------------------------------------------------

def session_login(name, agent=None, found=None):
    """(located start, its login files (stored_form), describe()) of the start NAME. ValueError: no such start, one of
    another agent than AGENT, no HOME here, no usable login."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"not a start's name: {one_line(name)[:80]}")
    located = inspect().locate(name, found)
    shown = one_line(located["name"])
    if agent is not None and located["agent"] != agent:
        raise ValueError(f"{shown} is a {one_line(located['agent'])} session: it has no {agent} login")
    if located["agent"] not in AGENTS:
        raise ValueError(f"{shown} runs {one_line(located['agent'])}: sandbox-ai takes no login of it (only "
                         f"{', '.join(AGENTS)})")
    if located["home"] is None:
        raise ValueError(located["home_why"] or f"{shown} has no HOME here")
    try:
        files = read_login(Path(located["home"]), located["agent"])
    except FileNotFoundError:
        raise ValueError(f"{shown} has no {located['agent']} login in its HOME") from None
    except ValueError as exc:
        raise ValueError(f"the login of {shown} is not taken: {one_line(exc)}") from None
    return located, files, describe(located["agent"], files)


def preview(agent, name):
    """What importing NAME's login as the central login of AGENT would do: {"agent", "session": {name, id, state},
    "login": describe, "central": describe of the stored one or None, "same": it is that very login, "same_account",
    "needs_replace", "running"}."""
    located, _files, info = session_login(name, agent)
    stored = central(agent)
    same = bool(stored and stored["fingerprint"] == info["fingerprint"])
    return {"agent": agent, "session": {"name": located["name"], "id": located["id"], "state": located["state"]},
            "login": public(info), "central": public(stored), "same": same,
            "same_account": same or same_account(info, stored),
            "needs_replace": bool(stored) and not same and not same_account(info, stored),
            "running": located["state"] in RUNNING}


def preview_lines(result):
    session = result.get("session") or {}
    lines = [f"The {result.get('agent')} login of {one_line(session.get('name'))} "
             f"({one_line(session.get('state'))}):", *info_lines(result.get("login") or {})]
    stored = result.get("central")
    if result.get("same"):
        lines.append("It is the central login already.")
    elif stored:
        lines.append("It replaces the central login" + (" of the same account:" if result.get("same_account") else
                                                         " of ANOTHER account (or one that cannot be told):"))
        lines += info_lines(stored)
    else:
        lines.append(f"No central {result.get('agent')} login is stored yet: sandboxes use this computer's login "
                     "until then.")
    return lines


def import_login(agent, name, expect=None, replace=False):
    """Make NAME's login of AGENT the central login (login.write_store), only when its fingerprint is EXPECT (when
    given) and, for a stored login of another account, with REPLACE. Returns {"imported", "agent", "session", "login",
    "replaced", "notes"}."""
    if expect is not None and not FINGERPRINT.fullmatch(str(expect)):
        raise ValueError("--expect is the 8 hex characters of a login's fingerprint")
    located, files, info = session_login(name, agent)
    shown = one_line(located["name"])
    if expect is not None and info["fingerprint"] != expect:
        raise ValueError(f"the login of {shown} changed since it was shown (fingerprint {info['fingerprint']} now, "
                         f"not {expect}): look at it again")
    stored = central(agent)
    notes = []
    if stored and stored["fingerprint"] == info["fingerprint"]:
        return {"imported": False, "agent": agent, "session": located["name"], "login": public(info),
                "replaced": None, "notes": ["it is the central login already"]}
    if stored and not same_account(info, stored) and not replace:
        raise ValueError(f"the central {agent} login is of another account, or its account cannot be told: add "
                         "--replace to replace it")
    login_module().write_store(agent, files)
    if located["state"] in RUNNING:
        notes.append(f"{shown} still runs with this login: when it refreshes the tokens there, the central copy may "
                     f"stop working (sandbox-ai login {agent} logs in again)")
    elif agent != "claude":
        # Its next start (with the central login) takes the store's newer tokens over the copy in its HOME
        # (agent_sandbox.sync_store_login), as a sandbox whose login came from the store does.
        with contextlib.suppress(OSError, ValueError):
            digest = hashlib.sha256(read_file(Path(located["home"]), launcher().AUTH_TARGETS[agent],
                                              LOGIN_LIMIT)).hexdigest()
            launcher().remember_sync(agent, located["home"], digest)
    return {"imported": True, "agent": agent, "session": located["name"], "login": public(info),
            "replaced": public(stored), "notes": notes}


def login_module():
    loaded = module("login")
    loaded._launcher = loaded._launcher or launcher()
    return loaded


# --- a start's login from a session (--auth-from-session) -------------------------------------------------------------

def copy_into(a, home, warn=None):
    """Prepare the HOME of the start A (--auth-from-session NAME): NAME's login of A's agent copied in, unless HOME
    has one already (a resumed start keeps its own). Returns [(what it came from, relative target)]; sets
    a.session_login, what write_back compares at the end. ValueError: NAME is not a start of that agent with a
    usable login here."""
    l = launcher()
    warn = warn or (lambda line: print(f"agent-sandbox: {line}", file=sys.stderr))
    name, agent = a.auth_from_session, a.agent
    target = l.AUTH_TARGETS[agent]
    a.session_login = None
    if (Path(home) / target).exists() or (Path(home) / target).is_symlink():
        return []
    try:
        located, files, info = session_login(name, agent)
    except ValueError as exc:
        raise ValueError(f"--auth-from-session: {exc}") from None
    if located["home"] and Path(located["home"]).resolve() == Path(home).resolve():
        raise ValueError("--auth-from-session names this very start")
    source = read_file(Path(located["home"]), target, LOGIN_LIMIT)
    copied = []
    for relative in sorted(files, key=lambda path: path == target):  # (Claude's account state first)
        l.replace_private(files[relative], Path(home), relative)
        copied.append((f"session {located['name']}: ~/{relative}", relative))
    a.session_login = {"name": located["name"], "id": located["id"], "agent": agent,
                       "source": hashlib.sha256(source).hexdigest(),
                       "copied": hashlib.sha256(files[target]).hexdigest(), "accounts": {}}
    if info["kind"] != "api-key":
        warn("--auth-from-session: " + ROTATING_NOTE.format(agent=agent, name=one_line(located["name"])))
    if agent == "claude":
        # Which account the copied tokens are, asked now while they are valid (the end of the run compares it).
        oauth = l.claude_oauth_from(files[target])
        state = a.session_login["accounts"]
        threading.Thread(target=lambda: l.claude_token_account(state, oauth, final=True), daemon=True).start()
    return copied


def write_back(a, home):
    """At the end of a run with --auth-from-session: the tokens the agent refreshed in HOME go back into the source
    session's kept HOME - only when the source does not run, its login is still the one copied in, and the refreshed
    login is of the same account. A status line, or None (nothing refreshed)."""
    state = getattr(a, "session_login", None)
    if not state:
        return None
    l = launcher()
    agent, target = state["agent"], l.AUTH_TARGETS[state["agent"]]
    try:
        data = read_file(Path(home), target, LOGIN_LIMIT)
    except (OSError, ValueError):
        return None
    if hashlib.sha256(data).hexdigest() == state["copied"]:
        return None
    shown = one_line(state["name"])
    where = f"the HOME of session {shown}"
    try:
        located = inspect().locate(state["id"] or state["name"])
    except ValueError:
        return f"the refreshed {agent} login was not written back: session {shown} is gone"
    if located["state"] in RUNNING:
        return (f"the refreshed {agent} login was not written back to {where}: it runs (a running session's HOME is "
                "never written); it may have to log in again")
    if located["home"] is None:
        return f"the refreshed {agent} login was not written back: {one_line(located['home_why'] or 'no HOME')}"
    sessions = inspect().module("sessions")
    try:
        with sessions.session_lock(located["id"]):
            located = inspect().locate(located["id"])
            if located["state"] in RUNNING or located["home"] is None:
                return f"the refreshed {agent} login was not written back to {where}: it was started meanwhile"
            return merge_back(agent, Path(located["home"]), data, state, where)
    except ValueError as exc:
        return f"the refreshed {agent} login was not written back to {where}: {one_line(exc)}"


def merge_back(agent, source_home, data, state, where):
    """write_back's merge, under the session's lock: DATA (the run's login file) into SOURCE_HOME's."""
    l = launcher()
    target = l.AUTH_TARGETS[agent]
    try:
        current = read_file(source_home, target, LOGIN_LIMIT)
    except (OSError, ValueError) as exc:
        return f"the refreshed {agent} login was not written back to {where}: {one_line(exc)}"
    if hashlib.sha256(current).hexdigest() != state["source"]:
        return f"the {agent} login in {where} changed since it was copied: kept its own"
    if agent == "claude":
        new, old = l.claude_oauth_from(data), l.claude_oauth_from(current)
        if new is None or old is None or not (old["expiresAt"] < new["expiresAt"]
                                              and new["expiresAt"] / 1000 <= time.time() + l.OAUTH_MAX_LIFETIME):
            return None
        accounts = state.setdefault("accounts", {})
        mine, theirs = (l.claude_token_account(accounts, old, final=True),
                        l.claude_token_account(accounts, new, final=True))
        if not (isinstance(mine, (tuple, list)) and isinstance(theirs, (tuple, list)) and tuple(mine) == tuple(theirs)):
            return (f"the refreshed Claude login was not written back to {where}: its account could not be checked "
                    "to be the same")
        content = l.merge_claude_login(current, new)
    else:
        try:
            stored, sandbox = json.loads(current), json.loads(data)
        except (ValueError, RecursionError):
            return None
        merged = l.merge_refreshed(agent, stored, sandbox) if isinstance(stored, dict) else None
        if merged is None:
            return None
        content = json.dumps(merged, indent=2).encode()
    l.replace_home_file(str(source_home), target, content)
    return f"refreshed {agent} login written back to {where}"


# --- a login given to a sandbox that runs (sandbox-ai login give) -----------------------------------------------------

# Whether the agent reads its login file again by itself while it runs (else it must be started again in its
# sandbox: sandbox-ai kits restart-agent). Claude Code reads ~/.claude/.credentials.json again before a request
# (what the launcher's own sync of a running sandbox relies on, agent_sandbox.claude_login_sync).
READS_AGAIN = {"claude": True}
GIVE_STATES = ("running", "paused")


def companions(located):
    """The agents the start LOCATED runs beside its own (--with A,B of its kept options), each once."""
    options = []
    if located.get("directory"):
        with contextlib.suppress(OSError, ValueError, RecursionError):
            data = json.loads((Path(located["directory"]) / "session.json").read_text())
            options = data.get("options") if isinstance(data, dict) and isinstance(data.get("options"), list) else []
    found, index = [], 0
    while index < len(options):
        flag, sep, joined = str(options[index]).partition("=")
        if flag in ("--with", "-wa"):
            value = joined if sep else str(options[index + 1]) if index + 1 < len(options) else ""
            found += [part.strip() for part in value.split(",") if part.strip()]
            index += 0 if sep else 1
        index += 1
    return list(dict.fromkeys(found))


def make_home_dirs(home, relative):
    """The folders that hold RELATIVE in the sandbox HOME, made where they are missing (0700) - each step through a
    directory descriptor that follows no link (the agent may swap a folder for a link while it runs). ValueError for
    a link or a non-directory on the way."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = Path(relative).parts[:-1]
    if any(part in ("..", "/") for part in Path(relative).parts):
        raise ValueError(f"path outside the sandbox HOME: {relative}")
    fd = os.open(home, flags)
    try:
        for part in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            try:
                following = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(f"~/{relative}: a link or no folder on the way (not followed)") from None
                raise
            os.close(fd)
            fd = following
    finally:
        os.close(fd)


def give_source(agent, from_session, target):
    """(what it is called, its login files (stored_form), describe()) of the login to give: the central one, or the
    session FROM_SESSION's (not TARGET's own). ValueError: none there."""
    if from_session is None:
        files = central_files(agent)
        if files is None:
            raise ValueError(f"no central {agent} login is stored (sandbox-ai login {agent}); give another session's "
                             "with --from-session NAME (sandbox-ai login --status --sessions)")
        try:
            return "the central login", files, describe(agent, files)
        except ValueError as exc:
            raise ValueError(f"the central {agent} login is not usable ({one_line(exc)}): sandbox-ai login "
                             f"{agent}") from None
    if not isinstance(from_session, str) or not NAME.fullmatch(from_session):
        raise ValueError(f"not a start's name: {one_line(from_session)[:80]}")
    named = inspect().locate(from_session)
    if named["id"] and named["id"] == target["id"] or (
            named["home"] and target["home"] and Path(named["home"]).resolve() == Path(target["home"]).resolve()):
        raise ValueError(f"{one_line(target['name'])} is the session the login is taken from: it has it")
    located, files, info = session_login(from_session, agent)
    return f"the login of session {one_line(located['name'])}", files, info


def give_target(run, agent=None):
    """(located start, the agent whose login it takes) of the running start RUN. ValueError names the start: not
    running, another agent's, no HOME on this machine."""
    located = inspect().locate(run)
    shown = one_line(located["name"])
    if located["state"] not in GIVE_STATES:
        raise ValueError(f"{shown} does not run ({one_line(located['state'])}): a login is given to a sandbox that "
                         f"runs. A stopped one takes the central login at its next start by itself, or another "
                         f"session's: sandbox-ai resume {shown} --auth-from-session NAME")
    agent = agent or located["agent"]
    if agent not in AGENTS:
        raise ValueError(f"{shown} runs {one_line(located['agent'])}: sandbox-ai gives no login of it (only "
                         f"{', '.join(AGENTS)})")
    if agent != located["agent"] and agent not in companions(located):
        raise ValueError(f"{shown} is a {one_line(located['agent'])} session and runs no {agent} beside it (--with): "
                         f"it takes no {agent} login")
    if located["home"] is None:
        raise ValueError(f"{shown}: {one_line(located['home_why'] or 'its HOME is not on this machine')}; a login "
                         "cannot be put into it from here")
    return located, agent


def give_preview(run, agent=None, from_session=None):
    """What `login give RUN` would do, nothing written: {"agent", "run": {name, id, state}, "source", "login":
    describe, "current": describe of the login RUN has now or None, "same": it has that very login,
    "same_account", "reads_again", "restart": whether its agent can be started again inside it}."""
    target, agent = give_target(run, agent)
    source, files, info = give_source(agent, from_session, target)
    current = None
    with contextlib.suppress(FileNotFoundError, ValueError):
        current = describe(agent, read_login(Path(target["home"]), agent))
    same = bool(current and current["fingerprint"] == info["fingerprint"])
    return {"agent": agent, "run": {"name": target["name"], "id": target["id"], "state": target["state"]},
            "source": source, "login": public(info), "current": public(current), "same": same,
            "same_account": same or same_account(info, current), "reads_again": bool(READS_AGAIN.get(agent)),
            "restart": can_restart(target)}


def can_restart(target):
    """Whether `kits restart-agent` can start the agent of the running start TARGET again inside its sandbox."""
    with contextlib.suppress(Exception):  # noqa: BLE001 - asked only to say what comes next
        return bool(module("kits_live").listing(target["name"]).get("restart"))
    return False


def give_lines(result):
    run = result.get("run") or {}
    lines = [f"{str(result.get('source') or 'The login')[:1].upper()}{str(result.get('source') or 'the login')[1:]} "
             f"({result.get('agent')}):", *info_lines(result.get("login") or {})]
    shown = one_line(run.get("name"))
    if result.get("same"):
        lines.append(f"{shown} has this login already.")
    elif result.get("current"):
        lines.append(f"It replaces the login {shown} ({one_line(run.get('state'))}) has now" + (
            " of the same account:" if result.get("same_account") else " of ANOTHER account (or one that cannot be "
                                                                        "told):"))
        lines += info_lines(result["current"])
    else:
        lines.append(f"{shown} ({one_line(run.get('state'))}) has no {result.get('agent')} login now.")
    return lines


def give_login(run, agent=None, from_session=None, expect=None, restart=False):
    """Put the login (central, or FROM_SESSION's) into the HOME of the running start RUN, only when its fingerprint
    is EXPECT (when given). Returns {"given", "agent", "run", "name", "source", "login", "replaced", "reads_again",
    "restart_needed", "restarted", "notes"}; never a token. RESTART: start its agent again inside the sandbox when
    it does not read the login by itself (kits restart-agent)."""
    if expect is not None and not FINGERPRINT.fullmatch(str(expect)):
        raise ValueError("--expect is the 8 hex characters of a login's fingerprint")
    l = launcher()
    target, agent = give_target(run, agent)
    shown = one_line(target["name"])
    source, files, info = give_source(agent, from_session, target)
    if expect is not None and info["fingerprint"] != expect:
        raise ValueError(f"{source} changed since it was shown (fingerprint {info['fingerprint']} now, not {expect}): "
                         "look at it again")
    home = Path(target["home"])
    main = l.AUTH_TARGETS[agent]
    current, data = None, None
    with contextlib.suppress(FileNotFoundError, ValueError):
        data = read_file(home, main, LOGIN_LIMIT)
        current = describe(agent, read_login(home, agent))
    reads_again = bool(READS_AGAIN.get(agent))
    result = {"given": False, "agent": agent, "run": target["id"], "name": target["name"], "source": source,
              "login": public(info), "replaced": public(current), "reads_again": reads_again,
              "restart_needed": False, "restarted": False, "notes": []}
    if current and current["fingerprint"] == info["fingerprint"]:
        result["notes"].append(f"{shown} has this login already")
        return result
    sessions = inspect().module("sessions")
    lock = sessions.session_lock(target["id"]) if target.get("directory") and target.get("id") else \
        contextlib.nullcontext()
    with lock:  # not while it is being started or stopped (its HOME is being prepared or packed)
        again = inspect().locate(target["id"] or target["name"])
        if again["state"] not in GIVE_STATES or again["home"] != target["home"]:
            raise ValueError(f"{shown} ended or was restarted meanwhile: nothing was given")
        for relative in sorted(files, key=lambda path: path == main):  # (Claude's account state first)
            content = files[relative]
            if agent == "claude" and relative == main:
                # The login alone: what else its credentials file holds stays (the OAuth tokens of its MCP servers).
                content = l.merge_claude_login(data, l.claude_oauth_from(files[relative]))
            elif agent == "claude":
                # ~/.claude.json is Claude's own state while it runs (its projects, its settings): only a HOME that
                # has none gets the account subset; an existing one is left to Claude, which asks its account anew.
                with contextlib.suppress(FileNotFoundError, ValueError):
                    read_file(home, relative, STATE_LIMIT)
                    continue
            make_home_dirs(str(home), relative)
            l.replace_home_file(str(home), relative, content)
    result["given"] = True
    with contextlib.suppress(Exception):  # noqa: BLE001 - the launcher's log: names and a fingerprint, never a token
        l.LOG.info("login", f"give: the {agent} login {info['fingerprint']} ({source}) into the HOME of {shown} "
                            f"(run {target.get('id')})")
    if info["kind"] != "api-key":
        result["notes"].append(
            f"{shown} and {'every sandbox with the central login' if from_session is None else source} now share "
            "this login: subscription logins rotate their refresh tokens, and two sandboxes that refresh the same "
            "one can log each other out (log in again then: sandbox-ai login " + agent + ")")
    if reads_again:
        result["notes"].append(f"{agent} reads its login file again before its next request: nothing to restart")
        return result
    result["restart_needed"] = True
    if restart:
        try:
            module("kits_live").restart_agent(target["name"], out=lambda line: None)
            result["restarted"] = True
        except Exception as exc:  # noqa: BLE001 - the login is given; said, with the way that works
            result["notes"].append(f"its agent was not started again ({one_line(exc)[:300]})")
    if not result["restarted"]:
        result["notes"].append(f"{agent} reads its login when it starts: start it again inside the sandbox (sandbox-ai "
                               f"kits restart-agent {shown}; its conversation continues), or restart the start "
                               f"(sandbox-ai resume {shown} --restart)")
    return result


def give_parser():
    """The parser of `sandbox-ai login give` (its own: `login` hands the launcher's options on; also what completes
    the command, completion.OWN_PARSERS)."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="sandbox-ai login give", allow_abbrev=False,
        description="Give the running sandbox RUN (its start's name or run id, sandbox-ai list) an agent login now: "
                    "the central login of its agent (sandbox-ai login AGENT), or with --from-session NAME the login "
                    "another session has in its HOME. It is put into RUN's HOME as a start puts it there (checked, "
                    "mode 0600, never through a link); no token is shown, logged or passed as an argument. Claude "
                    "Code reads it again by itself; the other agents must be started again inside the sandbox "
                    "(asked, or --restart: sandbox-ai kits restart-agent RUN).")
    parser.add_argument("run", metavar="RUN")
    parser.add_argument("agent", nargs="?", choices=AGENTS, metavar="AGENT",
                        help=f"whose login (default: the agent RUN runs): {', '.join(AGENTS)}")
    parser.add_argument("--from-session", metavar="NAME",
                        help="the login of this session (sandbox-ai login --status --sessions) instead of the central "
                             "one")
    parser.add_argument("--yes", action="store_true", help="give it without asking")
    parser.add_argument("--restart", action="store_true",
                        help="start the agent again inside the sandbox when it does not read the login by itself "
                             "(sandbox-ai kits restart-agent RUN; its conversation continues)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be given, give nothing")
    parser.add_argument("--expect", metavar="FINGERPRINT", help="give only the login with this fingerprint (the one "
                                                                "shown)")
    parser.add_argument("--json", action="store_true", help="as JSON, for programs")
    parser.add_argument("--host", metavar="HOST", help="on this host of sandbox-ai hosts (over SSH): its run, its "
                                                       "logins")
    return parser


def cli_give(argv, out=print, ask=None):
    """sandbox-ai login give RUN [AGENT] [--from-session NAME]: shown, asked (or --yes), given; then whether the
    agent takes it by itself or must be started again (asked, or --restart)."""
    a = give_parser().parse_args(argv)
    if a.expect is not None and not FINGERPRINT.fullmatch(a.expect):
        raise ValueError("--expect is the 8 hex characters of a login's fingerprint")
    host = None if local_host(a.host) else a.host
    words = [a.run, *([a.agent] if a.agent else []), *([f"--from-session={a.from_session}"] if a.from_session else [])]
    shown = give_preview(a.run, a.agent, a.from_session) if host is None else \
        remote(host, ["give", *words, "--dry-run", "--json"])
    login = shown.get("login") if isinstance(shown.get("login"), dict) else {}
    run = shown.get("run") if isinstance(shown.get("run"), dict) else {}
    where = "" if host is None else f" on {one_line(host)}"
    if a.expect is not None and login.get("fingerprint") != a.expect:
        raise ValueError(f"the login changed since it was shown (fingerprint {one_line(login.get('fingerprint'))} now, "
                         f"not {a.expect}): look at it again")
    if a.dry_run or not a.json:
        if a.json:
            out(json.dumps(shown, indent=2))
        else:
            for line in give_lines(shown):
                out(line)
        if a.dry_run:
            return 0
    if shown.get("same"):
        out(json.dumps({"given": False, "agent": shown.get("agent"), "name": run.get("name"), "login": login,
                        "notes": ["it has this login already"]}, indent=2) if a.json else
            "Nothing to do: it has this login already.")
        return 0
    name = one_line(run.get("name") or a.run)
    if not a.yes:
        if not confirm(f"Give this login to {name}{where}? Its {shown.get('agent')} works as that account from now "
                       "on.", ask):
            out("Not given.")
            return 1
    restart = a.restart
    if not restart and not a.yes and not shown.get("reads_again") and shown.get("restart"):
        # Asked first, as `kits attach` asks: starting the agent again ends what it does now.
        restart = confirm(f"{shown.get('agent')} reads its login when it starts. Start it again inside {name} now "
                          "(its conversation continues; what it does right now ends)?", ask)
    expect = login.get("fingerprint")
    if host is None:
        result = give_login(a.run, a.agent, a.from_session, expect=expect, restart=restart)
    else:
        result = remote(host, ["give", *words, "--yes", f"--expect={expect}", *(["--restart"] if restart else []),
                               "--json"], timeout=240)
    if a.json:
        out(json.dumps(result, indent=2))
        return 0
    if not result.get("given"):
        out("Nothing to do: " + "; ".join(one_line(note) for note in result.get("notes") or ["not given"]) + ".")
        return 0
    out(f"Gave {one_line(result.get('source'))} ({result.get('agent')}, fingerprint {one_line(expect)}) to "
        f"{name}{where}" + ("; its agent was started again" if result.get("restarted") else ""))
    for note in result.get("notes") or []:
        out(f"note: {one_line(note)}")
    return 0


# --- the command line (login.py's --status --sessions and --from-session) ---------------------------------------------

def remote(host, args, timeout=120):
    """The JSON `sandbox-ai login ARGS` prints on HOST (over SSH); ValueError with what it said."""
    status, out, err = inspect().module("hosts").run(host, ["login", *args], timeout=timeout)
    if status != 0:
        text = one_line((err or out).strip()[-600:])
        if "unrecognized arguments" in text or "invalid choice" in text:
            text = f"the sandbox-ai on {host} is older and cannot do this: update it (sandbox-ai hosts update {host})"
        raise ValueError(f"{one_line(host)}: {text.removeprefix('sandbox-ai login: ') or f'exit status {status}'}")
    try:
        data = json.loads(out)
    except (ValueError, RecursionError):
        raise ValueError(f"{one_line(host)}: sandbox-ai login printed no JSON") from None
    if not isinstance(data, dict):
        raise ValueError(f"{one_line(host)}: not what sandbox-ai login says")
    return data


def local_host(host):
    return host in (None, "", "local")


def cli_sessions(a, out=print):
    result = sessions() if local_host(a.host) else remote(a.host, ["--status", "--sessions", "--json"])
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in sessions_lines(result):
            out(line)
    return 0


def confirm(question, ask=None):
    """Whether the human says yes to QUESTION on the terminal (ASK: a function of the question, for tests)."""
    if ask is not None:
        return ask(question)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise ValueError("there is no terminal to ask: add --yes to import it")
    print(question + " [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes", "j", "ja")


def cli_import(a, out=print, ask=None):
    """sandbox-ai login AGENT --from-session NAME: shown, asked (or --yes), imported."""
    if a.expect is not None and not FINGERPRINT.fullmatch(a.expect):
        raise ValueError("--expect is the 8 hex characters of a login's fingerprint")
    host = None if local_host(a.host) else a.host
    name = a.from_session
    shown = preview(a.agent, name) if host is None else remote(host, [a.agent, "--from-session", name, "--dry-run",
                                                                      "--json"])
    login = shown.get("login") if isinstance(shown.get("login"), dict) else {}
    if a.expect is not None and login.get("fingerprint") != a.expect:
        raise ValueError(f"the login of {one_line(name)} changed since it was shown (fingerprint "
                         f"{one_line(login.get('fingerprint'))} now, not {a.expect}): look at it again")
    if a.dry_run:
        if a.json:
            out(json.dumps(shown, indent=2))
        else:
            for line in preview_lines(shown):
                out(line)
        return 0
    if not a.json:
        for line in preview_lines(shown):
            out(line)
    if shown.get("same"):
        result = {"imported": False, "agent": a.agent, "session": name, "login": login, "replaced": None,
                  "notes": ["it is the central login already"]}
        out(json.dumps(result, indent=2) if a.json else "Nothing to do: it is the central login already.")
        return 0
    if shown.get("needs_replace") and not a.replace:
        raise ValueError(f"the central {a.agent} login is of another account, or its account cannot be told: add "
                         "--replace to replace it")
    if not a.yes:
        where = "" if host is None else f" on {one_line(host)}"
        question = (f"Import this login as the central {a.agent} login{where}? Every {a.agent} sandbox started from "
                    "now on uses it.")
        if not confirm(question, ask):
            out("Not imported.")
            return 1
    expect = login.get("fingerprint")
    if host is None:
        result = import_login(a.agent, name, expect=expect, replace=a.replace)
    else:
        result = remote(host, [a.agent, "--from-session", name, "--yes", f"--expect={expect}",
                               *(["--replace"] if a.replace else []), "--json"], timeout=180)
    if a.json:
        out(json.dumps(result, indent=2))
        return 0
    if not result.get("imported"):
        out("Nothing to do: " + "; ".join(one_line(note) for note in result.get("notes") or ["not imported"]) + ".")
        return 0
    where = "" if host is None else f" on {one_line(host)}"
    out(f"Imported the {a.agent} login of {one_line(name)} as the central login{where} (fingerprint "
        f"{one_line(expect)}): every {a.agent} sandbox started from now on uses it")
    for note in result.get("notes") or []:
        out(f"note: {one_line(note)}")
    return 0
