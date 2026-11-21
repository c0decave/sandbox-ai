# Credentials verification B (2026-11-19)

Question (user, 2026-11-19): can credentials, Git logins and the rest really be handed to sessions cleanly and
attached while they run - on the command line and in the app?

Reviewer B, independent of reviewer A (nothing of A's was read). Branch `review/creds-verify-b` from main
(`e151e45b`, 0.1.134). Read the code paths end to end, ran the existing tests, then wrote new end-to-end tests
through the real entry points. No live container was available: what that means for each claim is in
[Proven only with stand-ins](#proven-only-with-stand-ins).

> **Status (2026-11-19, later):** the three open defects (4, 5, 6) are fixed on `feature/creds-give`; their strict
> xfail tests pass. Finding 6 was decided as the design point it names: a Git login given during a run goes to the
> run's connection proxy (no token in the sandbox), into the sandbox only where the proxy cannot log in and only on
> the human's word - see [credentials-give.md](credentials-give.md), section 3. The rows below that name the run's
> `git-tokens.json` describe the state this review found.

## Verdict

**Yes on the command line, yes for giving in the app and the web, with three gaps.** Every way a credential reaches
a sandbox was followed from the entry point to the place where Git, the agent or the proxy uses it, and each value
arrived there and nowhere else: not in a command line, not in a log, not in a file that outlives the run, not on
the machine the command was sent from when the session runs on another host. What a run is given ends with the
run; what `--keep` wrote is there again at the next start and at a resume.

The gaps: three defects of the command line were small and are fixed here (test first). Three are left open with
a failing test each (strict `xfail`): taking a given credential back is not possible in the app's window (an API
key at all, a Git login only by retyping its URL) nor, for an API key, in the web interface; and the app does not
say that a Git login given during a run is readable by the agent.

## Method: what is real, what stands in

Real: `./sandbox-ai` as a subprocess of a user's host (`tests/live_fakes.py`: HOME, XDG folders, PATH); the
launcher's whole start (run.json, the session directory the sandbox sees as `/sandbox/session`, the proxy's
directory `/sandbox/proxy`, the cleanup at the end); the supervisor's control server (`runtime.control_server`);
the connection proxy (`runtime.ProxyServer`) started with the very `proxy.json` the launcher wrote, TLS on both
sides; `git` (credential fill, a push over smart HTTP to a server that wants a Basic login); the secret store's
files, plain and encrypted with a primary password; the app's `MainWindow` (offscreen) with its real backend; the
web interface's `Service` with its real command runner; the remote shell command `sandbox-ai hosts run` builds.

Stood in for: `podman` (the fake of live_fakes: the start goes as far as `podman run --pod` and "runs" until the
test ends it), the firewall (a list of what nft would be told), DNS (names below `.creds.test` answer 192.0.2.77),
`ssh` (logs its command line, then runs the remote command as the other host's user - another HOME - without a
network), the proxy's redirect (a fixed destination; the proxy reaches the test server instead of the pinned
address).

## What works (each with its test)

Core tests: `tests/test_credentials_verify_b.py` (T). App tests:
`projects/sandbox-ai-app/tests/test_credentials_verify_b_app.py` (A).

