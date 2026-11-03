# TODO

## Future: GitHub push proxy on the host (token kept outside)

`--git-token-env`/`--git-token-file` put the token into the sandbox, where the agent can read
it. A push proxy would give the agent the ability to push without the secret:

- **Host-side proxy** for Git's smart HTTP protocol, reachable from the
  sandbox only through a forwarded loopback port or a mounted Unix socket
  (same path as `--allow-host-port`). The token stays on the host; the proxy
  adds the `Authorization` header itself.
- **Repository allowlist**, e.g. `--github-push owner/repo` (repeatable).
  Every other repository and the GitHub API are refused.
- **Ref policy**: parse the ref update commands at the start of
  `git-receive-pack` and allow, e.g., only `agent/*` branches; refuse
  force pushes (non-fast-forward), deletions and pushes to the default
  branch. A fine-grained token cannot express this.
- **Audit log**: one JSON line per push with repository, ref, old and new
  object IDs, next to `egress-denied.jsonl`.
- ~~Privacy check in the proxy~~: done in 0.1.20 by the connection proxy in
  the sandbox (every push over HTTP(S) is checked with the full rules before
  the server sees it). What remains here is keeping the token out of the
  sandbox.
- Without `--allow-github`: the sandbox reaches only the proxy, never
  github.com directly.

## Open findings from the security review (2026-11-01)

Low severity; each needs a larger change than a local fix.

- **Claude write-back within one run.** Write-back now requires an existing
  host login, an unchanged host file and tokens that differ from the ones
  copied in; the host wins at every start. During a single run the agent can
  still replace the tokens with another account's, which then reach the host.
  Closing this needs an account check against Anthropic (or an opt-out such
  as `--no-auth-writeback`).
- **Signals during cleanup.** SIGTERM/SIGHUP while the `finally` blocks run
  can interrupt `podman pod rm` or the slirp4netns stop. Block these signals
  around cleanup. Session pods now carry a `sandbox-ai.session` label and a
  restart removes only its own stale pod; one-off pods still use random names
  without a label.
- **File logger.** Symlinked directories are skipped, but `os.walk` still
  races with a swap below the top directory; walk with `openat` and
  `O_NOFOLLOW|O_DIRECTORY` and add watches through `/proc/self/fd`.
- **Project config `--copy` of credential files** should count as an auth
  option in `merge_config`, like `--copy-auth` does.
- **apt packages** in the images are not pinned; agent, uv and Node.js base
  can be pinned with `--pin`, `--uv` and `--node` (newest is the default).

## Open findings from the review of 2026-11-02

Fixed in the same change: Claude's `.credentials.json` copied in full (MCP
OAuth tokens), write-back into a host file without a login, discovered
credential sources and workspaces inside agent-writable trees, TIOCSTI through
the controlling terminal, risky workspaces (HOME, `/`, temp, the launcher),
`--allow` scope IDs, relative paths in project and MCP files, session
`agent_args`/backend from the project file, stale session and MCP pods,
resize frames inside input frames, MCP servers frozen with a bwrap session,
the wizard's origin default. Still open, low severity:

- **PID reuse.** Session and MCP status trusts PID plus command line; also
  compare the start time (field 22 of `/proc/PID/stat`).
- **bwrap pause is best-effort.** Two SIGSTOP passes; a process that keeps
  forking can escape. A cgroup freezer would be exact.
- **Xvfb outside Firejail.** It runs in the outer mount namespace where
  `/sandbox/log` and `/sandbox/vnc` are visible; run it inside Firejail with
  the same blacklist.
- **Tokens inside the sandbox.** `--git-token-*` and `--git-credentials` hand
  the tokens (every login in the file) to the agent; the push proxy above would
  keep them on the host.

## Kits, memory and plugins (2026-11-02)

Not verified live yet (no Podman/Bubblewrap or agent images where this was built):

- **Skill locations of Codex and OpenCode.** Kits mount Codex skills at
  `~/.agents/skills` and OpenCode's at `~/.config/opencode/skills`, as their
  current documentation says; older releases read `~/.codex/skills` and
  `~/.config/opencode/skill`. Check with the images' versions.
- **Claude with a read-only plugin cache.** `cache/` and `marketplaces/` of the
  store are read-only, the index files writable copies with `autoUpdate` off.
  Check that Claude loads the plugins and does not fail on a background update.
- **Codex plugins**: no integration yet; marketplaces.json for Codex is noted
  and ignored. OpenCode's npm plugins are passed on, not installed by sandbox-ai.
- **Memory of a crashed run** in a throwaway HOME is lost (copy-in/copy-out);
  a persistent HOME is saved at its next start.
- **One `--state-dir` used by two runs at once** is not locked. Memory I/O no
  longer follows symlinks there, but logins and settings of the two runs can
  still overwrite each other.
- **`--no-plugins` on a persistent HOME** keeps the `enabledPlugins` of an
  earlier run while the cache is no longer mounted.

## Logins (2026-11-02)

Fixed: the Claude write-back compared the whole host credentials file, so a
host Claude rewriting it (MCP OAuth tokens) made the launcher drop the
sandbox's rotated tokens, leaving the host with a used-up refresh token and the
next start with "Login expired". It now compares only the login; a missed
write-back on a persistent HOME is recovered at the next start. New: the login
store (`sandbox-ai login AGENT`). Open:

- **One refresh token per stored login.** Two sandboxes of one agent running
  at the same time still race for it; a host-side token broker (the sandbox
  asks the launcher for fresh access tokens, never holds the refresh token)
  would end that, and would also keep refresh tokens out of the sandbox.
- **Login commands not verified live.** `claude auth login`,
  `codex login --device-auth` and `opencode auth login` inside the sandbox
  need a run against the real services; other arguments can be passed after
  `--` (`sandbox-ai login codex -- login`).
- **Online check of a login.** Expiry is checked offline only (Claude's
  `refreshTokenExpiresAt`); a refresh token revoked or used up elsewhere shows
  only when the agent refreshes.

## Connection proxy: next steps

- HTTP/2 towards clients that insist on it (gRPC); today only HTTP/1.1 is offered.
- QUIC/HTTP3 is not proxied; UDP stays with the firewall rules.
- The push check finds what its rules describe. An agent that deliberately encodes data (base64,
  archives, encryption) gets past any pattern check; the connection log records what was sent.
- Live tests of the NAT redirect on real Podman and Bubblewrap hosts.
