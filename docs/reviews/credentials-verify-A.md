# Credentials given to sandboxes and attached - independent verification A (2026-11-19)

> **Status (2026-11-19, later):** the three open defects below (findings 1-3) are fixed on `feature/creds-give`;
> their strict xfail tests pass. What changed and what was decided is in
> [credentials-give.md](credentials-give.md) (section 3): a Git login given during a run goes to the run's connection
> proxy, no longer into `/sandbox/session/git-tokens.json` - so the rows of "Works" that name that file describe the
> state this review found, not the current one. Finding 4's fix was replaced by review B's (`net.named_token_files`;
> one fix kept, both regression tests green).

Question (user, 2026-11-19): do Git logins, secrets, publish logins, agent logins and the rest really reach a sandbox
cleanly - at the start and while it runs, on the CLI and in the app (and the web), on this machine and on a host of
`sandbox-ai hosts`? Reviewer A, independent of B. Branch `review/creds-verify-a` from main `e151e45b` (0.1.134).

Method: the code paths read end to end (git_logins.py, secret_store.py `give`, net.py allow/revoke/keep,
agent_sandbox.py start (git_tokens, registry_git_tokens, git_environment, api_key_*, proxy_config, auth_plan /
prepare_home), runtime.py (git_credential, ProxyServer.git_token, api_key_head, credential_host_refusal, control
server), hosts.py (argv / remote_command), webui.py net_change, the app's credentials_page.py and ui.py
NetworkDialog). Then new end-to-end tests through the real entry point - `./sandbox-ai` as a subprocess with only
Podman/tmux/ssh faked (tests/live_fakes.Host): a real launcher makes the run (run.json, the session directory,
config.json, the proxy's proxy.json, podman's argument list), the supervisor's control socket is served by the real
`runtime.control_server` in a thread, and what the sandbox's Git and proxy do is run with the real runtime.py
functions on the very files the launcher and sandbox-ai wrote. The app test drives the Credentials page offscreen
with the app's real backend running the real `./sandbox-ai`.

## Proven only with fakes (no live container here)

- That Podman mounts the files: checked on podman's arguments (`type=bind,src=SESSION,dst=/sandbox/session,ro`,
  `dst=/sandbox/proxy`), not inside a container. Firejail's `--blacklist` of /sandbox/control and /sandbox/proxy for
  the agent (runtime.py:5619-5621) is read, not run.
- The proxy's TLS and wire: `api_key_head`, `host_header_problem`, `ProxyServer.git_token` called directly on the
  files sandbox-ai wrote; tests/test_proxy.py has the proxy against a real upstream (not re-done here).
- SSH: a fake `ssh` runs the remote command with `sh -c` in a second user's environment (HOME, XDG, PATH with
  sandbox-ai). tests/test_hosts_ssh.py covers a real sshd (arguments surviving a real remote shell).
- The agent's own use of its login (Codex/Claude reading auth.json): only that the file is in the sandbox's HOME.

## Works (with evidence)

