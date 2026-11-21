# Credentials: edited in place, and given to sandboxes that run - here and on another host (2026-11-19)

Questions (user, 2026-11-19):

1. "check in-depth if Zugangsdaten/logins/creds can really be added to sessions, also remote sessions, via right
   mouse at Zugangsdaten"
2. "wieso kann man bei 'Zugänge' eingetragene Creds nicht editieren??"

Branch `feature/creds-give` (from main `c1ae1708`, 0.1.137, with `review/creds-verify-a` and `review/creds-verify-b`
merged). This file says what worked before, what did not, what was built, what was decided (the user was away:
security first) and what stays out, with the test of each claim. The two independent verifications are
`credentials-verify-A.md` and `credentials-verify-B.md`; their open defects are closed here (section 3).

## 1. What worked before (verified, not changed in substance)

| What | Where | Evidence |
|---|---|---|
| A Git login given to a running sandbox on this machine or on the host chosen in the page's host chooser (that host's secrets for that host's runs, over SSH) | CLI `git-logins give`, app Git logins tab (button and right click), web Network card | `tests/test_credentials_verify_a.py`, `_b.py`; app: `test_credentials_verify_a_app.py`, `_b_app.py` |
| A secret given as an API key (or, for a Git server's host, as a Git login) | CLI `secrets give`, app Secrets tab (button and right click), web Network card | the same, and `projects/sandbox-ai-app/tests/test_credentials_give_e2e.py` |
| Every list's buttons on its right-click menu (`CredentialTab.set_list` -> `list_menu`) | app | `tests/test_list_menus.py` |

What "worked" hid: where the Git login's token went (section 3), and that the kinds below could not be given at all.

## 2. What did not work, and what was built

### 2.1 Entries could not be edited (the first item)

Every tab had Add and Remove only (identities had Edit…): changing a user or a server meant removing and adding
again, and a secret's name could not be changed at all.

Built:

| Kind | Command line | App (button, right click, double click) | Web |
|---|---|---|---|
| Git login | `git-logins edit URL [--url NEW] [--user USER] [--secret NAME]` | Edit…: the Add dialog filled in | Logins: Edit… on its row |
| Publish login | `publish-logins edit KIND [--secret NAME] [--url URL\|default]` | Edit… (the kind is fixed: it names the login) | Logins: Edit… on its row |
| Secret | `secrets rename OLD NEW`; a value: `secrets set NAME` as before | Edit…: name, and Replace value… (hidden input) | Secrets: Edit… (rename); a value by adding the name again, asked first, as before |
| VPN profile | `vpn set NAME --provider/--auth-secret/--account-secret/--auth-key-secret/--exit-node/--login-server/--hostname/--exit-country` (and Tor's bootstrap, as before) | Edit…: the fields of `vpn show --json` "settings" | VPN: Edit… on its row |
| Identity | `identities set`, `login`, `publish` (existed) | Edit… existed; a double click opens it now | as before |
| Agent login | none | none | none |

Rules that hold for all of them:

- **A secret's value is never shown, read back or filled in.** The edit dialogs get names only (a login's secret
  is a name; `vpn show --json` "settings" holds names of secrets, hosts, countries). Replace value… is a field
  typed into, hidden as Add's, passed on standard input (`secrets set NAME`), never an argument. Tests:
  `test_a_git_login_is_edited_in_its_dialog_and_no_token_is_ever_shown`,
  `test_replace_value_takes_a_new_token_on_standard_input_and_changes_nothing_else`,
  `test_a_secret_is_renamed_and_its_value_replaced_without_ever_being_shown`
  (`projects/sandbox-ai-app/tests/test_credentials_edit_give_app.py`).
- **A rename is carried through what sandbox-ai keeps** (`sandbox/credrefs.py`): the Git logins, the publish
  logins, the identities, VPN profiles (the secrets they name, not the keys they keep under their own names), the
  options of the trusted project files (`--git-token-secret URL=NAME`, `--secret-env [VAR=]NAME`, `--api-key
  HOST=VAR[=NAME]`, `--mcp-env SERVER:VAR@NAME`, long and short forms; the variable the agent sees keeps its name)
  and of the kept starts (under the start's lock). **It says what still names the old one**: an LLM endpoint, an
  MCP server (their stores are not rewritten by this command), a project file that changed since you trusted it
  (an agent may have changed it: never rewritten, never trusted along), a start that is being started or stopped.
  Tests: `tests/test_credentials_edit.py` (20 tests, through `./sandbox-ai`).
- **A changed address** (`git-logins edit --url`): what else names the old server is said (an identity's login, a
  project file's or kept start's `--git-token-*` option). Those are other logins for that server, so they are
  named, not changed.
- **A change is for the starts from then on.** `git-logins edit` names the sandboxes that run and the command that
  gives one the login as it is now; the app asks whether to give it to a running one
  (`test_an_edited_git_login_names_the_sandboxes_that_run_and_how_to_give_it`). A publish login and a VPN profile
  say that a running sandbox needs a restart.
- Every change in the app and the web is asked first (policy keys `login-edit`, `secret-rename`, `secret-replace`,
  `vpn-replace`).

Decided: **agent logins get no Edit…** - an agent's login is its own OAuth state or key file; there is no field
of it to edit, and Log in… replaces it. A VPN profile's key or configuration file is replaced by adding the
profile again (`vpn add NAME ... --force`), not by an edit: the edit never touches key material.

### 2.2 Agent logins could not be given to a running session (gap 1)

Only "Use for a new start" and "Import as central login" existed. Built: `sandbox-ai login give RUN [AGENT]
[--from-session NAME] [--yes] [--restart] [--dry-run] [--expect FINGERPRINT] [--json] [--host HOST]`
(`sandbox/session_login.py`):

- The source is the central login of the run's agent, or with `--from-session NAME` the login in another session's
  HOME - read as a start reads it (no link followed, size limits, the structure checks of `sandbox-ai login`).
- It is written into the running sandbox's HOME as a start writes it: the same checked form, the same files, mode
  `0600`, folders made `0700` through directory descriptors, the file written beside its target and renamed over
  it - never through a link (the agent may be writing its HOME, or have planted one:
  `test_login_give_refuses_naming_the_run_and_changes_nothing`). Claude's `.credentials.json` keeps what else it
  holds (its MCP servers' OAuth tokens); a running Claude's `~/.claude.json` is left to Claude
  (`test_a_claude_login_is_merged_into_its_credentials_file_and_read_again_by_itself`).
- Shown first (account as the file says, fingerprint, whether it replaces a login of ANOTHER account), then asked;
  `--expect` gives only the login that was shown (what the app and the web pass after their question).
- **Whether the agent picks it up**: Claude Code reads its login file again before a request (what the launcher's
  own sync of running sandboxes already relies on). Codex, OpenCode, Hermes, Qwen Code and Pi read theirs when
  they start: the agent is started again inside the sandbox - asked first on a terminal, `--restart`, or the
  app's tick box - with `kits restart-agent`; when that is not possible (no tmux window of its own) the login is
  given all the same and the note names the way that works.
- No token in any output, command line or log: the launcher's log line holds names and the fingerprint. The
  central store never takes another account from a sandbox (its write-backs only merge refreshed tokens of the
  store's own account: `test_another_sessions_login_is_given_and_the_central_store_stays_as_it_is`).
- `--host HOST` runs it on that host with that host's logins (`test_a_login_is_given_to_a_sandbox_on_another_host`).
- App: Give to a running sandbox… on the agent logins list and on the logins of the sessions (right click
  mirrored); web: on the Logins page, both lists. (The agent logins row lost its own Refresh to make room: the
  Refresh of the sessions' logins below reads both lists, and the list's right click has it.)

Decided: a login of AGENT is given to a run of that agent, or to one that runs AGENT beside it (`--with`); a
start that does not run is refused with what brings it the login at its next start. A login is copied between
sessions **of one host** only: an agent login never leaves its machine through this command (unlike a secret,
2.3, there is no "send a login to another host" - logins rotate, and two machines refreshing one login log each
other out; `sandbox-ai login AGENT` on that host is the way).

### 2.3 A secret or Git login of this machine could not reach a session on another host (gap 2)

Decided: **allowed, as a step of its own that is asked first every time** - not refused, and not folded silently
into Give. Why:

- For: the transport exists and is the one already used to keep a secret on a host typed in the app (`hosts run
  HOST -- secrets set NAME`, the value on standard input); refusing would push people to copy tokens by hand,
  through shells and histories.
- Against doing it silently: a credential leaves this machine **and stays** on another one (its secret store),
  under the control of whoever controls that host. That is the human's decision each time, said in the question.

Built: `sandbox-ai secrets send NAME HOST [--as NAME] [--replace] [--yes]`. The value is read from this machine's
store into the process and written to the standard input of `sandbox-ai secrets set` on HOST over its SSH
connection: never an argument (here or there), never a temporary file, never printed or logged
(`test_a_secret_of_this_machine_is_sent_to_another_host_and_given_there` scans every command line of both hosts
and every file of both for the value). A secret of that name there is replaced only with `--replace` (the app
asks a second question). Then it is given there as any secret of that host. App: Give to a sandbox on another
host… for a secret and for a Git login - on the list's right click, and as "A sandbox on another host…" in the
Give dialog, where one notices that the sandbox meant is not listed (not in the button row: with it the page no
longer fitted a small window in German) - host chosen, asked, sent, then the usual Give dialog with that host's
sandboxes; the value never passes through the app (`sandbox-ai` itself reads and sends it). Web: Send to another
host… on the Secrets page.

Not built: giving a secret to a remote run **without** keeping it in that host's store (a pass-through that ends
with the run). It would be the smaller footprint, but `--keep` and a later `revoke`/`give` need the name on that
host, and the remote give commands take names, not values on standard input. Listed as a possible follow-up.

### 2.4 Publish logins, identities, VPN profiles (gap 3)

Decided: **not given while a sandbox runs**, and said so where one looks:

- A **publish login** is more than a token: its tools' variables (TWINE_PASSWORD ...) are set when the sandbox
  starts, and - the part that matters for safety - the questions before every upload are rules of that start
  (the ruleset "publish", which a run-time rules reload does not replace). Giving the token alone would publish
  without those questions. `publish-logins give RUN KIND` therefore gives nothing: it names the run, the restart
  that brings the login, and the explicit way to give the token alone as an API key (`secrets give`), which the
  human then chooses knowingly.
- An **identity** is chosen at the start (commit name, its Git logins instead of the default ones, its publish
  logins). One of its Git logins can be given as any Git login.
- A **VPN** is the sandbox's network before the agent starts (tunnel and kill switch in the pod's namespace).

Their Give to a running sandbox… button is there on each tab, greyed out, with the reason and what to do in its
tooltip (app and web).

### 2.5 What a sandbox was given could not be seen or taken back in the app (verification B, finding 5)

Built: the Credentials page's tab **Given to sandboxes** - every running sandbox of the chosen host with the Git
logins and API keys it was given, where each token is (with its proxy / in the sandbox), Revoke… (asked first,
several at once). Sandboxes › Secrets and logins… opens it for the selected running start. Approvals › Allowed
now lists and revokes them too.

## 3. The open defects of the two verifications

| Finding | Fix | Test (was a strict xfail) |
|---|---|---|
| A1: a Git login given during a run was ignored where the proxy already had one | the supervisor hands the given login to the proxy (control op `git-logins`), which uses it before the start's | `test_a_login_given_during_the_run_wins_over_the_proxys_login_for_that_server` (now end to end through `./sandbox-ai`) |
| A2 = B6: the token was written into `/sandbox/session/git-tokens.json`, readable by the agent | the login goes to the proxy; nothing in the sandbox | `test_a_git_login_given_while_it_runs_stays_out_of_the_sandbox`; app: `test_the_question_before_a_git_login_is_given_says_that_the_agent_can_read_its_token` |
| A3 = B4: an API key given during a run could not be taken back in the web | `POST /net/RUN` `op: revoke` takes `api_key: {name}`; the row has Revoke | `test_the_web_takes_back_an_api_key_it_gave[local,ct1]`, `test_the_web_interface_takes_back_an_api_key_given_during_the_run` |
| B5: nor in the app | Given to sandboxes, and the rows of Allowed now | `test_what_a_run_was_given_can_be_taken_back_in_the_window` |
| Both: the relative `--file --keep` path | ONE fix kept (`net.named_token_files`, B's; A's `kept_token_file` removed) | both regression tests green |

### The design decided for A2 = B6

A login given during a run goes where a login of `sandbox-ai git-logins` goes at a start: to the run's connection
proxy. `net.py` reads the token on the host and sends the whole list of given logins to the supervisor (control
op `git-logins`, modelled on `api-keys`); the supervisor writes `git-logins-added.json` into the proxy's run
directory (`0600`, owned by the proxy's user, which the agent cannot read); `ProxyServer.git_token` looks at
those entries before `proxy.json`'s. The run's own list - what `revoke` and the listing start from - is
`git-logins.json` in the run directory on the host (`0600`; the directory that holds the control socket, hidden
from the agent), exactly as `api-keys.json` is. Both go with the run. The supervisor checks each entry's form
(nothing that could break out of a Basic login's header) and the policy again (a request written by hand reaches
its socket too).

Consequences, all intended:

- The given login **wins** over the start's for its server (A1), and is the start's again once revoked.
- A run started with `--token-proxy` takes a login during the run (it refused all before).
- A remote whose URL names another user gets the proxy's login too (as with every login of the list); with the
  helper it did not.
- **The helper path stays only where the proxy cannot log in**: a run without the connection proxy, a server it
  does not inspect (`--no-inspect`), a run started by an older sandbox-ai (its record lacks `proxy_git_logins`).
  There the give is **refused** - naming the run and saying that the only other way puts the token where the
  agent can read it - unless the human says so: `--in-sandbox` (`net allow --git-token-in-sandbox`). The app and
  the web ask a second question that says it; the outcome, `net show` and the listings mark such a login.

The two xfail tests that modelled the old storage were rewritten to the new one and say so in their docstrings
(A1's unit test monkeypatched the sandbox's file; it now runs a start with a read-only registry login, gives a
push login through `./sandbox-ai` and asks the real `ProxyServer.git_token`). Tests of both reviews that asserted
the token in `git-tokens.json` assert the proxy's file and an empty session directory instead; B's push test
pushes with the real `git` through the real proxy over TLS.

## 4. Every give path and its test

| Path | Here | Another host |
|---|---|---|
| Git login | `test_credentials_verify_a.py`, `_b.py` (a real push through the proxy), `test_net.py`, `test_credentials_given.py` | `test_a_login_and_a_key_given_on_another_host_reach_that_hosts_run`, `test_credentials_for_a_session_on_another_host_go_to_that_host_and_stay_there` |
| Git login into the sandbox (`--in-sandbox`) | `test_a_git_login_into_the_sandbox_is_given_only_on_the_humans_word` (a real push), `test_git_logins_give_to_a_run_without_the_proxy_wants_the_humans_word` | the same command over `hosts run` |
| API key | the verifications | the verifications |
| Agent login | `test_the_central_login_is_given_to_a_sandbox_that_runs`, `test_another_sessions_login_...`, `test_a_claude_login_...` | `test_a_login_is_given_to_a_sandbox_on_another_host` |
| This machine's secret / Git login for another host | - | `test_a_secret_of_this_machine_is_sent_to_another_host_and_given_there`, `test_a_git_login_of_this_machine_reaches_a_session_on_another_host` |
| Publish login (refusal) | `test_a_publish_login_is_not_given_to_a_running_sandbox_and_the_refusal_names_it` | - |
| App (window) | `test_credentials_edit_give_app.py` (17), `test_credentials_give_e2e.py`, the verifications' app tests | the verifications' app tests (ssh as the app builds it) |
| Web | `test_web_matrix.py` ACTIONS (every new route in every login state on both hosts), `test_credentials_verify_a_web.py`, B's web tests | `[ct1]` cases |

Refusals name the run ("RUN (NAME)") in every path: not running, another agent's, no proxy, no central login, a
supervisor that does not know the request, a publish login.

## 5. Proven only with stand-ins, and untested

As in the two verifications: Podman, the firewall, DNS and ssh are stood in for; that the agent cannot read the
proxy's run directory and the run directory is the live sandbox's property (Firejail), not shown here. In
addition:

- `kits restart-agent` after a login give: the fakes have no tmux session, so the tests cover "not possible:
  said, with the way" and the app's and web's passing of `--restart`; the restart itself is `kits_live`'s own
  tests.
- That Codex, OpenCode, Hermes, Qwen Code and Pi need a restart to read a new login file, and that Claude Code
  does not, is what their behaviour and the launcher's existing sync say; no agent was run.
- macOS (podman machine): the control op goes through `podman exec` as `api-keys` does; not run.
- The web page's new forms (app.js) are checked by `node --check`, the i18n test and the routes' tests; no
  browser drove them.

## 6. Decisions taken (user away), in one list

1. A Git login given during a run goes to the proxy; into the sandbox only where the proxy cannot, and only with
   `--in-sandbox` / a second yes.
2. The run's own list of given logins holds the tokens in its run directory, as `api-keys.json` does (needed to
   put a list back as it was when a later step fails; hidden from the agent like the control socket beside it).
3. Cross-host: allowed as `secrets send`, asked first every time, kept in that host's store; no silent send.
4. Publish logins, identities and VPN profiles are not given while a sandbox runs; the buttons are there, greyed
   out with the reason.
5. Agent logins: no Edit…; given only within one host; the lead agent's or a `--with` agent's.
6. A rename rewrites sandbox-ai's own lists, VPN profiles, trusted project files (only while unchanged) and kept
   starts; it names, and does not rewrite, LLM endpoints, MCP servers and changed project files.
7. Messages name no version number for "an older sandbox-ai" (the release this lands in is the coordinator's).
8. A security mode's or the policy's refusal of `--git-token-*` holds for the control op too.

## 7. Not done, and why

- **Publish login mid-run with its questions** (adding the "publish" ruleset to a running sandbox): the rules a
  start fixes are deliberately not replaced by a run-time reload; doing it safely is a change to the rules
  engine, not to credentials.
- **A pass-through secret for a remote run** (2.3).
- **Renaming an identity or a VPN profile**: their names are referenced by start options (`--identity`, `--vpn`)
  in presets, project files and kept starts; `secrets rename`'s machinery would carry over, but it was not asked
  for and is a change of its own.
- **Rewriting LLM endpoints and MCP servers on a secret's rename**: named instead (their stores have their own
  commands and checks).
- **The app's Board and wizard** as entry points for editing: unchanged.
