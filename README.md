# sandbox-ai

[Deutsch](README_DE.md) · [Quickstart](QUICKSTART.md)

Linux launchers for Codex, Claude Code and OpenCode with two sandbox layers:

```text
Host user → rootless Podman pod or Bubblewrap → Firejail → agent
```

`codex-sandbox`, `claude-sandbox` and `opencode-sandbox` share
`sandbox/agent_sandbox.py`. Podman is the default backend. Before executing an
agent, a guard verifies that inner Firejail created additional mount and PID
namespaces, dropped all five capability sets, and enabled NoNewPrivs and
Seccomp. A failed check aborts startup.

## Requirements

Run as an ordinary host user, without `sudo`. The launchers need Bash,
Python 3.9 or newer and `readlink`.

- **Podman:** working rootless Podman, delegated UID/GID ranges, `pasta` and a
  prebuilt image. The image contains the agents and Firejail.
- **Bubblewrap:** host-installed agents, `bwrap`, `firejail`, `nft`, `setpriv`,
  `newuidmap`, `newgidmap` and, for networking, `slirp4netns`. Both `/etc/subuid`
  and `/etc/subgid` need a delegated range of at least 65536 IDs. An NVM-based
  installation may also need an explicit read-only `--runtime` directory.

The host kernel and policy must allow the required namespaces, helpers and
nftables. These launchers do not override host restrictions.

```bash
./sandbox-build-image \
  --build-arg CODEX_VERSION=0.164.0 \
  --build-arg CLAUDE_VERSION=2.1.311 \
  --build-arg OPENCODE_VERSION=1.18.32
./codex-sandbox --workspace "$HOME/src/demo" --offline --probe
./codex-sandbox --workspace "$HOME/src/demo"
```

These CLI versions were used for validation. Omitting version arguments uses
`latest`. The base is Node 22 on Debian; the base image digest and apt package
versions are not pinned yet. Launchers never implicitly build or pull images.

## Workspace and file permissions

`--workspace PATH` appears at `/work` and is writable by default. Relative
paths resolve from the invocation directory. Missing directories are created,
including parents. Existing directories produce a warning; startup continues
without changing their contents or permissions. `--dry-run` creates nothing.
A regular file used as a workspace, or insufficient permissions, causes errors.

```bash
./codex-sandbox --backend bwrap --workspace "$HOME/src/my_temp"
./codex-sandbox --workspace "$HOME/src/my_temp" --workspace-ro
```

Seeing `root:root` inside the sandbox is expected: namespace UID 0 maps to the
ordinary host user. This does not require `chmod 777`. Files written under
`/work` appear in the chosen host directory. The sandbox home at `/root` is
removed on exit unless `--state-dir` is supplied.

**Codex:** the launcher defaults to `--sandbox danger-full-access` after
establishing the verified outer sandbox and Firejail. Codex uses those external
boundaries. Its additional native sandbox failed the practical test while
creating a nested UID map (`bwrap: setting up uid map: Operation not permitted`).
Explicit Codex `--sandbox`, `-s` and
`--dangerously-bypass-approvals-and-sandbox` options are preserved.
`--workspace-ro` remains an external read-only mount that Codex cannot override.
Other explicitly chosen Codex modes may activate its native sandbox again.

Additional mounts are repeatable:

```bash
mkdir -p "$HOME/reference" "$HOME/output"
./opencode-sandbox --workspace "$HOME/src/demo" \
  --mount "$HOME/reference:/mnt/reference:ro" \
  --mount "$HOME/output:/mnt/output:rw"
```

Format: `SOURCE:DESTINATION:ro|rw`. Sources must exist; destinations must be
under `/work`, `/mnt` or `/opt`. Quote paths containing spaces. Commas,
invalid destinations and individual socket/device files are rejected.
Sockets inside mounted directories can still expose host services. Select
only needed project directories; using HOME or `/` exposes a much larger
part of the host filesystem.

Pass agent arguments after `--`:

```bash
./codex-sandbox --workspace "$HOME/src/demo" \
  -- exec --skip-git-repo-check --ephemeral 'Create hello.txt containing Hello.'
```

## Authentication copies and persistent state

Without an authentication option, existing default JSON credentials for the
selected agent are copied privately. Missing credentials do not prevent
startup. Existing credentials in the sandbox home are preserved.

| Agent | Default host source | Destination relative to sandbox home |
|---|---|---|
| Codex | `~/.codex/auth.json` | `.codex/auth.json` |
| Claude | `~/.claude/.credentials.json` | `.claude/.credentials.json` |
| Claude, additional metadata | `~/.claude.json` | `.claude.json` |
| OpenCode | `~/.local/share/opencode/auth.json` | `.local/share/opencode/auth.json` |

