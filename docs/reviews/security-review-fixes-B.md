# Security review (sandbox isolation), part B: kernel, policy, filesystem

What became of the findings SAI-01, -02, -03, -04, -05, -06, -07, -09, -10, -11, -13, -16, -17, -20 and -25 of
`/work/sandbox-security-review.md` (a passive review of a running sandbox-ai 0.1.99 sandbox: rootless Podman,
Firejail 0.9.72 inside). The review's text was treated as claims: each finding was checked against the code of
`main` (0.1.120, commit e81ce449) before anything was changed. Branch `security-fix-B`; line numbers are those of
that branch. Part A (local channels and credentials) is `docs/reviews/security-review-fixes-A.md` on
`security-fix-A`; nothing here repeats it, and the two branches change no line in common on purpose (see "Merging
with part A").

## The two facts behind most of them

**1. The container had no system call filter, and Firejail's is a list.** The launcher starts the container with
`--security-opt=seccomp=unconfined`, `apparmor=unconfined` and `label=disable` (`agent_sandbox.py:10787`): the
supervisor mounts, makes namespaces and loads nftables, and Firejail needs the same. Inside, Firejail's `--seccomp`
refuses the 71 calls its release knows (`runtime.py:4643`); what it does not name passes. So the review's
"assurance gaps" about an outer policy it could not see were not gaps of evidence: there was none.

Measured in the sandbox this was made in (a real sandbox-ai run of the 0.1.99 line; the calls only make and close
a socket or fail, nothing leaves the process):

| Probe | Answer before | Meaning |
| --- | --- | --- |
| `io_uring_setup(0, NULL)` | `EFAULT` | the call reached the kernel (SAI-13) |
| `unshare -U true` | exit 0 | a user namespace was made (SAI-03); mapping an ID into it failed, so `bwrap` did not work either (SAI-05) |
| `socket(AF_VSOCK)`, `socket(AF_ALG)`, `socket(AF_RDS)` | success | families no firewall rule sees (SAI-20) |
| `socket(AF_NETLINK, NETLINK_GENERIC)` | success | every netlink protocol |
| `/work`, `/root` in `/proc/self/mountinfo` | `rw,nodev,relatime` | no `nosuid` (SAI-06) |
| `Seccomp_filters` | 2 | Firejail's 64-bit and 32-bit lists, nothing else (SAI-16) |
| `/proc/self/attr/current` | not readable (`EINVAL`) or `unconfined` | no LSM label (SAI-02) |

**2. Only the host can add some things.** A rootless engine loads no AppArmor profile, sets no project quota and
changes no sysctl. For those sandbox-ai now says what is missing and with which command (doctor, a strict start's
notes, the manifest), gives the administrator a place to name what they made (the policy's `lsm`), and holds a run
to it.

## The findings

| Finding | Verdict | Where (branch) | Severity (review) | Fix | Next step |
| --- | --- | --- | --- | --- | --- |
| **SAI-13** the inner policy does not deny `io_uring` | **Right**, and worse than stated: no outer filter existed, so nothing denied it (probe above). | `runtime.py:2003` SECCOMP_CONTAINER, `:2016` seccomp_policy, `:2233` own_filter, `:2956` inner_bootstrap, `:4750` inner_guard | Medium | **Fixed**, 27fc3071. The three calls are refused (`EPERM`) in the container's filter - the supervisor installs it on itself before the firewall is loaded, so it holds for the proxy, Firejail and everything of the run - and again in the sandbox's filter, which the guard installs before the agent. The guard asks the kernel in a child (`seccomp_probe`): `io_uring_setup` must answer `EPERM`, or nothing starts. Tests: `tests/test_security_review_seccomp.py` (48: every architecture by an interpreter of the program, and the real kernel in a child process). | The host's own switch (`kernel.io_uring_disabled=2`) closes it for every program; doctor names the command. |
| **SAI-03** namespace creation in the ordinary profile | **Right** (probe above). Half-working before: the namespace was made, no ID could be mapped (`CAP_SETFCAP`), so nothing useful ran in it - but the kernel's namespace code was reachable. | `runtime.py:2076` seccomp_lines (clone, unshare), `:2259` firejail_narrowing, `:4633` firejail_command | Medium | **Fixed**, 27fc3071. `unshare` and `clone` with a namespace flag and `setns` are `EPERM`, `clone3` is `ENOSYS` (its flags lie behind a pointer; glibc, musl, Rust and Go fall back to `clone` - threads and child processes are tested under the real filter). Twice: sandbox-ai's own filter, and Firejail's `--restrict-namespaces` where the installed Firejail has it (0.9.72 does; its filter was decoded with `fseccomp` and compared, `test_firejails_own_namespace_and_protocol_filters_agree_with_ours`). `--nested-containers` keeps namespaces and mounting, as its Firejail filter did. | A per-sandbox `user.max_user_namespaces` is the host's; not needed with the calls refused. |
| **SAI-20** socket address families not allowlisted | **Right** (probe above: `AF_VSOCK`, `AF_ALG`, RDS creatable). `AF_VSOCK` is the one that matters most: it is not part of a network namespace on most kernels, so the firewall does not see it. | `runtime.py:2007` SOCKET_FAMILIES, `:2008` NETLINK_PROTOCOLS, `:2076` seccomp_lines (socket, socketpair, socketcall) | High (assurance) | **Fixed**, 27fc3071. The agent: `socket` and `socketpair` only for unix, inet, inet6, and netlink with the protocols route, sock_diag and kobject_uevent; anything else `EAFNOSUPPORT` (netlink: `EPROTONOSUPPORT`). The container (supervisor, proxy, display): unix, inet, inet6, netlink, packet. The 32-bit `socketcall`, which carries the family behind a pointer and which Firejail's `--protocol` lets through, is `ENOSYS` in both. Firejail's `--protocol=unix,inet,inet6,netlink` is added as the second filter. | `--nested-containers` allows packet sockets and every netlink protocol (its engines need them); a narrower list there wants a measured Podman run. Type and protocol are not filtered for inet (the firewall and the capability-less user are the control there). |
| **SAI-16** the inner seccomp policy is a version-sensitive deny list | **Right.** | `runtime.py:1913` SYSCALL_NUMBERS, `:1986` FIREJAIL_REFUSED, `:1980` SYSCALL_TOP, `:2129` seccomp_program, `:2145` seccomp_account | Medium (assurance) | **Fixed in the part that makes it version-sensitive**, 27fc3071. sandbox-ai's filter is made from symbolic names per architecture (x86-64, i386 calls of a 64-bit process, arm64, arm), refuses Firejail 0.9.72's whole list itself (whatever Firejail an image has), and answers every call number it does not know - above Linux 6.17's last, in the kernel's gaps, x32 - with `ENOSYS`: a newer kernel's call is closed until somebody adds it. A machine without a table (not x86-64 or arm64) is said; `high` and `paranoid` do not start there. The manifest carries each filter's SHA-256, length and what it refuses; `tests/...::test_every_number_of_every_architecture_has_one_answer...` computes the decision for every number on every architecture, and `test_the_numbers_are_libseccomps` compares the table with libseccomp's. | It is still "everything known but a list", not an allowlist measured from the agents' workloads: that needs traces of real runs of twelve agents and their tools, and breaks them when it is wrong. The table has one place to grow (`SYSCALL_TOP`, `SYSCALL_NUMBERS`). |
| **SAI-06** writable mounts lack `nosuid` | **Right** for every bind mount of a run without `--nested-containers` (`mount_option` added it only there); the tmpfs mounts had it. No privilege could be gained (`no_new_privs`, empty capability sets) - defence in depth, as the review says. | `agent_sandbox.py:10626` mount_option, `:10683` host_folders; `runtime.py:2359` check_mounts | Low | **Fixed**, 27fc3071. Every bind mount of every run is `nosuid,nodev` (device folders `nosuid`); the guard reads `/proc/self/mountinfo` inside Firejail, after all mounts, against the launcher's list - a folder without a flag is said, and in `high` and `paranoid` the agent does not start. | `noexec` on data-only mounts is not set: `/tmp` and the workspaces run builds, and the session's folder holds scripts the agents' hooks run. |
| **SAI-11** Firejail 0.9.72 predates upstream fixes | **Partly.** The version is right (Debian 12's package in every image, `Containerfile.*`). The escape class it cites does not apply as described: Firejail is started by the supervisor as the container's root with `--noprofile` (`runtime.py:4643`), never by the agent with a privilege to gain (`no_new_privs`, and an agent's own `firejail` only says "an existing sandbox was detected"), and no profile is read from the HOME. The path `~/.config/firejail` was indeed absent and creatable by the agent. | `runtime.py:2284` seal_firejail_config, `:4649` (blacklist), `:2580` isolation_ready | High (assurance) | **Fixed for the folder**, 27fc3071: the supervisor makes it before Firejail starts (the sandbox's root's, 0700, opened without following a link; a file or link a run left under the name is removed), Firejail blacklists it, and `high` refuses a HOME whose `.config` is a link. The version is in the manifest; doctor checks the host's Firejail for the Bubblewrap backend. **Open:** a newer Firejail in the images. | Build upstream's current release in a stage of the twelve Containerfiles (pinned tag and SHA-256, at least 14 days old), then run the whole live suite: `inner_bootstrap` already carries the workaround 0.9.80 needs (`runtime.py:2957`). Could not be built or started where this was made. |
| **SAI-25** no per-run storage quota | **Right.** HOME and workspaces are bind mounts of your file system; nothing but the disk bounds them. | `runtime.py:2431` disk_state, `:2461` watch_disk; `agent_sandbox.py:10641` DISK_RESERVE, `:10690` disk_watch; `doctor.py:457` | High (availability) | **Fixed in part**, 27fc3071: the supervisor watches the disks behind what the run writes and behind its own log and control directories (every 2 s, every 0.2 s near the reserve). Below 256 MiB or 2000 free inodes a safeguard warning; in `high` and `paranoid` the run is ended at 512 MiB or 5000 inodes, while the reserve is there (tested with a real process and the real disk). | **Open:** a hard quota. It is the host's (XFS or ext4 project quota, a file system per run); doctor names the commands. A design that needs no root: a loop-mounted image per run through `fuse2fs` or an overlay on a size-bounded tmpfs for HOME - both change where a kept HOME lives. No I/O-rate limit (`--device-write-bps` needs a block device the user owns the cgroup of). |
| **SAI-02** not confined by an LSM profile | **Right**, by construction (`apparmor=unconfined`, `label=disable`). | `agent_sandbox.py:10648` lsm_value_ok, `:10657` lsm_options; `runtime.py:2396` unconfined_label, `:2402` lsm_problem; `doctor.py:428` | Medium | **Fixed as far as a rootless engine can**, 27fc3071: the administrator's policy key `lsm` (`{"apparmor": PROFILE}` or `{"selinux": TYPE}`) starts every container in that confinement; the supervisor reads its own label before it starts the agent and starts nothing outside it (an engine that ignored the option, a profile that is not loaded). Bubblewrap refuses a start under such a policy. Doctor says what the host runs and how a profile is named; a start in `high` or `paranoid` says in a note that it has none. | **Open:** a profile of sandbox-ai's own. It has to allow mount, pivot_root, user namespaces, nftables and signals for the supervisor and deny them for the agent (a child profile entered by Firejail's `--apparmor` or `aa-exec`); AppArmor's syntax for user namespaces differs between 3.x and 4.x, and none could be loaded or tested here (the kernel this was made on has no AppArmor). A strict mode does not refuse to start without one: every start in `high` and `paranoid` on every host would fail. |
| **SAI-10** the connection proxy is a trusted parser outside Firejail | **Right** that it is outside Firejail's filter. Its capabilities were already empty (it sets its user from root, which empties the sets) and `no_new_privs` was set; the bounding set was left, and nothing checked. | `runtime.py:11275` drop_to_proxy_user, `:2564` proxy_confined, `:2580` isolation_ready | Medium | **Fixed in part**, 27fc3071: the proxy drops its bounding set first, marks itself not dumpable, reads what the kernel says of it (any capability, or a privilege to gain: it does not start), and installs the sandbox's filter on itself - no new namespace, no io_uring, no mount, no ptrace, sockets of unix, inet, inet6 and netlink-route only. The supervisor reads the proxy's `/proc` status again and puts it into the manifest. | **Open:** a container or namespace of its own for the proxy (the redirect by its UID, the CA handed to the agent and the control channel all assume one network namespace and one `/run`); fuzzing of its parsers as part of CI. |
| **SAI-09** the supervisor's and Firejail's capabilities | **Right**: the supervisor keeps `CAP_SYS_ADMIN`, `NET_ADMIN`, `SETUID`, `SETGID`, `SYS_CHROOT`, `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETPCAP` for the run (`agent_sandbox.py:505`). The agent cannot see or signal it (another PID namespace). | `runtime.py:2379` process_account, `:2494` isolation_manifest | Medium | **Reduced**, 27fc3071: the container's filter takes from the supervisor, the proxy and Firejail what none of them needs and a flaw in which would be the host's (modules, kexec, bpf, perf, keyring, userfaultfd, files by handle, swap, reboot, io_uring, foreign socket families). The manifest records the capability sets of the supervisor and the proxy. | **Open:** dropping the capabilities after the start. Linux capabilities are per thread, the supervisor's threads run before Firejail starts, and it needs `NET_ADMIN` for `sandbox-ai net`, `CHOWN`/`FOWNER` to give files back after `--nested-containers`. Design: a helper process started first, holding `NET_ADMIN` alone, that takes validated nftables changes over a socket pair; the supervisor then drops everything in its main thread before it starts any other. |
| **SAI-01** the host-side policy is not auditable | **Right for the reviewer** (the launcher and its tests were not in their workspace); the launcher and its 19 000 tests exist. What was missing is an account of a run's effective policy. | `runtime.py:2494` isolation_manifest, `:2544` write_manifest; `agent_sandbox.py:10723` isolation_plan | High (assurance) | **Fixed**, 27fc3071: every run writes `isolation.json` into its log and control directories before the agent starts - kernel release and settings, the supervisor's and the proxy's capability sets, `no_new_privs`, seccomp state and label, ID maps, namespaces, Firejail's version and options, each filter with SHA-256 and content, every mount with its options, devices, the firewall's SHA-256, the limits, the disk watch. `--dry-run` has `"isolation"`; doctor has four rows. A strict mode starts nothing when a part is missing (the filters, the mounts' flags, the sealed folder, the proxy's sets, the policy's label). | The OCI specification Podman generates is not in it (the supervisor sees the result, not the file); `podman inspect` of the run's container has it. A live test holds the manifest against a real start (`tests/test_agent_sandbox_live.py::test_real_isolation_...`). |
| **SAI-07** host topology in mount metadata | **Right**: `/proc/self/mountinfo` shows the host path of every bind mount (the path inside the source file system), with your user's name. It gives no access. | `runtime.py:2330` mount_table | Low | **Not fixable where the folders are bind mounts**; the manifest names mounts by their place in the sandbox only (tested against this process's own table), and README says it. | Staging every folder on a file system of its own (an overlay, a FUSE pass-through) hides the path at the cost of every file operation going through it; the kernel has no switch to hide the field. |
| **SAI-05** the per-command Bubblewrap layer is unavailable | **Right, and intended**: the launcher starts Codex with `--sandbox danger-full-access` and writes `sandbox_mode = "danger-full-access"` into its configuration (`agent_sandbox.py:6720`) because Bubblewrap cannot map IDs inside the sandbox; sandbox-ai's sandbox is the one boundary. | - | Medium (defence in depth) | **Decided, not built**: it is not part of the security model. Every command of every tool descends from the guard and keeps its namespaces, empty capability sets, `no_new_privs` and all filters; with new namespaces refused, a nested Bubblewrap cannot start at all. README says so. | Part A's `runtime.py --untrusted` (Landlock) is the per-command layer that works inside: no namespace needed. |
| **SAI-04** writable host-persistent channels | **Right, and by design**: a workspace is yours and writable, a kept HOME carries over. Partly covered before: `--mode paranoid` makes every workspace read-only and discards the HOME; `sandbox-ai resume --review-home` shows what changed in the files of a kept HOME that decide the next start (`home_review.py`), `--restore-home` puts them back; checkpoints undo a workspace. | - | High for hostile code | **The hostile-code profile is `--mode paranoid`**, which since 27fc3071 also requires each part above (nothing starts without the filters, the flags, the sealed folder, a proxy without a bounding set) and ends the run before a disk is full. README, "For code you do not trust". No new mode: a sixth name would change every list of modes in the launcher, the app and the web interface for what one existing mode now does. | **Open (designed below):** a writable output folder for a paranoid run with an import that checks every file. |
| **SAI-17** host renderers are unreviewed parser boundaries | **Not as feared, where checked.** The HTML report escapes every text and has no script, image or remote resource (`report.py:2063`, `tests/test_report.py::test_every_text_of_the_logs_is_shown_escaped`, `...never_escapes_into_the_terminal...`); notifications make markup and control characters harmless (`notify.py:364` clean); the app shows messages as plain text (`safe_tooltip`, `printable_text`). | `report.py:2060` HTML_POLICY | Medium (assurance) | **Hardened**, 27fc3071: the HTML report carries its own Content-Security-Policy (`default-src 'none'`, inline style only, no base, no form): nothing is loaded and no script runs even if a text were ever not escaped. Test with script, image and terminal sequences in every block. | The connection log goes on when its file cannot be written (and counts what was lost), as the review notes: in `high` and `paranoid` the disk watch now ends the run before that. A sink-by-sink inventory of the app and the web interface is the app's review, not this one's. |

## What was verified, and how

- **Against the kernel.** `tests/test_security_review_seccomp.py` installs each filter in a child process of the
  test (x86-64 here; arm64 where the tests run there) and asks: `io_uring_setup` and `io_uring_enter` `EPERM`,
  `AF_VSOCK`, `AF_ALG`, RDS and TIPC `EAFNOSUPPORT`, netlink-generic `EPROTONOSUPPORT`, a new user namespace
  `EPERM`, an unknown call number `ENOSYS` - and a thread, a fork, a program, unix, inet and netlink-route sockets
  work. The kernel accepted all three programs (container, sandbox, nested).
- **Every architecture, by an interpreter.** The same file runs the classic BPF programs for x86-64, its 32-bit
  calls, arm64 and its 32-bit calls: every named call, every number up to the table's end, the argument rules for
  `clone`, `unshare`, `socket` and `socketpair`, the high halves of 64-bit arguments, x32.
- **Against other tables.** The call numbers equal libseccomp's (where installed); the sandbox's filter refuses
  everything the installed Firejail's default filter refuses, and its additions are exactly the listed ones;
  Firejail's `--restrict-namespaces` and `--protocol` filters were decoded and agree.
- **Through the real entry point.** `./sandbox-ai claude --dry-run` (the plan's `isolation`, a strict mode's notes,
  `SANDBOX_AI_SYSCALL_FILTER=off` warned about or refused), `./sandbox-ai doctor --json` (the four rows):
  `tests/test_security_review_isolation.py` (18).
- **Contracts**: 36 new top-level functions with happy, sad and edge cases in
  `tests/test_agent_sandbox_contracts.py`.
- **Not verified here: a real start.** There is no Podman in the sandbox this was made in, and its Firejail cannot
  be started from inside a sandbox. What that leaves unproven: that Firejail 0.9.72 starts with
  `--restrict-namespaces --protocol=...` under the container's filter, that the supervisor's tools (nft, Xvfb,
  x11vnc, PulseAudio) need nothing the container's filter takes, and that the arm64 program is accepted by a real
  arm64 kernel (it differs from the tested one in numbers only). Two live tests hold a real start to all of it:
  `pytest --agent-live tests/test_agent_sandbox_live.py -k isolation` and `-k strict_mode_starts`. They must pass
  before a release. If a start fails there, `SANDBOX_AI_SYSCALL_FILTER=off` is the way to start meanwhile (not in
  the strict modes), and the supervisor's message names what did not hold.

## Decisions taken without asking

- **The filters are on in every mode**, not behind an option: the review's findings are about the ordinary
  profile. The compatibility they cost is Docker's default profile's (io_uring refused, `clone3` `ENOSYS`), plus
  socket families no agent tool uses. One environment variable switches them off for a start
  (`SANDBOX_AI_SYSCALL_FILTER=off`), never in `high` and `paranoid`.
- **No new start option and no new mode.** The administrator's part is a policy key (`lsm`); the human's switch
  is an environment variable beside the other `SANDBOX_AI_*` ones; the hostile-code profile is `paranoid`.
- **`ENOSYS`, not `EPERM`, for what the table does not know and for `clone3`**: programs fall back on "no such
  call" and give up on "not permitted".
- **The disk watch ends a run only in the strict modes** (512 MiB); elsewhere it warns (256 MiB). A default that
  ends runs on a nearly full disk would end them on the machine this was made on.
- **A strict mode does not refuse to start without AppArmor or SELinux.** It says so at every start instead.
- **32-bit programs lose `socketcall`** in every mode: with it open, a 64-bit process reaches every socket family
  by `int 0x80`, and the family filter would stop only the programs that do not try.

## Designed, not built

**An output folder for a paranoid run, and its import (SAI-04).** `--output DIR`: the launcher makes an empty
staging folder in its state directory, mounts it at `/output` (`rw,nosuid,nodev,noexec`) - the one writable host
folder of the run, allowed in the strict modes because nothing reads it directly - and when the run ends imports
it into DIR: the tree is opened once (`O_NOFOLLOW`, by descriptor, never by path again); regular files only, with
one name, no set-ID or sticky bit; names without control or bidirectional characters, no two that differ only in
case or Unicode form; nothing that decides what runs later (`.git`, hooks, agent and editor settings, CI
workflows) unless named; within budgets of bytes, files and depth; content copied into fresh files (0644, no
extended attributes, holes written out against the byte budget). What is refused is listed and stays in the
staging folder for `sandbox-ai` to show; nothing is imported partially without `--partial`. Cost: one option with
its documentation in three interfaces, a module of about 300 lines with tests for link swaps, FIFOs, sparse and
huge files and name collisions, and the end-of-run hook in the session supervisor and the foreground launcher.

**The supervisor without capabilities (SAI-09).** As in the table: a helper with `CAP_NET_ADMIN` alone for
nftables, started before any thread; the supervisor drops the rest in its main thread, then starts its threads.
`--nested-containers` keeps `CHOWN` and `FOWNER` in a second helper for the give-back at the end. Cost: the
start's order in `inner_bootstrap` (nested setup, display, proxy start all need capabilities and run after
threads exist today), and every `sandbox-ai net` path.

**An AppArmor profile of sandbox-ai's own (SAI-02).** Two profiles in one file, loaded by the administrator
(`sudo apparmor_parser -r`): `sandbox-ai` for the container (mount, pivot_root, userns, network admin, signals to
its children; no access to `/sys/kernel/security`, `/proc/sysrq-trigger`, `/proc/kcore`, raw devices) and a child
profile `sandbox-ai//agent` that the guard enters before it starts the agent (`aa_change_onexec` through
`/proc/self/attr/exec`: no library needed), without mount, userns, ptrace of anything outside, or write access to
`/proc/sys`. The policy's `lsm` and the supervisor's label check are the hook it plugs into.

**A measured allowlist (SAI-16).** Record the system calls of the live suite's runs of each agent with the
filter in a logging mode (`SECCOMP_RET_LOG`), take the union, and refuse the rest; `SYSCALL_NUMBERS` then lists
what is allowed instead of what is refused. Needs the live suite and a release of soak.

## Merging with part A

Both branches change `sandbox/runtime.py`, `agent_sandbox.py`, `doctor.py`, the contracts test, the live test, the
twelve skills, README and the release notes. B's changes were placed so that no hunk touches a line A changed:
its runtime sections stand before `namespace_state` (A changed nothing there), its lines in `firejail_command`,
`inner_guard` and `inner_bootstrap` are at least a line away from A's, doctor's new checks stand before
`check_login` and its group entry on the Engine line, the contracts stand before `test_namespace_state`, the live
tests before `test_practical_dangerously_skip_permissions...`, the skill's paragraph before the telemetry one,
and the release notes at the end of the file. One thing to do by hand after both are merged: A's
`untrusted()` runs a command that already has B's filters (it descends from the guard); nothing to add.

## What was deliberately not done

- No change to the images (Firejail's version) and no AppArmor or SELinux profile: neither could be built, loaded
  or started here, and both fail every start when they are wrong.
- No Podman seccomp file: the supervisor's own filter does the same from inside, works for Bubblewrap,
  `--podman-url` and podman machine alike (a profile file would have to exist on the engine's side), and could be
  tested against a real kernel here.
- No `noexec`, no I/O limits, no allowlist of calls: each breaks ordinary work unless measured first.
- Nothing was probed outside this sandbox; the probes make and close sockets and call into the kernel with null
  arguments. No credential was read.

## Test results

The whole core suite in CI's four parts (`ls tests/test_*.py | awk 'NR % 4 == part'`), on the branch's last commit
that changes code (the read of a run's isolation tolerating deep nesting):

| Part | Passed | Skipped | Not passed |
| --- | --- | --- | --- |
| 0 | 6713 | 64 | - |
| 1 | 4213 | 143 | 1 error |
| 2 | 3744 (and 120 subtests) | 82 | 6 failed |
| 3 | 4646 | 52 | - |

19 316 passed, 341 skipped. The seven that did not pass are tests that read the MCP catalogs from
raw.githubusercontent.com (`tests/test_mcp_variables.py`, its `mcp add mcp:duckduckgo` fixture;
`tests/test_deep_round_1.py`, four cases with `mcp add hub:binwalk`; `tests/test_live_regressions.py`,
`test_exiftool_needs_folder` and `test_mcp_add_pull_failure_is_a_message`): "handshake operation timed out",
"Connection reset by peer", "read operation timed out" - the sandbox's network to that host was slow while the
suites ran (measured beside them: TLS handshakes of 0.3 to 11 s, one request of three without an answer in
30 s). Each of them passed when repeated on the same commit; none touches what this branch changes. An earlier
run of parts 0 and 1 found one real failure, which the last commit fixes
(`tests/test_stability_round3.py::test_every_json_read_that_tolerates_damage_tolerates_deep_nesting_too`).

The new tests: `tests/test_security_review_seccomp.py` (48), `tests/test_security_review_isolation.py` (18), 108
contract cases for 36 functions in `tests/test_agent_sandbox_contracts.py`, two live tests
(`tests/test_agent_sandbox_live.py`, skipped without `--agent-live`). On `main` the two new files do not load
(the functions do not exist). The app is unchanged; its suite was not run.
