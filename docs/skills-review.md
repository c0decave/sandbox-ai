# Security review of the vendored skills

sandbox-ai 0.1.94 ships nine third-party skill sets as **built-in kits** (`sandbox/builtin/kits/NAME`), for Claude
Code, Codex and OpenCode alike, and one it cannot ship (claude-d3js-skill: no licence). This document is the review
behind them: what each brings, what runs, what it may load into an agent as instructions, what talks to the
network, what was left out and why, and how sandbox-ai keeps it in check. Reviewed 2026-11-12.

## How they are shipped

- **Vendored, pinned, unchanged.** Each kit holds the upstream files of one commit, byte for byte. `kit.json` lists
  every file with its SHA-256 and its git blob id - the id `git ls-tree -r COMMIT` shows upstream, so anyone can
  check a file against the pinned commit without trusting sandbox-ai's copy. `tests/test_vendored_skills.py`
  recomputes both for every file and fails on any file not listed. Files that are not needed were left out (each kit's
  `kit.json` lists them under `left_out`, the reasons are below); no file was edited.
- **At least 14 days old.** Every pin is the newest first-parent commit of the default branch committed on or before
  2026-10-28 (the release is 2026-11-12). Where newer commits exist, what they add is said below.
- **One copy for every agent.** A built-in kit's `skills/` folder is shared by its agent folders (`claude/`,
  `codex/`, `opencode/`, each marked with a `.shared-skills` file; only sandbox-ai's own kits can do this, see
  `kits.shared_skills`). A start copies the skills read-only to where each agent looks: `~/.claude/skills`
  (Claude Code), `~/.agents/skills` (Codex), `~/.config/opencode/skills` (OpenCode 1.19.5; it takes a skill whose
  `name` matches its folder, `^[a-z0-9]+(-[a-z0-9]+)*$`, with a description of at most 1024 characters - every
  vendored skill passes, tested). Nothing is installed, fetched or built at a start.
- **Off by default.** No kit is part of a start unless it is named: `--kit NAME`, `"options": ["--kit", "NAME"]` in
  the project's `.sandbox-ai.json`, or *Use in the current project* on the app's Kits & plugins page. Reasons: each
  skill's description goes into every session's context and steers the agent; superpowers changes how the agent
  works altogether; security-audit and ffuf are for security testing; hyperframes and hallmark pull in tools and
  services. sandbox-ai's own skill stays in every sandbox.
- **Hooks off.** No vendored kit runs a hook. Hallmark's optional edit-time hook is a kit of its own,
  `hallmark-hook` (Claude Code only), which runs only after `sandbox-ai kits trust-hooks hallmark-hook`.
  Superpowers' SessionStart hook is not part of the kit.
- **Telemetry off.** With the default `--telemetry off` (and `strict`), every sandbox gets
  `HYPERFRAMES_NO_TELEMETRY=1`, `DO_NOT_TRACK=1`, `HYPERFRAMES_NO_UPDATE_CHECK=1`, `HYPERFRAMES_NO_AUTO_INSTALL=1`,
  `HYPERFRAMES_NO_OPTIONAL_INSTALL=1`, `HYPERFRAMES_SKIP_SKILLS=1` and `SUPERPOWERS_DISABLE_TELEMETRY=1`. Only
  `--telemetry on` leaves them out.
- **The sandbox still holds.** Whatever a skill tells the agent to run, it runs inside the sandbox: its firewall and
  connection proxy (no host the start does not allow; `registry.npmjs.org` only with `--allow-npm`), its read-only
  kit copies, approvals, the privacy check of pushes, the kill switch. The review below assumes that and names the
  places where a skill would want more.

## Method

For every upstream at its pinned commit:

1. The file list from the commit's tree; the licence and the repository's own install paths (plugin manifests,
   hooks, installers) read in full, whether vendored or not.
2. A scan of **every** vendored file (a script, not trusted alone): URLs and hosts; network APIs (`fetch`, HTTP
   clients, sockets, `curl`, `npx`, `npm install`, `git clone`, `giget`); process spawning and `eval`; file writes,
   `$HOME` and dot-folder paths; environment reads; credential words; prompt-injection and exfiltration phrasing
   (ignore previous instructions, without telling the user, silently, send/upload to, `curl | sh`, disable the
   sandbox, telemetry, analytics, beacon, auto-update); invisible and bidirectional Unicode; long base64 blobs;
   binary files. Every hit was looked at.