Host source discovery respects `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and
`XDG_DATA_HOME`. Claude metadata is searched for in its selected configuration
directory, then in HOME. OS keyring credentials are not exported. Host
originals are never updated from sandbox copies.

- `--no-auth`: disable automatic copying; existing sandbox state remains.
- `--copy-auth`: explicitly require copying; a missing primary auth file or
  occupied destination causes an error.
- `--auth-file FILE`: copy a different file to the standard auth destination.
- `--import-codex-auth FILE`: OpenCode only; translate Codex ChatGPT OAuth into
  a private OpenCode auth copy. The account must be entitled to the model.
- `--copy SOURCE:RELATIVE_DESTINATION`: copy an additional file into sandbox HOME.

Explicit auth sources replace automatic selection. Auth selection switches
are mutually exclusive. Copied files use mode `0600`; private directories use
`0700`. Copy destinations are never overwritten. An existing OpenCode model
catalog is also copied if absent from sandbox HOME. Other host configuration
files are not generally imported.

```bash
./codex-sandbox --workspace "$HOME/src/demo" \
  --state-dir "$HOME/.local/state/sandbox-ai/codex"
./opencode-sandbox --workspace "$HOME/src/demo" \
  --import-codex-auth "$HOME/.codex/auth.json" \
  -- run --pure --model openai/gpt-6.1-sol 'Reply with OK'
```

Without `--state-dir`, HOME is deleted on exit. An existing state directory
must belong to the current user and have mode `0700`. Keep state **outside the
workspace**, and use separate directories for concurrent sessions. Refreshed
tokens persist there when persistent state is selected; they are not copied
back to host authentication files.

## Network policy and OpenCode Cloud

A separate network namespace and nftables restrict traffic. The following
names are resolved at startup, pinned in `/etc/hosts` and allowed as destination
IP addresses on **TCP/443**:

```text
api.openai.com    chatgpt.com    auth.openai.com
api.anthropic.com    claude.ai    claude.com    platform.claude.com
```

**OpenCode additionally allows `opencode.ai`** by default. A configuration file
being stored locally does not imply that it uses only local models. Select
local-only mode explicitly when using your own model servers:

```bash
# Documentation address: replace with your actual model server IP.
./opencode-sandbox --workspace "$HOME/src/demo" \
  --local-config "$HOME/.config/opencode/private-model.jsonc" \
  --allow 192.0.2.10
```

`--local-config` privately copies the chosen OpenCode configuration, removes
all default cloud permissions and sets agent environment switches disabling
project configuration, model catalog fetching, auto-updates and sharing.
The file itself must configure only the intended local providers and models.
Explicit `--allow` and `--allow-host` rules remain possible. A different
configuration does not silently overwrite an existing persistent copy.

| Option | Effect |
|---|---|
| `--allow IP_OR_CIDR` | All IP protocols and ports to that destination; repeatable, IPv4/IPv6. |
| `--allow-host NAME` | Resolve and pin addresses; allow TCP/443. |
| `--no-provider-defaults` | Remove all default provider permissions. |
| `--offline` | No external network; conflicts with additional network options. |
| `--network NAME` | Use an existing private Podman network. |
| `--ip ADDRESS`, `--ip6 ADDRESS` | Static address within the selected Podman network. |

```bash
# Example addresses only; adapt to the existing network.
./codex-sandbox --workspace "$HOME/src/demo" \
  --network research --ip 198.51.100.10 --allow 198.51.100.0/24
