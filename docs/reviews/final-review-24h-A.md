# 24h review A: security and core correctness (2026-11-17)

One of two independent reviews of everything merged into `main` in the last 24 hours (`f4edf0a6..167ee6e9`: 374
files, about 35k lines of product code). This one's focus: security and core correctness. Review B (user journeys,
robustness) is in `final-review-24h-B.md`; the two did not coordinate.

Branch: `review/24h-A` (worktree `.claude/worktrees/review24-A`), nine commits on top of 167ee6e9, **not merged, not
pushed**. Every fix has a regression test that failed before it.

Independence: the earlier reviews in this folder were not read for this one - with one slip: the first 25 lines of
`final-review-3-robustness.md` (its scope and its first finding, the supervisor.lock descriptors) were printed while
looking for the form of a report. Nothing below comes from it.

Severity: **M** medium, **L** low, **I** information (nothing to fix, or a decision for the human).

## 1. What was read, and how deep

Read in full, line by line: `oauth_relay.py`, the control op `oauth-callback` and `ApprovalBroker` in `runtime.py`
(ask, late answers, granted, control/decide), `approvals.py` (answer, decide_again, redecide, questions, listed, the
command line), `monitor.py` (the counters, Series, Recorder, the readers, top, usage), `start_monitor` in
`agent_sandbox.py`, the whole diff of `webui.py` (every new route), the crypto, the header, the attempt counter and the
agent of `secret_store.py`, sealed files and moved tokens, `allusers.py` (reading as data, the drop), the purge of
`uninstall.py`, `remotefs.py` (parse, the sshfs and mount command lines), the read and bundle side of `hostkits.py` and
the import of `kitctl.py`, `telemetry_guard.codex_login_args`, the list options (`ListOption`, `spread_lists`), the
traffic light (`model_profiles.py`), the app's `monitor_page.py` actions and the answers of its Approvals page, the
web's new JavaScript for what it opens and inserts.

Read for their structure and their trust boundaries, not line by line: `mcp_copy.py`, `bridge_remote.py`, the local
model relay's new parts in `runtime.py` (title, hidden tools, the client watch, rejected requests), `statedirs.py`,
`homepack.py`, `team_image.py`, the app's other pages. The merge messages say that three reviews went over most of
these before; this one put its time where the newest code is (approvals rework, the callback relay, the monitor, the
traffic light, kits from the host, sealed files).

Tried, beyond reading: 270 malformed series (every header and sample key with 15 odd values) through `stats usage`
in every grouping and window; 570 malformed live records through `top`; the catcher with two questions on one port,
as real processes; links and a FIFO in a log folder under the collector.

## 2. Findings

| # | Sev | Where | What | State |
|---|-----|-------|------|-------|
| 1 | M | `sandbox/monitor.py` `Series.trim`, `Series.append`, `write_json` (at 167ee6e9: lines 728-760, 800-810) | The collector read and wrote through links in the run's log folder, and waited on a FIFO there with its lock held | fixed, a5b5f3d1 |
| 12 | M | `sandbox/approvals.py` `redecide` (2275-2312), `sandbox/runtime.py` `ApprovalBroker.control` (14041-14044) | Undo / ask-again / deny on an allowed name left it pinned and its addresses open | fixed, f4397b03 |
| 2 | M | `sandbox/oauth_relay.py` `start` (502-511) | A second login on the same port within ten minutes: "port busy, use the device-code login" | fixed, 71f615b6 |
| 3 | L | `sandbox/oauth_relay.py` `start` / `relay_once` (506-509, 411-415) | A second start for one question wrote "port busy" into the record that also means "relayed": the catcher's one request was refused | fixed, 71f615b6 |
| 4 | L | `sandbox/oauth_relay.py` `relay_once` (397-426) | A request of another shape (from `approvals callback`'s standard input): TypeError / AttributeError, partly after the one relay was taken | fixed, 71f615b6 |
| 5 | L | `sandbox/monitor.py` `clean_record` (1249-1267), `usage`, `summarise` | One odd line in any project's series ended `stats usage` (and the app's and the web's page) with a traceback | fixed, 080f4544 |
| 6 | L | `sandbox/hostkits.py` `scan` (342), `sandbox/kitctl.py` `cmd_host_import` | The secrets check before an import read the first MiB of each file only | fixed, cf17b58c |
| 7 | L | `sandbox/secret_store.py` `derive` (227-232) | A header's scrypt parameters could ask for 17 GiB, against the function's own promise | fixed, 50e9e393 |
| 8 | I | `sandbox/monitor.py` `sandbox_top` (253-264) | The innermost `libpod-*` cgroup counts as the run's | fixed in backlog A (Podman's own forms only) |
| 9 | I | `sandbox/oauth_relay.py` `Catcher.read` (272-275) | Any local process, and any site's top-level navigation, can use up the one request | fixed in backlog A (only the callback with the login's state) |
| 10 | I | `sandbox/approvals.py` / app / web | "Allow always" and "Block always" are no longer confirmed | by design: named for the human |
| 11 | I | `sandbox/monitor.py` `without_host` (1771) | `top --hos HOST` (an abbreviation) is passed on to the host | fixed in backlog A (abbreviations resolved first) |

