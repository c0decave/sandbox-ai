# Helpful, not blocking: what is missing, and what sandbox-ai offers to do about it

The rule (user, 2026-11-22): *if something is missing or not working, offer to help and get it run - like a missing
container, an MCP server not added. Do not block with "sorry, not available": build it and attach it.*

One mechanism does it everywhere, the **fix plan** (`sandbox/fixes.py`): a refusal because something is missing
carries its *fixes* - what is missing, what the fix does, its commands, how long it takes, whether it adds something
unsigned or is security-relevant, and whether the security mode takes it.

| Where | What happens |
|---|---|
| A start on a terminal | Everything the start lacks is listed with what would be done for each, then **one question**: `[Y]es, all / [c]hoose / [n]o`. The fixes run with their own output ("fix 2/3: ..."), then the start goes on. With your default `fix_questions each` (`sandbox-ai defaults set fix_questions each`): one question per fix. A fix you decline that the start needs: what you chose is done, and the start is refused with the commands for the rest. |
| A start without a terminal (a script, CI, a background job) | Refused as before - now **always** ending with the complete block of commands that fix it, one per line, to copy. `--fix` does them first, without asking, then starts; `--fix-only ID` exactly the ones named. |
| `AGENT ... --fix-plan` | The plan as JSON (nothing is made, nothing starts): what the app and the web interface ask about. |
| `sandbox-ai doctor` | After its table: the commands that fix what it found, one per line; `sandbox-ai doctor --fix` runs the builds among them (an image not built); a login and the plugin installer talk to you and stay yours to run. In `--json` each such check has a `"fix"`. |
| The app | Its prerequisites check (Board, Start page) reads the launcher's plan on the start's host with what it looks for itself, and lists what is missing in one question - Fix all / Choose… / Cancel - or asks per item (Settings > Polling: *What a start lacks*), with the progress in the tab *Preparing the start* and the outcome in the page's notes; for this machine and for other hosts. A start that names nothing that can be missing is not held up for the plan: its own image, when not built, is asked about by the start itself in its terminal tab. |
| The web interface | The start form asks the host for the plan first (`POST /api/hosts/HOST/fix-plan`) and asks in the page - Fix all and start / Choose… / Cancel, or a tick per fix; the start's terminal then does exactly what was chosen (`--fix-only ID`) before it starts. The form never passes `--fix`. |

Three rules hold for every fix:

- **Unsigned and security-relevant fixes are always named as such** in the question (`UNSIGNED`, `SECURITY: ...`).
- **An offer never widens the security mode.** In `high` and `paranoid` a fix that would add something unsigned or
  open network is not offered and never run by `--fix`; the message names the laxer mode that would take it
  (`--mode medium`). Building one of sandbox-ai's own images and adding an entry of a signed catalog are offered in
  every mode that takes the thing itself.
- **sandbox-ai never runs sudo for you.** A host package that is missing is shown with the command of your
  distribution (apt, dnf, pacman, zypper, apk; brew on a Mac), never installed.

A dry run (`--dry-run`: the app's and the web interface's checks) makes nothing, asks nothing, and its refusal is the
one it always was - `--fix-plan` is the plan for them.

A fix that fails stops the start with the last lines of its output and the command that tries it again. A fix is
never tried twice in one start. `--fix`, `--fix-only` and `--fix-plan` are command-line options only: not allowed in
a project configuration (a project must not make your host build or add things unasked), not kept by a session for
its later runs.

## The inventory

Every refusal or stop a start (or a live action) can hit because something is missing or not set up. *Offered*: done
after a yes, or by `--fix`. *Shown*: its command is in the block, never run for you. *Optional*: the start goes on
without it; it is listed with what the start needs and done only when it is chosen (Choose, each, `--fix-only`),
never by "all" or `--fix`, and never a reason to stop.