```

General DNS is blocked. For example, permit `models.dev` with `--allow-host`
when an online model catalog fetch is wanted. An imported catalog can be used
without that fetch. Restart when provider addresses change. IP filtering does
not distinguish domains sharing CDN addresses. Loopback, reply traffic and
necessary IPv6 neighbor discovery are permitted. `127.0.0.1` means the sandbox;
a host model server needs an address reachable from its network.

Podman defaults to `pasta`; Bubblewrap uses `slirp4netns` with host loopback
blocked. No ports are published. To select a Podman service, use for example
`--podman-url "unix://${XDG_RUNTIME_DIR}/podman/podman.sock"`. Bind sources must
exist at the same paths on the engine. This does not automatically transfer
files to another machine.

## Other options

| Option | Purpose |
|---|---|
| `--backend podman\|bwrap` | Choose outer sandbox; default Podman. |
| `--image NAME` | Select an existing Podman image. |
| `--binary PATH` | Run a different executable; automatic Codex arguments are omitted. |
| `--runtime DIRECTORY` | Bubblewrap: expose an additional runtime read-only at the same path. |
| `--env NAME` | Forward a selected host variable only to the agent; values are absent from the plan. |
| `--dry-run` | Plan without DNS, credential reads, directory creation or sandbox startup. |
| `--probe` | Verify firewall and both sandbox layers, then exit without an agent. |
| `--help` | Complete CLI help. |

## Assessment of the proposed profiles

The launcher uses `firejail --noprofile` with its own options; it does not load
`~/.config/firejail/opencode.profile`. The proposed profile includes useful
controls such as Seccomp and capability dropping, but needs adaptation here:

- `protocol unix,inet,inet6` limits socket families, not destination IPs/ports.
- HOME whitelists need to account for the workspace, native agent/runtime
  installation paths, and `.local/state/opencode` when persisting state.
- `private-cache` creates a temporary cache; persistence needs a matching policy.
- `disable-mnt` and `disable-write-mnt.inc` may block intended mount paths;
  blacklisting `/usr/libexec` may block toolchain helpers. Included local
  profiles also affect the result.
- `restrict-namespaces` can block tools creating additional namespaces.
  `noroot` should not be carried over without checking the namespace-UID-0 setup.

The proposed Bubblewrap script shares the host network with `--share-net`,
inherits the environment without `--clearenv`, and exposes the full `/etc`.
Writable binds of auth/data directories are not independent copies. Directly
executing OpenCode does not start Firejail. These launchers instead use private
auth copies, a selected environment, a separate network and verified Firejail.

## Validation and troubleshooting

```bash
python3 -m pytest tests/test_agent_sandbox.py tests/test_agent_sandbox_{contracts,matrix,edges}.py -q
python3 -m pytest tests/test_agent_sandbox_live.py --agent-live \
  -k 'real_probe or real_auth_copy_and_mount_permissions' -q

# Four real model tests: Codex and a free OpenCode model, both backends.
python3 -m pytest tests/test_agent_sandbox_live.py \
  --agent-live --agent-provider-tests -k practical_agent_tools -q
```

Validation passed 314 unit cases, 64 subtests and 22 real cases, including
four practical model checks. Unit tests contain happy, sad and edge contracts
for all 35 functions. A manifest
test rejects new functions without those contracts. The matrix combines three
agents, two backends, five auth modes and new/existing workspaces. Additional
matrices exercise network rules, state, startup failures, pipe allocation and
bootstrap. See the [test matrix and results](evidence/test-matrix.json).
The mount-overlap and partial-pipe regressions were measured failing before
fixing production code: [regression evidence](evidence/regressions.json).

Run these from `sandbox-ai/`. Real auth-copy tests require JSON credentials
for all three agents. Practical model tests need Codex auth and an OpenCode
model catalog at `~/.cache/opencode/models.json`; OpenCode uses no auth copy
for those tests. Codex uses the account's regular allowance or billing.
`--agent-backend podman|bwrap`, `--codex-model` and `--free-model` narrow the
scope or select models. The OpenCode catalog must report zero input/output
prices, and the test also checks the model's reported cost. Free-model
availability can change.

Practical tests read a file with random values absent from the prompt, write
the correct result and verify it through a shell tool. Host files and tool
events are then checked. A successful CLI exit or a text reply alone is
insufficient. See the [validation report](evidence/validation-summary.json)
and four individual reports in [evidence/](evidence/). These tests do not prove
a Claude model request.

| Symptom | Check |
|---|---|
| Codex reports a nested UID-map error | Use the current launcher; explicitly enabling another Codex sandbox can reproduce the failure. |
| `/work` stays empty | Check the host path and invocation directory; write into `/work`, not the temporary HOME. |
| `root:root` inside the sandbox | Expected UID mapping; inspect host permissions without broadly relaxing them. |
| `newuidmap`/`newgidmap` fails | Check helpers, delegated ranges and host policy. |
| Missing image | Build it or select a suitable `--image`. |
| HTTP 401 | Check login and token refresh. |
| Unknown model | Check configuration, entitlement and model catalog. |
| Copy target exists | Omit repeated explicit copy options or select separate state. |

## Remaining work by urgency

1. **P1 — Path and state separation:** HOME and `/` are not technically rejected
   as workspaces; neither is a state directory inside the workspace. Credentials
   can accidentally enter a project. Shared state lacks locking, and concurrent
   changes and symlink races are not comprehensively addressed.
2. **P2 — Operational bounds and failures:** CPU/RAM/PID limit CLI options and
   additional real forced-interruption and concurrent-session checks are missing.
   Unit tests cover cleanup and abort paths.
3. **P2 — Agent lifecycle:** Practically verify a Claude model writing files,
   token refresh and a subsequent session using refreshed credentials.
4. **P3 — Reproducible builds:** Pin the base image digest and system packages;
   repeat validation when updating CLI versions.

These are remaining limits. Passing functional tests is not a kernel-escape
audit or a guarantee for every host configuration.