| What | CLI | App | Web | Evidence |
|---|---|---|---|---|
| `--git-token-secret/-env/-file` at the start: Git's helper via `GIT_CONFIG_*`, token in the agent's environment (documented: README "Pushing to GitHub", the agent can read it), `GH_TOKEN` for github.com; nothing in podman's argv | yes | (Board options) | (start) | `test_a_start_hands_each_credential_to_where_it_is_used_and_nowhere_else` |
| `sandbox-ai git-logins add` (registry): only in the proxy's proxy.json (`git_tokens`), never in config.json or the agent's env | yes | Git logins tab (existing tests) | yes | same test |
| `--api-key HOST=NAME=SECRET`: key in proxy.json, the agent gets `sandbox-ai-api-key-placeholder-NAME` | yes | | | same test |
| `--publish pypi` (publish-logins): token in proxy.json for upload.pypi.org, never in config.json | yes | | | same test |
| `--secret-env NAME`: the agent's variable, listed in `secret_names` (dropped for `--untrusted` commands) | yes | | | same test |
| No value in podman's argv (`--env` carries only HOME/USER/PATH/...: the values go in the 0600 config.json of the read-only session mount), none in the launcher's log or any file left on the host after the run; session and proxy directories removed at the end (agent_sandbox.py:14149-14150, 14225-14226) | yes | | | same test (`leaked()` scans every file of the user) |
| `git-logins give RUN URL --secret NAME [--user]` during the run: git-tokens.json 0600 in the run's session directory; the real `git credential fill` with the sandbox's helper answers for exactly that origin and user (not another port, 127.0.0.1 for localhost, https for http, or a URL naming another user); `list --run` / `net show` never show the token; `revoke` takes it away; the session directory - and the login - go when the run is stopped | yes | Git logins tab "Give to a running sandbox…" | Allow: Git login | `test_a_git_login_given_while_it_runs_reaches_its_git_and_ends_with_the_run`, app: `test_the_git_logins_tabs_give_reaches_the_runs_git_through_the_real_sandbox_ai` |
| `secrets give RUN NAME --api-key HOST --as VAR`: the supervisor gives the proxy the key (0600), the host is allowed; the proxy's `api_key_head` puts it in a credential header only (not `X-Note`), only for HTTPS to HOST:443 (not another host, plain http, another port); a request through HOST's TLS naming another Host is refused; the run's own list (run directory = /sandbox/control, blacklisted for the agent) is the only other copy; `secrets revoke` empties it | yes | Secrets tab "Give…" (existing e2e) | Allow: API key | `test_a_key_given_while_it_runs_is_put_in_on_the_wire_for_its_host_only` |
| `--keep`: the login (`--git-token-secret https://alice@HOST=NAME`) and the key (`--api-key HOST=SECRET=SECRET`) go into the trusted .sandbox-ai.json as references, never values; the next start of the project AND `sandbox-ai resume` of the kept start hand them over (SANDBOX_GIT_TOKEN_0/USER alice, the helper for that URL; the key in proxy.json, the placeholder in the env) | yes | Give dialog "Also for the later starts" | keep | `test_kept_logins_and_keys_reach_the_next_start_and_a_resume` |
| Another host: `sandbox-ai hosts run box -- git-logins give --secret GITEA --user alice -- RUN URL` (the exact arguments the app's `give_login_args` makes) reaches box's run with box's secret, never this machine's; one ssh call per command to `me@box.example`, each argument quoted, no value in any argument; `secrets give` and `git-logins revoke` the same way | yes | (same command line: app's `host_run`) | (hosts.argv) | `test_a_login_and_a_key_given_on_another_host_reach_that_hosts_run` |
| Agent logins: `--copy-auth` (the host's), `--auth-file FILE`, `--auth-from-session NAME`, `login codex --from-session NAME --yes` (the central login, which a plain start then uses instead of the host's): the login in the sandbox's HOME, 0600, the same document; never in podman's argv; on the host only where it belongs (the HOMEs, the central store, the source files) | yes | Board "Login from" (existing tests) | | `test_agent_logins_reach_the_sandboxs_home_by_each_way_and_nowhere_else` |
| Git asks its credential helper only after a 401 (the mechanism behind finding 1), with the real git | | | | `test_git_asks_its_credential_helper_only_after_a_401` |
| Web: a Git login given during the run is taken back (`net revoke NAME --git-token-env=URL=REVOKE`), local and remote | | | yes | `test_the_web_takes_back_a_git_login_it_gave[local,ct1]` |

Existing suites run and green before and after (see "Test results"): test_credentials_given.py, test_git_credentials,
test_git_logins, test_net, test_publish_logins, test_secret_store, test_session_login, test_identities,
test_mcp_variables (MCP `env_secrets`), test_login_device_default, test_security_review_oauth, test_proxy,
test_web_matrix; app: test_credentials_give_e2e, test_credentials_page, test_session_logins_app, test_remote_parity,
test_list_menus.

## Broken

### 1. A Git login given during the run never takes effect for a server the proxy already has a login for (medium)

`git-logins give` / `net allow --git-token-*` writes the login for the sandbox's Git helper (net.py:1501
`write_git_logins`, runtime.py:17598 `git_credential`). Git asks a helper only after a 401
(`test_git_asks_its_credential_helper_only_after_a_401`). The proxy adds its own login - `sandbox-ai git-logins`, an
identity's, `--token-proxy` - to every Git request that carries none (runtime.py:11566 `ProxyServer.git_token`), i.e.
to Git's first request. So for such a server the given login is never used: with a read-only registry token,
clone/fetch work with that one and a push gets the server's 403 (no 401, so Git never falls back). At the start the
opposite holds: a `--git-token-*` option for a server wins over git-logins (agent_sandbox.py:3379
`registry_git_tokens` skips it); during the run the proxy keeps its own, and nothing says so (`give` reports "its Git
logs in with it until the run ends"). Not a small fix: the proxy runs as a user of its own and cannot read the 0600
git-tokens.json; the supervisor would have to tell it (as it does with "api-keys").
Test: `test_a_login_given_during_the_run_wins_over_the_proxys_login_for_that_server` (xfail, strict).

### 2. `git-logins give` puts the token where the agent can read it, against git-logins' own promise (medium, security)

sandbox-ai git-logins promises "no token ever enters its sandbox" (git_logins.py:19, README.md:3386), and the app's
Git logins tab says "no token enters a sandbox" (credentials_page.py:216-218) right above its "Give to a running
sandbox…" button (credentials_page.py:243). That button - like `git-logins give`, `net allow --git-token-*` and the
web's Git login of Allow - writes the token into the run's session directory, which the sandbox mounts read-only at
/sandbox/session for its Git helper (agent_sandbox.py:3345-3349; podman `--userns=host --user=0:0`: the agent is the
file's owner). /sandbox/session is not among firejail's blacklisted folders (runtime.py:5619). Neither the CLI's
message, the app's question nor the README section "Changing the network of a running sandbox" says that the agent
can read it (README does say so for `--git-token-*` at the start). A run started with `--token-proxy` refuses a
login during the run (net.py:2716) instead of handing it to its proxy. Same fix direction as 1 (give the login to the
proxy through the supervisor); until then at least the message, the question and the README should say it.
Test: `test_a_git_login_given_while_it_runs_stays_out_of_the_sandbox` (xfail, strict; fails with "the token is
readable in the sandbox: /sandbox/session/['git-tokens.json']").

### 3. An API key given during the run cannot be taken back from the web or the app (low-medium, parity)

The web gives a key (`api_key` in POST /net/NAME, op allow) but refuses it for revoke (webui.py:2425-2432: "for
allow"), and the page's row of a given key has no Revoke (app.js:2972: `null`; Git logins have one: app.js:2970). The
app gives keys (Secrets tab, credentials_page.py:683) but has no take-back: the Sandboxes page's Revoke… dialog
(ui.py:2008 NetworkDialog) has a Git login row but no API key, and its "what the run has now" (ui.py:1988
`given_network`) and the Allowed section (approvals_sections.py:745 `allowed_rows`) show neither given logins nor
keys. Only the CLI's `secrets revoke RUN NAME` works.
Test: `test_the_web_takes_back_an_api_key_it_gave[local,ct1]` (xfail, strict: 400 "for allow"). The app's missing
action is a UI to design; no test pins its shape.

### 4. FIXED: a token file kept by a relative path broke every later start of the project

`git-logins give RUN URL --file ../tok --keep` (and `net allow --git-token-file URL=../tok --keep`) wrote
`https://HOST=../tok` into the trusted .sandbox-ai.json; every later start of that project was then refused
("use absolute paths (or ~/...), not: --git-token-file ...", agent_sandbox.py:3750 `relative_paths`). Fix: net.py
`kept_token_file` (net.py:2269) keeps the path absolute, as it was read. Regression test (e2e, failing before the fix):
`test_a_token_file_kept_by_its_relative_path_is_kept_absolute`.

## Observations (not defects, or by design)

- `secrets revoke` takes the key back but leaves the host it opened allowed until `net revoke --allow-host`.
- On another host the "variable" kind of a Git login (`--git-token-env`, offered by the app's Allow dialog and the
  web for every host) reads that host's non-interactive SSH environment, where the user's variable is not set: it can
  only fail there ("Host variable is not set"). The secret kind is the one that works remotely.
- A secret given as a Git login from the Secrets tab is always `https://HOST` (credentials_page.py:661): a local Gitea
  on the host's loopback (`http://localhost:3080`) can be given only from the Git logins tab or the CLI.
- A sandbox that outlives its launcher (its pod cannot be removed) keeps its session directory - and a login given
  to it - until it is cleaned up later (`unless_outlived`, agent_sandbox.py:14150); not exercised here.

## Untested here (and why)

- Live containers: the agent actually reading /sandbox/session, firejail's blacklist in effect, the real proxy
  process as its own user (no Podman here).
- macOS (the VM's control directory, `podman exec` path of `net`): Linux only here.
- `--token-proxy` with a login during the run: refused by design (net.py:2716; test_net covers the refusal).
- A locked primary password during `give` (the secret store asks on a terminal); existing
  test_secrets_primary_password covers the store, not `give` while locked.
- MCP `env_secrets` end to end into a server's sandbox: only the existing test_mcp_variables.py (green), not re-proved
  through a start here.
- The Board's "Login from" and prerequisites.py's login checks: existing app tests (test_session_logins_app, green),
  not re-read in depth.

## Tests added

- tests/test_credentials_verify_a.py: 8 passing e2e tests (start, give/revoke/end of a Git login, give/revoke of a key
  on the wire, --keep into next start and resume, remote host, agent logins, git's 401 mechanism, relative token file
  regression) and 2 strict xfails (findings 1 and 2).
- tests/test_credentials_verify_a_web.py: 2 passing (web revokes a Git login, local and remote), 2 strict xfails
  (finding 3).
- projects/sandbox-ai-app/tests/test_credentials_verify_a_app.py: 1 passing (the Git logins tab's Give through the
  app's real backend and the real ./sandbox-ai).
- tests/live_fakes.py: `FAKE_PODMAN_KEEP=DIR` also copies the proxy's private directory to DIR/proxy (proxy.json).

## Test results

- Baseline on main before any change (credentials suites: given, git_credentials, git_logins, net, publish_logins,
  secret_store, session_login, identities, mcp_variables, login_device_default, security_review_oauth): 340 passed,
  1 skipped.
- After (core, 28 files: the new ones, those suites, test_allow_domain, test_web_matrix, test_proxy, and every
  suite using live_fakes' FAKE_PODMAN_KEEP - llm_unknown_window, hotfix_board_llm, llm_harness_limits, qwen, hide,
  hermes, hermes_telemetry_guard, telemetry_guard, tool_rules_agents_e2e, with_all_agents, workspace_review,
  live_regressions): **5023 passed, 20 skipped, 4 xfailed** (the 4 strict xfails above) in 522 s.
- App (offscreen; the new test, credentials_give_e2e, credentials_page, credentials_layout, session_logins_app,
  remote_parity, list_menus, wizard_token_secret): **54 passed** in 87 s.
