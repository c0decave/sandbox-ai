# Final review 3 of 3: robustness, concurrency, regressions (2026-11-17)

Scope: everything merged into `main` between 511a33b8 and 056fd47c (61k lines, 347 files: state directories and
gc, `--with` for every agent, log reviews 1-3, bridges across hosts, remote folders, secrets' primary password, MCP
copy and MCP egress, uninstall, `list --all-users`, plugins, teams on the Board, the app's host-following pages,
the web interface). Focus: races, a kill in the middle of a step, a full disk, slow, unreachable and old hosts, many
sessions and hosts, large inputs, descriptor and thread leaks, macOS paths, Python 3.9; and the whole core and app
test suites run until green, every failure and flake fixed at its root.

Branch: `review/final-3` (worktree `.claude/worktrees/final3`), not merged. Every fix has a regression test that
failed before it (`tests/test_final_review3.py` unless named otherwise).

Severity: **H** high (data lost, the suite or a feature unusable), **M** medium, **L** low.

## 1. Findings

1. **H (suite) / L (use) - A launcher kept one descriptor per run it ended: "filedescriptor out of range in
   select()".** `register_run` took `runs/RUN/supervisor.lock` (`hold_supervisor_lock`) and the descriptor was never
   closed - by design while the launcher runs, but also after the run's directory was removed. Every in-process start
   of the suite added one: `test_feature_matrix.py` alone ended with 926 open `supervisor.lock (deleted)` descriptors
   (956 in all), and the next `select()` of a test in the same process failed (`test_terminal_frames` in the first
   full run). In use: a launcher that goes on after a run (a team's) held one per run. Fixed:
   `release_supervisor_lock`, the last step of the run's cleanup unless the sandbox outlives the launcher.
   test_feature_matrix now ends with no descriptor more than it began with
   (`test_a_launcher_holds_no_descriptor_of_a_run_it_ended`, contract `test_release_supervisor_lock`).
2. **M - select() where long-running processes reach descriptors of 1024 and above.** `runtime.wait_ready` (poll)
   existed for the proxy, but in the same supervisor process the terminal relay (`relay_terminal`), the shell relay
   (`relay_shell`), the keep-alive of a local model's stream (`llm_kept_alive`), the file logger, the canary watch and
   the proxy's start still used `select.select`. `llm_kept_alive` swallowed the ValueError: in a busy proxy no
   keep-alive at all, and Claude Code gives a silent local model up after 300 s - the very case the keep-alive is for.
   The web interface's terminals (`webui`) and the app's SFTP connections (`remote.py`) had the same limit. Fixed: all
   of them wait with poll() (`runtime.wait_ready`, `webui.ready_fds`, `remote.fd_ready`); only the clients' own
   terminals on the host keep select() (few descriptors; poll() does not take a macOS terminal). A guard test keeps
   select() out of the supervisor.
3. **M - `secrets delete` cut off left a secret of zero bytes.** `FileStore.delete` wrote the file over with zeros and
   then removed it; killed in between, a file of NUL bytes stayed, read back as the secret's (plain) value by every
   start that uses it. Fixed: the name goes first, the blocks are written over through the descriptor opened before.
4. **M - `secrets set` raced by `secrets password remove` lost the secret.** The value was sealed and written without
   the store's lock: a remove in another process in between decrypted every file and removed the header, then the
   new sealed file came - nothing can open it any more. Fixed: the write checks under the store lock that the store
   is as it was when the value was sealed (no password set, changed or removed meanwhile), else writes nothing and
   says so.
5. **M - A remote folder whose host is gone held `gc --usage`, the app's Disk usage, uninstall's sizes and the end
   of a run.** `statedirs.Scan` did `lstat()` on every folder below the state directory, the mount points under
   `state/sandbox-ai/remote/RUN/N/LABEL` too; `remotefs.release` asked `is_dir()`/`exists()` of each mount point before
   unmounting it. On an NFS mount (hard by default) or a reconnecting sshfs whose host is gone such a look waits as
   long as the host stays away, past any time budget. Fixed: Scan skips mount points by mountinfo without looking at
   them (said as partial); release finds its mount points by their folders' listings (scandir's entry types).