### 1. The collector followed links in the run's log folder (M)

The series of a run lie in its project's log folder (`monitor-TIME-RUN.jsonl`, `-1m-`, `-1h-`). That folder is mounted
read-write into the sandbox (`agent_sandbox.py`: `bindings.append((a.log_dir..., LOG_DIR, "rw"))`; the supervisor
writes the logs there; the agent itself does not see it). The collector is a thread of the launcher on the host, as
the human. Everything else the host writes there goes "never through a link" (`activity.append`: O_NOFOLLOW, a regular
file); the monitor's `append` opened with O_NOFOLLOW too, but:

- `Series.trim` read the series with `path.read_bytes()` and wrote its replacement with
  `temporary.write_bytes(...)`, the temporary file's name known (`.NAME.PID`). A link at the temporary name: the
  human's file it leads to is written over with the series. The series' path swapped for a link before a trim: the
  last lines of the file it leads to are copied into the log folder, where the sandbox's side reads them.
- `Series.append` counted the lines of whatever the path led to (`open(path, "rb")`), and both it and the open for
  appending waited on a FIFO for ever - with the series' lock held, so `Recorder.stop()` (the launcher's end:
  `series.close()` takes the same lock) waited too and the run's cleanup never came.
- `write_json` (the live records, in the state directory: not the sandbox's) wrote its temporary file through a link
  as well, against its own docstring.

Who can do it: what runs as root in the run's container outside Firejail (the supervisor's side), not the agent. So
this is the second line, not the first - but it is the line the other log writers hold. Fixed: `open_series`
(O_NOFOLLOW, O_NONBLOCK, a regular file or OSError), lines counted and read through that descriptor and bounded
(`TRIM_LIMIT`), temporary files made exclusively (`write_private`). Tests: `tests/test_monitor_log_folder.py` (a link
in the temporary file's place, the path made a link, a link at the first append, a FIFO, the live record).

### 2. A second login on the same port was told "port busy" (M)

`codex login` again within ten minutes of the first (the tab closed, the wrong account, the first try failed): the
new question's callback is the same `127.0.0.1:1455`, which the first question's catcher still holds. `start()` took
that for a foreign program: "cannot be relayed: port 1455 is busy; use the device-code login". The callback then went
to the *old* question's catcher (which relays it while its own eleven minutes last, recorded under the old question)
or nowhere. Fixed: a catcher says that it listens (`callbacks/PORT.json` in the state directory: its PID and
question); when a port is busy, `start()` looks whether that is a catcher of sandbox-ai (the process's command line
must be `oauth_relay.py --internal-catch FD ID`, for that question - a record alone ends nothing). The same question's:
it listens already, nothing more to do. An earlier login's: it is asked to close (SIGTERM; it closes between two
connections and records "closed", a request it is relaying is finished first) and the new one takes the port; the
note says so. Anything else on the port: busy, as before. Tests:
`test_a_new_login_on_the_same_port_takes_it_from_the_older_catcher` (real catcher processes),
`test_only_a_catcher_of_sandbox_ai_is_taken_for_one`.

### 3. "port busy" written into the record that means "relayed once" (L)

`ID.callback` is made with O_EXCL by `relay_once` - that is what makes the relay happen once. `start()` wrote its
"port busy" note into the same file. `sandbox-ai approvals relay ID --url URL` a second time while the question's
catcher listened (by hand; the app and the web send it once per answer) found the port busy - by that catcher - and
wrote the note; the catcher's one request was then refused as "relayed already". Fixed: a record whose outcome is
"port busy" is no relay; `take_once` replaces it, one taker at a time (a lock beside it), and with finding 2 a second
start for the same question no longer writes anything. Test:
`test_a_second_start_that_finds_the_port_busy_does_not_use_up_the_questions_one_relay`.