| What is missing | The message before | The fix | How it is done | Where | Mode limits |
|---|---|---|---|---|---|
| The agent's image | `The image of claude (localhost/claude-sandbox:latest) is not built on this host (nothing is pulled at a start): sandbox-ai build claude` | offered: build it (~5 min) | `sandbox-ai build AGENT` | CLI, web, app (in its dialog when the start names something else to check; a plain start asks in its terminal tab) | every mode. Without a terminal and without `--fix` a start still builds its own image by itself, as it always did (`SANDBOX_AI_NO_AUTO_BUILD=1`: the refusal only) |
| The team image, Cairn's, the browser's, a VPN's (Tor) | the same, with `sandbox-ai build team ...`, `build cairn`, `build browser`, `build tor` | offered: build it (some minutes) | the build the message names | CLI, app, web | every mode |
| An image you named from a registry (`--image REF`) | `... podman pull REF, or build it` | offered, named UNSIGNED and SECURITY: pull it | `podman pull REF` | CLI, web; the app shows it with its command (it runs only sandbox-ai's own commands of a host's plan) | not in `high`, `paranoid` |
| A local image of your own (`--image localhost/...`) | the same | shown: yours to build | - | CLI | - |
| An image variant sandbox-ai has a recipe for | `--image-variant media: no such variant of claude; make it from its recipe and build it: ...` | offered: make it from its recipe and build it | `sandbox-ai images recipe NAME --from AGENT`, `sandbox-ai build --variant NAME` | CLI, app (its own question since 0.1.95), web | every mode |
| An image variant without a recipe | `--image-variant x: no such variant; create it with: sandbox-ai images new x --from claude` | shown: what goes into it is yours to say | `sandbox-ai images new NAME --from AGENT`, then its build | CLI, app (opens the Images page) | - |
| A variant that is defined, not built | the image message, with `sandbox-ai build --variant NAME` | offered: build it | that build | CLI, app, web | `high`, `paranoid` refuse a variant on a base image of yours (the mode's own rule) |
| An MCP server not added, in a signed catalog | `Unknown MCP server: NAME; defined: ...; add a server with sandbox-ai mcp add ...` | offered: add it (its image is pulled by digest) | `sandbox-ai mcp add NAME [--mount TARGET=HOST]` (the folders the start gives it with `--mcp-mount`) | CLI, app, web | offered wherever the mode takes the server; `paranoid` refuses a server with network (the mode's own rule, naming `--mode high`) |
| ... in a catalog that lists it without a signed image yet (pending) | the same, with `sandbox-ai mcp add NAME --build` | offered, named UNSIGNED and SECURITY: build it here from its reviewed source and add it | `sandbox-ai mcp add NAME --build --yes [--unsigned]` | CLI, app (the repository, commit and source archive's SHA-256 read first and named in the one question: no question of its own afterwards), web | not in `high`, `paranoid` (names `--mode medium`) |
| ... in a catalog that is not signed | the same | offered, named UNSIGNED | `sandbox-ai mcp add NAME --unsigned` | CLI, app, web | not in `high`, `paranoid` |
| ... with a folder the start does not give, or a variable its entry requires | `NAME needs --mount /work=HOST` (before the folder defaults of 0.1.21x) | offered like any other: a folder gets the server's default - `/work` the start's workspace, any other a folder of its own - and the fix says where its folders are; a required variable is declared (`--env NAME`: read from your environment or the secret of that name when the server starts). What `mcp add` would refuse otherwise is shown with why, never tried | `sandbox-ai mcp add NAME [--env NAME]` | CLI, web; the app asks for the folder | as the server |
| ... in several catalogs, in none, or in one whose signature does not verify | the same | shown: which one is yours to choose; none: define it yourself; a bad signature: nothing of that catalog is used | - | CLI; the app asks which catalog | - |
| A toolchain not in the store | `the toolchain go is not in the store: sandbox-ai toolchain add go` | offered: add it from the signed catalog (a download; its hash and signature are checked) | `sandbox-ai toolchain add NAME[@VERSION]` | CLI, app, web | every mode (its registries stay closed in `high`, `paranoid`) |
| An identity | `--identity work: no such identity (yours: ...); add one with: sandbox-ai identities add work` | offered: make it, empty (no logins) | `sandbox-ai identities add --description=... -- NAME` | CLI, app, web | every mode |
| A kit (for this agent) | `--kit NAME: PATH does not exist (kits for claude: ...)` | offered: make it, empty (its skills are yours to add) | `sandbox-ai kits new NAME --agent AGENT` | CLI, app, web | every mode |
| The plugins the start's kits declare | a note: `plugins not in the plugin store yet: ...; install them with: sandbox-ai plugins claude` | optional, on a terminal: install them | `sandbox-ai plugins AGENT [--kit NAME]` | CLI (with the plan); `doctor` names it | - |
| The agent's login | a note: `the host has no claude login; log in with sandbox-ai login claude ...` | optional, on a terminal: log in now inside a sandbox | `sandbox-ai login AGENT` | CLI (with the plan; an expired one is asked about on its own, as before), app (its own question); `doctor` names it | - |
| A Git login whose secret is no access token, or is not there | a note: `git-logins: URL left out: the secret NAME is no access token - ...` | optional, on a terminal: enter it again (a line break or a blank pasted with it is the usual cause - the note says so now) | `sandbox-ai secrets set NAME` | CLI (with the plan) | - |
| A VPN profile | `no VPN profile NAME (profiles: ...); add one with: sandbox-ai vpn add NAME ...` | two ways, one yours to choose: a Tor profile of that name (offered, named SECURITY, asked on its own with No as its default: the name may stand for a VPN of yours), or your own with its keys or login (shown) | `sandbox-ai vpn add NAME --tor` (its image is then the next offer: `sandbox-ai build tor`) / `sandbox-ai vpn add NAME --wireguard FILE` | CLI (asked; `--fix-only vpn:NAME`; never taken by "all" or `--fix`), app (a line of the one question), web (a tick) | as the mode takes `--vpn` |
| An LLM server | `no LLM endpoint NAME (known: ...); add one with: sandbox-ai llm add NAME --kind KIND --url URL` | shown: its kind and address are yours to name | that command | CLI | - |
| An LLM server that is added but does not answer | nothing: the agent's requests failed later | a note at the start, with what makes it answer (the start goes on) | the server is yours to start; then `sandbox-ai llm check NAME` | CLI (a note; optional in the plan) | - |
| A `--service` image of sandbox-ai's own Containerfiles (`showcase-web`) | `--service web: no image web of yours; it is one of sandbox-ai's own Containerfiles, built once with: ...` | offered, named SECURITY (its FROM is a third party's image, UNCHECKED like your images): build it | `sandbox-ai images build-image NAME --builtin NAME` | CLI, app (its own line since 0.1.212), web | every mode that takes `--service` |
| A `--service` image of yours that is not recorded | `--service db: no image of yours named db ...; add it with: sandbox-ai images pull REFERENCE --as db ...` | shown: which image it is is yours to say | `sandbox-ai images pull REFERENCE --as NAME` | CLI | - |
| A `--service` image that Podman's store no longer has | `--service db: the image db (...) is not in Podman's store ...: sandbox-ai images pull ... --replace` | offered, named SECURITY: added again as it was - a pulled one by the digest it was added with (the same image), a Containerfile built again | `sandbox-ai images pull REPO@sha256:... --as NAME --replace` / `images build-image NAME ... --replace` | CLI, web, `--fix-plan` (looked at before anything runs) | as above |
| A secret a start reads (`--secret-env`, `--api-key`, a service's `secret=`, a publish login's, a VPN profile's, an LLM server's key) | `... no secret NAME; keep one with: sandbox-ai secrets set NAME` | offered, on a terminal: its value is asked there, never shown | `sandbox-ai secrets set NAME` | CLI (asked before the session's supervisor starts: it has no terminal); without a terminal the refusal says "on a terminal" instead of `--fix` | - |
| A variable a service takes from your environment (`env=NAME`) | `--service db: env=NAME: your environment has no NAME` | shown: its value is yours (or keep it as a secret and give `secret=` instead) | `export NAME=...` | CLI | - |
| `--publish KIND` without a login (yours, or the identity's with `--identity`) | `--publish npm: no login; add one with: sandbox-ai publish-logins add npm --secret NAME ...` | offered, named SECURITY (the agent can publish as you; the proxy adds the token, each upload is asked): the token kept as `KIND_TOKEN` (asked on the terminal; one kept already is taken) and the login added | `sandbox-ai secrets set NPM_TOKEN`, `sandbox-ai publish-logins add npm --secret NPM_TOKEN` (`sandbox-ai identities publish ID npm --secret NPM_TOKEN`) | CLI, web, app (a fix that asks for a value is left to the start's own terminal, which asks it: said in the notes, the start goes on to it) | - |
| An MCP server whose image Podman no longer has | `MCP server NAME: its image ... is not there (...): podman pull IMAGE, or add it again` | one pinned by its digest: pulled again by that digest - from a signed catalog (your mcp.json's `signed_by`): offered, the very image that was added; added unsigned, or a project file's server of that name (whatever it claims): UNSIGNED, offered, and in `high`/`paranoid` only shown with its command (manual); one built here or named by a tag: shown, added again | `podman pull REPO@sha256:...` / `sandbox-ai mcp remove NAME && sandbox-ai mcp add NAME` | CLI (the app and the web show it with its command: they run only sandbox-ai's own) | - |
| The agent's executable for Bubblewrap | `Agent executable missing: claude` | a choice: the same start with Podman instead (the agent comes with its image), or the agent installed on this host (shown: its npm package where sandbox-ai knows it) | `--backend podman` / `npm install -g @anthropic-ai/claude-code` | CLI | - |
| sshfs for a remote folder (`sshfs://`) | `sshfs:// needs sshfs on this host, which is not installed: ...` | shown: the distribution's package command (`fuse-sshfs` on Fedora) | `sudo apt install -y sshfs` ... | CLI, `doctor` (optional there) | - |
| A device your user cannot open (`--device`, `devices add`) | a warning with the udev rule in words | shown, optional: the rule written, udev told, the device triggered (root's commands, to copy) | `sudo sh -c "echo 'RULE' >> /etc/udev/rules.d/70-sandbox-ai.rules"`, `sudo udevadm control --reload`, `sudo udevadm trigger` | CLI (start and `devices add`) | - |
| `doctor`: the delegated ID ranges, the systemd user session | the check's hint in words | shown: the commands (the usual 100000-165535 where the system has them; `loginctl enable-linger`, apt's dbus-user-session) | `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 USER`, `podman system migrate`; `sudo loginctl enable-linger USER` | `doctor` (the block after its table; `--json`'s "fix") | - |
| What a kept session lacks when it is resumed | its supervisor (no terminal) refused it with the commands | asked by `sandbox-ai resume` on its terminal, done, then resumed | the plan of its start (`AGENT OPTIONS --fix-plan`) | CLI (`resume`; the app's Restart runs it in a terminal tab) | as the session's mode |
| A live action's server or toolchain: `mcp attach NAME START` (also `--live`, `mcp start --for`), `toolchain attach RUN NAME` | `Unknown MCP server: NAME; defined: ...` / `the toolchain go is not in the store` | offered as a start offers it (from its signed catalog, built here unsigned where the mode takes it; the toolchain from the signed catalog), then attached | `sandbox-ai mcp add NAME`, `sandbox-ai toolchain add NAME` | CLI (on a terminal; else refused with the commands) | the run's mode for an MCP server |
| `kits attach START KIT` with a kit that has nothing for the agent | `kit KIT has nothing for Claude Code (kits for it: ...)` | shown: an empty kit gives a running sandbox nothing - what goes into it is yours | `sandbox-ai kits new KIT --agent AGENT`, `sandbox-ai kits add-skill KIT DIR --agent AGENT` | CLI | - |
| A host's sandbox-ai too old for the start (it does not know `--fix-plan` or one of the start's options) | the app: nothing (the start's terminal said "unrecognized arguments"); the web: "update it on the page Hosts" | offered: its signed update, run on this machine | `sandbox-ai hosts update NAME` | app (a line of the one question; declined, the start goes on as before, not asked again until the app ends), web (an Update button in every refusal "too old") | - |
| The agent's own image on a plain start in the app | asked by the start itself in its terminal tab | a line of the one question, built in the tab Preparing the start | `sandbox-ai build AGENT` | app (this machine: `podman image exists` in the background while the Board or Expert view shows the agent, the answer kept - Start never waits for Podman; another host: the list its Images page read) | every mode |
| Podman's init binary (catatonit) for an MCP image server or a `--service` whose image has no init | Podman's `lookup init binary: exec: "catatonit": executable file not found in $PATH`, after the session was made | not needed any more for sandbox-ai's own containers (the browser's, Cairn's: sandbox-ai's own init, `sandbox/pid1.py`; the services of sandbox-ai's own Containerfiles: their program is its own PID 1 - nginx's master reaps every child - `services.OWN_INIT`); for the others the check before the start names it and shows the package command | `sudo pacman -S --needed catatonit` / `sudo apt install -y catatonit` / `sudo dnf install -y catatonit` | CLI, `doctor` ("Podman init") | - |
| An agent the start's options name but its session does not have: `--cairn-workers claude` without claude | `--cairn-workers claude: no agent of this session (the lead opencode); add --with claude` | a choice, by letter: add the agent beside the lead, or the session's own agents as Cairn's workers instead (every harness is one since 0.1.216; one whose light is red is not offered); the start goes on with its options changed | `--with claude` / `--cairn-workers opencode` (no command runs; the team image the start then needs is the next offer) | CLI (asked; else both whole command lines), web (one tick of several), app (asked; the Board puts the chosen way onto its own parts - an agent part, the Cairn part's workers - as one step of Undo, then starts; with a Team part both ways are said) | never taken by "all" or `--fix`; `--fix-only option:with-claude` |
| An agent's feature without that agent (`--hermes-enable` and the seven others) | `--hermes-enable is for Hermes (sandbox-ai hermes, or --with hermes), not claude` | a choice: add the agent beside the lead, or start without the option | `--with hermes` / the start without `--hermes-enable` | as above | as above |
| A host tool: podman, pasta, newuidmap, the Bubblewrap tools, nft and nsenter, tmux | `Required tool missing: podman`; doctor: `install podman (rootless) and pasta (package passt)` | shown: the distribution's package command | `sudo apt install -y podman passt uidmap` ... | CLI, `doctor` | - |

## Round 2 (0.1.218)

Everything round 1 left is in the table above: the services' images (sandbox-ai's own built, yours shown, one gone
from Podman added again by its digest), an MCP image gone, `--publish` without a login, every secret a start reads
(one recognised refusal: `fixes.recognised`, offered on a terminal), the agent's executable for Bubblewrap, the Tor
profile on the command line, an LLM server that does not answer, sshfs, a device's udev rule, the ID ranges and the
user session of `doctor`, `sandbox-ai resume`, the live actions, a host too old (the app's question, the web's
Update button), the Board applying a chosen way to start to its own parts, the app's one page for the MCP servers'
catalogs and commits and the Tor profile, the agent's image of a plain start in the app, the services of
sandbox-ai's own Containerfiles without the host's catatonit, and the agents' skill.

A way only you choose (a Tor profile for a VPN profile you named, Podman instead of Bubblewrap) is asked on its
own with No as its default, never taken by "all" or `--fix`, and named in the refusal with its `--fix-only ID`.

## What is not offered, and why

| What | Why it stays a refusal (with its commands) |
|---|---|
| A device that is not plugged in (`--device`, `devices add`) | nothing sandbox-ai can make: plug it in |
| A host package (podman, pasta, sshfs, catatonit, the Bubblewrap tools), a udev rule, the ID ranges, linger | they need root: sandbox-ai never runs sudo for you - the commands are shown |
| Your own image of a `--service`, your VPN profile's keys, an LLM server's kind and address, a variable's value | only you know which image, which keys, which server, which value |
| A kit with nothing for the agent, for `kits attach` | an empty kit gives a running sandbox nothing |
| An MCP server's folder the start explicitly cleared | the app asks for it (the default applies otherwise) |
| A server in several catalogs | which one is yours to choose (the app asks first, the command line names each `mcp add PREFIX:NAME`) |
| What a security mode refuses | an offer never widens the mode: the laxer mode is named |
| A host this machine cannot reach over SSH | nothing to update or ask there until it answers |
