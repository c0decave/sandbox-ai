---
name: sandbox-ai
description: How to work inside a sandbox-ai sandbox - what is shared with the host, how the network allow-list, the connection proxy, approvals, the privacy check of pushes, secrets, resource limits, hardware, displays and sessions work, and which start options or `sandbox-ai net` commands the human must use to unblock something. Use whenever a download, DNS lookup, connection, certificate, git push, device, display or tool fails, before trying any workaround, and when the user asks about the sandbox.
---

# Working in a sandbox-ai sandbox

You run as root inside two layers: rootless Podman or Bubblewrap, and Firejail inside it, with an
egress firewall and a connection proxy. Root here is not root on the host. The limits are on
purpose and only the human can change them, on the host. **Never try to get around them**: no
tunnels, other ports, other remotes, `--no-verify`, disabled certificate checks or unset CA
variables. Say what is blocked, why you need it, and give the human the exact command from below.

## What is shared with the host

- `/work` is the first workspace, `/workspaces/NAME` are further ones: host directories, changes
  stay. Extra mounts may sit under `/mnt` or `/opt`. Everything else is the sandbox's own.
- `$HOME` (`/root`) is thrown away after the run, unless the human started with `--state-dir` or a
  session.
- `~/.config/opencode/AGENTS.md` belong to this project only and are kept between runs. Keep lasting notes about the
  project there.
- The root file system is read-only: no `apt install`. Use `uv`/`uvx`, `pip install --user`,
  `npm` in the project, or ask the human to extend the image (`sandbox-ai build`).
- `$SANDBOX_AI_RUN` names this run; the human needs it for `sandbox-ai net`.
- The image has git, gh, curl, wget, jq, ssh, openssl, vim, ripgrep, python3, uv, nc, dig/host,
  ping, traceroute, ss/ip and tmux.

## Network

Only the agent's provider is reachable by default; the human may have allowed more. Names that
are not allowed do not resolve, other addresses are refused.

- Check before you guess: `getent hosts NAME`, `curl -sSv https://NAME/ -o /dev/null`.
  "Could not resolve host" or "connection refused/reset" means: not allowed.