### 4. A callback request of another shape (L)

`approvals callback ID` reads JSON from another machine's catcher. `int(request.get("port"))` with a list or an
object: TypeError; headers that are a list or a text: AttributeError; a body that is a number: TypeError inside the
`try` - after `ID.callback` had been made, so the question's one relay was gone too. All of them tracebacks, not
"sandbox-ai approvals: ...". Fixed: the shape is checked first (ValueError, nothing taken). Test:
`test_a_request_of_another_shape_is_refused_not_a_crash` (7 shapes).

### 5. One odd line ended `stats usage` for every project (L)

The module says its readers "take every file as untrusted data"; `clean_record` kept any number under any key. A
header with `"agent": 5`: `stats usage --by agent` sorted groups by `(…, key)` and compared 5 with "claude"
(TypeError). `"mcp_servers": 5`: `"m" in 5` in the attribution (TypeError). A sample with `"dt": 1e300`:
`int(inf)` in the totals (OverflowError). Each ended the command, the app's System monitor trends and the web's page -
for every project, since all series are read together. The series lie in the log folders (finding 1: the sandbox's
side can write there). Fixed in `clean_record`: text keys take texts only, `mcp_servers` a list, a measurement is a
number from 0 to 1e15 (more than 0 for what others are divided by: dt, n, interval). Tests: 7 cases in
`tests/test_monitor_log_folder.py`; the two fuzzers of section 1 find nothing now.

### 6. A secret behind the first MiB of a file passed the import's check (L)

`kits host-import` refuses parts with secret-looking lines unless `--allow-secrets` - a kit's copy reaches every
sandbox that uses the kit. `hostkits.scan` read `data[:SCAN_LIMIT]` (1 MiB) of each file; a skill may hold files up to
64 MiB. A reference file with a token further in was imported without a word, and `host-show` showed none. Fixed: an
import and `host-show` check every byte (also of a bundle from another host: scanned here, as before); `host-list`
still reads the start only and says `"secrets": null` (not known), not 0, for a part with a longer file. Test:
`test_a_secret_behind_the_first_mebibyte_of_a_file_is_found_before_an_import`.