| Path | Entry | Proven | Test |
|---|---|---|---|
| `--secret-env NAME`, `NAME=SECRET`, `--env NAME` | CLI start | value in the agent's variables (config.json "env", private), names in `secret_names` for redaction, in no command line | T `test_a_start_puts_each_credential_where_it_is_used_and_nowhere_else` |
| `--api-key HOST=NAME=SECRET` | CLI start | agent gets the placeholder only; key in `proxy.json`; on the wire the key replaces the placeholder (Bearer, X-API-Key) for the key's verified host only; another server keeps the placeholder; a Host header naming the key's host at another server is refused; the answer and the log hold no key | same |
| `--publish pypi` (publish-logins) | CLI start | `TWINE_PASSWORD` a placeholder; token in `proxy.json`; PyPI's Basic login gets the token on the wire | same |
| `--git-token-secret` (no `--token-proxy`) | CLI start | Git's helper answers for that server only (real `git credential fill` with the run's own `GIT_CONFIG_*`) | same |
| git-logins registry (`git-logins add`) | CLI start | login in `proxy.json` only (never Git's in the sandbox) | same |
| `--token-proxy` | CLI start | no token, no helper, no `GH_TOKEN` in the sandbox; proxy adds Basic to Git's smart HTTP requests of that server only, never to API calls, never over the agent's own login; a `give` to such a run is refused with the reason | T `test_token_proxy_keeps_the_git_token_out_of_the_sandbox_and_adds_it_on_the_wire` |
| `--identity` (its Git logins, commit name), `--git-credentials FILE`, `--llm` key | CLI start | identity's login replaces the list's in the proxy and is added on the wire; credential file's login is Git's helper; LLM key a placeholder for the agent, the key on the wire | T `test_an_identitys_logins_a_credential_file_and_an_llm_key_at_a_start` |
| `git-logins give / revoke / list --run`, `--user`, `--keep` | CLI, running | token in the run's `git-tokens.json` (0600); **a real `git push` works with it and fails before and after**; a remote naming another user gets nothing; listed without the token; `--keep` writes the reference into the trusted project file; ends with the run | T `test_a_git_login_given_during_the_run_makes_git_push_work_until_it_is_revoked` |
| `secrets give --api-key --as / revoke / list --run`, `--keep` | CLI, running | key reaches the proxy's run directory through the supervisor (0600), host pinned and opened, never in what the sandbox sees; on the wire for that host only, also as the password of Git's Basic login; revoked: the next request keeps the placeholder; the next start has it from `--keep` | T `test_a_secret_given_during_the_run_is_put_in_on_the_wire_for_its_host_until_revoked` |
| `--keep`, then `sandbox-ai resume NAME` | CLI | the resumed session has the kept login and key; what was given without `--keep` is gone | T `test_what_keep_wrote_is_there_again_when_the_session_is_resumed` |
| Secret store locked (primary password) | CLI | start and give refuse with "sandbox-ai secrets unlock", change nothing (no file, no host opened); work once unlocked; the file never holds the value | T `test_a_locked_secret_store_refuses_cleanly_and_works_again_once_unlocked` |
| Another host: `hosts run HOST -- secrets set / secrets give / git-logins give / revoke` | CLI, remote | the secret's value goes over ssh's standard input; the right command (each argument quoted) to the right target and port; login and key arrive in that host's run; **nothing on this machine** (no file, no command line); an unknown host starts no ssh | T `test_credentials_for_a_session_on_another_host_go_to_that_host_and_stay_there` |
| `--auth-file`, central store, `login AGENT --from-session --expect`, `--auth-from-session`, `--no-auth`, `--copy-auth`, `login --logout`, `login --status --sessions` | CLI | each login lands in the session's HOME (0600) from the source named; a wrong fingerprint imports nothing; a refresh during the run goes back into the store; no token in any output, command line or log | T `test_agent_logins_reach_a_session_from_a_file_the_store_and_another_session` |
| `login --status --sessions --host HOST`, `login AGENT --from-session NAME --host HOST` | CLI, remote | listed and imported on that host (`--yes --expect`, what the app and the web send); the login never comes to this machine | T `test_a_sessions_login_on_another_host_becomes_that_hosts_central_login` |
| MCP server `env_secrets` | CLI start `--mcp` | the bridge starts each connection's container with `--env=NAME` (name only), the value in podman's environment; the bridge's environment is not readable by other processes; nothing in the definition, state, list or logs | T `test_an_mcp_servers_secret_reaches_its_container_by_name_only` |
| Launcher killed (SIGKILL) | CLI | the leftover private files with values go with the next sandbox-ai command | T `test_a_killed_launcher_leaves_its_credentials_only_until_the_next_command` |
| Web: `POST /secrets/NAME`, `/net/RUN` (git_login, api_key, revoke of a Git login), `GET /net/RUN` | web service | the real commands run; value on standard input only; login and key arrive; not confirmed: nothing runs; no value in any response | T `test_the_web_interface_keeps_a_secret_and_gives_it_to_a_running_session` |
| App: Git logins tab Add… (token typed), right-click menu = the buttons, Give to a running sandbox…, Remove… | app, this machine | real subprocesses; only this machine's run is offered; token in the run's `git-tokens.json`; in no text of the window, not in the app's log | A `test_a_git_login_added_and_given_in_the_window_reaches_the_running_session` |
| App with the host chooser on another host: Secrets tab Add, Give… (API key), Git logins tab Add… and Give… | app, remote | over ssh as the app builds it: kept and given on that host; only that host's start is offered; nothing on this machine; no value in a command line, text or log | A `test_credentials_for_a_session_on_another_host_are_kept_and_given_there` |
| Board: Agent / Git / HOME and login parts -> the start's command line | app, here and remote | references only in the command line; run as the terminal would run it, here and through `ssh -tt`: the sandbox has the credentials of the host it starts on | A `test_a_board_start_with_credentials_runs_with_them_here_and_on_another_host` |
| Logins of the sessions › Use for a new start… (right-click), Log in… on another host | app | the Board gets `--auth-from-session NAME`; the start it makes has that login in its HOME; Log in… is `ssh -tt … sandbox-ai login AGENT` to that host | A `test_a_sessions_agent_login_is_used_for_a_new_start_from_the_window` |
| Sandboxes › Secrets and logins… of a start on another host | app | opens the Credentials page with that host chosen | A `test_secrets_and_logins_of_a_start_on_another_host_opens_that_hosts_credentials` |

Existing tests of these paths (10 core files, 6 app files) pass unchanged: see [Results](#results).

## What is broken

### Fixed here (test first, then the fix)

1. **A token file given during a run was read wherever its path led** (security, low: the human must name the
   path). `git-logins give RUN URL --file PATH` / `net allow --git-token-file` read any private file of yours the
   path reached - also through a link inside the run's own workspace, which its agent can make - and wrote the
   content into the sandbox's `git-tokens.json`. A start refuses exactly this ("goes through the link …, sandbox-ai
   does not follow it", `agent_sandbox.py:4102 check_host_source`) and warns about a token file inside a
   workspace; a give did neither. Fix: `sandbox/net.py:1642 run_trees`, `:1653 check_token_file`, called at
   `:2758`. Test: T `test_a_token_file_given_during_the_run_is_held_as_a_start_holds_it`.
2. **A relative token file was kept as typed.** `--file tokens/gitea --keep` wrote `--git-token-file
   URL=tokens/gitea` into the project file, and the "start with:" hint named it so too: the next start, from
   another directory, read another file or none. Fix: `sandbox/net.py:1629 named_token_files`, called at `:2743`.
   Test: T `test_a_relative_token_file_is_kept_as_the_file_it_named`.
3. **`net show RUN --keep` silently dropped API keys.** It kept the host of a key given during the run
   (`--allow-host`) but not the key and said nothing (for Git logins it says so): the next start reached the host
   without its key. Fix: a note naming each key with the option that brings it back (`sandbox/net.py:2454`).
   Test: T `test_net_show_keep_says_what_it_does_not_keep_of_the_credentials`.

### Open (a strict `xfail` test each)

4. **An API key given during a run cannot be taken back in the web interface.** The row is listed with "on that
   host: sandbox-ai net revoke" (`sandbox/web/app.js:2972`, request `null`); `/net/RUN` refuses `api_key` for
   `op: revoke` (`sandbox/webui.py:2425-2433`). The command line can (`secrets revoke`). Test: T
   `test_the_web_interface_takes_back_an_api_key_given_during_the_run`.
5. **What a run was given cannot be taken back in the app's window.** Approvals › Allowed now lists what
   `net revoke` takes back, but `allowed_rows` knows no `git` and no `api_keys` of `net show --json`
   (`approvals_sections.py:745`); Sandboxes › Revoke… (`ui.py:2008 NetworkDialog`) has a Git login field that wants
   the URL and a reference typed again, and nothing for an API key; the Credentials page has Give only. Test: A
   `test_what_a_run_was_given_can_be_taken_back_in_the_window`.
6. **The app does not say that a Git login given during a run is readable by the agent.** The Git logins tab says
   "no token enters a sandbox" (true for a start: the proxy logs in). Give to a running sandbox… on that same tab
   writes the token into the run's session directory, which the sandbox reads (its Git helper answers with it); the
   question asked first says only "Its Git logs in with it from now on". The README says it ("written into the
   run's session directory, which the sandbox sees read-only"); `git-logins give --help` does not either. Test: A
   `test_the_question_before_a_git_login_is_given_says_that_the_agent_can_read_its_token`. Behind the wording is a
   design point for the user to decide: a login given during a run could go to the proxy as an API key does
   (`secrets give`), so that the token never enters the sandbox; today only a start (`--token-proxy`, the
   git-logins list) keeps it out.

## Proven only with stand-ins

- **Firejail hiding `/sandbox/proxy` and `/run/sandbox-proxy` from the agent, and the proxy's own user**: the tests
  show which directory each value is written to and its mode; that the agent cannot read the proxy's directories
  is the live sandbox's property (`tests/test_agent_sandbox_live.py`), not shown here.
- **nftables**: that a host given with a key or a login is really opened is a line in a list here.
- **The redirect to the proxy**: the proxy is handed the pinned address as the connection's destination and
  reaches the test server instead; its checks (verified name, port, Host header) are the real ones.
- **The Git helper's path**: in the sandbox it is `/sandbox/runtime.py` reading `/sandbox/session/git-tokens.json`;
  here the same function reads the run's own file by its host path.
- **ssh**: the command line and the remote shell command are the real ones, run without a network. Host keys, the
  shared connection (ControlMaster) and a real sshd are not exercised.
- **The agents themselves**: that Claude Code, Codex and the others read the login file put into their HOME, send
  the placeholder where a key is expected and use `GH_TOKEN` is their behaviour, not tested here. `sandbox-ai login
  AGENT` itself (the agent's interactive login in a sandbox) was not run; what was tested is everything after a
  login file exists.

## Untested

- macOS (podman machine): a give goes through `podman exec` there (`net.py ask_machine`), a descriptor cannot go
  along; not run.
- The system keyring and the macOS Keychain as the secret store (the private files, plain and encrypted, were).
- `--publish` kinds other than pypi end to end, and an identity's publish logins (unit tests exist:
  `tests/test_publish_logins.py`).
- The app's Start page and wizard as entry points of their own (the Board's command line was run; the wizard's
  token secret has `tests/test_wizard_token_secret.py`); the Publish logins tab's Add… and the Identities tab
  through the window (same code path as the Git logins tab's Add…: `LoginsBox.add_with_token`).
- A give to a paused run, a run of a sandbox-ai before 0.1.92, and a run with `--no-proxy`: refusals read in the
  code (`net.py:1395`, `:1579`), existing tests cover them in-process.
- Rotation of a subscription login used by two sandboxes at once (documented with a warning; needs the provider).

## Observations (no defect)

- A login of the git-logins list or of an identity opens nothing by itself: its server must be allowed as usual
  (`--allow-host`, a preset), which `git-logins --help` says; a `--git-token-*` option opens its server.
- `--copy-auth` copies the login store's login while it holds one, this computer's own only after `login AGENT
  --logout` (`agent_sandbox.py:6765 auth_sources`); the option's help says "they must exist on the host".
- After `login AGENT --from-session S`, a later refresh goes into the store, not into S's kept HOME: `--auth-from-
  session S` then copies the older tokens (the start warns about rotating refresh tokens).
- A dry run with `--secret-env` does not read the store (a locked store shows only at the real start); with
  `--api-key` it does.

## Tests added

- `tests/test_credentials_verify_b.py`: 17 tests (16 pass, 1 strict xfail).
- `projects/sandbox-ai-app/tests/test_credentials_verify_b_app.py`: 7 tests (5 pass, 2 strict xfail).

## Results

Core (`/work/.venv/bin/python -m pytest -q -p no:cacheprovider`, one process per run):

- Before any change, the ten existing credentials files (`test_credentials_given`, `test_git_credentials`,
  `test_git_logins`, `test_publish_logins`, `test_secret_store`, `test_secrets_primary_password`,
  `test_secrets_sealed_files`, `test_session_login`, `test_proxy`, `test_net`): 367 passed, 2 skipped.
- `tests/test_credentials_verify_b.py`: 16 passed, 1 xfailed (three runs in a row).
- The new file with the 31 files the change to `net.py`, the READMEs and the release notes can touch (the ten
  above, the docs tests `test_docs_claims`, `test_docs_every_option`, `test_release_notes`, `test_main_help`,
  `test_completion`, approvals, allow lists, kill switch, taint, launch log, orphaned commands, VPN during a run,
  kits live, bridge, MCP variables, `test_agent_sandbox_contracts`, `test_live_regressions`): 4098 passed, 12
  skipped, 1 xfailed, 2 failed. The two failures are `test_agent_sandbox_contracts.py::test_take_relays[happy]`
  and `[edge]` (`os.dup2` onto the fixed descriptor numbers 230-233: `OSError: [Errno 16] Device or resource
  busy`, `runtime.py:381`, code this branch does not touch): they pass alone (3 passed), and the new file together
  with the whole contracts file gives 3016 passed, 1 xfailed; they failed 18 minutes into that one long process
  - an order-dependent race of that test, not of this change (the new file leaves no descriptor open: 4 before,
  4 after).

App (`QT_QPA_PLATFORM=offscreen`, the checkout as `SANDBOX_AI_HOME`):

- Before any change, the six existing credentials files (`test_credentials_give_e2e`, `test_credentials_page`,
  `test_session_logins_app`, `test_credentials_layout`, `test_identities_page`, `test_wizard_token_secret`): 39
  passed.
- With `tests/test_credentials_verify_b_app.py`: 44 passed, 2 xfailed. `tests/test_references.py`: 11 passed.

`sandbox-ai privacy-check` of the branch: 0 blocking, 0 warnings.