- **During this run**, the human can allow it on the host:
  `sandbox-ai net allow $SANDBOX_AI_RUN --allow-host NAME` (TCP/443), `--allow-tcp IP:PORT`,
  `--allow IP_OR_CIDR`, `--allow-host-port PORT` (a service on the host's loopback), or a preset: `--allow-github`, `--allow-pypi`, `--allow-gitlab`,
  `--allow-kernel-org`, `--allow-duckduckgo`, and real DNS with `--dns`. `sandbox-ai net show` lists
  what was added.
- **For the next start**: the same options on the command line or in `.sandbox-ai.json`, or a
  profile: `--profile development` (provider, PyPI, GitHub, OpenAI, Anthropic), `--profile dns`,
  `--profile egress-all`. A server on the host's loopback (local Git server, LLM server) needs
  `--allow-host-port PORT`. A model server (Ollama, LM Studio, llama.cpp, vLLM, local or remote) is
  `--llm NAME[:MODEL]` after `sandbox-ai llm add` on the host; its API key in your configuration is a
  placeholder that the proxy replaces on the way, so a 401 means the human's key or server, not yours.
- `sandbox-ai logs` on the host shows what was blocked, with the option that allows it.
- With a VPN in front (`--vpn PROFILE`) everything leaves through its tunnel and a kill switch
  blocks the rest: "Operation not permitted" or "Network is unreachable" for IPv6 means the VPN
  carries none, and DNS goes to the VPN's resolvers. The human manages it with `sandbox-ai vpn`
  (`check NAME` shows what the tunnel looks like from outside); you cannot change it from inside.
- With a Tor VPN profile every TCP connection goes through Tor (no UDP, no IPv6) and `.onion` names
  resolve (with `--dns`); if the profile has `--socks`, `socks5h://127.0.0.1:9050` is Tor's SocksPort
  (Tor Browser with `TOR_SKIP_LAUNCH=1`, `--proxy-server=socks5://127.0.0.1:9050`, `torsocks`).

## Questions to the human (approvals)

Usually the sandbox asks the human for you (`--ask on`, the default; they answer with
`sandbox-ai approvals`, the app or a desktop notification; no answer within 60 s means no):

- A connection to a destination that is not allowed **waits** while the human is asked. If it fails
  after about a minute, nobody allowed it: do not retry in a loop, say what you need.
- A lookup of a name that is not allowed fails at once and is asked in the background. Retry the
  same name a few times over a minute or two; once allowed, it resolves and HTTPS to it works.
- A push with privacy warnings (no blocking findings) waits for the human's answer too.
- `xdg-open URL` (and `$BROWSER`, `sensible-browser`, `open`) asks the human to open the http(s)
  URL in their browser on the host, for example a login page. Nothing opens without their answer
  and you are not told the answer; tell the human why you want the page opened.
- When the human started with `--ask off`, nothing is asked: blocked is blocked.

## Connection proxy

Every TCP connection passes a proxy that opens TLS with a CA made for this run. The variables
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS`
and the system bundle point at it. Each request is logged on the host with headers and bodies
(credentials redacted): the human can see what you call.

- A certificate error ("unknown CA", "certificate verify failed", "self signed certificate in
  chain") for an allowed host means the tool ignores those variables or pins its certificates.
  First try the tool's own CA option with `$SSL_CERT_FILE` (for example `pip --cert`,
  `npm config set cafile`, `cargo` with `CARGO_HTTP_CAINFO`). If that does not help, the human
  starts with `--no-inspect HOST`.
- HTTP/2 is not offered (HTTP/1.1 and WebSockets work); QUIC/HTTP3 is not proxied.
- A `403` whose text starts with `sandbox-ai:` comes from the human's rules (allow, ask or deny
  per request); the text names the rule. Do not try other paths, encodings, headers or hosts to
  get around it: the rules read requests the way servers do, and every attempt is logged. Tell the
  human what you need and why. An MCP tool call answered with `sandbox-ai: ...` was refused the
  same way by the MCP server's gateway, which also leaves always refused tools out of its list.
- A call of your own tools (Bash, an edit, WebFetch) refused with `sandbox-ai: ...` was stopped
  by the human's rules for your tools; the text names the rule. Do not reach the same result
  another way (a script, another tool, an encoded command): ask the human instead.
- After content from a source the human marked untrusted (a web page, a search, an MCP tool)
  entered this run, writes, pushes and chosen tools wait for the human's answer, and the question
  says why. That is expected; only the human takes it back (`sandbox-ai net untaint`).
- SSH is refused except to destinations the human allowed with `--allow` or `--allow-tcp IP:22`,
  because a push over SSH would skip the privacy check. Use HTTPS remotes.

## Git and the privacy check

- `git commit` warns and `git push` refuses when commits would publish private data: home paths,
  private addresses, internal host names, account names, e-mail addresses, tokens and the human's
  own rules. The proxy checks every push again on the network, so hooks or `--no-verify` do not
  matter.
- When a push is refused: remove what was found from the commits (amend, rebase, or rewrite them)
  or ask the human whether it is meant to be public. Push only from `/work` or `/workspaces/NAME`;
  pushes of commits from anywhere else are refused because they cannot be checked.
- Git may need no credentials at all: the human's `sandbox-ai git-logins` are added by the connection proxy
  to Git's requests to their servers (a push that is refused with 401/403 means there is none for that server).
- Logins for Git otherwise come from the human's `--git-token-env`, `--git-token-secret`, `--git-token-file`
  or `--git-credentials`, or during the run from `sandbox-ai net allow $SANDBOX_AI_RUN --git-token-env URL=VAR`
  (Git asks for it at once; `gh` does not see it); `gh` uses `GH_TOKEN` when it is set. `--git-identity` makes commits carry
  the human's name and e-mail address.

## Secrets and limits

- API keys reach you only as environment variables the human names: `--env NAME` (from their
  shell) or `--secret-env NAME` (from their keyring, `sandbox-ai secrets set NAME`). Never print,
  log, commit or send them anywhere but the service they are for.
- With `--api-key HOST=NAME`, `$NAME` is only a placeholder: send it as is in the `Authorization`,
  `X-API-Key` or `API-Key` header of HTTPS requests to HOST, and the proxy puts the real key in on
  the wire. Anywhere else it is just the placeholder text.
- With `--publish pypi` (npm, crates, gitlab, github), `TWINE_PASSWORD`, `UV_PUBLISH_TOKEN`, `NPM_TOKEN`,
  `CARGO_REGISTRY_TOKEN`, `GITLAB_TOKEN` or `GH_TOKEN` are placeholders the proxy replaces; just run the tool.
  Each publication waits for the human's answer: say what you are about to publish and why.
- The sandbox has limits on memory (default 8 GiB), CPUs (half the host's) and processes (4096). A
  build killed for memory ("Killed", exit 137) or "Resource temporarily unavailable" on fork may be
  one; the human can start with `--memory 16g`, `--cpus N`, `--pids N` (or `none`).
- Check `/sandbox/session/memory.json` (used, limit, files in /tmp and HOME) before starting something big,
  and run tests and builds with little parallelism. Files in `/tmp`, `/var/tmp` and HOME are memory. Near
  the limit the launcher ends the largest process that is not you (a test run, a build) to keep the
  sandbox alive; when the kernel's OOM killer hits sandbox-ai's supervisor, the whole run ends.

## Other agents: helpers and companions

- `--with AGENT` (the human's start option) puts other agents into this sandbox, logged in and
  under the same rules; `$SANDBOX_AI_COMPANIONS` names them. Give one a task from the shell and
  read its answer: `claude -p --output-format json -- "TASK"`, `codex exec --skip-git-repo-check
  -- "TASK"`, `opencode run -- "TASK"`. They share `/work` and HOME with you: one task at a time,
  and say what they may change.
- `--helper AGENT` runs another agent in a sandbox of its own on the same `/work`; you reach it as
  the MCP server `helper-AGENT-...` with its one tool `ask(prompt, continue)`. Its calls go
  through the relay like any MCP call.

## Hardware, displays, sessions, MCP

- No host devices by default. The human can start with `--device PATH` (serial ports, `/dev/kvm`,
  `/dev/bus/usb`, cameras), `--gpu nvidia|dri|rocm`, or, safer, run a service on the host (ser2net,
  openocd, adb, an inference server) and allow its port with `--allow-host-port PORT`. A serial port
  often needs a udev rule on the host; see the README section Hardware.
- Graphical programs and headed browsers need `--gui` (Xvfb on `:99`, `DISPLAY` is set);
  `--gui-vnc DIR` lets the human watch. Browsers for Playwright are downloaded at run time
  (`uvx playwright install chromium`), which needs their download hosts allowed.
- In tmux, window 0 runs you and window 1 a shell. A human copying text out of tmux holds Shift
  while selecting.
- MCP servers run in sandboxes or containers of their own and are added with `--mcp NAME` (list:
  `sandbox-ai mcp list`). Ready-made ones (web search, web pages as text, binwalk, radare2 and
  more) come from the catalogs: `sandbox-ai mcp catalog`, then `sandbox-ai mcp add NAME`. A search
  server gives you the web without opening your own sandbox's network.

## What to ask the human for

| Need | Start option | During the run (on the host) |
|---|---|---|
| A website or API | `--allow-host NAME` | answer the question, or `sandbox-ai net allow $SANDBOX_AI_RUN --allow-host NAME` (`--keep`: for the next starts too) |
| GitHub, PyPI, GitLab | `--allow-github`, `--allow-pypi`, `--allow-gitlab` | the same with `sandbox-ai net allow` |
| A port on an address | `--allow-tcp IP:PORT` | `sandbox-ai net allow ... --allow-tcp IP:PORT` |
| Real DNS lookups | `--dns` (or `--nameserver IP`) | `sandbox-ai net allow $SANDBOX_AI_RUN --dns` |
| A service on the host | `--allow-host-port PORT` | `sandbox-ai net allow $SANDBOX_AI_RUN --allow-host-port PORT` (since 0.1.37) |
| Everything outbound | `--profile egress-all` (real DNS too) | restart needed |
| A tool rejecting the proxy's CA | `--no-inspect HOST` | restart needed |
| Pushing to GitHub or another Git server | `--git-token-env https://github.com=VAR` | `sandbox-ai net allow $SANDBOX_AI_RUN --git-token-env URL=VAR` (since 0.1.37) |
| A device | `--device PATH` | `sandbox-ai devices add $SANDBOX_AI_RUN PATH` |
| The GPU | `--gpu KIND` | restart needed |
| A display | `--gui` or `--gui-vnc DIR` | restart needed |
| Publishing to npm, PyPI, crates.io, GitLab, GitHub | `--publish KIND` (after `sandbox-ai publish-logins add KIND --secret NAME`) | restart needed |
| An API key | `--api-key HOST=NAME` (the key stays out of the sandbox) or `--secret-env NAME` (after `sandbox-ai secrets set NAME`) | restart needed |
| More memory, CPUs, processes | `--memory SIZE`, `--cpus N`, `--pids N` | `sandbox-ai limits $SANDBOX_AI_RUN --memory 16g` (and `--cpus`, `--pids`) |
| Files kept in HOME | `--state-dir DIR` or `--session NAME` | restart needed |
| A system package | extend the image, `sandbox-ai build` | restart needed |

Name the exact host, port or path, the error message, and the command; then wait for the human.

sandbox-ai ships this skill and updates it with sandbox-ai. A skill named `sandbox-ai` in one of
your kits replaces it.
