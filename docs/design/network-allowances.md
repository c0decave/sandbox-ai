# Network allowances: what a sandbox may reach, and why

Status: analysis of 0.1.242 (todos 2026-11-24, "Netzwerkfreigaben ... organisch gewachsen"), the clear wins built
first (section 4), and the bigger changes, decided by the user on 2026-11-24 and built (section 5). Sections 1-3
describe the state before section 5. Nothing here weakens a rule or a default.

## 1. Concepts today

Everything below opens something for the agent's sandbox. What only narrows it is listed apart (1.4).

### 1.1 What a start opens (start allowances)

| Concept | How it is given | Port / scope |
|---|---|---|
| The agent's provider | always, per agent (`PROVIDERS`), off with `--no-provider-defaults` or `--local-config`; `--with` adds each companion's | names, TCP/443 |
| `--frontier` | option; network profiles `development`, `frontier` | OpenAI + Anthropic names, TCP/443 |
| Network profile `--profile NAME` | option | `development` = provider + `--allow-pypi --allow-github --frontier`; `dns`; `egress-all`; `offline` |
| Host set `--allow-pypi`, `-github`, `-gitlab`, `-kernel-org`, `-duckduckgo`, `-npm` (code and help call them "presets") | option | fixed names, TCP/443 |
| `--allow-host NAME`, `--allow-host '*.DOMAIN'` | option | name pinned, TCP/443; a wildcard opens each name below as it is looked up |
| `--allow-domain DOMAIN` | option | = `--allow-host DOMAIN --allow-host '*.DOMAIN'` |
| `--allow-shared-wildcard '*.DOMAIN'` | option (the risk confirmed) | a wildcard below a shared service |
| `--allow-tcp IP:PORT` | option | one TCP port of one address |
| `--allow IP_OR_CIDR` | option | every port and protocol of an address or network |
| `--allow-host-port PORT` | option (Podman) | the host's 127.0.0.1:PORT through a relay |
| `--dns`, `--nameserver IP` | option, profile `dns` | real lookups (each logged); addresses stay closed |
| `--egress-all-allow` | option, profile `egress-all` | every outbound destination (+ DNS) |
| `--offline` | option, profile `offline` | nothing (wins over every other) |
| `--vpn-bypass-host-ports`, `--vpn-bypass-tcp` | option (Tor) | named ports past Tor |
| Project file `.sandbox-ai.json` "options" | file, trusted by hash (`wizard trust`) | every start of the project |
| Approvals "always" (`approved.json`) | an answer "Always, also for later sessions" | names and TCP targets, every start of the project |
| Implied by other options | `--toolchain` (its registries, unless `--toolchain-offline`), `--git-token-*`/`--git-credentials` (its server; github.com: `--allow-github`), `--api-key HOST=...`, publish logins, `--llm` (its server), `--AGENT-enable` features | the names/ports each needs |
| Your defaults `defaults.json` | file | no allowances: only `subnet`/`subnet6`, `mode`, `ask_timeout` ... |
| Preset (`--preset NAME`, ready setups) | option | its options join the start's (any of the above) |

### 1.2 What is added while it runs (run allowances)

