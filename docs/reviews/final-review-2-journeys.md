# Final review 2: correctness and user journeys (2026-11-17)

Scope: everything merged into `main` from 511a33b8 to 056fd47c (the features of 2026-11-16: 51 merges, 269
commits), used the way the user uses it, not read from the feature agents' reports. Reviewer R2 of three (R1
security, R3 robustness ran in parallel); branch `review/final-2`, not merged.

How:

- **Command line** through the real `./sandbox-ai` with `tests/live_fakes.py`'s user (fake Podman, tmux, npm, SSH,
  sshfs): starts (`--dry-run` and real ones that stop at `pod create`), resume, `--with`, allow lists, remote folders,
  `gc --usage`/`--pack`/resume of a packed HOME, secrets with and without a primary password, `web user --no-totp`,
  `uninstall`, `team start --dry-run`, `mcp list|set|copy`, help of every parser.
- **A second host and an old host:** a fleet of this machine plus `lab1` (this version) and `old1` running
  **sandbox-ai 0.1.97** (a worktree of the tag), reached through a fake `ssh` that runs the other user's real
  `./sandbox-ai`: `hosts list`, `hosts run`, `mcp copy` to both, `secrets status` there.
- **Upgrades from 0.1.97:** state made *by 0.1.97 itself* (its `wizard trust`, `hosts add`, a kept start, a plain
  secrets store) and an `mcp.json` in 0.1.97's format, then used with this version: start, `list`, `resume`, `gc`,
  `gc --usage`, `mcp list`, `doctor`, `secrets`.
- **App** offscreen (PySide6, the real widgets): the Resume with… dialog in English and German, the MCP Properties…
  path, the Credentials tab's primary-password flow read against the command it runs; German strings added in the
  range scanned for cryptic placeholders and English left over.
- **Web**: the API handlers of the new features read against the commands they run (secrets protection, unlock,
  first password, MCP network kinds, mcp copy); the bridge commands could not be driven here (this machine is itself a
  sandbox: `bridge add` refuses inside one, correctly).

## 1. Findings

Severity: **H** high (wrong result, data lost), **M** medium (a journey that fails or misleads), **L** low.