6. **L - A killed compaction left its temporary file for good.** `compress` and `split` write `.NAME.gz.PID.tmp` and
   `.NAME.rest.PID.tmp`; killed (the daily background pass at a shutdown, kill -9) they stayed - as large as the log -
   counted by `gc --usage`, removed by nothing. Fixed: `compact` removes such a file once its process is gone and it
   was not written for an hour (`left_behind`; one of a compaction that still runs stays).
7. **L - `mcp copy --copy-image` could wait an hour for a chatty sender.** `copy_image` read the receiver to its end
   before anything of the sender's stderr pipe: a sender saying more than 64 KiB (podman save's progress, ssh's
   warnings) stopped half-way and the copy waited `IMAGE_TIMEOUT` (3600 s). Fixed: the sender's stderr goes to a
   temporary file (its last 64 KiB read for the message); on a timeout both processes are waited for.
8. **L - The proxy's questions waited their whole time after the supervisor's channel ended.** `AskChannel._read`
   ended on a reset with an unhandled `ConnectionResetError` (a thread exception in the suite) and on any end left
   the questions already asked waiting `APPROVAL_SECONDS + 10`; a JSON line that was not an object ended the reader
   too. Fixed: the channel's end answers every waiting and later question at once (denied, "no answer").
9. **L - A bridge call with no thread to serve it stopped the link's reader.** `Link.dispatch` started a thread per
   call without handling RuntimeError (the process limit): the reader ended silently, the call's slot kept, nothing
   read from the other half until `LINK_IDLE`. Fixed: that call is refused with the reason, the reader goes on.
10. **L - `bridge list --hosts` with a host that does not answer.** The threads were joined `HOSTS_WAIT + 15` s each
    in turn, the list sorted while a late thread could still add to it ("list modified during sort"), the late host
    not said. Fixed: one deadline for all, the rows taken under the lock, a host without an answer named.
11. **L - Goose's guard ended with a traceback on a settings file nested too deep** (`telemetry_guard.goose_hooks`,
    the known failure of `test_stability_round3`'s deep-nesting guard on main). Fixed: said like a damaged file.
12. **L - The Bubblewrap egress rules loaded libc in nft's child.** `load_into_sandbox`'s `preexec_fn` called
    `ctypes.CDLL` between fork and exec: a dlopen() in the child of a process with threads can wait forever on a
    loader lock held at the fork. Fixed: libc is loaded in the parent and handed to `enter_namespaces`.

