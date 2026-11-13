# sandbox-ai-app

A graphical front end for [sandbox-ai](https://github.com/c0decave/sandbox-ai), written in Python
with Qt (PySide6). It sets every option of sandbox-ai, starts agents and sessions (in a terminal tab
of the app or a terminal window), answers what running sandboxes ask, shows what they reached and
what they cost in tokens, lets you copy files in and out of their workspaces, and manages images,
logins, secrets and MCP servers - without the kernel or virtual-machine parts of tools such as
Capsem: the isolation stays sandbox-ai's own (rootless Podman or Bubblewrap, Firejail, the egress
firewall and the connection proxy).

[Deutsch](#deutsch) below.

## Projects

The current project is shown large at the top of the window, with the state of its `.sandbox-ai.json` (none,
trusted, changed since trusted, not trusted) and how many of its sandboxes run and ask. The switcher next to it
lists the projects sandbox-ai and the app know, without reading inside them: the app's own recent list, the trusted
project configurations, the sessions, the running sandboxes, projects with answers remembered "always", and
folders of earlier runs (sandbox-ai's `exposed.json`) that have a log folder of their own; *Open folder…* and
*New project (wizard)…* add one. The project drives the pages that are per project: *Start* (its folder is the
first workspace; *Review and trust…* when its file is not trusted), *Board* (its `.sandbox-ai.json` and
`.sandbox-ai.board.json`), *Connections* and *Statistics* (its log folder), *Files* (its folder), *Approvals* and
*Sandboxes & sessions* (its rows in bold and counted), *Kits & plugins* (the kits it uses). Each project keeps its own
board - changes not applied yet, the undo history, the zoom - while another project is shown; reading a project's
file again, a preset or the wizard on the same board ask before they drop such changes. The last project opens
again at the next start (`project` and `recent_projects` in the app's `settings.json`).

## Tabs, window controls and panels

**Tabs.** Open projects, sessions and sandboxes are tabs across the top of the page area, like a browser's; the
current tab is the current project (a session's or sandbox's tab: its workspace). A session or sandbox tab shows its
state as a dot (running, paused, stopped), its agent and backend; its context menu attaches in the app, pauses,
resumes, stops (after asking) or shows it on *Sandboxes & sessions*. Tabs close with their button, a middle click or
Ctrl+W - the last tab of a project whose board has changes not applied asks first -, move by dragging, and cycle
with Ctrl+Tab and Ctrl+Shift+Tab. *+* lists the known projects, the sessions and sandboxes of the account that are
not open yet, *Open folder…* and *New project (wizard)…*. A session or sandbox that starts while the app runs opens
a tab of its own without taking the focus (setting *Open a tab for each session or sandbox that starts*, on by
default): one tab per start, for its agent - what belongs to the start (its MCP servers, helpers, the other members
of a team it leads) opens none, *Open terminal* on the Terminals page reaches it; one that ended keeps its tab,
stopped, until it is closed. The tabs, their order and the current one
are kept for the next start (`tabs`, `tab_current`).

**Window controls.** For desktops whose window manager draws no controls: a menu bar - *File* (open a project
folder, new project, switch project, close tab, quit with Ctrl+Q), *View* (minimize, maximize or restore, full
screen with F11, hide to the system tray - greyed out with the reason where there is none -, the theme, the board's
zoom, the panels), *Go* (the pages with Ctrl+1 … Ctrl+9, *Back* and *Forward*, next and previous tab), *Settings* (*Settings…*, Ctrl+,:
every setting of the app in one dialog, the same as *All settings…* on the System page), *Help* (the versions of the app,
sandbox-ai, PySide6, Qt and Python; this README) - and painted, theme-aware buttons in the header bar: minimize,
maximize/restore (its icon and tooltip follow the window), full screen, to the tray, quit. Quitting asks the usual
questions (running sandboxes, terminals, work in progress) and shuts down as closing does. On the Terminals page
Ctrl+W, Ctrl+Tab, Alt+Left and Alt+Right go to the terminal.

**Back, Forward and the mouse's side buttons.** The window keeps the pages shown in order (the Help page with the
section it showed, at most 50 steps): the mouse's back button, Alt+Left (Cmd+[ on macOS) or *Go > Back* goes to the
one before, the forward button, Alt+Right (Cmd+]) or *Go > Forward* on again; a page chosen meanwhile drops what was
ahead, as in a browser. The next side button of a mouse that has one (Qt's ExtraButton3) opens the Settings. The
buttons are taken where the input reaches the window (a filter on its own QWindow and on a floated panel's, never on
the whole application), before a list, a table, the board or a terminal's web view, which would keep them.

**Lists and tables.** Ctrl+click adds a row, Shift+click takes a range, Ctrl+A all, wherever acting on several makes
sense: the Sandboxes tree (Stop, Pause, Resume, Delete), secrets, Git and publish logins, identities and VPN profiles
(Remove…), hosts (Remove…), kits (Delete kit…, your own), LLM servers (Remove…), image variants (Delete variant…),
teams (Stop…, Delete…), MCP servers (Stop), the remembered approvals (Forget…) and the files (as before). One question
names them all, then each is done in turn in the background and what failed is said together; the Delete key does
what Remove… does there; buttons for one row are greyed out, saying why, while several are selected. The logs
(Connections, Activity, the history of the answers, the Statistics tables) take several rows too, to copy them. Every list,
table and tree copies: Ctrl+C puts the selected rows on the clipboard as text (tabs between the columns, a line per
row, in the order shown; a single cell as it is), and its context menu has *Copy* and *Copy cell*. A line between
the column titles shows where a column is grabbed to make it wider or narrower.

**Navigation and tours.** Each section of the navigation folds with a click on its title; *View > Navigation*
and the navigation's context menu fold or unfold all of them, and *Only icons* (Ctrl+Shift+B, the « button below
it) shrinks it to a strip of icons whose tooltips are the page titles. A page reached another way (Ctrl+1 …, a
tour, a link) unfolds its section. Both are kept for the next start (`nav_collapsed`, `nav_compact`). Besides the
guided tour through all pages, every page has a tour of its own: a few steps, each outlining the control it is
about (and bringing its tab to the front). Start it with F1 on the page, *Help > Area tours*, the navigation's
context menu, or *Learn this area* in the guided tour.

**Help, (?) and tours through a task.** Every page, area and board part has a small round *?* (the one at the right
end of the tab row explains the page shown): it says what the thing is, what it is for, what happens when you use
it and the command-line option behind it, and *More in Help* opens the *Help* page (navigation, *System* section;
*Help > Help*, Shift+F1) at the matching section. The Help page explains every page and feature of the app and the
ideas behind sandbox-ai (sandbox, sessions, network, proxy, approvals, taint, canaries, presets, kits, rulesets,
MCP, devices, display, VPN and Tor, LLM servers, web interface, hosts, teams, memory watch, privacy check,
updates), searchable in both languages, in the app's language or switched to the other. *Help > Tours through a
task* (and the Help page) walk through whole tasks across pages - the first start, your own construct on the board
saved as your own preset, approvals and the network of a running sandbox, the preset gallery, the help itself -
outlining each control and ticking a step off when the window sees it done. The texts live in `help_texts.py`
(the (?)s), `help_content.py` (the Help page) and `tours.py`.

**Drag and drop on the board.** Drag a part from the parts panel onto the board: it lands where you drop it (a
dashed card in the theme's accent colour shows where; a part a board has once is greyed out while it is there).
Double-click or Enter still add a part. Connect by dragging from a part's right port onto another part - the parts
that can take the connection are framed, Esc cancels. Folders and files dropped from a file manager become a
workspace, a read-only or writable mount, or the folder of the workspace or mount they were dropped on (a menu
asks); a `.sandbox-ai.json` or a folder with one opens its project, and a preset file of the app is imported (and
the board built from it, if you choose). Every change is one step of Undo.

**Panels.** The Board's parts and its inspector with the findings, and the open questions of *Approvals*, are
panels: they can be floated (a floating panel stays on every page - e.g. the open questions while you work on the
board), docked at another edge or stacked, closed and brought back from *View > Panels*; *Reset the panels*
restores the start. Where they are is kept (`QMainWindow.saveState` in the app's settings: `dock_state`,
`docks_closed`).

## What the app keeps

Everything set up in the window comes back at the next start, from `~/.config/sandbox-ai/app/state.json` (mode
0600, written atomically a moment after a change and when the app quits; `sandbox_ai_app/state.py`): the window's
size, place and state (maximized, full screen), the page shown and the folded sections of the navigation, the
panels, the splitters, every table's sort order and the column widths you dragged, the filters and check boxes
(Connections, Images, Sandboxes & sessions), the last log folders, the row selected on *Sandboxes & sessions*,
the folder each file dialog was left in, the open tabs with their order and the current one, and per project the
board's zoom and scroll position and its changes not applied yet: after a crash or a restart they come back on
the board with a notice, as one step that *Undo* takes back to the project's file, and *Discard* drops them. The
Start page keeps the agent and profile chosen and per agent its last options and agent arguments (the workspace
follows the project). Secrets never go into the file: password fields are not kept, and agent arguments lose
what may be a secret (the value after an option named like key, token, secret, password, auth, …, and anything
that looks like a token). A damaged file - `state.json` or `settings.json` - is kept aside as
`NAME.damaged-TIME`, the app starts with defaults and says so at the top of the window; a value of the wrong type
falls back to its default on its own.

## Presets

*New from preset…* on the Board (and the wizard's *Start from* page) offers ready boards, simple and complex, in a
gallery: a card per preset with a picture of its graph in the current theme, its description, difficulty, the
security score it gets and how many values are left to fill in; a search box; Enter or a double-click creates it,
laid out so it reads left to right without *Arrange*.

| Category | Presets |
|---|---|
| Simple | Locked down (offline); Development (provider, PyPI, npm and GitHub through the proxy, token proxy); Read-only review |
| Development | Web app with a database on this host (`--allow-host-port 5432`, tmux); Systems programming (kernel.org, GitHub, GitLab, more memory and CPUs) |
| Research | Web research (DuckDuckGo search and page fetching as MCP servers defined in the project, each fetch asked for by a ruleset); Browser automation (`--gui`, a browser MCP server, Playwright noted); Binary analysis, offline (samples read-only, catalog servers such as `hub:binwalk`) |
| Local & remote LLM | Local model only (`--llm`, `--no-provider-defaults`); Remote vLLM behind Tailscale (`--llm` and its `--vpn`); Cloud and local models (OpenCode) |
| Privacy & VPN | Everything through a WireGuard VPN (checks stop the start, `--dns` through the tunnel); MCP servers behind different VPNs (search through one profile, fetching through another); Tailnet-only internal services; Own subnet and read-only web (`--subnet`, `--subnet6`, a ruleset that refuses every write) |
| Teams & policy | Strict rules for GitHub (no gists, no issue writes, other writes asked; token proxy); Long-running session (tmux, limits, `--log-dir`, `--log-debug`); GPU workstation (`--gpu`, limits, Hugging Face, an image variant) |

What only you know is a placeholder - `<vpn:search>`, `<llm:local>`, `<mcp:browser>`, `<folder:samples>`, `<gpu:kind>`
- asked for in a short form that offers what sandbox-ai knows: the VPN profiles of `sandbox-ai vpn list --json`, the
LLM servers of `sandbox-ai llm list --json` (an LLM server registered with a VPN fills in its VPN profile too) and the
MCP servers of the MCP servers page. *Fill in later* keeps them: each is a problem on the board (the part outlined in
red), and Apply and Start refuse until it is filled in; an optional part left empty is left out. The tests fill every
preset with VPN profiles and LLM servers registered by sandbox-ai's own commands in a private configuration and have
the launcher check each in a dry run.

sandbox-ai's own presets (`sandbox-ai presets list`) follow in sections of sandbox-ai's categories, in its order
(*sandbox-ai: Development*, *Research & web*, *Local LLMs*, *Security analysis*, *Forensics & reverse engineering*,
*Hardware & devices*, *Network*, *Privacy & anonymity*, *Writing & docs*, *Learning, games & demos*, *Testing
sandbox-ai*), each with its description under the heading; in German with sandbox-ai's German titles and
descriptions. A preset that gives an MCP server a folder of its own (`coding-memory`'s brain, `research-memory`'s case)
asks for it as a folder, and preparing the start adds that server with it (`sandbox-ai mcp add engram --mount
/data=FOLDER:rw`).

*Save as preset…* (on the Board, in its context menu, in the File menu and in the gallery) keeps the board as your own
preset in `~/.config/sandbox-ai/app/presets.json` (0600), listed under *My presets*: a name, a description of several
lines, a category (sandbox-ai's), notes, and by default its folders and the values filled in - the services, MCP
servers with their limits and folders, LLM parameters, the ruleset, the agent's arguments and the layout. Each folder
(the workspaces, the mounts' sources, an MCP server's folder) becomes a placeholder offered with its path of now at
the next use, and a note names what belongs to this computer (paths, devices, names registered here); unticked, the
workspaces are left out and every folder is asked for anew. A board made from a preset of yours replaces it (after
asking) unless unticked; *Rename…* changes a preset's name, description, category and notes. *Export…* and
*Import…* pass presets on as JSON files; a workspace in one is only ever a placeholder, never a path wired in. A preset never holds a secret: an imported file is checked
like untrusted data (the launcher's own parser for the options, sandbox-ai's own checks for MCP definitions and
rulesets, size and types) and refused when anything looks like a token or key, or when it would copy a login in
(`--copy-auth`, `--auth-file`, `--import-codex-auth`, `--git-credentials`).

## Pages

At start a short intro (about nine seconds) plays over the window, in the colours of the theme: the sandbox-ai mark draws itself and opens up into a sandbox, an agent works inside it, and its requests leave through the proxy: two on the allow-list pass, a tracker is stopped, a new host waits until it is approved. Then the title card, and the app. A click or any key skips it (the *Skip* button too); *Help → Watch the intro again* and the button in *Settings → Appearance* play it once more. *Settings → Appearance* turns it off, and so does `SANDBOX_AI_APP_NO_INTRO=1`; `SANDBOX_AI_APP_REDUCED_MOTION=1` shows a short still title card instead. It is drawn by the app itself (`intro.py`, the story in `intro_timeline.py`): no images, nothing fetched.

**Guided tour and setup help.** *Help → Guided tour* walks through every page in the order of the navigation: each step shows the page, what it is for and what to try there, with *Back*, *Next* and *Close the tour* (Esc); it starts where you left it and is offered once at the first start. A part added from the Board's list is set up question by question: the few things that matter for it (a workspace's folder, a mount's source and mode, a network's hosts, an MCP server's name, ...), each with what it means, and at the end what the board still finds wrong with it. *Set up step by step…* in the inspector asks again for a part that is on the board already. The answers are launcher options the part owns, one undo step; *Settings → Appearance* turns the help (and the tour at the first start) off. What it asks lives in `guide.py` (no Qt); the tests check every question against the launcher's own options.

The navigation groups the pages: **Project** (Start, Board), **Running** (Sandboxes & sessions, Approvals,
Terminals), **Insight** (Connections, Activity, Statistics, Files), **Manage** (Kits & plugins, MCP servers, LLM
servers, Images, Credentials, Teams) and **System** (System, Hosts, Help). Ctrl+1 … Ctrl+9 open the first nine pages
in that order. The app's version is at the bottom right of the window, with sandbox-ai's beside it where the two
differ (where sandbox-ai is installed on hover).

| Page | What it does |
|---|---|
| **Start** | The agent, every launcher option (grouped: workspace, network, proxy and privacy, Git, agent, login, display and terminal, hardware, limits, engine), agent arguments, the resulting command line. *Dry run* checks the options and shows the plan; *Start in the app* (a terminal tab), *Start in a terminal window*, *Start as session*; profiles; *Save as project configuration* writes `.sandbox-ai.json` into the first workspace and trusts it, after showing it. |
| **Board** | One sandbox as a node graph (Qt's QGraphicsScene; no Node.js, no web engine): agent, workspaces, mounts, HOME and login, kits, approvals, the connection proxy (TLS inspection, `--no-inspect` exceptions, `--allow-http` rules in a table, privacy check), Git (identity, tokens from a variable, a secret or a file, the token proxy), MCP servers (with their own network, folders and VPN), LLM servers (`--llm`), VPN, rulesets (`--rules`, the project's `rules`), network, devices, limits, display, session, logs (`--log-debug`) and engine. Drag nodes, connect them (a workspace, mount or MCP server counts only while connected to the agent), add them from the palette, undo and redo; the inspector is built from the launcher's own options. Problems, security badges and a score, the command line and the launcher's own verdict (a dry run in the background) follow every change; what the administrator's policy forbids is greyed out. *New from preset…* opens the [preset gallery](#presets), *Apply* writes `.sandbox-ai.json` after showing it and the diff and trusts it (the layout goes to `.sandbox-ai.board.json` next to it), *Dry run*, *Start* (in the app, a window or as a session). The Devices and Limits nodes of a project whose sandbox runs can be applied live. The Kits node offers the kits by name (`sandbox-ai kits list`: built-in ones such as `egm` marked, those not made for the board's agent greyed out, unknown names flagged). *Wizard…* builds the same board step by step (folder, agent, preset, network, proxy and TLS, Git and tokens - a typed token goes to a private file, never into the project file -, MCP, devices and limits, review) and ends on the board. |
| **Sandboxes & sessions** | Every sandbox-ai instance of this account in one table, read again every few seconds in the background: one-off runs from the run registry (`sandbox-ai net list`: started here, in a terminal, by a script or by another copy of the app - with a sandbox-ai that has it, also runs registered under the state folder because their `XDG_RUNTIME_DIR` differed), sessions (running, paused or stopped, wherever they were started), MCP server sandboxes, and launcher processes of this user that have not registered (yet), found in /proc (dry runs left out). Everything that belongs to a start stands below it as one group: the MCP servers and helpers started for it (their `mcp.json`'s `started_for`) or that only it uses, the other members of a team it leads, its pod; a server of a sandbox-ai before 0.1.78, which did not record its start, below the start of its host that started at the same time (said to be a guess in its tooltip); MCP servers of no start together under *Belongs to no start*. Kind, name, project, agent, backend, start time, state and proxy; the current project's rows in bold, or only those (with what belongs to them). The buttons follow the kind of the selected row and say in their tooltip why they do not apply: sessions *Attach* (in a window or in the app), *Pause*, *Resume* and *Delete*; a session of this machine *Copy to host…* and *Move to host…* (`sandbox-ai session copy|move`: a host of `sandbox-ai hosts`, the name there, each folder of the session where it lies there - a changed one becomes `--map` - *Check only* for the dry run there - folders that cannot be created there, MCP servers the host does not define and a session of that name there are refused, a missing login of the agent there is a warning - its output shown; *Replace a session of that name there* deletes it there first, after asking (`--replace`); a start there that fails leaves no session behind; a move stops the session here after asking, its HOME stays; logins and HOME are not copied); anything that runs *Allow…*, *Revoke…* and *Allowed during the run* (`sandbox-ai net`), *Connection log*, *Add device…* (refused devices greyed out with the launcher's reason, its notes shown first), *Remove device* and *Limits…* (`sandbox-ai devices` / `limits`, until the sandbox ends); *Reload rules* (asks first: `sandbox-ai rules reload`, the sandbox's proxy reads your rulesets and its project's anew; the administrator's and the security mode's stay); *Resume with…* also takes other launcher options (one value replaces, a list adds) and options to take out (`sandbox-ai resume NAME OPTIONS --drop-option`, asked first, checked as a new start); a stopped kept start whose HOME changed and that you do not resume after its review is offered *Restore HOME* (`sandbox-ai resume NAME --restore-home`: the recorded text of the changed start files put back, asked first); the ruleset field's *Check* runs `sandbox-ai rules check` and *All rules…* `sandbox-ai rules show` (every ruleset of the start with where it comes from); *Stop…* (asks first) runs `sandbox-ai session stop` (the session's HOME is kept), `sandbox-ai mcp stop` or, for a one-off run, `sandbox-ai net stop` (SIGTERM to its launcher after checking it is that launcher, which then cleans up; a sandbox-ai without it is named as the reason). A launcher that has not registered can only be ended in its terminal. |
| **Activity** | What the agents did in a run, a session or every run of a project, live (`sandbox-ai activity --json --follow`, read as it comes): prompts, replies, tool calls, shell commands with exit code and duration, file changes, network events allowed and refused, approvals, MCP tool calls, the session's start, pause, resume and end, and the tokens of each turn; subagents under their own name. Each kind in its colour of the theme (refusals and errors in red), filters by kind and text, the full record of the selected event (escaped JSON), *Export…* as JSON lines or text (what the filters show). *Show in Connections* opens a network event's run log filtered to its host, *Show in Files* opens a file event's folder in the file browser (a double click does the same). |
| **Approvals** | Open questions of all running sandboxes (a connection, a name lookup, a push despite privacy warnings, a URL to open): run, what is asked, its age and the countdown to the refusal after 60 s. *Allow* with the scope once, for this run or always (this project; capped by the administrator's `max_approval_scope`) - after a confirmation that names the sandbox, the question and the scope (Cancel is the default for *always*) - *Deny* with one click, *Block always* (refused at every later start of the project), through `sandbox-ai approvals allow|deny`, in the background. A URL to open is shown large and escaped; allowing it opens it in your browser (sandbox-ai does that). The selected question explains itself from what is already known, never by looking anything up (a name the agent chose may carry data, so it is resolved only once you allow it): for a connection the address, port, protocol and the name the agent gave (marked unverified), and for both kinds the run's last five log entries to that address or name. The history with answers and reasons, and *Allowed always* per project with *Forget…* (`sandbox-ai approvals remembered` / `forget`; sandboxes running now keep what they were allowed until they end - *Revoke…* on the Sandboxes page takes it back there). Polled every second, also while the window is closed to the system tray; new questions raise a desktop notification and a count in the navigation. |
| **Connections** | The connection proxy's log of a project and run: every request, push and refusal, refused ones in red, a filter, and the full record (headers and bodies, redacted) of the selected line. Follows the log while the sandbox runs. |
| **Statistics** | Per log directory (`logs.telemetry` and the MCP servers' tool calls): model calls and tokens, HTTP requests per host, credential headers sent per host (names only, never values), pushes checked and refused, refused connections, programs started, MCP tool calls; tables with simple bars. |
| **Files** | The workspaces of every running sandbox and session, their HOME when it is on the host (a session's, or a run's `--state-dir`), the current project and any folder you choose: list folders and files, *Copy to the host…* and *Copy files / a folder here…*. Symbolic links are shown as such (with their target) and never followed: not to list, not to copy from or to. Copies stop at 1 GiB, run in the background and can be cancelled; a file gets its name only when it is complete, so a copy cut short (cancelled, failed, or the app quitting) never leaves a truncated file behind, and a folder it created is removed unless the copy completes (folders that were there before are kept). Closing the window while work runs asks first; quitting cancels copies and stops running sandbox-ai commands with every process they started (SIGTERM does the same without asking). Files copied to the host are never executable there (executable bits are dropped), and files that tools run or read as configuration (`.git/config`, hooks, `.envrc`, …) are listed with a warning after the copy. |
| **Kits & plugins** | `sandbox-ai kits list --json`: sandbox-ai's own kit and the default kit (both in every sandbox), the built-in kits (e.g. `egm`, the Elephant-Goldfish Model plugin `egm@sandbox-ai`, Claude only) and yours, each with what it brings per agent (skills, agents, commands, prompts, the memory template, marketplaces and plugins with whether they are installed, notes and problems) and whether the current project uses it; sandbox-ai's own plugin marketplace (sandcastle, egm) with where each plugin comes from, whether it is in the plugin store and which kits declare it. The plugin store's actions come first, each list's actions in a row above it and on its context menu. *New kit…*, *Add to the kit* - *Skill from a folder…* (copied by sandbox-ai without following a link), *New skill…* (SKILL.md from a template), *Plugin by its name…* (the kit declares it; nothing is downloaded), *Marketplace…* (GitHub or a Git server; sandbox-ai refuses a folder and the reason is shown), *Hook…* (a hook script with event and matcher, a `hooks.json` or an OpenCode plugin) -, *Remove from the kit…* (skills, plugins, hook files) and *Delete kit…* run `sandbox-ai kits ...`; each kit's hooks are listed with whether they are approved, and *Review and approve hooks…* runs `sandbox-ai kits trust-hooks KIT` in a terminal, where you read every file and confirm (the app never approves on its own); sandbox-ai's own and the built-in kits are read-only (greyed out, with the reason). *Use in the current project…* adds `--kit NAME` to the project's `.sandbox-ai.json` through the Board's *Apply* (the change is shown, then written and trusted). *Install the kits' plugins* and *Update installed plugins* (group *Plugin store*) run `sandbox-ai plugins claude [--update]` in the background, with `--kit NAME` for the built-in kits the project uses where sandbox-ai supports it: it needs no running sandbox, its output goes to the log window and a notification says how it went (a failure with *Show the log*). A kit only declares a plugin; installing downloads what the kits declare into the plugin store, once for every sandbox. The *Marketplaces* tab is a marketplace browser (`sandbox-ai plugins marketplaces --json`): every marketplace of the host - sandbox-ai's, the kits', the plugin store's, Claude's and Codex's, and OpenCode's npm plugins - with where it comes from, and the plugins of the one selected with their state; for the kit chosen above it (the default kit or one of yours; the built-in ones read-only) it adds a marketplace, imports one Claude or Codex knows, and adds, enables, disables or removes a plugin, each asked first and run as `sandbox-ai kits ...`. |
| **MCP servers** | Your servers with their status (start, stop) and the catalogs of sandbox-ai-mcp and axross-mcp-hub; *Add…* asks for folders, variables, network and limits and runs `sandbox-ai mcp add`. |
| **Images** | `sandbox-ai images`: the agents' images and your variants - built or not, whether each is current with its extension (or needs a rebuild for a changed extension or a new base), agent version, created, size; the installed packages of the selected image (Debian, npm, Python tools, tools) with a filter. *Build* runs `sandbox-ai build AGENT|VARIANT` where commands run (*Other builds*: in the app's terminal, in a terminal window, only the extension layer, all images); *New variant…* (`sandbox-ai images new`, from the agent's extension or empty) and *Delete variant…* (`sandbox-ai images remove`, after a confirmation). The **extension editor** of the selected image: tabs for the Debian packages, npm packages and Python tools (search, add with the validation of sandbox-ai's own images module as you type, remove), the files copied in (path in the image, mode), the Containerfile fragment in an editor with syntax highlighting (instructions, comments, strings, variables, options, continuations; in the theme's colours), line numbers and the validator's findings underlined in place (errors red, warnings amber, the message as tooltip, and as a list that jumps to the line), and a read-only preview of the final Containerfile. *Save* runs `sandbox-ai images extend` (the fragment on standard input) and is refused while there are errors; the app never writes an extension itself. Read without blocking the window. |
| **LLM servers** | `sandbox-ai llm` on this machine or a host: the servers with *Add…*, *Edit…*, *Check*, *Models* and *Remove*. The dialog tests a server before it is saved (*Test connection*: `sandbox-ai wizard models --url`, nothing stored; with the CA an edited server has, `--ca-of`) and fills its default model; an API key typed in becomes a secret of sandbox-ai on that host (on standard input), never an argument. What each server has loaded, with sizes and the memory it uses (`sandbox-ai llm status --json`), read again while the page shows; *Models* lists a server's models, one becomes its default or is copied as `NAME:MODEL` for `--llm`. |
| **Credentials** | Agent logins for sandboxes (`sandbox-ai login --status`: from sandbox-ai's login store or the host, valid or expired), *Log in…* and *Log out…* in a terminal. Secrets (`sandbox-ai secrets`): names and where they are kept (keyring or private files); *Keep secret* takes the value from a password field and passes it on standard input, never as an argument; *Delete…* asks first. Values are never shown. An *Overview* tab lists everything configured with its state, and a tab per kind changes it: also Git logins and publish logins (`sandbox-ai git-logins`, `publish-logins`), identities (`sandbox-ai identities`) and VPN profiles (`sandbox-ai vpn`: WireGuard, OpenVPN, Mullvad, Tailscale, Tor; a key or password typed in becomes a secret, never an argument) - on this machine or, with the host chooser at the top, on a host of `sandbox-ai hosts`. |
| **Terminals** | Terminal tabs inside the app (xterm.js in QtWebEngine): agents started *in the app*, sessions attached in the app, builds and logins. Above them every start that runs on this machine (or a host of `sandbox-ai hosts`), however it was started (command line, app, web interface): one click attaches a start in tmux, one tab per start; a start in the foreground (`--no-tmux`, a task, no terminal) says why it has nothing to attach (`attach_why` of `sandbox-ai list --json`). |
| **Teams** | `sandbox-ai team` on this machine or a host: the teams with their members and task board (`team list`, `team show --json`); *Start* (`team start --agents A,B[,C] --workspace DIR NAME [-- OPTIONS]` in a terminal tab), *Post a task*, *Talk to the lead* (`attach NAME-LEAD`), *Stop* and *Delete* (asked first). |
| **System** | The administrator's policy (`/etc/sandbox-ai/policy.json`, read-only: what it forbids is greyed out everywhere), *All settings…* (appearance, window and notifications, polling intervals, the sandbox-ai checkout to use, defaults for new projects, the board's grid), the sandbox-ai version and whether a newer one exists (`sandbox-ai update --check`, run in the background, quiet when offline), `sandbox-ai doctor` as a table with fixes, image builds, updates, the privacy check of a repository, and the app's settings: theme (see *Themes*), language (system, English, German), where commands such as builds and logins run, whether closing the window keeps the app answering in the system tray (on by default; *Quit* in the tray menu ends it) and an optional start at login in the tray (`~/.config/autostart/sandbox-ai-app.desktop`, written only when ticked; it starts this copy of the app by absolute paths with `python -I` and is refused when the app or the sandbox-ai it loads lies in a folder a sandbox can write - workspaces, HOMEs and rw mounts of running sandboxes and sessions, and sandbox-ai's record of what earlier runs exposed; at login the app waits up to 30 s for the tray, then shows the window). |
| **Hosts** | Other machines (Proxmox LXC containers, servers) of `sandbox-ai hosts`, managed over SSH with your keys: one row per host with whether it answered, its sandbox-ai version and whether an update is there, its starts and open questions (*Show starts*, *Show questions* lead to the Sandboxes and Approvals pages, filtered by it). *Add…*, *Edit…*, *Remove…*, *Test*; *Install…*, *Update sandbox-ai*, *Update all* and *Update from here…* (`hosts install`, `hosts update [--from-here]`, with sudo as set for the host or chosen; a sudo that wants a password opens a terminal tab); tabs for its doctor, its updates, its defaults for every start and its web interface (`web user`, `web cert`, `web vendor`, `web serve`). |
| **Help** | Every page and feature of the app and the concepts of sandbox-ai, searchable in English and German, with the related commands and options and links between the sections; *Tour about…* (Ctrl+F1) finds the tours and sections about a topic. Every page, area and board part has a small round *?* whose *More in Help* opens the matching section here. |

The form is built from the launcher's own option parser, so a new sandbox-ai option appears
without a new app. Every action runs the sandbox-ai command itself: the app can do nothing the
command line cannot, and the command line's checks apply to all of it. Reading uses sandbox-ai's
own modules (loaded from its installation, never copied). At every start, before any of them is loaded, the app checks that neither it nor that installation lies in a folder a sandbox can or could write (sandbox-ai's `exposed.json`, running sandboxes and sessions); if one does, it asks before loading and keeps a warning on screen. Texts that come from a sandbox (host
names, URLs, file names, log entries) are shown escaped and never as HTML.

## Themes

Nineteen looks, chosen on the System page or in *All settings…* and applied at once - the window, the navigation
with its icons, the board's canvas and nodes, and the terminal tabs:

| Theme | Look |
|---|---|
| as the system | the desktop's own style and colours |
| light, dark | modern: soft greys, a blue accent, rounded cards, a dotted canvas |
| hacker | black and phosphor green, monospaced, square |
| cyberpunk | night purple, neon pink and cyan, yellow titles |
| amber | an amber monochrome terminal |
| unicorn | pastel gradient navigation, purple accent, round corners |
| Windows 3.11 | grey bevelled buttons, navy selections and node title bars, a teal canvas |
| Mac | macOS-like: system blue, light grey sidebar, rounded controls |
| Fallout | a green Pip-Boy-like screen with inverted selection bars |
| Solarized Dark, Solarized Light, Dracula, Nord, Gruvbox Dark, One Dark, Catppuccin Mocha, Catppuccin Latte, Tokyo Night | the editor themes in their upstream palettes; where an upstream colour is below 4.5:1 for status text (e.g. Solarized's red, Catppuccin Latte's green and yellow), the nearest shade of the same hue that reaches it, named in a comment in `theme.py` |

Every theme is a set of tokens in `sandbox_ai_app/theme.py` (colours, font, corner radius, bevel, canvas and
terminal colours); `tests/test_theme.py` checks that text in every theme has a contrast of at least 4.5:1
on every background it is drawn on and borders and the canvas grid at least 3:1 (WCAG 2.1), and that Qt
accepts each stylesheet. Fonts fall back to what is installed (e.g. JetBrains Mono, then DejaVu Sans Mono).

*All settings… > Appearance* also chooses the **interface font** (Inter, IBM Plex Sans, Noto Sans, Cantarell, …)
and the **monospace font** of the terminal tabs, code and logs (JetBrains Mono, Fira Code, Cascadia Code, Source
Code Pro, Hack, IBM Plex Mono, Iosevka, Ubuntu Mono, DejaVu Sans Mono, Noto Sans Mono, …), or the theme's own.
Only installed fonts are used: one that is not is listed as *not installed* with how to get it (the package of
Debian/Ubuntu, Fedora, Arch, Homebrew, and the project's site). The app downloads and bundles no fonts. A chosen
font goes first, the theme's after it, so a missing one falls back to the theme's. The web interface offers the
same lists (in `sandbox/web/themes.json`) for the browser it runs in.

Notifications follow the theme too (`sandbox_ai_app/notifications.py`): a popup at the bottom right corner of the
screen - also while the window is hidden in the tray - in the theme's base and text colours, its level shown by a
bar, a border and a sign in the theme's own colour: the accent for information, its warning colour for warnings
(a new question), its error colour for errors (a canary that went off). Errors stay until they are closed, the
others close after *Notifications stay (seconds)* (not while the pointer is over them); a question's popup has
*Allow* and *Block* - the click is the answer (once, for this run), nothing asks again -, *Allow always…* and
*Block always…* - they outlive the run, so they ask once, and that confirmation is the answer - and *Open
approvals*. One answer anywhere (the popup, the Approvals page, the Questions window, the command line, the web)
settles the question everywhere: its popup closes and the Questions window leaves it out. Title and text are always plain text. Where a window cannot place itself on the screen (Wayland),
and with *Notifications are shown: as the desktop's notifications* (Settings, *Window*), the desktop's own
notification is used, with the icon of the level.

PySide6 6.12.0 releases a reference to `None` once too often in every call without a result; below Python
3.12 that aborts the app after a while ("deallocating None"). The app refuses that combination at start
and its requirements exclude it there; use 6.11.x or a later release.

## Terminal tabs

The terminal tabs use [xterm.js](https://xtermjs.org/) in QtWebEngine, connected to a pseudo-terminal
on the host (`sandbox_ai_app/pty_bridge.py`: `os.openpty`, the command as session leader of the new
terminal, a `QSocketNotifier` on the master side, bytes as base64 over QWebChannel, resizing with
`TIOCSWINSZ`, SIGHUP on close and SIGKILL if the command stays). No Node.js is needed, neither to
build nor to run.

- The page (`sandbox_ai_app/web/terminal.html`) loads only local files. A request interceptor blocks
  every URL except the files below `sandbox_ai_app/web` and Qt's `qwebchannel.js`, the page's Content
  Security Policy allows nothing else, navigation away is refused, local content may not reach remote
  URLs, and the browser profile keeps nothing on disk.
- xterm.js is not part of the repository. Vendor it with the standard library only:

  ```bash
  python3 scripts/vendor_xterm.py
  ```

  It downloads the versions pinned in `vendor/xterm.json` (`@xterm/xterm`, `@xterm/addon-fit`) from
  registry.npmjs.org, verifies each tarball against the pinned `integrity` (sha512, as in npm's
  `dist.integrity`) and refuses it on a mismatch, then takes only `lib/xterm.js`, `css/xterm.css`,
  `lib/addon-fit.js` and the licences (regular files only, never links or anything outside the package)
  into `sandbox_ai_app/web/vendor/`, with a `manifest.json` of their SHA-256 sums. While a pin is empty
  it verifies against the registry's own value instead and prints a `::warning::` with the value to
  pin; `--require-pins` refuses that. Check the value and put it into `vendor/xterm.json`.
- QtWebEngine is a separate wheel since PySide6 6.12 (`PySide6-WebEngine`, part of `pip install PySide6`).
  Without it or without the vendored files the app runs commands in a terminal window instead (the first
  of `$SANDBOX_AI_TERMINAL`, x-terminal-emulator, gnome-terminal, konsole, kitty, alacritty, wezterm,
  foot, xfce4-terminal, tilix, xterm), which stays open after the end.
- QtWebEngine runs without the GPU (`--disable-gpu --disable-vulkan`): xterm.js needs none, and on a
  machine without usable acceleration Chromium filled the log with Vulkan, GLX and command-buffer errors.
  `SANDBOX_AI_APP_GPU=1` keeps the GPU; your own `QTWEBENGINE_CHROMIUM_FLAGS` always wins.

## The log

The app writes `~/.local/state/sandbox-ai/app.log` however it is started (from the desktop menu, at login, from
a terminal; rotated at 5 MiB, `app.log.1` … `app.log.3` kept, mode 0600); started from a terminal it runs in the
background and its output goes there too, `--foreground` keeps it on the terminal. If the process itself
crashes (a segfault in Qt), Python's faulthandler writes the stack of every thread into the log. *Debug mode*
(*Settings > Window*, `--debug` or `SANDBOX_AI_APP_DEBUG=1`) also logs every page switch, theme change,
background call and sandbox-ai command with its time and outcome, and Qt's debug messages; *Help > Show the log*
shows the end of the log. Every entry the app writes starts with a line that says when, what and where:

```
2026-11-06T16:49:22.522+01:00 ERROR [images_page.ImagesPage.edited] pid 3568531, thread MainThread, app 0.1.11,
  pyside 6.11.3, qt 6.11.3, python 3.13.5, sandbox-ai 0.1.50 at images_page.py:798 (delivered by MainWindow / ImagesPage / CodeEditor)
  AttributeError: 'ImagesPage' object has no attribute 'check_timer'
  | Traceback (most recent call last): ...
```

The component is the innermost frame of the app's own code; "delivered by" is the window, page and widget
that had the focus. Qt's warnings get the same header (`WARNING [qt.qpa.xcb qxcbwindow.cpp:42]`), the
first line of a start names the platform and the QtWebEngine flags. Lines of native libraries
(Chromium, Mesa) reach the log as they print them.

## Install

With sandbox-ai (recommended): its `install.sh` installs the app too, as the `sandbox-ai-app` command and a
desktop menu entry, and puts PySide6 into an environment of the app's own when `python3` has none
(`--app-terminal` adds the terminal tabs, `--no-app-deps` never downloads, `--no-app` leaves the app out).
A start at login then runs the installation's stable `current/sandbox-ai-app`, so it survives updates.

```bash
cd sandbox-ai && ./install.sh --from .     # or a release: curl ... install.sh | bash
sandbox-ai-app
```

From a sandbox-ai checkout without installing: `./sandbox-ai-app` (needs PySide6 in `python3`).

Ready-made, with sandbox-ai, Python and PySide6 inside: the AppImage for Linux (`sandbox-ai-x86_64.AppImage`,
`sandbox-ai-aarch64.AppImage`) and `sandbox-ai.app` for macOS (`sandbox-ai-arm64.dmg`, `sandbox-ai-x86_64.dmg`)
from [sandbox-ai's releases](https://github.com/c0decave/sandbox-ai/releases); sandbox-ai's README ("Four ways to
install") has the commands, the first start of the unsigned macOS app and how to check the downloads. They come
without QtWebEngine: terminals open in a terminal window. A start at login runs the AppImage or the app
(`SANDBOX_AI_APP_PACKAGE_LAUNCHER`, set by their start).

`sandbox-ai-app --self-test` builds the main window against the sandbox-ai it finds, loads its launcher and quits:
exit 0 and one line `sandbox-ai-app self-test: ok (...)`, or 1 and the problem, without a dialog (with
`QT_QPA_PLATFORM=offscreen` also without a display; the packages' checks in sandbox-ai's release workflow run it).

On its own:

```bash
pip install --user ./sandbox-ai-app        # or: pipx install ./sandbox-ai-app
pip install --user "PySide6-WebEngine"     # optional: terminal tabs (or the full "PySide6")
python3 sandbox-ai-app/scripts/vendor_xterm.py   # optional: terminal tabs, before installing
sandbox-ai-app
```

It finds sandbox-ai through `SANDBOX_AI_HOME` pointing at a checkout (then only there: `$PATH` is not
looked at), else through the `sandbox-ai` command (install.sh or pip); a command or its Python that lies
in a folder a sandbox can write is refused, never run. Profiles and settings are kept in `~/.config/sandbox-ai/app/` (`profiles.json`,
`settings.json`, mode 0600). The language follows the locale unless chosen on the System page
(`SANDBOX_AI_APP_LANGUAGE=en|de` overrides it); a new language applies at the next start, a theme at once.

## Tests

```bash
SANDBOX_AI_HOME=/path/to/sandbox-ai QT_QPA_PLATFORM=offscreen python -m pytest
```

- `tests/test_backend.py`, `tests/test_files.py`, `tests/test_vendor_xterm.py` and `tests/test_i18n.py`
  need no display; `test_files.py` tries symlink escapes, `..`, FIFOs, size limits and races (a folder
  swapped for a link or moved out of the root during a walk, a file swapped for a link before its copy).
- `tests/test_pty.py` drives real pseudo-terminals (output, input, size, SIGWINCH, hang-up, kill).
- `tests/test_state.py` covers what is kept: the file (round trip, 0600, atomic, debounced, damaged or partial),
  each kind of value through a restart (a second window in the same test), the board draft with Undo and Discard,
  and that secrets typed into password fields, agent arguments or the board never reach a file.
- `tests/test_tabs.py` covers the tabs (opening, switching with the pages following, a board per project,
  closing with unapplied changes, order and persistence, a session that starts opening a tab, the context and *+*
  menus), the window's menus and header buttons (quit through the usual questions and shutdown, maximize and full
  screen toggles, the theme menu, tray, zoom, about, README) and the panels (per page, floating, closed, kept).
- `tests/test_projects.py` covers the navigation (sections, shortcuts, a glyph per page), the project switcher and what
  follows it, and every kind of sandbox-ai instance (fake registry entries, sessions, MCP state and harmless sleeper
  processes that look like launchers); `tests/test_kits_page.py` the Kits & plugins page and the Board's kits node
  against a fake inventory, and end to end against the checkout's `sandbox-ai kits` when it has it.
- `tests/test_presets.py` builds every preset (no errors but its placeholders, a layout without overlaps, the
  project file and layout round trip) and fills it in with VPN profiles and LLM servers registered in a private
  configuration until the launcher's dry run accepts it; own presets are saved, exported and imported, and files
  with secrets or unknown options refused. `tests/test_preset_gallery.py` drives the gallery (cards, search, keys,
  the placeholder form, own presets), the Board page and the wizard with the same library.
- `tests/test_ui.py` builds every page offscreen against a fake run registry, log directory and session;
  answers go through the real `sandbox-ai approvals` command, secrets through `sandbox-ai secrets`
  with the file store.
- `tests/test_terminal.py` runs the terminal page in QtWebEngine with a small stand-in for xterm.js
  (and checks that the page reaches nothing remote); with xterm.js vendored, an end-to-end test runs the
  real one. QtWebEngine in containers and CI needs `QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"`
  (`tests/conftest.py` sets it for the tests only).

CI (`.github/workflows/ci.yml`, ubuntu-24.04) checks out sandbox-ai (its default branch, or the ref given
as the `sandbox_ai_ref` input or the `SANDBOX_AI_REF` repository variable), installs PySide6 and the system
libraries Qt needs, vendors xterm.js and runs all tests offscreen with `SANDBOX_AI_APP_REQUIRE_QT=1`: a test
skipped for missing Qt, QtWebEngine or sandbox-ai fails instead (`tests/conftest.py`), and so does a skipped
terminal end-to-end test.

## Licence

MIT. PySide6 is LGPL-3.0 and installed as a separate package; xterm.js (MIT) is downloaded by
`scripts/vendor_xterm.py` with its licence.

## Deutsch

sandbox-ai-app ist eine grafische Oberfläche für sandbox-ai (Python, Qt/PySide6). Sie setzt jede
Option des Launchers, startet Agenten und Sitzungen - in einem Terminal-Tab der App oder einem
Terminalfenster -, beantwortet, was laufende Sandboxen fragen (*Freigaben*: Verbindungen,
Namensauflösungen, Pushes trotz Datenschutz-Warnungen, zu öffnende URLs, mit Countdown bis zur
Ablehnung nach 60 s, Desktop-Benachrichtigung und Zähler in der Navigation), zeigt Statistiken aus den
Logs (Modellaufrufe und Tokens, Hosts, gesendete Zugangsdaten nur mit Namen, Pushes, Ablehnungen,
Programme, MCP-Werkzeugaufrufe), kopiert Dateien aus den Arbeitsordnern und HOMEs von Sandboxen und
Sitzungen heraus und hinein (symbolische Links werden angezeigt, aber nie verfolgt), zeigt die
Agent-Images und Ihre Varianten mit ihren Paketen und bearbeitet ihre Erweiterungen (Paketlisten, Dateien und
ein Containerfile-Fragment mit Syntaxhervorhebung, Zeilennummern, den Befunden der Prüfung direkt im Text
und einer Vorschau des fertigen Containerfiles; gespeichert über `sandbox-ai images extend`, gebaut im
Terminal), zeigt live, was die Agenten in einem Lauf, einer Sitzung oder einem
Projekt getan haben (*Aktivität*: Eingaben, Antworten, Werkzeugaufrufe, Befehle, Dateiänderungen, Netzwerk,
Freigaben, MCP-Aufrufe, Tokens; mit Filtern, Details, Export als JSON oder Text und Sprung zu Verbindungen
und Dateien), verwaltet Anmeldungen und Geheimnisse (Werte werden nie angezeigt und
nur über die Standardeingabe übergeben) und meldet neuere sandbox-ai-Versionen.

Jede Aktion läuft über die Kommandozeile von sandbox-ai; die App kann nichts, was die Kommandozeile
nicht kann. Farbschema (System, hell, dunkel) und Sprache (System, Englisch, Deutsch) werden auf der
Seite *System* eingestellt. Für die Terminal-Tabs braucht es QtWebEngine (`PySide6-WebEngine`) und
xterm.js, das `scripts/vendor_xterm.py` geprüft (sha512) von registry.npmjs.org holt; ohne beides
öffnen sich Befehle in einem Terminalfenster.

Fertig gebaut, mit sandbox-ai, Python und PySide6 darin: das AppImage für Linux und `sandbox-ai.app` für macOS auf
der [Release-Seite von sandbox-ai](https://github.com/c0decave/sandbox-ai/releases) (ohne QtWebEngine; die Befehle
dazu im README von sandbox-ai, „Vier Wege zur Installation"). `sandbox-ai-app --self-test` baut das Hauptfenster
gegen das gefundene sandbox-ai, lädt dessen Launcher und beendet sich mit 0 (oder 1 und dem Problem, ohne Dialog;
mit `QT_QPA_PLATFORM=offscreen` auch ohne Bildschirm).

Oben im Fenster steht groß das aktuelle Projekt mit dem Zustand seiner `.sandbox-ai.json`; die Auswahl daneben
listet die Projekte, die sandbox-ai und die App kennen (zuletzt geöffnete, vertraute Projektkonfigurationen,
Sitzungen, laufende Sandboxen, dauerhaft gemerkte Freigaben, frühere Läufe), dazu *Ordner öffnen…* und *Neues
Projekt (Assistent)…*. *Hilfe → Geführte Tour* führt durch jede Seite in der Reihenfolge der Navigation (was
sie kann, was man dort probiert; Zurück, Weiter, Esc beendet) und wird beim ersten Start einmal angeboten. Jede Seite hat außerdem eine eigene Tour (F1 auf der Seite, *Hilfe → Bereichs-Touren*, das Kontextmenü der Navigation oder *Diesen Bereich kennenlernen* in der geführten Tour): wenige Schritte, jeder umrahmt das Bedienelement, um das es geht. Die Abschnitte der Navigation klappen per Klick ein, *Ansicht → Navigation* klappt alle ein oder aus, *Nur Symbole* (Strg+Umschalt+B) macht sie zu einer schmalen Symbolleiste. Die App schreibt bei jeder Art von Start `~/.local/state/sandbox-ai/app.log` (rotiert, bei einem Absturz mit dem Python-Stack aller Threads); der *Debug-Modus* (*Einstellungen → Fenster*, `--debug`) protokolliert jeden Schritt, *Hilfe → Log anzeigen* zeigt das Ende des Logs, *Hilfe → Intro noch einmal ansehen* spielt das Intro. Jede Seite, jeder Bereich und jedes Teil des Boards hat ein kleines rundes *?*: Es erklärt, was das Ding ist, wozu es dient, was passiert, wenn man es benutzt, und welche Option der Befehlszeile dahintersteht; *Mehr in der Hilfe* öffnet die Seite *Hilfe* (Navigation, Abschnitt *System*; *Hilfe → Hilfe*, Umschalt+F1) beim passenden Abschnitt. Die Hilfe erklärt jede Seite und Funktion der App und die Konzepte von sandbox-ai, durchsuchbar auf Englisch und Deutsch; *Hilfe → Touren durch eine Aufgabe* führen über mehrere Seiten durch ganze Aufgaben (erster Start, eigenes Konstrukt auf dem Board als eigene Vorlage, Freigaben und Netzwerk, Vorlagen-Galerie, die Hilfe selbst) und haken erledigte Schritte ab. Auf dem Board zieht man Bausteine per Drag and Drop dorthin, wo sie hin sollen (eine gestrichelte Karte zeigt die Stelle), verbindet Teile durch Ziehen vom rechten Anschluss auf ein anderes Teil (Esc bricht ab) und legt Ordner und Dateien aus dem Dateimanager als Arbeitsordner oder Einbindung ab; eine `.sandbox-ai.json` öffnet ihr Projekt, eine Vorlagen-Datei wird importiert. Doppelklick und Enter fügen Bausteine weiterhin hinzu; jede Änderung ist ein Schritt von Rückgängig. Ein Teil,
den man aus der Liste des Boards hinzufügt, wird Frage für Frage eingerichtet, mit dem, was das Board danach noch
findet; *Schritt für Schritt einrichten…* im Inspektor fragt für einen vorhandenen Teil erneut. *Einstellungen →
Darstellung* schaltet beides ab. Start, Board, Verbindungen, Statistik, Dateien, Freigaben, Sandboxen und Kits folgen dem
Projekt. Offene Projekte, Sitzungen und Sandboxen stehen als Tabs über den Seiten (Zustand als Punkt, Kontextmenü
zum Anhängen, Pausieren, Fortsetzen, Stoppen; schließen mit Mittelklick oder Strg+W, verschieben per Ziehen,
wechseln mit Strg+Tab; *+* öffnet weitere); jedes Projekt behält sein eigenes Board samt Rückgängig und Zoom, eine
neu startende Sitzung oder Sandbox bekommt automatisch einen Tab, und die Tabs bleiben über einen Neustart
erhalten. Eine Menüleiste (Datei, Ansicht, Einstellungen - *Einstellungen…*, Strg+, -, Gehe zu, Hilfe) und Schaltflächen im Kopf (Minimieren, Maximieren,
Vollbild, Systembereich, Beenden) ersetzen fehlende Fensterknöpfe; die Bausteine und Eigenschaften des Boards und
die offenen Freigaben sind abdockbare Bereiche, deren Anordnung gespeichert wird. Alles, was im Fenster
eingerichtet ist - Größe und Zustand, Seite, eingeklappte Abschnitte, Bereiche, Trenner, Sortierung und
Spaltenbreiten, Filter, Ordner der Dateidialoge, Tabs, das Formular der Startseite je Agent und je Projekt Zoom
und nicht übernommene Änderungen des Boards (mit Hinweis, Rückgängig und Verwerfen) -, steht nach einem Neustart
wieder da (`~/.config/sandbox-ai/app/state.json`, 0600, atomar geschrieben); Geheimnisse gelangen nie in die
Datei, und eine beschädigte Datei wird beiseitegelegt und gemeldet. Die Navigation ist in
Abschnitte gegliedert (Projekt, Laufend, Einblick, Verwalten, System); Strg+1 … Strg+9 öffnen die ersten neun
Seiten. Unten rechts im Fenster steht die Version der App, daneben die von sandbox-ai, wo sie sich unterscheiden. *Sandboxen & Sitzungen* zeigt jede Instanz von sandbox-ai im Konto - einmalige Läufe (auch aus einem
Terminal oder einer anderen App gestartet), Sitzungen, MCP-Server und noch nicht angemeldete Launcher - mit den
passenden Aktionen; *Stoppen…* nutzt `sandbox-ai session stop`, `mcp stop` oder `net stop`. *Terminals* listet
über den Tabs jeden Start, der auf diesem Rechner (oder einem Host) läuft, gleich wie er gestartet wurde: Ein
Klick verbindet einen Start in tmux (ein Tab je Start); ein Start im Vordergrund (`--no-tmux`, eine Aufgabe,
kein Terminal) sagt, warum es nichts zum Verbinden gibt. *Kits & Plugins*
zeigt alle Kits (auch das eingebaute `egm` mit dem Elephant-Goldfish-Plugin) und auf dem Reiter *Marktplätze* jeden
Marktplatz des Hosts (den von sandbox-ai, die der Kits, des Plugin-Speichers, von Claude und Codex) mit seinen
Plugins und ihrem Zustand (`sandbox-ai plugins marketplaces`),
legt Kits, Skills, Plugins, Marktplätze und Hooks über `sandbox-ai kits` an und trägt ein Kit über *Übernehmen* des
Boards (gezeigt, geschrieben, vertraut) in das Projekt ein. Die Hooks eines Kits stehen mit ihrer Freigabe da;
*Hooks prüfen und freigeben…* startet `sandbox-ai kits trust-hooks KIT` in einem Terminal, wo Sie jede Datei lesen
und bestätigen (die App gibt nie selbst frei). *LLM-Server* verwaltet die Server von `sandbox-ai llm` (Hinzufügen,
Bearbeiten, Prüfen, Modelle, Entfernen; *Verbindung testen* vor dem Speichern) und zeigt, was jeder geladen hat und
wie viel Speicher er belegt (`sandbox-ai llm status`). *Teams* startet und beendet Teams von Agenten
(`sandbox-ai team`), mit Mitgliedern und Aufgabenbrett. *Zugangsdaten* zeigt eine Übersicht und je Art einen Reiter:
Agenten-Logins, Geheimnisse, Git- und Veröffentlichungs-Logins, Identitäten und VPN-Profile. *Hosts* verwaltet
andere Rechner über SSH: Version, Updates (auch *Von hier aktualisieren…*), Doctor, Vorgaben und Weboberfläche je
Host. Fast jede Seite arbeitet auf diesem Rechner oder, über die Host-Auswahl, auf einem Host.

*Neu aus Vorlage…* auf dem Board (und die Seite *Ausgangspunkt* des Assistenten) bietet fertige Boards in einer
Galerie: je Vorlage eine Karte mit einem Bild des Graphen im aktuellen Farbschema, Beschreibung, Schwierigkeit,
Sicherheitswertung und der Zahl der auszufüllenden Werte; mit Suche, Enter oder Doppelklick legt sie an, fertig
angeordnet. Die Kategorien: Einfach (offline, Entwicklung, Review nur lesend), Entwicklung (Web-App mit Datenbank
auf diesem Rechner, Systemprogrammierung), Recherche (Web-Recherche mit Such- und Fetch-MCP-Servern des Projekts,
Browser-Automatisierung, Binäranalyse offline), lokale und entfernte LLMs (nur lokales Modell, entferntes vLLM hinter
Tailscale, Cloud und lokal mit OpenCode), Privatsphäre und VPN (alles durch WireGuard, MCP-Server hinter
verschiedenen VPNs, interne Dienste nur im Tailnet, eigenes Subnetz mit nur lesendem Web) sowie Teams und Richtlinien
(strenge Regeln für GitHub, lang laufende Sitzung mit Grenzen und `--log-debug`, GPU-Workstation). Was nur Sie wissen,
ist ein Platzhalter (`<vpn:search>`, `<llm:local>`, `<mcp:browser>`, `<folder:samples>`): ein kurzes Formular bietet
die VPN-Profile aus `sandbox-ai vpn list --json`, die LLM-Server aus `sandbox-ai llm list --json` und die MCP-Server
an; *Später ausfüllen* lässt sie stehen, jeder ist dann ein Problem auf dem Board, und Übernehmen und Starten
verweigern, bis er ausgefüllt ist. Die Vorlagen von sandbox-ai folgen in Abschnitten nach dessen Kategorien, in
dessen Reihenfolge (*sandbox-ai: Entwicklung*, *Recherche & Web*, *Lokale LLMs*, *Sicherheitsanalyse*, *Forensik &
Reverse Engineering*, *Hardware & Geräte*, *Netzwerk*, *Datenschutz & Anonymität*, *Schreiben & Dokumentation*,
*Lernen, Spiele & Demos*, *sandbox-ai testen*), jeder mit seiner Beschreibung unter der Überschrift, mit den deutschen
Titeln und Beschreibungen von sandbox-ai. Eine Vorlage, die einem MCP-Server einen eigenen Ordner gibt (das Gedächtnis
von `coding-memory`, der Fall von `research-memory`), fragt ihn als Ordner ab, und die Vorbereitung des Starts fügt
diesen Server damit hinzu (`sandbox-ai mcp add engram --mount /data=ORDNER:rw`). *Als Vorlage speichern…* (auf dem
Board, in seinem Kontextmenü, im Menü Datei und in der Galerie) behält das Board als eigene Vorlage in
`~/.config/sandbox-ai/app/presets.json` (0600), unter *Meine Vorlagen*: Name, eine mehrzeilige Beschreibung, eine
Kategorie (die von sandbox-ai), Hinweise und standardmäßig seine Ordner und die ausgefüllten Werte - Services,
MCP-Server mit ihren Grenzen und Ordnern, LLM-Parameter, das Regelwerk, die Argumente des Agenten und die Anordnung.
Jeder Ordner (Arbeitsordner, Quellen der Einbindungen, der Ordner eines MCP-Servers) wird ein Platzhalter, bei der
nächsten Verwendung mit seinem jetzigen Pfad angeboten, und ein Hinweis nennt, was zu diesem Rechner gehört;
abgewählt bleiben die Arbeitsordner weg und jeder Ordner wird neu erfragt. Ein Board aus einer eigenen Vorlage ersetzt
diese (nach Rückfrage), außer abgewählt; *Umbenennen…* ändert Name, Beschreibung, Kategorie und Hinweise.
*Exportieren…* und *Importieren…* geben Vorlagen als JSON-Dateien weiter; ein Arbeitsordner darin ist immer nur ein
Platzhalter, nie ein fest eingetragener Pfad. Eine Vorlage enthält nie ein Geheimnis: eine importierte Datei
wird wie fremde Daten geprüft (Optionen mit dem Parser des Launchers, MCP-Definitionen und Regelsätze mit den
Prüfungen von sandbox-ai) und abgelehnt, wenn etwas wie ein Token oder Schlüssel aussieht oder sie eine Anmeldung
hineinkopieren würde.

Neunzehn Farbschemata stehen zur Wahl (Seite *System* oder *Alle Einstellungen…*), sofort wirksam für Fenster,
Navigation, Board und Terminals: wie das System, hell, dunkel, Hacker, Cyberpunk, Bernstein, Einhorn,
Windows 3.11, Mac, Fallout und die Editor-Themes Solarized Dark und Light, Dracula, Nord, Gruvbox Dark, One Dark,
Catppuccin Mocha und Latte sowie Tokyo Night in ihren Original-Paletten. Jedes erfüllt die WCAG-Kontraste (Text
4,5:1, Rahmen 3:1); die Tests prüfen das. Wo eine Originalfarbe für Statustext unter 4,5:1 liegt, gilt der nächste
Ton derselben Farbe, der es erreicht (in `theme.py` vermerkt). *Alle Einstellungen… > Aussehen* wählt außerdem die
Schrift der Oberfläche und die Festbreitenschrift für Terminals, Code und Logs; nicht installierte Schriften
stehen als *nicht installiert* mit dem Paketnamen bzw. der Projektseite in der Liste - die App lädt keine
Schriften herunter.
Auch Benachrichtigungen folgen dem Design: ein Hinweis unten rechts auf dem Bildschirm (auch wenn das Fenster im
Tray ist), Warnungen (eine neue Frage) in der Warnfarbe des Designs, Fehler (ein ausgelöster Canary) in seiner
Fehlerfarbe; Fehler bleiben, bis sie geschlossen werden. Unter Wayland, oder mit *Benachrichtigungen erscheinen:
als Benachrichtigungen des Desktops*, kommt die Benachrichtigung des Desktops.