| Concept | Where | Holds |
|---|---|---|
| `sandbox-ai net allow RUN ...` (the same options as the start, plus Git logins and `--api-key`) | CLI; app: Sandboxes page › Allow…; web: Network card › Allow… | until it ends; with `--keep` also written into the project file |
| `net revoke` | CLI; app: Sandboxes › Revoke…, Approvals › Allowed now › Revoke…; web: Network card › Revoke | takes back what the run added (never the start's) |
| An answer to a question: Allow once / Always allow (the run) / Always, also for later sessions (the project) | app: Approvals › Questions, the Questions window, notifications; web: Approvals; CLI `approvals allow --scope` | once, the run, or `approved.json` |
| Blocked list: Allow for this run / Always, also for later sessions… / Ask again / Deny for this run | app: Approvals (Refused tab); web: Network card › Refused during this run, Approvals | the run, or the project (file or ruleset exception) |
| `net show --keep` / `--keep-all` | CLI; app: What it may reach… › Keep for later starts…; web: Network card › Keep for later starts… | moves the run's allowances into the project file |
| `net allow --public-only --keep` | CLI | into `approved.json`, not the project file |
| `git-logins give`, `secrets give --api-key` | CLI; app: Credentials › Give to a running sandbox… | the run (`--keep`: later starts) |
| `toolchain attach`, `mcp attach --live`, `workspace add` | CLI; app: Sandboxes › Add… | open their own registries/servers for the run |

### 1.3 Where it is shown (before this branch)

| Place | Shows | Missing |
|---|---|---|
| `sandbox-ai net list` | profile, VPN, approvals waiting, names added, cut | the start's allowances |
| `sandbox-ai net show RUN` | what was added during the run, kill switch, taint | **the start's allowances and every source** |
| `sandbox-ai --dry-run` plan | `https_hosts`, `allow`, `allow_tcp`, `host_ports`, `nameservers`, profiles | sources |
| `sandbox-ai approvals remembered` | `approved.json` per project | - |
| App › Board › Network part (and VPN, Toolchains, LLM parts) | the start's own options, before it starts | what other parts imply (toolchain registries, Git server, LLM), sources |
| App › Start (Expert) › groups Network, Proxy & privacy | the options as fields | as above |
| App › Sandboxes › Allowed during the run | `net show` text (added during the run only) | start allowances |
| App › Approvals › Allowed now | added during the run, Revoke | start allowances; its explanation sent the user to the Board |
| App › Approvals › Remembered | `approved.json`, Forget… | the project file's allowances |
| Web › Starts › Network… card | added during the run (Revoke each), refused (Allow…), Keep | start allowances |

### 1.4 What only narrows (never opens)

Administrator policy (`forbid`, `allow_hosts`/`allow_within` ceilings, `max_approval_scope`, `mcp_network`), the
security mode (high/paranoid refuse options that open hosts, cap the approval scope), the kill switch (`net cut`, File ›
Emergency stop), the taint (untrusted content), Tor/VPN (everything through the tunnel), rulesets and `--allow-http`
(per request), the package guard, the secret guard, `--ask off`.

### 1.5 MCP servers

Each server's sandbox has its own network, in other words: `--network none|full|allowlist|lan`, `--allow-private
CIDR`, `--allow-tcp IP:PORT` (`mcp add`, `mcp set`; app: MCP servers › Add/Settings; Board › MCP part). It is not
part of the agent's allowances and is not listed with them.

## 2. Problems found

1. **No place answers "what can this sandbox reach, and why".** `net show`, the app's "Allowed during the run" and
   "Allowed now" and the web's Network card list only what the run added; the start's allowances (the larger part,
   and the ones a user cannot remember: provider, profile, project file, approvals kept always, toolchain registries,
   a Git server) were visible only as options before the start (Board, Expert page), never with a source, never for
   a running sandbox. The Approvals page explained "What its start options allow is on the Board (Network)" - the
   Board shows the options, not what they mean, and not those of a start made elsewhere (CLI, web, a team).
2. **One word, two scopes.** The questions' "Always allow" is for the run (user's wording, 2026-11-19); the Blocked
   list's "Allow always…" was for the project - in German both read "Immer erlauben". The run scope itself has two
   labels: "Always allow" (questions) and "Allow for this run" (Blocked list).
3. **Two stores keep "for later starts"**: the project file (`net allow --keep`, `net show --keep`, the Blocked
   list's "always" for a destination) and `approved.json` (an answer "Always, also for later sessions", `net allow
   --public-only --keep`). They are shown in different places (the file nowhere as allowances; `approved.json` under
   Remembered), forgotten in different ways (edit and re-trust the file; Forget…), and a user cannot tell which one
   holds a destination.
4. **"Preset" means two things**: `--allow-PRESET` (a fixed set of host names; `net allow --help` and the app's Allow…
   dialog call them presets) and `--preset NAME` (a ready setup of a whole start, the Presets window).
5. **"Profile" means four things**: network profiles (`--profile`), model profiles (`llm profiles`), VPN profiles
   (`--vpn PROFILE`), browser profiles (`--browser-profile`).
6. **Several entry points for the same allowance during a run**: a Git login or API key can be given by Allow… (Git
   login field; the web card also has an API key field, the app's dialog has none), Credentials › Give to a running
   sandbox…, `git-logins give`, `secrets give`, `net allow --git-token-*`/`--api-key`.
7. **Two views of the same list in the app**: Sandboxes › "Allowed during the run" (text, with Untaint… and Keep…)
   and Approvals › Allowed now (table, with Revoke…).
8. **The MCP servers' network** uses other words (`none|full|allowlist|lan`) than the agent's (`offline`,
   `egress-all`, `--allow-host`, `--allow` for a network).
9. **`--allow` alone** opens every port and protocol of an address or network: the broadest per-address option has
   the least telling name, next to `--allow-host` (443 only) and `--allow-tcp` (one port).
10. **`--dns`** reads as "the network is open" but only makes lookups real (addresses stay closed): the app and help
    say so, the option name does not.

## 3. The proposed clear model

Few concepts, one place:

- **Start allowances** - what the start opens, each with a **source**: the agent's provider, a network profile, a
  host set, the start's own options, the project file, approvals kept always, or another option that needs it (a
  toolchain's registries, a Git login's server, an API key's host, `--llm`, an agent feature).
- **Run allowances** - what was added while it runs (Allow…, an answer once or for the run); Revoke takes it back;
  Keep for later starts moves it into the start allowances of the project.
- **How long each holds** - this start, every start of the project (project file, approvals always), until the run
  ends (run allowances).
- **Limits that only narrow** - policy, security mode, kill switch, taint, VPN/Tor, request rules, guards: named as
  notes, never as allowances.
- **One place** - "What it may reach": every allowance of a running sandbox with source and how long it holds, the
  run's with Revoke - the same list in the CLI (`sandbox-ai net show`), the app (Approvals › Allowed now; Sandboxes ›
  What it may reach…) and the web (Network card).

## 4. Built first (no product decision needed)

- `agent_sandbox.reach(a)`: every allowance of a start with its source (`provider`, `frontier`, `profile`,
  `project`, `option`, `approved`, `toolchain`, `git-login`, `api-key`, `publish`, `llm`, `feature`), a detail (the
  agent, the profile, the project file's path, the toolchains) and, where it outlasts the start, its scope. What other
  options open is recorded where they open it (`parse_args`: `note_opened`), so nothing is guessed afterwards. It only
  explains: a failure lists nothing and never stops a start (`recorded_reach`).
- `run.json` "reach" and the dry run's plan "reach" carry it.
- `sandbox-ai net show RUN`: "what it may reach from its start" (KIND, SOURCE, HOLDS FOR, WHAT) above "allowed during
  this run"; `--json` adds `started`. A run of an older sandbox-ai says its start is not recorded.
- App › Approvals › **Allowed now**: the start's allowances below the run's, with the columns Why and Holds for; they
  are never revoked from there. The explanation says what the list is instead of pointing at the Board.
- App › Sandboxes: "Allowed during the run" is **What it may reach…** (the whole `net show`), also in a terminal tab's
  "While it runs" menu and the help.
- Web › Network card: **From its start** (Kind, What, Why, Holds for) above "Allowed during this run".
- The project scope's button reads **Always, also for later sessions…** in the Blocked list as on the questions
  (problem 2's collision; English and German).

## 5. Decided (user, 2026-11-24)

| # | Proposal | Decision | What was built (branch feature/net-proposals) |
|---|---|---|---|
| A | Call `--allow-PRESET` **host sets** | approved | "Host set" / "Host-Set" in `--help` ("Host set: allow ..."), `net allow --help` and `net`'s usage (`--allow-SET`), the app (Allow… dialog: a "Host sets (TCP/443)" heading; the setup wizard's row; the Help), the web (Network card: "Host sets"), the READMEs, the skill source (scripts/skill_source) and the tours; the options are unchanged. "Preset" now means only a start template (`--preset NAME`). The video narration source (docs/video/storyboard.py) is updated; the videos `network`, `app-web` and the app-pages series' `approvals` need a re-render |
| B | One run-scope label | **rejected**: "Always allow" stays for the run | - |
| C | One store for "later starts" | **rejected**: `approved.json` stays | - |
| D | Git logins and API keys during a run only from Credentials | approved | The app's Allow… dialog has no Git login field; its *Git login or API key…* closes it (what was typed is kept as a draft) and opens Credentials for the host. Credentials › Git logins › Give to a running sandbox… with no login chosen gives one whose token is in a variable, a secret or a file (`git-logins give --env/--secret/--file`): nothing the dialog could do is lost. The web's Network card has no Git login or API key fields; its *Git login or API key…* opens the page Logins at *Give to a running sandbox* (a Git login, an API key, for a running start, that start chosen; the same `net allow` request as before, with the into-the-sandbox question). `sandbox-ai net allow --git-token-*`/`--api-key` stay on the command line |
| E | One app view of what a sandbox may reach | approved | Sessions › What it may reach… (and a terminal tab's While it runs) opens Approvals › Inspection › Allowed now for that run (this machine's or a host's start); it has Revoke…, Untaint… (only for a tainted run), Keep for later starts…, Keep everything for later starts… and Copy as text (the plain text of `sandbox-ai net show RUN`, read on its host). The separate text window is gone |
| F | `--allow-net IP_OR_CIDR` as the documented name of `--allow` | approved | `--allow-net` is the first long name in `--help`, `net allow\|revoke --help` and the READMEs; `--allow` stays an alias everywhere: argparse takes both, `canonical_options` writes `--allow` (what project files, presets, kept starts, MCP definitions, board bundles, the app's fields and older hosts read), completion offers both, the security modes name `--allow-net` |
| G | MCP servers' network in the agents' words | approved | `mcp add\|set --network offline\|egress-all\|allowlist\|lan`; `none` and `full` are taken as aliases (command line, catalog entries, `mcp.json`, the app, the web) and shown in the new words (`mcp list`/`show`, `mcp catalog`, doctor, the app's combo boxes and catalog table, the web's MCP page). Stored as before (`none`, `full`) so an older sandbox-ai, its app and a host read them |
| H | Board › Network part shows what other parts open | to the board agent | - |

Besides (same day, the user): the app's Approvals page has two tabs, **Questions** (the questions of every host, as
before; its title counts the open ones) and **Inspection** (which sandbox, Allowed now, Remembered, each with the
whole height). Its questions are no longer a panel above the page. It opens on Questions; the tab chosen stays while
the app runs; a new question never switches it; every way to a lower section (Sessions › What it may reach…, a
refusal's "Inspection › Remembered › Forget") opens Inspection there; the answer keys act only on Questions.

## 6. Not changed

No rule, default, check or ceiling: `reach()` reads what the start already decided; the app and the web only show
more. The kill switch, the taint, the policy and the mode are named as narrowing, not listed as allowances.