3. Read in full: the `SKILL.md` of every kit's main skill and of every skill that runs scripts or uses services
   (hallmark, using-superpowers, playwright-skill, hyperframes, hyperframes-cli, media-use, figma, security-audit,
   design-system, ffuf-web-fuzzing, tdd, d3-viz), every hook and installer, every script that runs on its own or
   sends anything off the machine (the telemetry modules, the API clients, the visual companion, the validators, the
   helpers), and the references that tell the agent to install, publish, upload, log in or report. The other `SKILL.md` files and the
   ~2 MB of references (themes, macrostructures, motion and media recipes) were covered by the scan above plus a
   second pass for concealment phrasing (hide, conceal, don't tell, never reveal, from the user), each hit read in
   context; the scripts' 0.86 MB by the scan, every network, process and write hit read.
4. The npm packages of the CLIs the skills drive (hyperframes, Playwright): resolved with
   `npm install --package-lock-only --before=2026-10-29` (only versions published 14 days before), every package's
   licence and install script listed; the hyperframes 0.8.214 tarball checked against the registry's SHA-512 and read
   where it sends data, updates or installs.

Findings are graded **risk** (needs a counter-measure, taken), **note** (worth knowing, no change) and **ok**.

## Summary

| Kit | Upstream (licence) | Pin (committed) | Vendored | Verdict |
|---|---|---|---|---|
| hallmark | [Nutlope/hallmark](https://github.com/Nutlope/hallmark) (MIT) | `8f6969f8922caabde9de1784959eae11ddf68073` (2026-10-28) | 113 files, 1.5 MiB | ok; installers and theme pictures left out; hook opt-in (`hallmark-hook`) |
| superpowers | [obra/superpowers](https://github.com/obra/superpowers) (MIT) | `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b` (2026-10-21, v7.0.1) | 65 files, 386 KiB | ok; telemetry logo off; diagnosing-superpowers left out; no hook |
| playwright-skill | [lackeyjb/playwright-skill](https://github.com/lackeyjb/playwright-skill) (MIT) | `dd47a6a023e249eb1b36e9e943eab89d0900865d` (2026-08-14) | 7 files, 35 KiB | ok; its setup cannot download in a sandbox; image recipe `playwright` |
| (none) | [chrisvoncsefalvay/claude-d3js-skill](https://github.com/chrisvoncsefalvay/claude-d3js-skill) (**no licence**) | `e198c87d03f1c9811acbff681886fac24baee53d` (2025-10-17) | **not vendored** | content ok; may not be redistributed |
| hyperframes | [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes) (Apache-2.0) | `cfa653af4d0f95d751286fb25c8d38bdb56ef663` (2026-10-28) | 211 files, 3.1 MiB | risk handled: telemetry, updates and run-time installs off; CLI from a pinned lockfile (recipe `hyperframes`) |
| security-audit | [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill) (MIT) | `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8` (2026-09-14) | 19 files, 255 KiB | ok; reports land in the kept HOME |
| design-system | [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) `design/skills/design-system` (Apache-2.0) | `2574e29377c7173d0cecffef29a6ecbd7f8ccb5d` (2026-10-27) | 2 files, 17 KiB | ok; upstream instead of collectivebrain.de's copy |
| ffuf | [jthack/ffuf_claude_skill](https://github.com/jthack/ffuf_claude_skill) (MIT per README) | `50fd4af4d8bd60cfc48fd84eda1720fbd718857d` (2025-10-16) | 5 files, 36 KiB | ok; offensive tool, authorised targets only |
| tdd | [mattpocock/skills](https://github.com/mattpocock/skills) `skills/engineering/tdd` (MIT) | `59db609eb1e18aaf59b63284d0e52255aff0c63c` (2026-10-27) | 5 files, 8 KiB | ok; overlaps superpowers' TDD |

All vendored files together: 5.3 MiB (427 files), plus the two npm lockfiles of the image recipes (64 KiB).

## hallmark

**Upstream:** Nutlope/hallmark, `skills/hallmark` (v2.0.0, "Powered by Together AI"), MIT
(`Copyright (c) 2026 Hallmark contributors`). Pin `8f6969f8922caabde9de1784959eae11ddf68073`, 2026-10-28. The 22
commits after it (2026-10-29 to 2026-11-11) are fixes to sloplint, theme.mjs, capture.mjs and imagegen.mjs (CRLF
handling, symlink cycles, capture of HTTP errors, reroll exhaustion), their tests and repository chores; nothing in
them changes what the skill asks of the agent beyond `references/flow.md` wording.

**What it is:** a design workflow (SKILL.md, 18 KB) with a "Floor" of rules, 60+ theme descriptions, 21 page
shapes and a static linter. Scripts (Node, standard library only): `sloplint.mjs` (static checks of HTML/CSS/JSX;
writes `.hallmark/` snapshots in the project), `sloplint-render.mjs`, `capture.mjs`, `picture.mjs` (screenshots
through puppeteer-core and a local Chrome, found locally, via `HALLMARK_PUPPETEER` or `npm root -g`), `theme.mjs`
(deals themes from the local index, writes `tokens.css`/`base.css` and `.hallmark/log.json`; `photos --save`
downloads a library photo, refusing anything but `https://upload.wikimedia.org/`), `imagegen.mjs` (POSTs a prompt to
`https://api.together.xyz/v1/images/generations` with `TOGETHER_API_KEY`), `lint-hook.mjs` (the hook, below).
`photos.json` lists 755 Wikimedia Commons photos (CC0/CC BY/CC BY-SA, credited per use).

**Instructions (SKILL.md and references, scanned in full, SKILL.md read in full):** no hidden or injected
instructions; one zero-width joiner, inside an emoji regex in sloplint.mjs. It asks the user once for material,
never invents facts, and tells the agent to run its own scripts. It names network use honestly: generated images only
"when `TOGETHER_API_KEY` is set and the user asked for them ... Never auto-fire it"; library photos are hot-linked
from upload.wikimedia.org or saved from there; fonts from fonts.googleapis.com/api.fontshare.com go into the pages
it writes (the pages load them, not the agent).

**Findings:**

- **risk (handled): `install-hook.mjs` rewrites a `settings.json`** (`./.claude/settings.json` of the project, or
  `~/.claude/settings.json` with `--global`) to add a PostToolUse hook - inside a sandbox that would land in the
  user's project, pointing at a sandbox path. Not vendored. The hook itself is offered as the kit `hallmark-hook`
  (Claude Code only: Hallmark has no hook for Codex or OpenCode), a `hooks.json` that runs the vendored, unchanged
  `lint-hook.mjs` after Write/Edit/MultiEdit; like every kit hook it runs only as a managed hook once approved with
  `sandbox-ai kits trust-hooks hallmark-hook`. `lint-hook.mjs` (read in full) reads the hook's JSON from stdin, reads
  the written file, runs `node sloplint.mjs FILE --json --tier floor` with a 4 s timeout and returns its failures as
  `additionalContext`; it never blocks and touches nothing else.
- **ok: `install.mjs` left out** - it copies the skill into `~/.claude/skills`, `~/.agents/skills` and
  `~/.config/opencode/skills`, which sandbox-ai does itself (and which are read-only in a sandbox). The self-test
  `refcheck.mjs` and its `scripts/fixtures/` are left out with it.
- **note: theme pictures left out** (`references/themes/*.jpg`, 50 files, 5.2 MiB): the skill says "look at the
  three pictures" at the deal; `theme.mjs` works without them (it lists a theme's picture only if it exists) and
  `scripts/picture.mjs` renders them locally where puppeteer-core and Chromium are there (the image recipe
  `hyperframes` brings both; set nothing else - the recipe sets `HALLMARK_PUPPETEER=/opt/hyperframes`).
- **note: network** - Together AI (paid, the user's key, only on request), upload.wikimedia.org, and
  `npm i -g puppeteer-core` suggested when the render tier is missing: each needs the host allowed at the start.
- **ok:** no telemetry; the scripts read and write only the project (`.hallmark/`, the files named) and the skill's
  own folder (read).

## superpowers

**Upstream:** obra/superpowers (Jesse Vincent, Prime Radiant), MIT. Pin `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`,
tag v7.0.1, 2026-10-21. **Newer:** v7.0.2 (`22f0b463`, 2026-11-03, 9 days old at the release, so not taken; it was
the pin of the earlier Claude-only plugin, now moved back to v7.0.1): brainstorming tuning, SessionStart-hook
bootstrap fixes, subagent-driven-development helper fixes (a new `sdd-repo` script), and the systematic-debugging
pressure-test files removed.

**What it is:** 15 skills for a disciplined workflow. Vendored: brainstorming, dispatching-parallel-agents,
executing-plans, finishing-a-development-branch, receiving-code-review, requesting-code-review,
subagent-driven-development, systematic-debugging, test-driven-development, using-git-worktrees, using-superpowers,
verification-before-completion, writing-plans, writing-skills. Scripts: `brainstorming/scripts/server.cjs` (a
zero-dependency HTTP/WebSocket "visual companion" bound to 127.0.0.1 on a random port, with a per-session secret key
and an Origin check; `start-server.sh`, `stop-server.sh`), `executing-plans/scripts/task-start|task-done` and
`subagent-driven-development/scripts/*` (git and file bookkeeping in the repository), `find-polluter.sh` (runs the
test suite to bisect a polluting test), `writing-skills/render-graphs.js` (Graphviz `dot` locally). Read in full
(the scripts) or scanned (all skills); nothing sends data anywhere.

**Findings:**

- **risk (handled): the visual companion's logo is a tracking pixel.** `server.cjs` puts
  `<img src="https://primeradiant.com/brand/superpowers-visual-brainstorming-logo.png?v=VERSION">` into every page
  it serves, so the browser that shows the companion tells primeradiant.com the version and its address - unless
  `SUPERPOWERS_DISABLE_TELEMETRY`, `DISABLE_TELEMETRY` or `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is set. sandbox-ai
  sets `SUPERPOWERS_DISABLE_TELEMETRY=1` in every sandbox (and `DISABLE_TELEMETRY=1` for Claude already); the
  server then renders the text only. The server also opens a browser through `BRAINSTORM_OPEN_CMD` or the platform
  opener when one exists (inside a sandbox there is none unless `--gui`).
- **risk (handled): `diagnosing-superpowers` left out.** It reads the agent's session transcripts from disk, writes a
  case under `~/.superpowers/`, and - after the user approves the text - files an issue on github.com/obra/superpowers
  with `gh` (or prepares a prefilled URL). Approval-gated, but its purpose is publishing session material for the
  maintainers; not useful for sandbox-ai's users and the one skill here that moves data out.
- **ok: no hook.** Upstream's SessionStart hook (`hooks/session-start`, read in full) only prints the
  using-superpowers skill wrapped in `<EXTREMELY_IMPORTANT>` as session context. The kit ships the skill without the
  hook: it loads by its description ("Use when starting any conversation"). The Claude plugin with the hook stays
  available as `superpowers@sandbox-ai` (pinned to the same commit), for a kit of yours that names it.
- **note: strong steering.** using-superpowers tells the agent it "ABSOLUTELY MUST" invoke any skill with a 1 %
  chance of applying, before any answer. That is the framework's design, not an injection; it states that the
  user's instructions (CLAUDE.md, AGENTS.md, direct requests) take precedence. Reason enough to keep the kit opt-in.
- **note: destructive advice, by design.** test-driven-development says "Write code before the test? Delete it.
  Start over." finishing-a-development-branch offers to push, merge or delete the branch (`git branch -D` after the
  human chooses discard) and forbids `--force` "on your own initiative". In a sandbox a push still passes the privacy
  check and needs a Git login the human gave.
- **note:** using-git-worktrees runs `npm install` / `pip install` in a new worktree when the project has a
  manifest - subject to the sandbox's allow-list like any install. Skills refer to each other as `superpowers:NAME`
  (the plugin's namespace); as plain skills they are `NAME`, which the agents resolve.

## playwright-skill

**Upstream:** lackeyjb/playwright-skill 5.0.0, MIT. Pin `dd47a6a023e249eb1b36e9e943eab89d0900865d`, 2026-08-14 (the
newest commit).

**What it is:** SKILL.md (the agent writes Playwright scripts and runs them through `run.js`), `API_REFERENCE.md`,
`lib/helpers.js` (dev-server detection by HEAD requests to localhost ports, a browser launcher honouring
`PW_BROWSER`, `PW_CHANNEL`, `PW_EXECUTABLE_PATH`, `PW_HEADLESS`, extra headers, cookie-banner clicks, screenshots),
`run.js` (runs a script or `-e` snippet with `node`, optional copy to `PW_SCRIPT_DIR`), `package.json` and its lock
(`playwright ^1.62.0`). All read in full.

**What it downloads:** its setup, `npm run setup` = `npm install && npx playwright install chromium`, fetches
Playwright from registry.npmjs.org and a Chromium build (~170 MB) from Playwright's CDN (cdn.playwright.dev /
playwright.download.prss.microsoft.com), which Playwright does not check against a hash. `install-all-browsers`
adds Firefox and WebKit.

**Findings:**

- **risk (handled): nothing is downloaded, silently or at all.** In a sandbox the skill folder is a read-only copy,
  so `npm run setup` fails at its first write, and the firewall refuses registry.npmjs.org (unless `--allow-npm`) and
  the CDN (unless allowed). To make the skill work, `sandbox-ai images recipe playwright --from AGENT` builds a
  variant with Playwright 1.64.1 (2026-10-14; `playwright` and `playwright-core`, both Apache-2.0, pinned by SHA-512 in
  `sandbox/recipes/playwright/package-lock.json`, installed with `npm ci --ignore-scripts`) linked into
  `/usr/local/lib/node` - one of Node's global folders, which `run.js`'s `require.resolve('playwright', {paths:
  [skillDir]})` and the agent's CommonJS scripts search - and Debian's Chromium (signed by Debian's archive key) with
  `PW_EXECUTABLE_PATH=/usr/bin/chromium`. The skill's `helpers.launchBrowser()` takes that path; a script that calls
  `chromium.launch()` itself must pass `executablePath: process.env.PW_EXECUTABLE_PATH` (sandbox-ai's own skill says
  so). Debian 12's Chromium (150 at the review) is not the build Playwright 1.64 was made for (156); the DevTools protocol
  is stable enough for what the skill does, but newer features may be missing.
- **alternative:** the signed MCP server `playwright` (sandbox-ai-mcp catalog, `--mcp playwright`) brings its own
  reviewed browser in its own container.
- **note:** `connectOverCDP('http://127.0.0.1:9222')` reuses a running Chrome with the user's session - in a
  sandbox only a browser inside it. The skill itself warns not to use it for secrets.
- **ok:** no telemetry, no network use besides the pages the agent is asked to open (allow-list applies).

## claude-d3js-skill (not shipped)

**Upstream:** chrisvoncsefalvay/claude-d3js-skill, one commit, `e198c87d03f1c9811acbff681886fac24baee53d`
(2025-10-17): `SKILL.md` (skill name `d3-viz`), three references (colour schemes, d3 patterns, scales) and three
assets (two React/JSX templates, sample data). All read: plain d3.js guidance; the only URL is the d3 CDN
(`https://d3js.org/d3.v7.min.js`) in an example; no scripts that run, no injection.

**Why it is not vendored:** the repository has **no licence** - no LICENSE file, nothing in the README or the
skill's front matter, and GitHub detects none. Without a licence nobody may redistribute it, so sandbox-ai does not
ship it. To use it yourself (your own copy, on your machine):

```bash
git clone https://github.com/chrisvoncsefalvay/claude-d3js-skill /tmp/d3js
git -C /tmp/d3js checkout e198c87d03f1c9811acbff681886fac24baee53d
sandbox-ai kits new d3js
sandbox-ai kits add-skill d3js /tmp/d3js --as d3-viz    # every agent; a snapshot, symlinks skipped
```

Its folder name must be `d3-viz` (the skill's name) for OpenCode, hence `--as`.

## hyperframes

**Upstream:** heygen-com/hyperframes (HeyGen), Apache-2.0 (no NOTICE file upstream; `CREDITS.md` names prior art
and MPL-2.0 mediabunny, which is part of the studio, not of the skills). Pin
`cfa653af4d0f95d751286fb25c8d38bdb56ef663`, 2026-10-28, plugin version 0.8.214. **Newer:** 669 commits to
2026-11-12 (CLI 0.9.39), among them two more skills (`reverse-any-video`, `style-sampling`, 2026-11-09 - hence 9
skills upstream today, 7 at the pin), "every published skill reports its own run" (2026-11-04: usage beacons in
every skill), more agent runtimes detected in telemetry, and a rewording of billing lines "the Snyk audit reads as
hiding cost". None of it is taken.

**What it is:** 7 skills - hyperframes (the HTML composition contract, film, captions, overlays, motion, look),
hyperframes-cli, hyperframes-registry, media-use (images, video, audio, voices, captions, grading; providers HeyGen,
Gemini, ElevenLabs, OpenAI, Groq, Openverse, Freesound), figma, slideshow, remotion-to-hyperframes - with
frame presets (HTML, OFL fonts), 20 Pixabay sound effects (Pixabay Content License: free use and redistribution as
part of derived works, no attribution required), and Node/Python helper scripts. Left out: 91 files - the skills'
tests (`*.test.mjs`, `*_test.py`, `scripts/tests/`), Remotion's test corpus (its `run.sh`/`setup.sh` install
packages), `.gitignore`/`.gitkeep`.

**Findings:**

- **risk (handled): telemetry.** The CLI (`hyperframes` on npm) sends PostHog events (`us.i.posthog.com`, public
  key `phc_zjjb...`) with CLI version, OS, CPU, memory, CI/agent/sandbox detection, an anonymous id kept in
  `~/.hyperframes/config.json`, and - once you sign in to HeyGen - your account e-mail or user name. media-use's
  scripts send their own events (`scripts/lib/telemetry.mjs`) with the same identity. The skills tell the agent to
  send more: figma's "Usage beacon: ... run `npx hyperframes events --skill=figma-motion` when you begin" and
  `--event=skill_completed` at the end; hyperframes-cli's "After a verified render, send one `feedback --rating` ...
  unless telemetry is off" - and `references/feedback.md` adds a "MISSING FEATURE" report for every ask HyperFrames
  cannot serve, "what the person asked for, in their words" (names, figures and paths left out), to what it calls "a
  public channel"; hyperframes-registry's `feedback --search-miss` for catalogue gaps. **The opt-out, proven:**
  - in the CLI 0.8.214 (tarball SHA-512 `ZPxgxGB5...aahIQ==`, equal to the registry's, with npm provenance):
    `telemetry/policy.ts` returns an override for `HYPERFRAMES_NO_TELEMETRY` or `DO_NOT_TRACK` set to `1`, `true`,
    `yes` or `on`; `shouldTrack()` is then false; `trackEvent` (the only producer of the event queue, and so of
    `flush`, `flushSync`, the detached `node -e fetch(...)` at exit and the DNS probe of the PostHog host) returns
    before queueing; `events` goes through `trackEvent`; `feedback` and `feedback --search-miss` (POSTs to HeyGen's
    API) print "Telemetry is disabled. Feedback not sent." / "Nothing sent." and return; the telemetry notice and
    the canary cohorts are skipped (`dist/chunk-ZCHULPJZ.js`, `chunk-FI3JWR34.js`, `feedback-*.js`, `events-*.js`);
  - in media-use's `telemetry.mjs` (vendored): `optedOut()` is true for `HYPERFRAMES_NO_TELEMETRY=1` or
    `DO_NOT_TRACK=1`, and `track()` returns before it writes an id or calls `fetch`. `tests/test_vendored_skills.py`
    runs that very file under Node with a stub `fetch`: with the environment a sandbox gets, zero calls and no
    `~/.hyperframes`; without it (the control), one POST.
  sandbox-ai sets both variables in every sandbox unless `--telemetry on`; and PostHog's host is not on any
  allow-list, so the firewall refuses it anyway.
- **risk (handled): downloads and updates at run time.** The CLI checks npm for updates and can install a newer CLI
  in the background (`autoUpdate`), installs optional packages on first use (`HYPERFRAMES_NO_OPTIONAL_INSTALL`),
  installs or refreshes skills from GitHub (`init`, `skills update`; `HYPERFRAMES_SKIP_SKILLS`), and downloads its
  own Chrome through `@puppeteer/browsers` unless `HYPERFRAMES_BROWSER_PATH` points at one. `npx hyperframes` itself
  downloads the latest CLI when none is installed. sandbox-ai sets `HYPERFRAMES_NO_UPDATE_CHECK=1`,
  `HYPERFRAMES_NO_AUTO_INSTALL=1`, `HYPERFRAMES_NO_OPTIONAL_INSTALL=1`, `HYPERFRAMES_SKIP_SKILLS=1` (each checked in
  the dist), and the image recipe gives a CLI so `npx` never fetches one (below). Without the recipe, `npx` reaches
  registry.npmjs.org only under `--allow-npm`.
- **risk (handled): the plugin launcher.** `hyperframes/scripts/plugin-cli.mjs` (for plugin installs) runs
  `npx hyperframes@VERSION`, a download. It is chosen only when a `plugin.json` lies two folders above SKILL.md; the
  kit ships no plugin manifest (tested), so the skills take the standalone path.
- **note: cloud services on request.** `render --cloud`/`lambda`/`cloudrun`/`publish` upload the project (to
  HeyGen's S3, your AWS or GCP); media-use calls HeyGen (OAuth or API key; billed), Gemini (`GEMINI_API_KEY`;
  `hyperframes/scripts/locate.mjs` too), ElevenLabs, OpenAI/Groq (transcription), Openverse and Freesound; the
  composition pages load GSAP and fonts from cdn.jsdelivr.net and fonts.googleapis.com when rendered; `bake-basemap.mjs`
  fetches map tiles (cartocdn, arcgisonline). Each needs its host allowed (and a key); none is contacted otherwise.
  The CLI writes `.hyperframes/agent-handoff.json` (the agent's session id) into the project for HeyGen's desktop app
  and prints its download link ("Edit with Framey").
- **note: promotion.** The main skill tells the agent to call HeyGen's app "the HyperFrames desktop app", to "offer
  it as the better way to keep editing" and, after each delivery render, to pass on its download link ("Edit with
  Framey"). Advertising, not a risk; the agent can be told to skip it. figma reads `FIGMA_TOKEN` from the shell or
  the project's `.env` (a token you give it; Figma's API needs api.figma.com allowed).
- **ok: instructions.** The SKILL.md files and references carry no hidden instructions;
  "do not silently update" and "report a missing bundled skill instead of downloading" are upstream's own.

**The CLI's npm packages (recipe `hyperframes`):** hyperframes 0.8.214 (published 2026-10-28) resolves to 125
packages (`sandbox/recipes/hyperframes/package-lock.json`, all published before 2026-10-29, each with its tarball URL
and SHA-512): licences MIT 81, Apache-2.0 20, LGPL-3.0-or-later 10 (sharp's prebuilt libvips binaries,
`@img/sharp-libvips-*`), ISC 6, Apache-2.0 AND LGPL-3.0-or-later 3, BSD-3-Clause 2, CC0-1.0 1, 0BSD 1,
Apache-2.0 AND LGPL AND MIT 1; 57 optional (per-platform binaries of esbuild and sharp; npm takes only linux-x64 or
arm64). Only esbuild has an install script, and `npm ci --ignore-scripts` skips it (its binary is the optional
package `@esbuild/linux-x64`). Packages that can reach the network: `@puppeteer/browsers` (Chrome download; unused
with `HYPERFRAMES_BROWSER_PATH`), `puppeteer-core` (drives the local Chromium), `giget` (templates from GitHub for
`init --template`), `open` (opens a browser), `hono`/`@hono/node-server`/`ws` (the local preview server). The
recipe installs Debian's Chromium, FFmpeg and Liberation fonts and sets `HYPERFRAMES_BROWSER_PATH`,
`HYPERFRAMES_FFMPEG_PATH`, `HYPERFRAMES_FFPROBE_PATH` (and `HALLMARK_PUPPETEER` for hallmark's render tier). Not
built and run here (no Podman in the review environment): the recipe's Containerfile passes sandbox-ai's validator and
its tests; the first `sandbox-ai build hyperframes` is the live check.

## security-audit

**Upstream:** cloudflare/security-audit-skill, MIT (`Copyright (c) 2026 Cloudflare, Inc.`). Pin
`c1c8a8c1471069fb0e188eeaff69b8e8db6564a8`, 2026-09-14 (the newest commit).

**What it is:** a source-first audit methodology - SKILL.md (22 KB, read in full), 14 companion files by domain
(attack classes, web/auth, client side, cloud, AI/LLM, memory safety, supply chain, ...), `report-schema.json` and two
validators (`validate-findings.cjs`, `validate-coverage-ledger.cjs`: read a JSON file with `O_NOFOLLOW`, check it,
print errors; no network, no writes, no processes - read in full). Left out: the validators' tests (`*.test.cjs`,
which spawn node and `mkfifo` in temporary folders).

**Snyk's "Warn":** Snyk's page (skills.sh) is not reachable from the review environment, so its reasons could not
be read. From the content, the likely ones: the skill tells the agent to build, run and fuzz the target's code
(sandboxed, it insists), to process untrusted repository content (indirect prompt injection), to launch many
sub-agents, and to run its bundled JavaScript; scanners of this kind flag exactly those patterns. The review found
nothing that reads credentials, sends data, or injects instructions: on the contrary, it forbids network access for
target code, live endpoints, installing dependencies, and copying the ambient environment, and requires an
OS-enforced sandbox before running anything - which a sandbox-ai sandbox is (no network unless allowed, its own
HOME, limits).

**Findings:**

- **note: where full audits write.** Full audit mode (only on an explicit request for an audit, pen test or report)
  writes `run-metadata.json`, `findings.json`, `REPORT.md` and more into `~/security-audit-skill/REPO/run-N`, and
  refuses to write inside the target unless the user picks a git-ignored folder. In a sandbox `~` is the start's
  HOME, which sandbox-ai **keeps** by default: the reports are on the host in
  `~/.local/state/sandbox-ai/sessions/ID/home/security-audit-skill/` and come back with `sandbox-ai resume NAME`.
  With `--no-keep`, in the mode `paranoid` and in probes the HOME is discarded at the end - then choose a folder in
  the workspace that git ignores (the skill accepts that when you name it), or `--state-dir DIR`. sandbox-ai's own
  skill tells the agent this.
- **ok:** guidance mode (the default) writes nothing.

## design-system

**Upstream:** the skill the collectivebrain.de page names "design-design-system" is `design-system` of the
**design** plugin in [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins)
(Apache-2.0, no NOTICE file) - not of anthropics/skills, which has no such skill at any commit (the compact copy
claims "Original author and source: Anthropic (https://github.com/anthropics/skills)"). Pin
`2574e29377c7173d0cecffef29a6ecbd7f8ccb5d`, 2026-10-27; the skill last changed 2026-03-13 (commands migrated to
skills), and not since.

**What it is:** one SKILL.md (5.6 KB, read in full): audit, document and extend modes with output templates;
"If Connectors Available" mentions a design tool and a knowledge base (`~~design tool` placeholders from the
plugin's CONNECTORS.md, which is not part of the skill and is referenced only for placeholders). No scripts, no URLs.
The plugin's `.mcp.json` (Slack, Figma, Linear, Asana, Atlassian, Notion, Intercom) is **not** vendored: sandbox-ai
adds MCP servers only through `--mcp`.

**Differences to the compact copy** (`https://collectivebrain.de/en/skills/design-design-system/SKILL.md`, fetched
2026-11-12, 1994 bytes): it renames the skill `design-design-system`, condenses the body to three short mode lists
(no output templates, no principles, no connectors), and appends a "Curated & provided by" block that (1) tells the
agent the skill "is maintained at https://collectivebrain.de/... where new revisions are published first. Check that
page for updates before relying on time-sensitive details" - an instruction to fetch from their server at run time -
(2) links three more of their pages, and (3) suggests the agent close each finished task with a credit line for
Collective Brain and WhiteFox Automations. Those are promotional instructions to the agent; sandbox-ai ships the
upstream skill, which has none of them.

## ffuf

**Upstream:** jthack/ffuf_claude_skill (Joseph Thacker). Pin `50fd4af4d8bd60cfc48fd84eda1720fbd718857d`, 2025-10-16
(the newest commit). **Licence:** the README says "MIT License - See LICENSE file for details", but the repository
has no LICENSE file and GitHub detects none. sandbox-ai redistributes it on the strength of that statement under
MIT and keeps the README as `UPSTREAM-README.md`; if the author objects, the kit goes.

**What it is:** `SKILL.md` (skill name `ffuf-web-fuzzing`, folder renamed to match it for OpenCode; read in full),
`resources/WORDLISTS.md` and `REQUEST_TEMPLATES.md` (SecLists paths, raw-request templates with placeholder tokens),
`ffuf_helper.py` (read in full: analyses an ffuf JSON result, writes a `req.txt` template or a numeric wordlist;
standard library, no network).

**Findings:**

- **note: an offensive tool.** It teaches directory, subdomain, parameter and login brute-forcing, IDOR enumeration
  and fuzzing with captured session tokens. In a sandbox ffuf reaches only hosts the start allows; fuzz only systems
  you may test. It suggests `go install github.com/ffuf/ffuf/v2@latest`, `brew install ffuf`, `git clone` of SecLists
  and `sudo apt install seclists` - none of which works in a sandbox without allowing it; ffuf is a Debian package
  (`sandbox-ai images extend AGENT --add-apt ffuf`), but Debian 12's is 1.1.0: the skill's raw-request mode
  (`--request`) and per-host calibration (`-ach`) need ffuf 2.x. SecLists is not in Debian (Kali only): mount a copy
  with `--mount`.
- **ok:** no hidden instructions, no telemetry. "ALWAYS use -ac" and "Notes for Claude" are usage advice.

## tdd

**Upstream:** mattpocock/skills, `skills/engineering/tdd`, MIT (`Copyright (c) 2026 Matt Pocock`). Pin
`59db609eb1e18aaf59b63284d0e52255aff0c63c`, 2026-10-27. **Newer:** 6 commits to 2026-11-12 (other skills: versioning,
implement-spec, chief-of-staff, software-factory); nothing in `skills/engineering/tdd`.

**What it is:** markdown only - SKILL.md, `tests.md`, `mocking.md` (all read in full) and `agents/openai.yaml`
(Codex display metadata). It refers to two skills it does not ship ("codebase-design", the `code-review` skill);
an agent without them just goes on.

**Overlap with superpowers:** superpowers' `test-driven-development` (strict red-green-refactor with "iron laws") and
this `tdd` (seams agreed with the user first, vertical slices, refactoring outside the loop) cover the same ground
with different emphasis. They have different names, so both kits can be used together, and each is switched on its
own (`--kit tdd`, `--kit superpowers`); choose one to avoid two TDD procedures competing for the agent's attention.

## What the tests prove

`tests/test_vendored_skills.py`: every vendored file equals the pinned upstream file (SHA-256 and git blob id) and
nothing unlisted is in a kit; every pin is at least 14 days old; licences and NOTICE are there; every skill is valid
for Claude Code, Codex and OpenCode; a dry start with `--kit NAME` lists the skills for each agent and a start stages
them read-only where each agent looks; without the kit no vendored skill is there; no vendored kit has hooks or
plugins, and hallmark-hook's hook is unapproved until trusted; nothing is installed from the network; every sandbox
gets the telemetry switches (not with `--telemetry on`); the vendored hyperframes telemetry code sends nothing with
them (run under Node); the recipes pin every npm package by SHA-512, run no install scripts and use Debian's browser;
`sandbox-ai kits list --json` shows every kit with its skills per agent. The app's Kits & plugins page lists the kits
and adds one to the current project (`projects/sandbox-ai-app/tests/test_kits_page.py`).