12a. **L - The app's From another host… dialog (mcp copy)** asked for a missing secret's value with `QInputDialog` and
    a password echo set by hand (no show-value toggle, the app's rule for every secret field) and put the target's
    host name and the source's folder into a label and two tooltips unescaped (Qt reads a tooltip as rich text). Both
    were caught by the app's own guards, failing on main. Fixed: `widgets.ask_secret_value`, `safe_tooltip`.

### Test-suite defects (no product bug)

13. **H (suite) - A run that collected `test_make_pyz.py` with any `PYTHON*` variable set (PYTHONPATH,
    PYTHONUNBUFFERED) ended with exit status 1 and no output.** `dist_boot.py` re-executes itself with `-I`
    (`os.execv`) when such a variable is set - correct as the .pyz's start, but loaded by the test module it replaced
    the pytest process itself. The test now loads it without them.
14. **`test_invalid_network_input` failed on main** since allow-type options take several items: `"example.com\naccept"`
    is two hosts now, each checked (no injection: a rule never gets the line break). The test checks that, and
    `example.com;accept` for the injection.
15. **`test_review_core_r1`'s plugin-store refusal failed on main**: since plugins for Codex and OpenCode 2, the store
    takes `claude, codex, opencode, opencode2 or all`.
16. **Processes left behind:** 17 `--internal-mcp-serve` processes (500 MB) of `test_mcp_variables`' session start
    were running on this machine from other worktrees' earlier runs (left alone). With main's code the test leaves
    none (checked); the full runs below list every process left with the shard's folder in its command line.

### Looked at, not changed

- **`remove_password` cut off between wiping and unlinking the header** leaves a header of zeros: the store then says
  "the primary password's file is damaged" although every secret is plain again (recovered by removing the file the
  message names). Same fix as finding 3 (unlink, then wipe through the descriptor); not made here because the
  parallel `feature/seal-ovpn-tokens` branch rewrites those lines.
- **Wrong-password throttling** (`_note_attempt`) reads and writes `.attempts` without a lock: two wrong attempts at
  the same moment count once. Small; a lock there must not be the store lock (unlock runs under it).
- **Kernel NFS mounts are hard mounts** (`remotefs.kernel_command` passes no `soft`/`timeo`): a host that goes away
  leaves every access - the agent's, an `umount` without `-l` - waiting in the kernel; `subprocess.run`'s timeout
  cannot end a process in that state. Whether to mount `soft,timeo=...` (errors instead of waits, at the risk of a
  write lost) is a design choice for a live test.
- **homepack** (pack and unpack of kept HOMEs): every step killed in turn was walked through - `tidy` finishes or
  undoes each state; nothing found.
- **uninstall**: a purge killed half-way is finished by running it again (the plan is made anew); mount points are
  found by mountinfo (on macOS by a walk that stats - remote folders are Linux-only).
- **list --all-users**: every read is bounded (file, directory, Podman answer sizes, timeouts).
- **select() left** where the descriptors stay few: the VPN container (`vpn_runtime`), the secrets agent and its
  start, the launcher's VPN tunnel start (`vpn.Tunnel`) and its one-time TIOCSTI check, the clients' terminals on the
  host (`attach_client`, `shell_client`).
- **Python 3.9**: `test_python39_compat` passes on the branch (no 3.9 interpreter on this machine any more; the
  syntax-tree checks ran).

## 2. The full suites

Core (`tests/`, 273 files, shards by file in parallel pytest processes; the second run with a plugin that counts
each test's open descriptors and lists the processes left behind with the shard's folder in their command line):

| run | passed | skipped | failed | what failed |
| --- | --- | --- | --- | --- |
| 1 (main + the first fixes, 6 shards) | 17,408 | ~340 | 3 | `test_invalid_network_input` and `test_review_core_r1` (stale on main, items 14-15); `test_terminal_frames` (`filedescriptor out of range in select()`: items 1-2) |
| 2 (the branch, 5 shards) | 17,431 | 337 | 1 | the deep-nesting guard on this review's own `AskChannel._read` (fixed: RecursionError) |
| after | 1,238 (bridges, mcp copy, proxy, stability, web, this review's) + `test_agent_sandbox.py`, the contracts' `send_all`/`relay` | | 0 | |

The second run's descriptor count: test_feature_matrix's 956 descriptors are gone; what remains per shard is 17-179
descriptors in all (sockets of servers a test keeps for its module, kept on purpose), of which the largest single
leak - 60 listening MCP servers and their threads of `test_review_mcp_socket_appears_only_once_listening` (+90) - is
closed now. No process was left behind by any shard. One thread exception (`test_send_all[edge]`'s reader on a
closed socket, reported at `test_relay[happy]`) is fixed in the test.

Not every shard ran at once: a shard that collected `test_make_pyz.py` with `PYTHONPATH` set ended at once with exit
status 1 and no word (item 13); it ran again after the fix.

App (`projects/sandbox-ai-app/tests`, offscreen, one process, 50 minutes): **3,708 passed, 20 skipped, 3 failed** -
`test_review5::test_no_tooltip_or_dialog_label_takes_unescaped_text` and
`test_round4_presets::test_no_field_hides_its_value_but_through_the_show_value_toggle` (both on main: `mcp copy`'s
dialog asked for a secret with a hand-made password field and put a host's names into tooltips and a label
unescaped - fixed with `widgets.ask_secret_value` and `safe_tooltip`), and
`test_mcp_variables_ui::test_the_mcp_part_shows_variables_and_two_buttons_of_their_own_size` (a flake under load: an
answer that came in during `processEvents()` rebuilt the inspector and deleted the buttons the test held - it now
measures the ones shown). The three files with `test_mcp_copy_app.py`, `test_remote.py` and `test_files_remote.py`
pass after the fixes (110). Three `fake_sftp_server.py` processes stayed until the app run ended (SFTP connections a
test opened and did not close; nothing left after the run).

## 3. What needs a live test

- The supervisor's terminal relay and the LLM keep-alive under a real busy proxy (more than 1024 descriptors).
- `gc --usage` and a run's end with a real sshfs/NFS mount whose host was switched off.
- `mcp copy --copy-image` between two real hosts with a large image.
- The web interface's terminals after many sessions (poll on a real pty master).