The app showed its "Also the parts with secret-looking lines" box only for a counted finding: a part the list had
not checked whole (null: a longer file, or past the list's budget - that case existed before) was refused by the
import with no way to allow it from the app. The box is offered for such a part now
(`agent_parts_view.flagged`; app test `test_the_box_is_offered_for_a_part_the_list_did_not_check`). The web's page
always has the box.

Left as it is: a file with a NUL byte in its first 8 KiB counts as binary and is not scanned (UTF-16 notes); the
check is a help, not a guarantee, and says "secret-looking".

### 7. A header's scrypt parameters could ask for 17 GiB (L)

`derive()` bounds the parameters "so that a changed header never makes this process take gigabytes" (also
docs/SECRETS_DESIGN.md) - n up to 2^22 with r up to 32 is 128 * n * r = 17 GiB for one attempt, in whatever unlocks:
a terminal, the app, the web interface's service. Only someone who can already write the user's private store can
change the header, so this is a broken promise more than an attack. Fixed: 128 * n * r at most 1 GiB (the store's own
cost: 128 MiB). Test: `test_a_changed_header_never_makes_scrypt_take_gigabytes`.

### 8. The innermost `libpod-*` cgroup counts as the run's (I, open)

`sandbox_top` walks from the process's cgroup upwards and takes the first `libpod-*` it meets. A process that could
make a cgroup named `libpod-x` below the container's and move the measured process into it would be measured alone -
the run's use (and its warnings) would look small. The measured process is the container's PID 1 (the supervisor),
which the agent cannot move, and rootless Podman mounts the container's cgroup read-only: not reachable from the
agent. The outermost match would be wrong for sandbox-ai inside a sandbox (the outer pod's). Left; the monitor informs,
it enforces nothing (the limits are the kernel's).

### 9. Who can use up the one callback (I, open)

The catcher takes one request for the callback's path from this machine's loopback: a request without Sec-Fetch
headers counts as a navigation (older browsers, curl), and any site may send the browser there by a top-level
navigation. So another local user's process, or a page open in the same browser, can use the one request up within
the ten minutes (the login then fails; start it again), and can hand the agent a code of its own - which the agent's
own `state` check refuses, as with any loopback redirect (Codex's and Claude Code's own logins have the same shape on
a machine without sandbox-ai). Nothing of the human's reaches the sandbox by it: no cookies, no Origin, no Referer;
the agent's page comes back inert (CSP `sandbox`, no script). Nothing to fix without breaking the redirect.

### 10. "Always" is answered as clicked (I)

Since the approvals rework the app and the web send "Allow always" and "Block always" without a second question (the
web's `approval-allow-always` / `approval-deny-always` confirmations are gone; Undo for 8 seconds, which takes it
back for the run - the project's entry stays until forgotten under Remembered, and the page says so). The caps still
hold on the host and in the supervisor (the policy's `max_approval_scope`, the security mode, pushes, requests, tool
calls and devices at most for the run, a name remembered only when proved). This is the design of that merge, not a
defect; named here because it is the one place where the new flow keeps something across starts on one click.

### 11. `top --hos HOST` (I, open)

argparse takes `--hos` for `--host`; `without_host` removes only `--host` spelled out, so the host is asked for
`top --hos HOST` and looks for a host of that name among its own. An error message there, nothing more.

### 12. Taking an allowed name back did not close it (M)

Found last, listed first among the mediums. The rework's promise is that every answer can be changed: Undo on the
page for eight seconds (it sends ask-again), `approvals ask-again ID`, `approvals deny ID`. For a connection the
supervisor takes the firewall's entry back (`control`: `network.change({"op": "revoke", "tcp": ...})`). For a name it
only added a note - "the name stays pinned and its addresses open: sandbox-ai net revoke RUN --allow-host NAME takes
them back" - as the second line of the command's output: the app's message shows it behind "Undone.", the web's
"Undone: 1" does not. So: Allow on `evil.example.org` by mistake, Undo at once, "Undone" - and the sandbox reaches
the name for the rest of the run (its line in /etc/hosts, its addresses in the firewall and the proxy's list).
`approvals deny ID` on the allowed name: the same, plus a deny that only later lookups would have met.

The pin and the addresses are the host's (`net.update_hosts`, names.json), so the host takes them back: `redecide`
(deny, ask-again - not its internal use after `allow ID` again) runs what the note named, `net revoke RUN
--allow-host NAME` (no lookup; an address another allowed name needs stays), when the question was allowed and the
name was pinned by an answer of this run - a name of the start's own options is not the question's to close - and
says "NAME is no longer pinned and its addresses are closed again". Tests:
`test_taking_an_allowed_name_back_closes_it` (ask-again, deny; through the control socket, /etc/hosts, names.json and
the run's network), `test_allowing_an_allowed_name_again_keeps_it_open`.

## 3. Read and found sound

- **The callback relay's boundaries.** The catcher binds 127.0.0.1 (or ::1) only, takes the callback's path, Host
  127.0.0.1|localhost|[::1]:PORT (no DNS rebinding), GET or POST, a navigation; forwards method, target, body and
  four headers - never cookies; the host's side checks the question again (a browser question, allowed, of a running
  sandbox, the same port and path, within 11 minutes of the answer, once); the supervisor reaches only the sandbox's
  loopback on that port, follows redirects only to the same port there (three at most) and returns the page, which
  the browser gets with `Content-Security-Policy: ... sandbox` and no script. A redirect elsewhere is shown as a
  link, not followed. The control op is refused for a peer inside the sandbox like every other op.
- **Approvals: can a sandbox influence its own approval?** Answers are files only the host writes; the broker reads
  decision and scope and caps the scope again (policy, mode); a late "allow once" is kept for the exact request
  (its slot: for a tool call the digest of its arguments) and used up by its next try; `decide` is reached through
  the control socket only, looks the question up by its ID in the broker's own directory, refuses an open one,
  holds the cap for an allow for the run, and takes an allowed connection back from the firewall on deny or
  ask-again. A name is always resolved on the host (through the run's VPN where it has one), only its public
  addresses are opened, a canary's name is never looked up - also on the new path for answered questions
  (`decide_again`). "Always" for an answered name is remembered as an answer (public addresses at every start), not
  as the project's `--allow-host`.
- **Secrets.** HMAC-SHA-512 in counter mode with encrypt-then-MAC, keys separated per message by a random 24-byte
  nonce, the AAD length-prefixed, the tag compared in constant time and checked before anything is decrypted; the
  header's key wrapped the same way, bound to version, id and KDF parameters; wrong passwords counted in the store
  under a lock of their own (guesses sent at once are attempts in a row). The agent: a socket in a folder that must
  be the user's alone (each level checked, never a second place), the peer's uid from the kernel on both ends, the
  key from a pipe, no core dump and not dumpable, it seals and opens and never gives the key out; a Bubblewrap
  sandbox (same uid) sees neither /run nor /tmp of the host. Sealed files are bound to their name and key reference.
  A moved token file names its secret - no more than `--git-token-secret` could name, and only from a private file
  of the user's own without other hard links.
- **Root reading other users.** Every directory opened relative to the one above without following links, owner and
  inode checked again on the descriptor; files regular, the user's, bounded, O_NONBLOCK; names from the user database
  checked before they reach runuser; a clean environment; root's own copy of sandbox-ai.
- **Uninstall.** Only below sandbox-ai's own folders (the parent's real path), a link removed as a link, a folder
  that holds a mount left and named, secrets kept unless `--secrets`, Podman objects by label.
- **The web interface's new routes.** Every value checked against a pattern and passed as `--flag=value` or after
  `--`; the primary password in the body only (left out of the audit log), to standard input, with a limiter per
  host; the callback is relayed only when the page's client is this machine's loopback; `mcp copy` and bridges go
  through the commands' own checks.
- **Remote folders.** User and host cannot start with `-`, no comma, quote or control character reaches sshfs's
  options, host keys are always checked, SMB passwords go through a 0600 credentials file, never the command line.
- **Kits from the agents' setup.** Nothing followed out of an agent's folder, files through directory descriptors;
  a host's bundle is data (ids, relative paths without `..`, limits, modes masked to 0777); hooks never imported, a
  skill's own hooks taken out, plugin modules only as hooks that need approval; written through a temporary folder
  with O_EXCL files.

## 4. Not tried here

A real Podman pod's cgroup and a real `podman stats` (the monitor's counters were read from trees the tests write);
a real browser against the catcher (the tests speak HTTP to it); macOS (`ps` instead of /proc for the catcher's
check, LOCAL_PEERCRED); another machine over SSH (`approvals callback` was driven through its function and a fake
runner).

## 5. Tests

Run on the branch's last code commit (131c87fb), with the brief's commands.

- **Core, the whole suite** (`tests`, one process): 17907 passed, 6 failed, 337 skipped, 120 subtests passed, in
  1:15:21. The fix for finding 12 (`approvals.py`, `tests/test_approvals_rework.py`) and finding 6's app part were
  committed while that run was under way; the approvals files were run again afterwards on the final code
  (`test_approvals.py`, `test_approvals_json.py`, `test_approvals_one_prompt.py`, `test_approvals_rework.py`,
  `test_live_approvals.py`: 113 passed).
- **App, every file, in chunks of 20:** 3752 passed, 6 failed, 20 skipped.

All twelve failures fail the same way on `main` at 167ee6e9 (each was run there too); none is touched by this
branch, and none was fixed here - the full suites are Review B's to make green, and two branches fixing the same
tests would only conflict:

| Test | What it says |
|------|--------------|
| core `test_lazy_loaders.py::test_no_lazy_loader_publishes_a_module_before_it_ran` | `hostkits.py:68` (`module()`) |
| core `test_option_help.py` (2 tests) | `stats usage --by` and `--json` have no help text (`monitor.py`) |
| core `test_python39_compat.py::test_nothing_of_python_3_10_or_later_on_the_host` | `hostkits.py` imports `tomllib` without a fallback (3 places) |
| core `test_stability_round3.py::test_every_json_read_that_tolerates_damage_tolerates_deep_nesting_too` | `oauth_relay.py` `main`: `json.loads(argv[3])` without RecursionError (line 587 on main) |
| core `test_video_storyboard.py::test_every_page_of_the_app_has_a_video_of_its_own` | the navigation has the System monitor page, the storyboard does not |
| app `test_error_lines.py::test_no_message_goes_through_the_names_printable` | `monitor_page.py` (2 lines) |
| app `test_hotfix_tabs.py::test_every_tab_row_of_the_app_is_the_shared_one` | a QTabBar in a QGroupBox |
| app `test_review5.py::test_the_start_form_speaks_german_and_marks_what_weakens` | an option without its words |
| app `test_sai_presets.py::test_every_preset_of_sandbox_ai_becomes_one_of_the_gallery` | jadx and cfr are not in the expected list |
| app `test_ui.py::test_pages_and_preview`, `test_video_player.py::test_the_window_has_the_video_button_...` | the expected navigation lacks the System monitor page |

Also seen, not a failure: a device question answered on the app's page is offered Undo, which `approvals ask-again`
refuses (the run does not decide device questions itself) - the page then says why. Left for Review B.