| # | Sev | Finding | Status |
|---|-----|---------|--------|
| 1 | M | **Upgrade: an MCP server added for your own network silently loses it.** 0.1.97's README: `mcp add gowitness --network full --env ALLOW_PRIVATE_TARGETS=1` (WhatWeb, gowitness "on your own network"). 0.1.98 makes `full` the internet without your network; such a definition keeps working but reaches none of its targets, and `mcp list`, `doctor`, `mcp add|set` and the app said nothing (their note covered only allowlist entries). | Fixed: `egress_note` names it with the fix (`mcp set NAME --allow-private CIDR`, or `--network lan`); shown by list, add/set, doctor and the app's Properties… |
| 2 | M | **Resume with… (app) could not take a folder of another machine, and asked for `SOURCE:DESTINATION[:ro|rw]` (`QUELLE:ZIEL[:ro|rw]`)** - the cryptic kind of field the user asked to be rid of (as "ZIEL=HOST_ORDNER"). The line was split at every `:`, so `sshfs://bob@lab1:2222/srv` - which `sandbox-ai resume --mount`/`--workspace` take - was refused, by the dialog and again by `backend.resume_args`. | Fixed: a row per folder (folder chosen from the start's host or typed `sshfs://...`, where the sandbox sees it with `/mnt/NAME` offered, "The agent may change it"); `resume_args` passes remote folders on |
| 3 | L | `mcp add|set --allow-private` / `--allow-tcp` took one item per value, while every other allow-type option (the merge of 57ab2da7) takes a comma/space list - the egress merge came later. `--allow-tcp "192.168.1.2:80, 192.168.1.3:80"` was refused as one address. | Fixed (list action, help says so, READMEs) |
| 4 | L | `mcp copy`/`mcp export` said "copied: network the start's rules" and "network full" (the definition's word; `full` changed meaning in this release). | Fixed: "network: internet (no LAN)", "its command's own options (the sandbox's rules)" |
| 5 | L | A refused `mcp copy` printed "MCP server notes on lab1: its definition imported" and then "not copied". | Fixed: "its definition would be imported" while there are problems |
| 6 | L | `team start --help`: the agents were one comma-joined word, wrapped in the middle of a name ("ope / nhands"). | Fixed |
| 7 | L | `defaults show`: keys longer than 10 characters (`desktop_notifications`, `connection_bodies`) pushed their value out of the column. | Fixed |
| 8 | L | README: "`lan` and these [--allow-private/--allow-tcp] are asked first in the app and the web interface" - neither can set them (they show them). | Fixed (README, README_DE) |
| 9 | M | **`sandbox-ai uninstall --purge` removes nothing for a pip/pipx (the new PyPI package), AppImage, `.pyz` or checkout copy** - it says `pip uninstall sandbox-ai-app`, which leaves the kept HOMEs, logs and sandbox-ai's Podman images (tens of GB on the user's host). There is no command that removes the data for these users. Documented as the design (tests assert it), so not changed here; proposal: with `--purge` on such a copy remove the data (install.sh's names are skipped by `purge_targets` already) and then say how to remove the program. | Open - the user's decision |
| 10 | L | `sandbox-ai mcp copy NAME --to HOST` needs `--from local` spelled out (`--from` is required). | Open (suggest default `local`) |
| 11 | L | `sandbox-ai claude --with hermes,qwen` is refused (one agent per `--with`), while `build team --with hermes,qwen` and `--with all` take lists - and the start's own hint says "sandbox-ai build team --with hermes,qwen". The error names the choices, so it is found. | Open (suggest the same list action) |
| 12 | L | `list --all` shows a packed HOME as "kept"; only `gc --usage` says "packed" (resume unpacks it, verified). | Open |
| 13 | L | The app's "From another host…" for a host older than 0.1.95 still asks for `ZIEL=ORDNER` lines (fallback; newer hosts get fields). | Open (fallback only) |
| 14 | L | `hosts list` has no header row (unlike `list`, `session list`, `net list` since this range). Pre-existing. | Open |
| 15 | L | The team lead's first prompt says "codex work in sandboxes of their own" for one member (plural verb). Pre-existing. | Open |

## 2. Journeys verified OK

- **Secrets, primary password:** a new store refuses a plain secret without a terminal and names both ways out;
  `password set --password-stdin` (8+ characters, said), `status` / `status --json`, `lock`, a locked `set` and a
  locked start refused with `secrets unlock`; a **0.1.97 plain store**: `set` of a new secret asks for a password
  first (app: the Set password… step runs before the secret), `password set` encrypts the old files, `password
  remove` decrypts them; a start with a plain secret warns. `set --plain` with a password set encrypts anyway (as
  the help's "your choice to keep plain files" applies to a store without one). App and web parse `status --json`
  (`plain` is a list there: counted).
- **Allow lists:** `--allow-host "a, b c"`, `--allow "1.2.3.4 10.0.0.0/8"`, `--allow-tcp a:22,b:443` at a start;
  a host name in `--allow` is refused with "goes to --allow-host (-A)".
- **Remote folders:** `-w sshfs://bob@lab1/srv/code` mounts with the right sshfs options (BatchMode,
  StrictHostKeyChecking=yes, reconnect), warns with the risk, unmounts after; the kept start lists the remote source,
  resume mounts it again, `resume --mount sshfs://bob@lab1:2222/srv/data:/mnt/data:ro` (a port in the URL) works.
- **0.1.97 state:** a trusted project file (options with `--allow-host`, `--allow-tcp`, `--nameserver`, `--mount`)
  starts the same; a kept start of 0.1.97 lists, resumes, expires on 2027-02-15 (90 days after its last use),
  `gc --usage` measures it; `hosts.json` of 0.1.97's `hosts add` reads; `mcp.json` of 0.1.97 lists with
  "added before 0.1.78" catalogs.
- **gc:** `--usage` summary and tables, `--pack --home NAME --dry-run`/`--yes`, the packed start resumes (unpacked
  first, said).
- **Hosts:** `hosts list` with a 0.1.98 and a 0.1.97 host; `mcp copy` to the 0.1.97 host says it needs 0.1.98 and
  `hosts update old1 --from-here`; to the 0.1.98 host a command server is copied (dry run first, then for real, then
  listed there); `hosts run old1 -- secrets status` shows 0.1.97's own refusal.
- **`--with` companions:** `--with hermes --with qwen` picks `team-hermes-qwen-sandbox` and says how to build it.
- **Teams:** `team start --dry-run [--json]` with `--role`, `--member-model`; a member option for an agent not in
  the team and a role given twice are refused with the reason.
- **Web logins without TOTP:** `web user add --no-totp --stdin` warns and lists "(no TOTP)"; `reset` gives TOTP.
- **uninstall** from a checkout says what to do and removes nothing (see finding 9 for pip).
- **Help:** every option of every parser scanned for words glued together (only finding 6); German strings added in
  the range (394) scanned for English left over and cryptic `A=B` placeholders (findings 2 and 13).

## 3. Commits (branch `review/final-2`)

- 56b4d19a ALLOW_PRIVATE_TARGETS note; `--allow-private`/`--allow-tcp` lists; mcp copy network in words; team help
- 34b468f1 App: Resume with… folder rows (remote folders too)
- a1467e63 `defaults show` alignment
- 755e7108 a refused mcp copy never says "imported"
- (this document and the release notes)

## 4. Tests

- `tests/test_final_review_2.py` (core, 8): the note as a function and through `./sandbox-ai mcp list` on a 0.1.97
  `mcp.json`; `mcp set` with lists (good, bad item named, empty); `mcp add|set --help`; `team start --help`; mcp
  copy's network words and a refused plan's head; `defaults show`.
- `projects/sandbox-ai-app/tests/test_final_review_2_app.py` (app, 3): the Resume with… rows (chosen from the host,
  `/mnt/NAME` offered, read-only by default, a remote folder, remove, a half-filled row named), `resume_args` with
  remote folders, the German words.
- Run green with them: core `test_mcp.py`, `test_mcp_egress.py`, `test_mcp_copy.py`, `test_web_mcp_copy.py`,
  `test_completion.py`, `test_docs_every_option.py`, `test_option_help.py`, `test_main_help.py`, `test_agent_teams.py`,
  `test_defaults_cmd.py`, `test_docs_claims.py`, `test_release_notes.py`, and the others listed in the final report;
  app `test_start_hosts.py`, `test_remote_paths.py`, `test_dialog_sizes.py`, `test_remote_parity.py`, `test_i18n.py`,
  `test_references.py`, `test_mcp_copy_app.py`, `test_mcp_limits_ui.py`, `test_mcp_variables_ui.py`,
  `test_allow_lists.py`.

## 5. What needs a live test

- Finding 1 on a real host: an upgraded gowitness/WhatWeb with `ALLOW_PRIVATE_TARGETS=1` - the note, then
  `mcp set NAME --allow-private 192.168.x.0/24` and a scan of the LAN through the real nft rules.
- Finding 2: Resume with… a kept start on a real host with an `sshfs://` folder row (real sshfs, the key there).
- Bridges across hosts (`bridge add lab1:a lab2:b`): cannot be made inside a sandbox, so not driven here.
- The primary password's key agent across a real logout/login and `--idle`.
